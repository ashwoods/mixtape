"use strict";
// Shared by the control page and the perf report (which inlines it): the DOM helper and the
// pipeline graph's layout, renderer and link tooltip. The page fetches the graph and wires the
// hooks of `graph`; the report renders the graph a run saved.

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

// the SVG namespace, from the parser rather than spelled out: the report names no URL at all
const SVG_NS = (() => { const d = document.createElement("div"); d.innerHTML = "<svg></svg>"; return d.firstChild.namespaceURI; })();

// ---- pipeline graph ----------------------------------------------------------------------
//
// Left to right, laid out per bin: a bin's children are blocks in columns by the longest path
// of the links between them, a bin being one block; blocks in a column are stacked and aligned
// on the line their flow runs through. Bins without an exposed element inside start folded.

const graph = {
  data: null, all: false, open: new Map(), pinned: null, tip: null,
  frame: null,     // (graph data) → the pipeline as a node framing the rest, or null
  reveal: null,    // (key) → show an exposed element's properties; null: nodes are no links
  measures: null,  // what perf measured, on the nodes and in the link tooltip (perf.js), or null
  rendered: null,  // () → after each render
};
const GL = { h: 34, gapX: 36, gapY: 10, pad: 10, head: 26, measured: 6 };

let measureCtx = null;
function textWidth(text, font) {
  measureCtx = measureCtx || document.createElement("canvas").getContext("2d");
  measureCtx.font = `${font} ${getComputedStyle(document.documentElement).getPropertyValue("--mono")}`;
  return Math.ceil(measureCtx.measureText(text).width);
}

/** The factory, unless the name already says it (videotestsrc0) or it is a plain bin. */
const factoryLabel = (n) =>
  (n.factory === "bin" || n.name.replace(/\d+$/, "") === n.factory.replace(/-/g, "") ? "" : n.factory);

/** Caps as their media type and fields, without the field types: one entry per structure. */
function parseCaps(caps) {
  const split = (s, sep) => {
    const out = [];
    let depth = 0, quote = false, start = 0;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c === '"' && s[i - 1] !== "\\") quote = !quote;
      else if (!quote && "{[<(".includes(c)) depth++;
      else if (!quote && "}]>)".includes(c)) depth--;
      else if (!quote && !depth && c === sep) { out.push(s.slice(start, i).trim()); start = i + 1; }
    }
    out.push(s.slice(start).trim());
    return out.filter(Boolean);
  };
  return split(caps, ";").map((structure) => {
    const [type, ...fields] = split(structure, ",");
    return { type, fields: fields.map((f) => {
      const eq = f.indexOf("=");
      return [f.slice(0, eq).trim(), f.slice(eq + 1).trim().replace(/^\([\w ]+\)/, "")];
    }) };
  });
}

/** The elements that are not bins in the order data flows through them: by the longest path
 * of links from a source, then by key. */
function flowOrder(g) {
  const keys = Object.keys(g.nodes).filter((k) => !g.nodes[k].bin);
  const succ = new Map(keys.map((k) => [k, []])), indeg = new Map(keys.map((k) => [k, 0]));
  for (const l of g.links) {
    if (!succ.has(l.source) || !succ.has(l.sink) || l.source === l.sink) continue;
    succ.get(l.source).push(l.sink);
    indeg.set(l.sink, indeg.get(l.sink) + 1);
  }
  const depth = new Map(), ready = keys.filter((k) => !indeg.get(k));
  for (const k of ready) depth.set(k, 0);
  while (ready.length) {
    const k = ready.shift();
    for (const z of succ.get(k)) {
      depth.set(z, Math.max(depth.get(z) || 0, depth.get(k) + 1));
      indeg.set(z, indeg.get(z) - 1);
      if (!indeg.get(z)) ready.push(z);
    }
  }
  const at = (k) => (depth.has(k) ? depth.get(k) : Infinity);  // in a cycle: last
  return keys.sort((a, b) => at(a) - at(b) || (a < b ? -1 : a > b ? 1 : 0));
}

