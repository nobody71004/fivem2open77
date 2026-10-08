// Shared client state: /api/state, live jobs, and an event fan-out for the pages.
import { api } from "./api.js";

export const store = {
  state: null,              // /api/state
  jobs: new Map(),          // id -> summary
  listeners: new Set(),     // fn(event)
  emit(ev) { for (const fn of [...this.listeners]) { try { fn(ev); } catch (e) { console.error(e); } } },
  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); },
};

export async function refreshState() {
  store.state = await api.get("/state");
  for (const j of store.state.jobs) store.jobs.set(j.id, j);
  store.emit({ type: "state" });
  return store.state;
}

// run fn(ev) for live events until the returned disposer is called
export function onEvent(fn) { return store.on(fn); }
