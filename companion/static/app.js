"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Decimal input in either notation: "4,93" (Lithuanian keypad) or "4.93"
const num = (v) => Number(String(v ?? "").replace(/\s/g, "").replace(",", "."));
const eur = (v) => (v == null ? "–" : `€${Number(v).toFixed(2)}`);

const TITLES = {
  home: "Grocy+", scan: "Scan barcode", label: "Nutrition label", produce: "Market produce", weigh: "Weigh item",
  cook: "Cook recipe", stock: "Your stock", due: "Open / due", receipt: "Finish shopping", usage: "Claude usage", bug: "Report a bug", plan: "Week plan", report: "Weekly report", events: "History", login: "Grocy+",
};
let lookups = { products: [], recipes: [], stores: [], locations: [] };

// --- plumbing -----------------------------------------------------------------
// --- what happened before a bug report: last steps, errors (kept in memory, sent only with a report) ---
const trail = { actions: [], errors: [] };
const clock = () => new Date().toTimeString().slice(0, 8);
function remember(kind, entry) {
  const list = trail[kind];
  list.push({ t: clock(), ...entry });
  if (list.length > 60) list.shift();
}
window.addEventListener("error", (e) => remember("errors", { js: `${e.message} (${e.filename?.split("/").pop()}:${e.lineno})` }));
window.addEventListener("unhandledrejection", (e) => remember("errors", { js: String(e.reason?.message || e.reason) }));

function toast(msg, ms = 3500) {
  if (ms > 3500) remember("errors", { shown: msg });  // long toasts are the error messages
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

async function api(path, { method = "GET", json, form, busy } = {}) {
  if (busy) { $("#busy-text").textContent = busy; $("#busy").hidden = false; }
  try {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (json !== undefined) { opts.body = JSON.stringify(json); opts.headers["Content-Type"] = "application/json"; }
    if (form) opts.body = form;
    const started = Date.now();
    const res = await fetch(path, opts);
    const step = { api: `${method} ${path.split("?")[0]}`, status: res.status, ms: Date.now() - started };
    if (res.status === 401) { remember("actions", step); show("login"); throw new Error("Please log in"); }
    const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = data?.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText;
      remember("actions", { ...step, error: msg });
      remember("errors", { api: step.api, status: res.status, error: msg });
      throw new Error(msg);
    }
    remember("actions", step);
    return data;
  } finally {
    if (busy) $("#busy").hidden = true;
  }
}

let currentPanel = null;
let lastScreen = null;
function show(id) {
  if (id !== "bug") lastScreen = id;
  remember("actions", { screen: id });
  const changed = currentPanel !== id;
  currentPanel = id;
  $$(".panel").forEach((p) => (p.hidden = p.id !== id));
  if (changed) window.scrollTo(0, 0);  // a new screen starts at the top, not where the last one was scrolled
  $("#title").innerHTML = id === "home" || id === "login" ? 'Grocy<span class="plus">+</span>'
    : esc(id === "receipt" && lastReceipt ? MODE_TITLES[lastReceipt.mode || "receipt"] : TITLES[id]);
  $("#back").hidden = id === "home" || id === "login";
  $("#bug-btn").hidden = !lookups.testing || id === "login" || id === "bug";  // test phase only
  if (!["scan", "label"].includes(id)) receiptCtx = null;  // back on the receipt (or elsewhere): line context done
  const forLine = receiptCtx != null && (id === "scan" || id === "label")
    ? (receiptCtx === -1 ? { text: "New item for the list" } : lastReceipt?.lines[receiptCtx]) : null;
  $("#ctx").hidden = !forLine;
  if (forLine) $("#ctx-text").textContent = forLine.text;
  if (id !== "scan") stopScan();
  if (id === "scan" && receiptCtx != null) startScan().catch(() => {});
  if (id === "due") loadDue();
  if (id === "events") loadEvents();
  if (id === "home") { loadHomeDue(); loadCreditWarning(); loadFirstStep(); }
  if (id === "receipt") openFinishShopping();
  if (id === "stock") loadStock();
  if (id === "usage") loadUsage();
  if (location.hash !== `#${id}` && id !== "login") history.pushState(null, "", `#${id}`);
}

function formJson(form, numeric = []) {
  const out = {};
  for (const [k, v] of new FormData(form).entries()) {
    if (v instanceof File || v === "") continue;
    out[k] = numeric.includes(k) ? num(v) : v;
  }
  return out;
}

function fillPickers() {
  const opt = (v, t) => `<option value="${esc(v)}">${esc(t)}</option>`;
  $$("select.products").forEach((s) => {
    const keepNew = s.querySelector('option[value="new"]');
    s.innerHTML = (keepNew ? opt("new", "New product") : "") + lookups.products.map((p) => opt(p.id, `${p.name} (${p.unit})`)).join("");
  });
  $$("select.recipes").forEach((s) => (s.innerHTML = lookups.recipes.map((r) => opt(r.id, r.name)).join("")));
  $$("select.stores").forEach((s) => {
    s.innerHTML = opt("", "–") + lookups.stores.map((n) => opt(n, n[0].toUpperCase() + n.slice(1))).join("");
  });
  $("#produce-form [name=store]").value = "turgus";
  $("#product-names").innerHTML = lookups.products.map((p) => `<option value="${esc(p.name)}">`).join("");
}

async function refreshLookups() {
  lookups = await api("/lookups");
  $("#version").textContent = (lookups.version ? `version ${lookups.version}` : "") + (lookups.testing ? " · test phase" : "");
  fillPickers();
}

function list(items) {
  return `<ul class="list">${items.map((i) => `<li>${i}</li>`).join("")}</ul>`;
}

function nutritionLine(n) {
  if (!n) return "";
  const miss = n.missing?.length ? ` <span class="muted">(no data: ${esc(n.missing.join(", "))})</span>` : "";
  return `${n.kcal} kcal · P ${n.protein} g · F ${n.fat} g · C ${n.carbs} g${miss}`;
}

// --- due list ---------------------------------------------------------------------
function dueItem(i) {
  const d = i.days_left;
  const cls = d == null ? "" : d <= 0 ? "bad" : d <= 1 ? "warn" : "";
  const when = d == null ? "no due date" : d < 0 ? `${-d} d overdue` : d === 0 ? "today" : `${d} d left`;
  return `<strong>${esc(i.name)}</strong> ${i.opened ? '<span class="badge">opened</span>' : ""}<br>
    <span class="small">${esc(i.amount_text)} · <span class="${cls}">${when}</span>${i.due ? ` (${esc(i.due)})` : ""}</span>`;
}

async function loadDue() {
  const days = $("#due-days").value || 3;
  const items = await api(`/stock/open-due?days=${days}`);
  $("#due-list").innerHTML = `<div class="card">${items.length ? list(items.map(dueItem)) : "Nothing opened or due. 👍"}</div>`;
}

async function loadHomeDue() {
  try {
    const items = await api("/stock/open-due");
    $("#home-due").innerHTML = items.length ? `<h2>Use these first</h2>${list(items.slice(0, 6).map(dueItem))}` : "";
  } catch { /* not logged in yet */ }
}

// One-time first step: put what's already at home into Grocy (shown until done or skipped)
function loadFirstStep() {
  const card = $("#first-step");
  card.hidden = lookups.home_stock_done !== false;
  if (card.hidden) return;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("gp-receipt") || "null"); } catch { /* none */ }
  const going = (lastReceipt || saved)?.mode === "home";
  $("#first-step-go").textContent = going ? "Continue the stock-take" : "Start";
}

