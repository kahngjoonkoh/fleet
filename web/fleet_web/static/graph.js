// Brain graphs: every <svg class="graph" data-graph="ID"> draws the nodes and edges in the JSON
// script #ID, with a small force layout. Drag the background to pan, the wheel or two fingers to
// zoom, a dot to move it; tap a dot to open its note. Hovering a dot (or tapping it once on touch
// screens) lights up its neighbours. The legend above hides and shows kinds.
"use strict";

const SVG = "http://www.w3.org/2000/svg";
// Kinds with a colour in style.css; any other kind gets one from its name.
const KNOWN = new Set(["project", "decision", "requirement", "tech", "component", "problem", "convention", "todo",
  "milestone", "question", "machine", "service", "concept", "person", "session"]);
const kindColor = (kind) => {
  let h = 0;
  for (const c of kind) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 45% 55%)`;
};
const el = (name, attrs = {}) => {
  const e = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};

for (const svg of document.querySelectorAll("svg.graph")) draw(svg);

function draw(svg) {
  const data = JSON.parse(document.getElementById(svg.dataset.graph).textContent);
  const nodes = data.nodes.map((n, i) => ({ ...n, i, x: Math.cos(i) * (40 + i * 3), y: Math.sin(i) * (40 + i * 3), vx: 0, vy: 0, degree: 0 }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges = data.edges.map((e) => ({ ...e, s: byId.get(e.source), t: byId.get(e.target) })).filter((e) => e.s && e.t);
  for (const e of edges) { e.s.degree++; e.t.degree++; }
  const neighbours = new Map(nodes.map((n) => [n, new Set([n])]));
  for (const e of edges) { neighbours.get(e.s).add(e.t); neighbours.get(e.t).add(e.s); }
  // Sessions link to everything they touched, which knots the graph, so they start hidden.
  const hidden = new Set(nodes.length > 1 && nodes.some((n) => n.kind !== "session") ? ["session"] : []);

  const view = el("g");
  const edgeLayer = el("g", { class: "edges" }), nodeLayer = el("g", { class: "nodes" });
  view.append(edgeLayer, nodeLayer);
  svg.append(view);
  for (const e of edges) { e.line = el("line"); edgeLayer.append(e.line); }
  for (const n of nodes) {
    n.r = 4 + Math.min(10, Math.sqrt(n.degree) * 2.2) + (n.kind === "project" ? 4 : 0);
    n.g = el("g", { class: "node", "data-kind": n.kind, tabindex: "0", role: "link" });
    const circle = el("circle", { r: n.r, "data-kind": n.kind });
    if (!KNOWN.has(n.kind)) circle.style.setProperty("--kc", kindColor(n.kind));
    const title = el("title");
    title.textContent = `${n.title} (${n.kind}${n.status ? ", " + n.status : ""})`;
    n.label = el("text", { x: n.r + 3, y: 4 });
    n.label.textContent = n.title.length > 34 ? n.title.slice(0, 33) + "…" : n.title;
    n.g.append(circle, title, n.label);
    if (n.degree >= 3 || n.kind === "project" || nodes.length < 25) n.g.classList.add("labelled");
    if (n.outside) n.g.classList.add("outside");  // another project's note that this one links to
    nodeLayer.append(n.g);
  }

  // --- legend ---------------------------------------------------------------------
  const legend = document.querySelector(`[data-legend="${svg.dataset.graph}"]`);
  if (legend) {
    const counts = new Map();
    for (const n of nodes) counts.set(n.kind, (counts.get(n.kind) || 0) + 1);
    for (const [kind, count] of [...counts].sort((a, b) => b[1] - a[1])) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "legend-item";
      const dot = document.createElement("span");
      dot.className = "kind-dot";
      dot.dataset.kind = kind;
      if (!KNOWN.has(kind)) dot.style.setProperty("--kc", kindColor(kind));
      b.append(dot, `${kind} ${count}`);
      b.classList.toggle("off", hidden.has(kind));
      b.addEventListener("click", () => {
        hidden.has(kind) ? hidden.delete(kind) : hidden.add(kind);
        b.classList.toggle("off", hidden.has(kind));
        for (const n of nodes) n.g.classList.toggle("gone", hidden.has(n.kind));
        for (const e of edges) e.line.classList.toggle("gone", hidden.has(e.s.kind) || hidden.has(e.t.kind));
        heat(0.5);
      });
      legend.append(b);
    }
  }

  // --- layout -----------------------------------------------------------------------
  let alpha = 1, frame = null;
  const LINK = 95, CHARGE = 2600;
  function tick() {
    const live = nodes.filter((n) => !hidden.has(n.kind));
    for (let a = 0; a < live.length; a++) {
      const p = live[a];
      for (let b = a + 1; b < live.length; b++) {
        const q = live[b];
        let dx = q.x - p.x, dy = q.y - p.y, d2 = dx * dx + dy * dy || 0.01;
        if (d2 > 250000) continue;
        const f = (CHARGE * alpha) / d2, d = Math.sqrt(d2);
        dx = (dx / d) * f; dy = (dy / d) * f;
        p.vx -= dx; p.vy -= dy; q.vx += dx; q.vy += dy;
      }
    }
    for (const e of edges) {
      if (hidden.has(e.s.kind) || hidden.has(e.t.kind)) continue;
      const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = ((d - LINK) / d) * 0.06 * alpha;
      e.s.vx += dx * f; e.s.vy += dy * f; e.t.vx -= dx * f; e.t.vy -= dy * f;
    }
    for (const n of live) {
      n.vx -= n.x * 0.01 * alpha; n.vy -= n.y * 0.01 * alpha;  // gently toward the middle
      if (n === dragging) { n.vx = n.vy = 0; continue; }
      n.x += n.vx; n.y += n.vy;
      n.vx *= 0.6; n.vy *= 0.6;
    }
    alpha *= 0.985;
  }
  function paint() {
    for (const e of edges) {
      e.line.setAttribute("x1", e.s.x); e.line.setAttribute("y1", e.s.y);
      e.line.setAttribute("x2", e.t.x); e.line.setAttribute("y2", e.t.y);
    }
    for (const n of nodes) n.g.setAttribute("transform", `translate(${n.x},${n.y})`);
    view.setAttribute("transform", `translate(${pan.x},${pan.y}) scale(${pan.k})`);
  }
  function loop() {
    tick(); tick();
    paint();
    frame = alpha > 0.01 ? requestAnimationFrame(loop) : null;
  }
  function heat(a) {
    alpha = Math.max(alpha, a);
    if (!frame) frame = requestAnimationFrame(loop);
  }

  // --- view: fit, pan, zoom -------------------------------------------------------------
  const pan = { x: 0, y: 0, k: 1 };
  function fit() {
    const live = nodes.filter((n) => !hidden.has(n.kind));
    if (!live.length) return;
    const { width, height } = svg.getBoundingClientRect();
    const xs = live.map((n) => n.x), ys = live.map((n) => n.y);
    const [x0, x1, y0, y1] = [Math.min(...xs) - 20, Math.max(...xs) + 140, Math.min(...ys) - 20, Math.max(...ys) + 20];  // labels sit to the right
    pan.k = Math.min(1.25, width / (x1 - x0), height / (y1 - y0));
    pan.x = width / 2 - ((x0 + x1) / 2) * pan.k;
    pan.y = height / 2 - ((y0 + y1) / 2) * pan.k;
    paint();
  }
  const toGraph = (cx, cy) => {
    const r = svg.getBoundingClientRect();
    return { x: (cx - r.left - pan.x) / pan.k, y: (cy - r.top - pan.y) / pan.k };
  };
  function zoom(factor, cx, cy) {
    const r = svg.getBoundingClientRect(), k = Math.min(4, Math.max(0.2, pan.k * factor));
    const px = cx - r.left, py = cy - r.top;
    pan.x = px - ((px - pan.x) * k) / pan.k;
    pan.y = py - ((py - pan.y) * k) / pan.k;
    pan.k = k;
    paint();
  }
  svg.addEventListener("wheel", (e) => { e.preventDefault(); zoom(Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY); }, { passive: false });

  const pointers = new Map();
  let dragging = null, moved = 0, panStart = null, pinch = null;
  svg.addEventListener("pointerdown", (e) => {
    svg.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved = 0;
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: pan.k };
      dragging = null; panStart = null;
      return;
    }
    const g = e.target.closest(".node");
    dragging = g ? nodes.find((n) => n.g === g) : null;
    panStart = dragging ? null : { x: e.clientX - pan.x, y: e.clientY - pan.y };
  });
  svg.addEventListener("pointermove", (e) => {
    if (!pointers.has(e.pointerId)) return;
    const last = pointers.get(e.pointerId);
    moved += Math.abs(e.clientX - last.x) + Math.abs(e.clientY - last.y);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      zoom((pinch.k * Math.hypot(a.x - b.x, a.y - b.y)) / pinch.d / pan.k, (a.x + b.x) / 2, (a.y + b.y) / 2);
    } else if (dragging) {
      const p = toGraph(e.clientX, e.clientY);
      dragging.x = p.x; dragging.y = p.y;
      heat(0.3);
    } else if (panStart) {
      pan.x = e.clientX - panStart.x; pan.y = e.clientY - panStart.y;
      paint();
    }
  });
  const release = (e) => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    const tapped = dragging && moved < 6;
    if (tapped) {
      // On touch screens the first tap shows a dot's neighbours, the second opens it.
      if (e.pointerType !== "mouse" && focused !== dragging) focus(dragging);
      else if (dragging.url) location.href = dragging.url;
    } else if (!dragging && moved < 6 && e.pointerType !== "mouse") {
      focus(null);
    }
    dragging = null; panStart = null;
  };
  svg.addEventListener("pointerup", release);
  svg.addEventListener("pointercancel", release);

  let focused = null;
  function focus(n) {
    focused = n;
    svg.classList.toggle("focusing", !!n);
    const near = n ? neighbours.get(n) : null;
    for (const m of nodes) m.g.classList.toggle("near", !!near && near.has(m));
    for (const e of edges) e.line.classList.toggle("near", !!n && (e.s === n || e.t === n));
  }
  for (const n of nodes) {
    n.g.addEventListener("pointerenter", (e) => { if (e.pointerType === "mouse") focus(n); });
    n.g.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") focus(null); });
    n.g.addEventListener("keydown", (e) => { if (e.key === "Enter" && n.url) location.href = n.url; });
  }
  for (const n of nodes) n.g.classList.toggle("gone", hidden.has(n.kind));
  for (const e of edges) e.line.classList.toggle("gone", hidden.has(e.s.kind) || hidden.has(e.t.kind));
  // Settle the layout before the first paint, so the graph appears in place rather than unfolding;
  // after that it only moves when you drag or hide something.
  const settle = nodes.length < 150 ? 300 : nodes.length < 400 ? 120 : 40;
  for (let i = 0; i < settle && alpha > 0.01; i++) tick();
  fit();
  new ResizeObserver(() => fit()).observe(svg);
}
