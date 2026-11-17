"use strict";
// What the `perf` feature measures, on the pipeline graph and in uPlot charts. Shared by the
// control page (live: a window every interval) and a run's report.html (inlined, the whole run),
// after graph.js, uPlot and metrics.js (METRIC_UNITS, the unit of each metric, generated from
// mixtape/perf/metrics.py).
//
// The graph answers "where does the time go, what backs up" at a glance: each element's mean
// latency on its node, the slowest one in bold, and a level bar under each queue at its fullest.
// A link's tooltip adds its flow (bitrate, buffers), push time and lateness. The charts show
// how all of it changes over time.

const PERF_SUMMARY = "perf.summary";

/** The charts, in order: the first ones always, the others with "All metrics". `stat` is the
 * statistic of each window plotted: the mean, or the peak where a short one matters. */
const CHARTS = [
  { metric: "pipeline.latency", title: "Pipeline latency", stat: "mean" },
  { metric: "element.latency", title: "Element latency", stat: "mean" },
  { metric: "queue.fill", title: "Queue fill", stat: "max" },
  { metric: "process.cpu", title: "CPU", stat: "mean" },
  { metric: "thread.cpu", title: "Thread CPU", stat: "mean", more: true },
  { metric: "pad.push", title: "Push time", stat: "mean", more: true },
  { metric: "pad.lateness", title: "Lateness", stat: "mean", more: true },
  { metric: "pad.bitrate", title: "Bitrate", stat: "mean", more: true },
  { metric: "pad.buffers", title: "Buffers", stat: "mean", more: true },
  { metric: "pad.interval", title: "Buffer interval", stat: "max", more: true },
  { metric: "queue.time", title: "Queue time", stat: "max", more: true },
  { metric: "queue.buffers", title: "Queue buffers", stat: "max", more: true },
  { metric: "queue.bytes", title: "Queue bytes", stat: "max", more: true },
];

const LIVE_SPAN = 300;  // seconds of history the page keeps

// ---- numbers -----------------------------------------------------------------------------

const SCALES = {
  ns: [[1e9, " s"], [1e6, " ms"], [1e3, " µs"], [1, " ns"]],
  "bit/s": [[1e9, " Gbit/s"], [1e6, " Mbit/s"], [1e3, " kbit/s"], [1, " bit/s"]],
  bytes: [[2 ** 30, " GiB"], [2 ** 20, " MiB"], [2 ** 10, " KiB"], [1, " B"]],
  "buffers/s": [[1, " buf/s"]],
  buffers: [[1, ""]],
  ratio: [[0.01, "%"]],
  "%": [[1, "%"]],
};

/** The step of `unit` to show `v` in: the largest not above it. */
function scaleOf(unit, v) {
  const steps = SCALES[unit] || [[1, ` ${unit}`]];
  return steps.find(([d]) => Math.abs(v) >= d) || steps[steps.length - 1];
}

/** A measure in its unit with three significant digits: 23.8 ms, 2.12 Mbit/s, 53.1%. */
function fmtMeasure(v, unit, scale = null) {
  if (v == null || !Number.isFinite(v)) return "—";
  scale = scale || scaleOf(unit, Number(v.toPrecision(3)));  // 999.6 ms reads 1.00 s
  const x = v / scale[0], a = Math.abs(x);
  return x.toFixed(Number.isInteger(x) || a >= 100 ? 0 : a >= 10 ? 1 : 2) + scale[1];
}

/** Signed, for a measure whose sign means something (lateness: early when negative). */
const fmtSigned = (v, unit) => (v > 0 ? "+" : v < 0 ? "−" : "") + fmtMeasure(Math.abs(v), unit);

/** Axis labels: one step for all ticks, as few digits as tell them apart. */
function tickLabels(ticks, unit) {
  const scale = scaleOf(unit, Math.max(0, ...ticks.map(Math.abs)));
  return ticks.map((t) => parseFloat((t / scale[0]).toPrecision(4)) + scale[1]);
}

