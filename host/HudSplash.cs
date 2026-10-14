// The Studio's start screen and the installer's window: the web app's Neural HUD drawn natively
// while the server starts.  A hex lattice that lights up around the cursor, a horizon grid running
// towards you, drifting motes wired to the cursor, a slow scan, the emblem with turning rings and a
// pulse, a decoding title, a segmented progress bar, HUD read-outs in the corners -- and the cursor:
// a turning reticle, a light trail, sparks, click shockwaves that ripple the lattice, and lock
// brackets on the one button it can have.  GDI+, double buffered, about 60 fps, and only while it
// is on screen (the timer stops when it is hidden).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Windows.Forms;

namespace Fivem2Open77.Studio
{
    sealed class HudSplash : Control
    {
        // the web app's tokens (hud.css / fx.js)
        static readonly Color Bg0 = Color.FromArgb(3, 5, 10);
        static readonly Color Bg1 = Color.FromArgb(8, 13, 26);
        static readonly Color A = Color.FromArgb(158, 243, 237);     // accent
        static readonly Color M = Color.FromArgb(255, 79, 163);      // hot
        static readonly Color Y = Color.FromArgb(255, 210, 63);      // signal
        static readonly Color Ink = Color.FromArgb(228, 238, 248);
        static readonly Color Dim = Color.FromArgb(112, 132, 160);

        const float HexR = 21f;                 // lattice cell: centre to corner
        const float CursorReach = 210f;         // the lattice lights up this far round the cursor
        const float TrailLife = 0.38f;
        const float WaveLife = 1.25f;
        const string Glyphs = "#%&*+=<>/\\|01ABCDEFXZ";

        string status = "", detail = "", actionLabel;
        Action action;
        int progress = -1;
        float statusAt;

        public string Version { get; set; } = "";
        public string Caption { get; set; } = "fivem2open77";
        public string Tagline { get; set; } = "STUDIO";

        /// <summary>What is happening now, e.g. "Starting the Studio server".</summary>
        public string Status { get => status; set { if ((value ?? "") != status) { status = value ?? ""; statusAt = Now; } } }

        /// <summary>A second, quieter line (a path, a port, an error hint).</summary>
        public string Detail { get => detail; set => detail = value ?? ""; }

        /// <summary>-1: working (a packet runs along the bar); 0..100: how much is done.</summary>
        public int Progress { get => progress; set => progress = Math.Max(-1, Math.Min(100, value)); }

        /// <summary>One button drawn in the HUD (null label: none).</summary>
        public void SetAction(string label, Action onClick) { actionLabel = label; action = onClick; }

        readonly Timer timer = new Timer { Interval = 15 };
        readonly Stopwatch clock = Stopwatch.StartNew();
        float Now => (float)clock.Elapsed.TotalSeconds;
        float lastStep;

        PointF mouse = new PointF(-9999, -9999), reticle = new PointF(-9999, -9999);
        bool inside;
        RectangleF actionRect = RectangleF.Empty;
        RectangleF lockBox = RectangleF.Empty;
        float lockAlpha;

        struct TrailPoint { public PointF P; public float T; }
        struct Spark { public PointF P, V; public float T, Life; public Color C; }
        struct Wave { public PointF P; public float T; public float Rot; }
        struct Mote { public float X, Y, Speed, Size, Phase; }

        readonly List<TrailPoint> trail = new List<TrailPoint>();
        readonly List<Spark> sparks = new List<Spark>();
        readonly List<Wave> waves = new List<Wave>();
        readonly Random rnd = new Random(2077);
        Mote[] motes = new Mote[0];
        PointF[] hexes = new PointF[0];
        readonly PointF[] hexUnit = new PointF[6];
        Bitmap backdrop;                         // gradient, vignette and the dim lattice, per size
        Font titleFont, tagFont, monoFont, monoSmall, glyphFont;

        public HudSplash()
        {
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint |
                     ControlStyles.ResizeRedraw | ControlStyles.Selectable, true);
            BackColor = Bg0;
            for (int i = 0; i < 6; i++)
            {
                double a = Math.PI / 6 + i * Math.PI / 3;           // pointy-top hexagon
                hexUnit[i] = new PointF((float)Math.Cos(a), (float)Math.Sin(a));
            }
            titleFont = new Font("Segoe UI Light", 30f, FontStyle.Regular, GraphicsUnit.Point);
            tagFont = new Font("Consolas", 10.5f, FontStyle.Bold, GraphicsUnit.Point);
            monoFont = new Font("Consolas", 10f, FontStyle.Regular, GraphicsUnit.Point);
            monoSmall = new Font("Consolas", 8.5f, FontStyle.Regular, GraphicsUnit.Point);
            glyphFont = new Font("Segoe UI Semibold", 30f, FontStyle.Regular, GraphicsUnit.Point);
            timer.Tick += (s, e) => { Step(); Invalidate(); };
        }

