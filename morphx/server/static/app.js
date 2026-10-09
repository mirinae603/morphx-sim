/* morphx ops console. Plain JavaScript, no libraries.
 * Everything that comes from the server is put on the page with textContent, never innerHTML. */
(() => {
  'use strict';

  // ───────────────────────────── helpers ─────────────────────────────
  const $ = (sel, root = document) => root.querySelector(sel);

  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  const store = {
    get(key, fallback) { try { return localStorage.getItem('morphx.' + key) ?? fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem('morphx.' + key, value); } catch { /* private mode */ } },
  };

  const pad2 = (n) => String(n).padStart(2, '0');
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const timeOf = (iso) => (iso ? iso.slice(11, 23) : '—');
  const shortTime = (iso) => (iso ? iso.slice(11, 19) : '—');

  function dur(ms) {
    if (ms == null || Number.isNaN(ms)) return '—';
    if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}s`;
    const s = Math.round(ms / 1000);
    return s < 3600 ? `${Math.floor(s / 60)}m${pad2(s % 60)}s` : `${Math.floor(s / 3600)}h${pad2(Math.floor(s / 60) % 60)}m`;
  }

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function withAlpha(hex, alpha) {
    let c = hex.replace('#', '');
    if (c.length === 3) c = [...c].map((x) => x + x).join('');
    const n = parseInt(c, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
  }

  // ───────────────────────────── constants & state ─────────────────────────────
  const RANGE = { wbc: [4, 11], rbc: [4.2, 6.1], hb: [12, 17.5] }; // synthetic, display only
  const UNIT = { wbc: '10^3/uL', rbc: '10^6/uL', hb: 'g/dL' };
  const THEMES = ['mono', 'green', 'amber', 'ice'];
  const THEME_NAME = { mono: 'graphite', green: 'phosphor', amber: 'amber', ice: 'ice' };
  const WINDOW = 300; // how many recent arrivals we pull for the selected device

  const flagOf = (metric, v) => (v < RANGE[metric][0] ? 'L' : v > RANGE[metric][1] ? 'H' : '');

  const S = {
    theme: store.get('theme', 'mono'),
    fx: store.get('fx', 'on') === 'on',
    paused: false,
    order: 'seq', // 'seq' | 'arrival'
    filter: '',
    selected: null, // device id
    selRow: null, // arrival id of the highlighted record
    info: null,
    infoAt: 0,
    devices: [],
    rows: [], // selected device's recent records, arrival ascending, with lag/late
    link: true,
    offset: 0, // server clock minus browser clock, ms
    agentUrls: store.get('agents', ''), // empty = auto-detect on the default ports
    agents: {}, // url -> { ok, data }
    prevOnline: {},
    prevCounters: null,
    seen: {}, // device -> highest arrival id already logged
    history: [], // [{t, total}] for throughput
    log: [],
    lastArrivalRendered: 0,
    hover: null, // { id, i }
    outageTotal: 0,
    busy: false,
  };

  const serverNow = () => Date.now() + S.offset;

  // ───────────────────────────── api ─────────────────────────────
  async function api(path, opts = {}) {
    const t0 = performance.now();
    const res = await fetch(path, { cache: 'no-store', ...opts });
    const text = await res.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = text; }
    return { ok: res.ok, status: res.status, body, ms: performance.now() - t0 };
  }

  async function fetchAgent(url) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 1500);
    try {
      const res = await fetch(url.replace(/\/+$/, '') + '/status', { signal: ctl.signal, cache: 'no-store' });
      if (!res.ok) throw new Error(res.status);
      return { ok: true, data: await res.json() };
    } catch {
      return { ok: false };
    } finally {
      clearTimeout(timer);
    }
  }

  const AUTO_PORTS = [8001, 8002, 8003, 8004];
  const typedUrls = () => S.agentUrls.split(',').map((u) => u.trim()).filter(Boolean);
  const agentUrls = () => (typedUrls().length ? typedUrls() : AUTO_PORTS.map((p) => `http://127.0.0.1:${p}`));
  const agentNext = {}; // url -> time before which we do not ask again (it was unreachable)
  const seenAgents = new Set();

  async function pollAgents() {
    const urls = agentUrls();
    await Promise.all(urls.map(async (url) => {
      if (agentNext[url] && Date.now() < agentNext[url]) return;
      const res = await fetchAgent(url);
      S.agents[url] = res;
      if (res.ok) seenAgents.add(url);
      // an agent that was there is retried quickly; a port nobody ever answered on, rarely
      agentNext[url] = res.ok ? 0 : Date.now() + (seenAgents.has(url) ? 4000 : 20000);
    }));
    for (const url of Object.keys(S.agents)) if (!urls.includes(url)) delete S.agents[url];
  }
  const agentFor = (deviceId) => Object.values(S.agents).find((a) => a.ok && a.data.device_id === deviceId);

  // ───────────────────────────── log ─────────────────────────────
  function addLog(kind, text) {
    const d = new Date(serverNow());
    S.log.push({ kind, text, at: `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}` });
    if (S.log.length > 200) S.log.shift();
  }

  function renderLog() {
    const box = $('#log');
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.replaceChildren(...S.log.map((l) => h('div', { class: l.kind }, h('span', { class: 't' }, l.at), l.text)));
    if (stick) box.scrollTop = box.scrollHeight;
  }

  // ───────────────────────────── derive ─────────────────────────────
  function derive(itemsNewestFirst) {
    let maxSeq = 0;
    return [...itemsNewestFirst].reverse().map((it) => {
      const lag = Date.parse(it.received_at) - Date.parse(it.measured_at);
      const late = it.sequence < maxSeq; // arrived after a higher sequence was already there
      maxSeq = Math.max(maxSeq, it.sequence);
      return { ...it, lag, late };
    });
  }

  function cadenceMs() {
    const a = agentFor(S.selected);
    if (a) return a.data.interval * 1000;
    const seqs = [...S.rows].sort((x, y) => x.sequence - y.sequence).slice(-9);
    const diffs = [];
    for (let i = 1; i < seqs.length; i++) diffs.push(Date.parse(seqs[i].measured_at) - Date.parse(seqs[i - 1].measured_at));
    diffs.sort((x, y) => x - y);
    return diffs.length ? clamp(diffs[Math.floor(diffs.length / 2)], 200, 60000) : 5000;
  }

  function trackLogs() {
    const dev = S.selected;
    if (!dev) return;
    const top = S.rows.length ? S.rows[S.rows.length - 1].arrival : 0;
    if (S.seen[dev] == null) {
      S.seen[dev] = top;
      addLog('info', `attached to ${dev} · ${S.rows.length} record(s) in view`);
      return;
    }
    const fresh = S.rows.filter((r) => r.arrival > S.seen[dev]);
    S.seen[dev] = top;
    if (!fresh.length) return;
    if (fresh.length <= 6) {
      for (const r of fresh) addLog(r.lag > 3000 ? 'warn' : 'ok', `▲ #${r.sequence} stored · lag ${dur(r.lag)}${r.late ? ' · LATE' : ''}`);
    } else {
      const seqs = fresh.map((r) => r.sequence);
      const oldest = Math.max(...fresh.map((r) => r.lag));
      addLog('warn', `↺ backlog flush: ${fresh.length} records at once (seq ${Math.min(...seqs)}–${Math.max(...seqs)}), oldest waited ${dur(oldest)}`);
    }
  }

  function trackCounters(c) {
    const prev = S.prevCounters;
    S.prevCounters = c;
    if (!prev) return;
    const say = {
      duplicate: ['info', (n) => `◆ ${n} duplicate upload(s) absorbed (event already stored → 200)`],
      conflict: ['bad', (n) => `✗ ${n} conflict(s): sequence already used by another record → 409`],
      invalid: ['bad', (n) => `✗ ${n} invalid upload(s) refused → 422`],
      unavailable: ['warn', (n) => `⚠ ${n} upload(s) refused during simulated outage → 503`],
    };
    for (const [key, [kind, fmt]] of Object.entries(say)) {
      const n = c[key] - (prev[key] || 0);
      if (n > 0) addLog(kind, fmt(n));
    }
  }

  function trackAgents() {
    for (const [url, a] of Object.entries(S.agents)) {
      const now = a.ok ? a.data.online : 'gone';
      const was = S.prevOnline[url];
      S.prevOnline[url] = now;
      if (was === undefined || was === now) continue;
      const who = a.ok ? a.data.device_id : url;
      if (now === false) addLog('bad', `✗ agent ${who} cannot reach the server, measuring continues and the backlog builds (${a.data.last_error})`);
      else if (now === true) addLog('ok', `✔ agent ${who} reached the server again, draining its backlog`);
      else if (now === 'gone') addLog('warn', `agent status ${url} is not reachable`);
    }
  }

  // ───────────────────────────── polling ─────────────────────────────
  function setLink(ok) {
    if (ok === S.link) return;
    S.link = ok;
    addLog(ok ? 'ok' : 'bad', ok ? '✔ link to server restored' : '✗ link to server lost, retrying every second');
  }

  async function tick() {
    if (S.busy || S.paused) return;
    S.busy = true;
    try {
      const [info, devs] = await Promise.all([api('/v1/info'), api('/v1/devices')]);
      if (!info.ok || !devs.ok) throw new Error('bad status');
      S.info = info.body;
      S.infoAt = Date.now();
      S.offset = Date.parse(S.info.now) - Date.now();
      S.devices = devs.body.devices;
      if (!S.selected || !S.devices.some((d) => d.device_id === S.selected)) S.selected = S.devices[0]?.device_id ?? null;
      if (S.selected) {
        const rec = await api(`/v1/recent?limit=${WINDOW}&device_id=${encodeURIComponent(S.selected)}`);
        S.rows = rec.ok ? derive(rec.body.items) : [];
      } else {
        S.rows = [];
      }
      const total = S.devices.reduce((n, d) => n + d.count, 0);
      S.history.push({ t: serverNow(), total });
      S.history = S.history.filter((p) => p.t > serverNow() - 120000);
      setLink(true);
      trackCounters(S.info.counters);
      trackLogs();
    } catch {
      setLink(false);
    } finally {
      S.busy = false;
    }
    await pollAgents();
    trackAgents();
    renderAll();
  }

  // ───────────────────────────── renderers ─────────────────────────────
  function renderTopbar() {
    const chip = $('#chip-link');
    let kind = 'ok', label = 'ONLINE';
    if (!S.link) { kind = 'bad'; label = 'LINK LOST'; }
    else if (S.paused) { kind = 'warn'; label = 'PAUSED'; }
    else if (S.info && S.info.outage_remaining > 0) { kind = 'warn'; label = `SIMULATED OUTAGE ${Math.ceil(S.info.outage_remaining)}s`; }
    else if (!S.info) { kind = ''; label = 'CONNECTING'; }
    chip.className = `chip ${kind}`;
    chip.lastElementChild.textContent = label;
    $('#chip-uptime').textContent = S.info ? `up ${dur((S.info.uptime_s + (Date.now() - S.infoAt) / 1000) * 1000)}` : 'up —';
    $('#chip-demo').hidden = !(S.info && S.info.demo);

    const banner = $('#banner');
    if (!S.link) {
      banner.hidden = false; banner.className = 'banner';
      banner.textContent = '✗ LINK LOST: the server is not answering. This page keeps polling and will recover by itself. (Agents keep measuring meanwhile; see the device panel.)';
    } else if (S.info && S.info.outage_remaining > 0) {
      banner.hidden = false; banner.className = 'banner warn';
      banner.textContent = `⚠ SIMULATED OUTAGE: uploads get 503 for ${Math.ceil(S.info.outage_remaining)} more second(s). Watch the agent backlog grow, then drain.`;
    } else {
      banner.hidden = true;
    }
  }

  function tickClock() {
    const d = new Date(serverNow());
    $('#chip-clock').textContent = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())} UTC`;
  }

  const kpiState = {};
  function setKpi(id, label, value, sub, cls = '') {
    let card = $(`#kpi-${id}`);
    if (!card) {
      card = h('div', { class: 'kpi', id: `kpi-${id}` }, h('div', { class: 'k' }, label), h('div', { class: 'v' }), h('div', { class: 's' }));
      $('#kpis').append(card);
    }
    card.className = `kpi ${cls}`;
    const v = card.children[1];
    if (kpiState[id] !== value) {
      v.textContent = value;
      if (kpiState[id] !== undefined) { v.classList.remove('bump'); void v.offsetWidth; v.classList.add('bump'); }
      kpiState[id] = value;
    }
    card.children[2].textContent = sub;
    card.children[2].title = sub;
  }

  function renderKpis() {
    const total = S.devices.reduce((n, d) => n + d.count, 0);
    setKpi('records', 'records stored', total.toLocaleString(), `${S.devices.length} device(s)`);

    const hist = S.history;
    let rate = '—';
    if (hist.length > 1) {
      const first = hist.find((p) => p.t >= serverNow() - 60000) || hist[0];
      const last = hist[hist.length - 1];
      const span = last.t - first.t;
      if (span > 4000) rate = ((last.total - first.total) * 60000 / span).toFixed(0);
    }
    setKpi('rate', 'throughput', rate, 'records / min');

    const recent = S.rows.slice(-30);
    const avg = recent.length ? recent.reduce((n, r) => n + r.lag, 0) / recent.length : null;
    const max = S.rows.length ? Math.max(...S.rows.map((r) => r.lag)) : null;
    setKpi('lag', 'avg sync lag', dur(avg), `max ${dur(max)} in view`, avg > 5000 ? 'warn' : '');

    const dev = S.devices.find((d) => d.device_id === S.selected);
    const interval = cadenceMs();
    let ageText = '—', cls = '';
    if (dev && dev.last_received_at) {
      const age = serverNow() - Date.parse(dev.last_received_at);
      ageText = dur(age);
      cls = age > interval * 12 ? 'bad' : age > interval * 4 ? 'warn' : '';
    }
    setKpi('last', 'last record', ageText, dev ? `every ~${dur(interval)}` : 'no device', cls);

    setKpi('missing', 'missing sequences', dev ? String(dev.missing) : '—',
      dev ? `seq ${dev.first_sequence}–${dev.last_sequence} held` : 'none yet', dev && dev.missing > 0 ? 'warn' : '');

    const a = agentFor(S.selected);
    setKpi('backlog', 'device backlog', a ? String(a.data.outbox.pending) : '—',
      a ? (a.data.online === false ? 'server unreachable, retrying' : `${a.data.outbox.synced} synced · ${a.data.outbox.rejected} rejected`) : 'run agent with --status-port',
      a ? (a.data.outbox.pending > 3 ? 'warn' : '') : '');
  }

  function renderDevices() {
    const box = $('#devices');
    $('#devices-empty').hidden = S.devices.length > 0;
    box.replaceChildren(...S.devices.map((d) => {
      const age = d.last_received_at ? serverNow() - Date.parse(d.last_received_at) : Infinity;
      const state = age < 10000 ? 'ok' : age < 60000 ? 'warn' : 'bad';
      return h('button', { class: `dev${d.device_id === S.selected ? ' sel' : ''}`, onclick: () => selectDevice(d.device_id), title: 'show this device' },
        h('i', { class: `dot ${state}` }),
        h('span', { class: 'name' }, d.device_id),
        h('span', { class: 'n' }, d.count.toLocaleString()),
        h('span', { class: 'meta' }, `seq ${d.first_sequence}–${d.last_sequence} · ${Number.isFinite(age) ? dur(age) + ' ago' : '—'}`,
          d.missing > 0 ? h('span', { class: 'badge' }, `${d.missing} missing`) : null));
    }));
  }

  function renderAgents() {
    const box = $('#agents');
    const auto = !typedUrls().length;
    let urls = agentUrls();
    if (auto) urls = urls.filter((u) => S.agents[u] && S.agents[u].ok);
    if (!urls.length) {
      box.replaceChildren(h('div', { class: 'agent' },
        h('header', {}, h('span', { class: 'name' }, 'no agent found'), h('span', { class: 'state off' }, 'IDLE')),
        h('div', { class: 'line' }, 'looking on 127.0.0.1:8001–8004. start one with:'),
        h('div', { class: 'line' }, h('code', {}, 'morphx-agent --status-port 8001'))));
      return;
    }
    box.replaceChildren(...urls.map((url) => {
      const a = S.agents[url];
      if (!a || !a.ok) {
        return h('div', { class: 'agent' },
          h('header', {}, h('span', { class: 'name' }, url), h('span', { class: 'state off' }, 'NO STATUS')),
          h('div', { class: 'line' }, 'start the agent with:'),
          h('div', { class: 'line' }, h('code', {}, `morphx-agent --status-port ${url.split(':').pop().replace(/\D/g, '') || 8001}`)));
      }
      const d = a.data, o = d.outbox, total = Math.max(1, o.total ?? (o.pending + o.synced + o.rejected));
      const online = d.online === true, offline = d.online === false;
      return h('div', { class: 'agent' },
        h('header', {}, h('span', { class: 'name' }, d.device_id),
          h('span', { class: `state ${online ? 'ok' : offline ? 'bad' : 'off'}` }, online ? 'ONLINE' : offline ? `OFFLINE · retry ${Math.ceil(d.retry_in)}s` : 'STARTING')),
        h('div', { class: 'line' }, `measures every ${d.interval}s → ${d.server_url}`),
        h('div', { class: 'meter', title: 'synced / pending / rejected' },
          h('i', { class: 'synced', style: `width:${(o.synced / total) * 100}%` }),
          h('i', { class: 'pending', style: `width:${(o.pending / total) * 100}%` }),
          h('i', { class: 'rejected', style: `width:${(o.rejected / total) * 100}%` })),
        h('div', { class: 'counts' },
          h('span', { class: 'p' }, 'pending ', h('b', {}, o.pending)), h('span', { class: 's' }, 'synced ', h('b', {}, o.synced)),
          h('span', { class: 'x' }, 'rejected ', h('b', {}, o.rejected))),
        h('div', { class: 'line' }, `last sync ${shortTime(d.last_synced_at)} · up ${dur(d.uptime_s * 1000)}`),
        d.last_error ? h('div', { class: 'line err' }, d.last_error) : null);
    }));
  }

  function renderOutage() {
    const box = $('#outage');
    if (!S.info) return;
    if (!S.info.demo) {
      if (!box.dataset.built) {
        box.dataset.built = 'off';
        box.replaceChildren(h('p', { class: 'hint' }, 'restart the server with demo mode to simulate network outages from here:'), h('code', {}, 'morphx-server --demo'));
      }
      return;
    }
    if (box.dataset.built !== 'on') {
      box.dataset.built = 'on';
      const go = (s) => async () => { S.outageTotal = s; const r = await api(`/v1/demo/outage?seconds=${s}`, { method: 'POST' }); toast(r.ok ? `outage for ${s}s: uploads now get 503` : 'could not start outage', r.ok ? 'warn' : 'bad'); tick(); };
      box.replaceChildren(
        h('p', { class: 'hint' }, 'make the server refuse uploads (503). the agent keeps measuring and retries with backoff.'),
        h('div', { class: 'btnrow' },
          h('button', { onclick: go(10) }, '⚡ 10s'), h('button', { onclick: go(30) }, '⚡ 30s'), h('button', { onclick: go(60) }, '⚡ 60s'),
          h('button', { onclick: async () => { await api('/v1/demo/outage', { method: 'DELETE' }); toast('outage ended'); tick(); } }, '⏻ restore')),
        h('div', { id: 'outage-status', class: 'line dim' }));
    }
    const left = S.info.outage_remaining;
    const status = $('#outage-status');
    if (left > 0) {
      const total = Math.max(S.outageTotal, left);
      status.replaceChildren(h('div', { class: 'outage-bar' }, h('i', { style: `width:${(left / total) * 100}%` })), `outage: ${Math.ceil(left)}s remaining`);
    } else {
      status.textContent = 'server healthy';
    }
  }

  const INGEST = [
    ['created', '201', 'created', 'new record stored', '--accent'],
    ['duplicate', '200', 'duplicate', 'retry absorbed, nothing changed', '--info'],
    ['conflict', '409', 'conflict', 'sequence already used by another record', '--violet'],
    ['invalid', '422', 'invalid', 'failed validation, refused', '--warn'],
    ['unavailable', '503', 'outage', 'refused during a simulated outage', '--bad'],
  ];
  function renderIngest() {
    const box = $('#ingest');
    if (!S.info) return;
    const c = S.info.counters;
    const max = Math.max(1, ...Object.values(c));
    if (!box.dataset.built) {
      box.dataset.built = '1';
      box.replaceChildren(...INGEST.map(([key, code, label, desc, color]) => h('div', { class: 'ing', id: `ing-${key}` },
        h('span', { class: 'code', style: `color:var(${color})` }, code), h('div', { class: 'bar' }, h('i', { style: `background:var(${color});box-shadow:0 0 8px var(${color})` })),
        h('span', { class: 'n' }), h('small', {}, `${label} · ${desc}`))));
    }
    for (const [key] of INGEST) {
      const row = $(`#ing-${key}`);
      row.querySelector('i').style.width = `${(c[key] / max) * 100}%`;
      row.querySelector('.n').textContent = c[key].toLocaleString();
    }
  }

  function visibleRows() {
    const q = S.filter.trim().toLowerCase();
    let rows = [...S.rows];
    if (q) rows = rows.filter((r) => `${r.sequence} ${r.arrival} ${r.sample_id} ${r.event_id}`.toLowerCase().includes(q));
    rows.sort(S.order === 'seq' ? (a, b) => b.sequence - a.sequence : (a, b) => b.arrival - a.arrival);
    return rows;
  }

  function renderTable() {
    const rows = visibleRows();
    const body = $('#records tbody');
    const frag = document.createDocumentFragment();
    const showGaps = S.order === 'seq' && !S.filter.trim();
    const newest = S.rows.length ? Math.max(...S.rows.map((r) => r.arrival)) : 0;
    let prev = null;
    for (const r of rows) {
      if (showGaps && prev && prev.sequence - r.sequence > 1) {
        const lo = r.sequence + 1, hi = prev.sequence - 1;
        frag.append(h('tr', { class: 'gaprow' }, h('td', { colspan: 10 }, `⋯ missing ${lo === hi ? lo : `${lo}–${hi}`} (${hi - lo + 1}) · not received yet ⋯`)));
      }
      prev = r;
      const m = r.measurements;
      const fl = { wbc: flagOf('wbc', m.wbc.value), rbc: flagOf('rbc', m.rbc.value), hb: flagOf('hb', m.hb.value) };
      const cell = (metric) => h('td', { class: `r${fl[metric] ? ' abn' : ''}` }, m[metric].value.toFixed(metric === 'hb' ? 1 : 2), fl[metric] ? ` ${fl[metric]}` : '');
      const tr = h('tr', {
        class: `${r.arrival === S.selRow ? 'sel ' : ''}${S.lastArrivalRendered && r.arrival > S.lastArrivalRendered ? 'fresh' : ''}`,
        'data-arrival': r.arrival, onclick: () => { S.selRow = r.arrival; openDrawer(r); renderTable(); },
      },
        h('td', { class: 'arr' }, r.arrival), h('td', { class: 'seq' }, r.sequence),
        h('td', { class: 't' }, timeOf(r.measured_at)), h('td', { class: 't' }, timeOf(r.received_at)),
        h('td', { class: `lag ${r.lag > 3000 ? 'high' : r.lag > 800 ? 'mid' : ''}` }, dur(r.lag)),
        h('td', { class: 'sample' }, r.sample_id), cell('wbc'), cell('rbc'), cell('hb'),
        h('td', {}, r.late ? h('span', { class: 'tag late' }, 'LATE') : null, r.lag > 3000 ? h('span', { class: 'tag backlog' }, 'BACKLOG') : null));
      frag.append(tr);
    }
    if (!rows.length) frag.append(h('tr', {}, h('td', { colspan: 10, class: 'empty' }, S.rows.length ? 'no record matches the filter' : 'waiting for records…')));
    body.replaceChildren(frag);
    S.lastArrivalRendered = newest;
    $('#table-count').textContent = `${rows.length} shown · last ${S.rows.length} arrivals of ${S.selected ?? '—'}`;
    $('#btn-order b').textContent = S.order === 'seq' ? 'sequence' : 'arrival';
  }

  function renderReadouts() {
    const last = [...S.rows].sort((a, b) => a.sequence - b.sequence).pop();
    for (const metric of ['wbc', 'rbc', 'hb']) {
      const v = last ? last.measurements[metric].value : null;
      $(`#v-${metric}`).textContent = v == null ? '—' : v.toFixed(metric === 'hb' ? 1 : 2);
      const f = v == null ? '' : flagOf(metric, v);
      const el = $(`#f-${metric}`);
      el.textContent = f; el.className = `flag ${f}`;
    }
  }

  function renderAll() {
    renderTopbar();
    renderKpis();
    renderDevices();
    renderAgents();
    renderOutage();
    renderIngest();
    renderTable();
    renderReadouts();
    renderLog();
    drawCharts();
  }

  // ───────────────────────────── charts ─────────────────────────────
  function prepare(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(40, Math.round(rect.width)), hh = Math.max(40, Math.round(rect.height));
    if (canvas.width !== w * dpr || canvas.height !== hh * dpr) { canvas.width = w * dpr; canvas.height = hh * dpr; }
    const g = canvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, hh);
    g.font = '10px ui-monospace, Menlo, monospace';
    return { g, w, h: hh };
  }

  function placeholder(g, w, hh, text) {
    g.fillStyle = css('--dim'); g.textAlign = 'center'; g.fillText(text, w / 2, hh / 2); g.textAlign = 'left';
  }

  function tooltip(g, w, x, y, lines) {
    const pad = 6, lh = 13;
    const tw = Math.max(...lines.map((l) => g.measureText(l).width)) + pad * 2;
    const th = lines.length * lh + pad * 2 - 3;
    const bx = clamp(x + 10, 2, w - tw - 2), by = Math.max(2, y - th - 8);
    g.fillStyle = 'rgba(0,0,0,.88)'; g.strokeStyle = css('--accent'); g.lineWidth = 1;
    g.fillRect(bx, by, tw, th); g.strokeRect(bx + .5, by + .5, tw, th);
    g.fillStyle = css('--fg');
    lines.forEach((l, i) => g.fillText(l, bx + pad, by + pad + 8 + i * lh));
  }

  function drawSpark(metric) {
    const canvas = $(`#c-${metric}`);
    const { g, w, h: hh } = prepare(canvas);
    const pts = [...S.rows].sort((a, b) => a.sequence - b.sequence).slice(-80);
    canvas._pts = null;
    if (pts.length < 2) return placeholder(g, w, hh, 'waiting for data…');

    const L = 34, R = 8, T = 10, B = 18, pw = w - L - R, ph = hh - T - B;
    const vals = pts.map((p) => p.measurements[metric].value);
    const [lo, hi] = RANGE[metric];
    let min = Math.min(lo, ...vals), max = Math.max(hi, ...vals);
    const padY = (max - min) * 0.1; min -= padY; max += padY;
    const X = (i) => L + (i / (pts.length - 1)) * pw;
    const Y = (v) => T + (1 - (v - min) / (max - min)) * ph;
    const accent = css('--accent'), warn = css('--warn');

    g.fillStyle = css('--accent'); g.globalAlpha = 0.07; g.fillRect(L, Y(hi), pw, Y(lo) - Y(hi)); g.globalAlpha = 1;
    g.strokeStyle = css('--line'); g.fillStyle = css('--dim'); g.lineWidth = 1; g.setLineDash([2, 4]);
    for (let k = 0; k <= 3; k++) {
      const v = min + ((max - min) * k) / 3, y = Math.round(Y(v)) + 0.5;
      g.beginPath(); g.moveTo(L, y); g.lineTo(w - R, y); g.stroke();
      g.fillText(v.toFixed(metric === 'rbc' ? 1 : 0), 2, y + 3);
    }
    g.strokeStyle = accent; g.globalAlpha = 0.35;
    for (const v of [lo, hi]) { const y = Math.round(Y(v)) + 0.5; g.beginPath(); g.moveTo(L, y); g.lineTo(w - R, y); g.stroke(); }
    g.globalAlpha = 1; g.setLineDash([]);

    const grad = g.createLinearGradient(0, T, 0, hh - B);
    grad.addColorStop(0, withAlpha(accent, 0.33)); grad.addColorStop(1, withAlpha(accent, 0));
    g.beginPath(); g.moveTo(X(0), hh - B);
    vals.forEach((v, i) => g.lineTo(X(i), Y(v)));
    g.lineTo(X(vals.length - 1), hh - B); g.closePath(); g.fillStyle = grad; g.fill();

    g.shadowColor = accent; g.shadowBlur = 8; g.strokeStyle = accent; g.lineWidth = 1.6; g.lineJoin = 'round';
    g.beginPath(); vals.forEach((v, i) => (i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)))); g.stroke(); g.shadowBlur = 0;

    vals.forEach((v, i) => {
      const out = flagOf(metric, v);
      if (!out && i !== vals.length - 1) return;
      g.beginPath(); g.arc(X(i), Y(v), i === vals.length - 1 ? 3.5 : 3.2, 0, Math.PI * 2);
      if (out) { g.fillStyle = '#000'; g.fill(); g.strokeStyle = warn; g.lineWidth = 1.6; g.stroke(); }
      else { g.fillStyle = accent; g.fill(); }
    });

    g.fillStyle = css('--dim');
    g.fillText(`#${pts[0].sequence}`, L, hh - 4);
    g.textAlign = 'right'; g.fillText(`#${pts[pts.length - 1].sequence}`, w - R, hh - 4); g.textAlign = 'left';

    canvas._pts = { n: pts.length, L, pw };
    if (S.hover && S.hover.id === metric && S.hover.i < pts.length) {
      const i = S.hover.i, p = pts[i], v = vals[i], x = X(i), y = Y(v);
      g.strokeStyle = css('--fg'); g.globalAlpha = 0.4; g.beginPath(); g.moveTo(x + .5, T); g.lineTo(x + .5, hh - B); g.stroke(); g.globalAlpha = 1;
      tooltip(g, w, x, y, [`#${p.sequence}  ${v}${flagOf(metric, v) ? ' ' + flagOf(metric, v) : ''}`, UNIT[metric], shortTime(p.measured_at)]);
    }
  }

  function drawLag() {
    const canvas = $('#c-lag');
    const { g, w, h: hh } = prepare(canvas);
    const rows = S.rows.slice(-120);
    canvas._pts = null;
    if (!rows.length) return placeholder(g, w, hh, 'waiting for data…');

    const L = 38, R = 8, T = 8, B = 16, pw = w - L - R, ph = hh - T - B;
    const LO = 1, HI = Math.log10(120000); // 10 ms … 120 s on a log scale
    const Y = (ms) => T + (1 - (clamp(Math.log10(Math.max(ms, 10)), LO, HI) - LO) / (HI - LO)) * ph;
    g.fillStyle = css('--dim'); g.strokeStyle = css('--line'); g.lineWidth = 1; g.setLineDash([2, 4]);
    for (const [ms, label] of [[100, '100ms'], [1000, '1s'], [10000, '10s'], [60000, '60s']]) {
      const y = Math.round(Y(ms)) + 0.5;
      g.beginPath(); g.moveTo(L, y); g.lineTo(w - R, y); g.stroke(); g.fillText(label, 2, y + 3);
    }
    g.setLineDash([]);

    const slot = pw / rows.length, bw = Math.max(1.5, slot - 1);
    const colours = [css('--bar1'), css('--bar2'), css('--bar3')];
    rows.forEach((r, i) => {
      const c = colours[r.lag < 1000 ? 0 : r.lag < 5000 ? 1 : 2];
      const y = Y(r.lag);
      g.fillStyle = c; g.shadowColor = c; g.shadowBlur = r.lag >= 1000 ? 6 : 0;
      g.fillRect(L + i * slot, y, bw, T + ph - y);
    });
    g.shadowBlur = 0;
    g.fillStyle = css('--dim'); g.fillText('older ←  arrival order  → newer', L, hh - 3);
    canvas._pts = { n: rows.length, L, pw };
    if (S.hover && S.hover.id === 'lag' && S.hover.i < rows.length) {
      const i = S.hover.i, r = rows[i], x = L + i * slot + bw / 2;
      tooltip(g, w, x, Y(r.lag), [`arrival #${r.arrival}  seq #${r.sequence}`, `lag ${dur(r.lag)}${r.late ? '  LATE' : ''}`]);
    }
  }

  function drawCharts() { ['wbc', 'rbc', 'hb'].forEach(drawSpark); drawLag(); }

  function wireHover(id, redraw) {
    const canvas = $(`#c-${id}`);
    canvas.addEventListener('mousemove', (e) => {
      const p = canvas._pts;
      if (!p) return;
      const x = e.clientX - canvas.getBoundingClientRect().left;
      const frac = clamp((x - p.L) / p.pw, 0, 1);
      const i = id === 'lag' ? clamp(Math.floor(frac * p.n), 0, p.n - 1) : Math.round(frac * (p.n - 1));
      S.hover = { id, i };
      redraw();
    });
    canvas.addEventListener('mouseleave', () => { S.hover = null; redraw(); });
  }

  // ───────────────────────────── detail drawer ─────────────────────────────
  function jsonView(obj) {
    const text = JSON.stringify(obj, null, 2);
    const pre = h('pre', { class: 'json' });
    const re = /("(?:\\.|[^\\"])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) pre.append(text.slice(last, m.index));
      const kind = m[1] ? (m[2] ? 'k' : 's') : m[3] ? 'b' : 'n';
      pre.append(h('span', { class: kind }, m[2] ? m[1] : m[0]));
      if (m[2]) pre.append(m[2]);
      last = re.lastIndex;
    }
    pre.append(text.slice(last));
    return pre;
  }

  const wireOf = (row) => { const { arrival, lag, late, ...rest } = row; return rest; };
  let drawerRow = null;

  function openDrawer(row) {
    drawerRow = row;
    $('#drawer').hidden = false;
    $('#drawer-title').textContent = `record #${row.sequence} · ${row.device_id}`;
    const m = row.measurements;
    const kv = (k, v, cls) => [h('dt', {}, k), h('dd', { class: cls }, v)];
    $('#drawer-body').replaceChildren(
      h('dl', { class: 'kv' },
        ...kv('arrival order', `#${row.arrival}  (the order the server received it)`),
        ...kv('acquisition seq', `#${row.sequence}  (the order the device measured it)`),
        ...kv('sync lag', `${dur(row.lag)}  (received − measured)`, row.lag > 3000 ? 'badc' : 'good'),
        ...kv('arrived', row.late ? 'LATE: after a record with a higher sequence' : 'in order', row.late ? 'badc' : 'good'),
        ...kv('values', ['wbc', 'rbc', 'hb'].map((k) => `${k} ${m[k].value} ${m[k].unit}${flagOf(k, m[k].value) ? ' [' + flagOf(k, m[k].value) + ']' : ''}`).join('  ·  ')),
        ...kv('event_id', row.event_id)),
      h('h4', { class: 'sub' }, 'as stored on the server'),
      jsonView(wireOf(row)));
  }

  function closeDrawer() { $('#drawer').hidden = true; drawerRow = null; S.selRow = null; renderTable(); }

  // ───────────────────────────── probes & toasts ─────────────────────────────
  function toast(text, kind = '') {
    const el = h('div', { class: `toast ${kind}` }, text);
    $('#toasts').append(el);
    setTimeout(() => el.remove(), 2600);
  }

  async function probe(kind, baseRow) {
    const base = baseRow || [...S.rows].sort((a, b) => b.sequence - a.sequence)[0];
    const out = $('#probe-out');
    if (!base) { toast('no record yet to probe with', 'warn'); return; }
    let body = wireOf(base), what;
    if (kind === 'dup') what = `replay #${base.sequence} unchanged`;
    if (kind === 'conflict') { body = { ...body, event_id: crypto.randomUUID(), sample_id: 'SMP-PROBE' }; what = `new event_id, but device+sequence #${base.sequence} is taken`; }
    if (kind === 'invalid') { body = structuredClone(body); body.event_id = crypto.randomUUID(); body.measurements.hb.unit = 'g/L'; what = 'haemoglobin unit "g/L" instead of "g/dL"'; }
    let res;
    try {
      res = await api('/v1/records', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    } catch {
      out.replaceChildren(h('span', { class: 'bad' }, 'request failed: server unreachable'));
      return;
    }
    const cls = res.status === 201 ? 'ok' : res.status === 200 ? 'ok' : res.status >= 500 ? 'bad' : 'warn';
    const meaning = { 200: 'duplicate: stored once, nothing changed', 201: 'created', 409: 'conflict: refused, nothing stored', 422: 'invalid: refused, nothing stored', 503: 'server unavailable: agent would retry' }[res.status] || '';
    out.replaceChildren(
      h('span', { class: 'cmd' }, `$ POST /v1/records   # ${what}\n`),
      h('span', { class: cls }, `← ${res.status} ${meaning}  (${Math.round(res.ms)} ms)\n`),
      JSON.stringify(res.body));
    toast(`${res.status} ${meaning}`, cls === 'ok' ? '' : cls);
    tick();
  }

  // ───────────────────────────── controls ─────────────────────────────
  function selectDevice(id) {
    if (id === S.selected) return;
    S.selected = id; S.rows = []; S.selRow = null; S.lastArrivalRendered = 0;
    closeDrawerQuiet();
    renderAll();
    tick();
  }
  function closeDrawerQuiet() { $('#drawer').hidden = true; drawerRow = null; }

  function applyTheme() {
    document.documentElement.dataset.theme = S.theme;
    document.body.classList.toggle('fx', S.fx);
    store.set('theme', S.theme); store.set('fx', S.fx ? 'on' : 'off');
    if (S.rows.length) drawCharts();
  }

  function setPaused(p) {
    S.paused = p;
    $('#btn-pause').textContent = p ? '▶ resume' : '❚❚ pause';
    addLog('info', p ? 'live updates paused' : 'live updates resumed');
    renderTopbar(); renderLog();
    if (!p) tick();
  }

  function moveSelection(dir) {
    const rows = visibleRows();
    if (!rows.length) return;
    let i = rows.findIndex((r) => r.arrival === S.selRow);
    i = clamp(i < 0 ? 0 : i + dir, 0, rows.length - 1);
    S.selRow = rows[i].arrival;
    renderTable();
    if (!$('#drawer').hidden) openDrawer(rows[i]);
    $('#records tbody tr.sel')?.scrollIntoView({ block: 'nearest' });
  }

  function wire() {
    $('#btn-pause').onclick = () => setPaused(!S.paused);
    $('#btn-theme').onclick = () => { S.theme = THEMES[(THEMES.indexOf(S.theme) + 1) % THEMES.length]; applyTheme(); toast(`theme: ${THEME_NAME[S.theme]}`); };
    $('#btn-fx').onclick = () => { S.fx = !S.fx; applyTheme(); };
    $('#btn-help').onclick = () => { $('#help').hidden = !$('#help').hidden; };
    $('#help').onclick = (e) => { if (e.target.id === 'help') $('#help').hidden = true; };
    $('#btn-order').onclick = () => { S.order = S.order === 'seq' ? 'arrival' : 'seq'; renderTable(); };
    $('#filter').addEventListener('input', (e) => { S.filter = e.target.value; renderTable(); });
    $('#drawer-close').onclick = closeDrawer;
    $('#drawer-copy').onclick = async () => {
      try { await navigator.clipboard.writeText(JSON.stringify(wireOf(drawerRow), null, 2)); toast('json copied'); } catch { toast('copy failed', 'warn'); }
    };
    $('#drawer-replay').onclick = () => drawerRow && probe('dup', drawerRow);
    document.querySelectorAll('[data-probe]').forEach((b) => { b.onclick = () => probe(b.dataset.probe); });
    $('#agent-urls').value = S.agentUrls;
    $('#agent-form').onsubmit = (e) => { e.preventDefault(); S.agentUrls = $('#agent-urls').value.trim(); store.set('agents', S.agentUrls); S.prevOnline = {}; for (const k of Object.keys(agentNext)) delete agentNext[k]; tick(); };

    wireHover('wbc', () => drawSpark('wbc')); wireHover('rbc', () => drawSpark('rbc')); wireHover('hb', () => drawSpark('hb'));
    wireHover('lag', drawLag);
    if ('ResizeObserver' in window) new ResizeObserver(() => drawCharts()).observe($('.main'));

    document.addEventListener('keydown', (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const typing = e.target instanceof Element && e.target.matches('input, textarea');
      if (e.key === 'Escape') { if (typing) e.target.blur(); if (!$('#help').hidden) $('#help').hidden = true; else if (!$('#drawer').hidden) closeDrawer(); return; }
      if (typing) return;
      const devIdx = S.devices.findIndex((d) => d.device_id === S.selected);
      switch (e.key) {
        case 'j': moveSelection(1); break;
        case 'k': moveSelection(-1); break;
        case 'Enter': { const r = S.rows.find((x) => x.arrival === S.selRow); if (r) openDrawer(r); break; }
        case 'o': $('#btn-order').click(); break;
        case 'p': $('#btn-pause').click(); break;
        case 't': $('#btn-theme').click(); break;
        case 'c': $('#btn-fx').click(); break;
        case '?': $('#btn-help').click(); break;
        case '/': e.preventDefault(); $('#filter').focus(); break;
        case ']': if (S.devices.length) selectDevice(S.devices[(devIdx + 1) % S.devices.length].device_id); break;
        case '[': if (S.devices.length) selectDevice(S.devices[(devIdx - 1 + S.devices.length) % S.devices.length].device_id); break;
        default: return;
      }
    });
  }

  // ───────────────────────────── boot ─────────────────────────────
  function typeBrand() {
    const el = $('#typed'), text = 'tail -f records --follow';
    el.textContent = '';
    let i = 0;
    const id = setInterval(() => { el.textContent = text.slice(0, ++i); if (i >= text.length) clearInterval(id); }, 38);
  }

  function boot(done) {
    let seen = false;
    try { seen = sessionStorage.getItem('morphx.booted') === '1'; } catch { /* ignore */ }
    if (seen || matchMedia('(prefers-reduced-motion: reduce)').matches) { done(); return; }
    const lines = [
      'MORPHX OPS CONSOLE  v1.0',
      '',
      '[ ok ] mounting /v1/devices ........ done',
      '[ ok ] opening record stream ........ done',
      '[ ok ] calibrating clocks (utc) ..... done',
      '[ ok ] attaching device agents ...... done',
      '',
      'ready.',
    ];
    const el = $('#boot'), pre = $('#boot-text');
    el.hidden = false;
    let n = 0, finished = false;
    const finish = () => {
      if (finished) return; finished = true; clearInterval(timer);
      el.hidden = true;
      try { sessionStorage.setItem('morphx.booted', '1'); } catch { /* ignore */ }
      document.removeEventListener('keydown', finish);
      done();
    };
    const timer = setInterval(() => { pre.textContent = lines.slice(0, ++n).join('\n'); if (n >= lines.length) setTimeout(finish, 350); }, 130);
    el.addEventListener('click', finish); document.addEventListener('keydown', finish);
  }

  applyTheme();
  wire();
  boot(() => { typeBrand(); tick(); });
  setInterval(tick, 1000);
  setInterval(tickClock, 250);
  setInterval(() => { if (S.info && !S.paused) renderTopbar(); }, 1000);
})();