function renderGraph() {
  const box = document.getElementById("graph");
  if (!box) return;
  const sec = box.closest(".graph-sec");
  const g = graph.data;
  sec.hidden = !g || !Object.keys(g.nodes).length;
  hideTip();
  if (sec.hidden) return;
  if (graph.measures) graph.measures.clear();

  const nodes = g.nodes;
  const parent = (k) => (nodes[k].parent != null && nodes[k].parent in nodes ? nodes[k].parent : null);
  const kids = new Map();
  for (const k of Object.keys(nodes)) {
    if (!kids.has(parent(k))) kids.set(parent(k), []);
    kids.get(parent(k)).push(k);
  }
  // per bin: how many elements it holds, whether one of them is exposed
  const inside = new Map();
  const count = (k) => {
    let n = 0, exposed = false;
    for (const c of kids.get(k) || []) {
      const [cn, ce] = count(c);
      n += 1 + cn; exposed = exposed || ce || nodes[c].exposed;
    }
    inside.set(k, [n, exposed]);
    return [n, exposed];
  };
  count(null);
  const within = (k) => [k, ...(kids.get(k) || []).flatMap(within)];
  const foldable = Object.keys(nodes).filter((k) => inside.get(k)[0] > 0);
  const isOpen = (k) => (graph.open.has(k) ? graph.open.get(k) : graph.all || inside.get(k)[1]);
  /** The block that shows element k: itself, or the outermost folded bin around it. */
  const shown = (k) => {
    let v = k;
    for (let p = parent(k); p != null; p = parent(p)) if (!isOpen(p)) v = p;
    return v;
  };
  /** The child of bin c (null: the pipeline) that holds block k, if any. */
  const childIn = (c, k) => {
    let x = k;
    while (x != null && parent(x) !== c) x = parent(x);
    return x;
  };

  const edges = new Map();
  for (const l of g.links) {
    const s = shown(l.source), t = shown(l.sink);
    if (s === t) continue;
    const id = `${s}\n${t}`;
    if (!edges.has(id)) edges.set(id, { s, t, links: [] });
    edges.get(id).links.push(l);
  }

  const block = (k) => {
    const ch = kids.get(k) || [];
    const b = { key: k, n: nodes[k], open: ch.length > 0 && isOpen(k), count: inside.get(k)[0] };
    if (b.open) b.children = ch.map(block);
    return b;
  };
  // the pipeline itself is no node: when the model exposes it (playbin name=pb), it frames the rest
  const pipe = graph.frame ? graph.frame(g) : null;
  const root = { key: null, open: true, root: true, children: (kids.get(null) || []).map(block), n: pipe };
  const nodeH = GL.h + (graph.measures ? GL.measured : 0);

  const measure = (b) => {
    const n = b.n, fl = n ? factoryLabel(n) : "";
    if (!b.open) {
      const second = textWidth(fl, "11px") + (graph.measures ? (fl ? 10 : 0) + graph.measures.width : 0);
      b.w = 22 + Math.max(textWidth(n.name, "600 12px"), second);
      if (b.count) b.w += 18 + textWidth(`+${b.count}`, "11px") + 8;
      b.h = nodeH; b.anchor = nodeH / 2;
      return;
    }
    const ch = b.children;
    ch.forEach(measure);
    const index = new Map(ch.map((c, i) => [c.key, i]));
    const succ = ch.map(() => []), pred = ch.map(() => []), linked = ch.map(() => false);
    for (const e of edges.values()) {
      const a = index.get(childIn(b.key, e.s)), z = index.get(childIn(b.key, e.t));
      if (a !== undefined) linked[a] = true;
      if (z !== undefined) linked[z] = true;
      if (a === undefined || z === undefined || a === z) continue;
      succ[a].push(z); pred[z].push(a);
    }
    // columns by the longest path from a source, a cycle broken at its first block
    const layer = ch.map(() => 0), done = ch.map(() => false), indeg = pred.map((p) => p.length);
    const ready = ch.map((_, i) => i).filter((i) => !indeg[i]);
    for (let left = ch.length; left > 0;) {
      const i = ready.length ? ready.shift() : done.indexOf(false);
      if (done[i]) continue;
      done[i] = true; left--;
      layer[i] = Math.max(0, ...pred[i].filter((p) => done[p] && p !== i).map((p) => layer[p] + 1));
      for (const z of succ[i]) if (--indeg[z] === 0 && !done[z]) ready.push(z);
    }
    const cols = [];
    ch.forEach((_, i) => (cols[layer[i]] = cols[layer[i]] || []).push(i));
    const row = new Map();
    // under the blocks of the same predecessors; blocks without links (playsink's spare identity) last
    const bary = (i) => {
      if (!linked[i]) return Number.MAX_VALUE;
      const ps = pred[i].filter((p) => row.has(p));
      return ps.length ? ps.reduce((s, p) => s + row.get(p), 0) / ps.length : ch.length;
    };
    // each block level with what feeds it, pushed down past the one above, the column then
    // centred on where its blocks want to be: a chain runs straight, a tee's branches fan out
    const mid = (i) => ch[i].y + ch[i].anchor;
    let x = 0;
    for (const col of cols.filter(Boolean)) {
      col.sort((p, q) => bary(p) - bary(q) || p - q);
      col.forEach((i, r) => row.set(i, r));
      const want = col.map((i) => {
        const ps = pred[i].filter((p) => ch[p].y !== undefined);
        return ps.length ? ps.reduce((s, p) => s + mid(p), 0) / ps.length - ch[i].anchor : null;
      });
      let next = null;
      const tops = col.map((i, r) => {
        const t = Math.max(want[r] ?? next ?? 0, next ?? -Infinity);
        next = t + ch[i].h + GL.gapY;
        return t;
      });
      const wanted = want.flatMap((w, r) => (w === null ? [] : [w - tops[r]]));
      const shift = wanted.length ? Math.min(0, wanted.reduce((s, d) => s + d, 0) / wanted.length) : 0;
      const w = Math.max(...col.map((i) => ch[i].w));
      col.forEach((i, r) => { ch[i].x = x + (w - ch[i].w) / 2; ch[i].y = tops[r] + shift; });
      x += w + GL.gapX;
    }
    const minY = Math.min(...ch.map((c) => c.y)), maxY = Math.max(...ch.map((c) => c.y + c.h));
    for (const c of ch) c.y -= minY;
    // a bin is entered at its first linked block
    const entry = cols[0].find((i) => linked[i]) ?? cols[0][0];
    const framed = !b.root || b.n;
    const top = framed ? GL.head : 0, pad = framed ? GL.pad : 0;
    const head = n ? 30 + textWidth(n.name, "600 11.5px") + (fl ? 8 + textWidth(fl, "11px") : 0) : 0;
    b.inner = { x: pad, y: top };
    b.w = Math.max(x - GL.gapX + 2 * pad, head);
    b.h = top + maxY - minY + pad;
    b.anchor = top + mid(entry);
  };
  measure(root);

  const canvas = h("div", { class: "gcanvas", style: `width:${root.w}px;height:${root.h}px` });
  const lines = document.createElementNS(SVG_NS, "svg");
  lines.setAttribute("class", "gedges");
  lines.setAttribute("width", root.w); lines.setAttribute("height", root.h);
  const frames = [], blocks = [], rects = new Map();
  const place = (b, x, y, depth) => {
    if (b.open) {
      if (!b.root || b.n) frames.push(binFrame(b, x, y, depth));
      for (const c of b.children) place(c, x + b.inner.x + c.x, y + b.inner.y + c.y, depth + 1);
    } else {
      rects.set(b.key, { x, y, w: b.w, h: b.h });
      blocks.push(graphNode(b, x, y));
    }
  };
  place(root, 0, 0, 0);
  for (const e of edges.values()) lines.append(graphEdge(e, rects.get(e.s), rects.get(e.t)));
  canvas.append(...frames, lines, ...blocks);
  box.replaceChildren(canvas);
  graphEdges();
  if (graph.measures) graph.measures.update();

  const total = Object.keys(nodes).length;
  const visible = rects.size + frames.length - (root.n ? 1 : 0);
  sec.querySelector(".gcount").textContent = visible < total ? `${visible} of ${total} elements` : `${total} element${total === 1 ? "" : "s"}`;
  const allBtn = sec.querySelector(".gall");
  allBtn.hidden = !foldable.some((k) => !inside.get(k)[1]);
  allBtn.setAttribute("aria-pressed", String(foldable.every(isOpen)));
  const clickable = !!graph.reveal && (!!pipe || Object.values(nodes).some((n) => n.exposed));
  const capsHint = g.links.length ? `hover a link for its caps${graph.measures ? " and flow" : ""}` : "";
  sec.querySelector(".ghint").textContent = clickable
    ? `Click an element for its properties${capsHint && ` · ${capsHint}`}`
    : capsHint.replace("h", "H");
  if (graph.rendered) graph.rendered();

  function binFrame(b, x, y, depth) {
    const n = b.n, fl = factoryLabel(n);
    const fold = b.root ? null : h("button", { class: "gfold", "aria-expanded": "true", title: `Fold ${n.name}`,
      "aria-label": `Fold ${n.name}`, onclick: () => { graph.open.set(b.key, false); renderGraph(); } });
    return h("div", { class: "gbin" + (depth % 2 ? " odd" : ""), "data-key": b.key || n.name,
      style: `left:${x}px;top:${y}px;width:${b.w}px;height:${b.h}px` },
      h("div", { class: "gbh" }, fold,
        n.exposed && graph.reveal ? h("button", { class: "gname", text: n.name, title: `Go to ${b.key || n.name}`,
          onclick: () => graph.reveal(b.key || n.name) }) : h("span", { class: "gname", text: n.name }),
        fl ? h("span", { class: "gf", text: fl }) : null));
  }

  function graphNode(b, x, y) {
    const n = b.n, fl = factoryLabel(n);
    const sub = graph.measures ? h("span", { class: "gsub" }, fl ? h("span", { class: "gf", text: fl }) : null)
      : fl ? h("span", { class: "gf", text: fl }) : null;
    const label = h("span", { class: "glabel" }, h("span", { class: "gk", text: n.name }), sub);
    const style = `left:${x}px;top:${y}px;width:${b.w}px;height:${b.h}px`;
    const unfold = () => { graph.open.set(b.key, true); renderGraph(); };
    const more = b.count ? h("span", { class: "gmore", text: `+${b.count}` }) : null;
    const title = `${b.key} · ${n.factory}${b.count ? ` · ${b.count} elements inside` : ""}`;
    let node;
    if (n.exposed && graph.reveal) {
      const go = h("button", { class: "ggo", title: `${title}\nGo to its properties`, onclick: () => graph.reveal(b.key) }, label, more);
      node = h("div", { class: "gn exposed" + (b.count ? " folded" : ""), "data-key": b.key, style },
        b.count ? h("button", { class: "gfold", "aria-expanded": "false", "aria-label": `Unfold ${n.name}`, title: `Unfold ${n.name}`, onclick: unfold }) : null, go);
    } else if (b.count) {
      node = h("div", { class: "gn folded", "data-key": b.key, style },
        h("button", { class: "ggo", "aria-expanded": "false", title: `${title}\nUnfold`, onclick: unfold },
          h("span", { class: "gfold", "aria-hidden": "true" }), label, more));
    } else {
      node = h("div", { class: "gn" + (n.exposed ? " exposed" : ""), "data-key": b.key, title, style }, label);
    }
    if (graph.measures) graph.measures.node(node, sub, b.count ? within(b.key) : [b.key]);
    return node;
  }
}

