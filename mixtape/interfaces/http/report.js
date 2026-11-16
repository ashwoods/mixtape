"use strict";
// The page of a run's report.html (mixtape/perf/report.py inlines it after graph.js, uPlot
// and perf.js): its pipeline with the whole run's numbers, its windows as charts, and the
// statistics of summary.json as a table.
(() => {
  const { summary, windows } = JSON.parse(document.getElementById("run").textContent);
  const stats = {};
  for (const [metric, m] of Object.entries(summary.metrics || {})) stats[metric] = m.subjects;
  const seconds = (Number(summary.end_ns) - Number(summary.start_ns)) / 1e9;

  // -- head: what ran, how long, how it was measured --
  const facts = [
    ["ran for", seconds < 60 ? fmtMeasure(seconds * 1e9, "ns") : fmtElapsed(seconds)],
    ["windows", `${summary.windows} of ${+Number(summary.interval).toFixed(2)} s`],
    ["tracers", (summary.tracers || []).map((t) => t.replace(/\(.*\)$/, "")).join(", ") || "—"],
  ];
  document.getElementById("head").append(
    h("div", { class: "brand" }, h("span", { class: "app", text: "mixtape" }), h("span", { class: "name", text: "Perf report" })),
    summary.pipeline ? h("p", { class: "desc", text: summary.pipeline }) : null,
    h("ul", { class: "facts" }, facts.map(([k, v]) => h("li", {}, `${k} `, h("b", { text: v })))));

  // -- the graph and the charts --
  const view = createPerf({ live: false, run: { start: summary.start_ns, windows, stats } });
  const main = document.getElementById("main");
  main.replaceChildren(graphSection(), view.section);
  graph.measures = view.measures;
  if (summary.graph) {
    graph.data = summary.graph;
    view.setGraph(summary.graph);
    renderGraph();
    const hint = document.querySelector(".graph-sec .ghint");
    if (hint.textContent) hint.textContent += " · numbers are means over the run";
  }
  view.draw();
  view.section.querySelector(".ghint").textContent = "Drag to zoom · double-click to reset · click a name to hide it";

  // -- the table: every metric and subject, as data flows --
  const order = summary.graph ? flowOrder(summary.graph) : [];
  const rank = (s) => { const i = order.indexOf(s.split(/[:→]/)[0]); return i < 0 ? Infinity : i; };
  const known = CHARTS.map((c) => c.metric);
  const metrics = Object.keys(summary.metrics || {}).sort((a, b) =>
    (known.indexOf(a) + 1 || 99) - (known.indexOf(b) + 1 || 99) || (a < b ? -1 : 1));
  const cols = ["n", "mean", "p50", "p95", "p99", "max"];
  const body = h("tbody");
  for (const metric of metrics) {
    const m = summary.metrics[metric], unit = m.unit;
    const title = (CHARTS.find((c) => c.metric === metric) || { title: metric }).title;
    body.append(h("tr", { class: "mh" }, h("td", { colspan: cols.length + 1 },
      h("b", { text: title }), h("code", { text: `${metric} · ${unit}` }), h("span", { text: m.description || "" }))));
    const subjects = Object.keys(m.subjects).sort((a, b) => rank(a) - rank(b) || (a < b ? -1 : 1));
    for (const s of subjects) {
      const st = m.subjects[s];
      body.append(h("tr", {}, h("td", { class: "subject", text: s, title: s }),
        ...cols.map((c) => c === "n"
          ? h("td", { class: "n", text: String(st.n) })
          : h("td", { text: (metric === "pad.lateness" ? fmtSigned : fmtMeasure)(st[c], unit) }))));
    }
  }
  main.append(h("section", { "aria-label": "Statistics" },
    h("h2", { class: "sec-h" }, "Whole run", h("span", { class: "n", text: `${metrics.length} metrics · from summary.json` })),
    metrics.length
      ? h("div", { class: "stats-wrap" }, h("table", { class: "stats" },
          h("thead", {}, h("tr", {}, h("th", { text: "subject" }), ...cols.map((c) => h("th", { text: c })))), body))
      : h("div", { class: "empty", text: "The run measured nothing." })));
})();
