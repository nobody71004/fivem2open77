"""fivem2open77 Studio: the converter as a desktop app.

A local HTTP server (stdlib only) serves the web UI and a JSON API; the Windows host shows it in
a WebView2 window.  Everything the UI does goes through the API, so the AI agent and scripts can
drive the same operations: import a FiveM resource, inspect it, edit the conversion profile,
preview it in 3D, build it with WolvenKit, check the output, and install it on Open77 servers.
"""