async function startHomeStock() {
  if (!lastReceipt) { try { lastReceipt = JSON.parse(localStorage.getItem("gp-receipt") || "null"); } catch { /* none */ } }
  if (lastReceipt?.mode === "home") return show("receipt");
  if (lastReceipt?.lines.some((l) => l.include && !l.done) && !confirm("A shopping checklist is still open. Replace it with the stock-take?")) return;
  startList("home");
}

async function loadEvents() {
  const ev = await api("/events?limit=100");
  $("#events-list").innerHTML = `<div class="card">${list(ev.map((e) => `<span class="small muted">${esc(e.ts.replace("T", " "))} · ${esc(e.kind)}</span><br>${esc(e.message)}`))}</div>`;
}

// --- barcode scanning ------------------------------------------------------------------
let scanControls = null;
async function startScan() {
  if (!window.ZXingBrowser) return toast("Scanner library not loaded yet");
  stopScan();
  const reader = new ZXingBrowser.BrowserMultiFormatReader();
  try {
    scanControls = await reader.decodeFromConstraints(
      { video: { facingMode: "environment" } }, $("#video"),
      (result) => {
        if (!result) return;
        const code = result.getText();
        // Accept a code only when its check digit is right and two reads in a row agree:
        // single misreads (another barcode in view, a blurry frame) are skipped.
        if (!validBarcode(code) || code !== lastRead) { lastRead = code; return; }
        stopScan();
        navigator.vibrate?.(80);
        $("#manual-code [name=code]").value = code;
        toast(`Barcode ${code}`);
        lookupCode(code).catch((err) => toast(err.message, 6000));
      },
    );
  } catch (e) { toast(`Camera: ${e.message}`); }
}
let lastRead = null;
function validBarcode(code) {
  if (!/^\d{8}$|^\d{12,14}$/.test(code)) return /\S/.test(code) && !/^\d+$/.test(code);  // non-numeric codes: accept
  const digits = code.split("").map(Number);
  const check = digits.pop();
  const sum = digits.reverse().reduce((a, d, i) => a + d * (i % 2 === 0 ? 3 : 1), 0);
  return (10 - (sum % 10)) % 10 === check;
}
function stopScan() { lastRead = null; if (scanControls) { scanControls.stop(); scanControls = null; } }

async function lookupCode(code) {
  const r = await api(`/products/barcode/${encodeURIComponent(code)}`, { method: "POST", busy: "Looking up…" });
  const p = r.product;
  if (receiptCtx != null) {
    $("#add-stock").hidden = true;  // the receipt adds the stock, not the scan screen
    if (p) return assignToLine(p);
    $("#label-form [name=product_id]").value = "new";
    $("#label-form [name=barcode]").value = code;
    show("label");
    return toast("Not found in Open Food Facts: take a photo of the nutrition table", 6000);
  }
  let html = `<div class="card"><h2>${esc(p ? p.name : code)}</h2><p>${esc(r.message)}</p>`;
  if (p?.nutrition_per_100?.kcal != null) {
    const n = p.nutrition_per_100;
    html += `<p class="small muted">Per 100: ${n.kcal} kcal · P ${n.protein ?? "?"} · F ${n.fat ?? "?"} · C ${n.carbs ?? "?"}</p>`;
  }
  if (r.needs_label_photo) html += `<button id="go-label" class="secondary">📸 Photo of nutrition table</button>`;
  html += "</div>";
  $("#scan-result").innerHTML = html;
  $("#go-label")?.addEventListener("click", async () => {
    await refreshLookups();
    $("#label-form [name=product_id]").value = p ? String(p.id) : "new";
    $("#label-form [name=barcode]").value = code;
    show("label");
  });
  const form = $("#add-stock");
  form.hidden = !p;
  if (p) {
    form.product_id.value = p.id;
    form.unit.value = p.net_amount_per_pack ? "pack" : p.stock_unit;
  }
}

// --- receipt checklist -------------------------------------------------------------------
// Receipt photo -> lines -> per line: in Grocy? amount (pack size)? nutrition? -> add to stock.
// Missing products are scanned / label-photographed from the line itself (receiptCtx = line index).
let lastReceipt = null;
let receiptCtx = null;
const COUNT_UNITS = ["piece", "pack"];
const MASS_VOL = ["g", "kg", "ml", "l"];

const hasNutrition = (card) => {
  const n = card?.nutrition_per_100 || {};
  return [n.kcal, n.protein, n.fat, n.carbs].every((v) => v != null);
};
const needsPack = (l) => COUNT_UNITS.includes(l.unit) && l.product && MASS_VOL.includes(l.product.stock_unit);
const needsShelf = (card) => !Number(card?.default_best_before_days);
const baseUnit = (u) => (u === "kg" || u === "g" ? "g" : u === "l" || u === "ml" ? "ml" : null);

function initLine(l) {
  l.include = l.include ?? Boolean(l.is_food);
  if (l.pack_size == null && l.product?.net_amount_per_pack) {
    l.pack_size = Number(l.product.net_amount_per_pack);
    l.pack_unit = baseUnit(l.product.net_unit) || "g";
  }
  l.pack_unit = l.pack_unit || baseUnit(l.product?.stock_unit) || "g";
}

function lineState(l) {
  if (!l.product) return { ok: false, why: "missing" };
  if (!(l.quantity > 0)) return { ok: false, why: "qty" };
  if (l.shelf === "asking" || l.shelf === "question") return { ok: false, why: "shelf" };
  if (needsPack(l) && !l.pack_size) return { ok: false, why: "pack" };
  return { ok: true, why: hasNutrition(l.product) ? "" : "nutrition" };
}

function amountHtml(l, i) {
  const units = (UNITS.includes(l.unit) ? UNITS : [l.unit, ...UNITS]).map((u) => `<option${u === l.unit ? " selected" : ""}>${u}</option>`).join("");
  let pack = "";
  if (COUNT_UNITS.includes(l.unit)) {
    const total = l.pack_size && l.quantity ? `= <strong>${+(l.quantity * l.pack_size).toFixed(1)} ${esc(l.pack_unit)}</strong>` : "";
    const pu = ["g", "ml"].map((u) => `<option${u === l.pack_unit ? " selected" : ""}>${u}</option>`).join("");
    pack = ` ×
    <input class="rpack" data-i="${i}" type="text" inputmode="decimal" autocomplete="off" placeholder="${needsPack(l) ? "size" : "optional"}" value="${l.pack_size ?? ""}">
    <select class="rpunit" data-i="${i}">${pu}</select> <span class="rtotal">${total}</span>`;
  }
  return `<div class="ramount"><input class="rqty" data-i="${i}" type="text" inputmode="decimal" autocomplete="off" placeholder="how much" value="${l.quantity ?? ""}">
    <select class="runit" data-i="${i}">${units}</select>${pack}</div>`;
}

function chip(ok, text, warn) {
  return `<span class="chip ${ok ? "ok" : warn ? "warn" : "bad"}">${esc(text)} ${ok ? "✓" : "!"}</span>`;
}

