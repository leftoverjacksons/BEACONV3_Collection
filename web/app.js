/* BEACON Station touchscreen dashboard. Polls the local JSON API; no
   framework, one vendored dependency (uPlot). Everything the CSV stores in
   deg C is converted here for display only. */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const prefs = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

let CFG = null;
let latest = null;              // last /api/latest response
let latestOk = false;
let unit = prefs.get("unit", "F");
let page = "overview";
let trWindow = +prefs.get("trWindow", 3600);
let wrWindow = +prefs.get("wrWindow", 600);
let windView = prefs.get("windView", "rose");
let lastRose = null;
let tick = 0;

const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const toT = (c) => (c == null ? null : unit === "F" ? c * 9 / 5 + 32 : c);
const fmt = (v, d = 1) => (v == null || !isFinite(v) ? "–" : v.toFixed(d));
const pad2 = (n) => String(n).padStart(2, "0");
const hms = (t) => { const d = new Date(t * 1000); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`; };
const CARD16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
const cardinal = (deg) => CARD16[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];

function ago(s) {
  if (s == null || !isFinite(s)) return "–";
  if (s < 90) return `${Math.max(0, s | 0)} s`;
  if (s < 5400) return `${(s / 60) | 0} min`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.h);
  toast.h = setTimeout(() => (el.hidden = true), 2500);
}

/* ================================================================ health */
const ICON = { good: "●", warn: "▲", crit: "✕", off: "○" };

function health(inst) {
  if (!latestOk) return ["crit", "display server unreachable"];
  if (!CFG || !latest) return ["off", "–"];
  if (!CFG.enabled[inst]) return ["off", "disabled"];
  const now = latest.now;
  if (!latest.status_rx || now - latest.status_rx > 10) return ["crit", "logger not running"];
  const rd = latest.status.readers?.[inst];
  if (rd && rd.state !== "connected") return ["crit", rd.state === "waiting" ? "port unavailable" : rd.state];
  // HOBO rows are only written on change/heartbeat; "heard" is fresher.
  const t = rd?.last_seen ?? latest.latest?.[inst]?.t;
  if (t == null) return ["warn", "waiting for data"];
  const age = now - t;
  const [w, e] = CFG.stale[inst];
  if (age > e) return ["crit", `no data for ${ago(age)}`];
  if (age > w) return ["warn", `late (${ago(age)})`];
  return ["good", "live"];
}

function paintHealth() {
  for (const inst of ["beacon", "anemo", "hobo"]) {
    const [lvl, text] = health(inst);
    const chip = $(`#chip-${inst}`);
    chip.className = `chip ${lvl}`;
    chip.querySelector("i").textContent = ICON[lvl];
    chip.title = text;
    const st = $(`#st-${inst}`);
    st.className = `state ${lvl}`;
    st.textContent = `${ICON[lvl]} ${text}`;
  }
}

