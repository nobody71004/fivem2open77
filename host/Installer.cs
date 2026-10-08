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
            try
            {
                if (!IsInstalled(home))
                {
                    foreach (var p in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(ExeName)))
                    {
                        if (p.Id == Process.GetCurrentProcess().Id) continue;
                        try { if (p.MainModule.FileName.StartsWith(target, StringComparison.OrdinalIgnoreCase)) { p.CloseMainWindow(); p.WaitForExit(8000); } } catch { }
                    }
                    using (var progress = new ProgressForm("Installing " + Program.AppName + "..."))
                    {
                        progress.Show();
                        Application.DoEvents();
                        // replace the program files, keep nothing else in that folder
                        string staging = target + ".new";
                        if (Directory.Exists(staging)) Directory.Delete(staging, true);
                        CopyTree(home, staging, progress);
                        if (Directory.Exists(target))
                        {
                            string old = target + ".old";
                            if (Directory.Exists(old)) Directory.Delete(old, true);
                            Directory.Move(target, old);
                            try { Directory.Delete(old, true); } catch { }
                        }
                        Directory.Move(staging, target);
                        File.Delete(Path.Combine(target, "portable.txt"));
                    }
                }
                string exe = Path.Combine(target, ExeName);
                Shortcut(StartMenuLink, exe, target);
                Shortcut(DesktopLink, exe, target);
                using (var k = Registry.CurrentUser.CreateSubKey(UninstallKey))
                {
                    k.SetValue("DisplayName", Program.AppName);
                    k.SetValue("DisplayVersion", Application.ProductVersion);
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
                if (!quiet) MessageBox.Show("Installation failed:\n\n" + ex.Message, Program.AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
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

    sealed class ProgressForm : Form
    {
        readonly ProgressBar bar = new ProgressBar { Dock = DockStyle.Bottom, Height = 18, Maximum = 100 };

        public ProgressForm(string text)
        {
            Text = Program.AppName;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterScreen;
            ControlBox = false;
            Size = new System.Drawing.Size(420, 120);
            Controls.Add(new Label { Text = text, Dock = DockStyle.Fill, TextAlign = System.Drawing.ContentAlignment.MiddleCenter });
            Controls.Add(bar);
        }

        public void Set(int pct) => bar.Value = Math.Max(0, Math.Min(100, pct));
    }
}