/** Steps of the time axis: whole seconds, so no two ticks read the same */
const SECONDS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400];
const fmtClock = (s) => new Date(s * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
function fmtElapsed(s) {
  const t = Math.round(Math.max(0, s));
  const hh = Math.floor(t / 3600), mm = Math.floor(t / 60) % 60, ss = String(t % 60).padStart(2, "0");
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

const elementOf = (subject) => subject.split(":")[0];

// ---- colours: one per element, the same in every chart, in the order data flows ----------

const PALETTE = {
  light: ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1", "#76b7b2", "#c9a227", "#9c755f", "#ff8fa0", "#8c8c8c"],
  dark: ["#79a6dc", "#f5a35a", "#7cc06e", "#f07f81", "#c99bc2", "#8fd0ca", "#e8c95a", "#c49d84", "#ffadba", "#b0b0b0"],
};
const darkScheme = matchMedia("(prefers-color-scheme: dark)");
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---- the view ----------------------------------------------------------------------------

/** The perf view: `section` (the charts), `measures` (for graph.measures), `add(window)` for
 * each live window, `setGraph(graph)` for the flow order, `dispose()` when it is dropped. A report passes `run`: its summary,
 * windows and first time, and has no live windows. */
function createPerf({ live, run = null }) {
  const state = {
    windows: [],       // [{t: seconds, end: ns, span: seconds, metrics}]
    offset: 0,         // live: wall clock seconds minus the process clock's
    latest: null,      // shown at rest: the last window's metrics, or the run's statistics
    order: [],         // element keys as data flows through them
    more: false,
  };
  const charts = CHARTS.map((def) => ({ def, el: null, plot: null, key: "", idx: null, items: [] }));

  // -- the section --
  const moreBtn = h("button", { class: "toggle", "aria-pressed": "false", title: "Show the charts of every metric measured",
    onclick: () => { state.more = !state.more; moreBtn.setAttribute("aria-pressed", String(state.more)); draw(); } },
    "All metrics", h("span", { class: "n" }));
  const span = h("span", { class: "n" });
  const grid = h("div", { class: "pcharts" });
  const waiting = h("div", { class: "empty", text: "Waiting for the first measurements…" });
  const section = h("section", { class: "perf-sec", "aria-label": "Performance" },
    h("div", { class: "graph-h" }, h("h2", { class: "sec-h" }, "Performance", span),
      h("span", { class: "ghint", text: "Hover a chart for its values · click a name to hide it" }), moreBtn),
    waiting, grid);

  /** The subjects of a metric across the windows, as data flows. */
  function subjectsOf(metric) {
    const seen = new Set();
    for (const w of state.windows) for (const s of Object.keys(w.metrics[metric] || {})) seen.add(s);
    if (!live && run) for (const s of Object.keys(state.latest[metric] || {})) seen.add(s);
    const rank = (s) => { const i = state.order.indexOf(elementOf(s.split("→")[0])); return i < 0 ? Infinity : i; };
    return [...seen].sort((a, b) => rank(a) - rank(b) || (a < b ? -1 : 1));
  }

  function colorOf(subject, i, count) {
    const palette = PALETTE[darkScheme.matches ? "dark" : "light"];
    if (count === 1) return cssVar("--accent");
    const at = state.order.indexOf(elementOf(subject));
    return palette[(at < 0 || subject.includes("→") ? i : at) % palette.length];
  }

  /** What the legend shows when the cursor is not on a chart. */
  const atRest = (metric, subject, stat) => {
    const s = state.latest && state.latest[metric] && state.latest[metric][subject];
    return s ? s[stat] : null;
  };

  function draw() {
    const present = charts.filter((c) => state.windows.some((w) => w.metrics[c.def.metric]));
    const extra = present.filter((c) => c.def.more).length;
    moreBtn.hidden = !extra;
    moreBtn.querySelector(".n").textContent = String(extra);
    waiting.hidden = state.windows.length > 0;
    const shown = present.filter((c) => !c.def.more || state.more);
    for (const c of charts) if (!shown.includes(c) && c.el) { destroy(c); c.resize.disconnect(); c.el.remove(); c.el = null; }
    shown.forEach((c, i) => {
      if (!c.el) c.el = chartCard(c);
      if (grid.children[i] !== c.el) grid.insertBefore(c.el, grid.children[i] || null);
      update(c);
    });
    if (state.windows.length) {
      const first = state.windows[0], last = state.windows[state.windows.length - 1];
      const seconds = last.t - first.t + first.span;
      span.textContent = live
        ? `last ${seconds >= 90 ? `${Math.round(seconds / 60)} min` : `${Math.round(seconds)} s`} · every ${+last.span.toFixed(1)} s`
        : `${state.windows.length} windows`;
    }
  }

  function chartCard(c) {
    const plotEl = h("div", { class: "pplot" });
    c.when = h("span", { class: "pwhen" });
    c.legend = h("div", { class: "plg" });
    c.plotEl = plotEl;
    c.resize = new ResizeObserver(() => { if (c.plot && plotEl.clientWidth) c.plot.setSize({ width: plotEl.clientWidth, height: 140 }); });
    c.resize.observe(plotEl);
    return h("figure", { class: "pchart", "data-metric": c.def.metric },
      h("figcaption", { class: "pch" }, h("span", { class: "pt", text: c.def.title }),
        h("span", { class: "ps", text: c.def.stat === "max" ? "peak" : "mean" }), c.when),
      plotEl, c.legend);
  }

  function destroy(c) {
    if (c.plot) c.plot.destroy();
    c.plot = null; c.key = "";
  }

  function update(c) {
    const { metric, stat } = c.def;
    const subjects = subjectsOf(metric);
    const key = subjects.join("\n") + "\n" + state.order.join(" ") + darkScheme.matches;
    const data = [state.windows.map((w) => w.t), ...subjects.map((s) =>
      state.windows.map((w) => { const x = w.metrics[metric] && w.metrics[metric][s]; return x ? x[stat] : null; }))];
    if (key !== c.key || !c.plot) {
      destroy(c);
      c.key = key;
      c.subjects = subjects;
      c.plot = makePlot(c, subjects, data);
      c.items = subjects.map((s, i) => legendItem(c, s, i));
      c.legend.replaceChildren(...c.items.map((x) => x.el));
    } else {
      c.plot.setData(data);
    }
    legendAt(c, c.idx);
  }

  function makePlot(c, subjects, data) {
    const unit = METRIC_UNITS[c.def.metric];
    const font = `11px ${cssVar("--mono")}`;
    const axis = { stroke: cssVar("--faint"), font, grid: { stroke: cssVar("--line"), width: 1 },
      ticks: { stroke: cssVar("--line"), width: 1, size: 3 }, gap: 4 };
    const fixed = c.def.metric === "queue.fill";
    return new uPlot({
      width: c.plotEl.clientWidth || 400, height: 140,
      legend: { show: false },
      padding: [10, 12, 0, 0],
      cursor: { drag: { x: !live, y: false }, points: { size: 5 }, focus: { prox: 16 }, sync: { key: "perf" } },
      focus: { alpha: 0.25 },
      scales: {
        // one window is a point: a second each side, not uPlot's span of hours
        x: { time: false, range: (u, min, max) => min == null ? [0, 1] : min === max ? [min - 1, max + 1] : [min, max] },
        y: { range: (u, min, max) => {
          if (fixed) return [0, Math.max(1, Math.ceil((max || 0) * 10) / 10)];
          if (min == null || max == null) return [0, 1];
          const [lo, hi] = uPlot.rangeNum(Math.min(0, min), Math.max(max, 0), 0.1, true);
          return [Math.min(0, lo), hi > 0 ? hi : 1];
        } },
      },
      axes: [
        { ...axis, size: 22, space: 64, grid: { show: false }, incrs: SECONDS, values: (u, ticks) => ticks.map(live ? fmtClock : fmtElapsed) },
        { ...axis, space: 28, values: (u, ticks) => tickLabels(ticks, unit),
          size: (u, values) => 12 + Math.max(24, ...(values || []).map((v) => textWidth(v, "11px"))) },
      ],
      series: [{}, ...subjects.map((s, i) => ({ label: s, stroke: colorOf(s, i, subjects.length), width: 1.5,
        points: { show: false }, spanGaps: false }))],
      hooks: {
        setCursor: [(u) => { c.idx = u.cursor.idx; legendAt(c, c.idx); }],
        setSeries: [(u, i, opts) => { if (opts && "focus" in opts) c.items.forEach((x, j) => x.el.classList.toggle("on", opts.focus && i === j + 1)); }],
      },
    }, data, c.plotEl);
  }

  function legendItem(c, subject, i) {
    const value = h("span", { class: "plv" });
    const el = h("button", { class: "pli", type: "button", title: `${subject}: click to hide or show` },
      h("i", { style: `background:${colorOf(subject, i, c.subjects.length)}` }), h("span", { class: "pls", text: subject }), value);
    el.addEventListener("click", () => {
      const show = !c.plot.series[i + 1].show;
      c.plot.setSeries(i + 1, { show });
      el.classList.toggle("off", !show);
    });
    el.addEventListener("pointerenter", () => c.plot.series[i + 1].show && c.plot.setSeries(i + 1, { focus: true }));
    el.addEventListener("pointerleave", () => c.plot.setSeries(null, { focus: false }));
    return { el, value };
  }

  /** The legend's values at the cursor's window, or at rest. */
  function legendAt(c, idx) {
    const unit = METRIC_UNITS[c.def.metric];
    const hovered = idx != null && c.plot;
    c.when.textContent = hovered
      ? (live ? fmtClock : fmtElapsed)(c.plot.data[0][idx])
      : live ? "now" : `over the run`;
    c.items.forEach((x, i) => {
      const v = hovered ? c.plot.data[i + 1][idx] : atRest(c.def.metric, c.subjects[i], c.def.stat);
      x.value.textContent = fmtMeasure(v, unit);
    });
  }

  const onScheme = () => { for (const c of charts) if (c.el) destroy(c); draw(); };
  darkScheme.addEventListener("change", onScheme);

  /** Let go of what outlives the view: the colour scheme listener and the charts' observers. */
  function dispose() {
    darkScheme.removeEventListener("change", onScheme);
    for (const c of charts) if (c.el) { destroy(c); c.resize.disconnect(); c.el.remove(); c.el = null; }
  }

  // -- on the graph --
  let slots = [];
  const measures = {
    get width() { return textWidth("00.0 ms", "11px"); },
    clear() { slots = []; },
    /** Make room on a node for the measures of the elements it shows. */
    node(node, sub, keys) {
      const line = h("span", { class: "gm" });
      const bar = h("span", { class: "gfill", hidden: true }, h("i"));
      sub.append(line);
      node.append(bar);
      slots.push({ keys, line, bar });
    },
    /** Show the latest measures on the nodes. */
    update() {
      const m = state.latest || {};
      const worst = (metric, stat, keys) => {
        let best = null;
        for (const k of keys) {
          const s = m[metric] && m[metric][k];
          if (s && (!best || s[stat] > best.v)) best = { k, v: s[stat], s };
        }
        return best;
      };
      const what = live ? "in the last window" : "over the run";
      let hot = null;
      for (const slot of slots) {
        slot.lat = worst("element.latency", "mean", slot.keys);
        if (slot.lat && (!hot || slot.lat.v > hot.lat.v)) hot = slot;
        const many = slot.keys.length > 1;
        slot.line.textContent = slot.lat ? fmtMeasure(slot.lat.v, "ns") : "";
        slot.line.title = slot.lat
          ? `${many ? `Slowest inside: ${slot.lat.k}, ` : ""}${fmtMeasure(slot.lat.v, "ns")} mean, ${fmtMeasure(slot.lat.s.p95, "ns")} p95 through the element ${what}`
          : "";
        // how full on average: a queue's peak over a run is nearly always its limit
        const fill = worst("queue.fill", "mean", slot.keys);
        slot.bar.hidden = !fill;
        if (fill) {
          const v = Math.max(0, Math.min(1, fill.v));
          slot.bar.firstChild.style.width = `${v * 100}%`;
          slot.bar.classList.toggle("warn", v >= 0.75 && v < 0.95);
          slot.bar.classList.toggle("full", v >= 0.95);
          slot.bar.title = `${many ? `Fullest queue inside: ${fill.k}, ` : `${fill.k}: `}${fmtMeasure(fill.v, "ratio")} full on average, ${fmtMeasure(fill.s.max, "ratio")} at most ${what}`;
        }
      }
      for (const slot of slots) slot.line.classList.toggle("hot", slot === hot && slots.filter((s) => s.lat).length > 1);
    },
    /** What was measured on a link: its source pad's flow, push time and lateness. */
    link(l) {
      const m = state.latest || {}, pad = `${l.source}:${l.source_pad}`;
      const get = (metric) => m[metric] && m[metric][pad];
      const rate = get("pad.bitrate"), bufs = get("pad.buffers"), push = get("pad.push"), late = get("pad.lateness");
      if (!rate && !bufs && !push && !late) return null;
      const rows = [];
      if (rate || bufs) rows.push(["flow", [rate && fmtMeasure(rate.mean, "bit/s"), bufs && fmtMeasure(bufs.mean, "buffers/s")].filter(Boolean).join(" · ")]);
      if (push) rows.push(["push", `${fmtMeasure(push.mean, "ns")} · p95 ${fmtMeasure(push.p95, "ns")}`]);
      if (late) rows.push(["lateness", `${fmtSigned(late.mean, "ns")} · p95 ${fmtSigned(late.p95, "ns")}`]);
      const last = state.windows[state.windows.length - 1];
      return h("div", { class: "gcaps gperf" },
        h("div", { class: "gpm", text: live ? `mean of the last ${last ? +last.span.toFixed(1) : 1} s` : "mean over the run" }),
        h("dl", {}, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])));
    },
  };

  function setGraph(g) {
    const order = flowOrder(g);
    if (order.join(" ") === state.order.join(" ")) return;
    state.order = order;
    draw();
  }

  /** A live window, as `perf.summary` publishes them; null while there is none. */
  function add(w) {
    if (!w) return;
    const end = Number(w.end), start = Number(w.start);
    const last = state.windows[state.windows.length - 1];
    if (last && end === last.end) return;  // the same window again, from a reconnection
    if (last && end < last.end) state.windows = [];  // a new process: its clock started again
    // on the wall clock, to read beside the notices; spaced as the process clock has them
    if (!state.windows.length) state.offset = Date.now() / 1000 - end / 1e9;
    const t = end / 1e9 + state.offset;
    state.windows.push({ t, end, span: (end - start) / 1e9, metrics: w.metrics });
    while (state.windows.length && state.windows[0].t < t - LIVE_SPAN) state.windows.shift();
    state.latest = w.metrics;
    draw();
    measures.update();
    refreshTip();
  }

  if (run) {
    const t0 = Number(run.start);
    state.windows = run.windows.map((w) => ({ t: (Number(w.end) - t0) / 1e9, end: Number(w.end),
      span: (Number(w.end) - Number(w.start)) / 1e9, metrics: w.metrics }));
    state.latest = run.stats;
  }
  return { section, measures, add, setGraph, draw, dispose };
}