/* ================================================================ overview */
function paintOverview() {
  if (!latest) return;
  const b = latest.latest.beacon;
  const a = latest.latest.anemo;
  const st = latest.status;
  $$(".unitT").forEach((el) => (el.textContent = `°${unit}`));

  $("#ov-comp").textContent = fmt(toT(b?.comp_temp_C), 1);
  $("#ov-wbgt").textContent = fmt(toT(b?.WBGT_C), 1);
  $("#ov-rh").textContent = fmt(b?.RH_pct, 1);
  $("#ov-p").textContent = fmt(b?.P_hPa, 1);
  $("#ov-tmp").textContent = b ? `${fmt(toT(b.TMP119_C), 2)}°` : "–";
  $("#ov-sht").textContent = b ? `${fmt(toT(b.SHT3x_C), 2)}°` : "–";
  $("#ov-hdc").textContent = b ? `${fmt(toT(b.HDC3022_C), 2)}°` : "–";
  $("#ov-bfoot").textContent = b
    ? `sample ${hms(b.t)} (${ago(latest.now - b.t)} ago) · n=${st?.counts?.beacon ?? "–"} this run`
    : "no data";

  $("#ov-spd").textContent = fmt(a?.wind_ms, 2);
  const deadband = a && a.wind_ms === 0;
  if (a && a.wind_deg != null && !deadband) {
    $("#ov-dirtxt").textContent = `from ${cardinal(a.wind_deg)} · ${fmt(a.wind_deg, 0)}°`;
    $("#needle").setAttribute("transform", `rotate(${a.wind_deg})`);
    $("#compass").classList.remove("stale");
  } else {
    $("#ov-dirtxt").textContent = deadband ? "below sensor threshold (< 0.3 m/s)" : "direction –";
    $("#compass").classList.add("stale");
  }
  $("#ov-afoot").textContent = a
    ? `sample ${hms(a.t)} (${ago(latest.now - a.t)} ago) · n=${st?.counts?.anemo ?? "–"} this run`
    : "no data";

  const h = latest.latest.hobo;
  $("#ov-solar").textContent = fmt(h?.solar_Wm2, 1);
  $("#ov-accum").textContent = fmt(h?.solar_accum_MJm2, 4);
  $("#ov-htemp").textContent = fmt(toT(h?.hobo_T_C), 1);
  $("#ov-hrh").textContent = fmt(h?.hobo_RH_pct, 1);
  const rd = st?.readers?.hobo;
  $("#ov-hfoot").textContent = h
    ? `heard ${ago(latest.now - (rd?.last_seen ?? h.t))} ago · n=${st?.counts?.hobo ?? "–"} this run`
    : "no data";
}

function paintSolarNow() {
  if (!latest) return;
  const h = latest.latest.hobo;
  $$(".unitT").forEach((el) => (el.textContent = `°${unit}`));
  $("#so-now").textContent = fmt(h?.solar_Wm2, 1);
  $("#so-accum").textContent = fmt(h?.solar_accum_MJm2, 4);
  $("#so-t").textContent = fmt(toT(h?.hobo_T_C), 1);
  $("#so-rh").textContent = fmt(h?.hobo_RH_pct, 1);
  $("#so-0d").textContent = fmt(h?.hobo_ch0d, 3);
}

async function refreshOverviewWind() {
  try {
    const r = await getJSON("/api/wind?window=600");
    $("#ov-mean").textContent = fmt(r.mean, 2);
    $("#ov-gust").textContent = fmt(r.gust, 2);
  } catch { /* shown via health chips */ }
}

/* ================================================================ charts */
/* Chart groups, one per page. A series may come from a different source
   than its chart's default (e.g. HOBO temperature on the BEACON chart);
   such charts get their sources merged onto one x axis (see joinSources). */
const HOBO = "--s6";   // the HOBO keeps one colour on every chart
const GROUPS = {
  temp: {
    host: "#charts-temp", info: "#info-temp",
    specs: [
      {
        id: "temp", src: "beacon", h: 0.64, dec: 2, temp: true, labels: true,
        title: () => `Temp °${unit}`,
        series: [
          { k: "TMP119_C", label: "TMP119", color: "--s1", w: 1.25 },
          { k: "SHT3x_C", label: "SHT3x", color: "--s2", w: 1.25 },
          { k: "HDC3022_C", label: "HDC3022", color: "--s3", w: 1.25 },
          { k: "comp_temp_C", label: "comp", color: "--s4", w: 2.5 },
          { k: "WBGT_C", label: "WBGT", color: "--s5", w: 2, dash: [6, 4] },
          { k: "hobo_T_C", src: "hobo", label: "HOBO", color: HOBO, w: 2, dash: [2, 3] },
        ],
      },
      {
        id: "rh", src: "beacon", h: 0.36, dec: 1, xaxis: true,
        title: () => "RH %",
        series: [
          { k: "RH_pct", label: "BEACON", color: "--s1", w: 1.5 },
          { k: "hobo_RH_pct", src: "hobo", label: "HOBO", color: HOBO, w: 2, dash: [2, 3] },
        ],
      },
    ],
  },
  wind: {
    host: "#charts-wind", info: "#info-wind", windowVar: () => wrWindow,
    specs: [
      {
        id: "wind", src: "anemo", h: 0.55, dec: 2, y0: true, labels: true,
        title: () => "Wind m/s",
        series: [
          { k: "mean", label: "mean", color: "--s1", w: 1.5 },
          { k: "gust", label: "max", color: "--s2", w: 1, dash: [3, 3] },
          { k: "zero", label: "deadband", color: "--muted", points: true },
        ],
      },
      {
        id: "dir", src: "anemo", h: 0.45, dec: 0, y: [0, 360], dir: true, xaxis: true,
        title: () => "Direction (from)",
        series: [{ k: "dir", label: "dir", color: "--s1", points: true }],
      },
    ],
  },
  solar: {
    host: "#charts-solar", info: "#info-solar",
    specs: [
      {
        id: "solar", src: "hobo", h: 0.66, dec: 1, y0: true, labels: true,
        title: () => "Solar irradiance W/m²",
        series: [{ k: "solar_Wm2", label: "irradiance", color: HOBO, w: 2 }],
      },
      {
        id: "accum", src: "hobo", h: 0.34, dec: 4, xaxis: true,
        title: () => "Accumulated MJ/m²",
        series: [{ k: "solar_accum_MJm2", label: "accumulated", color: HOBO, w: 1.5 }],
      },
    ],
  },
};
const built = {};      // group -> [{spec, u, legend}]
const lastHist = {};   // group -> /api/history response
let xr = [0, 1];