function chipsHtml(l) {
  const counted = COUNT_UNITS.includes(l.unit);
  if (!l.product) {
    // Not in Grocy yet: nothing is known about it
    return [chip(false, "In Grocy"), chip(false, "Nutrition"), counted ? chip(false, "Weight") : chip(true, "Weight")].join("");
  }
  const weight = !counted || l.pack_size ? chip(true, "Weight") : needsPack(l) ? chip(false, "Weight") : chip(false, "Weight", true);
  return [
    chip(true, "In Grocy"),
    hasNutrition(l.product) ? chip(true, "Nutrition") : chip(false, "Nutrition", true),
    weight,
    shelfChip(l),
  ].join("");
}

function shelfChip(l) {
  const days = Number(l.product.default_best_before_days);
  if (!needsShelf(l.product)) return chip(true, days < 0 ? "No due date" : `Keeps ${days} d`);
  if (l.shelf === "asking") return chip(false, "Shelf life: asking Claude…", true);
  if (l.shelf === "question") return chip(false, "Shelf life: question", true);
  return chip(false, "Shelf life", true);
}

function shelfQuestionHtml(l, i) {
  if (l.shelf !== "question") return "";
  return `<p class="small">${esc(l.shelf_q.question)}</p><div class="ropts">${l.shelf_q.options
    .map((o) => `<button type="button" class="secondary" data-act="shelf" data-i="${i}" data-a="${esc(o)}">${esc(o)}</button>`).join("")}</div>`;
}

// Products made by hand in Grocy have no shelf life: Claude suggests it (asking first if it must)
async function askShelfLife(i, answer) {
  const l = lastReceipt.lines[i];
  l.shelf = "asking";
  updateReceiptLine(i);
  try {
    const r = await api(`/products/${l.product.id}/shelf-life`, { method: "POST", json: answer ? { answer } : {} });
    if (r.status === "question") Object.assign(l, { shelf: "question", shelf_q: r });
    else Object.assign(l, { shelf: "saved", product: r.product });
  } catch (err) {
    l.shelf = "failed";
    toast(`Shelf life for ${l.product.name}: ${err.message}`, 6000);
  }
  updateReceiptLine(i);
}

async function ensureShelfLife() {
  for (const [i, l] of lastReceipt.lines.entries()) {
    if (l.include && l.product && needsShelf(l.product) && !l.shelf) await askShelfLife(i);
  }
}

function summaryHtml(r) {
  const inc = r.lines.filter((l) => l.include);
  const ready = inc.filter((l) => lineState(l).ok).length;
  const noNut = inc.filter((l) => l.product && !hasNutrition(l.product)).length;
  return { ready, html: `<span><b>${ready}</b> of ${inc.length} ready</span>${noNut ? `<span class="warn">${noNut} without nutrition</span>` : ""}` };
}

// Typing a pack size updates the line in place (a full redraw would remove the box being edited)
function updateReceiptLine(i) {
  const l = lastReceipt.lines[i];
  const li = $(`#receipt-result .rline[data-i="${i}"]`);
  if (!li) return;
  li.querySelector(".chips").innerHTML = chipsHtml(l);
  const rq = li.querySelector(".rq");
  if (rq) rq.innerHTML = shelfQuestionHtml(l, i);
  const rtotal = li.querySelector(".rtotal");
  if (rtotal) rtotal.innerHTML = l.pack_size && l.quantity ? `= <strong>${+(l.quantity * l.pack_size).toFixed(1)} ${esc(l.pack_unit)}</strong>` : "";
  const hint = li.querySelector(".rhint");
  if (hint) { hint.textContent = hintText(l); hint.hidden = lineState(l).ok; }
  const sum = summaryHtml(lastReceipt);
  $("#receipt-result .rsum").innerHTML = sum.html;
  const btn = $("#receipt-commit");
  if (btn) { btn.disabled = !sum.ready; btn.textContent = commitText(sum.ready); }
  saveReceipt();
}

const UNITS = ["piece", "pack", "g", "kg", "ml", "l"];
const LOCATIONS = ["Fridge", "Freezer", "Pantry"];
const MODE_TITLES = { receipt: "Finish shopping", manual: "Finish shopping", home: "Stock at home" };

// A list without a receipt: "manual" (receipt won't read / no receipt) or "home" (one-time stock-take)
function startList(mode) {
  lastReceipt = { mode, store: null, date: new Date().toLocaleDateString("sv-SE"), total_eur: null, lines: [], location: "Fridge" };
  renderReceipt();
  show("receipt");
}

function newLine(product, name) {
  const r = lastReceipt;
  const unit = !product ? "piece"
    : product.net_amount_per_pack && MASS_VOL.includes(product.stock_unit) ? "pack"
    : product.stock_unit || "piece";
  return {
    text: product?.name || name, name_en: "", quantity: MASS_VOL.includes(unit) ? null : 1, unit,
    line_total_eur: null, is_food: true, include: true, added_by_hand: true,
    product_id: product?.id ?? null, product: product || null,
    location: r.mode === "home" ? r.location : null,
  };
}

function headerHtml(r) {
  if (r.mode === "home") {
    const seg = LOCATIONS.map((x) => `<button type="button" class="${x === r.location ? "" : "secondary"}" data-act="loc" data-loc="${x}">${x}</button>`).join("");
    return `<h2>What's at home</h2>
      <p class="small muted">Scan or name what you have, one place at a time. No prices needed; tick what's already open.</p>
      <div class="seg"><span class="small muted">Putting away in</span>${seg}</div>`;
  }
  if (r.mode === "manual") {
    const stores = `<option value="">Store…</option>` + lookups.stores.map((x) => `<option value="${esc(x)}"${x === (r.store || "").toLowerCase() ? " selected" : ""}>${esc(x[0].toUpperCase() + x.slice(1))}</option>`).join("");
    return `<h2>Without a receipt</h2>
      <p class="small muted">Scan or name each item. Prices are optional.</p>
      <select class="rstore" aria-label="Store">${stores}</select>`;
  }
  const sum = r.lines.reduce((a, l) => a + (l.line_total_eur || 0), 0);
  return `<h2>${esc(r.store || "Receipt")} ${r.date ? `<span class="badge">${esc(r.date)}</span>` : ""}</h2>
    <p class="small muted">Total ${eur(r.total_eur)} · lines ${eur(sum)}</p>`;
}

function extrasHtml(l, i, mode) {
  if (mode === "receipt" && !l.added_by_hand) return "";
  const loc = mode === "home" ? `<select class="rloc" data-i="${i}" aria-label="Where">${LOCATIONS.map((x) => `<option${x === (l.location || l.product?.location) ? " selected" : ""}>${x}</option>`).join("")}</select>` : "";
  const price = mode !== "home" ? `<label class="rbb">Price €<input class="rprice" data-i="${i}" type="text" inputmode="decimal" autocomplete="off" placeholder="optional" value="${l.line_total_eur ?? ""}"></label>` : "";
  return `<div class="rextra">
      <label class="check"><input type="checkbox" class="ropen" data-i="${i}"${l.opened ? " checked" : ""}> Already opened</label>
      <label class="rbb">Best before<input class="rbbd" data-i="${i}" type="date" value="${l.best_before || ""}"></label>
      ${loc}${price}</div>`;
}

