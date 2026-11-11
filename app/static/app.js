"use strict";
// All server values are rendered with textContent / DOM APIs only (never raw HTML).
const $ = (id) => document.getElementById(id);
const state = { examples: [], answered: new Set() };

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null) e.append(kid);
  return e;
}

const nf = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
function fmtCell(col, v) {
  if (v === null || v === undefined) return "-";
  if (typeof v === "number") {
    if (/pct|percent|rate/i.test(col)) return nf.format(v);
    return Number.isInteger(v) ? v.toLocaleString("en-US") : nf.format(v);
  }
  return String(v);
}

function sessionKey() { return "sid:" + $("user-select").value; }
function getSid() { return sessionStorage.getItem(sessionKey()) || undefined; }

async function loadInit() {
  const [users, schema, cfg] = await Promise.all(["/api/users", "/api/schema", "/api/config"].map((u) => fetch(u).then((r) => r.json())));
  const sel = $("user-select");
  for (const u of users) {
    const scope = u.fund_scope === "all" ? "all funds" : u.fund_scope.join(", ");
    sel.append(el("option", { value: u.user_id, text: `${u.name} (${u.role}; ${scope})` }));
  }
  state.examples = schema.examples;
  const chips = $("chips");
  for (const ex of schema.examples) {
    const b = el("button", { type: "button", class: "chip", text: `${ex.id}: ${ex.label}`, title: ex.question });
    if (ex.id === "Q4F") { b.disabled = true; b.title = "Available after you ask Q4 in this conversation"; b.id = "chip-Q4F"; }
    b.addEventListener("click", () => ask(ex.question, ex.id));
    chips.append(b);
  }
  $("model-info").textContent = `Provider: ${cfg.provider} | Model: ${cfg.model}` + (cfg.llm_configured ? "" : " (no API key configured)");
  $("asof-info").textContent = `As of ${cfg.as_of_date}; yesterday = ${cfg.yesterday_date}`;
}

function renderSources(sources) {
  const t = el("table", {}, el("caption", { text: "Tables used and why" }));
  t.append(el("thead", {}, el("tr", {}, ...["Table", "Why", "Key columns", "Join keys"].map((h) => el("th", { scope: "col", text: h })))));
  const tb = el("tbody");
  for (const s of sources) {
    const nameCell = el("td", {}, el("code", { text: s.table }));
    if (s.synthetic === "full") nameCell.append(" ", el("span", { class: "badge syn", title: "This table exists only in the generated demo supplement", text: "synthetic demo table" }));
    if (s.synthetic === "partial") nameCell.append(" ", el("span", { class: "badge syn", title: "This table includes demo-supplement rows (is_synthetic = 1)", text: "partly synthetic" }));
    tb.append(el("tr", {}, nameCell, el("td", { text: s.why }), el("td", { text: s.columns.join(", ") }), el("td", { text: s.join_keys.join("; ") || "-" })));
  }
  t.append(tb);
  return el("div", { class: "scroll" }, t);
}

function renderResult(cols, rows) {
  const t = el("table", {}, el("caption", { text: `Result (${rows.length} row${rows.length === 1 ? "" : "s"})` }));
  t.append(el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", { scope: "col", text: c })))));
  const tb = el("tbody");
  for (const r of rows) tb.append(el("tr", {}, ...r.map((v, i) => el("td", { class: typeof v === "number" ? "num" : "", text: fmtCell(cols[i], v) }))));
  t.append(tb);
  return el("div", { class: "scroll", tabindex: "0", role: "region", "aria-label": "Result table" }, t);
}

const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, text) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
}
const COLORS = ["#0b4f8a", "#c2570c", "#2e7d32"];

function renderChart(chart) {
  const W = 640, H = 300, m = { l: 70, r: 15, t: 20, b: 55 };
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": chart.title || "Chart", width: W, height: H });
  svg.append(s("title", {}, chart.title || "Chart"), s("desc", {}, `${chart.type} chart of ${chart.series.map((x) => x.name).join(", ")} by ${chart.x_label}`));
  const all = chart.series.flatMap((x) => x.values.filter((v) => typeof v === "number"));
  let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  if (lo === hi) hi = lo + 1;
  if (chart.type === "line") { const pad = (hi - lo) * 0.1; lo = Math.max(lo === 0 ? 0 : lo - pad, Math.min(...all) - pad); hi = hi + pad; }
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const y = (v) => m.t + ih - ((v - lo) / (hi - lo)) * ih;
  const n = chart.x.length;
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    svg.append(s("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: "#d5dde5" }));
    svg.append(s("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end", "font-size": 10, fill: "#333" }, nf.format(Math.abs(v) >= 1e6 ? v / 1e6 : v) + (Math.abs(v) >= 1e6 ? "m" : "")));
  }
  svg.append(s("line", { x1: m.l, x2: W - m.r, y1: y(0), y2: y(0), stroke: "#555" }));
  const step = iw / Math.max(n - (chart.type === "line" ? 1 : 0), 1);
  chart.x.forEach((lab, i) => {
    const cx = chart.type === "line" ? m.l + i * step : m.l + (i + 0.5) * (iw / n);
    if (n <= 12 || i % Math.ceil(n / 10) === 0) {
      const t = s("text", { x: cx, y: H - m.b + 14, "text-anchor": "end", "font-size": 10, fill: "#333", transform: `rotate(-35 ${cx} ${H - m.b + 14})` }, String(lab).replace(/^MS | Fund$/g, ""));
      svg.append(t);
    }
  });
  if (chart.type === "line") {
    chart.series.forEach((ser, k) => {
      const pts = ser.values.map((v, i) => (typeof v === "number" ? `${m.l + i * step},${y(v)}` : null)).filter(Boolean).join(" ");
      svg.append(s("polyline", { points: pts, fill: "none", stroke: COLORS[k % 3], "stroke-width": 2 }));
      ser.values.forEach((v, i) => { if (typeof v === "number") svg.append(s("circle", { cx: m.l + i * step, cy: y(v), r: 3, fill: COLORS[k % 3] })); });
    });
  } else {
    const gw = iw / n, bw = (gw * 0.7) / chart.series.length;
    chart.series.forEach((ser, k) => ser.values.forEach((v, i) => {
      if (typeof v !== "number") return;
      const x0 = m.l + i * gw + gw * 0.15 + k * bw;
      svg.append(s("rect", { x: x0, y: Math.min(y(v), y(0)), width: bw, height: Math.abs(y(v) - y(0)), fill: COLORS[k % 3] }));
    }));
  }
  chart.series.forEach((ser, k) => {
    svg.append(s("rect", { x: m.l + k * 150, y: 2, width: 10, height: 10, fill: COLORS[k % 3] }));
    svg.append(s("text", { x: m.l + k * 150 + 14, y: 11, "font-size": 11, fill: "#222" }, ser.name));
  });
  const box = el("div", { class: "chart" }, svg);
  const det = el("details", {}, el("summary", { text: "Chart data as a table" }));
  const rows = chart.x.map((lab, i) => [lab, ...chart.series.map((x) => x.values[i])]);
  det.append(renderResult([chart.x_label, ...chart.series.map((x) => x.name)], rows));
  box.append(det);
  return box;
}