function eventsPlugin(group, labels) {
  return {
    hooks: {
      draw: [(u) => {
        const ev = lastHist[group]?.events ?? [];
        if (!ev.length) return;
        const ctx = u.ctx;
        const { top, height } = u.bbox;
        ctx.save();
        ctx.strokeStyle = cssv("--s5");
        ctx.fillStyle = cssv("--ink-2");
        ctx.lineWidth = devicePixelRatio;
        ctx.setLineDash([4 * devicePixelRatio, 3 * devicePixelRatio]);
        ctx.font = `${11 * devicePixelRatio}px system-ui`;
        let lastX = -Infinity, row = 0;
        for (const e of ev) {
          const x = Math.round(u.valToPos(e.t, "x", true));
          if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
          ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
          if (!labels) continue;
          // Stagger labels of nearby markers; right-align near the edge.
          row = x - lastX < 90 * devicePixelRatio ? (row + 1) % 3 : 0;
          lastX = x;
          const w = ctx.measureText(e.note).width;
          const lx = x + w + 6 * devicePixelRatio > u.bbox.left + u.bbox.width ? x - w - 3 * devicePixelRatio : x + 3 * devicePixelRatio;
          ctx.fillText(e.note, lx, top + (12 + 12 * row) * devicePixelRatio);
        }
        ctx.restore();
      }],
    },
  };
}

