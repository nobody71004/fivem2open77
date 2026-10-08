// The Studio window: WebView2 showing the local Studio server.  Falls back to the default
// browser when the WebView2 runtime is missing.
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
        readonly Label status;
        readonly string stateFile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77", "window.txt");

        public MainForm(Backend backend, bool useBrowser)
        {
            this.backend = backend;
            this.useBrowser = useBrowser;
            Text = Program.AppName;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
            BackColor = Color.FromArgb(15, 17, 22);
            ForeColor = Color.FromArgb(230, 233, 240);
            StartPosition = FormStartPosition.CenterScreen;
            MinimumSize = new Size(1000, 680);
            Size = new Size(1440, 920);
            Font = new Font("Segoe UI", 10f);
            status = new Label
            {
                Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter,
                Text = "Starting fivem2open77 Studio...", ForeColor = Color.FromArgb(152, 161, 179),
            };
            Controls.Add(status);
            LoadWindowState();
            Shown += async (s, e) => await StartAsync();
            FormClosing += (s, e) => SaveWindowState();
        }

        async Task StartAsync()
        {
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
            try
            {
                web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(15, 17, 22) };
                string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "fivem2open77", "webview2");
                var env = await CoreWebView2Environment.CreateAsync(null, data);
                Controls.Add(web);
                await web.EnsureCoreWebView2Async(env);
                status.Visible = false;
                web.BringToFront();
                var core = web.CoreWebView2;
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
            status.Text = "fivem2open77 Studio is running in your browser.\n\nKeep this window open while you use it; closing it stops the Studio.\n\n" + backend.Url.Split('?')[0];
            var btn = new Button { Text = "Open in browser", AutoSize = true, Dock = DockStyle.Bottom, Height = 44, FlatStyle = FlatStyle.Flat };
            btn.Click += (s, e) => OpenExternal(backend.Url);
            Controls.Add(btn);
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
