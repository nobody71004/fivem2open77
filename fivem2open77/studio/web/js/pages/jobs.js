// Activity: every job (builds, installs, agent runs) with its log.
import { api, waitJob } from "../api.js";
import { store, onEvent } from "../store.js";
import { h, clear, fmtTime, pill, logLines } from "../ui.js";

export function jobsPage(root) {
  const tbody = h("tbody");
  const detail = h("div");
  let openId = null;

  function rows() {
    clear(tbody);
    const jobs = [...store.jobs.values()].sort((a, b) => b.created - a.created);
    if (!jobs.length) tbody.append(h("tr", h("td", { colSpan: 5 }, h("span.faint", "nothing has run yet"))));
    for (const j of jobs) {
      const prog = j.progress != null && j.state === "running" ? ` ${(j.progress * 100).toFixed(0)}%` : "";
      tbody.append(h("tr", { style: { cursor: "pointer" }, onclick: () => show(j.id) },
        h("td", fmtTime(j.created)), h("td", j.title), h("td", pill(j.state), h("span.muted", prog)),
        h("td.muted", j.step || j.error || ""),
        h("td", ["running", "queued"].includes(j.state) ? h("button.btn.small.danger", { onclick: (e) => { e.stopPropagation(); api.post(`/jobs/${j.id}/cancel`); } }, "Cancel") : "")));
    }
  }

  async function show(id) {
    openId = id;
    const log = h("div.log", { style: { maxHeight: "50vh" } });
    const head = h("div.row");
    clear(detail).append(h("div.card", head, log));
    const j = await waitJob(id, (jj) => {
      if (openId !== id) return;
      clear(head).append(h("h3", { style: { margin: 0 } }, jj.title), pill(jj.state));
      clear(log).append(...logLines(jj.allLines));
      log.scrollTop = log.scrollHeight;
    });
    if (j.meta && j.meta.traceback && openId === id) log.append(h("div.e", j.meta.traceback));
  }

  const off = onEvent((ev) => { if (ev.type === "job") rows(); });
  root.append(h("div.page-head", h("h1", "Activity")),
    h("div.card", h("table.tbl", h("thead", h("tr", h("th", "when"), h("th", "what"), h("th", "state"), h("th", ""), h("th", ""))), tbody)), detail);
  rows();
  return { dispose: off };
}