function renderReceipt() {
  const r = lastReceipt;
  r.mode = r.mode || "receipt";
  r.lines.forEach(initLine);
  const opts = (sel) => `<option value="">${sel ? "Not this product" : "Already in Grocy under another name? Pick it"}</option>` + lookups.products
    .map((p) => `<option value="${p.id}"${p.id === sel ? " selected" : ""}>${esc(p.name)}</option>`).join("");
  const rows = r.lines.map((l, i) => {
    const st = lineState(l);
    const suggest = !l.product && l.suggestion && !l.suggestion_no
      ? `<div class="rsugg"><span>In Grocy as <strong>${esc(l.suggestion.name)}</strong>?</span>
          <button type="button" data-act="sugg-yes" data-i="${i}">Yes</button>
          <button type="button" class="secondary" data-act="sugg-no" data-i="${i}">No</button></div>` : "";
    const actions = [
      !l.product ? `<button type="button" class="secondary" data-act="scan" data-i="${i}">Scan barcode</button>` : "",
      !l.product && l.added_by_hand && (!l.suggestion || l.suggestion_no) ? `<button type="button" class="secondary" data-act="create" data-i="${i}">Create "${esc(l.text)}"</button>` : "",
      l.product && !hasNutrition(l.product) ? `<button type="button" class="secondary" data-act="label" data-i="${i}">Photo of label</button>` : "",
    ].join("");
    const price = r.mode === "receipt" && !l.added_by_hand ? `<span class="num">${eur(l.line_total_eur)}</span>` : "";
    return `<li class="rline${l.is_food ? "" : " nonfood"}${l.include ? "" : " off"}" data-i="${i}">
      <div class="rtop"><label class="check"><input type="checkbox" class="rinc" data-i="${i}"${l.include ? " checked" : ""}>
        <strong>${esc(l.text)}</strong></label>${price}</div>
      <div class="small muted">${esc(l.name_en)}${l.is_food ? "" : ' · <span class="badge">not food</span>'}${l.product && l.product.name !== l.text ? ` · <span class="good">${esc(l.product.name)}</span>` : ""}</div>
      ${l.include ? `<div class="chips">${chipsHtml(l)}</div><div class="rq">${shelfQuestionHtml(l, i)}</div>
      ${l.product ? amountHtml(l, i) + extrasHtml(l, i, r.mode) : ""}
      ${suggest}${actions ? `<div class="ractions">${actions}</div>` : ""}
      <select class="rprod" data-i="${i}" aria-label="Grocy product">${opts(l.product_id)}</select>` : ""}
      ${l.include && l.product ? `<div class="small warn rhint"${st.ok ? " hidden" : ""}>${hintText(l)}</div>` : ""}</li>`;
  }).join("");
  const { ready, html: sumHtml } = summaryHtml(r);
  const addTitle = r.mode === "receipt" ? "Missing a line? Add it" : "Add an item";
  $("#receipt-result").innerHTML = `<div class="card">
    ${headerHtml(r)}
    ${r.lines.length ? `<div class="rsum">${sumHtml}</div>` : ""}
    <ul class="list">${rows}</ul>
    <div class="radd"><h3>${addTitle}</h3>
      <button type="button" data-act="add-scan">Scan barcode</button>
      <form class="rname row"><input name="name" placeholder="…or type a name, e.g. grikiai" autocomplete="off"><button type="submit" class="secondary">Add</button></form>
    </div>
    ${r.lines.length ? `<button id="receipt-commit" type="button"${ready ? "" : " disabled"}>${commitText(ready)}</button>
      <p class="small muted">Lines that aren't ready stay here: finish them, then add again.</p>` : ""}
    ${r.mode === "home" ? `<button type="button" class="secondary" data-act="home-done">I'm done with the first step</button>` : ""}
    <button id="receipt-discard" type="button" class="secondary danger">Discard this list</button></div>`;
  saveReceipt();
  ensureShelfLife();
}

function commitText(ready) {
  const word = lastReceipt.mode === "receipt" ? "ready line" : "item";
  return `Add ${ready} ${word}${ready === 1 ? "" : "s"} to stock`;
}

function hintText(l) {
  const st = lineState(l);
  if (st.why === "qty") return "How much? Type the amount.";
  if (st.why === "pack") return "How much is one pack? (e.g. 500 g of butter)";
  return "";
}

// The checklist survives closing the app (iPhone may also reload the page after using the camera)
function saveReceipt() {
  try {
    if (lastReceipt?.lines.length || (lastReceipt && lastReceipt.mode !== "receipt")) localStorage.setItem("gp-receipt", JSON.stringify(lastReceipt));
    else localStorage.removeItem("gp-receipt");
  } catch { /* storage blocked: the checklist just isn't kept */ }
  syncReceiptForm();
}

// The receipt photo form only belongs to "Finish shopping" with a receipt (or nothing started yet)
function syncReceiptForm() {
  const noReceipt = lastReceipt && lastReceipt.mode && lastReceipt.mode !== "receipt";
  $("#receipt-form").hidden = !!noReceipt;
  $("#receipt-inbox").hidden = !!noReceipt;
  $("#manual-start").hidden = !!lastReceipt;
}

async function openFinishShopping() {
  if (!lastReceipt) {
    try { lastReceipt = JSON.parse(localStorage.getItem("gp-receipt") || "null"); } catch { lastReceipt = null; }
    lastReceipt?.lines.forEach((l) => { if (l.shelf === "asking") l.shelf = null; });  // app closed mid-request
    if (lastReceipt?.lines.length || (lastReceipt && lastReceipt.mode !== "receipt")) renderReceipt();
  }
  syncReceiptForm();
  if (lastReceipt?.mode === "home") return;
  try {
    const r = await api("/receipts/inbox");
    $("#receipt-inbox").innerHTML = `<h2>Receipts waiting</h2>` + (r.receipts.length
      ? list(r.receipts.map((x) => `${esc(x.store || "Receipt")} ${esc(x.date || "")} <span class="muted small">${eur(x.total_eur)}</span>`))
      : `<p class="small muted">None waiting${lastReceipt?.lines.length ? "" : ": scan the paper receipt below"}.
         Receipts from e-mail will appear here in a later phase.</p>`);
  } catch (err) { $("#receipt-inbox").innerHTML = ""; }
}

async function assignToLine(card) {
  let l = lastReceipt.lines[receiptCtx];
  if (receiptCtx === -1) { l = newLine(card); lastReceipt.lines.push(l); }  // "Add an item" by scanning
  receiptCtx = null;
  Object.assign(l, { product_id: card.id, product: card, include: true, pack_size: null, shelf: null });
  await refreshLookups();
  renderReceipt();
  show("receipt");
  $(`#receipt-result .rline[data-i="${lastReceipt.lines.indexOf(l)}"]`)?.scrollIntoView({ block: "center" });
  toast(`${card.name} added to the list`);
}

async function addByName(name) {
  const r = await api("/products/by-name", { method: "POST", json: { name }, busy: "Looking it up…" });
  const l = newLine(r.product, name);
  if (r.status === "suggestion") Object.assign(l, { suggestion: r.suggestion });
  if (r.status === "created") await refreshLookups();
  lastReceipt.lines.push(l);
  renderReceipt();
  $(`#receipt-result .rline[data-i="${lastReceipt.lines.length - 1}"]`)?.scrollIntoView({ block: "center" });
  $("#receipt-result .rname [name=name]").value = "";
}

async function finishHomeStock() {
  const open = lastReceipt.lines.filter((l) => l.include && !l.done).length;
  if (open && !confirm(`${open} item(s) on the list aren't added yet. Finish anyway?`)) return;
  await api("/setup/home-stock", { method: "POST", json: { done: true } });
  lookups.home_stock_done = true;
  lastReceipt = null;
  saveReceipt();
  $("#receipt-result").innerHTML = "";
  toast("First step done: your stock is in Grocy", 5000);
  show("home");
}

