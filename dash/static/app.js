'use strict';

/* ------------------------------------------------------------------ helpers */
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function pref(key, fallback) { try { return localStorage.getItem('dash.' + key) ?? fallback; } catch (e) { return fallback; } }
function savePref(key, val) { try { localStorage.setItem('dash.' + key, val); } catch (e) { /* private mode etc. */ } }

let STATE = null;
let THR = null;
let chartType = pref('chart', 'bars');
let range = Number(pref('range', '24')) === 168 ? 168 : 24;
{ const q = new URLSearchParams(location.search); if (q.get('chart') === 'line' || q.get('chart') === 'bars') chartType = q.get('chart'); if (q.get('range') === '168' || q.get('range') === '24') range = Number(q.get('range')); }
let lastOk = null;
let fetchFailed = false;

function fmtBin(b) {
  b = Number(b) || 0;
  if (b >= 1024 ** 4) return (b / 1024 ** 4).toFixed(1) + ' TiB';
  if (b >= 1024 ** 3) return (b / 1024 ** 3).toFixed(1) + ' GiB';
  if (b >= 1024 ** 2) return Math.round(b / 1024 ** 2) + ' MiB';
  return Math.round(b / 1024) + ' KiB';
}
function fmtDec(b) {
  b = Number(b) || 0;
  if (b >= 1e12) return (b / 1e12).toFixed(1) + ' TB';
  if (b >= 1e9) return (b / 1e9).toFixed(1) + ' GB';
  if (b >= 1e6) return (b / 1e6).toFixed(0) + ' MB';
  return (b / 1e3).toFixed(0) + ' KB';
}
function fmtRate(bps) {
  bps = Number(bps) || 0;
  if (bps >= 1e6) return [(bps / 1e6).toFixed(1), 'MB/s'];
  if (bps >= 1e3) return [Math.round(bps / 1e3), 'kB/s'];
  return [Math.round(bps), 'B/s'];
}
function fmtUptime(s) {
  s = Number(s) || 0;
  const d = Math.floor(s / 86400);
  if (d >= 60) return `${Math.floor(d / 30)} mo ${d % 30} d`;
  if (d >= 1) return `${(s / 86400).toFixed(1)} days`;
  const h = Math.floor(s / 3600);
  return h ? `${h} h ${Math.floor((s % 3600) / 60)} m` : `${Math.floor(s / 60)} m`;
}
function fmtMin(m) {
  m = Number(m) || 0;
  const h = Math.floor(m / 60);
  return h ? `${h}h ${m % 60}m` : `${m}m`;
}
const fmtTime = (d) => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
const n0 = (v) => (v == null ? '–' : Number(v).toLocaleString());

/* ------------------------------------------------------------------ tile scaffolding */
function statusOf(sec) {
  if (!sec || !sec.configured) return '<span class="status"><span class="dot grey"></span>Not configured</span>';
  if (sec.ok === false) return '<span class="status"><span class="dot red"></span>Unreachable</span>';
  if (sec.ok == null) return '<span class="status"><span class="dot grey"></span>Loading</span>';
  return '<span class="status"><span class="dot"></span>Online</span>';
}

function body(sec, render) {
  if (!sec || !sec.configured) return '<div class="note">Not configured. Add its settings in the compose file to turn this tile on.</div>';
  if (!sec.data) return sec.ok === false ? `<div class="err">${esc(sec.error)}</div>` : '<div class="note">Loading…</div>';
  let html = render(sec.data);
  if (sec.ok === false) html += `<div class="err">Showing last good data. ${esc(sec.error)}</div>`;
  return html;
}