function buildGroup(g) {
  for (const c of built[g] ?? []) c.u.destroy();
  built[g] = [];
  const host = $(GROUPS[g].host);
  host.innerHTML = "";
  const H = host.clientHeight;
  const W = host.clientWidth;
  if (!H || !W) { delete built[g]; return; }   // page not visible yet
  const muted = cssv("--muted");
  const grid = cssv("--grid");
  const font = "11px system-ui";

  for (const spec of GROUPS[g].specs) {
    const wrap = document.createElement("div");
    wrap.className = "chart";
    const legend = document.createElement("div");
    legend.className = "legend";
    wrap.appendChild(legend);
    host.appendChild(wrap);

    const xAxis = {
      stroke: muted, font, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid },
      size: spec.xaxis ? 26 : 4,
      ...(spec.xaxis ? {} : { values: (u, s) => s.map(() => "") }),
    };
    const yAxis = {
      stroke: muted, font, size: 50, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid },
      ...(spec.dir ? {
        splits: () => [0, 90, 180, 270, 360],
        values: (u, s) => s.map((v) => ({ 0: "", 90: "E", 180: "S", 270: "W", 360: "N" })[v]),
      } : {}),
    };
    const yScale = spec.y ? { range: () => spec.y }
      : spec.y0 ? { range: (u, mn, mx) => [0, Math.max(1, (mx ?? 1) * 1.15)] }
        : { range: (u, mn, mx) => (mn == null ? [0, 1] : [mn - Math.max(0.2, (mx - mn) * 0.08), mx + Math.max(0.2, (mx - mn) * 0.08)]) };

    const opts = {
      width: W,
      height: Math.max(60, Math.floor(H * spec.h) - 20),
      legend: { show: false },
      cursor: { sync: { key: `g-${g}` }, drag: { x: false, y: false }, points: { size: 7 } },
      scales: { x: { time: true, auto: false, range: () => xr }, y: { auto: true, ...yScale } },
      axes: [xAxis, yAxis],
      series: [{}, ...spec.series.map((s) => {
        const col = cssv(s.color);
        return {
          label: s.label, stroke: col, width: s.w ?? 1, dash: s.dash, spanGaps: false,
          ...(s.points
            ? { paths: () => null, points: { show: true, size: 5, fill: col, stroke: col, width: 0 } }
            : { points: { show: false } }),
        };
      })],
      plugins: [eventsPlugin(g, !!spec.labels)],
      hooks: { setCursor: [(u) => paintLegend(spec, u, legend)] },
    };
    const empty = [[], ...spec.series.map(() => [])];
    const u = new uPlot(opts, empty, wrap);
    built[g].push({ spec, u, legend });
    paintLegend(spec, u, legend);
  }
  if (lastHist[g]) paintGroup(g);
}

function rebuildAll() {
  for (const g of Object.keys(built)) buildGroup(g);
}

function paintLegend(spec, u, el) {
  const idx = u.cursor.idx;
  const parts = [`<span class="ttl">${spec.title()}</span>`];
  spec.series.forEach((s, i) => {
    const ys = u.data[i + 1] ?? [];
    let v = null;
    // At the cursor, or the latest value; merged charts have holes
    // (undefined) where another source has a point, so search back.
    for (let j = idx ?? ys.length - 1; j >= 0; j--) if (ys[j] != null) { v = ys[j]; break; }
    const cls = s.points ? "sw dot" : s.dash ? "sw dash" : "sw";
    const col = cssv(s.color);
    const val = spec.dir && v != null ? `${cardinal(v)} ${fmt(v, 0)}°` : fmt(v, spec.dec);
    const show = spec.series.length > 1 || spec.dir;
    parts.push(`<span>${show ? `<i class="${cls}" style="background:${col};color:${col}"></i>${s.label} ` : ""}<b>${val}</b></span>`);
  });
  if (idx != null && u.data[0][idx] != null) parts.push(`<span class="muted">@ ${hms(u.data[0][idx])}</span>`);
  el.innerHTML = parts.join("");
}

/* Merge several sources' [t, ...ys] onto their union of timestamps. Where a
   source has no point, the value is left undefined, which uPlot draws
   through; the explicit nulls the server inserts at real dropouts stay
   null and still break the line. */
function joinSources(parts) {
  if (parts.length === 1) return [parts[0].t, ...parts[0].ys];
  const xs = [...new Set(parts.flatMap((p) => p.t))].sort((a, b) => a - b);
  const at = new Map(xs.map((x, i) => [x, i]));
  const out = [xs];
  for (const p of parts) {
    for (const y of p.ys) {
      const col = new Array(xs.length);
      p.t.forEach((x, j) => { col[at.get(x)] = y[j]; });
      out.push(col);
    }
  }
  return out;
}

function seriesData(h, spec) {
  // Group this chart's series by source, preserving series order.
  const bySrc = new Map();
  spec.series.forEach((s, i) => {
    const src = s.src ?? spec.src;
    if (!bySrc.has(src)) bySrc.set(src, []);
    bySrc.get(src).push(i);
  });
  const order = [];
  const parts = [];
  for (const [src, idxs] of bySrc) {
    const d = h[src];
    parts.push({
      t: d.t,
      ys: idxs.map((i) => {
        const s = spec.series[i];
        return spec.temp ? d[s.k].map(toT) : d[s.k];
      }),
    });
    order.push(...idxs);
  }
  const joined = joinSources(parts);
  // Put the columns back in the spec's series order.
  const cols = new Array(spec.series.length);
  order.forEach((si, j) => { cols[si] = joined[j + 1]; });
  return [joined[0], ...cols];
}