async function commitReceipt() {
  const todo = lastReceipt.lines.filter((l) => l.include && lineState(l).ok && !l.done);
  if (!todo.length) return toast("Nothing ready to add");
  const lines = todo.map((l) => ({
    product_id: l.product_id, quantity: l.quantity, unit: l.unit, line_total_eur: l.line_total_eur ?? null,
    ...(COUNT_UNITS.includes(l.unit) && l.pack_size ? { pack_size: l.pack_size, pack_unit: l.pack_unit } : {}),
    ...(l.opened ? { opened: true } : {}),
    ...(l.best_before ? { best_before: l.best_before } : {}),
    ...(l.location ? { location: l.location } : {}),
  }));
  const store = lastReceipt.mode === "home" ? null : lastReceipt.store;
  const r = await api("/receipts/commit", { method: "POST", json: { store, lines }, busy: "Adding to stock…" });
  r.lines.forEach((res, k) => { if (res.added) todo[k].done = true; });
  const left = lastReceipt.lines.filter((l) => l.include && !l.done);
  lastReceipt.lines = left;
  const doneHtml = `<div class="card"><h2>${r.added} line(s) added to stock</h2>
    ${list(r.lines.map((l) => l.added ? esc(l.added) : `<span class="bad">${esc(l.error)}</span>`))}</div>`;
  if (left.length || lastReceipt.mode === "home") {
    // The home stock-take continues (next shelf) until "I'm done"
    renderReceipt();
    $("#receipt-result").insertAdjacentHTML("afterbegin", doneHtml);
  } else { $("#receipt-result").innerHTML = doneHtml; saveReceipt(); }
}

function wireReceipt() {
  const box = $("#receipt-result");
  box.addEventListener("change", async (e) => {
    if (e.target.classList.contains("rstore")) { lastReceipt.store = e.target.value || null; return saveReceipt(); }
    const i = Number(e.target.dataset.i);
    const l = lastReceipt?.lines[i];
    if (!l) return;
    if (e.target.classList.contains("ropen")) { l.opened = e.target.checked; return saveReceipt(); }
    if (e.target.classList.contains("rbbd")) { l.best_before = e.target.value || null; return saveReceipt(); }
    if (e.target.classList.contains("rloc")) { l.location = e.target.value; return saveReceipt(); }
    if (["rqty", "rprice"].some((c) => e.target.classList.contains(c))) return;  // handled on "input"
    try {
      if (e.target.classList.contains("rinc")) l.include = e.target.checked;
      if (e.target.classList.contains("rpack")) return;  // handled on "input"
      if (e.target.classList.contains("rpunit")) { l.pack_unit = e.target.value; return updateReceiptLine(i); }
      if (e.target.classList.contains("runit")) {
        l.unit = e.target.value;
        if (!COUNT_UNITS.includes(l.unit)) l.pack_size = null;
      }
      if (e.target.classList.contains("rprod")) {
        const id = Number(e.target.value);
        Object.assign(l, id ? { product_id: id, product: await api(`/products/${id}/card`), pack_size: null, shelf: null } : { product_id: null, product: null, include: false });
      }
      renderReceipt();
    } catch (err) { toast(err.message, 6000); }
  });
  box.addEventListener("input", (e) => {
    const i = Number(e.target.dataset.i);
    const l = lastReceipt?.lines[i];
    if (!l) return;
    if (e.target.classList.contains("rpack")) l.pack_size = num(e.target.value) || null;
    else if (e.target.classList.contains("rqty")) l.quantity = num(e.target.value) || null;
    else if (e.target.classList.contains("rprice")) { l.line_total_eur = e.target.value.trim() ? num(e.target.value) : null; return saveReceipt(); }
    else return;
    updateReceiptLine(i);
  });
  box.addEventListener("submit", async (e) => {
    if (!e.target.classList.contains("rname")) return;
    e.preventDefault();
    const name = e.target.name.value.trim();
    if (!name) return;
    try { await addByName(name); } catch (err) { toast(err.message, 6000); }
  });
  box.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    try {
      if (b.id === "receipt-commit") return await commitReceipt();
      if (b.id === "receipt-discard") {
        if (!confirm("Discard this list? Nothing more is added to stock.")) return;
        lastReceipt = null;
        saveReceipt();
        try { localStorage.removeItem("gp-receipt"); } catch { /* ignore */ }
        $("#receipt-result").innerHTML = "";
        syncReceiptForm();
        show(currentPanel);  // title back to "Finish shopping"
        return toast("List discarded");
      }
      if (b.dataset.act === "shelf") return await askShelfLife(Number(b.dataset.i), b.dataset.a);
      if (b.dataset.act === "manual") return startList("manual");
      if (b.dataset.act === "loc") { lastReceipt.location = b.dataset.loc; return renderReceipt(); }
      if (b.dataset.act === "add-scan") { receiptCtx = -1; return show("scan"); }
      if (b.dataset.act === "home-done") return await finishHomeStock();
      if (b.dataset.act === "create") {
        const l = lastReceipt.lines[Number(b.dataset.i)];
        const r = await api("/products/by-name", { method: "POST", json: { name: l.text, create: true }, busy: "Creating with Claude…" });
        Object.assign(l, newLine(r.product), { text: l.text });
        await refreshLookups();
        return renderReceipt();
      }
      if (b.dataset.act === "sugg-yes" || b.dataset.act === "sugg-no") {
        const l = lastReceipt.lines[Number(b.dataset.i)];
        if (b.dataset.act === "sugg-yes") Object.assign(l, { product_id: l.suggestion.id, product: l.suggestion, include: true, shelf: null });
        else l.suggestion_no = true;
        return renderReceipt();
      }
      receiptCtx = Number(b.dataset.i);
      const l = lastReceipt.lines[receiptCtx];
      if (b.dataset.act === "scan") show("scan");
      if (b.dataset.act === "label") {
        $("#label-form [name=product_id]").value = String(l.product.id);
        $("#label-form [name=barcode]").value = "";
        show("label");
      }
    } catch (err) { toast(err.message, 6000); }
  });
}

// --- recipe check (cook) -----------------------------------------------------------------------
const NUTRIENT_ROWS = [["kcal", "Energy", "kcal"], ["protein", "Protein", "g"], ["fat", "Fat", "g"],
  ["satfat", "of which saturates", "g"], ["carbs", "Carbohydrates", "g"], ["sugar", "of which sugars", "g"],
  ["fiber", "Fibre", "g"], ["salt", "Salt", "g"]];

