"""The map manager's answers as the Studio hands them on: Lua's JSON has no empty array, so a map
with no saved spots arrives as "presets": {} (and nobody online as "players": {}).  The Live maps
page looped over that, broke, and showed the break as "Not connected"; every answer now has its
lists as lists."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.studio import maplink  # noqa: E402


def test_as_list():
    assert maplink.as_list([1, 2]) == [1, 2]
    assert maplink.as_list({}) == []
    assert maplink.as_list(None) == []
    assert maplink.as_list("x") == []
    assert maplink.as_list({"2": "b", "1": "a", "10": "c"}) == ["a", "b", "c"]
    assert maplink.as_list({"x": 1}) == [1]


def _status():
    return {
        "maps": [
            {"name": "cookies", "presets": {}, "history": {}, "running": {"x": 1, "y": 2, "z": 3, "yaw": 0}},
            {"name": "cedar_crest", "presets": [{"name": "home", "anchor": {"x": 0, "y": 0, "z": 0, "yaw": 0}}],
             "history": {"1": {"what": "place"}, "2": {"what": "apply (now)"}}},
        ],
        "baker": {"known": True},
        "restart": {"scheduled": False},
    }


def test_normalize_status_map_players_and_ops():
    st = maplink.normalize(_status())
    a, b = st["maps"]
    assert a["presets"] == [] and a["history"] == []
    assert b["presets"][0]["name"] == "home"
    assert [x["what"] for x in b["history"]] == ["place", "apply (now)"]
    assert st["baker"] == {"known": True}
    # no maps installed yet: {} too
    assert maplink.normalize({"maps": {}, "baker": {}})["maps"] == []
    assert maplink.normalize({"players": {}})["players"] == []
    # an operation answers with the map; GET /maps/<name> answers with the map itself
    assert maplink.normalize({"ok": True, "map": {"name": "x", "presets": {}}})["map"]["presets"] == []
    assert maplink.normalize({"name": "x", "presets": {}, "history": {}})["history"] == []
    assert maplink.normalize([1]) == [1]


class _FakeLink(maplink.ManagerLink):
    """The real API methods over canned answers (no SSH)."""

    def __init__(self, answers):
        self.answers = answers

    def request(self, method, path, body=None, token=False, timeout=25.0):
        return 200, self.answers[(method, path.split("/")[1])]


def test_link_api_returns_lists():
    link = _FakeLink({
        ("GET", "status"): _status(),
        ("GET", "maps"): {"name": "cookies", "presets": {}, "history": {}},
        ("GET", "players"): {"players": {}},
        ("POST", "maps"): {"ok": True, "map": {"name": "cookies", "presets": {}, "history": {}}},
    })
    assert link.status()["maps"][0]["presets"] == []
    assert link.map("cookies")["presets"] == []
    assert link.players()["players"] == []
    assert link.op("cookies", "place", {"x": 1})["map"]["history"] == []
