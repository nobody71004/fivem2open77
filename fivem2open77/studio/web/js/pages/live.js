// Live maps: every converted map on a server, moved through the map manager (see place.js).
import { api } from "../api.js";
import { store } from "../store.js";
import { h } from "../ui.js";
import { placePanel } from "./place.js";

export function livePage(root, args) {
  const [sid, folder, map] = [args[0], args[1] ? decodeURIComponent(args[1]) : null, args[2] || null];
  root.append(h("div.page-head", h("h1", "Live maps"), h("div.grow"), h("a.btn.ghost", { href: "#/maps" }, "Map projects")),
    h("p.muted", "The maps installed on a server, as its map manager (f2o77_maps) has them: where each stands, the new spot being set up, presets, and Apply. ",
      "The same operations as the in-game panel (F9), through the Studio's SSH connection."));
  const lookFor = async (name) => {
    const p = (store.state.projects || []).find((x) => x.kind === "map" && x.map && x.map.name === name);
    if (!p) return null;
    const st = await api.get(`/projects/${p.id}/map`);
    return st.look ? { pid: p.id, look: st.look, lookKey: st.look_key } : null;
  };
  const dispose = placePanel(root, { server: sid || store.state.settings.deploy.default_server, folder, mapName: null, initialMap: map, lookFor });
  return { dispose };
}