function graphEdge(e, a, z) {
  const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = z.x, y2 = z.y + z.h / 2;
  const bend = Math.max(18, Math.abs(x2 - x1) / 2);
  const d = `M${x1} ${y1} C${x1 + bend} ${y1} ${x2 - 5 - bend} ${y2} ${x2 - 5} ${y2}`;
  const group = document.createElementNS(SVG_NS, "g");
  const negotiated = e.links.every((l) => l.caps);
  group.setAttribute("class", "gedge" + (negotiated ? "" : " pending"));
  for (const [cls, attrs] of [["hit", { d }], ["ln", { d }], ["ah", { d: `M${x2 - 6} ${y2 - 3.5}L${x2} ${y2}L${x2 - 6} ${y2 + 3.5}z` }]]) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("class", cls);
    for (const [k, v] of Object.entries(attrs)) p.setAttribute(k, v);
    group.append(p);
  }
  group.addEventListener("pointerenter", (ev) => { if (!graph.pinned) showTip(e, ev); });
  group.addEventListener("pointermove", (ev) => { if (!graph.pinned) moveTip(ev); });
  group.addEventListener("pointerleave", () => { if (!graph.pinned) hideTip(); });
  group.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const again = graph.pinned === group;
    hideTip();
    if (again) return;
    graph.pinned = group;
    group.classList.add("on");
    showTip(e, ev);
  });
  return group;
}

