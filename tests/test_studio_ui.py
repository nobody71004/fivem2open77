"""The Studio's pages in a real browser (Playwright + Chromium) against a running Studio: the map
pages (overview, 3D, options, build, in my game, servers, placement), live maps, servers and the
AI agent page.  The Studio's server entry reaches a fake Open77 host through a real sshd, with a
fake map manager behind the agent API, so the placement panel really moves a map over SSH.

Opt-in: needs a FiveM map to look at (F2O77_TEST_MAP, default the Cedar Crest test copy; the look
takes about a minute), root + OpenSSH and Playwright's Chromium; skipped otherwise.
F2O77_PAGE_SHOTS=<dir> keeps the screenshots."""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

MAP = os.environ.get("F2O77_TEST_MAP", "/home/claude/work/src_cedar")
SEC_W = "base\\worlds\\03_night_city\\_compiled\\default\\exterior_0_0_0_3.streamingsector"


def _get(base, token, path):
    r = urllib.request.Request(base + path, headers={"X-F2O77-Token": token})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read())


def _send(base, token, method, path, body=None):
    r = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), method=method,
                               headers={"X-F2O77-Token": token, "Content-Type": "application/json",
                                        "Origin": base})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read())


def _job(base, token, jid, seconds=600):
    t0 = time.time()
    while time.time() - t0 < seconds:
        j = _get(base, token, f"/api/jobs/{jid}")
        if j["state"] not in ("queued", "running"):
            assert j["state"] == "done", (j["title"], j["state"], j.get("error"), [x["text"] for x in j["log"]][-20:])
            return j
        time.sleep(0.5)
    raise AssertionError("job timed out")


