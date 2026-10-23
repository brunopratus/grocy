"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = (v) => (v == null ? "–" : `€${Number(v).toFixed(2)}`);

const TITLES = {
  home: "Grocy+", scan: "Scan barcode", label: "Nutrition label", produce: "Market produce", weigh: "Weigh item",
  cook: "Cook recipe", due: "Open / due", receipt: "Shop receipt", plan: "Week plan", report: "Weekly report", events: "History", login: "Grocy+",
};
let lookups = { products: [], recipes: [], stores: [], locations: [] };

// --- plumbing -----------------------------------------------------------------
function toast(msg, ms = 3500) {
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
    const res = await fetch(path, opts);
    if (res.status === 401) { show("login"); throw new Error("Please log in"); }
    const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
    if (!res.ok) throw new Error(data?.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
    return data;
  } finally {
    if (busy) $("#busy").hidden = true;
  }
}

function show(id) {
  $$(".panel").forEach((p) => (p.hidden = p.id !== id));
  $("#title").innerHTML = id === "home" || id === "login" ? 'Grocy<span class="plus">+</span>' : esc(TITLES[id]);
  $("#back").hidden = id === "home" || id === "login";
  if (id !== "scan") stopScan();
  if (id === "due") loadDue();
  if (id === "events") loadEvents();
  if (id === "home") loadHomeDue();
  if (location.hash !== `#${id}` && id !== "login") history.pushState(null, "", `#${id}`);
}

function formJson(form, numeric = []) {
  const out = {};
  for (const [k, v] of new FormData(form).entries()) {
    if (v instanceof File || v === "") continue;
    out[k] = numeric.includes(k) ? Number(v) : v;
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
      (result) => { if (result) { const code = result.getText(); stopScan(); navigator.vibrate?.(80); lookupCode(code); } },
    );
  } catch (e) { toast(`Camera: ${e.message}`); }
}
function stopScan() { if (scanControls) { scanControls.stop(); scanControls = null; } }