function renderRecipeCheck(r) {
  const missing = r.ingredients.filter((i) => !i.ok);
  const rows = r.ingredients.map((i) => `<div class="ing ${i.ok ? "ok" : "no"}"><span class="mark">${i.ok ? "✓" : "✗"}</span>
    <span><strong>${esc(i.name)}</strong><br><span class="small muted">need ${i.amount} ${esc(i.unit)} · have ${i.in_stock} ${esc(i.unit)}${i.ok ? "" : ` · <span class="bad">missing ${i.missing} ${esc(i.unit)}</span>`}</span></span></div>`).join("");
  const table = NUTRIENT_ROWS.map(([k, label, u]) => `<tr${label.startsWith("of which") ? ' class="sub"' : ""}><td>${label}</td>
    <td class="num">${r.total[k] ?? 0} ${u}</td><td class="num">${r.per_serving[k] ?? 0} ${u}</td></tr>`).join("");
  $("#cook-result").innerHTML = `<div class="card">
    <h2>${esc(r.recipe.name)} <span class="badge">${r.servings} servings</span></h2>
    <div class="verdict ${r.all_in_stock ? "ok" : "no"}">${r.all_in_stock ? "✓ You have everything to cook this" : `✗ Missing ${missing.length} of ${r.ingredients.length} ingredients`}</div>
    <h3>Ingredients</h3>${rows}
    <h3>Nutrition</h3>
    <table class="nut"><tr><th></th><th class="num">Total</th><th class="num">Per serving</th></tr>${table}</table>
    ${r.missing_nutrition.length ? `<p class="small warn">No nutrition data yet for: ${esc(r.missing_nutrition.join(", "))}. The totals don't include them.</p>` : ""}
    <div class="cost"><span>Cost</span><strong>${eur(r.cost_total_eur)}</strong><span class="muted small">${eur(r.cost_per_serving_eur)} per serving</span></div>
    ${r.warnings?.length ? `<p class="small warn">${r.warnings.map(esc).join("<br>")}</p>` : ""}
    ${r.created_products?.length ? `<p class="small muted">New in Grocy: ${esc(r.created_products.join(", "))}</p>` : ""}
    <div class="ractions">
      ${missing.length ? `<button type="button" class="secondary" data-act="shop" data-id="${r.recipe.id}">Add missing to shopping list</button>` : ""}
      <button type="button" data-act="cook" data-id="${r.recipe.id}" data-servings="${r.servings}"${r.all_in_stock ? "" : ' class="secondary"'}>Cook now</button>
    </div></div>`;
}

// --- stock list and quick search -----------------------------------------------------------
let stockItems = [];

async function loadStock() {
  stockItems = await api("/stock/all");
  renderStock();
  $("#stock-q").focus();
}

function renderStock() {
  const q = $("#stock-q").value.trim().toLowerCase();
  const items = stockItems.filter((i) => !q || i.name.toLowerCase().includes(q));
  const value = stockItems.reduce((a, i) => a + i.value_eur, 0);
  $("#stock-sum").textContent = q ? `${items.length} of ${stockItems.length} products` : `${stockItems.length} products · ${eur(value)} total value`;
  const groups = {};
  for (const i of items) (groups[i.location || "No location"] ||= []).push(i);
  $("#stock-list").innerHTML = Object.keys(groups).sort().map((loc) => `<div class="card"><h2>${esc(loc)}</h2>${list(groups[loc].map((i) => {
    const d = i.days_left;
    const cls = d == null ? "" : d < 0 ? "bad" : d <= 2 ? "warn" : "";
    const when = d == null ? "" : d < 0 ? `${-d} d overdue` : d === 0 ? "due today" : `${d} d left`;
    return `<div class="sitem"><strong>${esc(i.name)}</strong><span class="num">${esc(i.amount_text)}</span></div>
      <div class="sline"><span class="small muted">${i.opened_text ? `<span class="badge">opened ${esc(i.opened_text)}</span> ` : ""}${when ? `<span class="${cls}">${when}</span>` : "no due date"}</span>
      <button type="button" class="secondary mini" data-open="${i.product_id}">Opened</button></div>`;
  }))}</div>`).join("") || `<div class="card muted">${q ? "Nothing in stock matches." : "Nothing in stock yet."}</div>`;
}

// --- bug reports ---------------------------------------------------------------------------
let bugShot = null;  // automatic screenshot of the screen the report is about

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if ([...document.scripts].some((x) => x.src === src)) return resolve();
    const el = Object.assign(document.createElement("script"), { src, onload: resolve, onerror: reject });
    document.head.appendChild(el);
  });
}

async function captureScreen() {
  try {
    await loadScript("https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js");
    const canvas = await window.html2canvas(document.body, {
      backgroundColor: "#0A0C0B", scale: 1, logging: false, ignoreElements: (el) => el.id === "busy",
      height: Math.min(document.body.scrollHeight, 4000), windowWidth: document.documentElement.clientWidth,
    });
    return await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.7));
  } catch { return null; }  // no screenshot: the report still has everything else
}

async function openBugReport() {
  $("#busy-text").textContent = "Taking a screenshot…";
  $("#busy").hidden = false;
  bugShot = await captureScreen();
  $("#busy").hidden = true;
  $("#bug-result").innerHTML = "";
  $("#bug-form").hidden = false;
  $("#bug-shot").innerHTML = bugShot
    ? `<p class="small muted">Screenshot of the screen you were on (attached):</p><img class="shot" src="${URL.createObjectURL(bugShot)}" alt="">`
    : "";
  show("bug");
}

async function sendBugReport(e) {
  const f = e.target;
  const context = {
    actions: trail.actions,
    errors: trail.errors,
    device: { userAgent: navigator.userAgent, screen: `${screen.width}x${screen.height}@${devicePixelRatio}`,
      standalone: matchMedia("(display-mode: standalone)").matches, language: navigator.language },
    page: location.hash || "#home",
    receipt: f.receipt.checked && lastReceipt ? lastReceipt : null,
  };
  const fd = new FormData();
  fd.append("description", f.description.value.trim());
  fd.append("screen", lastScreen || "");
  fd.append("context", JSON.stringify(context));
  const own = f.screenshot.files[0];
  if (own || bugShot) fd.append("screenshot", own || bugShot, "screenshot.jpg");
  const r = await api("/bugs", { method: "POST", form: fd, busy: "Sending…" });
  f.reset();
  f.hidden = true;
  $("#bug-shot").innerHTML = "";
  $("#bug-result").innerHTML = `<div class="card"><h2>Thanks, saved as bug #${r.id}</h2>
    <p class="small muted">${r.github_url ? `Also filed on GitHub: <a href="${esc(r.github_url)}">${esc(r.github_url.split("/").slice(-2).join(" #"))}</a>.` : "It's stored in the companion."}
    Ask Claude to "fix the reported bugs" in your coding chat.</p>
    <button type="button" id="bug-back" class="secondary">Back to where I was</button></div>`;
  $("#bug-back").addEventListener("click", () => show(lastScreen || "home"));
}

// --- Claude usage --------------------------------------------------------------------------
const usd = (v) => (v > 0 && v < 0.001 ? "<$0.001" : `$${Number(v || 0).toFixed(v >= 1 || !v ? 2 : 3)}`);

function creditHtml(c) {
  if (!c) return `<p class="small muted">Enter your credit balance below to see what's left.</p>`;
  const used = Math.min(100, (c.spent_since_usd / (c.balance_usd || 1)) * 100);
  return `<div class="credit ${c.low ? "low" : ""}"><span class="small muted">Credit left (estimate)</span>
    <strong>${usd(c.remaining_usd)}</strong>
    <div class="bar"><i style="width:${100 - used}%"></i></div>
    <span class="small muted">${usd(c.balance_usd)} on ${esc(c.since.slice(0, 10))} − ${usd(c.spent_since_usd)} used since</span>
    ${c.low ? '<p class="small bad">Running low: top up at console.anthropic.com → Billing, then enter the new balance below.</p>' : ""}</div>`;
}

