"""The MCP bridge while the setup replaces the app: it does not start the old Studio again."""
import os
import time

from fivem2open77.studio import bridge as B


def _bridge(tmp_path, monkeypatch, finds, updating):
    b = B.Bridge(str(tmp_path), log=str(tmp_path / "bridge.log"))
    seq = list(finds)
    monkeypatch.setattr(b, "find", lambda: seq.pop(0) if seq else None)
    launched = []
    monkeypatch.setattr(b, "launch", lambda: launched.append(time.time()) or setattr(b, "launched_at", time.time()))
    monkeypatch.setattr(B, "in_package", lambda: False)
    monkeypatch.setattr(B, "updating", updating)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    return b, launched


def test_waits_for_the_new_studio_while_updating(tmp_path, monkeypatch):
    b, launched = _bridge(tmp_path, monkeypatch, [None, None, None, 4711], lambda: True)
    assert b.ensure(wait=5) == 4711
    assert launched == []                                   # the setup starts the new one
    assert "being updated" in (tmp_path / "bridge.log").read_text()


def test_starts_it_when_the_setup_is_done_and_none_answers(tmp_path, monkeypatch):
    state = {"n": 0}

    def updating():
        state["n"] += 1
        return state["n"] < 3                               # the marker goes after a moment

    finds = [None] * 8 + [5150]
    b, launched = _bridge(tmp_path, monkeypatch, finds, updating)
    assert b.ensure(wait=5) == 5150
    assert len(launched) == 1


def test_not_updating_starts_it_once(tmp_path, monkeypatch):
    b, launched = _bridge(tmp_path, monkeypatch, [None, None, 6000], lambda: False)
    assert b.ensure(wait=5) == 6000
    assert len(launched) == 1


def test_marker_age(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(B.os, "name", "nt", raising=False)
    try:
        assert not B.updating()
        m = tmp_path / "fivem2open77" / "updating"
        m.parent.mkdir()
        m.write_text("1.5.3")
        assert B.updating()
        old = time.time() - 3600
        os.utime(m, (old, old))
        assert not B.updating()                             # a setup that did not finish
    finally:
        monkeypatch.undo()
