// The Studio server process: runtime\python\python.exe -m fivem2open77 studio, kept in a job
// object so it (and WolvenKit / 7-Zip under it) never outlives the window.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace Fivem2Open77.Studio
{
    sealed class Backend : IDisposable
    {
        public readonly string Home;
        public readonly string Python;
        public readonly string Workspace;
        public string LogFile { get; private set; }
        public string Url { get; private set; }
        public int Port { get; private set; }
        public Process Proc { get; private set; }
        public event Action<int> Exited;

        readonly JobObject job = new JobObject();
        StreamWriter log;
        bool disposing;

        public Backend(string home)
        {
            Home = home;
            Python = Path.Combine(home, "runtime", "python", "python.exe");
            Workspace = Environment.GetEnvironmentVariable("F2O77_WORKSPACE");
            if (string.IsNullOrEmpty(Workspace))
                Workspace = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77", "studio");
        }

        /// <summary>Start the server and wait until it listens.  Throws with the log's last lines on failure.</summary>
        public void Start(TimeSpan timeout)
        {
            if (!File.Exists(Python))
                throw new FileNotFoundException("The app is incomplete: " + Python + " is missing. Reinstall fivem2open77 Studio.");
            Directory.CreateDirectory(Path.Combine(Workspace, "logs"));
            CleanOldLogs(Path.Combine(Workspace, "logs"));
            LogFile = Path.Combine(Workspace, "logs", "studio-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".log");
            log = new StreamWriter(new FileStream(LogFile, FileMode.Create, FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false)) { AutoFlush = true };
            string ready = Path.Combine(Path.GetTempPath(), "f2o77-ready-" + Guid.NewGuid().ToString("N") + ".json");
            var psi = new ProcessStartInfo(Python,
                "-X utf8 -m fivem2open77 studio --port 0 --workspace \"" + Workspace + "\" --ready-file \"" + ready + "\"")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = Home,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            psi.EnvironmentVariables["F2O77_HOME"] = Home;
            psi.EnvironmentVariables["PYTHONUTF8"] = "1";
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            psi.EnvironmentVariables["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1";
            psi.EnvironmentVariables["DOTNET_NOLOGO"] = "1";
            Proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
            Proc.OutputDataReceived += (s, e) => { if (e.Data != null) Write(e.Data); };
            Proc.ErrorDataReceived += (s, e) => { if (e.Data != null) Write(e.Data); };
            Proc.Exited += (s, e) => { if (!disposing) Exited?.Invoke(SafeExitCode()); };
            Proc.Start();
            job.Add(Proc);
            Proc.BeginOutputReadLine();
            Proc.BeginErrorReadLine();
            var sw = Stopwatch.StartNew();
            while (sw.Elapsed < timeout)
            {
                if (File.Exists(ready))
                {
                    try
                    {
                        string json = File.ReadAllText(ready);
                        var m = Regex.Match(json, "\"url\"\\s*:\\s*\"([^\"]+)\"");
                        var p = Regex.Match(json, "\"port\"\\s*:\\s*(\\d+)");
                        if (m.Success && p.Success)
                        {
                            Url = m.Groups[1].Value;
                            Port = int.Parse(p.Groups[1].Value);
                            try { File.Delete(ready); } catch { }
                            return;
                        }
                    }
                    catch (IOException) { }
                }
                if (Proc.HasExited)
                    throw new InvalidOperationException("The Studio server stopped while starting (exit " + SafeExitCode() + ").\n\n" + Tail());
                Thread.Sleep(100);
            }
            throw new TimeoutException("The Studio server did not start in " + (int)timeout.TotalSeconds + " s.\n\n" + Tail());
        }

        int SafeExitCode() { try { return Proc.ExitCode; } catch { return -1; } }

        void Write(string line)
        {
            try { lock (this) log?.WriteLine(line); } catch { }
        }

        public string Tail(int lines = 12)
        {
            try
            {
                using (var fs = new FileStream(LogFile, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var r = new StreamReader(fs))
                {
                    var all = r.ReadToEnd().Split('\n');
                    int from = Math.Max(0, all.Length - lines);
                    return string.Join("\n", all, from, all.Length - from).Trim();
                }
            }
            catch { return "(no log)"; }
        }

        static void CleanOldLogs(string dir)
        {
            try
            {
                foreach (var f in new DirectoryInfo(dir).GetFiles("studio-*.log"))
                    if (f.LastWriteTime < DateTime.Now.AddDays(-14)) f.Delete();
            }
            catch { }
        }

        public void Dispose()
        {
            disposing = true;
            try { if (Proc != null && !Proc.HasExited) Proc.Kill(); } catch { }
            job.Dispose();          // kills anything still in the job (WolvenKit, 7-Zip)
            try { log?.Dispose(); } catch { }
        }
    }

    /// <summary>Windows job object with KILL_ON_JOB_CLOSE: closing the handle ends every process in it.</summary>
    sealed class JobObject : IDisposable
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string name);
        [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int cls, IntPtr info, uint len);
        [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);

        [StructLayout(LayoutKind.Sequential)]
        struct BASIC { public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags; public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass, SchedulingClass; }
        [StructLayout(LayoutKind.Sequential)]
        struct IO { public ulong a, b, c, d, e, f; }
        [StructLayout(LayoutKind.Sequential)]
        struct EXTENDED { public BASIC Basic; public IO Io; public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed; }

        IntPtr handle;

        public JobObject()
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            var info = new EXTENDED();
            info.Basic.LimitFlags = 0x2000;          // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            int len = Marshal.SizeOf(typeof(EXTENDED));
            IntPtr p = Marshal.AllocHGlobal(len);
            try
            {
                Marshal.StructureToPtr(info, p, false);
                SetInformationJobObject(handle, 9, p, (uint)len);   // JobObjectExtendedLimitInformation
            }
            finally { Marshal.FreeHGlobal(p); }
        }

        public void Add(Process p) { if (handle != IntPtr.Zero) AssignProcessToJobObject(handle, p.Handle); }

        public void Dispose()
        {
            if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
        }
    }
}