function paintGroup(g) {
  const h = lastHist[g];
  if (!built[g] || !h) return;
  xr = [h.t0, h.t1];
  for (const { spec, u, legend } of built[g]) {
    u.setData(seriesData(h, spec), true);
    paintLegend(spec, u, legend);
  }
  const n = h.n_raw;
  const info = { temp: `${n.beacon} beacon · ${n.hobo} hobo`, wind: `${n.anemo} anemo`, solar: `${n.hobo} hobo` }[g];
  $(GROUPS[g].info).textContent = `${info} samples in window`;
  if (g === "solar") paintSolarStats(h);
}

async function refreshGroup(g) {
  const host = $(GROUPS[g].host);
  if (!host || host.hidden) return;
  try {
    const win = GROUPS[g].windowVar ? GROUPS[g].windowVar() : trWindow;
    const pts = Math.min(1200, Math.max(200, host.clientWidth));
    lastHist[g] = await getJSON(`/api/history?window=${win}&points=${pts}`);
    if (!built[g]) buildGroup(g); else paintGroup(g);
  } catch (e) { /* health chips show connectivity */ }
}

function paintSolarStats(h) {
  const s = h.hobo.solar_Wm2.filter((v) => v != null);
  $("#so-peak").textContent = s.length ? fmt(Math.max(...s), 1) : "–";
}

/* ================================================================ wind rose */
function roseRamp() {
  return document.documentElement.dataset.theme === "light"
    ? ["#86b6ef", "#5598e7", "#256abf", "#104281"]
    : ["#184f95", "#2a78d6", "#6da7ec", "#b7d3f6"];
}

function paintRose() {
  const r = lastRose;
  const cv = $("#rose");
  const dpr = devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 22;
  const muted = cssv("--muted"), grid = cssv("--grid"), ink2 = cssv("--ink-2");
  const ramp = roseRamp();

  $("#rose-legend").innerHTML = ["0.5–1.5", "1.5–3", "3–5", "≥ 5"]
    .map((t, i) => `<div><i style="background:${ramp[i]}"></i>${t} m/s</div>`).join("");

  if (!r || !r.total) {
    ctx.fillStyle = muted; ctx.font = "14px system-ui"; ctx.textAlign = "center";
    ctx.fillText("no wind data in window", cx, cy);
    return;
  }
  const secFrac = r.counts.map((c) => c.reduce((a, b) => a + b, 0) / r.total);
  const maxFrac = Math.max(0.05, ...secFrac);
  // 3-4 rings at a "nice" percentage step
  const step = [0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25].find((x) => maxFrac / x <= 4) ?? 0.25;
  const top = Math.ceil(maxFrac / step) * step;

  // rings + labels
  ctx.strokeStyle = grid; ctx.fillStyle = muted; ctx.lineWidth = 1;
  ctx.font = "11px system-ui"; ctx.textAlign = "left";
  const la = (112.5 - 90) * Math.PI / 180;   // ring labels along ESE
  for (let f = step; f <= top + 1e-9; f += step) {
    const rr = (f / top) * R;
    ctx.beginPath(); ctx.arc(cx, cy, rr, 0, 2 * Math.PI); ctx.stroke();
    ctx.fillText(`${Math.round(f * 1000) / 10}%`, cx + Math.cos(la) * rr + 3, cy + Math.sin(la) * rr + 11);
  }
  ctx.textAlign = "center"; ctx.fillStyle = ink2; ctx.font = "13px system-ui";
  [["N", 0], ["E", 90], ["S", 180], ["W", 270]].forEach(([t, d]) => {
    const a = (d - 90) * Math.PI / 180;
    ctx.fillText(t, cx + Math.cos(a) * (R + 12), cy + Math.sin(a) * (R + 12) + 4);
  });

  // stacked wedges; class 0 is calm and not drawn
  const sw = 360 / r.sectors;
  const gap = 2 * Math.PI / 180;
  const surf = cssv("--page");
  for (let s = 0; s < r.sectors; s++) {
    const a0 = (s * sw - sw / 2 - 90) * Math.PI / 180 + gap;
    const a1 = (s * sw + sw / 2 - 90) * Math.PI / 180 - gap;
    let cum = 0;
    for (let c = 1; c < r.counts[s].length; c++) {
      const n = r.counts[s][c];
      if (!n) continue;
      const r0 = (cum / r.total / top) * R;
      cum += n;
      const r1 = (cum / r.total / top) * R;
      ctx.beginPath();
      ctx.arc(cx, cy, r1, a0, a1);
      ctx.arc(cx, cy, r0, a1, a0, true);
      ctx.closePath();
      ctx.fillStyle = ramp[c - 1];
      ctx.fill();
      ctx.strokeStyle = surf; ctx.lineWidth = 1.5; ctx.stroke();
    }
  }
}

