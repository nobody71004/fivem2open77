// The Studio window: WebView2 showing the local Studio server, behind the HUD start screen until
// the page has loaded.  Falls back to the default browser when the WebView2 runtime is missing.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Fivem2Open77.Studio
{
    sealed class MainForm : Form
    {
        readonly Backend backend;
        readonly bool useBrowser;
        WebView2 web;
        readonly HudSplash splash;
        bool shown;
        readonly string stateFile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77", "window.txt");

        public MainForm(Backend backend, bool useBrowser)
        {
            this.backend = backend;
            this.useBrowser = useBrowser;
            Text = Program.AppName;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
            ForeColor = Color.FromArgb(230, 233, 240);
            StartPosition = FormStartPosition.CenterScreen;
            MinimumSize = new Size(1000, 680);
            Size = new Size(1440, 920);
            Font = new Font("Segoe UI", 10f);
            BackColor = Color.FromArgb(3, 5, 10);
            // the start screen stays over the web view until the Studio's page has loaded
            splash = new HudSplash { Dock = DockStyle.Fill, Version = Program.Version, Status = "Starting the Studio server" };
            Controls.Add(splash);
            LoadWindowState();
            Shown += async (s, e) => await StartAsync();
            FormClosing += (s, e) => SaveWindowState();
        }

        async Task StartAsync()
        {
            splash.Detail = backend.Workspace;
            try
            {
                await Task.Run(() => backend.Start(TimeSpan.FromSeconds(120)));
            }
            catch (Exception ex)
            {
                var r = MessageBox.Show(this, "The Studio could not start:\n\n" + ex.Message + "\n\nOpen the log folder?",
                    Program.AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Error);
                if (r == DialogResult.Yes) Open(Path.GetDirectoryName(backend.LogFile ?? backend.Workspace));
                Close();
                return;
            }
            backend.Exited += code => BeginInvoke((Action)(() =>
            {
                MessageBox.Show(this, "The Studio server stopped (exit " + code + ").\n\n" + backend.Tail(), Program.AppName,
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }));
            if (useBrowser) { BrowserMode(); return; }
            splash.Status = "Loading the interface";
            splash.Detail = "server on 127.0.0.1:" + backend.Port;
            try
            {
                web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(3, 5, 10) };
                string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77", "webview2");
                var env = await CoreWebView2Environment.CreateAsync(null, data);
                Controls.Add(web);
                splash.BringToFront();
                await web.EnsureCoreWebView2Async(env);
                var core = web.CoreWebView2;
                core.NavigationCompleted += (s, e) =>
                {
                    if (shown) return;
                    if (!e.IsSuccess)
                    {
                        splash.Status = "The interface did not load";
                        splash.Detail = e.WebErrorStatus + " - " + backend.Url.Split('?')[0];
                        splash.SetAction("Try again", () => web.Reload());
                        return;
                    }
                    shown = true;
                    splash.Status = "Online";
                    splash.Progress = 100;
                    var hide = new Timer { Interval = 220 };
                    hide.Tick += (s2, e2) =>
                    {
                        hide.Dispose();
                        web.BringToFront();
                        splash.Visible = false;     // its timer stops with it
                        web.Focus();
                    };
                    hide.Start();
                };
                core.Settings.IsStatusBarEnabled = false;
                core.Settings.AreDefaultContextMenusEnabled = true;
                core.Settings.IsZoomControlEnabled = true;
                string origin = "http://127.0.0.1:" + backend.Port;
                core.NewWindowRequested += (s, e) => { e.Handled = true; OpenExternal(e.Uri); };
                core.NavigationStarting += (s, e) =>
                {
                    if (!e.Uri.StartsWith(origin + "/", StringComparison.OrdinalIgnoreCase) && e.Uri != origin)
                    {
                        e.Cancel = true;
                        OpenExternal(e.Uri);
                    }
                };
                core.DocumentTitleChanged += (s, e) => { Text = string.IsNullOrEmpty(core.DocumentTitle) ? Program.AppName : core.DocumentTitle; };
                web.Source = new Uri(backend.Url);
            }
            catch (WebView2RuntimeNotFoundException)
            {
                var r = MessageBox.Show(this,
                    "The Microsoft Edge WebView2 Runtime is not installed, so the Studio opens in your web browser instead.\n\n" +
                    "Install WebView2 now (recommended)? Choose No to just use the browser.",
                    Program.AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Information);
                if (r == DialogResult.Yes) OpenExternal("https://go.microsoft.com/fwlink/p/?LinkId=2124703");
                BrowserMode();
            }
        }

        void BrowserMode()
        {
            splash.Visible = true;
            splash.BringToFront();
            splash.Status = "Running in your browser";
            splash.Progress = 100;
            splash.Detail = "Keep this window open while you use it: closing it stops the Studio.  " + backend.Url.Split('?')[0];
            splash.SetAction("Open in browser", () => OpenExternal(backend.Url));
            OpenExternal(backend.Url);
        }

        static void OpenExternal(string url)
        {
            if (url == null) return;
            if (!url.StartsWith("http://", StringComparison.OrdinalIgnoreCase) && !url.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) return;
            try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); } catch { }
        }

        static void Open(string folder)
        {
            try { Process.Start(new ProcessStartInfo("explorer.exe", "\"" + folder + "\"") { UseShellExecute = true }); } catch { }
        }

        void LoadWindowState()
        {
            try
            {
                var p = File.ReadAllText(stateFile).Split(',');
                var r = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
                if (Screen.AllScreens.Length > 0 && Array.Exists(Screen.AllScreens, s => s.WorkingArea.IntersectsWith(r)))
                {
                    StartPosition = FormStartPosition.Manual;
                    Bounds = r;
                }
                if (p.Length > 4 && p[4] == "max") WindowState = FormWindowState.Maximized;
            }
            catch { }
        }

        void SaveWindowState()
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(stateFile));
                var r = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
                File.WriteAllText(stateFile, $"{r.X},{r.Y},{r.Width},{r.Height},{(WindowState == FormWindowState.Maximized ? "max" : "normal")}");
            }
            catch { }
        }
    }
}
