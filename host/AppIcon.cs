// The app's icon for its windows, with every size in it.
using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace Fivem2Open77.Studio
{
    static class AppIcon
    {
        /// <summary>app.ico from the exe's resources, all its sizes: the title bar takes the 16 px one and the
        /// taskbar the size its dpi needs.  (Icon.ExtractAssociatedIcon gives a single 32 px image, which both
        /// blur.)  The exe's own icon when the resource cannot be read; null when neither can.</summary>
        public static Icon Load()
        {
            try
            {
                using (var s = typeof(AppIcon).Assembly.GetManifestResourceStream("Fivem2Open77.Studio.app.ico"))
                    if (s != null) return new Icon(s);
            }
            catch { }
            try { return Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { return null; }
        }

        [DllImport("shell32.dll")]
        static extern void SHChangeNotify(int eventId, uint flags, IntPtr item1, IntPtr item2);

        /// <summary>Tell Explorer that icons changed, so shortcuts and pinned taskbar buttons of an updated copy
        /// show its icon instead of the one Explorer cached for the old exe.</summary>
        public static void RefreshShell()
        {
            const int SHCNE_ASSOCCHANGED = 0x08000000;
            const uint SHCNF_IDLIST = 0x0000;
            try { SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, IntPtr.Zero, IntPtr.Zero); } catch { }
        }
    }
}
