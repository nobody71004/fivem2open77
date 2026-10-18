// Quality: the around-the-clock checks of every car and map (qcsweep.py) -- what passes, what was
// built before a converter fix (stale), what fails a check and why, and the schedule that keeps it so.
import { api, waitJob } from "../api.js";
import { onEvent } from "../store.js";
import { h, clear, toast, fmtAgo, fmtTime, icon } from "../ui.js";

const LABEL = { ok: "ok", warn: "ok, notes", fail: "fails a check", failed: "build failed", stale: "built before a fix",
  unbuilt: "not built", skipped: "skipped" };
const PILL = { ok: "ok", warn: "warn", fail: "err", failed: "err", stale: "warn", unbuilt: "", skipped: "" };

export function qualityPage(root) {
  const head = h("div.page-head");
  const summary = h("div.card");
  const tbody = h("tbody");
  const filter = h("select", { onchange: () => rows() },
    h("option", { value: "issues" }, "needs attention"), h("option", { value: "all" }, "every project"),
    ...Object.keys(LABEL).map((k) => h("option", { value: k }, LABEL[k])));
  let view = null;
  let busy = false;

  async function load() {
    try {
      view = await api.get("/qc");
    } catch (e) {
      clear(summary).append(h("div.err", e.message));
      return;
    }
    draw();
  }

  function draw() {
    const v = view || {};
    const c = v.counts || {};
    clear(head).append(h("h1", "Quality"),
      h("span.sub", v.time ? `checked ${fmtAgo(v.time)} · converter ${v.converter}` : "not checked yet"),
      h("div.grow"),
      h("button.btn", { onclick: () => run(false), disabled: busy || !!v.running }, icon("refresh", "sm"), "Check now"),
      h("button.btn.primary", { onclick: () => run(true), disabled: busy || !!v.running }, icon("build", "sm"),
        "Check and rebuild what fails"));
    const tiles = ["ok", "warn", "stale", "fail", "failed", "unbuilt"].map((k) =>
      h("div.qc-tile." + (PILL[k] || "none"), { onclick: () => { filter.value = k; rows(); } },
        h("b", String(c[k] || 0)), h("span", LABEL[k])));
    const s = v.settings || {};
    const sched = v.schedule || {};
    const save = async (patch) => {
      try {
        await api.patch("/settings", { qc: patch });
        toast("Saved", "ok");
      } catch (e) { toast(e.message, "err"); }
      load();
    };
    const controls = h("div.row", { style: { gap: "14px", marginTop: "10px", flexWrap: "wrap", alignItems: "center" } },
      h("label", h("input", { type: "checkbox", checked: s.enabled !== false, onchange: (e) => save({ enabled: e.target.checked }) }),
        " check automatically"),
      h("label", "every ", h("select", { onchange: (e) => save({ interval_hours: Number(e.target.value) }) },
        [1, 3, 6, 12, 24].map((n) => h("option", { value: n, selected: Number(s.interval_hours || 6) === n }, `${n} h`)))),
      h("label", h("input", { type: "checkbox", checked: s.auto_rebuild !== false, onchange: (e) => save({ auto_rebuild: e.target.checked }) }),
        " rebuild what fails or was built before a fix"));
    clear(summary).append(
      h("div.qc-tiles", tiles), controls,
      h("p.muted", { style: { margin: "10px 0 0" } },
        s.enabled === false ? "Automatic checks are off (Settings)." :
          `Every car and map is checked every ${s.interval_hours || 6} h while the Studio runs` +
          (s.auto_rebuild ? ", and rebuilt when it fails a check or was built before a converter fix" : "") +
          (v.next ? ` · next ${fmtTime(v.next)}` : "") + "."),
      sched.supported ? h("p.muted", { style: { margin: "6px 0 0" } },
        sched.installed ? "Windows also runs the checks every few hours when the Studio is closed. "
                        : "When the Studio is closed nothing checks: ",
        h("button.btn.small", { onclick: () => schedule(!sched.installed) },
          sched.installed ? "Stop the background checks" : "Run them in the background (Task Scheduler)")) : null,
      v.running ? h("p", h("span.spin"), " a sweep is running — ", h("a", { href: "#/jobs" }, "see Activity")) : null);
    rows();
  }

  function rows() {
    clear(tbody);
    const want = filter.value;
    const list = ((view && view.projects) || []).filter((r) => want === "all" ? true :
      want === "issues" ? !["ok", "warn"].includes(r.status) || (r.warnings || []).length : r.status === want);
    if (!list.length) tbody.append(h("tr", h("td", { colSpan: 5 }, h("span.faint", "nothing here"))));
    for (const r of list) {
      const href = r.kind === "map" ? `#/map/${r.id}/overview` : r.kind === "world" ? `#/world/${r.id}` : `#/project/${r.id}/overview`;
      const issues = [...(r.errors || []).map((e) => h("div.err", e)), ...(r.warnings || []).slice(0, 4).map((w) => h("div.muted", w))];
      tbody.append(h("tr",
        h("td", h("a", { href }, r.name)), h("td.muted", r.kind + (r.donor ? ` · ${r.donor}` : "")),
        h("td.num", r.build ? `#${r.build}` : ""),
        h("td", h("span.pill" + (PILL[r.status] ? "." + PILL[r.status] : ""), LABEL[r.status] || r.status),
          r.rebuilt ? h("span.muted", " rebuilt") : null),
        h("td", { style: { fontSize: "12px" } }, issues)));
    }
  }

  async function run(rebuild) {
    busy = true;
    draw();
    try {
      const j = await api.post("/qc/run", { rebuild });
      toast(rebuild ? "Checking every project, then rebuilding what fails" : "Checking every project", "");
      const done = await waitJob(j.id, () => {});
      toast(done.state === "done" ? "Quality checks done" : `Quality checks ${done.state}`, done.state === "done" ? "ok" : "err");
    } catch (e) {
      toast(e.message, "err");
    } finally {
      busy = false;
      load();
    }
  }

  async function schedule(enable) {
    try {
      await api.post("/qc/schedule", { enable });
      toast(enable ? "Windows will run the checks in the background" : "Background checks removed", "ok");
    } catch (e) {
      toast(e.message, "err");
    }
    load();
  }

  const off = onEvent((ev) => { if (ev.type === "qc" || (ev.type === "job" && ev.job.kind === "qc" && ev.job.state !== "running")) load(); });
  root.append(head, summary,
    h("div.card", h("div.row", h("h3", "Projects"), h("div.grow"), filter),
      h("table.tbl", h("thead", h("tr", h("th", "project"), h("th", "kind"), h("th.num", "build"), h("th", "status"),
        h("th", "what the checks found"))), tbody)));
  load();
  return { dispose: off };
}