async function lookupCode(code) {
  const r = await api(`/products/barcode/${encodeURIComponent(code)}`, { method: "POST", busy: "Looking up…" });
  const p = r.product;
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

// --- receipt -------------------------------------------------------------------------------
const RECEIPT_UNITS = ["piece", "pack", "g", "kg", "ml", "l"];
let lastReceipt = null;

function renderReceipt(r) {
  lastReceipt = r;
  const opts = (sel) => `<option value="">Skip</option>` + lookups.products
    .map((p) => `<option value="${p.id}"${p.id === sel ? " selected" : ""}>${esc(p.name)}</option>`).join("");
  const rows = r.lines.map((l, i) => `<li class="rline${l.is_food ? "" : " nonfood"}" data-i="${i}">
      <div class="rtop"><strong>${esc(l.text)}</strong><span class="num">${eur(l.line_total_eur)}</span></div>
      <div class="small muted">${esc(l.name_en)} · ${l.quantity} ${esc(l.unit)}${l.is_food ? "" : ' · <span class="badge">not food</span>'}</div>
      <select class="rprod" aria-label="Grocy product">${opts(l.product_id)}</select></li>`).join("");
  const sum = r.lines.reduce((a, l) => a + (l.line_total_eur || 0), 0);
  $("#receipt-result").innerHTML = `<div class="card">
    <h2>${esc(r.store || "Receipt")} ${r.date ? `<span class="badge">${esc(r.date)}</span>` : ""}</h2>
    <p class="small muted">Receipt total ${eur(r.total_eur)} · lines ${eur(sum)}. Pick the Grocy product for each line,
      or Skip. Products not in Grocy yet: scan their barcode first.</p>
    <ul class="list">${rows}</ul>
    <button id="receipt-commit" type="button">Add chosen lines to stock</button></div>`;
  on("#receipt-commit", "click", commitReceipt);
}

async function commitReceipt() {
  const lines = $$("#receipt-result .rline").map((li) => {
    const l = lastReceipt.lines[Number(li.dataset.i)];
    const id = Number(li.querySelector(".rprod").value);
    return id ? { product_id: id, quantity: l.quantity, unit: l.unit, line_total_eur: l.line_total_eur } : null;
  }).filter(Boolean);
  if (!lines.length) return toast("Choose a Grocy product for at least one line");
  const r = await api("/receipts/commit", { method: "POST", json: { store: lastReceipt.store, lines }, busy: "Adding to stock…" });
  const errors = r.lines.filter((l) => l.error);
  $("#receipt-result").innerHTML = `<div class="card"><h2>${r.added} line(s) added to stock</h2>
    ${list(r.lines.map((l) => l.added ? esc(l.added) : `<span class="bad">${esc(l.error)}</span>`))}</div>`;
  if (!errors.length) toast("Receipt added to stock");
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
  $("#back").addEventListener("click", () => show("home"));
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
    if (f.img2.files[0]) fd.append("images", f.img2.files[0]);
    if (f.barcode.value) fd.append("barcode", f.barcode.value);
    const r = await api(`/products/${f.product_id.value}/label-photo`, { method: "POST", form: fd, busy: "Reading label…" });
    const n = r.product.nutrition_per_100 || {};
    $("#label-result").innerHTML = `<div class="card"><h2>${esc(r.product.name)} <span class="badge">${esc(r.status)}</span></h2>
      <p class="small">Per 100: ${n.kcal ?? "?"} kcal · P ${n.protein ?? "?"} · F ${n.fat ?? "?"} · C ${n.carbs ?? "?"} · salt ${n.salt ?? "?"}</p>
      <p class="small">Suggested: keep in <strong>${esc(r.suggested.storage_location)}</strong>, use within
      <strong>${r.suggested.shelf_life_after_opening_days} days</strong> after opening.</p>
      ${r.warnings.length ? `<p class="small warn">${r.warnings.map(esc).join("<br>")}</p>` : ""}</div>`;
    f.reset();
    refreshLookups();
  });

  on("#receipt-form", "submit", async (e) => {
    const fd = new FormData();
    fd.append("image", e.target.image.files[0]);
    fd.append("commit", "false");
    const r = await api("/receipts/photo", { method: "POST", form: fd, busy: "Reading receipt… (up to a minute)" });
    renderReceipt(r);
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
    const body = { grams_measured: Number(f.grams_measured.value), includes_container: f.includes_container.checked };
    if (f.container_g.value) body.container_g = Number(f.container_g.value);
    const r = await api(`/stock/${f.product_id.value}/weigh`, { method: "POST", json: body, busy: "Saving…" });
    $("#weigh-result").innerHTML = `<div class="card">${esc(r.message)}${r.notes?.length ? `<br><span class="small muted">${r.notes.map(esc).join(" ")}</span>` : ""}</div>`;
  });

  on("#cook-nutrition", "click", async () => {
    const id = $("#cook-form [name=recipe_id]").value;
    const r = await api(`/recipes/${id}/nutrition`, { busy: "Calculating…" });
    $("#cook-result").innerHTML = `<div class="card"><h2>${esc(r.recipe.name)} · ${r.servings} servings</h2>
      <p>Per serving: ${nutritionLine(r.per_serving)}<br>Cost per serving: ${eur(r.cost_per_serving_eur)}</p>
      ${list(r.ingredients.map((i) => `${esc(i.name)} <span class="muted small">${i.amount} ${esc(i.unit)} · ${i.kcal} kcal</span>`))}</div>`;
  });
  on("#cook-form", "submit", async (e) => {
    const f = e.target;
    const body = f.servings.value ? { servings: Number(f.servings.value) } : {};
    const r = await api(`/recipes/${f.recipe_id.value}/cook`, { method: "POST", json: body, busy: "Cooking…" });
    $("#cook-result").innerHTML = `<div class="card"><h2>${esc(r.recipe.name)} · ${r.servings} servings</h2>
      ${r.notices.length ? list(r.notices.map(esc)) : "<p>No partly used packs.</p>"}
      <p>Per serving: ${nutritionLine(r.nutrition_per_serving)}<br>Cost per serving: ${eur(r.cost_per_serving_eur)}${r.cost_complete ? "" : " (some prices unknown)"}</p></div>`;
  });

  on("#due-refresh", "click", loadDue);

  on("#plan-form", "submit", async (e) => {
    const f = e.target;
    const fd = new FormData();
    if (f.file.files[0]) fd.append("file", f.file.files[0]);
    for (const k of ["text", "week_start", "kcal_target", "budget_eur", "people", "preferences"]) if (f[k].value) fd.append(k, f[k].value);
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
