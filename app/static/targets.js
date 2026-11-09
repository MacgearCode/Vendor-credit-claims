"use strict";
// Targets tool (Reporting Site /targets): the standalone Targets app's front end, API under
// /targets/api. Light/dark and the colour palette come from the site shell.

// ---------- state ----------
let scenarios = [];
let sid = null;
let payload = null;           // {scenario, inputs, countries[]}
let growthOverrides = [];     // [{level,country,brand,customer,growth_pct}]
let pacingOverrides = [];     // [{level,country,brand,weights[12]}]
let brandExclusions = [];     // [{country,brand}]
let annualizeBrands = [];     // [{country,brand}] — partial-year brands scaled to a full year
let manualLines = [];         // [{country,brand,customer,target_annual}]
let projMode = null;          // null | 'flat' | 'tracking' — fill un-shipped months from a reference year
let projRef = null;           // reference dataset id for the projection
let projUnion = false;        // also include reference-year-only lines
let activeCountry = 0;
const expanded = {};          // `${country}|${brand}` -> bool
let sortState = { key: "plan", dir: "desc" };   // default: biggest plan first
let periodView = (() => { try { return localStorage.getItem("targets-period") || "monthly"; } catch (e) { return "monthly"; } })();
// a part-year scenario shows only its own months unless "Show full year" is on
let showFull = (() => { try { return localStorage.getItem("targets-showfull") === "1"; } catch (e) { return false; } })();
const H1_IDX = [0, 1, 2, 3, 4, 5];   // Apr–Sep
const FY_LABELS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"];
// A scenario plans a period of the year (full year, a half, a quarter, or custom months);
// commit replaces only those months of the official targets.
const PERIOD_PRESETS = [
  { value: "0-11", label: "Full year (Apr–Mar)" }, { value: "0-5", label: "H1 (Apr–Sep)" },
  { value: "6-11", label: "H2 (Oct–Mar)" }, { value: "0-2", label: "Q1 (Apr–Jun)" },
  { value: "3-5", label: "Q2 (Jul–Sep)" }, { value: "6-8", label: "Q3 (Oct–Dec)" },
  { value: "9-11", label: "Q4 (Jan–Mar)" }, { value: "custom", label: "Custom months…" },
];
const scenPeriod = () => (payload && payload.period) || { start: 0, end: 11, label: "Full year", months: [...Array(12).keys()] };
const isFullYear = () => { const p = scenPeriod(); return p.start === 0 && p.end === 11; };
const inPeriod = (i) => { const p = scenPeriod(); return i >= p.start && i <= p.end; };
async function askPeriod(current) {
  const r = await uiForm({
    title: "Period to plan",
    hint: "Only these months are planned and committed; the rest of the year keeps its official targets.",
    fields: [{ name: "p", label: "Period", type: "select", options: PERIOD_PRESETS, value: current || "0-11" }],
    okText: "Next",
  });
  if (!r) return null;
  if (r.p !== "custom") { const [a, b] = r.p.split("-").map(Number); return { start: a, end: b }; }
  const months = FY_LABELS.map((m, i) => ({ value: i, label: m }));
  const c = await uiForm({
    title: "Custom period",
    fields: [{ name: "a", label: "From", type: "select", options: months, value: 0 },
             { name: "b", label: "To", type: "select", options: months, value: 11 }],
    okText: "Use these months",
  });
  if (!c) return null;
  const a = Number(c.a), b = Number(c.b);
  if (b < a) { await uiAlert("The period must run forward within the year (April to March).", { title: "Period" }); return null; }
  return { start: a, end: b };
}
let saveTimer = null;
let pacingCtx = null;         // {level,country,brand,seasonality[12]}

const MONEY = new Intl.NumberFormat("en-AU", { maximumFractionDigits: 0 });
const money = (n) => "$" + MONEY.format(Math.round(n || 0));
const pctStr = (f) => (f >= 0 ? "+" : "") + (f * 100).toFixed(1) + "%";

