"""The map manager's panel (fivem2open77/mlo/manager/web) in a real browser (Playwright + Chromium)
behind a stub Open77 WebUI bridge: what it shows for the server's state and what it asks Lua for.
Skipped when Playwright or its Chromium is not installed.  F2O77_PAGE_SHOTS=<dir> keeps screenshots."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "fivem2open77", "mlo", "manager", "web")

STUB = """
window.__emitted = [];
window.__handlers = {};
window.Open77 = {
  on(name, fn) { window.__handlers[name] = fn; },
  emit(name, payload) { window.__emitted.push([name, JSON.parse(JSON.stringify(payload || {}))]); },
  ready() { window.__ready = true; },
};
window.__fire = (name, payload) => { window.__handlers[name](payload); };
"""

RUN = {"x": 480.0, "y": 1800.0, "z": 174.0, "yaw": 0.0}
KEYS = [{"id": "open_manager", "name": "Maps: open the map manager", "key": "F9", "defaultKey": "F9", "rebound": False},
        {"id": "save", "name": "Maps: editor: save the spot", "key": "ENTER", "defaultKey": "ENTER", "rebound": False},
        {"id": "forward", "name": "Maps: editor: move forward", "key": "W", "defaultKey": "UP", "rebound": True, "hold": True}]


def _map(name="cedar_crest", display="Cedar Crest Estate", **kw):
    m = {"name": name, "display": display, "running": dict(RUN), "presets": [], "history": [], "right": f"command.mlo.{name}"}
    m.update(kw)
    return m


def test_panel_in_a_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("   (skipped: no playwright)")
        return
    shots = os.environ.get("F2O77_PAGE_SHOTS") or tempfile.mkdtemp(prefix="f2o77-page-")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            print(f"   (skipped: no chromium: {e})")
            return
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(STUB)
        page.goto("file://" + os.path.abspath(os.path.join(WEB, "index.html")))

        def emitted(name=None):
            ev = page.evaluate("window.__emitted")
            return [e for e in ev if name is None or e[0] == name]

        def fire(name, payload):
            page.evaluate("([n, p]) => window.__fire(n, p)", [name, payload])

        def clear():
            page.evaluate("window.__emitted = []")

        assert page.evaluate("window.__ready") is True and emitted("maps:ready"), "the page says it is ready"
        assert not page.is_visible("#panel .head h1") or page.evaluate(
            "getComputedStyle(document.getElementById('panel')).opacity") == "0", "hidden until opened"

        # Lua knocks until the page answers; a panel opened before the maps came asks for them again
        clear()
        fire("maps:ping", {})
        assert emitted("maps:ready") and emitted("maps:ready")[-1][1] == {"maps": 0}, emitted()
        fire("maps:init", {"maps": [], "rights": {}, "config": {}, "keys": KEYS, "loading": True})
        clear()
        fire("maps:open", {})
        page.wait_for_timeout(150)
        assert emitted("maps:ready")[-1][1] == {"again": True}, emitted()
        assert "loading its maps" in page.inner_text("#empty")
        fire("maps:close", {})

        # what the real bridge sends: Lua's empty tables arrive as objects ({}), never as empty lists --
        # the bug behind "No converted maps" (the detail threw on presets.forEach)
        clear()
        lua_like = _map(presets={}, history={})
        fire("maps:init", {"maps": {"1": lua_like}, "rights": {"cedar_crest": True},
                           "config": {"restartCountdowns": {}}, "keys": {"1": KEYS[0], "2": KEYS[1]}, "stale": {}})
        fire("maps:open", {})
        page.wait_for_timeout(150)
        assert not emitted("maps:log"), emitted("maps:log")
        assert page.is_hidden("#empty"), page.inner_text("#empty")
        assert "Cedar Crest Estate" in page.inner_text("#maplist")
        assert page.is_visible("#detail") and "No saved positions yet" in page.inner_text("#d-presets")
        assert page.evaluate("document.querySelectorAll('#keys .key').length") >= 2
        fire("maps:close", {})
        # an error in a handler is reported (and the panel keeps working)
        clear()
        fire("maps:state", {"name": "cedar_crest", "display": "Cedar Crest Estate", "running": None,
                            "presets": [{"name": "broken"}], "history": []})
        page.wait_for_timeout(50)
        logs = emitted("maps:log")
        assert logs and logs[-1][1]["level"] == "error" and "maps:state" in logs[-1][1]["text"], logs
        fire("maps:state", lua_like)
        # a server that does not answer is not "no maps"
        fire("maps:init", {"maps": {}, "rights": {}, "config": {}, "keys": KEYS, "answered": False, "noAnswer": True})
        fire("maps:open", {})
        page.wait_for_timeout(100)
        assert "not answering" in page.inner_text("#empty"), page.inner_text("#empty")
        fire("maps:close", {})
        fire("maps:init", {"maps": [], "rights": {}, "config": {}, "keys": KEYS, "answered": False})
        fire("maps:open", {})
        page.wait_for_timeout(100)
        assert "Asking the server" in page.inner_text("#empty"), page.inner_text("#empty")
        fire("maps:close", {})
        # an agent at work: its words and its camera, on screen
        fire("maps:agent", {"text": "moving the estate 4 m east", "by": "Claude"})
        assert page.is_visible("#agent") and "moving the estate" in page.inner_text("#agent")
        fire("maps:agent", {"view": True, "mode": "top", "map": "Cedar Crest Estate"})
        assert "top view of Cedar Crest Estate" in page.inner_text("#agent")
        fire("maps:agent", {"view": False})
        assert "view of" not in page.inner_text("#agent")

        harbor = _map("harbor_dock", "Harbor Dock", pending={"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 90.0})
        fire("maps:init", {"maps": [_map(), harbor], "rights": {"cedar_crest": True, "harbor_dock": False},
                           "config": {"restartCountdowns": [30, 60, 120, 300], "defaultCountdown": 60},
                           "keys": KEYS, "menuKey": "F9", "baker": {"known": False}})
        fire("maps:open", {})
        page.wait_for_timeout(250)
        assert page.evaluate("document.body.classList.contains('open')")
        items = page.locator(".mapitem")
        assert items.count() == 2
        assert items.nth(0).inner_text().startswith("Cedar Crest Estate") and "live" in items.nth(0).inner_text().lower()
        assert "moved, not applied" in items.nth(1).inner_text().lower()
        assert "480.00" in page.inner_text("#d-running") and "1800.00" in page.inner_text("#d-running")
        assert page.is_hidden("#d-readonly") and page.is_visible("[data-op=edit]")
        assert page.is_disabled("[data-op=apply_now]"), "nothing to apply yet"
        assert "not reported" in page.inner_text("#d-baker") and "warn" in page.get_attribute("#d-baker", "class")
        page.screenshot(path=os.path.join(shots, "1-list.png"))

        # numbers -> place
        clear()
        for sel, v in (("#f-x", "500.5"), ("#f-y", "1810"), ("#f-z", "175.25"), ("#f-yaw", "15")):
            page.fill(sel, v)
        page.click("[data-op=save_fields]")
        ops = emitted("maps:op")
        assert ops and ops[-1][1]["op"] == "place" and ops[-1][1]["map"] == "cedar_crest"
        assert ops[-1][1]["args"] == {"x": 500.5, "y": 1810, "z": 175.25, "yaw": 15}, ops[-1]
        page.click("[data-nudge=fwd]")
        first = emitted("maps:op")[-1][1]
        assert first["args"] == {"dy": 1, "frame": "map"}
        # nudges while one is on its way wait, summed (a burst of clicks is one move per answer)
        page.click("[data-nudge=fwd]")
        page.click("[data-nudge=fwd]")
        page.click("[data-nudge=ccw]")
        assert emitted("maps:op")[-1][1] == first, "nothing more until the server answered"
        fire("maps:result", {"request": first["request"], "map": "cedar_crest", "op": "move", "ok": True})
        second = emitted("maps:op")[-1][1]
        assert second["args"] == {"dy": 2, "frame": "map"}, second
        fire("maps:result", {"request": second["request"], "map": "cedar_crest", "op": "move", "ok": True})
        assert emitted("maps:op")[-1][1]["args"] == {"dyaw": 5}
        fire("maps:result", {"request": emitted("maps:op")[-1][1]["request"], "map": "cedar_crest", "op": "move", "ok": True})
        page.click("[data-op=here]")
        assert emitted("maps:op")[-1][1]["op"] == "here"
        page.click("[data-op=edit]")
        assert emitted("maps:edit")[-1][1] == {"map": "cedar_crest"}

        # a new spot: Apply now asks for a confirmation (it restarts the server for everyone)
        moved = _map(pending={"x": 600.0, "y": 1900.0, "z": 200.0, "yaw": 45.0})
        fire("maps:state", moved)
        assert page.is_enabled("[data-op=apply_now]") and "600.00" in page.inner_text("#d-pending")
        page.select_option("#f-countdown", "120")
        clear()
        page.click("[data-op=apply_now]")
        assert not emitted("maps:op"), "first click only arms"
        assert "Confirm: restart in 2 min" in page.inner_text("[data-op=apply_now]")
        page.screenshot(path=os.path.join(shots, "2-confirm.png"))
        page.click("[data-op=apply_now]")
        a = emitted("maps:op")[-1][1]
        assert a["op"] == "apply" and a["args"] == {"mode": "now", "countdown": 120}, a
        page.click("[data-op=apply_empty]")
        assert emitted("maps:op")[-1][1]["args"] == {"mode": "empty"}

        # a move in progress
        fire("maps:state", _map(pending=moved["pending"], apply={"state": "restart_scheduled", "id": "x",
                                                                  "message": "built: the server restarts in 120 s"}))
        assert "restart scheduled" in page.inner_text("#d-chip").lower()
        assert page.is_disabled("[data-op=apply_now]") and page.is_disabled("[data-op=apply_empty]")
        assert page.is_enabled("[data-op=cancel]") and "120 s" in page.inner_text("#d-applymsg")
        clear()
        page.click("[data-op=cancel]")
        assert emitted("maps:op")[-1][1]["op"] == "cancel"
        fire("maps:state", _map(pending=moved["pending"], apply={"state": "baking", "id": "x", "message": "building"}))
        assert page.is_disabled("[data-op=cancel]") and "building" in page.inner_text("#d-chip").lower()
        fire("maps:state", _map(apply={"state": "failed", "id": "x", "message": "the bake failed: boom"},
                                bake={"state": "failed", "log": "a\nWolvenKit said no\n"}))
        assert "WolvenKit said no" in page.inner_text("#d-applymsg") and "error" in page.get_attribute("#d-applymsg", "class")

        # live at the new spot, with the old one to go back to
        fire("maps:state", _map(running={"x": 600.0, "y": 1900.0, "z": 200.0, "yaw": 45.0}, previous=dict(RUN),
                                apply={"state": "applied", "id": "x", "message": "live since the last restart"},
                                history=[{"at": 1760000000, "by": "player1 (#1)", "what": "applied",
                                          "anchor": {"x": 600, "y": 1900, "z": 200, "yaw": 45}}]))
        assert page.is_visible("#d-previous-row") and "480.00" in page.inner_text("#d-previous")
        clear()
        page.click("[data-op=revert]")
        assert emitted("maps:op")[-1][1]["op"] == "revert"
        page.click("details.history summary")
        assert "applied" in page.inner_text("#d-history")

        # presets: use, and delete with a confirmation
        fire("maps:state", _map(presets=[{"name": "Hill top", "anchor": {"x": 1, "y": 2, "z": 3, "yaw": 4}}]))
        clear()
        page.click(".preset button:has-text('Use')")
        assert emitted("maps:op")[-1][1] == {"map": "cedar_crest", "op": "presetUse", "args": {"name": "Hill top"},
                                             "request": emitted("maps:op")[-1][1]["request"]}
        page.click(".preset button:has-text('Delete')")
        assert emitted("maps:op")[-1][1]["op"] != "presetDelete" and page.is_visible(".preset button:has-text('Sure?')")
        page.click(".preset button:has-text('Sure?')")
        assert emitted("maps:op")[-1][1]["op"] == "presetDelete"
        page.fill("#f-preset", "Dock side")
        page.press("#f-preset", "Enter")
        assert emitted("maps:op")[-1][1] == {"map": "cedar_crest", "op": "presetSave", "args": {"name": "Dock side"},
                                             "request": emitted("maps:op")[-1][1]["request"]}

        # the baker
        fire("maps:baker", {"known": True, "version": "1.0.0", "hook": False})
        assert "no start hook" in page.inner_text("#d-baker")
        fire("maps:baker", {"known": True, "version": "1.0.0", "hook": True})
        assert page.inner_text("#d-baker").startswith("Map baker 1.0.0: ready")

        # a map this player may only look at
        page.click(".mapitem[data-map=harbor_dock]")
        assert page.is_visible("#d-readonly") and page.is_hidden("[data-op=edit]")
        page.click(".mapitem[data-map=cedar_crest]")

        # a refused op shows the reason
        fire("maps:result", {"request": 99, "op": "apply", "ok": False, "reason": "already_baking"})
        assert "apply: already baking" in page.inner_text("#toasts")

        # keys: capture a new key for a mapping
        page.click(".tab[data-tab=keys]")
        rows = page.locator(".keyrow")
        assert rows.count() == 3 and "open the map manager" in rows.nth(0).inner_text()
        assert page.is_enabled(".keyrow:nth-child(3) .reset") and page.is_disabled(".keyrow:nth-child(1) .reset")
        clear()
        page.click("[data-key-id=save]")
        assert "press a key" in page.inner_text("[data-key-id=save]")
        page.keyboard.press("k")
        assert emitted("maps:keys:rebind")[-1][1] == {"id": "save", "key": "K"}
        page.click("[data-key-id=save]")
        page.keyboard.press("Escape")
        assert not emitted("maps:close") and "press a key" not in page.inner_text("[data-key-id=save]"), \
            "Escape cancels a capture without closing"
        fire("maps:keys", {"keys": KEYS, "menuKey": "F9", "error": "keybind_save_failed"})
        assert "keybind_save_failed" in page.inner_text("#keys-error")
        page.click(".keyrow:nth-child(3) .reset")
        assert emitted("maps:keys:reset")[-1][1] == {"id": "forward"}
        page.screenshot(path=os.path.join(shots, "3-keys.png"))
        page.click(".tab[data-tab=maps]")

        # closing: the menu key and Escape (mapped keys do not fire while the page has the keyboard)
        clear()
        page.keyboard.press("F9")
        assert emitted("maps:close"), "menu key closes"
        clear()
        page.keyboard.press("Escape")
        assert emitted("maps:close"), "Escape closes"
        clear()
        page.focus("#f-x")
        page.keyboard.press("F9")
        assert emitted("maps:close"), "a function key closes from a field too"
        fire("maps:keys", {"keys": KEYS, "menuKey": "M"})
        clear()
        page.focus("#f-preset")
        page.keyboard.press("m")
        assert not emitted("maps:close") and page.input_value("#f-preset").endswith("m"), "a letter typed in a field stays"
        page.focus("[data-op=edit]")
        page.keyboard.press("m")
        assert emitted("maps:close"), "the letter menu key closes outside fields"
        fire("maps:close", {})
        assert not page.evaluate("document.body.classList.contains('open')")

        # the editor HUD over the game
        fire("editor:state", {"active": True, "map": "cedar_crest", "display": "Cedar Crest Estate",
                              "anchor": {"x": 610.5, "y": 1901.0, "z": 201.0, "yaw": 50.0}, "running": dict(RUN),
                              "aim": True, "speed": "fast",
                              "keys": {"move": "UP/DOWN/LEFT/RIGHT", "height": "PAGEUP/PAGEDOWN", "save": "ENTER",
                                       "cancel": "BACKSPACE"}})
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.classList.contains('editing')")
        assert "610.50" in page.inner_text("#h-coords") and page.is_visible("#h-aim")
        assert "from where it is now" in page.inner_text("#h-delta") and "ENTER" in page.inner_text("#h-keys")
        assert "ground check when it stands still" in page.inner_text("#h-fit")
        assert page.is_visible("#h-legend") and "ground outline" in page.inner_text("#h-legend")
        assert "gates" in page.inner_text("#h-legend") and "buildings" in page.inner_text("#h-legend")
        page.screenshot(path=os.path.join(shots, "4-editor.png"))

        # the wireframe's state and the ground check
        base = {"active": True, "map": "cedar_crest", "display": "Cedar Crest Estate",
                "anchor": {"x": 610.5, "y": 1901.0, "z": 201.0, "yaw": 50.0}, "running": dict(RUN), "keys": {}}
        fire("editor:state", dict(base, wire="full", outline="outline",
                                  fit={"float": 2.04, "buried": 0.6, "missing": 1, "samples": 32}))
        fit = page.inner_text("#h-fit")
        assert "floats up to 2.0 m" in fit and "buried up to 0.6 m" in fit and "1 of 32 points" in fit, fit
        assert page.get_attribute("#h-wire", "class") == "chip live"
        page.screenshot(path=os.path.join(shots, "5-editor-fit.png"))
        fire("editor:state", dict(base, wire="full", outline="outline",
                                  fit={"float": 0, "buried": 0, "missing": 0, "samples": 32}))
        assert "sits on the ground" in page.inner_text("#h-fit")
        assert "ok" in page.get_attribute("#h-fit", "class")
        fire("editor:state", dict(base, wire="full", outline="outline",
                                  fit={"float": 0.9, "buried": 0, "missing": 0, "samples": 40, "skipped": 12,
                                       "gateFloat": 0.9, "gateBuried": 0}))
        fit = page.inner_text("#h-fit")
        assert "gates 0.9 m above the street" in fit and "12 over where it is now" in fit, fit
        fire("editor:state", dict(base, wire="full", outline="outline", fit={"here": True, "samples": 0}))
        assert "where the map is now" in page.inner_text("#h-fit")
        fire("editor:state", dict(base, wire="full", outline="outline", fit={"far": 1647, "samples": 0}))
        assert "1647 m from you" in page.inner_text("#h-fit")
        fire("editor:state", dict(base, wire="full", outline="outline", fit={"samples": 40, "skipped": 40}))
        assert "move it further" in page.inner_text("#h-fit")
        fire("editor:state", dict(base, wire="reduced", outline="box"))
        assert "another resource" in page.inner_text("#h-wire").lower()
        assert "warn" in page.get_attribute("#h-wire", "class")
        assert "rebuild" in page.inner_text("#lg-outline")
        fire("editor:state", dict(base, wire="off", wireWhy="debug_geometry_quota", outline="outline"))
        assert "debug geometry quota" in page.inner_text("#h-wire").lower()
        assert "off" in page.get_attribute("#h-wire", "class")
        fire("editor:state", dict(base, wire="loading", outline="loading"))
        assert "reading" in page.inner_text("#h-wire").lower()
        fire("editor:state", {"active": False})
        assert not page.evaluate("document.body.classList.contains('editing')")
        assert not errors, errors
        browser.close()
    print(f"   screenshots in {shots}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(HERE, ".."))
    test_panel_in_a_browser()
    print("ok   test_panel_in_a_browser")