/* ------------------------------------------------------------------ TrueNAS */
function renderTrueNAS() {
  const tn = STATE.truenas, gl = STATE.glances, d = tn.data || {};
  const pools = (d.pools || []).filter((p) => p.total).sort((a, b) => b.total - a.total);
  const want = STATE.primary_pool && pools.findIndex((p) => p.name === STATE.primary_pool);
  if (want > 0) pools.unshift(pools.splice(want, 1)[0]);
  const main = pools[0];
  const sub = main ? `Storage · ${esc(main.name)}` : 'Storage';
  const h = `<div class="card-head"><a class="tile-title" href="${esc(STATE.links.truenas)}" target="_blank" rel="noopener">`
    + `<span class="logo logo-fill"><img src="icons/truenas.png" alt=""></span><div><h2>TrueNAS</h2><div class="sub">${sub}</div></div></a>${statusOf(tn)}</div>`;

  const inner = body(tn, () => {
    let html = '';
    if (main) {
      const pct = main.pct ?? 0;
      const color = pct >= 90 ? 'var(--red)' : pct >= 80 ? 'var(--amber)' : 'var(--green)';
      html += `<div><div class="big">${Math.round(pct)}%</div>`
        + `<div class="big-sub">${esc(main.name)} · ${fmtBin(main.used)} of ${fmtBin(main.total)} used${main.healthy ? '' : ' · <span style="color:var(--red-t)">unhealthy</span>'}</div>`
        + `<div class="bar"><div style="width:${Math.min(100, pct)}%;background:${color}"></div></div>`;
      if (pools.length > 1) {
        html += '<div class="pools-more">' + pools.slice(1).map((p) =>
          `<div><span>${esc(p.name)}${p.healthy ? '' : ' (unhealthy)'}</span><span>${Math.round(p.pct ?? 0)}% · ${fmtBin(p.used)} of ${fmtBin(p.total)}</span></div>`).join('') + '</div>';
      }
      html += '</div>';
    }
    const alerts = (d.alerts || []).filter((a) => a.level !== 'INFO' && a.level !== 'NOTICE');
    const updates = STATE.app_updates || [];
    const g = gl && gl.data;
    html += '<div class="sys"><div class="sys-title">System</div>';
    html += `<button type="button" class="sys-row ${alerts.length ? 'amber' : ''}" data-open-attention style="width:100%;text-align:left;cursor:pointer;font:inherit">`
      + `<div style="min-width:0"><div class="t">Alerts</div><div class="d">${alerts.length ? esc(alerts[0].text) : 'No active alerts'}</div></div>`
      + `<span class="badge ${alerts.length ? 'amber' : 'green'}">${alerts.length}</span></button>`;
    html += `<a class="sys-row ${updates.length ? 'amber' : ''}" href="#apps">`
      + `<div style="min-width:0"><div class="t">App updates</div><div class="d">${updates.length ? esc(updates.join(', ')) : 'All apps up to date'}</div></div>`
      + `<span class="badge ${updates.length ? 'amber' : 'green'}">${updates.length}</span></a>`;
    const model = (d.model || '').replace(/\s+\d+-Core Processor$/i, '');
    const cpu = g ? `${Math.round(g.cpu_pct)}%` : '–';
    html += `<div class="sys-row ${g && g.cpu_pct >= 85 ? 'amber' : ''}"><div style="min-width:0"><div class="t">CPU</div>`
      + `<div class="d">${esc(model || 'Processor')}${d.load1 != null ? ' · load ' + Number(d.load1).toFixed(2) : ''}</div></div><span class="v">${cpu}</span></div>`;
    const memUsed = g ? g.mem_used : null, memTotal = g ? g.mem_total : d.physmem;
    html += `<div class="sys-row ${g && g.mem_pct >= 90 ? 'amber' : ''}"><div><div class="t">Memory</div>`
      + `<div class="d">${memUsed != null ? fmtBin(memUsed) + ' of ' : ''}${fmtBin(memTotal)}${memUsed != null ? ' used' : ''}</div></div>`
      + `<span class="v">${g ? Math.round(g.mem_pct) + '%' : '–'}</span></div>`;
    html += `<div class="sys-row"><div><div class="t">Uptime</div><div class="d">Since last reboot</div></div><span class="v">${fmtUptime(d.uptime_s)}</span></div>`;
    html += '</div>';
    return html;
  });
  $('#truenas').innerHTML = h + inner;
}

