"""Quality control around the clock.

Every project's latest build is checked: a car with the converter's quality checks (fivem2open77/
qc.py: every part on a bone the rig has, the doors the donor can open, the wheels on their bones and
upright, no geometry left out) on top of the package checks (qa.py), a map with its package and
coverage checks.  A build the checks cannot vouch for -- made before the converter learnt a fix
(stale), failing a check, a build that failed, a project never built -- is rebuilt and checked again
when ``qc.auto_rebuild`` is on.  The outcome is kept in the workspace's qc/ folder: report.json
(the latest sweep), report.html (the same, to open in a browser) and history.jsonl (one line a
sweep).

Sweeps run a few minutes after the Studio starts, every ``qc.interval_hours`` while it runs, after
a build, and on demand: the Quality page, the run_qc MCP tool, and ``python -m fivem2open77 qc``
(what Windows Task Scheduler runs every few hours when the Studio is closed; it starts the Studio
without a window, which stops once the sweep and its rebuilds are done).

A project the sweep could not fix (its rebuild failed, or still fails a check) is tried again only
after the converter, the project's source or its profile changes: a sweep never rebuilds the same
thing over and over.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time

from .. import qc as car_qc
from ..pipeline import VERSION
from . import qa as qa_mod

STATUS_ORDER = ("fail", "failed", "stale", "unbuilt", "warn", "ok", "skipped")
FIX_STATUSES = ("fail", "failed", "stale", "unbuilt")
FIRST_SWEEP_DELAY = 300           # s after the Studio starts


def _now():
    return time.time()


class QualityControl:
    def __init__(self, studio):
        self.studio = studio
        self.dir = os.path.join(studio.workspace, "qc")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.running = None               # the sweep job while one runs
        self.report = self._load("report.json") or {}
        self.state = self._load("state.json") or {"attempts": {}}

    # ------------------------------------------------------------------ files
    def _load(self, name):
        try:
            with open(os.path.join(self.dir, name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self, name, data):
        p = os.path.join(self.dir, name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, default=str)
        os.replace(tmp, p)

    def settings(self) -> dict:
        return self.studio.settings.get().get("qc") or {}

    # ------------------------------------------------------------------ one project
    def _fingerprint(self, p) -> str:
        """What a rebuild depends on: the converter, the project's source and its profile."""
        src = p.data.get("source") or {}
        try:
            prof = p.profile_hash() if p.kind == "car" else json.dumps(p.map_options(), sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 - a broken profile is reported by the build itself
            prof = None
        return json.dumps([VERSION, car_qc.QC_VERSION, src.get("hash") or src.get("file") or src.get("original"),
                           prof], sort_keys=True, default=str)

    def check_project(self, p) -> dict:
        kind = p.kind or "car"
        row = {"id": p.id, "kind": kind, "name": p.data.get("name") or p.id, "checked": _now(),
               "status": "ok", "errors": [], "warnings": [], "build": None}
        builds = p.data.get("builds") or []
        if kind == "world":
            b = builds[-1] if builds else None
            if not b:
                row.update(status="unbuilt", errors=["never built"])
            elif not b.get("ok"):
                row.update(status="failed", build=b.get("n"), errors=[b.get("error") or "the build failed"])
            else:
                row["build"] = b.get("n")
            return row
        if not builds:
            row.update(status="unbuilt", errors=["never built"])
            return row
        b = builds[-1]
        row["build"] = b.get("n")
        if not b.get("ok"):
            row.update(status="failed", errors=[b.get("error") or "the build failed"])
            return row
        out = os.path.join(p.build_dir(int(b["n"])), "out")
        if not os.path.isdir(out):
            row.update(status="unbuilt", errors=["the build's files are gone"])
            return row
        try:
            q = qa_mod.check_map_build(out) if kind == "map" else qa_mod.check_build(out)
        except Exception as e:  # noqa: BLE001 - a broken build is a failed check, not a broken sweep
            row.update(status="fail", errors=[f"the checks could not read the build: {e}"])
            return row
        row["errors"] = [f"{c['id']}: {c['message']}" for c in q["checks"] if not c["ok"] and c["level"] == "error"]
        row["warnings"] = [f"{c['id']}: {c['message']}" for c in q["checks"] if not c["ok"] and c["level"] != "error"]
        stale = bool(q.get("stale"))
        if kind == "car":
            row["donor"] = (q.get("car_qc") or {}).get("donor")
            row["converter"] = q.get("converter")
        row["status"] = "stale" if stale else ("fail" if row["errors"] else ("warn" if row["warnings"] else "ok"))
        return row

    # ------------------------------------------------------------------ sweeps
    def sweep(self, job=None, rebuild=None, only=None) -> dict:
        """Check every project (or those in `only`); rebuild what needs it when `rebuild` (default:
        the qc.auto_rebuild setting) and check those again."""
        conf = self.settings()
        rebuild = conf.get("auto_rebuild", True) if rebuild is None else bool(rebuild)
        log = job.log if job else (lambda *a: None)
        projects = []
        for summary in self.studio.projects.list():
            if only and summary["id"] not in only:
                continue
            try:
                projects.append(self.studio.projects.get(summary["id"]))
            except Exception:  # noqa: BLE001 - removed meanwhile
                continue
        rows = {}
        for i, p in enumerate(projects):
            if job:
                job.check()
                job.set_progress(0.4 * i / max(1, len(projects)), f"checking {p.data.get('name') or p.id}")
            rows[p.id] = self.check_project(p)
        if rebuild:
            todo = [r for r in rows.values() if r["status"] in FIX_STATUSES and r["kind"] in ("car", "map")]
            todo = [r for r in todo if self._may_rebuild(r)]
            limit = int(conf.get("max_rebuilds", 100))
            for i, r in enumerate(todo[:limit]):
                p = self.studio.projects.get(r["id"])
                if job:
                    job.check()
                    job.set_progress(0.4 + 0.6 * i / max(1, len(todo[:limit])),
                                     f"rebuilding {r['name']} ({i + 1}/{len(todo[:limit])})")
                log(f"{r['name']}: {r['status']} ({'; '.join((r['errors'] or ['-'])[:2])}): rebuilding")
                self._note_attempt(p)
                try:
                    bj = self.studio.build(p.id)
                    if job:
                        job.on_cancel.append(bj.cancel)
                    self.studio.jobs.wait(bj)
                except Exception as e:  # noqa: BLE001 - a project that cannot be queued is reported as is
                    log(f"{r['name']}: cannot rebuild: {e}")
                    rows[p.id]["errors"].append(f"rebuild: {e}")
                    continue
                rows[p.id] = self.check_project(self.studio.projects.get(p.id))
                rows[p.id]["rebuilt"] = True
                log(f"{r['name']}: {rows[p.id]['status']} after the rebuild")
        return self._publish(rows, partial=bool(only))

    def _may_rebuild(self, row) -> bool:
        a = (self.state.get("attempts") or {}).get(row["id"])
        if not a:
            return True
        try:
            p = self.studio.projects.get(row["id"])
        except Exception:  # noqa: BLE001 - deleted meanwhile
            return False
        return a.get("fingerprint") != self._fingerprint(p)

    def _note_attempt(self, p):
        with self.lock:
            self.state.setdefault("attempts", {})[p.id] = {"fingerprint": self._fingerprint(p), "time": _now()}
            self._save("state.json", self.state)

    def _publish(self, rows: dict, partial=False) -> dict:
        with self.lock:
            prev = (self.report or {}).get("projects") or {}
            merged = dict(prev) if partial else {}
            merged.update(rows)
            for pid in list(merged):
                try:
                    self.studio.projects.get(pid)
                except Exception:  # noqa: BLE001 - deleted since
                    merged.pop(pid, None)
            counts = {s: 0 for s in STATUS_ORDER}
            for r in merged.values():
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            rep = {"time": _now(), "converter": VERSION, "qc_version": car_qc.QC_VERSION,
                   "ok": all(r["status"] in ("ok", "warn", "skipped") for r in merged.values()),
                   "counts": counts, "projects": merged}
            self.report = rep
            self._save("report.json", rep)
            if not partial:
                with open(os.path.join(self.dir, "history.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"time": rep["time"], "converter": VERSION, "counts": counts,
                                        "failing": sorted(r["name"] for r in merged.values()
                                                          if r["status"] in FIX_STATUSES)}) + "\n")
            try:
                with open(os.path.join(self.dir, "report.html"), "w", encoding="utf-8") as f:
                    f.write(render_html(rep))
            except OSError:
                pass
        try:
            self.studio.bus.publish({"type": "qc", "counts": rep["counts"], "ok": rep["ok"], "time": rep["time"]})
        except Exception:  # noqa: BLE001
            pass
        return rep

    def view(self) -> dict:
        rep = self.report or {}
        rows = sorted((rep.get("projects") or {}).values(),
                      key=lambda r: (STATUS_ORDER.index(r["status"]) if r["status"] in STATUS_ORDER else 9, r["name"].lower()))
        conf = self.settings()
        nxt = None
        if conf.get("enabled", True) and rep.get("time"):
            nxt = rep["time"] + float(conf.get("interval_hours", 6)) * 3600
        return {"time": rep.get("time"), "converter": rep.get("converter"), "ok": rep.get("ok"),
                "counts": rep.get("counts") or {}, "projects": rows, "settings": conf, "next": nxt,
                "running": self.running.id if self.running is not None and self.running.state in ("queued", "running") else None,
                "schedule": schedule_status()}

    # ------------------------------------------------------------------ jobs and the clock
    def run_job(self, rebuild=None, only=None):
        with self.lock:
            if self.running is not None and self.running.state in ("queued", "running"):
                return self.running

            def run(job):
                job.log(f"quality sweep: converter {VERSION}, checks v{car_qc.QC_VERSION}"
                        + (", rebuilding what fails" if (rebuild if rebuild is not None else self.settings().get("auto_rebuild", True)) else ""))
                rep = self.sweep(job, rebuild=rebuild, only=only)
                c = rep["counts"]
                job.log("quality sweep: " + ", ".join(f"{v} {k}" for k, v in c.items() if v))
                return {"ok": rep["ok"], "counts": c,
                        "failing": [{"id": r["id"], "name": r["name"], "status": r["status"], "errors": r["errors"][:3]}
                                    for r in rep["projects"].values() if r["status"] in FIX_STATUSES]}

            self.running = self.studio.jobs.submit("qc", "Quality checks" + (" and rebuilds" if rebuild else ""), run)
            return self.running

    def after_build(self, pid):
        """A build finished: check that project again (quietly)."""
        try:
            p = self.studio.projects.get(pid)
            self._publish({pid: self.check_project(p)}, partial=True)
        except Exception:  # noqa: BLE001 - never let a report fail a build
            pass

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._clock, name="qc-clock", daemon=True)
        self.thread.start()

    def _clock(self):
        if self.stop.wait(FIRST_SWEEP_DELAY):
            return
        while not self.stop.is_set():
            conf = self.settings()
            if conf.get("enabled", True):
                last = (self.report or {}).get("time") or 0
                if _now() - last >= float(conf.get("interval_hours", 6)) * 3600 - 60:
                    try:
                        self.run_job()
                    except Exception:  # noqa: BLE001 - try again next round
                        pass
            if self.stop.wait(60):
                return

    def close(self):
        self.stop.set()