async function loadUsage() {
  const u = await api("/usage/summary");
  const max = Math.max(...u.days.map((d) => d.cost_usd), 0.0001);
  const bars = u.days.map((d) => `<div class="day" title="${d.date}: ${usd(d.cost_usd)}, ${d.calls} calls">
    <i style="height:${Math.max(d.cost_usd ? 4 : 0, (d.cost_usd / max) * 100)}%"></i></div>`).join("");
  $("#usage-body").innerHTML = `<div class="card">${creditHtml(u.credit)}
      <div class="stats"><div><b>${usd(u.today_usd)}</b><span>today</span></div>
        <div><b>${usd(u.month_usd)}</b><span>this month</span></div>
        <div><b>${u.calls_30_days}</b><span>calls, 30 days</span></div></div></div>
    <div class="card"><h2>Last 30 days <span class="badge">${usd(u.last_30_days_usd)}</span></h2>
      <div class="chart">${bars}</div>
      <div class="chart-axis small muted"><span>${u.days[0].date.slice(5)}</span><span>today</span></div></div>
    <div class="card"><h2>What it was used for</h2>${u.by_task.length ? `<table>
      <tr><th>Task</th><th class="num">Calls</th><th class="num">Each</th><th class="num">Total</th></tr>
      ${u.by_task.map((t) => `<tr><td>${esc(t.name)}</td><td class="num">${t.calls}</td><td class="num">${usd(t.avg_usd)}</td><td class="num">${usd(t.cost_usd)}</td></tr>`).join("")}
      </table>` : '<p class="muted">No Claude calls in the last 30 days.</p>'}</div>`;
}

async function loadCreditWarning() {
  try {
    const u = await api("/usage/summary");
    const c = u.credit;
    $("#credit-warn").hidden = !c?.low;
    if (c?.low) $("#credit-warn").innerHTML = `<strong>Claude credit low: about ${usd(c.remaining_usd)} left.</strong>
      <span class="small">Top up at console.anthropic.com, then update it in <a href="#" data-go="usage">Claude usage</a>.</span>`;
    $$("#credit-warn [data-go]").forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); show(b.dataset.go); }));
  } catch { /* not important enough to bother the user */ }
}