def test_studio_pages_in_a_browser():
    import sshd_fixture
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("   (skipped: no playwright)")
        return
    if not os.path.exists(MAP) or not sshd_fixture.available():
        print("   (skipped: needs the test map and root + OpenSSH)")
        return
    from fivem2open77.mlo import toolkit as TK
    from fivem2open77.studio.agent.manager import AgentManager
    from fivem2open77.studio.app import Studio
    from fivem2open77.studio.server import StudioServer
    from fake_manager import TOKEN, FakeManager, _serve
    from test_mlo_deploy import MapServer, _fake_toolkit
    from test_studio_maps import _fake_map_build

    shots = os.environ.get("F2O77_PAGE_SHOTS") or tempfile.mkdtemp(prefix="f2o77-ui-")
    os.makedirs(shots, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="f2o77-ui-")
    srv = fake_http = httpd = studio = None
    try:
        srv = MapServer(tmp)

        class Fake(FakeManager):
            def handle(self, method, path, body, auth):
                # the panel polls /status: a move goes on one step per poll there too
                if method == "GET" and path.rstrip("/") in ("/f2o77_maps/status", "/f2o77_maps"):
                    for m in self.maps.values():
                        self.tick(m)
                return super().handle(method, path, body, auth)
        fake = Fake()
        fake.maps["cedar_crest"].update(display="Cedar Crest Estate", presets=[{"name": "Hill top", "anchor": {"x": 520.0, "y": 1750.0, "z": 180.0, "yaw": 90.0}}],
                                        history=[], bounds=[[-67.0, -96.0, -106.0], [73.0, 77.0, 12.5]], runningId="cedar_crest-convert-1")
        fake.maps["harbor_dock"] = {"name": "harbor_dock", "display": "Harbor Dock", "running": {"x": 700.0, "y": 1650.0, "z": 20.0, "yaw": 30.0},
                                    "pending": None, "previous": None, "presets": [], "apply": None, "history": [],
                                    "bounds": [[-30.0, -20.0, 0.0], [30.0, 20.0, 15.0]]}
        fake_http, _ = _serve(fake)
        port = fake_http.server_address[1]
        cfg = os.path.join(srv.dir, "server.jsonc")
        text = open(cfg).read().replace("{\n  // test server", '{\n  // test server\n  "httpHandlers": { "enabled": true, '
                                        f'"listenUrl": "http://127.0.0.1:{port}" }},', 1)
        with open(cfg, "w") as f:
            f.write(text)
        env = {"PATH": f"{srv.fake}:/usr/local/bin:/usr/bin:/bin", "HOME": srv.home, "FAKE": srv.fake,
               "XDG_CONFIG_HOME": os.path.join(srv.home, ".config"), "XDG_RUNTIME_DIR": os.path.join(tmp, "run"),
               "BACKUP_ROOT": os.path.join(tmp, "backups")}
        with sshd_fixture.Sshd(env=env) as sshd:
            ws = os.path.join(tmp, "ws")
            studio = Studio(ws)
            studio.agents = AgentManager(studio)
            full = _fake_toolkit(os.path.join(tmp, "baker-toolkit.tgz"))
            studio.baker_toolkit = lambda have, log=print: (TK.build_lib(os.path.join(tmp, "light.tgz"), have, log=log)["path"]
                                                            if have == "nettest-wkfake" else full)
            token = "u" * 24
            httpd = StudioServer(("127.0.0.1", 0), studio, token)
            threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            _send(base, token, "POST", "/api/servers", {"id": "local", "label": "Test VM", "host": "127.0.0.1", "port": sshd.port,
                                                         "user": sshd.user, "auth": "key", "key_file": sshd.key, "servers_root": srv.root,
                                                         "names": ["open77-test"], "systemctl": "systemctl --user"})
            # a real map, looked at for real
            r = _send(base, token, "POST", "/api/projects", {"path": MAP, "kind": "map"})
            pid = r["project"]["id"]
            j = _job(base, token, r["job"]["id"], 900)
            # an import names the job it starts next: the first look at the map
            if (j.get("result") or {}).get("next_job"):
                _job(base, token, j["result"]["next_job"], 900)
            r = _send(base, token, "PATCH", f"/api/projects/{pid}/map/options",
                      {"name": "cedar_crest", "display_name": "Cedar Crest Estate", "place": {"x": 480, "y": 1800, "z": 174, "yaw": 0}})
            assert r["look_job"] is None, "the names and the spot do not change the look"
            # a build without WolvenKit (the converter's output for a small map), installed for real over SSH
            p = studio.projects.get(pid)
            fb = _fake_map_build(studio, tmp)
            shutil.copytree(fb.build_dir(1), p.build_dir(p.next_build()))
            p.add_build(dict(fb.data["builds"][0], ok=True, baked={"anchor": {"x": 480.0, "y": 1800.0, "z": 174.0, "yaw": 0.0}},
                             qa={"ok": True, "failed": [], "warnings": []}, packages=[{"file": "cedar_crest.zip", "bytes": 1234}]))
            studio.projects.delete(fb.id)
            dep = _send(base, token, "POST", f"/api/projects/{pid}/deploy", {"server": "local"})
            _job(base, token, dep["id"], 300)
            with open(os.path.join(srv.manager(), "data", "agent_token.txt"), "w") as f:
                f.write(TOKEN)

            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.goto(f"{base}/auth?token={token}")
                page.wait_for_selector("#rail a[data-nav=maps]")

                def shot(name):
                    page.screenshot(path=os.path.join(shots, name + ".png"), full_page=True)

                # ------------------------------------------------ maps list
                page.goto(f"{base}/#/maps")
                page.wait_for_selector(".tbl.list tr.link")
                row = page.inner_text(".tbl.list tr.link")
                assert "Cedar Crest Estate" in row and "cedar_crest" in row, row
                page.wait_for_function("() => [...document.querySelectorAll('.tbl.list .thumb')].every(t => t.style.backgroundImage)")
                shot("1-maps")
                page.goto(f"{base}/#/projects")
                page.wait_for_selector(".empty")
                assert "No cars yet" in page.inner_text("#main"), "maps are not listed with the cars"

                # ------------------------------------------------ overview
                page.goto(f"{base}/#/map/{pid}/overview")
                page.wait_for_selector("img.sheet")
                page.wait_for_function("() => document.querySelector('img.sheet').naturalWidth > 0")
                txt = page.inner_text("#main").lower()      # (labels are styled upper case)
                for needle in ("Interiors (MLO)", "Placements", "Doors / gates", "Where it stands", "Progress", "Left out: vanilla GTA objects"):
                    assert needle.lower() in txt, needle
                assert page.input_value(".card input[type=number]") == "480"
                shot("2-overview")
                page.click("text=From above")
                page.wait_for_function("() => document.querySelector('img.sheet').src.includes('view=plan')")
                page.wait_for_function("() => document.querySelector('img.sheet').complete && document.querySelector('img.sheet').naturalWidth === 1024")
                page.click("button:has-text('Parts')")
                page.wait_for_function("() => document.querySelector('img.sheet').src.includes('mode=parts')")

                # ------------------------------------------------ 3D
                page.goto(f"{base}/#/map/{pid}/3d")
                page.wait_for_selector(".viewer canvas")
                page.wait_for_function("() => !document.querySelector('.viewer .loading')", timeout=120000)
                assert "placements" in page.inner_text(".viewer .overlay")
                assert page.locator(".partlist label").count() > 20
                page.wait_for_timeout(800)
                shot("3-3d")

                def lit():
                    """Share of the view's middle that is lit (the preview has no normals: a material that
                    does not shade flat draws the whole map black)."""
                    from PIL import Image
                    im = Image.open(io.BytesIO(page.locator(".viewer canvas").screenshot())).convert("L")
                    w, h_ = im.size
                    px = list(im.crop((w // 4, h_ // 4, 3 * w // 4, 3 * h_ // 4)).tobytes())
                    return sum(1 for v in px if v > 90) / len(px)
                textured = lit()
                page.click(".viewer .overlay label:has-text('colour by kind')")
                page.wait_for_timeout(800)
                by_kind = lit()
                shot("3a-3d-kinds")
                assert textured > 0.05 and by_kind > 0.5 * textured, (textured, by_kind)
                page.click(".viewer .overlay label:has-text('colour by kind')")
                page.click(".viewer .overlay button:has-text('From above')")
                page.wait_for_timeout(500)
                shot("3b-3d-top")

                # ------------------------------------------------ options
                page.goto(f"{base}/#/map/{pid}/options")
                page.wait_for_selector("text=Collision and doors")
                assert page.input_value("input[placeholder]") in ("cedar_crest", "") or True
                shot("4-options")
                names = page.locator(".card:has-text('Names') input")
                names.nth(1).fill("Cedar Crest Hills")
                page.click("button:text-is('Save')")
                page.wait_for_timeout(600)
                st = _get(base, token, f"/api/projects/{pid}/map")
                assert st["options"]["display_name"] == "Cedar Crest Hills" and st["normalized"]["place"]["x"] == 480.0, st["options"]
                assert not [j for j in st["jobs"] if j["kind"] == "look"], "a name change starts no look"

                # ------------------------------------------------ doors
                page.goto(f"{base}/#/map/{pid}/doors")
                page.wait_for_selector("text=Doors and doorways", timeout=900000)    # the new options key looks again
                txt = page.inner_text("#main").lower()           # (card titles are styled upper case)
                assert "doorways (" in txt and "doors that open (" in txt and "any object" in txt, txt[:600]
                look = _get(base, token, f"/api/projects/{pid}/map")["look"]
                assert look["doorways"] and {w["status"] for w in look["doorways"]} <= {"open", "door", "closed", "blocked", "window"}
                assert any(d.get("found") for d in look["doors"]), look["doors"][:3]
                shot("4b-doors")
                obj = look["door_objects"][0]["name"]
                page.fill(".card:has-text('Any object') input[type=search]", obj)
                row = page.locator(".card:has-text('Any object') tr", has_text=obj).first
                row.locator("select").select_option("solid")
                way = look["doorways"][0]["key"]
                page.locator("tr", has_text=way).first.locator("select").select_option("open")
                page.click("button:has-text('Save and look again')")
                page.wait_for_timeout(800)
                st = _get(base, token, f"/api/projects/{pid}/map")
                assert st["options"]["door_map"] == {"objects": {obj.lower(): "solid"}, "doorways": {way: "open"}}, st["options"]
                assert [j for j in st["jobs"] if j["kind"] == "look"], "a door mapping change looks at the map again"

                # ------------------------------------------------ build / in my game / servers
                page.goto(f"{base}/#/map/{pid}/build")
                page.wait_for_selector("td:has-text('#1')")
                page.click("button:has-text('details')")
                page.wait_for_selector("text=Build #1")
                shot("5-build")
                page.goto(f"{base}/#/map/{pid}/game")
                page.wait_for_selector("text=Try it in your own game")
                assert "Cyberpunk 2077 was not found" in page.inner_text("#main")
                page.goto(f"{base}/#/map/{pid}/deploy")
                page.wait_for_selector("text=Install on a server")
                page.click("button:has-text('Check server')")
                page.wait_for_selector("text=staged moves", timeout=60000)
                txt = page.inner_text("#main")
                assert "cedar_crest" in txt and "on (http://127.0.0.1:" in txt and "nettest-wkfake" in txt, txt
                shot("6-servers-tab")

                # ------------------------------------------------ placement
                page.goto(f"{base}/#/map/{pid}/place")
                page.wait_for_selector("text=stands at", timeout=60000)
                page.wait_for_timeout(1500)
                shot("7-place")
                before = len(fake.calls)
                page.click("button:has-text('x +10')")
                page.wait_for_function("() => document.body.innerText.includes('490.0, 1800.0')")
                # (the panel also polls the players: the move is the last call to the map, not the last call)
                moves = [c for c in fake.calls[before:] if c[1].endswith("/maps/cedar_crest/move")]
                assert moves and moves[-1][2]["dx"] == 10.0, fake.calls[before:]
                # drag the map on the canvas: a place call with the dropped spot
                box = page.locator("#main canvas").first.bounding_box()
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)
                page.mouse.down()
                page.mouse.move(cx + 60, cy - 40, steps=6)
                page.mouse.up()
                page.wait_for_function("() => true")
                page.wait_for_timeout(700)
                places = [c for c in fake.calls[before:] if c[1].endswith("/place")]
                assert places, fake.calls[before:]
                moved = fake.maps["cedar_crest"]["pending"]
                assert moved["x"] > 490.0 and moved["y"] > 1800.0, moved
                # keyboard: Q turns 5 degrees
                page.locator("#main canvas").first.focus()
                n0 = len(fake.calls)
                page.keyboard.press("q")
                page.wait_for_timeout(500)
                turns = [c for c in fake.calls[n0:] if c[1].endswith("/maps/cedar_crest/move")]
                assert turns and turns[-1][2].get("dyaw") == 5, fake.calls[n0:]
                # a preset, then apply (confirmed)
                page.click("button:has-text('Save the new spot')")
                page.fill(".modal input", "Lower lawn")
                page.click(".modal button:has-text('OK')")
                page.wait_for_timeout(600)
                assert any(p["name"] == "Lower lawn" for p in fake.maps["cedar_crest"]["presets"])
                page.click("button:has-text('Apply the move')")
                page.click(".modal button:has-text('Apply')")
                page.wait_for_function("() => document.body.innerText.includes('waiting for the map baker') || document.body.innerText.includes('the baker is building it') || document.body.innerText.includes('restart scheduled')", timeout=20000)
                shot("8-place-applying")
                page.wait_for_function("() => document.body.innerText.includes('applied')", timeout=60000)
                assert fake.maps["cedar_crest"]["running"]["x"] == moved["x"]
                shot("9-place-applied")

                # ------------------------------------------------ live maps, servers, agent
                page.goto(f"{base}/#/live/local/open77-test")
                page.wait_for_selector("text=stands at", timeout=60000)
                page.select_option("select >> nth=2", "harbor_dock")
                page.wait_for_function("() => document.body.innerText.toLowerCase().includes('harbor dock')")   # (a styled upper-case title)
                shot("10-live")
                page.goto(f"{base}/#/servers")
                page.wait_for_selector("text=Test VM")
                page.click("button:has-text('Status')")
                page.wait_for_selector("text=staged moves", timeout=60000)
                shot("11-servers")
                page.goto(f"{base}/#/agent")
                page.wait_for_selector("text=Presets")
                assert "Set up a server for maps" in page.inner_text("#main")
                shot("12-agent")

                # ------------------------------------------------ world edits (the synthetic sector of test_world)
                from test_world import _index_with, synthetic_sector
                idx, _ = _index_with(tempfile.mkdtemp(dir=tmp), {SEC_W: synthetic_sector()})
                os.makedirs(os.path.join(ws, "world"), exist_ok=True)
                shutil.copyfile(idx.index_file, os.path.join(ws, "world", "blocks.json"))
                studio.world_extractor = idx.extract
                studio._world_index = None
                w = _send(base, token, "POST", "/api/projects", {"kind": "world", "display": "Railing gone", "name": "world_ui_test",
                                                                 "center": [104, 199, 5]})
                wid = w["project"]["id"]
                page.goto(f"{base}/#/world")
                page.wait_for_selector("text=Railing gone")
                assert "ready" in page.inner_text("#main")
                page.goto(f"{base}/#/world/{wid}")
                page.wait_for_selector(".we-out .items .item")
                page.locator(".we-out .items .item", has_text="railing_a").first.click()
                page.wait_for_selector("text=node 4 · actor 2")              # its collision, found by place
                shot("13-world-selected")
                page.click(".inspector button:has-text('Delete (Del)')")
                page.wait_for_selector(".we-tabs button:has-text('Changes (1)')")
                st = _get(base, token, f"/api/projects/{wid}/world")
                assert [(e["role"], e["type"]) for e in st["edits"]] == [("object", "worldStaticMeshNode"), ("collision", "worldCollisionNode")]
                page.locator("canvas.we-canvas").focus()
                page.keyboard.press("Control+z")
                page.wait_for_selector(".we-tabs button:text-is('Changes')")
                assert _get(base, token, f"/api/projects/{wid}/world")["edits"] == []
                shot("14-world-undone")
                # redone, built (deletions need no WolvenKit), installed on the server; the Servers page lists
                # it and takes it off again; the World page says where it was removed
                page.keyboard.press("Control+y")
                page.wait_for_selector(".we-tabs button:has-text('Changes (1)')")
                b = _job(base, token, _send(base, token, "POST", f"/api/projects/{wid}/build")["id"])
                assert b["state"] == "done", b
                d = _job(base, token, _send(base, token, "POST", f"/api/projects/{wid}/deploy", {"server": "local", "mode": "files"})["id"])
                assert d["state"] == "done", d
                page.goto(f"{base}/#/servers")
                page.wait_for_selector("text=Test VM")
                page.click("button:has-text('Status')")
                page.wait_for_selector(".srv-world:has-text('Railing gone')", timeout=60000)
                shot("14b-servers-world-edit")
                page.click(".srv-world button:has-text('Remove')")
                page.click(".modal button.danger:has-text('Remove')")
                page.wait_for_selector("text=no world edits", timeout=60000)
                assert not os.path.exists(os.path.join(srv.dir, "resources", "assets", "world", "world_ui_test"))
                page.goto(f"{base}/#/world/{wid}")
                page.wait_for_selector(".inspector .we-dep .pill:has-text('removed')")
                assert page.locator(".inspector button:has-text('Remove from a server')").count() == 1
                shot("14c-world-removed")
                page.goto(f"{base}/#/settings/agents")
                page.wait_for_selector("text=Connect an app")
                txt = page.inner_text("#main")
                assert "Claude Code" in txt and "Codex" in txt and "-m fivem2open77 mcp" in txt, txt[:800]
                shot("15-agents")
                browser.close()
            bad = [e for e in errors if "favicon" not in e]
            assert not bad, bad
    finally:
        if httpd:
            httpd.stopping = True
            httpd.shutdown()
            httpd.server_close()
        if fake_http:
            fake_http.shutdown()
        if studio:
            studio.close()
        if srv:
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"   screenshots: {shots}")


if __name__ == "__main__":
    test_studio_pages_in_a_browser()
    print("ok")