function showTip(e, ev) {
  graph.tip = e;
  fillTip();
  document.getElementById("gtip").hidden = false;
  moveTip(ev);
}

/** The tooltip's content for the link it shows: its pads, what was measured on it, its caps. */
function fillTip() {
  const tip = document.getElementById("gtip");
  const pad = (key, name) => h("span", {}, h("b", { text: key }), h("span", { class: "gp", text: `.${name}` }));
  tip.replaceChildren(...graph.tip.links.map((l) => h("div", { class: "gt" },
    h("div", { class: "gth" }, pad(l.source, l.source_pad), h("span", { class: "gp", text: " → " }), pad(l.sink, l.sink_pad)),
    graph.measures ? graph.measures.link(l) : null,
    l.caps ? parseCaps(l.caps).map((s) => h("div", { class: "gcaps" }, h("div", { class: "gmt", text: s.type }),
      s.fields.length ? h("dl", {}, s.fields.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])) : null))
      : h("div", { class: "gnone", text: "Not negotiated yet: caps appear once the pipeline prerolls." }))));
}

/** Show what was measured since the tooltip opened, in place. */
function refreshTip() {
  const tip = document.getElementById("gtip");
  if (tip && !tip.hidden && graph.tip) fillTip();
}

function moveTip(ev) {
  const tip = document.getElementById("gtip");
  const host = tip.parentElement.getBoundingClientRect();
  const left = Math.min(ev.clientX - host.left + 14, host.width - tip.offsetWidth);
  tip.style.left = `${Math.max(0, left)}px`;
  tip.style.top = `${ev.clientY - host.top + 16}px`;
}

