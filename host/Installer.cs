// Per-user install (no admin): copies the app to %LOCALAPPDATA%\Programs\fivem2open77, adds Start
// menu and desktop shortcuts and an entry in Settings > Apps.  --uninstall removes all of it but
// keeps the user's projects (%LOCALAPPDATA%\fivem2open77\studio) unless they say otherwise.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using Microsoft.Win32;

namespace Fivem2Open77.Studio
{
    static class Installer
    {
        const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\fivem2open77-studio";
        const string ExeName = "fivem2open77-studio.exe";

        public static string InstallDir =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "fivem2open77");

        static string StartMenuLink =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), Program.AppName + ".lnk");

        static string DesktopLink =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Program.AppName + ".lnk");

        public static bool IsInstalled(string home) =>
            string.Equals(Path.GetFullPath(home).TrimEnd('\\'), Path.GetFullPath(InstallDir).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);

        /// <summary>Run from a download: offer to install.  True when the installed copy was started instead.</summary>
        public static bool OfferInstall(string home)
        {
            // portable use is fine too (e.g. a USB stick): a marker file skips the question
            if (File.Exists(Path.Combine(home, "portable.txt"))) return false;
            var r = MessageBox.Show(
                "Install " + Program.AppName + " on this PC?\n\n" +
                "It goes to " + InstallDir + " (no admin rights needed) with a Start menu and desktop shortcut.\n\n" +
                "Yes: install and start.  No: run it from here this time.",
                Program.AppName, MessageBoxButtons.YesNoCancel, MessageBoxIcon.Question);
            if (r == DialogResult.Cancel) Environment.Exit(0);
            if (r == DialogResult.No) return false;
            return Install(home, launch: true, quiet: false) == 0;
        }

        public static int Install(string home, bool launch, bool quiet)
        {
            string target = InstallDir;
            // while this marker is there, an agent app's MCP bridge (1.5.3 and newer) does not start the Studio
            // it lost when the setup closed it; it waits for the new one
            string marker = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77", "updating");
            try
            {
                if (!IsInstalled(home))
                {
                    try { Directory.CreateDirectory(Path.GetDirectoryName(marker)); File.WriteAllText(marker, Program.Version); } catch { }
                    using (var progress = new ProgressForm("Installing " + Program.AppName + "..."))
                    {
                        progress.Show();
                        Application.DoEvents();
                        progress.Say("Closing the Studio");
                        StopRunning(target);
                        Sweep(target);
                        progress.Say("Installing " + Program.AppName + "...");
                        // the new copy next to the old one first, then into place
                        string staging = target + ".new";
                        if (Directory.Exists(staging))
                        {
                            try { Directory.Delete(staging, true); }
                            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { staging += "-" + DateTime.UtcNow.Ticks.ToString("x"); }
                        }
                        CopyTree(home, staging, progress);
                        Swap(staging, target);
                        try { File.Delete(Path.Combine(target, "portable.txt")); } catch { }
                        // an older bridge starts the old Studio again as soon as it loses it: that one goes too,
                        // or it would keep the window (one per user) and the new copy could not start
                        StopRunning(target);
                    }
                }
                string exe = Path.Combine(target, ExeName);
                Shortcut(StartMenuLink, exe, target);
                Shortcut(DesktopLink, exe, target);
                using (var k = Registry.CurrentUser.CreateSubKey(UninstallKey))
                {
                    k.SetValue("DisplayName", Program.AppName);
                    k.SetValue("DisplayVersion", Program.Version);
                    k.SetValue("Publisher", "fivem2open77");
                    k.SetValue("DisplayIcon", exe);
                    k.SetValue("InstallLocation", target);
                    k.SetValue("UninstallString", "\"" + exe + "\" --uninstall");
                    k.SetValue("QuietUninstallString", "\"" + exe + "\" --uninstall --quiet");
                    k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                    k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                    k.SetValue("EstimatedSize", (int)(DirSize(target) / 1024), RegistryValueKind.DWord);
                }
                if (launch) Process.Start(new ProcessStartInfo(exe) { UseShellExecute = true, WorkingDirectory = target });
                else if (!quiet) MessageBox.Show(Program.AppName + " is installed.", Program.AppName);
                return 0;
            }
            catch (Exception ex)
            {
                if (!quiet) MessageBox.Show("Installation failed:\n\n" + ex.Message + "\n\nClose the Studio and the apps that use its agent " +
                    "connection (Claude, Cursor, VS Code, Codex), then run the setup again.", Program.AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
            finally
            {
                try { File.Delete(marker); } catch { }
            }
        }

        // ---------------------------------------------------------------- replacing a copy that is in use

        /// <summary>Close what runs the Studio from the install folder: its window (and the server in the window's job),
        /// a server an agent app started on its own, and WolvenKit or 7-Zip left from one.  An agent app's MCP bridge
        /// (runtime\python\python.exe) is left running: the app would start it again at once; Swap moves its files aside.</summary>
        static void StopRunning(string target)
        {
            int me = Process.GetCurrentProcess().Id;
            foreach (var p in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(ExeName)))
            {
                if (p.Id == me || !Under(ImagePath(p), target)) continue;
                try
                {
                    p.CloseMainWindow();
                    if (!p.WaitForExit(8000)) { p.Kill(); p.WaitForExit(5000); }
                }
                catch { }
            }
            // the Studio server of the workspace (the one the window ran is gone with it; one an agent started is not)
            try
            {
                string run = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77", "studio", "run", "studio.json");
                if (File.Exists(run))
                {
                    var m = System.Text.RegularExpressions.Regex.Match(File.ReadAllText(run), "\"pid\"\\s*:\\s*(\\d+)");
                    if (m.Success)
                    {
                        var p = Process.GetProcessById(int.Parse(m.Groups[1].Value));
                        if (Under(ImagePath(p), target)) { p.Kill(); p.WaitForExit(5000); }
                    }
                }
            }
            catch { }
            foreach (var name in new[] { "dotnet", "7z" })
                foreach (var p in Process.GetProcessesByName(name))
                    try { if (Under(ImagePath(p), target)) { p.Kill(); p.WaitForExit(3000); } } catch { }
        }

        /// <summary>The new copy into place.  In one move when nothing runs from the old folder; otherwise file by file:
        /// a file in use cannot be replaced or deleted, but it can be renamed, so it is moved aside (deleted by Sweep
        /// on a later start) and the new one takes its place.</summary>
        static void Swap(string staging, string target)
        {
            if (!Directory.Exists(target)) { Directory.Move(staging, target); return; }
            string old = target + ".old";
            bool moved = false;
            try
            {
                if (Directory.Exists(old)) Directory.Delete(old, true);
                Directory.Move(target, old);
                moved = true;
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { }
            if (moved)
            {
                try { Directory.Move(staging, target); }
                catch { try { Directory.Move(old, target); } catch { } throw; }      // never leave no app behind
                try { Directory.Delete(old, true); } catch { }
                return;
            }
            var keep = new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var f in Directory.GetFiles(staging, "*", SearchOption.AllDirectories))
            {
                string rel = f.Substring(staging.Length).TrimStart('\\');
                keep.Add(rel);
                string dst = Path.Combine(target, rel);
                Directory.CreateDirectory(Path.GetDirectoryName(dst));
                if (File.Exists(dst)) Aside(dst, true);
                File.Move(f, dst);
            }
            // what the old version had and this one has not (and stale compiled Python)
            foreach (var f in Directory.GetFiles(target, "*", SearchOption.AllDirectories))
            {
                string rel = f.Substring(target.Length).TrimStart('\\');
                if (!keep.Contains(rel) && rel.IndexOf(AsideTag, StringComparison.OrdinalIgnoreCase) < 0) Aside(f, false);
            }
            RemoveEmptyDirs(target);
            try { Directory.Delete(staging, true); } catch { }
        }

        const string AsideTag = ".f2o77-old-";

        /// <summary>Delete a file, or rename it when it is in use.  must: throw when neither works.</summary>
        static void Aside(string path, bool must)
        {
            try { File.SetAttributes(path, FileAttributes.Normal); } catch { }
            try { File.Delete(path); return; }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { }
            try { File.Move(path, path + AsideTag + DateTime.UtcNow.Ticks.ToString("x")); }
            catch (Exception e) when (!must && (e is IOException || e is UnauthorizedAccessException)) { }
        }

        /// <summary>Delete the files an update moved aside, where nothing uses them any more (and the update's leftovers).</summary>
        public static void Sweep(string target)
        {
            try
            {
                if (!Directory.Exists(target)) return;
                foreach (var f in Directory.EnumerateFiles(target, "*" + AsideTag + "*", SearchOption.AllDirectories))
                {
                    try { File.SetAttributes(f, FileAttributes.Normal); File.Delete(f); } catch { }
                }
                foreach (var d in new[] { target + ".old", target + ".new" })
                    try { if (Directory.Exists(d)) Directory.Delete(d, true); } catch { }
            }
            catch { }
        }

        static void RemoveEmptyDirs(string dir)
        {
            foreach (var d in Directory.GetDirectories(dir))
            {
                RemoveEmptyDirs(d);
                try { if (Directory.GetFileSystemEntries(d).Length == 0) Directory.Delete(d); } catch { }
            }
        }

        static bool Under(string path, string dir) =>
            !string.IsNullOrEmpty(path) && path.StartsWith(dir.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);

        [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
        static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
        static extern bool CloseHandle(IntPtr h);
        [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
        static extern bool QueryFullProcessImageName(IntPtr h, int flags, System.Text.StringBuilder name, ref int size);

        /// <summary>A process's exe path (any bitness, limited rights), or null.</summary>
        static string ImagePath(Process p)
        {
            IntPtr h = OpenProcess(0x1000, false, p.Id);          // PROCESS_QUERY_LIMITED_INFORMATION
            if (h == IntPtr.Zero) return null;
            try
            {
                var sb = new System.Text.StringBuilder(1024);
                int n = sb.Capacity;
                return QueryFullProcessImageName(h, 0, sb, ref n) ? sb.ToString(0, n) : null;
            }
            finally { CloseHandle(h); }
        }

        public static int Uninstall(string home, bool quiet)
        {
            if (!quiet)
            {
                var r = MessageBox.Show("Remove " + Program.AppName + " from this PC?\n\nYour cars and builds in " +
                    Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77", "studio") +
                    " are kept.", Program.AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question);
                if (r != DialogResult.Yes) return 1;
            }
            StopRunning(InstallDir);
            foreach (var l in new[] { StartMenuLink, DesktopLink }) { try { File.Delete(l); } catch { } }
            try { Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false); } catch { }
            // the running exe cannot delete its own folder: a short-lived cmd does it after we exit
            string dir = InstallDir;
            if (Directory.Exists(dir))
            {
                Process.Start(new ProcessStartInfo("cmd.exe", "/c timeout /t 2 /nobreak >nul & rmdir /s /q \"" + dir + "\"")
                { CreateNoWindow = true, UseShellExecute = false, WorkingDirectory = Path.GetTempPath() });
            }
            if (!quiet) MessageBox.Show(Program.AppName + " was removed.", Program.AppName);
            return 0;
        }

        static void CopyTree(string from, string to, ProgressForm progress)
        {
            var files = Directory.GetFiles(from, "*", SearchOption.AllDirectories);
            int i = 0;
            foreach (var f in files)
            {
                string rel = f.Substring(from.Length).TrimStart('\\');
                string dst = Path.Combine(to, rel);
                Directory.CreateDirectory(Path.GetDirectoryName(dst));
                File.Copy(f, dst, true);
                if (++i % 25 == 0) { progress.Set(i * 100 / files.Length); Application.DoEvents(); }
            }
        }

        static long DirSize(string d)
        {
            long n = 0;
            try { foreach (var f in new DirectoryInfo(d).GetFiles("*", SearchOption.AllDirectories)) n += f.Length; } catch { }
            return n;
        }

        static void Shortcut(string link, string target, string workdir)
        {
            Type t = Type.GetTypeFromProgID("WScript.Shell");
            if (t == null) return;
            dynamic shell = Activator.CreateInstance(t);
            dynamic sc = shell.CreateShortcut(link);
            sc.TargetPath = target;
            sc.WorkingDirectory = workdir;
            sc.IconLocation = target + ",0";
            sc.Description = "Convert FiveM cars into Open77 cars";
            sc.Save();
        }
    }

    /// <summary>The installer's window: the HUD start screen with the copy's progress (drag it anywhere).</summary>
    sealed class ProgressForm : Form
    {
        readonly HudSplash hud;

        [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool ReleaseCapture();
        [System.Runtime.InteropServices.DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, IntPtr l);

        public ProgressForm(string text)
        {
            Text = Program.AppName;
            try { Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen;
            ShowInTaskbar = true;
            Size = new System.Drawing.Size(780, 470);
            BackColor = System.Drawing.Color.FromArgb(3, 5, 10);
            hud = new HudSplash { Dock = DockStyle.Fill, Version = Program.Version, Status = text, Progress = 0, Detail = Installer.InstallDir };
            hud.MouseDown += (s, e) =>
            {
                if (e.Button != MouseButtons.Left) return;
                ReleaseCapture();
                SendMessage(Handle, 0xA1, (IntPtr)2, IntPtr.Zero);     // WM_NCLBUTTONDOWN on the caption: move the window
            };
            Controls.Add(hud);
        }

        public void Set(int pct) { hud.Progress = pct; hud.Refresh(); }

        public void Say(string text) { hud.Status = text; hud.Refresh(); Application.DoEvents(); }
    }
}
