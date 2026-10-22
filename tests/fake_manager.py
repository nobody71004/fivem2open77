"""A fake map manager agent API that answers like fivem2open77/mlo/manager/server/main.lua (routes,
token, 409 refusals, a move going requested -> baking -> restart_scheduled -> applied with a server
restart in between), for the Studio's live map tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "abcdefghijklmnop0123456789abcdef"


class FakeManager:
    def __init__(self):
        self.maps = {"cedar_crest": {"name": "cedar_crest", "display": "Cedar Crest Estate",
                                     "running": {"x": 480.0, "y": 1800.0, "z": 174.0, "yaw": 0.0},
                                     "pending": None, "previous": None, "presets": [], "apply": None}}
        self.calls = []
        self.down = 0                 # answer this many requests with a dropped connection (a restart)
        self.restarting = False

    def summary(self, name):
        return json.loads(json.dumps(self.maps[name]))

    def tick(self, m):
        """Each read moves an apply one step on, the way the baker and the restart do."""
        a = m.get("apply")
        if not a:
            return
        nxt = {"requested": "baking", "baking": "restart_scheduled"}.get(a["state"])
        if nxt:
            a["state"] = nxt
        elif a["state"] == "restart_scheduled" and not self.restarting:
            self.restarting = True
            self.down = 2                 # the server goes away for a moment
        elif a["state"] == "restart_scheduled":
            self.restarting = False
            m["previous"], m["running"], m["pending"] = m["running"], a["anchor"], None
            a["state"], a["message"] = "applied", "live since the last restart"

    def handle(self, method, path, body, auth):
        self.calls.append((method, path, body))
        if not path.startswith("/f2o77_maps"):
            return 404, {"error": "not_found"}
        path = path[len("/f2o77_maps"):] or "/status"
        if method == "GET" and path == "/status":
            return 200, {"resource": "f2o77_maps", "maps": [self.summary(n) for n in self.maps], "baker": {"known": True}}
        if method == "GET" and path == "/baker":
            return 200, {"baker": {"known": True, "hook": True}, "status": {}}
        if method == "GET" and path.startswith("/maps/") and path.count("/") == 2:
            name = path.split("/")[2]
            if name not in self.maps:
                return 404, {"error": "unknown_map"}
            self.tick(self.maps[name])
            return 200, self.summary(name)
        if auth != "Bearer " + TOKEN:
            return 401, {"error": "unauthorized"}
        if method == "GET" and path == "/players":
            return 200, {"players": [{"playerId": 1, "name": "V", "rights": {"cedar_crest": True}}]}
        if path == "/screenshot":
            return 202, {"requested": True}
        parts = path.split("/")
        if len(parts) != 4 or parts[1] != "maps":
            return 404, {"error": "unknown_route"}
        name, op = parts[2], parts[3]
        if name not in self.maps:
            return 404, {"error": "unknown_map"}
        m = self.maps[name]
        base = m["pending"] or m["running"]

        def answer(ok, reason=None, result=None):
            return (200 if ok else 409), {"ok": ok, "reason": reason, "result": result, "map": self.summary(name)}
        if op == "place":
            if not isinstance(body.get("x"), (int, float)):
                return answer(False, "invalid_x")
            m["pending"] = {"x": body["x"], "y": body["y"], "z": body["z"], "yaw": body.get("yaw") or 0.0}
            return answer(True, result=m["pending"])
        if op == "move":
            m["pending"] = {"x": base["x"] + body.get("dx", 0), "y": base["y"] + body.get("dy", 0),
                            "z": base["z"] + body.get("dz", 0), "yaw": base["yaw"] + body.get("dyaw", 0)}
            return answer(True, result=m["pending"])
        if op in ("drop", "ground"):
            g = {"x": body.get("x") or base["x"], "y": body.get("y") or base["y"], "z": 42.5}
            if op == "drop":
                m["pending"] = dict(g, yaw=base["yaw"])
            return answer(True, result=g)
        if op == "discard":
            m["pending"] = None
            return answer(True)
        if op == "revert":
            if not m["previous"]:
                return answer(False, "no_previous_spot")
            m["pending"] = m["previous"]
            return answer(True, result=m["pending"])
        if op == "goto":
            return answer(True, result={"x": 1, "y": 2, "z": 3})
        if op == "presetSave":
            m["presets"].append({"name": body["name"], "anchor": base})
            return answer(True)
        if op == "apply":
            if not m["pending"]:
                return answer(False, "nothing_to_apply")
            m["apply"] = {"id": "x-1", "state": "requested", "mode": body.get("mode"), "countdown": body.get("countdown"),
                          "anchor": m["pending"], "message": "waiting for the map baker"}
            return answer(True, result=m["apply"])
        if op == "cancel":
            if not m["apply"]:
                return answer(False, "nothing_to_cancel")
            m["apply"]["state"] = "cancelled"
            return answer(True)
        return 404, {"error": "unknown_op"}


def _serve(fake):
    class H(BaseHTTPRequestHandler):
        def _go(self, method):
            if fake.down > 0:
                fake.down -= 1
                self.close_connection = True
                self.connection.close()
                return
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            code, value = fake.handle(method, self.path, body, self.headers.get("Authorization"))
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
