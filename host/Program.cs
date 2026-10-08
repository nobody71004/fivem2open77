// fivem2open77 Studio for Windows: starts the Studio server (embedded Python) and shows it in a
// WebView2 window.  Also installs itself (--install) and uninstalls (--uninstall).
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Windows.Forms;

namespace Fivem2Open77.Studio
{
    static class Program
    {
        public const string AppName = "fivem2open77 Studio";

        [STAThread]
        static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
            Application.ThreadException += (s, e) => Fatal(e.Exception);
            AppDomain.CurrentDomain.UnhandledException += (s, e) => Fatal(e.ExceptionObject as Exception);

            string home = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            bool has(string a) => args.Any(x => string.Equals(x, a, StringComparison.OrdinalIgnoreCase));

            if (has("--install")) return Installer.Install(home, launch: !has("--no-launch"), quiet: has("--quiet"));
            if (has("--uninstall")) return Installer.Uninstall(home, quiet: has("--quiet"));

            // run from a download (not installed yet): offer to install first
            if (!Installer.IsInstalled(home) && !has("--portable") && Installer.OfferInstall(home))
                return 0;

            using (var mutex = new Mutex(true, "fivem2open77-studio-" + Environment.UserName, out bool first))
            {
                if (!first)
                {
                    WindowFinder.ActivateExisting(AppName);
                    return 0;
                }
                using (var backend = new Backend(home))
                {
                    Application.Run(new MainForm(backend, has("--browser")));
                }
            }
            return 0;
        }

        static void Fatal(Exception e)
        {
            try
            {
                string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77");
                Directory.CreateDirectory(dir);
                File.AppendAllText(Path.Combine(dir, "host-errors.log"), DateTime.Now.ToString("s") + " " + e + Environment.NewLine);
            }
            catch { }
            MessageBox.Show("Something went wrong:\n\n" + (e?.Message ?? "unknown error"), AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    static class WindowFinder
    {
        [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
        [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);

        public static void ActivateExisting(string title)
        {
            foreach (var p in Process.GetProcessesByName(Process.GetCurrentProcess().ProcessName))
            {
                if (p.Id == Process.GetCurrentProcess().Id || p.MainWindowHandle == IntPtr.Zero) continue;
                ShowWindow(p.MainWindowHandle, 9);   // SW_RESTORE
                SetForegroundWindow(p.MainWindowHandle);
                return;
            }
        }
    }
}