// --- wiring ------------------------------------------------------------------------------
function on(sel, ev, fn) {
  $(sel).addEventListener(ev, async (e) => {
    if (ev === "submit") e.preventDefault();
    try { await fn(e); } catch (err) { toast(err.message, 6000); }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  $$("[data-go]").forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); show(b.dataset.go); }));
  $("#back").addEventListener("click", () => {
    if (currentPanel === "bug") return show(lastScreen || "home");
    show(receiptCtx != null && ["scan", "label"].includes(currentPanel) ? "receipt" : "home");
  });
  $("#ctx-back").addEventListener("click", () => show("receipt"));
  wireReceipt();
  on("#credit-form", "submit", async (e) => {
    await api("/usage/credit", { method: "POST", json: { balance_usd: num(e.target.balance_usd.value) } });
    e.target.reset();
    toast("Balance saved");
    await loadUsage();
  });
  on("#bug-btn", "click", openBugReport);
  on("#bug-link", "click", (e) => { e.preventDefault(); return openBugReport(); });
  on("#bug-form", "submit", sendBugReport);
  $("#stock-q").addEventListener("input", renderStock);
  $("#stock-list").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-open]");
    if (!b) return;
    try {
      const r = await api(`/stock/${b.dataset.open}/open`, { method: "POST", busy: "Marking as opened…" });
      toast(r.message, 5000);
      stockItems = await api("/stock/all");
      renderStock();
    } catch (err) { toast(err.message, 6000); }
  });
  window.addEventListener("popstate", () => show(location.hash.slice(1) || "home"));

  on("#login-form", "submit", async (e) => {
    await api("/login", { method: "POST", json: { token: e.target.token.value } });
    await refreshLookups();
    show("home");
  });
  on("#logout", "click", async (e) => { e.preventDefault(); await api("/logout", { method: "POST" }); show("login"); });

  on("#scan-start", "click", startScan);
  on("#scan-stop", "click", stopScan);
  on("#manual-code", "submit", (e) => lookupCode(e.target.code.value.trim()));
  on("#add-stock", "submit", async (e) => {
    const body = formJson(e.target, ["product_id", "amount", "price_total"]);
    const r = await api("/stock/add", { method: "POST", json: body, busy: "Adding…" });
    toast(r.message);
    e.target.reset();
    e.target.hidden = true;
  });
  on("#due-photo", "change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("image", f);
    const r = await api("/ocr/due-date", { method: "POST", form: fd, busy: "Reading date…" });
    if (r.date) { $("#add-stock [name=best_before]").value = r.date; toast(`Date: ${r.date} (${r.confidence})`); } else toast("Could not read a date");
    e.target.value = "";
  });

  on("#label-form", "submit", async (e) => {
    const f = e.target;
    const fd = new FormData();
    fd.append("images", f.img1.files[0]);
    if (f.barcode.value) fd.append("barcode", f.barcode.value);
    if (num(f.net_amount.value) > 0) { fd.append("net_amount", String(num(f.net_amount.value))); fd.append("net_unit", f.net_unit.value); }
    const r = await api(`/products/${f.product_id.value}/label-photo`, { method: "POST", form: fd, busy: "Reading label…" });
    if (!r.pack_size && ["g", "ml"].includes(r.product.stock_unit)) r.warnings.push("Pack size isn't printed on the label: type it in \"Pack size\" next time, or on the receipt line.");
    if (receiptCtx != null) {
      f.reset();
      if (r.warnings.length) toast(r.warnings.join(" "), 6000);
      return assignToLine(r.product);
    }
    const n = r.product.nutrition_per_100 || {};
    $("#label-result").innerHTML = `<div class="card"><h2>${esc(r.product.name)} <span class="badge">${esc(r.status)}</span></h2>
      <p class="small">Per 100: ${n.kcal ?? "?"} kcal · P ${n.protein ?? "?"} · F ${n.fat ?? "?"} · C ${n.carbs ?? "?"} · salt ${n.salt ?? "?"}</p>
      <p class="small">Suggested: keep in <strong>${esc(r.suggested.storage_location)}</strong>, use within
      <strong>${r.suggested.shelf_life_after_opening_days} days</strong> after opening.</p>
      ${r.warnings.length ? `<p class="small warn">${r.warnings.map(esc).join("<br>")}</p>` : ""}</div>`;
    f.reset();
    refreshLookups();
  });

  on("#manual-start", "click", () => startList("manual"));
  on("#first-step-go", "click", () => startHomeStock().catch((err) => toast(err.message, 6000)));
  on("#first-step-skip", "click", async (e) => {
    e.preventDefault();
    if (!confirm("Skip the first step? Grocy then only knows what you add from now on.")) return;
    await api("/setup/home-stock", { method: "POST", json: { done: true } });
    lookups.home_stock_done = true;
    loadFirstStep();
  });
  on("#receipt-form", "submit", async (e) => {
    const fd = new FormData();
    fd.append("image", e.target.image.files[0]);
    fd.append("commit", "false");
    let r;
    try {
      r = await api("/receipts/photo", { method: "POST", form: fd, busy: "Reading receipt… (up to a minute)" });
    } catch (err) {
      e.target.reset();
      $("#receipt-result").innerHTML = `<div class="card warn-card"><p><b>Couldn't read this receipt</b><br>${esc(err.message)}</p>
        <p>Try a sharper, flatter photo, or enter the items yourself.</p>
        <button data-act="manual">Enter the items manually</button></div>`;
      return;
    }
    if (!r.lines?.length) r.lines = [];
    lastReceipt = { mode: "receipt", ...r };
    renderReceipt();
    e.target.reset();
  });
  on("#produce-form", "submit", async (e) => {
    const body = formJson(e.target, ["weight_g", "count", "price_eur"]);
    const r = await api("/products/produce", { method: "POST", json: body, busy: "Adding produce…" });
    $("#produce-result").innerHTML = `<div class="card">${esc(r.message)}<br><span class="small muted">${r.status === "created" ? (r.product.nutrition_source ? `New product, nutrition from ${r.product.nutrition_source === "fineli" ? "Fineli (THL)" : "USDA"}.` : "New product, no nutrition match found.") : ""}</span></div>`;
    e.target.reset();
    $("#produce-form [name=store]").value = "turgus";
    refreshLookups();
  });

  on("#weigh-form", "submit", async (e) => {
    const f = e.target;
    const body = { grams_measured: num(f.grams_measured.value), includes_container: f.includes_container.checked };
    if (f.container_g.value) body.container_g = num(f.container_g.value);
    const r = await api(`/stock/${f.product_id.value}/weigh`, { method: "POST", json: body, busy: "Saving…" });
    $("#weigh-result").innerHTML = `<div class="card">${esc(r.message)}${r.notes?.length ? `<br><span class="small muted">${r.notes.map(esc).join(" ")}</span>` : ""}</div>`;
  });

  on("#cook-pick", "submit", async (e) => {
    const f = e.target;
    const q = f.servings.value ? `?servings=${Number(f.servings.value)}` : "";
    renderRecipeCheck(await api(`/recipes/${f.recipe_id.value}/check${q}`, { busy: "Checking stock…" }));
  });
  on("#cook-import", "submit", async (e) => {
    const f = e.target;
    const fd = new FormData();
    for (const k of ["url", "text", "servings"]) if (f[k].value.trim()) fd.append(k, f[k].value.trim());
    if (f.file.files[0]) fd.append("file", f.file.files[0]);
    if (!f.url.value.trim() && !f.text.value.trim() && !f.file.files[0]) return toast("Give a link, the text or a photo");
    const r = await api("/recipes/import", { method: "POST", form: fd, busy: "Reading the recipe… (up to a minute)" });
    f.reset();
    await refreshLookups();
    renderRecipeCheck(r);
  });
  $("#cook-result").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    try {
      const id = b.dataset.id;
      if (b.dataset.act === "shop") {
        const r = await api(`/recipes/${id}/shopping-list`, { method: "POST", busy: "Adding…" });
        toast(r.message);
      }
      if (b.dataset.act === "cook") {
        if (!confirm("Cook now? The ingredients are taken out of stock.")) return;
        const r = await api(`/recipes/${id}/cook`, { method: "POST", json: { servings: Number(b.dataset.servings) }, busy: "Cooking…" });
        $("#cook-result").innerHTML = `<div class="card"><h2>Cooked: ${esc(r.recipe.name)}</h2>
          ${r.notices.length ? list(r.notices.map(esc)) : "<p>Ingredients taken out of stock.</p>"}</div>`;
      }
    } catch (err) { toast(err.message, 6000); }
  });

  on("#due-refresh", "click", loadDue);

  on("#plan-form", "submit", async (e) => {
    const f = e.target;
    const fd = new FormData();
    if (f.file.files[0]) fd.append("file", f.file.files[0]);
    for (const k of ["text", "week_start", "kcal_target", "budget_eur", "people", "preferences"]) {
      if (f[k].value) fd.append(k, k === "budget_eur" ? String(num(f[k].value)) : f[k].value);
    }
    fd.append("dry_run", f.dry_run.checked ? "true" : "false");
    const r = await api("/mealplan/generate", { method: "POST", form: fd, busy: "Planning the week… (up to a few minutes)" });
    const s = r.summary;
    let html = `<div class="card"><h2>Week of ${esc(s.week_start)} ${r.dry_run ? '<span class="badge">preview</span>' : '<span class="badge good">written to Grocy</span>'}</h2>
      <p>Estimated cost: <strong>${eur(s.week_cost_eur)}</strong></p>`;
    for (const d of s.days) {
      html += `<h3>${esc(d.date)} · ${eur(d.cost_eur)} · ${d.nutrition_per_person.kcal} kcal/person</h3>`;
      html += list(d.meals.map((m) => `<strong>${esc(m.slot)}</strong>: ${esc(m.recipe)} <span class="small muted">${eur(m.cost_eur)}</span>
        ${m.uses_waste_risk.length ? `<br><span class="small good">uses up: ${esc(m.uses_waste_risk.join(", "))}</span>` : ""}`));
    }
    const shop = s.shopping_delta_grocy || s.shopping_delta;
    html += `<h3>Shopping list ${s.shopping_delta_grocy ? "(added in Grocy)" : "(to buy)"}</h3>` +
      (shop.length ? list(shop.map((i) => `${esc(i.name)} <span class="muted small">${i.amount} ${esc(i.unit)}</span>`)) : "<p>Nothing to buy.</p>");
    if (s.prep_schedule?.length) html += `<h3>Prep</h3>${list(s.prep_schedule.map(esc))}`;
    if (s.notes) html += `<p class="small muted">${esc(s.notes)}</p>`;
    if (s.grocy?.warnings?.length) html += `<p class="small warn">${s.grocy.warnings.map(esc).join("<br>")}</p>`;
    $("#plan-result").innerHTML = html + "</div>";
  });

  on("#report-form", "submit", async (e) => {
    const start = e.target.start.value;
    const r = await api(`/reports/week${start ? `?start=${start}` : ""}`, { busy: "Building report…" });
    let html = `<div class="card"><h2>${esc(r.week_start)} – ${esc(r.week_end)}</h2>
      <p>Meal plan cost: <strong>${eur(r.week.cost_eur)}</strong> (${eur(r.week.cost_per_person_per_day_eur)} per person/day)<br>
      Per person/day: ${nutritionLine(r.week.nutrition_per_person_per_day)}</p>
      <table><tr><th>Day</th><th class="num">kcal/p</th><th class="num">P</th><th class="num">€</th></tr>
      ${r.days.map((d) => `<tr><td>${esc(d.date.slice(5))}<br><span class="small muted">${d.meals.map((m) => esc(m.name)).join(", ")}</span></td>
        <td class="num">${d.nutrition_per_person.kcal}</td><td class="num">${d.nutrition_per_person.protein}</td><td class="num">${eur(d.cost_eur)}</td></tr>`).join("")}
      </table></div>
      <div class="card"><h2>Stock used ${eur(r.consumed_value_eur)} · wasted <span class="${r.wasted_value_eur > 0 ? "bad" : ""}">${eur(r.wasted_value_eur)}</span></h2>
      ${r.stock_wasted.length ? `<h3>Wasted</h3>${list(r.stock_wasted.map((w) => `${esc(w.name)} <span class="small muted">${w.amount} ${esc(w.unit)} · ${eur(w.value_eur)}</span>`))}` : ""}
      ${r.stock_consumed.length ? `<h3>Consumed</h3>${list(r.stock_consumed.slice(0, 25).map((w) => `${esc(w.name)} <span class="small muted">${w.amount} ${esc(w.unit)} · ${eur(w.value_eur)}</span>`))}` : '<p class="muted">Nothing consumed from stock this week.</p>'}</div>`;
    $("#report-result").innerHTML = html;
  });

  // Monday of this week as default report start
  const now = new Date();
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
  $("#report-form [name=start]").value = monday.toLocaleDateString("sv-SE");

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

  try {
    await refreshLookups();
    show(location.hash.slice(1) || "home");
  } catch {
    show("login");
  }
});