# ---------------------------------------------------------------------- Windows Task Scheduler
TASK_NAME = "fivem2open77 Studio quality checks"


def schedule_status() -> dict:
    """Whether Windows runs the sweep when the Studio is closed (schtasks)."""
    if os.name != "nt":
        return {"supported": False}
    import subprocess
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True,
                           timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return {"supported": True, "installed": False}
    return {"supported": True, "installed": r.returncode == 0}


def install_schedule(hours: int = 6, enable: bool = True) -> dict:
    """Register (or remove) the Windows scheduled task that runs ``python -m fivem2open77 qc`` every
    `hours` hours for this Windows user (no administrator rights needed)."""
    if os.name != "nt":
        raise RuntimeError("the background schedule is for Windows (Task Scheduler)")
    import subprocess
    import sys
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not enable:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True, timeout=30,
                       creationflags=flags)
        return schedule_status()
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(w):
            exe = w                                   # no console window flashing up
    cmd = f'"{exe}" -X utf8 -m fivem2open77 qc --rebuild'
    hours = max(1, min(24, int(hours)))
    r = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", str(hours), "/TR", cmd],
                       capture_output=True, text=True, timeout=30, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError("Task Scheduler refused the task: " + (r.stderr or r.stdout).strip()[:300])
    return schedule_status()