async function refreshWind() {
  try {
    lastRose = await getJSON(`/api/wind?window=${wrWindow}`);
    const r = lastRose;
    $("#wr-mean").textContent = fmt(r.mean, 2);
    $("#wr-gust").textContent = fmt(r.gust, 2);
    $("#wr-prev").textContent = r.prevailing == null ? "–" : `${cardinal(r.prevailing)} ${fmt(r.prevailing, 0)}°`;
    $("#wr-calm").textContent = r.total ? fmt(100 * r.calm / r.total, 0) : "–";
    $("#wr-n").textContent = r.total;
    paintRose();
  } catch { /* health chips */ }
}

/* ================================================================ system */
const THROTTLE_BITS = [[0, "under-voltage"], [1, "freq capped"], [2, "throttled"], [3, "soft temp limit"]];
function decodeThrottle(hex) {
  if (hex == null) return ["", "n/a"];
  const v = parseInt(hex, 16);
  if (!v) return ["good", "OK"];
  const now = THROTTLE_BITS.filter(([b]) => v & (1 << b)).map(([, t]) => t);
  const past = THROTTLE_BITS.filter(([b]) => v & (1 << (b + 16))).map(([, t]) => t);
  if (now.length) return ["crit", `NOW: ${now.join(", ")}`];
  return ["warn", `since boot: ${past.join(", ")}`];
}

