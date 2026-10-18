"""Background jobs (inspect, build, deploy, agent runs...) and the event stream the UI listens to.

A job runs in its own thread; its log lines, progress and state changes are published on the
EventBus, which the /api/events endpoint relays to every open UI as server-sent events.
"""
from __future__ import annotations

import contextlib
import queue
import threading
import time
import traceback
import uuid


class EventBus:
    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()
        self.seq = 0

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict):
        with self._lock:
            self.seq += 1
            event = dict(event, seq=self.seq, t=time.time())
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass        # a stalled client misses events; it re-reads state on reconnect


class JobCancelled(Exception):
    pass


class Job:
    MAX_LOG = 20000

    def __init__(self, manager, kind, title, project=None, meta=None):
        self.id = uuid.uuid4().hex[:12]
        self.manager = manager
        self.kind = kind
        self.title = title
        self.project = project
        self.meta = meta or {}
        self.state = "queued"
        self.progress = None
        self.lines = []
        self.result = None
        self.error = None
        self.created = time.time()
        self.started = None
        self.finished = None
        self.cancel_event = threading.Event()
        self.on_cancel = []            # callables run when cancel is requested (kill a subprocess...)
        self.cancel_late = None        # the step a cancel came during, when that step can't stop half-way
        self._shields = []
        self._lock = threading.Lock()

    # ---- used by job functions
    def log(self, *parts):
        text = " ".join(str(p) for p in parts)
        for line in text.splitlines() or [""]:
            entry = {"t": time.time(), "text": line}
            self.lines.append(entry)
            if len(self.lines) > self.MAX_LOG:
                del self.lines[: len(self.lines) - self.MAX_LOG]
            self.manager.bus.publish({"type": "log", "job": self.id, "text": line})

    def set_progress(self, p, label=None):
        self.progress = None if p is None else max(0.0, min(1.0, float(p)))
        if label:
            self.meta["step"] = label
        self.manager.bus.publish({"type": "job", "job": self.summary()})

    def check(self):
        if self.cancel_event.is_set():
            raise JobCancelled()

    @contextlib.contextmanager
    def shield(self, why):
        """A step that must not stop half-way (a server installing files, restarting): a cancel asked
        for inside waits for its end, then stops the job before anything that follows."""
        with self._lock:
            self._shields.append(why)
        try:
            yield
        finally:
            with self._lock:
                self._shields.pop()
                late = self.cancel_late is not None and not self._shields and not self.cancel_event.is_set()
            if late:
                self._cancel_now(f"stopping now: {self.cancel_late} is over")

    # ---- control
    def cancel(self):
        """Stop the job: at once, or, inside a step that must not stop half-way, right after it.
        False when the job has already ended."""
        if self.state in ("done", "failed", "cancelled"):
            return False
        with self._lock:
            step = self._shields[-1] if self._shields and not self.cancel_event.is_set() else None
            first = step is not None and self.cancel_late is None
            if first:
                self.cancel_late = step
        if step is not None:
            if first:
                self.log(f"cancel requested during {step}: that can't stop half-way; the job stops as soon as it is over")
                self.manager.bus.publish({"type": "job", "job": self.summary()})
            return True
        self._cancel_now("cancel requested")
        return True

    def _cancel_now(self, msg):
        self.cancel_event.set()
        for fn in list(self.on_cancel):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        self.log(msg)
        self.manager.bus.publish({"type": "job", "job": self.summary()})

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "project": self.project, "state": self.state,
                "progress": self.progress, "step": self.meta.get("step"), "error": self.error,
                "created": self.created, "started": self.started, "finished": self.finished,
                "lines": len(self.lines),
                "stopping": self.state in ("queued", "running")
                and (self.cancel_event.is_set() or self.cancel_late is not None)}

    def detail(self, since=0) -> dict:
        d = self.summary()
        d["log"] = self.lines[since:]
        d["result"] = self.result
        d["meta"] = {k: v for k, v in self.meta.items() if k != "step"}
        return d


class JobManager:
    """Heavy jobs (WolvenKit) run one at a time; light ones in parallel."""

    def __init__(self, bus: EventBus, keep=200):
        self.bus = bus
        self.jobs = {}
        self.order = []
        self.keep = keep
        self.heavy = threading.Semaphore(1)
        self.lock = threading.Lock()

    def submit(self, kind, title, fn, *, project=None, heavy=False, meta=None) -> Job:
        job = Job(self, kind, title, project, meta)
        with self.lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            while len(self.order) > self.keep:
                old = self.order.pop(0)
                if self.jobs.get(old) and self.jobs[old].state in ("done", "failed", "cancelled"):
                    del self.jobs[old]
                else:
                    self.order.insert(0, old)
                    break
        self.bus.publish({"type": "job", "job": job.summary()})
        threading.Thread(target=self._run, args=(job, fn, heavy), daemon=True, name=f"job-{kind}-{job.id}").start()
        return job

    def _run(self, job, fn, heavy):
        if heavy:
            if not self.heavy.acquire(blocking=False):
                job.meta["step"] = "waiting for the running build"
                self.bus.publish({"type": "job", "job": job.summary()})
                while not self.heavy.acquire(timeout=0.5):
                    if job.cancel_event.is_set():
                        job.state = "cancelled"
                        job.finished = time.time()
                        self.bus.publish({"type": "job", "job": job.summary()})
                        return
        try:
            job.state = "running"
            job.started = time.time()
            self.bus.publish({"type": "job", "job": job.summary()})
            job.result = fn(job)
            if job.cancel_event.is_set() and job.cancel_late is None:
                job.state = "cancelled"
            else:
                job.state = "done"
                if job.cancel_late is not None:
                    # the cancel came during a step that could not stop, and nothing came after it
                    job.log(f"finished: the cancel came during {job.cancel_late}, which completed, and nothing "
                            "was left after it")
        except JobCancelled:
            job.state = "cancelled"
        except Exception as e:  # noqa: BLE001 - reported to the UI
            if job.cancel_event.is_set():
                job.state = "cancelled"
                if getattr(e, "log_on_cancel", False) and str(e):
                    job.log(str(e))            # what the stop left done (a deploy: where it installed)
            else:
                job.state = "failed"
                job.error = str(e) or e.__class__.__name__
                job.log(f"error: {job.error}")
                tb = traceback.format_exc()
                job.meta["traceback"] = tb[-4000:]
        finally:
            job.finished = time.time()
            if job.progress is not None and job.state == "done":
                job.progress = 1.0
            if heavy:
                self.heavy.release()
            self.bus.publish({"type": "job", "job": job.summary()})

    def get(self, jid) -> Job | None:
        return self.jobs.get(jid)

    def list(self, project=None) -> list:
        with self.lock:
            items = [self.jobs[j] for j in self.order if j in self.jobs]
        if project:
            items = [j for j in items if j.project == project]
        return [j.summary() for j in reversed(items)]

    def wait(self, job: Job, timeout=None) -> Job:
        t0 = time.time()
        while job.state in ("queued", "running"):
            if timeout is not None and time.time() - t0 > timeout:
                break
            time.sleep(0.1)
        return job