# ---------------------------------------------------------------------- report page
def render_html(rep: dict) -> str:
    def esc(x):
        return html.escape(str(x))
    rows = sorted((rep.get("projects") or {}).values(),
                  key=lambda r: (STATUS_ORDER.index(r["status"]) if r["status"] in STATUS_ORDER else 9, r["name"].lower()))
    color = {"ok": "#3fb950", "warn": "#d29922", "fail": "#f85149", "failed": "#f85149", "stale": "#db6d28",
             "unbuilt": "#8b949e", "skipped": "#8b949e"}
    trs = []
    for r in rows:
        issues = "<br>".join(esc(x) for x in (r.get("errors") or [])[:6] + (r.get("warnings") or [])[:4])
        trs.append(f"<tr><td>{esc(r['name'])}</td><td>{esc(r['kind'])}</td><td>{esc(r.get('build') or '')}</td>"
                   f"<td style='color:{color.get(r['status'], '#ccc')};font-weight:600'>{esc(r['status'])}</td>"
                   f"<td>{issues}</td></tr>")
    counts = ", ".join(f"{v} {k}" for k, v in (rep.get("counts") or {}).items() if v)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rep.get("time") or 0))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Quality report</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{{font:14px system-ui,sans-serif;background:#0d1117;color:#c9d1d9;margin:24px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #30363d;padding:6px 8px;text-align:left;vertical-align:top}}
th{{color:#8b949e;font-weight:500}}h1{{font-size:20px}}</style></head><body>
<h1>fivem2open77 quality report</h1><p>{esc(when)} · converter {esc(rep.get('converter'))} · {esc(counts)}</p>
<table><thead><tr><th>project</th><th>kind</th><th>build</th><th>status</th><th>issues</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></body></html>"""