async function refreshSystem() {
  let r;
  try { r = await getJSON("/api/system"); } catch { return; }
  const s = r.sys;
  const st = latest?.status;
  const rows = [];
  const sep = (t) => rows.push(`<tr class="sep"><th colspan="2">${t}</th></tr>`);
  const row = (k, v, cls = "") => rows.push(`<tr><th>${k}</th><td class="${cls}">${v ?? "–"}</td></tr>`);

  sep("Station");
  row("Host", s.hostname);
  row("Software", s.git ?? "unknown");
  if (!CFG?.readonly) row("Config", r.config_source);
  row("Clock", new Date(s.time_utc * 1000).toLocaleString());
  row("Clock synced", s.clock_synced == null ? "unknown" : s.clock_synced ? "yes (NTP)" : "NO — check RTC / network",
    s.clock_synced ? "good" : s.clock_synced === false ? "warn" : "");

  sep("Logger");
  if (st && latest.now - latest.status_rx < 10) {
    row("State", st.simulated ? "running — SIMULATED DATA" : "running", st.simulated ? "warn" : "good");
    row("Running for", ago(latest.now - st.started));
    row("File", st.file ?? "(opens on first sample)");
    row("Rows in file", st.file_rows);
    const NAMES = { beacon: "BEACON", anemo: "Anemometer", hobo: "HOBO MX2309" };
    for (const inst of ["beacon", "anemo", "hobo"]) {
      const rd = st.readers?.[inst];
      if (!rd) { row(NAMES[inst], "disabled"); continue; }
      const [lvl, text] = health(inst);
      const extra = inst === "hobo" && rd.address
        ? `<br><span class="muted">${rd.address}${rd.serial ? " · SN " + rd.serial : ""}</span>` : "";
      row(NAMES[inst],
        `${ICON[lvl]} ${text}<br><span class="muted">${rd.port}${rd.detail && rd.detail !== rd.port ? " — " + rd.detail : ""}</span>${extra}`, lvl);
    }
  } else {
    row("State", "NOT RUNNING — no status from beacon-logger", "crit");
  }

  const left = rows.splice(0);
  const n = s.net;
  if (n && !CFG?.readonly) {
    // How to reach this Pi — shown here so it can be read off the screen.
    sep("Network");
    row("Wi-Fi", n.ssid ?? "not connected", n.ssid ? "good" : "warn");
    row("IP address", n.lan_ips.length
      ? n.lan_ips.map((ip) => `${ip}<br><span class="muted">ssh ${n.user}@${ip}</span>`).join("<br>")
      : "none", n.lan_ips.length ? "" : "crit");
    row("Tailscale", n.tailscale_ip
      ? `${n.tailscale_ip}<br><span class="muted">ssh ${n.user}@${s.hostname}</span>`
      : "off");
  }
  sep("Host");
  const t = s.cpu_temp_c;
  row("CPU temp", t == null ? "n/a" : `${fmt(t, 1)} °C`, t == null ? "" : t >= 80 ? "crit" : t >= 70 ? "warn" : "good");
  const [tc, tt] = decodeThrottle(s.throttled);
  row("Power / throttle", tt, tc);
  if (s.disk) {
    const f = s.disk.free_gb;
    row("Disk free", `${fmt(f, 1)} / ${fmt(s.disk.total_gb, 0)} GB`, f < 0.5 ? "crit" : f < 2 ? "warn" : "good");
  }
  if (s.mem) row("Memory available", `${fmt(s.mem.avail_mb, 0)} / ${fmt(s.mem.total_mb, 0)} MB`);
  row("Uptime", ago(s.uptime_s));
  rows.push(`<tr class="sep"><th>Display</th><td><button id="themeBtn">${document.documentElement.dataset.theme === "light" ? "Dark theme" : "Light theme (sun)"}</button></td></tr>`);
  // Only the Pi's own browser gets the exit button (the server enforces it too).
  if (r.local && !CFG?.readonly) {
    rows.push(`<tr><th>Kiosk</th><td><button id="exitKioskBtn">Exit to desktop</button><br><span class="muted">reopen: "BEACON Kiosk" icon, or reboot</span></td></tr>`);
  }

  $("#sys-table").innerHTML = left.join("");
  $("#sys-table2").innerHTML = rows.join("");
  $("#themeBtn").onclick = toggleTheme;
  const ex = $("#exitKioskBtn");
  if (ex) ex.onclick = exitKiosk;

  $("#sys-notes").innerHTML = (r.notes || []).slice().reverse()
    .map((n) => `<li><time>${hms(n.t)}</time>${n.inst}: ${String(n.text).replace(/[<&]/g, (c) => ({ "<": "&lt;", "&": "&amp;" })[c])}</li>`)
    .join("") || `<li class="muted">none</li>`;
}

async function exitKiosk() {
  if (!confirm("Close the full-screen dashboard and go to the desktop?\n\nLogging continues. Reopen with the \"BEACON Kiosk\" icon or by rebooting.")) return;
  try {
    const r = await getJSON("/api/kiosk/exit", { method: "POST" });
    if (!r.ok) toast(`Exit failed: ${r.error}`);
  } catch (e) { toast(`Exit failed: ${e.message}`); }
}

function toggleTheme() {
  const light = document.documentElement.dataset.theme !== "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  prefs.set("theme", light ? "light" : "dark");
  rebuildAll();
  refreshSystem();
}

/* ================================================================ wiring */
function showPage(p) {
  page = p;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.page === p));
  $$(".page").forEach((s) => s.classList.toggle("active", s.id === `page-${p}`));
  refreshPage();
}