function hideTip() {
  const tip = document.getElementById("gtip");
  if (tip) tip.hidden = true;
  if (graph.pinned) graph.pinned.classList.remove("on");
  graph.pinned = null;
  graph.tip = null;
}

/** Fade the edges the graph scrolls past, so a wide one shows there is more. */
function graphEdges() {
  const box = document.getElementById("graph");
  if (!box) return;
  box.classList.toggle("more-left", box.scrollLeft > 1);
  box.classList.toggle("more-right", box.scrollLeft + box.clientWidth < box.scrollWidth - 1);
}
new ResizeObserver(() => graphEdges()).observe(document.documentElement);

function graphSection() {
  const all = h("button", { class: "toggle gall", "aria-pressed": "false", title: "Unfold every bin",
    onclick: () => { graph.all = all.getAttribute("aria-pressed") !== "true"; graph.open.clear(); renderGraph(); } }, "All elements");
  return h("section", { class: "graph-sec", "aria-label": "Pipeline", hidden: true },
    h("div", { class: "graph-h" },
      h("h2", { class: "sec-h" }, "Pipeline", h("span", { class: "n gcount" })),
      h("span", { class: "ghint" }), all),
    h("div", { class: "graph", id: "graph", onscroll: () => { hideTip(); graphEdges(); } }),
    h("div", { class: "gtip", id: "gtip", role: "tooltip", hidden: true }));
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && graph.pinned) hideTip(); });
document.addEventListener("click", () => { if (graph.pinned) hideTip(); });
