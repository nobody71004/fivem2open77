// f2o77_maps manager panel.  Talks to client/main.lua through the Open77 WebUI bridge:
//   Lua -> page   maps:init  maps:state  maps:baker  maps:open  maps:close  maps:select  maps:result
//                 maps:toast  maps:keys  editor:state
//   page -> Lua   maps:ready  maps:close  maps:edit  maps:preview  maps:op  maps:keys:rebind  maps:keys:reset
(function () {
  'use strict';
  const O = window.Open77 || { on() {}, emit() {}, ready() {} };
  // the page's own errors reach the game's log (red4ext/logs): Lua prints what maps:log carries
  const told = {};
  function report(level, text) {
    text = String(text).slice(0, 300);
    if (told[text]) return;
    told[text] = true;
    try { O.emit('maps:log', { level, text }); } catch (e) { /* nothing else to tell */ }
  }
  window.addEventListener('error', (ev) => report('error', `${ev.message} (${ev.filename || ''}:${ev.lineno || ''})`));
  window.addEventListener('unhandledrejection', (ev) => report('error', `promise: ${ev.reason}`));
  // A Lua table crosses the bridge as JSON, and an empty one comes out as an object ({}), never an
  // empty list: every list from Lua is read through arr().
  const arr = (v) => (Array.isArray(v) ? v : (v && typeof v === 'object' ? Object.values(v) : []));
  // every message from Lua is handled under a guard: one bad field never leaves the panel stale
  function guarded(name, fn) {
    return (p) => {
      try { return fn(p || {}); } catch (e) { report('error', `${name}: ${e && e.message ? e.message : e}`); return undefined; }
    };
  }
  const S = {
    maps: {}, order: [], rights: {}, config: {}, keys: [], menuKey: 'F9', baker: { known: false },
    selected: null, tab: 'maps', open: false, editor: { active: false },
    capturing: null, seq: 0, previewing: null, armed: null, requests: {},
  };
  const BUSY = ['requested', 'queued', 'baking'];
  const STAGED = ['built', 'restart_scheduled', 'waiting_empty'];

  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null) n.setAttribute(k, v);
    }
    (children || []).forEach((c) => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }
  const f2 = (v) => (Number.isFinite(+v) ? (+v).toFixed(2) : '-');
  const f1 = (v) => (Number.isFinite(+v) ? (+v).toFixed(1) : '-');
  function coords(a) {
    if (!a) return null;
    const frag = document.createDocumentFragment();
    [['x', f2(a.x)], ['y', f2(a.y)], ['z', f2(a.z)], ['yaw', f1(a.yaw)]].forEach(([k, v]) => {
      frag.appendChild(el('span', { class: 'k', text: k }));
      frag.appendChild(el('span', { class: 'v', text: v }));
    });
    return frag;
  }
  function setCoords(node, a, fallback) {
    node.textContent = '';
    const c = coords(a);
    if (c) node.appendChild(c); else node.textContent = fallback || '-';
  }
  function same(a, b) {
    if (!a || !b) return a === b;
    const dyaw = ((((a.yaw - b.yaw) % 360) + 540) % 360) - 180;
    return Math.abs(a.x - b.x) < 0.01 && Math.abs(a.y - b.y) < 0.01 && Math.abs(a.z - b.z) < 0.01 && Math.abs(dyaw) < 0.05;
  }
  function when(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
  }
  function countdownLabel(s) {
    s = +s || 0;
    return s >= 60 && s % 60 === 0 ? `${s / 60} min` : `${s} s`;
  }
  function toast(text, kind) {
    const t = el('div', { class: 'toast ' + (kind || ''), text: String(text) });
    $('toasts').appendChild(t);
    setTimeout(() => t.remove(), 4500);
    while ($('toasts').children.length > 4) $('toasts').firstChild.remove();
  }
  function request(map, op, args, done) {
    S.seq += 1;
    const seq = S.seq;
    S.requests[seq] = { map, op, done };
    O.emit('maps:op', { map, op, args: args || {}, request: seq });
    // an answer lost on the way must not keep a queue waiting forever
    if (done) setTimeout(() => { const r = S.requests[seq]; if (r && r.done) { delete S.requests[seq]; r.done(null); } }, 5000);
    return seq;
  }
  // two-click confirmation for actions that touch everyone (a server restart)
  function arm(key) {
    if (S.armed && S.armed.key === key && Date.now() - S.armed.at < 6000) { S.armed = null; return true; }
    S.armed = { key, at: Date.now() };
    setTimeout(() => { if (S.armed && S.armed.key === key) { S.armed = null; renderDetail(); } }, 6000);
    renderDetail();
    return false;
  }
  const armed = (key) => !!S.armed && S.armed.key === key;

  // a map's state as one word + chip class
  function status(m) {
    const a = m.apply || {};
    switch (a.state) {
      case 'requested': return [a.offline ? 'baker offline' : 'queued', a.offline ? 'failed' : 'busy'];
      case 'queued': return ['queued', 'busy'];
      case 'baking': return ['building', 'busy'];
      case 'restart_scheduled': return ['restart scheduled', 'restart'];
      case 'waiting_empty': return ['waits for empty server', 'restart'];
      case 'built': return ['built, next restart', 'restart'];
      case 'unstaging': return ['cancelling', 'busy'];
      case 'failed': return ['build failed', 'failed'];
      default: break;
    }
    if (m.pending && !same(m.pending, m.running)) return ['moved, not applied', 'pending'];
    return ['live', 'live'];
  }
  const canEdit = (name) => !!S.rights[name];

  // ---------------------------------------------------------------- render
  function renderList() {
    const list = $('maplist');
    list.textContent = '';
    $('empty').classList.toggle('hidden', S.order.length > 0);
    $('empty').textContent = S.loading ? 'The server is loading its maps...'
      : S.noAnswer ? 'The map manager on the server is not answering yet. If this stays, run MapTools\\Diagnose-Server.bat.'
        : S.answered === false ? 'Asking the server for its maps...'
          : 'No converted maps are installed on this server.';
    S.order.forEach((name) => {
      const m = S.maps[name];
      const [word, cls] = status(m);
      const where = m.running ? `${f1(m.running.x)}, ${f1(m.running.y)}, ${f1(m.running.z)}` : 'not placed';
      const item = el('button', {
        class: 'mapitem' + (S.selected === name ? ' selected' : ''), role: 'option', 'data-map': name,
        onclick: () => select(name),
      }, [
        el('div', {}, [el('div', { class: 'name', text: m.display || name }), el('div', { class: 'sub', text: where })]),
        el('span', { class: 'chip ' + cls, text: word }),
      ]);
      list.appendChild(item);
    });
  }

  function bakerLine(m) {
    const b = S.baker || {};
    if (!b.known) return ['Map baker: not reported yet. Moves need the f2o77-mapbaker service on the server host.', 'warn'];
    let s = `Map baker ${b.version || ''}: ready`;
    if (!b.hook) s += ' (no start hook: the new build is installed just before the restart)';
    const st = m && m.bake;
    if (st && st.state === 'failed' && st.log) s += ' — last error: ' + String(st.log).split('\n').filter(Boolean).slice(-1)[0];
    return [s, ''];
  }

  function renderDetail() {
    const name = S.selected;
    const m = name && S.maps[name];
    $('detail').classList.toggle('hidden', !m);
    if (!m) return;
    const edit = canEdit(name);
    $('d-title').textContent = m.display || name;
    const [word, cls] = status(m);
    const chip = $('d-chip');
    chip.textContent = word;
    chip.className = 'chip ' + cls;
    $('d-readonly').classList.toggle('hidden', edit);
    $('d-right').textContent = m.right || ('command.mlo.' + name);
    document.querySelectorAll('#detail .edit-only').forEach((n) => n.classList.toggle('hidden', !edit));
    setCoords($('d-running'), m.running, 'not placed yet');
    const prev = m.previous && !same(m.previous, m.running) ? m.previous : null;
    $('d-previous-row').classList.toggle('hidden', !edit || !prev);
    setCoords($('d-previous'), prev);
    setCoords($('d-pending'), m.pending, 'none saved: move it in the world, or type numbers below');
    // the numeric fields follow the server unless the user is typing in them
    const base = m.pending || m.running;
    const active = document.activeElement;
    [['f-x', 'x'], ['f-y', 'y'], ['f-z', 'z'], ['f-yaw', 'yaw']].forEach(([id, k]) => {
      const inp = $(id);
      if (active !== inp && base) inp.value = k === 'yaw' ? (+base[k]).toFixed(1) : (+base[k]).toFixed(2);
    });
    $('d-preview').checked = S.previewing === name;
    // presets
    const box = $('d-presets');
    box.textContent = '';
    const presets = arr(m.presets);
    if (!presets.length) box.appendChild(el('div', { class: 'none', text: 'No saved positions yet.' }));
    presets.forEach((p) => {
      const row = el('div', { class: 'preset' }, [
        el('span', { class: 'pname', text: p.name }),
        el('span', { class: 'pcoords', text: `${f1(p.anchor.x)} ${f1(p.anchor.y)} ${f1(p.anchor.z)} / ${f1(p.anchor.yaw)}` }),
      ]);
      if (edit) {
        row.appendChild(el('button', { text: 'Use', onclick: () => request(name, 'presetUse', { name: p.name }) }));
        row.appendChild(el('button', {
          class: 'ghost', text: armed('del:' + p.name) ? 'Sure?' : 'Delete',
          onclick: () => { if (arm('del:' + p.name)) request(name, 'presetDelete', { name: p.name }); },
        }));
      }
      box.appendChild(row);
    });
    // apply
    const a = m.apply || {};
    $('d-applystate').textContent = a.state ? a.state.replace(/_/g, ' ') : 'nothing in progress';
    // this game still has the old files of a moved map: say so, and offer the restart that fixes it
    const old = !!(S.stale && S.stale[name]);
    $('d-stale').classList.toggle('hidden', !old);
    document.querySelector('[data-op=relaunch]').classList.toggle('hidden', !S.relaunch);
    $('d-stale-manual').classList.toggle('hidden', !!S.relaunch);
    document.querySelector('[data-op=relaunch]').textContent = armed('relaunch') ? 'Confirm: close the game and restart it' : 'Restart the game';
    renderNext(m);
    let msg = a.message || '';
    if (a.state === 'failed' && m.bake && m.bake.log) msg += ' — ' + String(m.bake.log).split('\n').filter(Boolean).slice(-2).join(' ');
    $('d-applymsg').textContent = msg;
    $('d-applymsg').classList.toggle('error', a.state === 'failed' || !!a.offline);
    const moved = !!m.pending && !same(m.pending, m.running);
    const locked = BUSY.includes(a.state) || a.state === 'unstaging' || a.state === 'restart_scheduled';
    // countdown choices
    const sel = $('f-countdown');
    if (!sel.options.length) {
      (arr(S.config.restartCountdowns).length ? arr(S.config.restartCountdowns) : [30, 60, 120, 300]).forEach((s) => {
        sel.appendChild(el('option', { value: String(s), text: countdownLabel(s) }));
      });
      sel.value = String(S.config.defaultCountdown || 60);
    }
    const now = document.querySelector('[data-op=apply_now]');
    now.disabled = locked || !moved;
    now.textContent = armed('apply:' + name) ? `Confirm: restart in ${countdownLabel(sel.value)}` : 'Apply now';
    now.classList.toggle('confirm', armed('apply:' + name));
    document.querySelector('[data-op=apply_empty]').disabled = locked || !moved;
    document.querySelector('[data-op=cancel]').disabled = !(BUSY.includes(a.state) || STAGED.includes(a.state)) || a.state === 'baking';
    document.querySelector('[data-op=discard]').disabled = !m.pending;
    document.querySelector('[data-op=goto_pending]').disabled = !m.pending;
    const [bline, bcls] = bakerLine(m);
    $('d-baker').textContent = bline;
    $('d-baker').className = 'baker ' + bcls;
    // history
    const hist = $('d-history');
    hist.textContent = '';
    arr(m.history).slice(0, 25).forEach((h) => {
      const li = el('li', {}, [
        el('span', { text: when(h.at) + ' ' }), el('span', { class: 'who', text: String(h.by || '?') }),
        el('span', { text: ` ${h.what || ''} ` + (h.anchor ? `${f1(h.anchor.x)} ${f1(h.anchor.y)} ${f1(h.anchor.z)} / ${f1(h.anchor.yaw)}` : '') }),
      ]);
      hist.appendChild(li);
    });
  }

  function renderKeys() {
    const box = $('keys');
    box.textContent = '';
    arr(S.keys).forEach((k) => {
      const label = String(k.name || k.id).replace(/^Maps:\s*/, '');
      const keyBtn = el('button', {
        class: 'key' + (S.capturing === k.id ? ' capturing' : ''), 'data-key-id': k.id,
        text: S.capturing === k.id ? 'press a key…' : (k.key || '-'),
        onclick: (ev) => { ev.stopPropagation(); S.capturing = S.capturing === k.id ? null : k.id; renderKeys(); },
      });
      const row = el('div', { class: 'keyrow' }, [el('span', { class: 'label', text: label }), keyBtn]);
      row.appendChild(el('button', {
        class: 'ghost reset', text: 'Default (' + (k.defaultKey || '-') + ')', disabled: k.rebound ? null : 'disabled',
        onclick: () => O.emit('maps:keys:reset', { id: k.id }),
      }));
      box.appendChild(row);
    });
    $('foot-key').textContent = S.menuKey || 'F9';
  }

  function renderHud() {
    const e = S.editor;
    document.body.classList.toggle('editing', !!e.active);
    if (!e.active) return;
    $('h-map').textContent = e.display || e.map;
    setCoords($('h-coords'), e.anchor);
    $('h-speed').textContent = e.speed || 'normal';
    $('h-aim').classList.toggle('hidden', !e.aim);
    if (e.running && e.anchor) {
      const dx = e.anchor.x - e.running.x, dy = e.anchor.y - e.running.y, dz = e.anchor.z - e.running.z;
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
      const dyaw = ((((e.anchor.yaw - e.running.yaw) % 360) + 540) % 360) - 180;
      $('h-delta').textContent = `${dist.toFixed(1)} m and ${dyaw.toFixed(0)}° from where it is now`;
    } else {
      $('h-delta').textContent = '';
    }
    renderWire(e);
    const k = e.keys || {};
    const hint = $('h-keys');
    hint.textContent = '';
    [[k.move, 'move'], [k.height, 'height'], [k.rotate, 'rotate'], [k.fast, 'fast'], [k.fine, 'fine'],
      [k.aim, 'follow crosshair'], [k.ground, 'ground'], [k.save, 'save'], [k.cancel, 'cancel']].forEach(([key, what]) => {
      if (!key) return;
      hint.appendChild(el('span', { class: 'key', text: key }));
      hint.appendChild(document.createTextNode(what + '  '));
    });
  }

  // what the wireframe shows and how the map sits on the world's ground (client/main.lua's ground check)
  const WIRE_TEXT = {
    loading: ['reading the map...', 'quiet'],
    full: ['wireframe', 'live'],
    reduced: ['less detail: another resource is drawing', 'warn'],
    off: ['wireframe off', 'off'],
  };
  function renderWire(e) {
    const [text, cls] = WIRE_TEXT[e.wire] || ['wireframe', 'quiet'];
    const chip = $('h-wire');
    chip.className = 'chip ' + cls;
    chip.textContent = e.wire === 'off' && e.wireWhy ? text + ': ' + String(e.wireWhy).replace(/_/g, ' ') : text;
    $('lg-outline').textContent = e.outline === 'box' ? 'box (rebuild the map for its ground outline)' : 'ground outline';
    const fit = $('h-fit');
    fit.textContent = '';
    fit.className = 'fit';
    const f = e.fit;
    if (!f) {
      fit.textContent = e.outline === 'box' ? '' : 'ground check when it stands still';
      return;
    }
    if (f.here) { fit.textContent = 'this is where the map is now: move it to check the ground'; return; }
    if (f.far) { fit.textContent = `${Math.round(f.far)} m from you: come within 200 m to check the ground`; return; }
    const checked = (f.samples || 0) - (f.skipped || 0);
    if (checked <= 0) { fit.textContent = 'all of it is over where the map is now: move it further to check the ground'; return; }
    const parts = [];
    if (f.float > 0) parts.push(el('span', { class: 'float', text: `floats up to ${f1(f.float)} m` }));
    if (f.buried > 0) parts.push(el('span', { class: 'buried', text: `buried up to ${f1(f.buried)} m` }));
    if (f.gateFloat > 0) parts.push(el('span', { class: 'float', text: `gates ${f1(f.gateFloat)} m above the street` }));
    if (f.gateBuried > 0) parts.push(el('span', { class: 'buried', text: `gates ${f1(f.gateBuried)} m below the street` }));
    if (f.missing > 0) parts.push(el('span', { class: 'float', text: `${f.missing} of ${checked} points over no ground` }));
    const left = f.skipped > 0 ? ` (${f.skipped} over where it is now not checked)` : '';
    if (!parts.length) {
      fit.className = 'fit ok';
      fit.textContent = `sits on the ground: ${checked} points within 0.35 m${left}`;
      return;
    }
    fit.className = 'fit bad';
    parts.forEach((p, i) => { if (i) fit.appendChild(document.createTextNode(' · ')); fit.appendChild(p); });
    if (left) fit.appendChild(document.createTextNode(left));
  }

  // what happens next after Apply, in the player's words
  function renderNext(m) {
    const a = m.apply || {};
    const box = $('d-next');
    let lines = null;
    const back = 'Then start the game again from the Open77 launcher once the server is back (about a minute after the '
      + 'restart): starting it earlier keeps the old map files.';
    switch (a.state) {
      case 'requested': case 'queued': case 'baking':
        lines = ['The server is building the map at the new spot (seconds to a minute).',
          a.mode === 'empty' ? 'Then it restarts once nobody is online.' : 'Then the server restarts after the countdown.', back];
        break;
      case 'restart_scheduled':
        lines = ['Built. The server restarts' + (a.restartAt ? ' at ' + when(a.restartAt) : ' soon') + '.', back];
        break;
      case 'waiting_empty':
        lines = ['Built. The server restarts once nobody has been online for '
          + (S.config.emptyGraceSeconds || 30) + ' s: leave the game now.', back];
        break;
      case 'built':
        lines = ['Built: it goes live at the next server restart.', back];
        break;
      case 'applied':
        if (S.boot && a.appliedAt && a.appliedAt >= S.boot - 5) {
          lines = ['Moved at the last server restart.', 'If you still see it at its old spot, your game started before '
            + 'the server was back: restart it from the Open77 launcher.'];
        }
        break;
      default: break;
    }
    box.classList.toggle('hidden', !lines);
    box.textContent = '';
    if (!lines) return;
    const ol = el('ol');
    lines.forEach((t) => ol.appendChild(el('li', { text: t })));
    box.appendChild(el('b', { text: 'What happens next' }));
    box.appendChild(ol);
  }

  function render() { renderList(); renderDetail(); renderKeys(); }

  function select(name) {
    if (!S.maps[name]) return;
    if (S.previewing && S.previewing !== name) { O.emit('maps:preview', { map: S.previewing, on: false }); S.previewing = null; }
    S.selected = name;
    S.armed = null;
    render();
  }

  function setTab(tab) {
    S.tab = tab;
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === tab));
    $('tab-maps').classList.toggle('hidden', tab !== 'maps');
    $('tab-keys').classList.toggle('hidden', tab !== 'keys');
    S.capturing = null;
    renderKeys();
  }

  function close() {
    if (S.previewing) { O.emit('maps:preview', { map: S.previewing, on: false }); S.previewing = null; }
    O.emit('maps:close', {});
  }

  // ---------------------------------------------------------------- actions
  function num(id) {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) ? v : null;
  }
  const NUDGE = {
    fwd: { dy: 1, frame: 'map' }, back: { dy: -1, frame: 'map' }, left: { dx: -1, frame: 'map' }, right: { dx: 1, frame: 'map' },
    up: { dz: 0.25 }, down: { dz: -0.25 }, ccw: { dyaw: 5 }, cw: { dyaw: -5 },
  };

  // nudges go one at a time: clicks (or a held key) while one is on its way are summed into the next,
  // so the server gets a few moves a second however fast the button is hit
  const nudges = {};
  function nudge(name, d) {
    const q = nudges[name] || (nudges[name] = { busy: false, sum: null });
    const kind = d.dyaw ? 'turn' : (d.frame || 'world');
    if (q.sum && q.sum.kind !== kind) { (q.later = q.later || []).push(d); return undefined; }
    q.sum = q.sum || { kind, dx: 0, dy: 0, dz: 0, dyaw: 0, frame: d.frame };
    q.sum.dx += d.dx || 0; q.sum.dy += d.dy || 0; q.sum.dz += d.dz || 0; q.sum.dyaw += d.dyaw || 0;
    return flushNudge(name);
  }
  function flushNudge(name) {
    const q = nudges[name];
    if (!q || q.busy || !q.sum) return undefined;
    const s = q.sum;
    q.sum = null;
    q.busy = true;
    const args = {};
    ['dx', 'dy', 'dz', 'dyaw'].forEach((k) => { if (s[k]) args[k] = Math.round(s[k] * 1000) / 1000; });
    if (s.frame) args.frame = s.frame;
    return request(name, 'move', args, () => {
      q.busy = false;
      const later = q.later || [];
      q.later = null;
      later.forEach((d) => nudge(name, d));
      flushNudge(name);
    });
  }

  document.addEventListener('click', (ev) => {
    const t = ev.target.closest('button');
    if (S.capturing && !(t && t.classList.contains('key'))) { S.capturing = null; renderKeys(); }
    if (!t || t.disabled) return;
    if (t.id === 'close') return close();
    if (t.classList.contains('tab')) return setTab(t.dataset.tab);
    const name = S.selected;
    if (!name) return;
    if (t.dataset.nudge) return nudge(name, NUDGE[t.dataset.nudge]);
    const op = t.dataset.op;
    if (!op) return;
    switch (op) {
      case 'edit': return O.emit('maps:edit', { map: name });
      case 'goto_running': return request(name, 'goto_', { which: 'running' });
      case 'goto_pending': return request(name, 'goto_', { which: 'pending' });
      case 'here': return request(name, 'here', {});
      case 'drop': return request(name, 'drop', {});
      case 'discard': return request(name, 'discard', {});
      case 'revert': return request(name, 'revert', {});
      case 'save_fields': {
        const a = { x: num('f-x'), y: num('f-y'), z: num('f-z'), yaw: num('f-yaw') };
        if ([a.x, a.y, a.z].some((v) => v === null)) return toast('Type x, y and z first.', 'error');
        return request(name, 'place', a);
      }
      case 'preset_save': {
        const pname = $('f-preset').value.trim();
        if (!pname) return toast('Give the position a name.', 'error');
        $('f-preset').value = '';
        return request(name, 'presetSave', { name: pname });
      }
      case 'apply_now':
        if (!arm('apply:' + name)) return undefined;
        renderDetail();
        return request(name, 'apply', { mode: 'now', countdown: parseInt($('f-countdown').value, 10) || 60 });
      case 'apply_empty': return request(name, 'apply', { mode: 'empty' });
      case 'relaunch':
        if (!arm('relaunch')) return undefined;
        renderDetail();
        return O.emit('maps:relaunch', {});
      case 'cancel': return request(name, 'cancel', {});
      default: return undefined;
    }
  });

  $('d-preview').addEventListener('change', (ev) => {
    const name = S.selected;
    if (!name) return;
    S.previewing = ev.target.checked ? name : null;
    O.emit('maps:preview', { map: name, on: ev.target.checked });
  });
  $('f-countdown').addEventListener('change', () => renderDetail());

  // key capture for rebinding; the menu key and Escape close the panel (mapped keys do not fire
  // while this page owns the keyboard, so the page handles them itself)
  const KEYMAP = {
    ArrowUp: 'UP', ArrowDown: 'DOWN', ArrowLeft: 'LEFT', ArrowRight: 'RIGHT', PageUp: 'PAGEUP', PageDown: 'PAGEDOWN',
    Home: 'HOME', End: 'END', Insert: 'INSERT', Delete: 'DELETE', Backspace: 'BACKSPACE', Enter: 'ENTER', Tab: 'TAB',
    ' ': 'SPACE', Shift: 'SHIFT', Control: 'CTRL', Alt: 'ALT', CapsLock: 'CAPSLOCK',
  };
  function keyName(ev) {
    if (KEYMAP[ev.key]) return KEYMAP[ev.key];
    if (/^F([1-9]|1[0-2])$/.test(ev.key)) return ev.key;
    if (ev.key && ev.key.length === 1) return ev.key.toUpperCase();
    return null;
  }
  document.addEventListener('keydown', (ev) => {
    if (S.capturing) {
      ev.preventDefault();
      if (ev.key === 'Escape') { S.capturing = null; renderKeys(); return; }
      const k = keyName(ev);
      if (!k) return;
      O.emit('maps:keys:rebind', { id: S.capturing, key: k });
      S.capturing = null;
      renderKeys();
      return;
    }
    if (!S.open) return;
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes((document.activeElement || {}).tagName);
    if (ev.key === 'Escape') { ev.preventDefault(); close(); return; }
    // the menu key closes; a letter key only when the user is not typing it into a field
    if (keyName(ev) === S.menuKey && (!typing || ev.key.length !== 1)) { ev.preventDefault(); close(); return; }
    if (typing && ev.key === 'Enter' && document.activeElement.id === 'f-preset') {
      document.querySelector('[data-op=preset_save]').click();
    }
  });

  // ---------------------------------------------------------------- events from Lua
  O.on('maps:init', guarded('maps:init', (p) => {
    S.maps = {};
    S.order = [];
    const list = arr(p.maps);
    list.forEach((m) => { if (m && m.name) { S.maps[m.name] = m; S.order.push(m.name); } });
    S.rights = p.rights || {};
    S.config = p.config || {};
    if (p.keys) S.keys = arr(p.keys);
    S.menuKey = p.menuKey || S.menuKey;
    if (p.baker) S.baker = p.baker;
    S.loading = !!p.loading;
    S.noAnswer = !!p.noAnswer;
    S.answered = p.answered === undefined ? true : !!p.answered;
    S.relaunch = !!p.relaunch;
    S.stale = p.stale || {};
    S.boot = p.boot || S.boot;
    if (!S.selected || !S.maps[S.selected]) S.selected = S.order[0] || null;
    const sel = $('f-countdown');
    if (sel.options.length && p.config && p.config.restartCountdowns) sel.textContent = '';   // rebuilt by renderDetail
    render();
  }));
  O.on('maps:state', guarded('maps:state', (m) => {
    if (!m || !m.name) return;
    if (!S.order.includes(m.name)) S.order.push(m.name);
    S.maps[m.name] = m;
    if (!S.selected) S.selected = m.name;
    render();
  }));
  O.on('maps:baker', guarded('maps:baker', (b) => { S.baker = b || { known: false }; renderDetail(); }));
  O.on('maps:stale', guarded('maps:stale', (p) => {
    if (!p || !p.map) return;
    S.stale = S.stale || {};
    if (p.present) delete S.stale[p.map];      // a later look found it at its spot
    else S.stale[p.map] = true;
    if (p.relaunch !== undefined) S.relaunch = !!p.relaunch;
    render();
  }));
  O.on('maps:open', guarded('maps:open', () => {
    S.open = true;
    document.body.classList.add('open');
    setTab(S.tab);
    render();
    // opened without the maps (their message was lost while the page loaded): ask for them again
    if (!S.order.length) O.emit('maps:ready', { again: true });
  }));
  // Lua knocks until the page answers: the first messages of a page that loads fast can be lost
  O.on('maps:ping', guarded('maps:ping', () => O.emit('maps:ready', { maps: S.order.length })));
  O.on('maps:close', guarded('maps:close', () => {
    S.open = false;
    S.capturing = null;
    S.armed = null;
    document.body.classList.remove('open');
    if (S.previewing) S.previewing = null;
  }));
  O.on('maps:select', guarded('maps:select', (p) => { if (p && p.map) select(p.map); }));
  O.on('maps:result', guarded('maps:result', (r) => {
    const req = S.requests[r.request] || {};
    delete S.requests[r.request];
    if (req.done) req.done(r);
    if (!r.ok) return toast(`${r.op || req.op}: ${String(r.reason || 'failed').replace(/_/g, ' ')}`, 'error');
    const done = {
      place: 'New spot saved.', move: null, here: 'New spot: where you stand.', drop: 'Dropped onto the ground.',
      discard: 'New spot discarded.', goto_: null, presetSave: 'Position saved.', presetUse: 'Saved position loaded as the new spot.',
      presetDelete: 'Saved position deleted.', apply: 'Sent to the map baker.', cancel: 'Cancelled.',
      revert: 'The previous spot is the new spot: Apply to move back.',
    }[r.op];
    if (done) toast(done, 'ok');
    return undefined;
  }));
  O.on('maps:toast', guarded('maps:toast', (t) => toast(t.text, t.kind)));
  O.on('maps:keys', guarded('maps:keys', (p) => {
    S.keys = p.keys ? arr(p.keys) : S.keys;
    S.menuKey = p.menuKey || S.menuKey;
    if (p.error) { $('keys-error').textContent = 'Not changed: ' + p.error; $('keys-error').classList.remove('hidden'); }
    else $('keys-error').classList.add('hidden');
    renderKeys();
  }));
  O.on('editor:state', guarded('editor:state', (e) => { S.editor = e || { active: false }; renderHud(); }));
  // an agent at work: what it last said, and whether it holds the camera (F9 takes the view back)
  function renderAgent() {
    const fresh = S.agentAt && Date.now() - S.agentAt < 45000;
    $('agent').classList.toggle('hidden', !(fresh || S.agentView));
    $('a-by').textContent = S.agentBy || 'Agent';
    $('a-text').textContent = fresh ? (S.agentText || '') : '';
    $('a-view').classList.toggle('hidden', !S.agentView);
    $('a-view').textContent = S.agentView ? `${S.agentView} \u00b7 ${S.menuKey} takes your view back` : '';
  }
  O.on('maps:agent', guarded('maps:agent', (p) => {
    if (p.text !== undefined) {
      S.agentText = String(p.text);
      S.agentBy = String(p.by || 'Agent');
      S.agentAt = Date.now();
      setTimeout(renderAgent, 45500);
    }
    if (p.view !== undefined) S.agentView = p.view ? `camera: ${p.mode} view of ${p.map}` : null;
    renderAgent();
  }));

  // tests drive the page through this (and so can the debug console)
  window.__mapsState = S;

  O.ready();                      // the runtime's ui:ready: Lua may send from now on
  O.emit('maps:ready', {});
})();