/* ------------------------------------------------------------------ UniFi */
function renderUnifi() {
  const u = STATE.unifi, d = u.data || {};
  $('#unifi-sub').textContent = d.name || 'UniFi';
  const links = STATE.links || {};
  [['#unifi-link', links.unifi], ['#unifi-clients-link', links.unifi_clients]].forEach(([sel, href]) => {
    const el = $(sel);
    if (!el) return;
    if (href) el.setAttribute('href', href); else el.removeAttribute('href');
  });
  const linkStat = (href, inner) => href ? `<a class="stat stat-link" href="${esc(href)}" target="_blank" rel="noopener">${inner}</a>` : `<div class="stat">${inner}</div>`;
  if (!u.configured) {
    $('#unifi-strip').innerHTML = '<div class="note">Not configured.</div>';
  } else if (!u.data) {
    $('#unifi-strip').innerHTML = u.ok === false ? `<div class="err">${esc(u.error)}</div>` : '<div class="note">Loading…</div>';
  } else {
    const wanUp = d.wan_status === 'ok';
    const peak = THR && THR.peak_5min_mbps != null ? Math.round(THR.peak_5min_mbps) : '–';
    $('#unifi-strip').innerHTML =
      `<div class="stat"><div class="stat-l">Now · every ${STATE.poll_seconds} s</div><div class="stat-v"><span style="color:var(--down-t)">↓ ${d.down_mbps.toFixed(1)}</span> <span style="color:var(--up-t)">↑ ${d.up_mbps.toFixed(1)}</span> <small>Mbps</small></div></div>`
      + `<div class="stat"><div class="stat-l">Peak (5-min avg)</div><div class="stat-v">${peak} <small>Mbps</small></div></div>`
      + linkStat(links.unifi, `<div class="stat-l">WAN</div><div class="stat-v" style="display:flex;align-items:center;gap:8px"><span class="dot ${wanUp ? '' : 'red'}"></span>${wanUp ? 'Up' : 'Down'}${d.wan_uptime_s ? ' · ' + fmtUptime(d.wan_uptime_s) : ''}</div>`)
      + linkStat(links.unifi_clients, `<div class="stat-l">Clients</div><div class="stat-v">${d.lan_users + d.wlan_users} <small>${d.lan_users} wired · ${d.wlan_users} Wi-Fi</small></div>`)
      + (u.ok === false ? `<div class="err" style="grid-column:1/-1">Showing last good data. ${esc(u.error)}</div>` : '');
  }
  const top = STATE.unifi_top;
  const clients = (top.data && top.data.clients) || [];
  const max = clients.length ? clients[0].bytes || 1 : 1;
  $('#top-clients').innerHTML = clients.length
    ? clients.map((c) => `<li><div class="tc-row"><b>${esc(c.name)}</b><span>${fmtDec(c.bytes)}</span></div><div class="tc-bar"><div style="width:${(c.bytes / max * 100).toFixed(0)}%"></div></div></li>`).join('')
    : `<li style="font-size:14px;opacity:.85">${top.ok === false ? esc(top.error) : 'Loading…'}</li>`;
}

function niceMax(v) {
  if (v <= 1) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  return [1, 2, 4, 6, 8, 10].map((m) => m * p).find((x) => x >= v);   // divisible by 4 = round gridline labels
}