        protected override void OnVisibleChanged(EventArgs e)
        {
            base.OnVisibleChanged(e);
            timer.Enabled = Visible;
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            timer.Enabled = Visible;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                timer.Dispose();
                backdrop?.Dispose();
                titleFont.Dispose(); tagFont.Dispose(); monoFont.Dispose(); monoSmall.Dispose(); glyphFont.Dispose();
            }
            base.Dispose(disposing);
        }

        // ------------------------------------------------------------------ input
        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            var p = new PointF(e.X, e.Y);
            if (!inside) { reticle = p; inside = true; }
            float dx = p.X - mouse.X, dy = p.Y - mouse.Y;
            float speed = (float)Math.Sqrt(dx * dx + dy * dy);
            mouse = p;
            trail.Add(new TrailPoint { P = p, T = Now });
            if (trail.Count > 64) trail.RemoveAt(0);
            // sparks shed by a fast cursor
            int n = speed > 60 ? 3 : speed > 18 ? 1 : 0;
            for (int i = 0; i < n; i++) AddSpark(p, 0.6f + (float)rnd.NextDouble() * 1.6f, rnd.Next(3) == 0 ? M : A);
            Cursor = actionRect.Contains(p) ? Cursors.Hand : Cursors.Default;
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            base.OnMouseLeave(e);
            inside = false;
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            var p = new PointF(e.X, e.Y);
            waves.Add(new Wave { P = p, T = Now, Rot = (float)(rnd.NextDouble() * Math.PI) });
            for (int i = 0; i < 18; i++) AddSpark(p, 1.5f + (float)rnd.NextDouble() * 3.5f, i % 3 == 0 ? Y : i % 3 == 1 ? M : A);
            if (e.Button == MouseButtons.Left && action != null && actionRect.Contains(p)) action();
        }

        void AddSpark(PointF p, float speed, Color c)
        {
            double a = rnd.NextDouble() * Math.PI * 2;
            sparks.Add(new Spark
            {
                P = p, V = new PointF((float)Math.Cos(a) * speed * 60, (float)Math.Sin(a) * speed * 60),
                T = Now, Life = 0.35f + (float)rnd.NextDouble() * 0.45f, C = c,
            });
            if (sparks.Count > 220) sparks.RemoveAt(0);
        }

        // ------------------------------------------------------------------ the simulation
        void Step()
        {
            float now = Now, dt = Math.Min(0.05f, now - lastStep);
            lastStep = now;
            for (int i = sparks.Count - 1; i >= 0; i--)
            {
                var s = sparks[i];
                if (now - s.T > s.Life) { sparks.RemoveAt(i); continue; }
                s.P = new PointF(s.P.X + s.V.X * dt, s.P.Y + s.V.Y * dt);
                float damp = (float)Math.Pow(0.025, dt);
                s.V = new PointF(s.V.X * damp, s.V.Y * damp + 140 * dt);
                sparks[i] = s;
            }
            trail.RemoveAll(t => now - t.T > TrailLife);
            waves.RemoveAll(w => now - w.T > WaveLife);
            float k = 1f - (float)Math.Pow(0.0002, dt);
            reticle = new PointF(reticle.X + (mouse.X - reticle.X) * k, reticle.Y + (mouse.Y - reticle.Y) * k);
            for (int i = 0; i < motes.Length; i++)
            {
                motes[i].Y -= motes[i].Speed * dt;
                motes[i].X += (float)Math.Sin(now * 0.6 + motes[i].Phase) * 6 * dt;
                if (motes[i].Y < -8) { motes[i].Y = Height + 8; motes[i].X = (float)rnd.NextDouble() * Width; }
            }
            // the lock brackets ease onto the button under the cursor, or shrink back to the reticle
            bool locking = inside && !actionRect.IsEmpty && actionRect.Contains(mouse);
            RectangleF target = locking ? RectangleF.Inflate(actionRect, 5, 5) : new RectangleF(reticle.X - 12, reticle.Y - 12, 24, 24);
            if (lockBox.IsEmpty) lockBox = target;
            float e = locking ? 0.3f : 0.35f;
            lockBox = new RectangleF(lockBox.X + (target.X - lockBox.X) * e, lockBox.Y + (target.Y - lockBox.Y) * e,
                                     lockBox.Width + (target.Width - lockBox.Width) * e, lockBox.Height + (target.Height - lockBox.Height) * e);
            lockAlpha += ((locking ? 1f : 0f) - lockAlpha) * 0.25f;
        }

        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            if (Width < 8 || Height < 8) return;
            var list = new List<PointF>();
            float w = (float)(Math.Sqrt(3) * HexR), h = 1.5f * HexR;
            for (int row = -1; row * h < Height + h; row++)
                for (int col = -1; col * w < Width + w; col++)
                    list.Add(new PointF(col * w + ((row & 1) != 0 ? w / 2 : 0), row * h));
            hexes = list.ToArray();
            if (motes.Length == 0)
            {
                motes = new Mote[90];
                for (int i = 0; i < motes.Length; i++)
                    motes[i] = new Mote
                    {
                        X = (float)rnd.NextDouble() * Width, Y = (float)rnd.NextDouble() * Height,
                        Speed = 6 + (float)rnd.NextDouble() * 22, Size = 1f + (float)rnd.NextDouble() * 1.6f, Phase = (float)rnd.NextDouble() * 6.28f,
                    };
            }
            BuildBackdrop();
        }

        void BuildBackdrop()
        {
            backdrop?.Dispose();
            backdrop = new Bitmap(Width, Height);
            using (var g = Graphics.FromImage(backdrop))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias;
                using (var b = new LinearGradientBrush(new Rectangle(0, 0, Width, Height), Bg1, Bg0, 90f))
                    g.FillRectangle(b, 0, 0, Width, Height);
                DrawNebula(g, 0f);
                using (var pen = new Pen(Color.FromArgb(14, A), 1f))
                    foreach (var c in hexes) g.DrawPolygon(pen, Hex(c, HexR - 1.5f));
                // vignette
                using (var path = new GraphicsPath())
                {
                    path.AddEllipse(-Width * 0.25f, -Height * 0.3f, Width * 1.5f, Height * 1.6f);
                    using (var pg = new PathGradientBrush(path))
                    {
                        pg.CenterColor = Color.FromArgb(0, 0, 0, 0);
                        pg.SurroundColors = new[] { Color.FromArgb(200, 0, 0, 0) };
                        pg.FocusScales = new PointF(0.55f, 0.5f);
                        g.FillRectangle(pg, 0, 0, Width, Height);
                    }
                }
            }
        }

        PointF[] Hex(PointF c, float r) => Hex(c, r, new PointF[6]);

        PointF[] Hex(PointF c, float r, PointF[] into)
        {
            for (int i = 0; i < 6; i++) into[i] = new PointF(c.X + hexUnit[i].X * r, c.Y + hexUnit[i].Y * r);
            return into;
        }

        readonly PointF[] hexBuf = new PointF[6];

        static Color Mix(Color a, Color b, float t)
        {
            t = Math.Max(0, Math.Min(1, t));
            return Color.FromArgb((int)(a.R + (b.R - a.R) * t), (int)(a.G + (b.G - a.G) * t), (int)(a.B + (b.B - a.B) * t));
        }

        static Color Alpha(Color c, float a) => Color.FromArgb(Math.Max(0, Math.Min(255, (int)(a * 255))), c);

        // ------------------------------------------------------------------ drawing
        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            if (Width < 240 || Height < 240) { g.Clear(Bg0); return; }       // minimised or tiny: nothing to show
            if (backdrop == null || backdrop.Width != Width || backdrop.Height != Height) OnResize(EventArgs.Empty);
            if (backdrop == null) return;
            float t = Now;
            g.DrawImageUnscaled(backdrop, 0, 0);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            g.PixelOffsetMode = PixelOffsetMode.HighQuality;

            var center = new PointF(Width / 2f, Height * 0.39f);
            // the emblem leans towards the cursor
            if (inside) center = new PointF(center.X + (reticle.X - center.X) * 0.018f, center.Y + (reticle.Y - center.Y) * 0.018f);

            DrawHorizon(g, t);
            DrawLattice(g, t);
            DrawMotes(g);
            DrawScan(g, t);
            DrawEmblem(g, center, t);
            float y = DrawTitle(g, center.Y + 118, t);
            DrawStatus(g, y + 22, t);
            DrawFrame(g, t);
            DrawCursor(g, t);
        }

        // drawn once into the backdrop: a path-gradient fill over half the window each frame would be
        // the slowest thing on the screen
        void DrawNebula(Graphics g, float t)
        {
            void blob(float cx, float cy, float r, Color c, int alpha)
            {
                using (var path = new GraphicsPath())
                {
                    path.AddEllipse(cx - r, cy - r, r * 2, r * 2);
                    using (var pg = new PathGradientBrush(path))
                    {
                        pg.CenterColor = Color.FromArgb(alpha, c);
                        pg.SurroundColors = new[] { Color.FromArgb(0, c) };
                        g.FillEllipse(pg, cx - r, cy - r, r * 2, r * 2);
                    }
                }
            }
            const float px = 0, py = 0;
            blob(Width * 0.28f + (float)Math.Sin(t * 0.11) * 60 - px, Height * 0.30f + (float)Math.Cos(t * 0.13) * 40 - py, Width * 0.36f, M, 26);
            blob(Width * 0.74f + (float)Math.Cos(t * 0.09) * 70 - px, Height * 0.42f + (float)Math.Sin(t * 0.12) * 50 - py, Width * 0.40f, A, 22);
            blob(Width * 0.5f, Height * 0.39f, 240, A, 18);
        }

        void DrawHorizon(Graphics g, float t)
        {
            float hy = Height * 0.72f, vx = Width / 2f + (inside ? (reticle.X - Width / 2f) * -0.04f : 0);
            g.SetClip(new RectangleF(0, hy, Width, Height - hy));
            // lines running towards you
            float phase = (t * 0.32f) % 1f;
            for (int i = 0; i < 18; i++)
            {
                float k = (i + phase) / 18f;
                float y = hy + (Height - hy) * (float)Math.Pow(k, 2.3);
                using (var pen = new Pen(Alpha(Mix(M, A, k), 0.05f + 0.32f * k), 1f))
                    g.DrawLine(pen, 0, y, Width, y);
            }
            // lines to the vanishing point
            for (int i = -24; i <= 24; i++)
            {
                float bx = vx + i * Width / 13f;
                using (var pen = new Pen(Alpha(A, 0.14f - Math.Abs(i) * 0.004f), 1f))
                    g.DrawLine(pen, vx + i * 6f, hy, bx, Height);
            }
            g.ResetClip();
            // the horizon itself
            using (var b = new LinearGradientBrush(new RectangleF(0, hy - 60, Width, 61), Color.FromArgb(0, A), Color.FromArgb(34, A), 90f))
                g.FillRectangle(b, 0, hy - 60, Width, 60);
            using (var b = new LinearGradientBrush(new RectangleF(0, hy, Width, 2), Color.FromArgb(0, A), Color.FromArgb(0, A), 0f))
            {
                var blend = new ColorBlend
                {
                    Colors = new[] { Color.FromArgb(0, A), Color.FromArgb(200, A), Color.FromArgb(230, M), Color.FromArgb(200, A), Color.FromArgb(0, A) },
                    Positions = new[] { 0f, 0.3f, 0.5f, 0.7f, 1f },
                };
                b.InterpolationColors = blend;
                g.FillRectangle(b, 0, hy - 0.5f, Width, 1.5f);
            }
        }

        void DrawLattice(Graphics g, float t)
        {
            float now = t;
            using (var pen = new Pen(A, 1.2f))
            using (var fill = new SolidBrush(A))
            {
                foreach (var c in hexes)
                {
                    float k = 0f;
                    Color col = A;
                    if (inside)
                    {
                        float dx = c.X - reticle.X, dy = c.Y - reticle.Y;
                        float d = (float)Math.Sqrt(dx * dx + dy * dy);
                        if (d < CursorReach) { float q = 1 - d / CursorReach; k = q * q; }
                    }
                    foreach (var w in waves)
                    {
                        float age = (now - w.T) / WaveLife;
                        float r = 20 + 520 * (1 - (float)Math.Pow(1 - age, 2.2));
                        float dx = c.X - w.P.X, dy = c.Y - w.P.Y;
                        float d = Math.Abs((float)Math.Sqrt(dx * dx + dy * dy) - r);
                        if (d < 34)
                        {
                            float q = (1 - d / 34) * (1 - age);
                            if (q > k) { k = q; col = Mix(A, M, 0.55f); }
                        }
                    }
                    // a slow breathing shimmer across the lattice
                    float shimmer = 0.5f + 0.5f * (float)Math.Sin(c.X * 0.011 + c.Y * 0.017 - now * 1.3);
                    k = Math.Max(k, shimmer * shimmer * shimmer * 0.12f);
                    if (k < 0.02f) continue;
                    pen.Color = Alpha(col, 0.75f * k);
                    g.DrawPolygon(pen, Hex(c, HexR - 1.5f, hexBuf));
                    if (k > 0.25f)
                    {
                        fill.Color = Alpha(col, 0.09f * k);
                        g.FillPolygon(fill, Hex(c, HexR - 3f, hexBuf));
                    }
                }
            }
        }

        void DrawMotes(Graphics g)
        {
            using (var b = new SolidBrush(A))
            using (var pen = new Pen(A, 1f))
            {
                var near = new List<int>();
                for (int i = 0; i < motes.Length; i++)
                {
                    var m = motes[i];
                    b.Color = Alpha(A, 0.18f + m.Size * 0.12f);
                    g.FillEllipse(b, m.X - m.Size, m.Y - m.Size, m.Size * 2, m.Size * 2);
                    if (inside)
                    {
                        float dx = m.X - reticle.X, dy = m.Y - reticle.Y;
                        if (dx * dx + dy * dy < 180 * 180) near.Add(i);
                    }
                }
                // neural links: the motes near the cursor wired to it and to each other
                for (int a = 0; a < near.Count; a++)
                {
                    var p = motes[near[a]];
                    float d = (float)Math.Sqrt((p.X - reticle.X) * (p.X - reticle.X) + (p.Y - reticle.Y) * (p.Y - reticle.Y));
                    float k = 1 - d / 180;
                    pen.Color = Alpha(A, 0.4f * k);
                    g.DrawLine(pen, reticle.X, reticle.Y, p.X, p.Y);
                    for (int c = a + 1; c < near.Count; c++)
                    {
                        var q = motes[near[c]];
                        float dd = (float)Math.Sqrt((p.X - q.X) * (p.X - q.X) + (p.Y - q.Y) * (p.Y - q.Y));
                        if (dd < 110)
                        {
                            pen.Color = Alpha(M, 0.35f * k * (1 - dd / 110));
                            g.DrawLine(pen, p.X, p.Y, q.X, q.Y);
                        }
                    }
                    b.Color = Alpha(A, 0.9f * k);
                    g.FillRectangle(b, p.X - 1.5f, p.Y - 1.5f, 3, 3);
                }
            }
        }

        void DrawScan(Graphics g, float t)
        {
            float period = 5.5f, y = ((t % period) / period) * (Height + 160) - 80;
            var r = new RectangleF(0, y - 70, Width, 70);
            using (var b = new LinearGradientBrush(r, Color.FromArgb(0, A), Color.FromArgb(20, A), 90f))
                g.FillRectangle(b, r);
            using (var pen = new Pen(Color.FromArgb(70, A), 1f))
                g.DrawLine(pen, 0, y, Width, y);
        }

        void DrawEmblem(Graphics g, PointF c, float t)
        {
            // pulses leaving the core
            for (int i = 0; i < 2; i++)
            {
                float age = ((t + i * 1.1f) % 2.2f) / 2.2f;
                float r = 56 + age * 120;
                using (var pen = new Pen(Alpha(i == 0 ? A : M, 0.45f * (1 - age)), 1.5f))
                    g.DrawEllipse(pen, c.X - r, c.Y - r, r * 2, r * 2);
            }
            // tick ring
            var state = g.Save();
            g.TranslateTransform(c.X, c.Y);
            g.RotateTransform(-t * 6f);
            using (var pen = new Pen(Alpha(A, 0.35f), 1f))
            using (var strong = new Pen(Alpha(A, 0.8f), 1.4f))
            {
                for (int i = 0; i < 72; i++)
                {
                    double a = i * Math.PI * 2 / 72;
                    float r0 = i % 6 == 0 ? 100 : 104, r1 = 109;
                    g.DrawLine(i % 6 == 0 ? strong : pen, (float)Math.Cos(a) * r0, (float)Math.Sin(a) * r0, (float)Math.Cos(a) * r1, (float)Math.Sin(a) * r1);
                }
            }
            g.Restore(state);
            using (var pen = new Pen(Alpha(A, 0.22f), 1f))
                g.DrawEllipse(pen, c.X - 92, c.Y - 92, 184, 184);
            // turning arcs: three fast accents outside, two hot ones inside
            using (var pen = new Pen(Alpha(A, 0.95f), 2.2f) { StartCap = LineCap.Round, EndCap = LineCap.Round })
                for (int i = 0; i < 3; i++) g.DrawArc(pen, c.X - 92, c.Y - 92, 184, 184, t * 48f + i * 120f, 46f);
            using (var glow = new Pen(Alpha(A, 0.18f), 7f))
                for (int i = 0; i < 3; i++) g.DrawArc(glow, c.X - 92, c.Y - 92, 184, 184, t * 48f + i * 120f, 46f);
            using (var pen = new Pen(Alpha(M, 0.9f), 3f) { StartCap = LineCap.Round, EndCap = LineCap.Round })
                for (int i = 0; i < 2; i++) g.DrawArc(pen, c.X - 74, c.Y - 74, 148, 148, -t * 75f + i * 180f, 78f);
            using (var pen = new Pen(Alpha(Y, 0.7f), 1.4f))
                g.DrawArc(pen, c.X - 82, c.Y - 82, 164, 164, t * 130f, 18f);
            // the core: a hexagon with a gradient rim and a glow
            var hex = Hex(c, 54);
            using (var b = new LinearGradientBrush(new RectangleF(c.X - 54, c.Y - 54, 108, 108), Color.FromArgb(235, 10, 18, 34), Color.FromArgb(235, 22, 10, 30), 60f))
                g.FillPolygon(b, hex);
            foreach (var (w, a) in new[] { (11f, 0.06f), (6f, 0.14f), (3f, 0.3f) })
                using (var pen = new Pen(Alpha(A, a), w) { LineJoin = LineJoin.Round })
                    g.DrawPolygon(pen, hex);
            using (var rim = new LinearGradientBrush(new RectangleF(c.X - 54, c.Y - 54, 108, 108), A, M, 45f + t * 30f))
            using (var pen = new Pen(rim, 2f) { LineJoin = LineJoin.Round })
                g.DrawPolygon(pen, hex);
            using (var pen = new Pen(Alpha(A, 0.25f), 1f))
                g.DrawPolygon(pen, Hex(c, 44));
            // the glyph, with a glow
            using (var path = new GraphicsPath())
            {
                var fmt = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                float em = g.DpiY * glyphFont.SizeInPoints / 72f;
                var style = glyphFont.FontFamily.IsStyleAvailable(FontStyle.Regular) ? FontStyle.Regular : glyphFont.Style;
                path.AddString("F", glyphFont.FontFamily, (int)style, em, new RectangleF(c.X - 50, c.Y - 52, 100, 104), fmt);
                foreach (var (w, a) in new[] { (9f, 0.08f), (5f, 0.16f), (2.5f, 0.32f) })
                    using (var pen = new Pen(Alpha(A, a), w) { LineJoin = LineJoin.Round })
                        g.DrawPath(pen, path);
                using (var b = new LinearGradientBrush(new RectangleF(c.X - 30, c.Y - 30, 60, 60), Ink, A, 90f))
                    g.FillPath(b, path);
            }
            // a glint running round the core
            double ga = t * 2.4;
            var gp = new PointF(c.X + (float)Math.Cos(ga) * 54, c.Y + (float)Math.Sin(ga) * 54);
            using (var b = new SolidBrush(Alpha(Ink, 0.9f)))
                g.FillEllipse(b, gp.X - 2.2f, gp.Y - 2.2f, 4.4f, 4.4f);
        }

        float DrawTitle(Graphics g, float y, float t)
        {
            // the title decodes: each letter flickers through glyphs before it settles
            var sb = new System.Text.StringBuilder();
            for (int i = 0; i < Caption.Length; i++)
            {
                float at = 0.25f + i * 0.055f;
                if (t >= at + 0.25f || Caption[i] == ' ') sb.Append(Caption[i]);
                else if (t < at) sb.Append(' ');
                else sb.Append(Glyphs[(int)(t * 40 + i * 7) % Glyphs.Length]);
            }
            string s = sb.ToString();
            var size = g.MeasureString(Caption, titleFont);
            float x = Width / 2f - size.Width / 2f;
            using (var b = new SolidBrush(Alpha(A, 0.12f)))
                g.DrawString(s, titleFont, b, x + 1.5f, y + 1.5f);
            using (var b = new SolidBrush(Ink))
                g.DrawString(s, titleFont, b, x, y);
            // S T U D I O, letter-spaced, in the accent
            float ty = y + size.Height - 2;
            float spacing = 7f, tw = 0;
            var typo = StringFormat.GenericTypographic;
            foreach (char ch in Tagline) tw += g.MeasureString(ch.ToString(), tagFont, 100, typo).Width + spacing;
            tw -= spacing;
            float tx = Width / 2f - tw / 2f;
            using (var b = new SolidBrush(Alpha(A, Math.Min(1f, Math.Max(0f, (t - 0.9f) * 2.5f)))))
                foreach (char ch in Tagline)
                {
                    g.DrawString(ch.ToString(), tagFont, b, tx, ty, typo);
                    tx += g.MeasureString(ch.ToString(), tagFont, 100, typo).Width + spacing;
                }
            // hairlines either side of the tagline
            using (var pen = new Pen(Alpha(A, 0.35f), 1f))
            {
                float mid = ty + 9, half = tw / 2f + 16;
                g.DrawLine(pen, Width / 2f - half - 90, mid, Width / 2f - half, mid);
                g.DrawLine(pen, Width / 2f + half, mid, Width / 2f + half + 90, mid);
            }
            return ty + 22;
        }

        void DrawStatus(Graphics g, float y, float t)
        {
            int dots = (int)((t - statusAt) * 3) % 4;
            string line = "// " + (Status ?? "").ToUpperInvariant() + new string('.', dots);
            var fmt = new StringFormat { Alignment = StringAlignment.Center };
            using (var b = new SolidBrush(Alpha(Ink, 0.92f)))
                g.DrawString(line, monoFont, b, new RectangleF(0, y, Width, 22), fmt);
            // the progress bar: segments, a packet when the length of the work is unknown
            const int segs = 44;
            float seg = 8f, gap = 2.4f, bw = segs * (seg + gap) - gap, bx = Width / 2f - bw / 2f, by = y + 30;
            using (var b = new SolidBrush(A))
            {
                for (int i = 0; i < segs; i++)
                {
                    float f = (i + 0.5f) / segs, k;
                    if (Progress < 0)
                    {
                        float head = ((t * 0.55f) % 1.35f) - 0.15f;
                        float d = (f - head) / 0.09f;
                        k = 0.10f + 0.9f * (float)Math.Exp(-d * d);
                    }
                    else
                    {
                        float done = Progress / 100f;
                        k = f <= done ? 0.75f : 0.1f;
                        if (f <= done && f > done - 1.5f / segs) k = 1f;
                    }
                    b.Color = Alpha(Mix(A, M, f * 0.7f), k);
                    g.FillRectangle(b, bx + i * (seg + gap), by, seg, 5f);
                }
            }
            using (var pen = new Pen(Alpha(A, 0.3f), 1f))
            {
                g.DrawLine(pen, bx - 10, by - 4, bx - 10, by + 9);
                g.DrawLine(pen, bx + bw + 10, by - 4, bx + bw + 10, by + 9);
            }
            if (Progress >= 0)
                using (var b = new SolidBrush(Alpha(A, 0.9f)))
                    g.DrawString(Progress + "%", monoSmall, b, bx + bw + 16, by - 5);
            float ny = by + 18;
            if (!string.IsNullOrEmpty(Detail))
            {
                using (var b = new SolidBrush(Dim))
                    g.DrawString(Detail, monoSmall, b, new RectangleF(40, ny, Width - 80, 60), fmt);
                ny += 34;
            }
            // the one button
            actionRect = RectangleF.Empty;
            if (!string.IsNullOrEmpty(actionLabel))
            {
                var sz = g.MeasureString(actionLabel.ToUpperInvariant(), monoFont);
                var r = new RectangleF(Width / 2f - sz.Width / 2f - 22, ny + 4, sz.Width + 44, 34);
                actionRect = r;
                bool hot = inside && r.Contains(mouse);
                using (var b = new LinearGradientBrush(r, Alpha(A, hot ? 0.95f : 0.8f), Alpha(Mix(A, M, 0.35f), hot ? 0.95f : 0.75f), 0f))
                    g.FillRectangle(b, r);
                // a shine sweeping across it
                float sx = r.X + ((t * 0.8f) % 1.6f) * r.Width * 1.6f - r.Width * 0.4f;
                var shine = new RectangleF(sx, r.Y, 26, r.Height);
                g.SetClip(r);
                using (var b = new LinearGradientBrush(shine, Color.FromArgb(0, 255, 255, 255), Color.FromArgb(90, 255, 255, 255), 0f) { WrapMode = WrapMode.TileFlipX })
                    g.FillRectangle(b, shine);
                g.ResetClip();
                using (var b = new SolidBrush(Color.FromArgb(3, 20, 23)))
                    g.DrawString(actionLabel.ToUpperInvariant(), monoFont, b, new RectangleF(r.X, r.Y + 8, r.Width, r.Height), fmt);
            }
        }

        void DrawFrame(Graphics g, float t)
        {
            float m = 16, L = 26;
            using (var pen = new Pen(Alpha(A, 0.6f), 1.5f))
            {
                g.DrawLines(pen, new[] { new PointF(m, m + L), new PointF(m, m), new PointF(m + L, m) });
                g.DrawLines(pen, new[] { new PointF(Width - m - L, m), new PointF(Width - m, m), new PointF(Width - m, m + L) });
                g.DrawLines(pen, new[] { new PointF(m, Height - m - L), new PointF(m, Height - m), new PointF(m + L, Height - m) });
                g.DrawLines(pen, new[] { new PointF(Width - m - L, Height - m), new PointF(Width - m, Height - m), new PointF(Width - m, Height - m - L) });
            }
            // a light running along the top hairline
            using (var b = new LinearGradientBrush(new RectangleF(m + L + 10, m - 1, Width - 2 * (m + L + 10), 2), Color.FromArgb(0, A), Color.FromArgb(0, A), 0f))
            {
                float head = (t * 0.25f) % 1f;
                b.InterpolationColors = new ColorBlend
                {
                    Colors = new[] { Color.FromArgb(10, A), Color.FromArgb(10, A), Color.FromArgb(170, A), Color.FromArgb(150, M), Color.FromArgb(10, A), Color.FromArgb(10, A) },
                    Positions = new[] { 0f, Math.Max(0.001f, head - 0.12f), Math.Max(0.002f, head - 0.02f), Math.Min(0.998f, head + 0.02f), Math.Min(0.999f, head + 0.08f), 1f },
                };
                g.FillRectangle(b, m + L + 10, m - 0.5f, Width - 2 * (m + L + 10), 1f);
            }
            using (var b = new SolidBrush(Alpha(A, 0.75f)))
            using (var d = new SolidBrush(Dim))
            {
                g.DrawString("SYS://BOOT", monoSmall, b, m + 10, m + 8);
                g.DrawString("STUDIO " + (string.IsNullOrEmpty(Version) ? "" : "v" + Version), monoSmall, d, m + 10, m + 22);
                string right = "NEURAL HUD // NC-77";
                var rs = g.MeasureString(right, monoSmall);
                g.DrawString(right, monoSmall, b, Width - m - 10 - rs.Width, m + 8);
                string link = "LINK " + (Progress < 0 ? "SYNC" : "XFER") + "  " + ((int)(t * 12) % 2 == 0 ? "■" : "□");
                var ls = g.MeasureString(link, monoSmall);
                g.DrawString(link, monoSmall, d, Width - m - 10 - ls.Width, m + 22);
                string cur = inside ? $"CURSOR  X {(int)mouse.X:0000}  Y {(int)mouse.Y:0000}" : "CURSOR  X ----  Y ----";
                g.DrawString(cur, monoSmall, d, m + 10, Height - m - 22);
                string clock = $"T+{t:00.00}s";
                var cs = g.MeasureString(clock, monoSmall);
                g.DrawString(clock, monoSmall, b, Width - m - 10 - cs.Width, Height - m - 22);
            }
        }

        void DrawCursor(Graphics g, float t)
        {
            // shockwaves
            foreach (var w in waves)
            {
                float k = (t - w.T) / 0.65f;
                if (k >= 1) continue;
                float e = 1 - (float)Math.Pow(1 - k, 3);
                using (var pen = new Pen(Alpha(A, 1 - k), 2 * (1 - k) + 0.5f))
                    g.DrawEllipse(pen, w.P.X - (6 + e * 46), w.P.Y - (6 + e * 46), (6 + e * 46) * 2, (6 + e * 46) * 2);
                using (var pen = new Pen(Alpha(M, 0.7f * (1 - k)), 1.2f))
                    g.DrawEllipse(pen, w.P.X - (4 + e * 26), w.P.Y - (4 + e * 26), (4 + e * 26) * 2, (4 + e * 26) * 2);
                using (var pen = new Pen(Alpha(Y, 1 - k), 1.2f))
                    for (int i = 0; i < 8; i++)
                    {
                        double a = i * Math.PI / 4 + w.Rot;
                        float r0 = 10 + e * 40, r1 = r0 + 7 * (1 - k);
                        g.DrawLine(pen, w.P.X + (float)Math.Cos(a) * r0, w.P.Y + (float)Math.Sin(a) * r0, w.P.X + (float)Math.Cos(a) * r1, w.P.Y + (float)Math.Sin(a) * r1);
                    }
            }
            // sparks
            foreach (var s in sparks)
            {
                float k = 1 - (t - s.T) / s.Life;
                if (k <= 0) continue;
                using (var pen = new Pen(Alpha(s.C, k), 1.2f))
                    g.DrawLine(pen, s.P.X, s.P.Y, s.P.X - s.V.X * 0.04f, s.P.Y - s.V.Y * 0.04f);
            }
            if (!inside) return;
            // the light trail
            for (int pass = 0; pass < 2; pass++)
                for (int i = 1; i < trail.Count; i++)
                {
                    float k = 1 - (t - trail[i].T) / TrailLife;
                    if (k <= 0) continue;
                    float f = (float)i / trail.Count;
                    using (var pen = pass == 0
                        ? new Pen(Alpha(f > 0.5f ? A : M, k * 0.16f), 7 * k + 1)
                        : new Pen(Alpha(A, k * 0.85f), 1.6f * k + 0.3f))
                    {
                        pen.StartCap = pen.EndCap = LineCap.Round;
                        g.DrawLine(pen, trail[i - 1].P, trail[i].P);
                    }
                }
            // the lock brackets (on the button) or the reticle
            if (lockAlpha > 0.05f)
            {
                var b = lockBox;
                float L = Math.Min(12, Math.Min(b.Width, b.Height) / 3);
                using (var pen = new Pen(Alpha(A, 0.95f * lockAlpha), 1.5f))
                {
                    g.DrawLines(pen, new[] { new PointF(b.X, b.Y + L), new PointF(b.X, b.Y), new PointF(b.X + L, b.Y) });
                    g.DrawLines(pen, new[] { new PointF(b.Right - L, b.Y), new PointF(b.Right, b.Y), new PointF(b.Right, b.Y + L) });
                    g.DrawLines(pen, new[] { new PointF(b.X, b.Bottom - L), new PointF(b.X, b.Bottom), new PointF(b.X + L, b.Bottom) });
                    g.DrawLines(pen, new[] { new PointF(b.Right - L, b.Bottom), new PointF(b.Right, b.Bottom), new PointF(b.Right, b.Bottom - L) });
                }
                if (lockAlpha > 0.6f)
                {
                    const string label = "◢ EXEC";
                    var ls = g.MeasureString(label, monoSmall);
                    using (var bg = new SolidBrush(Color.FromArgb((int)(200 * lockAlpha), 5, 9, 18)))
                        g.FillRectangle(bg, b.X, b.Y - ls.Height - 2, ls.Width + 4, ls.Height);
                    using (var tb = new SolidBrush(Alpha(A, lockAlpha)))
                        g.DrawString(label, monoSmall, tb, b.X + 2, b.Y - ls.Height - 2);
                }
                return;
            }
            float rot = t * 92f;
            using (var pen = new Pen(Alpha(A, 0.8f), 1.2f))
            {
                for (int i = 0; i < 4; i++) g.DrawArc(pen, reticle.X - 13, reticle.Y - 13, 26, 26, rot + i * 90, 45);
                for (int i = 0; i < 4; i++)
                {
                    double a = (i * 90 - rot * 0.5) * Math.PI / 180;
                    g.DrawLine(pen, reticle.X + (float)Math.Cos(a) * 17, reticle.Y + (float)Math.Sin(a) * 17,
                               reticle.X + (float)Math.Cos(a) * 22, reticle.Y + (float)Math.Sin(a) * 22);
                }
            }
            using (var pen = new Pen(Alpha(M, 0.55f), 1f))
                g.DrawEllipse(pen, reticle.X - 6, reticle.Y - 6, 12, 12);
            using (var b = new SolidBrush(Ink))
                g.FillEllipse(b, mouse.X - 1.5f, mouse.Y - 1.5f, 3, 3);
        }
    }
}