function refreshPage() {
  if (page === "overview") refreshOverviewWind();
  else if (page === "temp") refreshGroup("temp");
  else if (page === "wind") { if (windView === "rose") refreshWind(); else refreshGroup("wind"); }
  else if (page === "solar") { paintSolarNow(); refreshGroup("solar"); }
  else if (page === "system") refreshSystem();
}

function segment(sel, attr, current, onPick) {
  $$(`${sel} button`).forEach((b) => {
    b.classList.toggle("active", b.dataset[attr] === String(current));
    b.onclick = () => {
      $$(`${sel} button`).forEach((x) => x.classList.toggle("active", x === b));
      onPick(b.dataset[attr]);
    };
  });
}

async function poll() {
  try {
    latest = await getJSON("/api/latest");
    latestOk = true;
  } catch {
    latestOk = false;
  }
  paintHealth();
  if (page === "overview") paintOverview();
  if (page === "solar") paintSolarNow();
  if (++tick % 5 === 0) refreshPage();
}

function clock() {
  const d = new Date();
  $("#clock").textContent = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

async function init() {
  document.documentElement.dataset.theme = prefs.get("theme", "dark");
  $("#compass .ticks").innerHTML = Array.from({ length: 12 }, (_, i) =>
    `<line y1="-50" y2="${i % 3 ? -46 : -43}" transform="rotate(${i * 30})"/>`).join("");
  $$("#tabs button").forEach((b) => (b.onclick = () => showPage(b.dataset.page)));
  // Temp/RH and Solar share one time window; Wind has its own.
  const pickTr = (w) => {
    trWindow = +w; prefs.set("trWindow", w);
    $$(".win-seg button").forEach((b) => b.classList.toggle("active", b.dataset.w === String(w)));
    refreshPage();
  };
  segment("#tr-window", "w", trWindow, pickTr);
  segment("#so-window", "w", trWindow, pickTr);
  segment("#wr-window", "w", wrWindow, (w) => { wrWindow = +w; prefs.set("wrWindow", w); refreshPage(); });
  const setWindView = (v) => {
    windView = v; prefs.set("windView", v);
    $("#wind-rose").hidden = v !== "rose";
    $("#charts-wind").hidden = v !== "ts";
    refreshPage();
  };
  segment("#wind-view", "v", windView, setWindView);
  $("#wind-rose").hidden = windView !== "rose";
  $("#charts-wind").hidden = windView !== "ts";
  segment("#unit-toggle", "u", unit, (u) => { unit = u; prefs.set("unit", u); rebuildAll(); paintOverview(); paintSolarNow(); });

  $("#markBtn").onclick = () => ($("#modal").hidden = false);
  $("#modalCancel").onclick = () => ($("#modal").hidden = true);
  $("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").hidden = true; };

  let resizeT;
  window.addEventListener("resize", () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => { rebuildAll(); if (page === "wind" && windView === "rose") paintRose(); }, 200);
  });

  for (;;) {
    try { CFG = await getJSON("/api/config"); break; }
    catch { latestOk = false; paintHealth(); await new Promise((r) => setTimeout(r, 2000)); }
  }
  if (CFG.readonly) {
    $("#markBtn").remove();
    const chip = document.createElement("span");
    chip.className = "chip off";
    chip.textContent = "view only";
    $("#health").prepend(chip);
  }
  $("#labels").innerHTML = CFG.event_labels.map((l, i) => `<button data-i="${i}"></button>`).join("");
  $$("#labels button").forEach((b) => {
    const label = CFG.event_labels[+b.dataset.i];
    b.textContent = label;
    b.onclick = async () => {
      $("#modal").hidden = true;
      try {
        const r = await getJSON("/api/event", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label }),
        });
        toast(r.ok ? `Marked “${label}” at ${hms(Date.now() / 1000)}` : `Mark failed: ${r.error}`);
      } catch (e) { toast(`Mark failed: ${e.message}`); }
    };
  });

  clock(); setInterval(clock, 1000);
  await poll(); setInterval(poll, 1000);
  refreshPage();
}

init();