function renderAnswer(question, a) {
  const card = el("article", { class: "card" });
  card.append(el("p", { class: "asked", text: `You asked: ${question}` }));
  const h = el("h3", { tabindex: "-1", text: a.interpreted_question });
  card.append(h, el("span", { class: "badge", text: `intent: ${a.intent}` }), " ", el("span", { class: "badge", text: `route: ${a.route}` }));
  if (a.error && a.error.code === "fund_access_denied") {
    card.append(el("div", { class: "deny", role: "alert" }, el("h4", { text: "Access denied for this fund" }),
      el("p", { text: a.error.message }),
      el("p", { text: "No query was run and no rows are returned. Your profile only covers the funds assigned to you. Ask about your own funds, or switch user in the header if you are entitled to more." })));
    return { card, h };
  }
  if (a.error) card.append(el("div", { class: "warn", role: "alert" }, el("strong", { text: `${a.error.code}: ` }), a.error.message));
  if (a.kpis.length) card.append(el("div", { class: "kpis" }, ...a.kpis.map((k) =>
    el("div", { class: "kpi" }, el("div", { class: "v", text: k.display + (k.unit ? ` ${k.unit}` : "") }), el("div", { class: "l", text: k.label })))));
  card.append(el("p", { text: a.narrative }));
  for (const w of a.warnings) if (!(a.error && w === a.error.message)) card.append(el("div", { class: "warn", role: "alert" }, el("span", { "aria-hidden": "true", text: "\u26A0 " }), w));
  if (a.sources.length) card.append(renderSources(a.sources));
  if (a.sql) {
    const pre = el("pre", {}, el("code", { text: a.sql }));
    const copy = el("button", { type: "button", text: "Copy SQL" });
    copy.addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(a.sql));
    card.append(el("details", {}, el("summary", { text: "SQL that was run" }), pre, copy));
  }
  if (a.columns.length) card.append(renderResult(a.columns, a.rows));
  if (a.chart) card.append(renderChart(a.chart));
  return { card, h };
}

async function ask(question, exId) {
  question = (question || "").trim();
  if (!question) return;
  const busy = (b) => { $("ask-btn").disabled = b; $("question").disabled = b; $("conversation").setAttribute("aria-busy", String(b)); $("status").textContent = b ? "Working..." : ""; };
  busy(true);
  try {
    const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, user_id: $("user-select").value, session_id: getSid() }) });
    const body = await r.json();
    if (!r.ok) throw new Error((body.error && body.error.message) || `Request failed (${r.status})`);
    sessionStorage.setItem(sessionKey(), body.session_id);
    $("empty-hint").hidden = true;
    const { card, h } = renderAnswer(question, body);
    $("conversation").append(card);
    h.focus();
    if (exId === "Q4" || /settlement rate/i.test(question)) { state.answered.add("Q4"); $("chip-Q4F").disabled = false; $("chip-Q4F").title = state.examples.find((e) => e.id === "Q4F").question; }
    $("question").value = "";
  } catch (e) {
    const card = el("article", { class: "card" }, el("div", { class: "warn", role: "alert", text: e.message }));
    $("conversation").append(card);
  } finally { busy(false); }
}

function resetConversation() {
  sessionStorage.removeItem(sessionKey());
  const c = $("conversation");
  for (const n of [...c.querySelectorAll(".card")]) n.remove();
  $("empty-hint").hidden = false;
  const q4f = $("chip-Q4F");
  if (q4f) { q4f.disabled = true; q4f.title = "Available after you ask Q4 in this conversation"; }
}

$("ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("question").value); });
$("user-select").addEventListener("change", resetConversation);
$("new-convo").addEventListener("click", resetConversation);
loadInit().catch((e) => { $("status").textContent = "Could not load the app: " + e.message; });