function renderChart() {
  document.querySelectorAll('[data-chart]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.chart === chartType)));
  document.querySelectorAll('[data-range]').forEach((b) => b.setAttribute('aria-pressed', String(Number(b.dataset.range) === range)));
  const chart = $('#chart'), xaxis = $('#chart-x');
  if (!THR) { chart.innerHTML = '<div class="chart-empty">Loading…</div>'; xaxis.innerHTML = ''; return; }
  const bucket = THR.bucket_seconds, n = range === 24 ? 24 : 28;
  $('#chart-caption').textContent = `Mbps, ${bucket === 3600 ? 'hourly' : '6-hour'} averages`;
  const end = Math.floor(Date.now() / 1000 / bucket) * bucket;
  const byT = new Map(THR.points.map((p) => [p.t, p]));
  const slots = Array.from({ length: n }, (_, i) => { const t = end - (n - 1 - i) * bucket; return { t, p: byT.get(t) }; });
  const vals = slots.filter((s) => s.p);
  if (!vals.length) {
    chart.innerHTML = `<div class="chart-empty">Collecting data. The graph fills in as samples arrive every ${STATE ? STATE.poll_seconds : 30} s.</div>`;
    xaxis.innerHTML = '';
    return;
  }
  const max = niceMax(Math.max(...vals.map((s) => Math.max(s.p.down, s.p.up))) * 1.1);
  let html = [0, 0.25, 0.5, 0.75, 1].map((f) => `<div class="gridline" style="bottom:${f * 100}%"><span>${max * f >= 10 ? Math.round(max * f) : (max * f).toFixed(1)}</span></div>`).join('');
  if (chartType === 'bars') {
    html += '<div class="bars">' + slots.map((s) => {
      if (!s.p) return '<div></div>';
      return `<div><i class="d" style="height:${(s.p.down / max * 100).toFixed(1)}%"></i><i class="u" style="height:${Math.max(1, s.p.up / max * 100).toFixed(1)}%"></i></div>`;
    }).join('') + '</div>';
  } else {
    const X = (i) => (n === 1 ? 500 : (i / (n - 1)) * 1000), Y = (v) => 250 - (v / max) * 250;
    const path = (key) => { let d = '', prev = false; slots.forEach((s, i) => { if (s.p) { d += (prev ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(s.p[key]).toFixed(1) + ' '; prev = true; } else prev = false; }); return d; };
    const first = slots.findIndex((s) => s.p), last = slots.length - 1 - [...slots].reverse().findIndex((s) => s.p);
    const area = path('down') + `L${X(last).toFixed(1)} 250 L${X(first).toFixed(1)} 250 Z`;
    html += `<svg viewBox="0 0 1000 250" preserveAspectRatio="none" aria-hidden="true">`
      + `<path d="${area}" fill="var(--down)" fill-opacity="0.18" stroke="none"/>`
      + `<path d="${path('down')}" fill="none" stroke="var(--down)" stroke-width="2.2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`
      + `<path d="${path('up')}" fill="none" stroke="var(--up)" stroke-width="2.2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>`;
  }
  if (chartType === 'line') {
    // dots for the hovered point, positioned in % so they line up with the stretched SVG
    html += '<div class="chart-guide" hidden><i class="d"></i><i class="u"></i></div>';
  }
  html = '<div class="chart-band" hidden></div>' + html + '<div class="chart-hits"></div>';
  chart.innerHTML = html;
  CHART = { slots, max, bucket, n };
  if (hoverIdx != null && !TIP.hidden) showTip(hoverIdx); else hideTip();
  xaxis.innerHTML = slots.map((s, i) => {
    const d = new Date(s.t * 1000);
    let label = '';
    if (range === 24) { if (i % 3 === 1) label = d.toLocaleTimeString([], { hour: 'numeric' }); }
    else if (d.getHours() < 6) label = d.toLocaleDateString([], { weekday: 'short' });
    return `<div>${label}</div>`;
  }).join('');
}

/* ------------------------------------------------------------------ chart hover */
let CHART = null, hoverIdx = null;

function fmtGB(gb) {
  if (gb == null) return '–';
  return gb >= 100 ? gb.toFixed(0) + ' GB' : gb >= 1 ? gb.toFixed(1) + ' GB' : Math.round(gb * 1000) + ' MB';
}

// The tooltip is one element on <body>, created once and only moved/refilled, never rebuilt with the chart.
const TIP = document.createElement('div');
TIP.className = 'chart-tip';
TIP.hidden = true;
document.body.appendChild(TIP);

function tipHtml(s, bucket) {
  const p = s.p;
  const start = new Date(s.t * 1000), end = new Date((s.t + bucket) * 1000);
  const hr = (d) => d.toLocaleTimeString([], { hour: 'numeric' }).replace(/\s/g, ' ');
  const day = start.toLocaleDateString([], { weekday: 'short' });
  const partial = Date.now() / 1000 < s.t + bucket;
  const head = `${day} ${hr(start)} - ${hr(end)}${partial ? ', so far' : ''}`;
  if (!p) return `<div class="tip-h">${esc(head)}</div><div class="tip-f">No data for this ${bucket === 3600 ? 'hour' : 'period'}</div>`;
  const f1 = (v) => (Number(v) || 0).toFixed(1);
  const sampled = p.coverage != null && p.coverage < 0.9 && !partial ? `, ${Math.round(p.coverage * 100)}% sampled` : '';
  return `<div class="tip-h">${esc(head)}</div>`
    + '<div class="tip-grid"><span></span><span class="tip-k">avg</span><span class="tip-k">peak</span>'
    + `<span class="tip-dn">Down</span><b>${f1(p.down)}</b><b>${f1(p.peak_down ?? p.down)}</b>`
    + `<span class="tip-up">Up</span><b>${f1(p.up)}</b><b>${f1(p.peak_up ?? p.up)}</b></div>`
    + `<div class="tip-f">Mbps. About ${fmtGB(p.gb_down)} down, ${fmtGB(p.gb_up)} up${sampled}</div>`;
}

function showTip(i) {
  try {
    const chart = $('#chart');
    if (!CHART || i == null || i < 0 || i >= CHART.slots.length) return;
    hoverIdx = i;
    const { slots, max, bucket, n } = CHART, s = slots[i], p = s.p;
    TIP.innerHTML = tipHtml(s, bucket);
    TIP.hidden = false;
    // highlight: a band behind the hovered column (bars) or a guide line with dots (line)
    const band = chart.querySelector('.chart-band'), guide = chart.querySelector('.chart-guide');
    if (band) { band.hidden = false; band.style.left = (i / n * 100) + '%'; band.style.width = (100 / n) + '%'; }
    if (guide) {
      guide.hidden = !p;
      guide.style.left = (n === 1 ? 50 : (i / (n - 1)) * 100) + '%';
      if (p && max > 0) {
        guide.querySelector('.d').style.bottom = Math.min(100, p.down / max * 100) + '%';
        guide.querySelector('.u').style.bottom = Math.min(100, p.up / max * 100) + '%';
      }
    }
    // place it over the chart, kept inside the chart's width
    const r = chart.getBoundingClientRect(), tw = TIP.offsetWidth;
    const cx = chartType === 'line' ? (n === 1 ? r.width / 2 : (i / (n - 1)) * r.width) : ((i + 0.5) / n) * r.width;
    const left = Math.round(r.left + Math.max(0, Math.min(r.width - tw, cx - tw / 2)));
    TIP.style.left = left + 'px';
    TIP.style.top = Math.round(r.top + 6) + 'px';
  } catch (e) {
    console.error('[dash] chart hover failed:', e);
  }
}

function hideTip() {
  hoverIdx = null;
  TIP.hidden = true;
  const chart = $('#chart');
  chart.querySelectorAll('.chart-band, .chart-guide').forEach((el) => { el.hidden = true; });
}

function slotAt(e) {
  // bars: which column the pointer is over; line: the nearest data point
  if (!CHART) return null;
  const r = $('#chart').getBoundingClientRect(), f = Math.max(0, Math.min(0.9999, (e.clientX - r.left) / r.width)), n = CHART.n;
  return chartType === 'line' ? Math.round(f * (n - 1)) : Math.floor(f * n);
}
$('#chart').addEventListener('pointermove', (e) => {
  if (!e.target.closest('.chart-hits')) return;
  const i = slotAt(e);
  if (i != null && i !== hoverIdx) showTip(i);
});
$('#chart').addEventListener('pointerdown', (e) => {          // tap on touch screens
  if (e.target.closest('.chart-hits')) showTip(slotAt(e));
});
$('#chart').addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') hideTip(); });
document.addEventListener('pointerdown', (e) => { if (!e.target.closest('#chart')) hideTip(); });
window.addEventListener('scroll', () => { if (!TIP.hidden) hideTip(); }, { passive: true });

/* ------------------------------------------------------------------ configurable tiles (services.toml) */
function ago(iso) {
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 90) return 'just now';
  if (sec < 3600) return Math.round(sec / 60) + ' min ago';
  if (sec < 86400) return Math.round(sec / 3600) + ' h ago';
  return Math.round(sec / 86400) + ' d ago';
}

const DOT = { green: '', grey: 'grey', amber: 'amber', red: 'red' };
const LEVEL = { ok: '', warning: 'amber', error: 'red' };

function tileHtml(t) {
  const d = t.data || {};
  let sub = esc(t.sub);
  if (t.configured && t.data && d.sub) sub = (d.sub.dot ? `<span class="dot ${DOT[d.sub.dot] ?? ''}"></span>` : '') + esc(d.sub.text);
  const link = d.link || t.link;
  const inner = `<span class="logo ${esc(t.logo || '')}"><img src="${esc(t.icon)}" alt="" onerror="this.onerror=null;this.src='icons/generic.svg'"></span>`
    + `<div><h3>${esc(t.name)}</h3><div class="sub">${sub}</div></div>`;
  const title = link ? `<a class="tile-title" href="${esc(link)}" target="_blank" rel="noopener">${inner}</a>` : `<div class="tile-title">${inner}</div>`;
  const right = t.configured && t.data && d.right != null ? (d.right ? `<span class="status">${esc(d.right)}</span>` : '') : statusOf(t);
  let html = `<div class="card-head">${title}${right}</div>`;

  if (!t.configured) return html + `<div class="note">Not configured: needs ${esc(t.missing.join(', '))} in services.toml.</div>`;
  if (!t.data) return html + (t.ok === false ? `<div class="err">${esc(t.error)}</div>` : '<div class="note">Loading…</div>');

  const stats = d.stats || [];
  if (stats.length) {
    html += `<div class="stats ${stats.length === 3 ? 'c3' : 'c2'}">` + stats.map((x) =>
      `<div class="stat"><div class="stat-v" title="${esc(x.v)}${x.u ? ' ' + esc(x.u) : ''}">${esc(x.v)}${x.u ? ` <small>${esc(x.u)}</small>` : ''}</div><div class="stat-l">${esc(x.l)}</div></div>`).join('') + '</div>';
  }
  (d.lines || []).forEach((l) => { html += `<div class="line-item ${LEVEL[l.level] ?? ''}">${esc(l.text)}</div>`; });
  if (d.more) html += `<button type="button" class="more-link" data-open-attention>+${d.more} more</button>`;
  if (d.note) html += `<div class="note">${esc(d.note)}</div>`;
  if ((d.bars || []).length) {
    html += '<div class="games">' + d.bars.map((g) =>
      `<div class="game"><div class="game-row"><span>${esc(g.name)}</span><b>${esc(g.value)}</b></div>`
      + `<div class="thin"><div style="width:${Math.max(0, Math.min(100, Number(g.pct) || 0)).toFixed(0)}%"></div></div></div>`).join('') + '</div>';
  }
  if ((d.list || []).length) {
    html += '<div class="recent">' + d.list.map((x) =>
      `<div class="track">${x.img ? `<img src="${esc(x.img)}" alt="" loading="lazy">` : (d.list_art ? '<span class="noart"></span>' : '')}`
      + `<div class="track-text"><div class="track-title">${esc(x.title)}</div>${x.sub ? `<div class="track-artist">${esc(x.sub)}</div>` : ''}</div>`
      + `<span class="track-ago">${x.when ? ago(x.when) : ''}</span></div>`).join('') + '</div>';
  }
  if (t.ok === false) html += `<div class="err">Showing last good data. ${esc(t.error)}</div>`;
  return html;
}

function renderTiles() {
  const sections = new Map();
  (STATE.tiles || []).forEach((t) => {
    if (!sections.has(t.section)) sections.set(t.section, []);
    sections.get(t.section).push(t);
  });
  const host = $('#tiles');
  const key = [...sections].map(([name, ts]) => name + ':' + ts.map((t) => t.id).join(',')).join('|');
  if (host.dataset.key !== key) {          // build the skeleton only when the tile list changes
    host.dataset.key = key;
    host.innerHTML = [...sections].map(([name, ts]) => `<section class="media"><h2 class="section-title">${esc(name)}</h2>`
      + `<div class="media-grid">${ts.map((t) => `<article class="card small" id="tile-${esc(t.id)}"></article>`).join('')}</div></section>`).join('');
  }
  (STATE.tiles || []).forEach((t) => { const el = document.getElementById('tile-' + t.id); if (el) el.innerHTML = tileHtml(t); });
}

/* ------------------------------------------------------------------ running apps */
function renderApps() {
  const tnApps = (STATE.truenas.data && STATE.truenas.data.apps) || [];
  const dk = (STATE.docker.data && STATE.docker.data.apps) || [];
  const map = new Map();
  tnApps.forEach((a) => map.set(a.name, { name: a.name, state: a.state, update: a.update }));
  dk.forEach((a) => {
    const m = map.get(a.name) || { name: a.name, state: 'RUNNING', update: false };
    Object.assign(m, { cpu: a.cpu, mem: a.mem, rx: a.rx_mbps, tx: a.tx_mbps, stats: true });
    map.set(a.name, m);
  });
  const rows = [...map.values()].sort((a, b) => ((b.state === 'RUNNING') - (a.state === 'RUNNING')) || ((b.mem || 0) - (a.mem || 0)) || a.name.localeCompare(b.name));
  const running = rows.filter((r) => r.state === 'RUNNING').length;
  const updates = rows.filter((r) => r.update).length;
  const cpuTot = rows.reduce((t, r) => t + (r.cpu || 0), 0), memTot = rows.reduce((t, r) => t + (r.mem || 0), 0);
  const maxCpu = Math.max(...rows.map((r) => r.cpu || 0), 0.5), maxMem = Math.max(...rows.map((r) => r.mem || 0), 1);
  const stateCell = (st) => {
    const m = { RUNNING: ['', 'Running'], STOPPED: ['grey', 'Stopped'], STOPPING: ['amber', 'Stopping'], DEPLOYING: ['amber', 'Deploying'], CRASHED: ['red', 'Crashed'] }[st] || ['grey', st || 'Unknown'];
    return `<span class="status" style="font-size:14px;color:var(--soft)"><span class="dot ${m[0]}"></span>${esc(m[1])}</span>`;
  };
  const dockerNote = STATE.docker.configured && STATE.docker.ok === false ? `<div class="err">Usage unavailable: ${esc(STATE.docker.error)}</div>` : '';
  const tnNote = STATE.truenas.configured && STATE.truenas.ok === false ? `<div class="err">App status and updates unavailable: ${esc(STATE.truenas.error)}</div>` : '';
  $('#apps').innerHTML = `<div class="card-head"><div class="tile-title"><span class="logo" style="background:#6d28d9">`
    + `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>`
    + `</span><div><h2>Running apps</h2><div class="sub">TrueNAS · refreshes every ${STATE.poll_seconds} s</div></div></div>`
    + `<div class="apps-summary"><span class="status" style="font-size:13px;color:var(--soft)"><span class="dot"></span>${running} running</span>`
    + `<span class="status" style="font-size:13px;color:var(--soft)"><span class="dot amber"></span>${updates} update${updates === 1 ? '' : 's'} available</span>`
    + `<span>CPU ${cpuTot.toFixed(1)}% · RAM ${fmtBin(memTot)}</span></div></div>`
    + tnNote + dockerNote
    + (rows.length ? `<div class="table-wrap"><table><thead><tr><th scope="col">App</th><th scope="col">Status</th><th scope="col" style="width:22%">CPU (of host)</th>`
      + `<th scope="col" style="width:22%">Memory</th><th scope="col">Network</th><th scope="col">Version</th></tr></thead><tbody>`
      + rows.map((r) => `<tr><td class="c-name">${esc(r.name)}</td><td class="c-state">${stateCell(r.state)}</td>`
        + (r.stats
          ? `<td class="c-cpu" data-label="CPU"><div class="meter"><div class="thin"><div style="width:${((r.cpu || 0) / maxCpu * 100).toFixed(0)}%"></div></div><span>${(r.cpu || 0).toFixed(1)}%</span></div></td>`
            + `<td class="c-mem" data-label="Memory"><div class="meter mem"><div class="thin"><div style="width:${((r.mem || 0) / maxMem * 100).toFixed(0)}%"></div></div><span>${fmtBin(r.mem)}</span></div></td>`
            + `<td class="net c-net" data-label="Network"><span><span class="dn">↓</span> ${(r.rx || 0).toFixed(1)} <span class="up">↑</span> ${(r.tx || 0).toFixed(1)} <span class="muted">Mbps</span></span></td>`
          : '<td class="muted c-cpu" data-label="CPU">–</td><td class="muted c-mem" data-label="Memory">–</td><td class="muted c-net" data-label="Network">–</td>')
        + `<td class="c-ver" data-label="Version">${r.update ? '<span class="tag-update">Update available</span>' : '<span class="muted" style="font-size:13px">Up to date</span>'}</td></tr>`).join('')
      + '</tbody></table></div>'
      : '<div class="note">Loading…</div>');
}

/* ------------------------------------------------------------------ header, KPIs, attention */
function renderHeader() {
  const k = STATE.kpis;
  $('#kpi-online').textContent = k.online;
  $('#kpi-attention').textContent = k.attention;
  $('#kpi-offline').textContent = k.offline;
  const flagged = STATE.alerts.filter((a) => a.level !== 'info').length;
  $('#attention-count').textContent = flagged;
  $('#attention-list').innerHTML = STATE.alerts.length
    ? STATE.alerts.map((a) => `<li class="${esc(a.level)}"><div class="src">${esc(a.source)}</div><div class="ttl">${esc(a.title)}</div>`
      + `<div class="txt">${esc(a.text)}${a.link ? ` <a href="${esc(a.link)}" target="_blank" rel="noopener">How to fix</a>` : ''}</div></li>`).join('')
    : '<li class="info"><div class="ttl">All clear</div><div class="txt">Nothing needs attention right now.</div></li>';
}

function safe(name, fn) {
  // one section failing must not stop the others from rendering
  try { fn(); } catch (e) { console.error(`[dash] ${name} failed to render:`, e); }
}

function renderAll() {
  if (!STATE) return;
  safe('header', renderHeader); safe('truenas', renderTrueNAS); safe('unifi', renderUnifi); safe('chart', renderChart);
  safe('tiles', renderTiles); safe('apps', renderApps);
  if (STATE.title) { $('#title').textContent = STATE.title; document.title = STATE.title; }
}

function tick() {
  const now = new Date();
  $('#clock').textContent = fmtTime(now);
  $('#date').textContent = now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
  const dot = $('#updated-dot');
  dot.className = 'dot' + (fetchFailed ? ' red' : '');
  $('#updated').textContent = lastOk ? (fetchFailed ? 'Dashboard server unreachable · last ' : 'Updated ') + fmtTime(lastOk) : (fetchFailed ? 'Dashboard server unreachable' : 'Connecting…');
}

/* ------------------------------------------------------------------ data loading */
async function loadState() {
  try {
    const r = await fetch('api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    STATE = await r.json();
    lastOk = new Date(STATE.now * 1000);
    fetchFailed = false;
  } catch (e) {
    fetchFailed = true;
  }
  if (!fetchFailed) { checkBuild(); renderAll(); }
  tick();
  setTimeout(loadState, ((STATE && STATE.poll_seconds) || 30) * 1000);
}

// Reload the page when the dashboard's files change (after an update), so the browser never mixes
// an old index.html with a new app.js. Waits until two polls agree, in case files are still being copied.
let BUILD = null, pendingBuild = null;
function checkBuild() {
  if (!STATE.build) return;
  if (BUILD == null) { BUILD = STATE.build; return; }
  if (STATE.build === BUILD) { pendingBuild = null; return; }
  if (STATE.build === pendingBuild) location.reload();
  else pendingBuild = STATE.build;
}

async function loadThroughput() {
  try {
    const r = await fetch('api/throughput?hours=' + range, { cache: 'no-store' });
    if (r.ok) THR = await r.json();
  } catch (e) { /* keep last */ }
  if (STATE) { safe('unifi', renderUnifi); safe('chart', renderChart); }
}

document.addEventListener('click', (e) => {
  const c = e.target.closest('[data-chart]');
  if (c) { chartType = c.dataset.chart; savePref('chart', chartType); renderChart(); return; }
  const r = e.target.closest('[data-range]');
  if (r) { range = Number(r.dataset.range); savePref('range', String(range)); THR = null; renderChart(); loadThroughput(); return; }
  if (e.target.closest('[data-open-attention]') || e.target.closest('#attention-btn')) $('#attention-dialog').showModal();
});
$('#attention-close').addEventListener('click', () => $('#attention-dialog').close());
$('#attention-dialog').addEventListener('click', (e) => { if (e.target.id === 'attention-dialog') e.target.close(); });

tick();
setInterval(tick, 1000);
loadState();
loadThroughput();
setInterval(loadThroughput, 60000);

/* ------------------------------------------------------------------ theme */
function setTheme(t, save) {
  document.documentElement.dataset.theme = t;
  const label = t === 'light' ? 'Switch to dark theme' : 'Switch to light theme';
  $('#theme-btn').title = label; $('#theme-btn').setAttribute('aria-label', label);
  if (save) savePref('theme', t);
}
setTheme(document.documentElement.dataset.theme || 'dark', false);
$('#theme-btn').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light', true));
// follow the OS setting live until a theme has been picked with the button
if (window.matchMedia) matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
  if (!pref('theme', null) && !new URLSearchParams(location.search).get('theme')) setTheme(e.matches ? 'light' : 'dark', false);
});