// ---------- api ----------
async function api(method, url, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  if (!r.ok) {
    const text = await r.text();
    let msg = text || r.statusText;
    try { const j = JSON.parse(text); if (j && j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (e) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

// ---------- load / select ----------
async function loadScenarios(selectId) {
  scenarios = await api("GET", "/targets/api/scenarios");
  const sel = document.getElementById("scenarioSelect");
  sel.innerHTML = "";
  scenarios.forEach((s) => {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = `${s.name} — ${s.target_fy} ${s.period_label === "Full year" ? "full year" : s.period_label}`;
    sel.appendChild(o);
  });
  if (!scenarios.length) { payload = null; renderEmpty(); return; }
  const target = selectId || (scenarios.some((s) => s.id === sid) ? sid : scenarios[0].id);
  sel.value = target;
  await selectScenario(target);
}

async function selectScenario(id) {
  sid = Number(id);
  applyPayload(await api("GET", `/targets/api/scenarios/${sid}`));
}

function applyPayload(p) {
  payload = p;
  growthOverrides = p.inputs.growth_overrides.map((g) => ({ ...g }));
  pacingOverrides = p.inputs.pacing_overrides.map((g) => ({ ...g, weights: [...g.weights] }));
  brandExclusions = (p.inputs.brand_exclusions || []).map((e) => ({ ...e }));
  annualizeBrands = (p.inputs.annualize || []).map((a) => ({ ...a }));
  manualLines = (p.inputs.manual_lines || []).map((m) => ({ ...m }));
  projMode = p.scenario.projection_mode || null;
  if (projMode === "grossup") projMode = "flat";   // legacy mode → new default
  projRef = p.scenario.ref_actuals_id || null;
  projUnion = !!p.scenario.projection_union;
  document.getElementById("globalGrowth").value = (p.scenario.default_growth_pct * 100).toFixed(1);
  render();
}

// ---------- save (debounced) ----------
function queueSave() {
  clearTimeout(saveTimer);
  const chip = document.getElementById("statusChip");
  if (chip) { chip.textContent = "Saving…"; chip.className = "tg-status saving"; }
  saveTimer = setTimeout(save, 300);
}
// "Draft — not committed" / "Committed Q4 · 23 Oct 2026" / "… · changed since"
function renderStatus(justSaved) {
  const chip = document.getElementById("statusChip");
  if (!chip || !payload) return;
  const st = payload.status || {};
  const c = st.committed;
  let txt, cls = "tg-status";
  if (!c) { txt = "Draft — not committed"; }
  else if (st.edited_since) { txt = `Committed ${c.period_label} · ${c.committed_at.slice(0, 10)} · changed since`; cls += " changed"; }
  else { txt = `Committed ${c.period_label} · ${c.committed_at.slice(0, 10)}`; cls += " done"; }
  chip.textContent = (justSaved ? "Saved · " : "") + txt;
  chip.className = cls;
}
async function save() {
  if (!sid) return;
  const body = {
    default_growth_pct: parseFloat(document.getElementById("globalGrowth").value || "0") / 100,
    growth_overrides: growthOverrides,
    pacing_overrides: pacingOverrides,
    brand_exclusions: brandExclusions,
    annualize: annualizeBrands,
    manual_lines: manualLines,
    projection_mode: projMode || "none",
    ref_actuals_id: projRef,
    projection_union: projUnion,
  };
  applyPayload(await api("PUT", `/targets/api/scenarios/${sid}`, body));
  renderStatus(true);
}

// ---------- override helpers ----------
function setGrowth(level, country, brand, customer, pctValue) {
  const key = (o) => o.level === level && o.country === country && o.brand === brand &&
    (level === "brand" ? true : o.customer === customer);
  growthOverrides = growthOverrides.filter((o) => !key(o));
  if (pctValue !== "" && pctValue !== null && !Number.isNaN(pctValue)) {
    growthOverrides.push({ level, country, brand, customer: level === "brand" ? "" : customer,
      growth_pct: pctValue / 100 });
  }
  queueSave();
}

// ---------- rendering ----------
function render() {
  if (!payload) return renderEmpty();
  renderTotals();
  renderTabs();
  renderGrid();
  updateExcludedButton();
  updateProjectionButton();
}

function updateExcludedButton() {
  const n = brandExclusions.filter((e) => e.country === curCountry()).length;
  const btn = document.getElementById("excludedBtn");
  document.getElementById("exclCount").textContent = `(${n})`;
  btn.classList.toggle("hidden", n === 0);
}

function updateProjectionButton() {
  const btn = document.getElementById("projectionBtn");
  const partial = !!(payload.dataset && payload.dataset.is_partial);
  const on = !!payload.projection;
  // Show whenever the dataset is partial, or projection is already on (so it can be turned off).
  btn.classList.toggle("hidden", !(partial || on));
  btn.classList.toggle("active", on);
  btn.textContent = on ? "🔮 Projection settings (on)…" : "🔮 Projection settings…";
}

// Summarise a set of fiscal-month indices, e.g. [0..5] -> "Apr–Sep (6 mo)".
function coveredLabel(mp) {
  if (!mp || !mp.length) return "no months";
  if (mp.length >= 12) return "full year";
  return `${FY_LABELS[mp[0]]}–${FY_LABELS[mp[mp.length - 1]]} (${mp.length} mo)`;
}

async function openProjection() {
  if (!payload || !payload.dataset) return;
  const ds = payload.dataset;
  const datasets = await api("GET", "/targets/api/actuals");
  const refOpts = datasets.filter((d) => d.id !== ds.id);
  if (!refOpts.length) {
    return uiAlert("Projection needs a reference year to borrow seasonality from — there are no earlier NetSuite years in the database.",
      { title: "No reference dataset" });
  }
  const full = refOpts.find((d) => !d.is_partial);
  const defRef = projRef || (full ? full.id : refOpts[0].id);
  const refFieldOpts = refOpts.map((d) => ({
    value: d.id,
    label: `${d.name}${d.is_partial ? " (partial)" : ""}${d.period_start ? ` · ${d.period_start}–${d.period_end}` : ""}`,
  }));
  const r = await uiForm({
    title: "Projection",
    hint: `${ds.name} covers ${coveredLabel(ds.months_present)}. Those months stay as actuals; the rest of the year is filled from a reference year. Global growth % applies to the filled months only.`,
    fields: [
      { name: "mode", label: "Fill the rest of the year with", type: "select", value: projMode || "none",
        options: [
          { value: "none", label: "Off (use the months so far as the whole year)" },
          { value: "flat", label: "Last year + growth%" },
          { value: "tracking", label: "Last year, scaled by how you're tracking" },
        ] },
      { name: "ref", label: "Reference year", type: "select",
        value: defRef, options: refFieldOpts },
      { name: "scope", label: "Lines to show", type: "select", value: projUnion ? "union" : "upload",
        options: [
          { value: "upload", label: "Only lines with sales this year" },
          { value: "union", label: "Also include reference-year lines (forecast their rest-of-year)" },
        ] },
    ],
    okText: "Apply",
  });
  if (!r) return;
  if (r.mode === "none") { projMode = null; projRef = null; projUnion = false; }
  else { projMode = r.mode; projRef = Number(r.ref); projUnion = r.scope === "union"; }
  queueSave();
}

function renderEmpty() {
  document.getElementById("totalsStrip").innerHTML =
    '<div class="stat"><div class="label">No scenarios</div><div class="val">—</div></div>';
  document.getElementById("countryTabs").innerHTML = "";
  document.getElementById("gridHead").innerHTML = "";
  document.getElementById("gridBody").innerHTML =
    '<tr><td style="padding:40px;color:#6b7684">Create a scenario to begin.</td></tr>';
}

function grandTotals() {
  let base = 0, target = 0;
  payload.countries.forEach((c) => { base += c.totals.baseline_annual; target += c.totals.target_annual; });
  return { base, target, delta: target - base, pct: base ? (target - base) / base : 0 };
}

// Short label for the actuals dataset the current scenario is built on.
function baseLabel() {
  const d = payload && payload.dataset;
  return (d && (d.source_fy || d.name)) || "baseline";
}

// What the target is compared against: the reference year when projecting, else the dataset.
function compareLabel() {
  const pr = payload && payload.projection;
  if (pr && pr.ref_dataset) return pr.ref_dataset.source_fy || pr.ref_dataset.name;
  return baseLabel();
}

function renderPeriodBtn() {
  const b = document.getElementById("periodBtn");
  if (b) b.textContent = "Period: " + scenPeriod().label;
}

function renderTotals() {
  renderPeriodBtn();
  renderStatus(false);
  document.getElementById("fullYearToggle").classList.toggle("hidden", isFullYear());
  document.getElementById("fullYearToggle").classList.toggle("on", showFull);
  const ci = cmpInfo(), p = scenPeriod();
  const cards = [];
  payload.countries.forEach((c) => {
    const cm = c.totals.cmp || {}, plan = bucketSum(c.totals.target_months, p.months);
    const v = vsPct(plan, cm.ly);
    const bits = [`vs ${ci.ly_label || "LY"} ${money(cm.ly)}: ${v == null ? "—" : pctStr(v)}`];
    if (ci.has_official) bits.push(`official now ${money(cm.official)}`);
    if (ci.trend && cm.ytd_ly) bits.push(`trend ${ci.trend.label} ${pctStr(vsPct(cm.ytd, cm.ytd_ly))}`);
    cards.push({ label: `${c.country} · plan ${planLabel()}`, val: money(plan), delta: bits.join(" · "),
      cls: v == null ? "" : (v >= 0 ? "pos" : "neg") });
  });
  const st = payload.start || {};
  cards.push({ label: "Starting point", val: esc(st.label || baseLabel()),
    delta: `growth ${pctStr(payload.scenario.default_growth_pct)} unless set on a brand or retailer`, cls: "" });
  if (payload.projection) {
    const pr = payload.projection;
    const refName = pr.ref_dataset ? (pr.ref_dataset.source_fy || pr.ref_dataset.name) : "—";
    const warn = pr.new_lines ? ` · ⚠ ${pr.new_lines} new` : "";
    const scope = pr.union ? "all lines" : "lines with sales this year";
    cards.push({ label: "Projection",
      val: pr.mode === "tracking" ? "Rest: tracking" : "Rest: last yr",
      delta: `actuals + ${refName} · ${scope}${warn}`, cls: "" });
  }
  document.getElementById("totalsStrip").innerHTML = cards.map((c) =>
    `<div class="stat"><div class="label">${c.label}</div><div class="val">${c.val}</div>
     <div class="delta ${c.cls}">${c.delta}</div></div>`).join("");
}

function renderTabs() {
  document.getElementById("countryTabs").innerHTML = payload.countries.map((c, i) =>
    `<button class="${i === activeCountry ? "active" : ""}" data-i="${i}">${c.country}</button>`).join("");
  document.querySelectorAll("#countryTabs button").forEach((b) =>
    b.onclick = () => { activeCountry = Number(b.dataset.i); render(); });
}

function arrow(key) {
  return sortState.key === key
    ? `<span class="sort-arrow">${sortState.dir === "desc" ? "▼" : "▲"}</span>` : "";
}

function th(key, label, cls) {
  return `<th class="sortable ${cls || ""}" data-sort="${key}">${label}${arrow(key)}</th>`;
}

// ---------- period columns (Monthly / Quarterly / Half-year) ----------
function periodCols() {
  const months = payload.countries[activeCountry].months;   // 12 labels Apr..Mar
  let cols;
  if (periodView === "quarterly") {
    cols = [{ label: "Q1", idxs: [0, 1, 2] }, { label: "Q2", idxs: [3, 4, 5] },
            { label: "Q3", idxs: [6, 7, 8] }, { label: "Q4", idxs: [9, 10, 11] }];
  } else if (periodView === "half") {
    cols = [{ label: "H1", idxs: [0, 1, 2, 3, 4, 5] }, { label: "H2", idxs: [6, 7, 8, 9, 10, 11] }];
  } else {
    cols = months.map((m, i) => ({ label: m, idxs: [i] }));
  }
  if (!isFullYear() && !showFull) cols = cols.filter((c) => c.idxs.some(inPeriod));
  return cols;
}

const bucketSum = (arr, idxs) => idxs.reduce((s, i) => s + (arr[i] || 0), 0);
// only draw the H1|H2 divider in monthly view, on the first H2 column (Oct = index 6)
const dividerCls = (i) => (periodView === "monthly" && i === 6) ? "h2-start" : "";

// a column is "outside" when none of its months are in the scenario's period
const outCls = (c) => c.idxs.some(inPeriod) ? "" : " out-period";
function periodHeaders() {
  return periodCols().map((c, i) => th("per:" + i, c.label, dividerCls(i) + outCls(c))).join("");
}
function h1Header() {
  return (isFullYear() && periodView !== "half") ? th("h1", "H1 (Apr–Sep)") : "";
}
// ---- the right-hand block: the plan for the period, then what to compare it with ----
const cmpInfo = () => (payload && payload.compare) || {};
const planLabel = () => isFullYear() ? payload.scenario.target_fy : `${scenPeriod().label} ${payload.scenario.target_fy}`;
function planHeaders() {
  const ci = cmpInfo();
  let h = th("plan", `Plan<br>${esc(planLabel())}`, "plan");
  h += th("ly", `${esc(ci.ly_label || "LY")}<br>same months`, "cmp cmp-first");
  h += th("vsly", "vs LY", "cmp");
  if (ci.has_official) h += th("off", "Official<br>now", "cmp");
  if (ci.trend) h += th("trend", `Trend<br>${esc(ci.trend.label)}`, "cmp");
  return h;
}
const vsPct = (a, b) => (b ? (a - b) / Math.abs(b) : null);
function pctCell(v, extra) {
  if (v == null) return `<td class="cmp muted ${extra || ""}">—</td>`;
  return `<td class="cmp ${v >= 0 ? "pos" : "neg"} ${extra || ""}">${pctStr(v)}</td>`;
}
function planCells(row, planCellHtml) {
  const ci = cmpInfo(), cm = row.cmp || {};
  const plan = bucketSum(row.target_months, scenPeriod().months);
  let h = planCellHtml || `<td class="plan">${money(plan)}</td>`;
  h += `<td class="cmp cmp-first">${money(cm.ly)}</td>` + pctCell(vsPct(plan, cm.ly));
  if (ci.has_official) h += `<td class="cmp">${cm.official ? money(cm.official) : '<span class="muted">—</span>'}</td>`;
  if (ci.trend) h += pctCell(cm.ytd_ly ? vsPct(cm.ytd, cm.ytd_ly) : null);
  return h;
}
function periodCells(monthsArr) {
  return periodCols().map((c, i) =>
    `<td class="${dividerCls(i)}${outCls(c)}">${money(bucketSum(monthsArr, c.idxs))}</td>`).join("");
}
function h1Cell(monthsArr) {
  return (isFullYear() && periodView !== "half") ? `<td>${money(bucketSum(monthsArr, H1_IDX))}</td>` : "";
}

// numeric value for a brand/customer row under a given sort key
function sortNum(row, key) {
  if (key === "annual") return row.target_annual;
  if (key === "base") return row.baseline_annual;
  if (key === "growth") return row.growth_pct;
  if (key === "h1") return bucketSum(row.target_months, H1_IDX);
  const cm = row.cmp || {}, plan = bucketSum(row.target_months, scenPeriod().months);
  if (key === "plan") return plan;
  if (key === "ly") return cm.ly || 0;
  if (key === "vsly") return cm.ly ? (plan - cm.ly) / Math.abs(cm.ly) : -1e9;
  if (key === "off") return cm.official || 0;
  if (key === "trend") return cm.ytd_ly ? (cm.ytd - cm.ytd_ly) / Math.abs(cm.ytd_ly) : -1e9;
  if (key && key.startsWith("per:")) {
    const c = periodCols()[Number(key.slice(4))];
    return c ? bucketSum(row.target_months, c.idxs) : row.target_annual;
  }
  return row.target_annual;
}

function sortRows(list, nameField) {
  const { key, dir } = sortState;
  const mul = dir === "asc" ? 1 : -1;
  const arr = [...list];
  arr.sort((a, b) => key === "name"
    ? mul * String(a[nameField]).localeCompare(String(b[nameField]))
    : mul * (sortNum(a, key) - sortNum(b, key)));
  return arr;
}

function renderGrid() {
  const c = payload.countries[activeCountry];
  document.getElementById("gridHead").innerHTML =
    `<tr>${th("name", "Brand / Retailer", "name")}<th title="Growth % on the starting point (${esc((payload.start || {}).label || baseLabel())})">Growth</th><th></th>${periodHeaders()}${h1Header()}
     ${planHeaders()}</tr>`;

  const defG = (payload.scenario.default_growth_pct * 100).toFixed(1);
  const refName = compareLabel();
  const rows = [];
  sortRows(c.brands, "brand").forEach((b) => {
    const key = `${c.country}|${b.brand}`;
    const open = !!expanded[key];
    const bg = b.brand_growth_override != null ? b.brand_growth_override * 100 : "";
    const bClass = b.brand_growth_override != null ? "src-brand" : "";
    const brandGrowthCell = b.is_manual
      ? `<td class="muted center">—</td>`
      : `<td><input class="grow-input ${bClass}" type="number" step="0.5" value="${bg}"
           placeholder="${defG}" data-level="brand" data-brand="${enc(b.brand)}"></td>`;
    const brandVsCell = b.is_manual
      ? `<td class="muted">new</td>`
      : `<td class="${b.delta >= 0 ? "pos" : "neg"}">${pctStr(b.growth_pct)}</td>`;
    const addLineBtn = `<button class="mini-btn add-line" data-brand="${enc(b.brand)}"
      title="Add a new customer / retailer line">＋ retailer</button>`;
    const brandExtras = b.is_manual
      ? `<span class="badge-new">new</span> ${addLineBtn}
         <button class="mini-btn del-brand" data-brand="${enc(b.brand)}" title="Remove this brand">✕</button>`
      : addLineBtn;
    let projCtl = "";
    if (b.projected) projCtl += `<span class="proj-badge" title="Months so far are actuals; the rest of the year is filled from ${esc(refName)}">🔮</span>`;
    if (b.new_lines) projCtl += `<span class="proj-warn" title="${b.new_lines} line(s) have no ${esc(refName)} history — only their actuals so far show; add the rest manually">⚠${b.new_lines}</span>`;
    let annualizeCtl = "";
    if (!b.is_manual && b.partial_year) {
      annualizeCtl = b.is_annualized
        ? `<button class="mini-btn annz-btn on" data-annz="${enc(b.brand)}"
             title="Annualized: partial FY26 scaled to a full-year run-rate (×${b.annualize_factor}) and shaped by global seasonality. Click to turn off.">↺ annualized ×${b.annualize_factor}</button>`
        : `<button class="mini-btn annz-btn" data-annz="${enc(b.brand)}"
             title="Launched partway through FY26, so Apr–Sep are empty. Click to scale it to a full-year run-rate (≈×${b.annualize_factor}).">⚠ part-yr</button>`;
    }
    rows.push(`<tr class="row-brand ${open ? "open" : ""}" data-key="${enc(key)}">
      <td class="name"><span class="caret">▶</span>${esc(b.brand)}
        <span class="muted" style="font-weight:400"> · ${b.customers.length}</span> ${projCtl} ${annualizeCtl} ${brandExtras}</td>
      ${brandGrowthCell}
      <td><button class="pace-btn ${b.has_pacing_override ? "active" : ""}"
        title="Monthly pacing for ${esc(b.brand)}" data-pace-brand="${enc(b.brand)}">⤢</button></td>
      ${periodCells(b.target_months)}
      ${h1Cell(b.target_months)}
      ${planCells(b)}</tr>`);

    if (open) {
      sortRows(b.customers, "customer").forEach((cu) => {
        if (cu.is_manual) {
          const hm = cu.has_months_override;
          const monthsBtn = `<button class="pace-btn ${hm ? "active" : ""}" title="Set the $ amount per month (e.g. $0 pre-launch)"
            data-months-brand="${enc(cu.brand)}" data-months-customer="${enc(cu.customer)}">📅</button>`;
          const annualCell = hm
            ? `<td title="Sum of the monthly amounts">${money(cu.target_annual)}</td>`
            : `<td><input class="target-input" type="number" step="1000" value="${Math.round(cu.target_annual)}"
                data-brand="${enc(cu.brand)}" data-customer="${enc(cu.customer)}"></td>`;
          rows.push(`<tr class="row-cust">
            <td class="name">${esc(cu.customer)}
              <button class="mini-btn del-line" data-brand="${enc(cu.brand)}" data-customer="${enc(cu.customer)}"
                title="Remove line">✕</button></td>
            <td class="muted center">—</td><td class="center">${monthsBtn}</td>
            ${periodCells(cu.target_months)}
            ${h1Cell(cu.target_months)}
            ${planCells(cu, isFullYear() ? annualCell.replace("<td", '<td class="plan"') : null)}</tr>`);
          return;
        }
        const gv = (cu.input_growth != null ? cu.input_growth : cu.growth_pct) * 100;
        const cls = cu.growth_source === "customer" ? "src-customer"
          : cu.growth_source === "brand" ? "src-brand" : "";
        const shown = cu.growth_source === "global" ? "" : gv.toFixed(1).replace(/\.0$/, "");
        let custProj = "";
        if (cu.projection_source === "new")
          custProj = ` <span class="proj-warn" title="No ${esc(refName)} history — only the actuals so far show; add the rest of the year manually">⚠</span>`;
        else if (cu.projected)
          custProj = ` <span class="proj-badge" title="Months so far are actuals; rest of year filled from ${esc(refName)}">🔮</span>`;
        rows.push(`<tr class="row-cust">
          <td class="name">${esc(cu.customer)}${custProj}</td>
          <td><input class="grow-input ${cls}" type="number" step="0.5" value="${shown}"
            placeholder="${defG}"
            data-level="customer" data-brand="${enc(cu.brand)}" data-customer="${enc(cu.customer)}"></td>
          <td></td>
          ${periodCells(cu.target_months)}
          ${h1Cell(cu.target_months)}
          ${planCells(cu)}</tr>`);
      });
    }
  });

  const t = c.totals;
  rows.push(`<tr class="tfoot"><td class="name">${c.country} total</td><td></td><td></td>
    ${periodCells(t.target_months)}
    ${h1Cell(t.target_months)}
    ${planCells(t)}</tr>`);

  document.getElementById("gridBody").innerHTML = rows.join("");
  wireGridEvents();
}

function wireGridEvents() {
  document.querySelectorAll("#gridHead th.sortable").forEach((h) => {
    h.onclick = () => {
      const key = h.dataset.sort;
      if (sortState.key === key) {
        sortState.dir = sortState.dir === "desc" ? "asc" : "desc";
      } else {
        sortState = { key, dir: key === "name" ? "asc" : "desc" };
      }
      renderGrid();
    };
  });
  document.querySelectorAll(".row-brand").forEach((r) => {
    r.querySelector(".name").onclick = () => {
      const key = dec(r.dataset.key);
      expanded[key] = !expanded[key];
      renderGrid();
    };
  });
  document.querySelectorAll(".grow-input").forEach((inp) => {
    inp.onclick = (e) => e.stopPropagation();
    inp.onchange = () => {
      const c = payload.countries[activeCountry].country;
      const v = inp.value === "" ? "" : parseFloat(inp.value);
      setGrowth(inp.dataset.level, c, dec(inp.dataset.brand),
        inp.dataset.customer ? dec(inp.dataset.customer) : "", v);
    };
  });
  document.querySelectorAll("[data-pace-brand]").forEach((btn) => {
    btn.onclick = (e) => { e.stopPropagation(); openPacing("brand", dec(btn.dataset.paceBrand)); };
  });
  // manual-line editing
  document.querySelectorAll(".target-input").forEach((inp) => {
    inp.onclick = (e) => e.stopPropagation();
    inp.onchange = () => setManualTarget(dec(inp.dataset.brand), dec(inp.dataset.customer),
      parseFloat(inp.value || "0"));
  });
  document.querySelectorAll(".add-line").forEach((btn) => {
    btn.onclick = (e) => { e.stopPropagation(); addLine(dec(btn.dataset.brand)); };
  });
  document.querySelectorAll(".del-line").forEach((btn) => {
    btn.onclick = (e) => { e.stopPropagation(); delLine(dec(btn.dataset.brand), dec(btn.dataset.customer)); };
  });
  document.querySelectorAll(".del-brand").forEach((btn) => {
    btn.onclick = (e) => { e.stopPropagation(); delBrand(dec(btn.dataset.brand)); };
  });
  document.querySelectorAll("[data-months-brand]").forEach((btn) => {
    btn.onclick = (e) => { e.stopPropagation(); openMonths(dec(btn.dataset.monthsBrand), dec(btn.dataset.monthsCustomer)); };
  });
  document.querySelectorAll(".annz-btn").forEach((btn) => {
    btn.onclick = (e) => { e.stopPropagation(); toggleAnnualize(dec(btn.dataset.annz)); };
  });
}

function toggleAnnualize(brand) {
  const country = curCountry();
  const i = annualizeBrands.findIndex((a) => a.country === country && a.brand === brand);
  if (i >= 0) annualizeBrands.splice(i, 1);
  else annualizeBrands.push({ country, brand });
  queueSave();
}

// ---------- manual brands / lines ----------
function curCountry() { return payload.countries[activeCountry].country; }

function findManual(brand, customer) {
  return manualLines.find((x) => x.country === curCountry() && x.brand === brand && x.customer === customer);
}

function setManualTarget(brand, customer, target) {
  const m = findManual(brand, customer);
  if (m) { m.target_annual = target; queueSave(); }
}

// ---------- monthly amount editor (manual lines) ----------
let monthsCtx = null;

function openMonths(brand, customer) {
  const c = payload.countries[activeCountry];
  const cu = (c.brands.find((b) => b.brand === brand)?.customers || []).find((x) => x.customer === customer);
  const ml = findManual(brand, customer);
  // prefill from an existing override, else from the currently-computed monthly split
  const vals = (ml && ml.months) ? ml.months : (cu ? cu.target_months : new Array(12).fill(0));
  monthsCtx = { brand, customer };
  document.getElementById("monthsTitle").textContent = `Monthly amounts — ${brand} / ${customer} (${c.country})`;
  document.getElementById("monthsInputs").innerHTML = c.months.map((m, i) =>
    `<label>${m}<input type="number" step="1000" data-i="${i}" value="${Math.round(vals[i] || 0)}"></label>`).join("");
  document.querySelectorAll("#monthsInputs input").forEach((inp) => inp.oninput = updateMonthsSum);
  updateMonthsSum();
  document.getElementById("monthsModal").classList.remove("hidden");
}

function readMonthsInputs() {
  const w = new Array(12).fill(0);
  document.querySelectorAll("#monthsInputs input").forEach((inp) => w[Number(inp.dataset.i)] = parseFloat(inp.value || "0"));
  return w;
}

function updateMonthsSum() {
  document.getElementById("monthsSum").textContent = money(readMonthsInputs().reduce((a, b) => a + b, 0));
}

function saveMonths() {
  const w = readMonthsInputs();
  const ml = findManual(monthsCtx.brand, monthsCtx.customer);
  if (ml) { ml.months = w; ml.target_annual = w.reduce((a, b) => a + b, 0); }
  closeMonths();
  queueSave();
}

function clearMonths() {
  const ml = findManual(monthsCtx.brand, monthsCtx.customer);
  if (ml) delete ml.months;   // revert to annual figure, split by pacing
  closeMonths();
  queueSave();
}

function closeMonths() { document.getElementById("monthsModal").classList.add("hidden"); }

async function addBrand() {
  const country = curCountry();
  const r = await uiForm({
    title: "Add brand",
    hint: `New brand for ${country} · ${payload.scenario.target_fy}.`,
    fields: [
      { name: "brand", label: "Brand name", type: "text", required: true },
      { name: "customer", label: "First customer / channel", type: "text", value: "Direct", required: true },
      { name: "target", label: `Annual ${payload.scenario.target_fy} target`, type: "number", step: "1000", value: "0", suffix: "$" },
    ],
    okText: "Add brand",
  });
  if (!r) return;
  if (manualLines.some((x) => x.country === country && x.brand === r.brand && x.customer === r.customer)) {
    return uiAlert("That brand / customer line already exists.", { title: "Duplicate line" });
  }
  manualLines.push({ country, brand: r.brand, customer: r.customer, target_annual: r.target || 0 });
  expanded[`${country}|${r.brand}`] = true;
  queueSave();
}

async function addLine(brand) {
  const country = curCountry();
  const r = await uiForm({
    title: `Add retailer — ${brand}`,
    fields: [
      { name: "customer", label: "New customer / retailer", type: "text", required: true },
      { name: "target", label: `Annual ${payload.scenario.target_fy} target`, type: "number", step: "1000", value: "0", suffix: "$" },
    ],
    okText: "Add line",
  });
  if (!r) return;
  const existing = (payload.countries[activeCountry].brands.find((b) => b.brand === brand)?.customers || [])
    .some((c) => c.customer.toLowerCase() === r.customer.toLowerCase());
  if (existing) return uiAlert(`"${r.customer}" already exists under ${brand}.`, { title: "Duplicate line" });
  manualLines.push({ country, brand, customer: r.customer, target_annual: r.target || 0 });
  expanded[`${country}|${brand}`] = true;
  queueSave();
}

function delLine(brand, customer) {
  manualLines = manualLines.filter((x) => !(x.country === curCountry() && x.brand === brand && x.customer === customer));
  queueSave();
}

async function delBrand(brand) {
  const ok = await uiConfirm(`Remove added brand "${brand}" and all its lines?`,
    { title: "Remove brand", okText: "Remove", danger: true });
  if (!ok) return;
  manualLines = manualLines.filter((x) => !(x.country === curCountry() && x.brand === brand));
  queueSave();
}

// ---------- $0 brand exclusion ----------
async function removeZeroBrands() {
  const country = curCountry();
  const zeros = payload.countries[activeCountry].brands
    .filter((b) => !b.is_manual && b.baseline_annual < 1)   // $0 or net-negative (returns-only)
    .map((b) => b.brand);
  if (!zeros.length) return uiAlert(`No $0 or negative brands in ${country}.`, { title: "Nothing to remove" });
  const ok = await uiConfirm(
    `Hide ${zeros.length} brand(s) with $0 or negative FY26 in ${country}?\n\n${zeros.join(", ")}\n\nReversible via the Excluded menu.`,
    { title: "Hide $0 brands", okText: "Hide" });
  if (!ok) return;
  zeros.forEach((brand) => {
    if (!brandExclusions.some((e) => e.country === country && e.brand === brand)) {
      brandExclusions.push({ country, brand });
    }
  });
  queueSave();
}

function restoreBrand(country, brand) {
  brandExclusions = brandExclusions.filter((e) => !(e.country === country && e.brand === brand));
  queueSave();
  openExcluded();
}

function openExcluded() {
  const country = curCountry();
  const list = brandExclusions.filter((e) => e.country === country).map((e) => e.brand).sort();
  document.getElementById("excludedCountry").textContent = country;
  document.getElementById("excludedContent").innerHTML = list.length
    ? list.map((b) => `<div class="dupe-group">${esc(b)}
        <button class="btn restore" data-brand="${enc(b)}" style="float:right;padding:2px 8px">Restore</button></div>`).join("")
    : "<p>No excluded brands in this country.</p>";
  document.querySelectorAll("#excludedContent .restore").forEach((btn) =>
    btn.onclick = () => restoreBrand(country, dec(btn.dataset.brand)));
  document.getElementById("excludedModal").classList.remove("hidden");
}

// ---------- pacing modal ----------
function currentPacing(level, brand) {
  const found = pacingOverrides.find((p) => p.level === level &&
    p.country === payload.countries[activeCountry].country && (level === "global" || p.brand === brand));
  return found ? found.weights : null;
}

function seasonality(level, brand) {
  // derive baseline seasonality from the computed baseline months of the target scope
  const c = payload.countries[activeCountry];
  let months = new Array(12).fill(0);
  const brands = level === "brand" ? c.brands.filter((b) => b.brand === brand) : c.brands;
  brands.forEach((b) => b.baseline_months.forEach((m, i) => months[i] += m));
  const tot = months.reduce((a, b) => a + b, 0);
  return tot > 0 ? months.map((m) => m / tot) : new Array(12).fill(1 / 12);
}

function openPacing(level, brand) {
  const c = payload.countries[activeCountry];
  const season = seasonality(level, brand);
  pacingCtx = { level, country: c.country, brand: brand || "", seasonality: season };
  const weights = currentPacing(level, brand) || season;
  document.getElementById("pacingTitle").textContent =
    level === "global" ? `Global monthly pacing — ${c.country}` : `Monthly pacing — ${brand} (${c.country})`;
  document.getElementById("pacingGlobalShape").classList.toggle("hidden", level !== "brand");
  document.getElementById("pacingInputs").innerHTML = c.months.map((m, i) =>
    `<label>${m}<input type="number" step="0.1" data-i="${i}" value="${(weights[i] * 100).toFixed(1)}"></label>`).join("");
  document.querySelectorAll("#pacingInputs input").forEach((inp) => inp.oninput = updatePacingSum);
  updatePacingSum();
  document.getElementById("pacingModal").classList.remove("hidden");
}

function readPacingInputs() {
  const w = new Array(12).fill(0);
  document.querySelectorAll("#pacingInputs input").forEach((inp) =>
    w[Number(inp.dataset.i)] = parseFloat(inp.value || "0") / 100);
  return w;
}

function updatePacingSum() {
  const sum = readPacingInputs().reduce((a, b) => a + b, 0) * 100;
  document.getElementById("pacingSum").textContent = sum.toFixed(1);
  document.getElementById("pacingWarn").classList.toggle("hidden", Math.abs(sum - 100) < 0.1);
}

function savePacing() {
  const w = readPacingInputs();
  const sum = w.reduce((a, b) => a + b, 0);
  if (sum <= 0) return;
  const norm = w.map((x) => x / sum); // normalize defensively
  const { level, country, brand } = pacingCtx;
  pacingOverrides = pacingOverrides.filter((p) =>
    !(p.level === level && p.country === country && (level === "global" || p.brand === brand)));
  pacingOverrides.push({ level, country, brand: level === "brand" ? brand : "", weights: norm });
  closePacing();
  queueSave();
}

function closePacing() { document.getElementById("pacingModal").classList.add("hidden"); }

// ---------- duplicates ----------
async function openDupes() {
  const d = await api("GET", "/targets/api/duplicates");
  const groups = d.duplicate_groups.map((g) =>
    `<div class="dupe-group"><b>likely same:</b> ${g.map(esc).join("  ·  ")}</div>`).join("");
  const junk = d.junk_names.length
    ? `<div class="dupe-group"><b>junk buckets:</b> ${d.junk_names.map(esc).join("  ·  ")}</div>` : "";
  document.getElementById("dupesContent").innerHTML =
    `<p class="hint">${d.total_customers} customers · ${d.duplicate_groups.length} duplicate groups flagged.</p>`
    + groups + junk || "<p>None flagged.</p>";
  document.getElementById("dupesModal").classList.remove("hidden");
}

// ---------- actuals datasets ----------
async function openActuals() {
  await refreshActualsList();
  document.getElementById("actualsModal").classList.remove("hidden");
}

// Actuals are NetSuite sales from the warehouse, one financial year each (read-only)
async function refreshActualsList() {
  const list = await api("GET", "/targets/api/actuals");
  const el = document.getElementById("actualsList");
  if (!list.length) { el.innerHTML = "<p class='hint'>No NetSuite sales found in the database.</p>"; return; }
  el.innerHTML = list.map((d) => {
    const period = d.period_start ? `${esc(d.period_start)} – ${esc(d.period_end)}` : "period n/a";
    const used = d.scenario_count
      ? `<span class="ds-used" title="Scenarios built on this year">${d.scenario_count} scenario${d.scenario_count > 1 ? "s" : ""}</span>`
      : `<span class="ds-unused">unused</span>`;
    const partial = d.is_partial
      ? ` <span class="ds-partial" title="Complete months so far: ${esc(coveredLabel(d.months_present))}">year to date · ${esc(coveredLabel(d.months_present))}</span>`
      : "";
    const t = d.totals || {};
    return `<div class="ds-row">
      <div class="ds-main"><b>${esc(d.name)}</b>${partial}
        <div class="ds-meta">${period} · Australia ${money(t.AU)} · New Zealand ${money(t.NZ)}</div></div>
      <div class="ds-side">${used}</div>
    </div>`;
  }).join("");
}

// ---------- misc ----------
function flash(msg) {
  const b = document.getElementById("banner");
  b.textContent = msg; b.classList.remove("hidden");
  clearTimeout(flash._t);
  flash._t = setTimeout(() => b.classList.add("hidden"), 1200);
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const enc = (s) => encodeURIComponent(s);
const dec = (s) => decodeURIComponent(s);

// ---------- in-app dialogs (replace native alert / confirm / prompt) ----------
function buildDialog(inner) {
  const ov = document.createElement("div");
  ov.className = "dialog";
  ov.innerHTML = `<div class="dialog-card" role="dialog" aria-modal="true">${inner}</div>`;
  document.getElementById("dialogHost").appendChild(ov);
  // Escape + backdrop click dismiss; detach key handler when the overlay closes.
  const onKey = (e) => { if (e.key === "Escape") ov.dismiss(); };
  ov.addEventListener("mousedown", (e) => { if (e.target === ov) ov.dismiss(); });
  document.addEventListener("keydown", onKey);
  const rm = ov.remove.bind(ov);
  ov.remove = () => { document.removeEventListener("keydown", onKey); rm(); };
  ov.dismiss = () => {};   // overridden per-dialog to resolve appropriately
  return ov;
}

function uiAlert(message, { title = "Notice", okText = "OK" } = {}) {
  return new Promise((resolve) => {
    const ov = buildDialog(
      `<h3>${esc(title)}</h3><p class="dialog-msg">${esc(message)}</p>
       <div class="dialog-actions"><span class="spacer"></span>
         <button class="btn btn-primary" data-ok>${esc(okText)}</button></div>`);
    const close = () => { ov.remove(); resolve(); };
    ov.dismiss = close;
    const ok = ov.querySelector("[data-ok]");
    ok.onclick = close; ok.focus();
    ok.onkeydown = (e) => { if (e.key === "Enter") close(); };
  });
}

function uiConfirm(message, { title = "Confirm", okText = "OK", cancelText = "Cancel", danger = false } = {}) {
  return new Promise((resolve) => {
    const ov = buildDialog(
      `<h3>${esc(title)}</h3><p class="dialog-msg">${esc(message)}</p>
       <div class="dialog-actions"><span class="spacer"></span>
         <button class="btn" data-cancel>${esc(cancelText)}</button>
         <button class="btn ${danger ? "btn-solid-danger" : "btn-primary"}" data-ok>${esc(okText)}</button></div>`);
    const done = (v) => { ov.remove(); resolve(v); };
    ov.dismiss = () => done(false);
    const ok = ov.querySelector("[data-ok]");
    ok.onclick = () => done(true); ok.focus();
    ok.onkeydown = (e) => { if (e.key === "Enter") done(true); };
    ov.querySelector("[data-cancel]").onclick = () => done(false);
  });
}

// fields: [{ name, label, type:"text"|"number", value, placeholder, step, suffix, required }]
// resolves to an object keyed by field name, or null if cancelled.
function uiForm({ title, hint, fields, okText = "Save", cancelText = "Cancel" }) {
  return new Promise((resolve) => {
    const fieldHtml = fields.map((f, i) => {
      const val = f.value != null ? esc(String(f.value)) : "";
      let control;
      if (f.type === "select") {
        const opts = (f.options || []).map((o) =>
          `<option value="${esc(String(o.value))}"${String(o.value) === String(f.value) ? " selected" : ""}>${esc(o.label)}</option>`).join("");
        control = `<select data-i="${i}">${opts}</select>`;
      } else {
        const ph = f.placeholder != null ? ` placeholder="${esc(String(f.placeholder))}"` : "";
        const step = f.step ? ` step="${f.step}"` : "";
        const type = f.type === "number" ? "number" : "text";
        const input = `<input data-i="${i}" type="${type}"${step} value="${val}"${ph}>`;
        control = f.suffix ? `<span class="suffix-wrap">${input}<span>${esc(f.suffix)}</span></span>` : input;
      }
      return `<div class="field"><label>${esc(f.label)}</label>${control}</div>`;
    }).join("");
    const ov = buildDialog(
      `<h3>${esc(title)}</h3>${hint ? `<p class="hint">${esc(hint)}</p>` : ""}${fieldHtml}
       <div class="field-error" data-err></div>
       <div class="dialog-actions"><span class="spacer"></span>
         <button class="btn" data-cancel>${esc(cancelText)}</button>
         <button class="btn btn-primary" data-ok>${esc(okText)}</button></div>`);
    const inputs = [...ov.querySelectorAll("[data-i]")];
    const err = ov.querySelector("[data-err]");
    const done = (v) => { ov.remove(); resolve(v); };
    ov.dismiss = () => done(null);
    const submit = () => {
      const out = {};
      for (let i = 0; i < fields.length; i++) {
        const f = fields[i];
        const raw = inputs[i].value;
        if (f.type === "number") {
          out[f.name] = parseFloat(raw === "" ? "0" : raw);
        } else if (f.type === "select") {
          out[f.name] = raw;
        } else {
          out[f.name] = raw.trim();
        }
        if (f.required && (f.type === "number" ? Number.isNaN(out[f.name]) : out[f.name] === "")) {
          err.textContent = `${f.label} is required.`;
          inputs[i].focus();
          return;
        }
      }
      done(out);
    };
    inputs.forEach((inp) => inp.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    ov.querySelector("[data-ok]").onclick = submit;
    ov.querySelector("[data-cancel]").onclick = () => done(null);
    if (inputs[0]) { inputs[0].focus(); if (inputs[0].select) inputs[0].select(); }
  });
}

// ---------- top bar wiring ----------
document.getElementById("scenarioSelect").onchange = (e) => selectScenario(e.target.value);
document.getElementById("globalGrowth").onchange = queueSave;
document.getElementById("exportBtn").onclick = () => { if (sid) location.href = `/targets/api/scenarios/${sid}/export.csv`; };
document.getElementById("exportH1Btn").onclick = () => { if (sid) location.href = `/targets/api/scenarios/${sid}/export.csv?scope=h1`; };

// ---------- period toggle ----------
function renderPeriodToggle() {
  document.querySelectorAll("#periodToggle button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === periodView));
}
document.querySelectorAll("#periodToggle button").forEach((b) => {
  b.onclick = () => {
    periodView = b.dataset.view;
    try { localStorage.setItem("targets-period", periodView); } catch (e) {}
    if (String(sortState.key).startsWith("per:")) sortState = { key: "plan", dir: "desc" };
    renderPeriodToggle();
    if (payload) renderGrid();
  };
});
renderPeriodToggle();
document.getElementById("projectionBtn").onclick = () => { if (payload) openProjection(); };
document.getElementById("globalPacingBtn").onclick = () => { if (payload) openPacing("global", ""); };
document.getElementById("addBrandBtn").onclick = () => { if (payload) addBrand(); };
document.getElementById("removeZeroBtn").onclick = () => { if (payload) removeZeroBrands(); };
document.getElementById("excludedBtn").onclick = () => { if (payload) openExcluded(); };
document.getElementById("excludedClose").onclick = () => document.getElementById("excludedModal").classList.add("hidden");
document.getElementById("restoreAllBtn").onclick = () => {
  const country = curCountry();
  brandExclusions = brandExclusions.filter((e) => e.country !== country);
  queueSave(); openExcluded();
};
document.getElementById("dupesBtn").onclick = openDupes;
document.getElementById("dupesClose").onclick = () => document.getElementById("dupesModal").classList.add("hidden");
document.getElementById("actualsBtn").onclick = openActuals;
document.getElementById("actualsClose").onclick = () => document.getElementById("actualsModal").classList.add("hidden");
document.getElementById("pacingCancel").onclick = closePacing;
document.getElementById("pacingSave").onclick = savePacing;
document.getElementById("monthsCancel").onclick = closeMonths;
document.getElementById("monthsSave").onclick = saveMonths;
document.getElementById("monthsEven").onclick = clearMonths;
document.getElementById("pacingNormalize").onclick = () => {
  const w = readPacingInputs(); const s = w.reduce((a, b) => a + b, 0) || 1;
  document.querySelectorAll("#pacingInputs input").forEach((inp) =>
    inp.value = (w[Number(inp.dataset.i)] / s * 100).toFixed(1));
  updatePacingSum();
};
document.getElementById("pacingReset").onclick = () => {
  document.querySelectorAll("#pacingInputs input").forEach((inp) =>
    inp.value = (pacingCtx.seasonality[Number(inp.dataset.i)] * 100).toFixed(1));
  updatePacingSum();
};
document.getElementById("pacingGlobalShape").onclick = () => {
  const w = currentPacing("global") || seasonality("global");
  document.querySelectorAll("#pacingInputs input").forEach((inp) =>
    inp.value = (w[Number(inp.dataset.i)] * 100).toFixed(1));
  updatePacingSum();
};

document.getElementById("newBtn").onclick = async () => {
  // What are you planning (year + period), and what do you start from?
  const ys = await api("GET", "/targets/api/actuals");
  const curFy = ys.length ? (ys[0].is_partial ? ys[0].id : ys[0].id + 1) : 2027;
  const fyLab = (y) => "FY" + String(y % 100).padStart(2, "0");
  const years = [curFy, curFy + 1].map((y) => ({ value: fyLab(y), label: fyLab(y) +
    ` (Apr ${y - 1} – Mar ${y})` + (y === curFy ? " — this year" : " — next year") }));
  const r = await uiForm({
    title: "New scenario",
    hint: "A scenario is a draft: nothing is official until you commit it.",
    fields: [
      { name: "fy", label: "Planning which year?", type: "select", options: years, value: years[0].value },
      { name: "period", label: "Which months?", type: "select", options: PERIOD_PRESETS, value: "0-11" },
      { name: "start", label: "Start from", type: "select", value: "last_year", options: [
        { value: "last_year", label: "Last year's sales for the same months × growth" },
        { value: "this_year", label: "This year so far, the rest from last year (re-forecast this year)" },
        { value: "official", label: "The current official targets (to revise them)" },
        { value: "blank", label: "Blank — I'll add lines by hand" }] },
      { name: "method", label: "When starting from this year so far, fill the remaining months with", type: "select", value: "flat",
        options: [{ value: "flat", label: "Last year's same months" },
                  { value: "tracking", label: "Last year's same months, scaled by how each brand is tracking this year" }] },
      { name: "growth", label: "Growth % for every line (change by brand or retailer later)", type: "number", step: "0.5", value: "0", suffix: "%" },
      { name: "name", label: "Name", type: "text", value: "", placeholder: "e.g. FY27 Q4 draft" },
    ],
    okText: "Create",
  });
  if (!r) return;
  let per;
  if (r.period === "custom") { per = await askPeriod("custom"); if (!per) return; }
  else { const [a2, b2] = r.period.split("-").map(Number); per = { start: a2, end: b2 }; }
  const preset = PERIOD_PRESETS.find((x) => x.value === `${per.start}-${per.end}`);
  const name = (r.name || "").trim() || `${r.fy} ${preset ? preset.label.split(" ")[0] : FY_LABELS[per.start] + "–" + FY_LABELS[per.end]} draft`;
  try {
    const p = await api("POST", "/targets/api/scenarios", {
      name, target_fy: r.fy, start_from: r.start, method: r.method,
      default_growth_pct: (parseFloat(r.growth) || 0) / 100,
      period_start: per.start, period_end: per.end,
    });
    sid = p.scenario.id;
    await loadScenarios(sid);
  } catch (e) {
    await uiAlert(e.message, { title: "Couldn't create the scenario" });
  }
};
document.getElementById("renameBtn").onclick = async () => {
  if (!sid) return;
  const r = await uiForm({ title: "Rename scenario",
    fields: [{ name: "name", label: "Name", type: "text", value: payload.scenario.name, required: true }], okText: "Rename" });
  if (!r || !r.name.trim()) return;
  await api("PUT", `/targets/api/scenarios/${sid}`, { name: r.name.trim() });
  await loadScenarios(sid);
};
document.getElementById("periodBtn").onclick = async () => {
  if (!sid) return;
  const p = scenPeriod();
  const preset = PERIOD_PRESETS.find((x) => x.value === `${p.start}-${p.end}`);
  const per = await askPeriod(preset ? preset.value : "custom");
  if (!per) return;
  applyPayload(await api("PUT", `/targets/api/scenarios/${sid}`, { period_start: per.start, period_end: per.end }));
};
document.getElementById("dupBtn").onclick = async () => {
  if (!sid) return;
  const r = await uiForm({
    title: "Duplicate scenario",
    fields: [{ name: "name", label: "Name for the copy", type: "text", value: payload.scenario.name + " copy", required: true }],
    okText: "Duplicate",
  });
  if (!r) return;
  const p = await api("POST", `/targets/api/scenarios/${sid}/duplicate`, { name: r.name, target_fy: payload.scenario.target_fy });
  await loadScenarios(p.scenario.id);
};
document.getElementById("delBtn").onclick = async () => {
  if (!sid) return;
  const ok = await uiConfirm(`Delete scenario "${payload.scenario.name}"? This cannot be undone.`,
    { title: "Delete scenario", okText: "Delete", danger: true });
  if (!ok) return;
  await api("DELETE", `/targets/api/scenarios/${sid}`);
  sid = null;
  await loadScenarios();
};


// ---------- commit (official targets) ----------
document.getElementById("commitBtn").onclick = () => openCommit().catch((e) => uiAlert(e.message, { title: "Commit" }));
async function openCommit() {
  if (!payload) return;
  const s = payload.scenario, p = scenPeriod();
  let countries = payload.countries.map((c) => c.country);
  const ov = buildDialog(`<h3>Commit "${esc(s.name)}"</h3>
    <p class="hint">This makes <b>${esc(p.label)} ${esc(s.target_fy)}</b> (${FY_LABELS[p.start]}–${FY_LABELS[p.end]}) official
      for the countries ticked. Other months and unticked countries keep their current official targets.</p>
    <div class="cp-countries">${countries.map((c) => `<label><input type="checkbox" data-c="${esc(c)}" checked> ${esc(c)}</label>`).join("")}</div>
    <div data-preview><p class="hint">Working out what changes…</p></div>
    <div class="field"><label>Note (optional) — e.g. "Approved at Oct board meeting"</label><input type="text" class="cp-note" data-note></div>
    <div class="dialog-actions"><span class="spacer"></span>
      <button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-ok>Commit</button></div>`);
  ov.querySelector(".dialog-card").style.maxWidth = "760px";
  const close = () => ov.remove();
  ov.dismiss = close;
  ov.querySelector("[data-cancel]").onclick = close;
  const box = ov.querySelector("[data-preview]");
  async function refresh() {
    countries = [...ov.querySelectorAll("[data-c]")].filter((x) => x.checked).map((x) => x.dataset.c);
    ov.querySelector("[data-ok]").disabled = !countries.length;
    if (!countries.length) { box.innerHTML = '<div class="cp-warn">Tick at least one country.</div>'; return; }
    const pv = await api("POST", `/targets/api/scenarios/${sid}/commit-preview`, { countries });
    const months = pv.countries[0].rows.map((r) => r.month);
    let h = `<table class="cp-table"><thead><tr><th>Official target</th>${months.map((m) => `<th>${m}</th>`).join("")}<th>${esc(p.label)}</th></tr></thead><tbody>`;
    pv.countries.forEach((c) => {
      const cell = (a2, b2) => `<td class="${b2 > a2 ? "up" : b2 < a2 ? "down" : ""}">${money(b2)}</td>`;
      h += `<tr><td>${esc(c.country)} now</td>${c.rows.map((r) => `<td>${money(r.now)}</td>`).join("")}<td>${money(c.now)}</td></tr>`;
      h += `<tr class="cp-total"><td>${esc(c.country)} after${c.included ? "" : " (not committed)"}</td>${c.rows.map((r) => cell(r.now, r.after)).join("")}${cell(c.now, c.after)}</tr>`;
    });
    h += "</tbody></table>";
    h += pv.warnings.map((w) => `<div class="cp-warn">⚠ ${esc(w)}</div>`).join("");
    box.innerHTML = h;
  }
  ov.querySelectorAll("[data-c]").forEach((x) => x.onchange = () => refresh().catch((e) => box.innerHTML = `<div class="cp-warn">${esc(e.message)}</div>`));
  ov.querySelector("[data-ok]").onclick = async () => {
    try {
      const c = await api("POST", `/targets/api/scenarios/${sid}/commit`, { note: ov.querySelector("[data-note]").value || "", countries });
      close();
      applyPayload(await api("GET", `/targets/api/scenarios/${sid}`));
      await uiAlert(`${c.period_label} ${c.target_fy} is now official for ${c.countries.join(" and ")} (commit #${c.id}). ` +
        "The home page measures sales against it from now.", { title: "Committed" });
    } catch (e) { box.insertAdjacentHTML("beforeend", `<div class="cp-warn">${esc(e.message)}</div>`); }
  };
  await refresh();
}
async function openCommits() {
  const list = await api("GET", "/targets/api/commits");
  const box = document.getElementById("commitsList");
  box.innerHTML = list.length ? `<table class="actuals-table"><thead><tr><th>FY</th><th>Period</th><th>Scenario</th><th>Note</th>` +
    `<th>Committed (Sydney)</th><th>By</th><th class="num">Australia</th><th class="num">New Zealand</th><th></th></tr></thead><tbody>` +
    list.map((c) => `<tr><td>${esc(c.target_fy)}${c.current ? '<span class="commit-current" title="Official for: ' + c.official_months.map((i) => FY_LABELS[i]).join(", ") + '">official</span>' : ""}</td>` +
      `<td>${esc(c.period_label)}${c.countries.length < 2 ? " · " + esc(c.countries.join(", ")) : ""}</td>` +
      `<td>${esc(c.scenario_name)}</td><td>${esc(c.note || "")}</td><td>${esc(c.committed_at)}</td><td>${esc(c.committed_by)}</td>` +
      `<td class="num">${money(c.total_au)}</td><td class="num">${money(c.total_nz)}</td>` +
      `<td><a href="/targets/api/commits/${c.id}/export.csv">⬇ CSV</a></td></tr>`).join("") + `</tbody></table>`
    : `<p class="hint">Nothing committed yet. Use ✔ Commit on a finished scenario.</p>`;
  document.getElementById("commitsModal").classList.remove("hidden");
}
document.getElementById("commitsBtn").onclick = () => openCommits().catch((e) => uiAlert(e.message, { title: "Load failed" }));
document.getElementById("commitsClose").onclick = () => document.getElementById("commitsModal").classList.add("hidden");

// ---------- toolbar menus ----------
function closeMenus() { document.querySelectorAll(".tg-menu-pop").forEach((m) => m.hidden = true); }
document.querySelectorAll(".tg-menu [data-menu]").forEach((b) => {
  b.onclick = (e) => {
    e.stopPropagation();
    const pop = b.parentNode.querySelector(".tg-menu-pop"), show = pop.hidden;
    closeMenus(); pop.hidden = !show;
  };
});
document.querySelectorAll(".tg-menu-pop button").forEach((b) => b.addEventListener("click", closeMenus));
document.addEventListener("click", (e) => { if (!e.target.closest(".tg-menu")) closeMenus(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });
document.getElementById("statusChip").onclick = () => openCommits().catch((e) => uiAlert(e.message, { title: "Load failed" }));
document.getElementById("fullYearToggle").onclick = () => {
  showFull = !showFull;
  try { localStorage.setItem("targets-showfull", showFull ? "1" : "0"); } catch (e) {}
  render();
};

// ---------- boot ----------
loadScenarios().catch((e) => uiAlert(e.message, { title: "Load failed" }));
