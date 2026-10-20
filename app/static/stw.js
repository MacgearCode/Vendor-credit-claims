/* Weekly Sell-Through (/reports/weekly). Loads the scoped dataset once and works
   out every tab in the browser, so filters are instant.

   Dataset (app/st_weekly.py): weeks (oldest first), regions, items [code, name,
   brand], customers {name, reports:[regions]}, rows [regionIdx, item, customer,
   sellThrough, sellIn, soh, retailerOnOrder, valueThisWeek] where a series is
   [startWeek, [values…]] or 0; mg.by_region[rg][item] = [onHand, avail, onPO,
   openOrders]; eta.by_region[rg][item] = [[po, eta, status, qty]…].

   Rules (guide + project calendar rule):
     6-week average = mean of the latest 6 weeks incl. this week
     weeks of cover = retail SOH ÷ 6-week average (<2 low, 2–4 getting low, >26 heavy)
     vs LY          = the week ending 364 days earlier
     YTD            = CALENDAR year to date (sell-through is reported on the calendar year)
     channel balance= sell-in − sell-through over the period (+ = retail stock building)
     Macgear stock is per SKU, counted once (never per customer row)
   A selected customer limits EVERY number on the page to that customer. */
(function () {
  "use strict";
  var R = window.RPT, esc = R.esc, int = R.int;
  var view = document.getElementById("stwView");
  var D = null, N = 0, LYI = [], WK = [];
  var S = {mk: "", brand: "", cust: "", per: 13, si: "rep", tab: "overview", q: "",
           sort: {}, more: false};
  var REG = [];                       // region codes by index
  var custReports = [];               // per customer: {AU:true, …}
  var siAll6 = {};                    // "rg:item" -> all-channel sell-in units, last 6 weeks
  var charts = [];

  /* ---------- series helpers ---------- */
  function at(p, k) { if (!p || k < 0) return 0; var i = k - p[0]; return i >= 0 && i < p[1].length ? p[1][i] : 0; }
  function addInto(arr, p) { if (!p) return; var s = p[0], v = p[1]; for (var i = 0; i < v.length; i++) arr[s + i] += v[i]; }
  function sum(arr, a, b) { var t = 0; for (var k = Math.max(a, 0); k <= b; k++) t += arr[k]; return t; }
  function sumP(p, a, b) { var t = 0; for (var k = a; k <= b; k++) t += at(p, k); return t; }

  /* ---------- state <-> address bar ---------- */
  function readHash() {
    var h = new URLSearchParams(location.hash.slice(1));
    ["mk", "brand", "cust", "si", "tab", "q"].forEach(function (k) { if (h.has(k)) S[k] = h.get(k); });
    if (h.has("per")) S.per = +h.get("per") || 0;
  }
  function writeHash() {
    var h = new URLSearchParams();
    ["mk", "brand", "cust", "si", "tab"].forEach(function (k) { if (S[k]) h.set(k, S[k]); });
    h.set("per", S.per);
    history.replaceState(null, "", "#" + h.toString());
  }

  /* ---------- derived from state ---------- */
  function regionsOn() { var i = REG.indexOf(S.mk); return i < 0 ? [0] : [i]; }
  function P() { return S.per ? Math.min(S.per, N) : N; }
  function P0() { return N - P(); }
  function siCounts(r) { return S.si === "all" || !!custReports[r[2]][REG[r[0]]]; }
  function cur() { return D.currency[REG[regionsOn()[0]]]; }

  var F = [];                          // rows passing market / brand / customer
  function filterRows() {
    var on = {}; regionsOn().forEach(function (i) { on[i] = true; });
    F = D.rows.filter(function (r) {
      if (!on[r[0]]) return false;
      if (S.brand && D.items[r[1]][2] !== S.brand) return false;
      if (S.cust && D.customers[r[2]].name !== S.cust) return false;
      return true;
    });
  }

  /* group the filtered rows: key(row) -> weekly arrays + this week's stock */
  function group(keyFn, rows) {
    var g = {}, order = [];
    (rows || F).forEach(function (r) {
      var k = keyFn(r);
      if (k == null) return;
      var x = g[k];
      if (!x) {
        x = g[k] = {key: k, st: new Float64Array(N), si: new Float64Array(N), soh: null, ro: null,
                    val: {}, items: {}, rows: 0};
        order.push(k);
      }
      addInto(x.st, r[3]);
      if (siCounts(r)) addInto(x.si, r[4]);
      if (r[5] != null) x.soh = (x.soh || 0) + r[5];
      if (r[6] != null) x.ro = (x.ro || 0) + r[6];
      if (r[7] != null) x.val[r[0]] = (x.val[r[0]] || 0) + r[7];
      x.items[r[0] + ":" + r[1]] = true;
      x.rows++;
    });
    return order.map(function (k) { return g[k]; });
  }

  function mgOf(itemsSet) {
    var t = {oh: 0, av: 0, po: 0, oo: 0, rate: 0, any: false, eta: null};
    if (!D.mg) return null;
    Object.keys(itemsSet).forEach(function (key) {
      var p = key.split(":"), rg = REG[+p[0]], m = D.mg.by_region[rg][p[1]];
      t.rate += siAll6[key] || 0;
      if (m) { t.oh += m[0]; t.av += m[1]; t.po += m[2]; t.oo += m[3]; t.any = true; }
      var e = nextEta(+p[0], +p[1]);
      if (e && (!t.eta || e.eta < t.eta.eta)) t.eta = e;
    });
    t.rate /= 6;
    t.cover = t.rate > 0 ? t.av / t.rate : null;
    return t;
  }
  function etaLines(ri, item) {
    if (!D.eta || !D.eta.by_region) return [];
    return (D.eta.by_region[REG[ri]] || {})[item] || [];
  }
  function nextEta(ri, item) {
    var l = etaLines(ri, item).filter(function (x) { return x[1]; });
    return l.length ? {eta: l[0][1], qty: l[0][3], status: l[0][2]} : null;
  }

  function stats(x) {
    var L = N - 1, st = x.st, si = x.si, a = P0();
    var o = {tw: st[L], lw: st[L - 1] || 0, a6: sum(st, N - 6, L) / 6,
             ly: LYI[L] >= 0 ? st[LYI[L]] : null,
             per: sum(st, a, L), si_tw: si[L], si_a6: sum(si, N - 6, L) / 6, si_per: sum(si, a, L),
             soh: x.soh, ro: x.ro, val: x.val};
    o.perLY = periodLY(st);
    o.net = o.si_per - o.per;
    o.woc = woc(o.soh, o.a6);
    o.ytd = sum(st, YTD.a, L);
    o.ytdLY = YTD.la >= 0 ? sum(st, YTD.la, YTD.lb) : null;
    o.spark = Array.prototype.slice.call(st, a, N);
    return o;
  }
  function periodLY(st) {
    var t = 0;
    for (var k = P0(); k < N; k++) { if (LYI[k] < 0) return null; t += st[LYI[k]]; }
    return t;
  }
  function woc(soh, a6) {
    if (soh == null) return null;
    if (soh <= 0) return a6 > 0 ? 0 : null;       // a negative retailer SOH is noise: no cover
    if (a6 > 0) return soh / a6;
    return Infinity;
  }

  /* ---------- formatting ---------- */
  function n0(x) { return x == null ? "—" : int(x); }
  function n1(x) { return x == null ? "—" : (Math.abs(x) >= 100 ? int(x) : (Math.round(x * 10) / 10).toLocaleString("en-AU")); }
  function dpct(a, b) {
    if (b == null || !b) return '<span class="flat">—</span>';
    var d = R.delta((a - b) / Math.abs(b) * 100);
    return '<span class="' + d.cls + '">' + d.arrow + " " + d.txt + "</span>";
  }
  function wocCell(w) {
    if (w == null) return '<span class="muted">—</span>';
    if (w === Infinity) return '<span class="stw-woc heavy" title="Stock, no sales in 6 weeks">no sales</span>';
    var c = w < 2 ? "low" : w < 4 ? "mid" : w > 26 ? "heavy" : "ok";
    return '<span class="stw-woc ' + c + '">' + (w >= 100 ? "99+" : w.toFixed(1)) + "</span>";
  }
  function money(v, cy) { return R.money(v, cy, true); }
  function valText(val) {
    var ks = Object.keys(val);
    if (!ks.length) return "—";
    return money(val[ks[0]], cur());
  }
  function dmy(iso) { if (!iso) return "—"; var p = iso.split("-"); return +p[2] + "/" + +p[1] + "/" + p[0].slice(2); }
  function perLabel() { return S.per ? S.per + " wks" : "All (" + N + " wks)"; }
  function netCell(n) {
    var cls = n > 0 ? "pos" : n < 0 ? "neg" : "";
    return '<td class="' + cls + '">' + (n > 0 ? "+" : "") + n0(n) + "</td>";
  }

  /* ---------- filter bar ---------- */
  function fillSelects() {
    var on = {}; regionsOn().forEach(function (i) { on[i] = true; });
    var brands = {}, custs = {};
    D.rows.forEach(function (r) {
      if (!on[r[0]]) return;
      brands[D.items[r[1]][2]] = true;
      custs[r[2]] = true;
    });
    var b = document.getElementById("stwBrand");
    b.innerHTML = '<option value="">All brands</option>' + Object.keys(brands).sort().map(function (x) {
      return '<option' + (x === S.brand ? " selected" : "") + ">" + esc(x) + "</option>";
    }).join("");
    if (S.brand && !brands[S.brand]) S.brand = "";
    var rep = [], other = [];
    Object.keys(custs).forEach(function (ci) {
      var c = D.customers[ci], reports = regionsOn().some(function (i) { return custReports[ci][REG[i]]; });
      (reports ? rep : other).push(c.name);
    });
    rep.sort(); other.sort();
    var c = document.getElementById("stwCust");
    function opts(list) {
      return list.map(function (x) { return "<option" + (x === S.cust ? " selected" : "") + ">" + esc(x) + "</option>"; }).join("");
    }
    c.innerHTML = '<option value="">All customers</option><optgroup label="Reporting retailers">' + opts(rep) + "</optgroup>" +
      (D.sell_in && other.length && (S.si === "all" || other.indexOf(S.cust) >= 0)
        ? '<optgroup label="Other channels (sell-in only)">' + opts(other) + "</optgroup>" : "");
    if (S.cust && rep.indexOf(S.cust) < 0 && other.indexOf(S.cust) < 0) S.cust = "";
  }
  function syncBar() {
    document.querySelectorAll("#stwMarket button").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.mk === S.mk); });
    document.querySelectorAll("#stwPeriod button").forEach(function (b) { b.setAttribute("aria-pressed", +b.dataset.per === S.per); });
    document.querySelectorAll("#stwSi button").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.si === S.si); });
    document.querySelectorAll("#stwTabs button").forEach(function (b) { b.classList.toggle("on", b.dataset.tab === S.tab); });
    var chips = [];
    if (S.brand) chips.push('<button type="button" class="stw-clear" data-clear="brand">' + esc(S.brand) + " ✕</button>");
    if (S.cust) chips.push('<button type="button" class="stw-clear" data-clear="cust" title="Only this customer\'s numbers are shown">' + esc(S.cust) + " only ✕</button>");
    document.getElementById("stwClear").innerHTML = chips.join(" ");
  }

  function chipsHead() {
    document.getElementById("stwWeek").innerHTML = "Week ending <b>" + R.wk(WK[N - 1], true) + "</b>";
    var c = [];
    if (D.mg && D.mg.as_at) c.push('<span class="r-chip" title="Macgear stock: latest NetSuite snapshot, saleable locations">MG stock ' + dmy(D.mg.as_at) + "</span>");
    if (D.sell_in) {
      if (!D.eta) c.push('<span class="r-chip warn" title="reporting.fact_open_po is not in the warehouse yet">PO ETAs not loaded yet</span>');
      else if (!D.eta.as_at) c.push('<span class="r-chip warn">PO ETAs: no open lines loaded</span>');
      else c.push('<span class="r-chip" title="When the open-PO list was loaded">ETAs ' + esc(new Date(D.eta.as_at).toLocaleString("en-AU", {day: "numeric", month: "short", hour: "numeric", minute: "2-digit"})) + "</span>");
    }
    document.getElementById("stwChips").innerHTML = c.join("");
  }

  /* ---------- render ---------- */
  function render() {
    charts = [];
    filterRows();
    syncBar();
    writeHash();
    var fn = {overview: overview, brands: brands, customers: customers, stores: stores, products: products, stock: stock, how: how}[S.tab] || overview;
    view.innerHTML = fn();
    if (S.tab === "overview") drawOverviewChart();
    if (S.tab === "products" || S.tab === "stores") bindSearch();
    measure();
  }

  /* Overview */
  var OV = null;
  function overview() {
    if (!F.length) return '<div class="stw-empty">Nothing to show for this selection.</div>';
    var all = group(function () { return "all"; })[0], o = stats(all), mg = mgOf(all.items);
    OV = {o: o, all: all};
    var L = N - 1, k = [];
    k.push(kpi("Sell-through units", n0(o.tw),
      "vs last week " + dpct(o.tw, o.lw) + "<br>vs 6-wk avg (" + n0(o.a6) + ") " + dpct(o.tw, o.a6) +
      "<br>vs same week LY " + (o.ly == null ? "—" : "(" + n0(o.ly) + ") " + dpct(o.tw, o.ly))));
    k.push(kpi("Sell-through value", valText(o.val),
      o.tw > 0 && o.val[regionsOn()[0]] != null
        ? "avg price <b>" + money(o.val[regionsOn()[0]] / o.tw, cur()) + "</b><br>units × RRP, this week"
        : "units × RRP, this week"));
    if (D.sell_in) k.push(kpi("Sell-in units", n0(o.si_tw),
      "6-wk avg <b>" + n0(o.si_a6) + "</b><br>" + perLabel() + " <b>" + n0(o.si_per) + "</b><br>" +
      (S.si === "rep" ? "reporting retailers only" : "all channels")));
    k.push(kpi("Retail stock (SOH)", n0(o.soh),
      "weeks of cover " + wocCell(o.woc) + (o.ro ? "<br>retailer on order <b>" + n0(o.ro) + "</b>" : "") +
      (mg ? "<br>MG available <b>" + n0(mg.av) + "</b> · on PO <b>" + n0(mg.po) + "</b>" : "")));
    k.push(kpi("Calendar YTD units", n0(o.ytd),
      "since 1 Jan · LY same weeks " + (o.ytdLY == null ? "—" : "<b>" + n0(o.ytdLY) + "</b> " + dpct(o.ytd, o.ytdLY))));
    if (D.sell_in) k.push(kpi("Channel balance · " + perLabel(), (o.net > 0 ? "+" : "") + n0(o.net),
      "sell-in " + n0(o.si_per) + " − sell-through " + n0(o.per) + "<br>" +
      (o.net > 0 ? '<span class="up">retail stock building</span>' : o.net < 0 ? '<span class="down">retail stock drawing down</span>' : "balanced")));

    var h = '<div class="stw-kpis">' + k.join("") + "</div>";
    h += '<div class="stw-grid stw-g2"><div class="stw-card"><h2>Weekly units <small>' + perLabel() + " to w/e " + R.wk(WK[L], true) + '</small></h2>' +
      '<div class="stw-chart" id="stwChart"></div><div class="stw-legend"><span><i style="background:var(--r-accent)"></i>Sell-through</span>' +
      (D.sell_in ? '<span><i style="background:var(--r-amber)"></i>Sell-in</span>' : "") +
      '<span style="color:var(--r-faint)"><i class="dash"></i>Sell-through same week LY</span></div></div>' +
      '<div class="stw-card"><h2>What stands out</h2>' + standOut(o) + "</div></div>";
    h += '<div class="stw-grid stw-g2b">' +
      topBars("Top 10 brands this week", group(function (r) { return D.items[r[1]][2]; }), "brand") +
      topBars("Top 10 customers this week", group(function (r) { return D.customers[r[2]].name; }), "cust") + "</div>";
    return h;
  }
  function kpi(lbl, val, sub) {
    return '<div class="stw-kpi"><div class="lbl">' + esc(lbl) + '</div><div class="val">' + val + '</div><div class="sub">' + sub + "</div></div>";
  }
  function drawOverviewChart() {
    var el = document.getElementById("stwChart");
    if (!el || !OV) return;
    var a = P0(), st = OV.all.st, si = OV.all.si, lab = WK.slice(a);
    var series = [{name: "Sell-through", type: "area", color: R.css("--r-accent"), values: Array.prototype.slice.call(st, a)}];
    if (D.sell_in) series.push({name: "Sell-in", type: "line", color: R.css("--r-amber"), values: Array.prototype.slice.call(si, a)});
    series.push({name: "Sell-through LY", type: "line", dash: true, color: R.css("--r-faint"),
                 values: lab.map(function (_, i) { var j = LYI[a + i]; return j >= 0 ? st[j] : null; })});
    charts.push(R.chart(el, {labels: lab, series: series}));
  }
  function topBars(title, groups, kind) {
    var rows = groups.map(function (g) { var s = stats(g); return {k: g.key, tw: s.tw, a6: s.a6}; })
      .filter(function (x) { return x.tw > 0 || x.a6 > 0; })
      .sort(function (x, y) { return y.tw - x.tw || y.a6 - x.a6; }).slice(0, 10);
    var max = Math.max.apply(null, rows.map(function (x) { return Math.max(x.tw, x.a6); }).concat([1]));
    var h = '<div class="stw-card"><h2>' + esc(title) + "<small>bar = this week · tick = 6-wk avg · click to filter</small></h2><div class=\"stw-hbars\">";
    rows.forEach(function (x) {
      h += '<div class="stw-hb" data-filter="' + kind + '" data-val="' + esc(x.k) + '" title="' + esc(x.k) + ": " + int(x.tw) + " this week, 6-wk avg " + n1(x.a6) + '">' +
        '<span class="nm">' + esc(x.k) + '</span><span class="tr"><span class="fl" style="width:' + (x.tw / max * 100).toFixed(1) + '%"></span>' +
        '<span class="tk" style="left:calc(' + (x.a6 / max * 100).toFixed(1) + '% - 1px)"></span></span><span class="n">' + int(x.tw) + "</span></div>";
    });
    if (!rows.length) h += '<div class="stw-empty">No sales this week.</div>';
    return h + "</div></div>";
  }
  function standOut(o) {
    var items = group(function (r) { return r[1]; }).map(function (g) { var s = stats(g); s.item = g.key; return s; });
    var movers = items.filter(function (s) { return Math.max(s.tw, s.a6) >= 5; });
    var up = movers.slice().sort(function (a, b) { return (b.tw - b.a6) - (a.tw - a.a6); })[0];
    var dn = movers.slice().sort(function (a, b) { return (a.tw - a.a6) - (b.tw - b.a6); })[0];
    var low = 0, lowEx = null, idle = 0, idleU = 0;
    F.forEach(function (r) {
      if (r[5] == null) return;
      var a6 = sumP(r[3], N - 6, N - 1) / 6, w = woc(r[5], a6);
      if (a6 >= 1 && w < 2) { low++; if (!lowEx || w < lowEx.w) lowEx = {w: w, r: r}; }
      if (r[5] > 0 && a6 === 0) { idle++; idleU += r[5]; }
    });
    var li = [];
    function item(ic, cls, html) { li.push('<li><span class="ic ' + cls + '">' + ic + "</span><span>" + html + "</span></li>"); }
    function pl(i) { return '<a class="stw-link" data-item="' + i + '">' + esc(D.items[i][1]) + "</a>"; }
    if (up && up.tw > up.a6) item("▲", "up", "Biggest riser: " + pl(up.item) + " — " + int(up.tw) + " this week vs " + n1(up.a6) + " avg (+" + int(up.tw - up.a6) + ")");
    if (dn && dn.tw < dn.a6) item("▼", "down", "Biggest faller: " + pl(dn.item) + " — " + int(dn.tw) + " this week vs " + n1(dn.a6) + " avg (" + int(dn.tw - dn.a6) + ")");
    if (low) item("!", "warn", '<a class="stw-link" data-tab="stock">' + int(low) + " line" + (low > 1 ? "s" : "") + "</a> under 2 weeks cover at retail" +
      (lowEx ? " — lowest " + esc(D.items[lowEx.r[1]][1]) + " at " + esc(D.customers[lowEx.r[2]].name) + " (" + lowEx.w.toFixed(1) + " wks)" : ""));
    if (D.sell_in && o.per) {
      var pc = o.net / o.per * 100;
      if (Math.abs(pc) >= 5) item(o.net > 0 ? "↑" : "↓", o.net > 0 ? "up" : "down",
        "Channel " + (o.net > 0 ? "building" : "drawing down") + ": sell-in " + (o.net > 0 ? "ran " : "ran ") + int(Math.abs(o.net)) + " units " +
        (o.net > 0 ? "ahead of" : "behind") + " sell-through over " + perLabel() + " (" + (pc > 0 ? "+" : "") + pc.toFixed(0) + "%)");
      else item("=", "flat", "Channel balanced over " + perLabel() + ": sell-in within 5% of sell-through");
    }
    if (idle) item("○", "flat", '<a class="stw-link" data-tab="stock">' + int(idle) + " line" + (idle > 1 ? "s" : "") + "</a> with stock and no sales in 6 weeks (" + int(idleU) + " units at retail)");
    if (!li.length) return '<div class="stw-empty">Nothing unusual this week.</div>';
    return '<ul class="stw-stand">' + li.join("") + "</ul>";
  }

  /* Sortable tables */
  function sorter(tab, rows, def) {
    var s = S.sort[tab] || def;
    var k = s.k, dir = s.dir;
    rows.sort(function (a, b) {
      var x = a[k], y = b[k];
      if (typeof x === "string" || typeof y === "string") return dir * String(x || "").localeCompare(String(y || ""));
      if (x == null) x = -Infinity; if (y == null) y = -Infinity;
      return dir * (x - y);
    });
    return s;
  }
  function th(label, key, s, cls, title) {
    return '<th class="' + (cls || "") + (s.k === key ? " sorted" : "") + '" data-sort="' + key + '"' + (title ? ' title="' + esc(title) + '"' : "") + ">" +
      label + (s.k === key ? (s.dir > 0 ? " ▲" : " ▼") : "") + "</th>";
  }
  function spark(v) { return R.spark(v, 80, 24); }

  function summaryRows(groups, withMg) {
    return groups.map(function (g) {
      var s = stats(g);
      s.name = g.key;
      s.vs6 = s.a6 ? (s.tw - s.a6) / s.a6 : null;
      s.vsLY = s.perLY ? (s.per - s.perLY) / s.perLY : null;
      s.ytdvs = s.ytdLY ? (s.ytd - s.ytdLY) / s.ytdLY : null;
      s.wocS = s.woc === Infinity ? 1e9 : s.woc;
      if (withMg) {
        var m = mgOf(g.items);
        s.mg_av = m ? m.av : null; s.mg_po = m ? m.po : null; s.mg_cover = m ? m.cover : null;
      }
      return s;
    }).filter(function (s) { return s.per || s.si_per || s.soh || s.ytd; });
  }
  function summaryTable(tab, rows, nameLabel, withMg, filterKind, selected) {
    var s = sorter(tab, rows, {k: "per", dir: -1});
    var si = D.sell_in, mg = withMg && D.mg;
    var tot = group(function () { return "t"; })[0];
    var T = tot ? stats(tot) : null, TM = tot && mg ? mgOf(tot.items) : null;
    var h = '<div class="stw-tablewrap"><table class="stw-t"><thead><tr class="grp"><th class="l"></th>' +
      '<th colspan="6" class="gs">Sell-through</th>' + (si ? '<th colspan="3" class="gs">Sell-in</th>' : "") +
      '<th colspan="' + (mg ? 4 : 2) + '" class="gs">Stock</th><th colspan="2" class="gs">Calendar YTD</th></tr><tr>' +
      th(nameLabel, "name", s, "l") +
      th("This wk", "tw", s, "gs") + th("vs 6-wk avg", "vs6", s) + th("6-wk avg", "a6", s) + th(perLabel(), "per", s) +
      th("vs LY", "vsLY", s, "", "Period vs the same weeks last year") +
      '<th class="stw-hide-sm" style="cursor:default">Trend</th>' +
      (si ? th("This wk", "si_tw", s, "gs") + th(perLabel(), "si_per", s) + th("Net", "net", s, "", "Sell-in − sell-through over the period; + = retail stock building") : "") +
      th("Retail SOH", "soh", s, "gs") + th("Wks cover", "wocS", s) +
      (mg ? th("MG avail", "mg_av", s) + th("MG on PO", "mg_po", s) : "") +
      th("YTD", "ytd", s, "gs") + th("vs LY", "ytdvs", s) + "</tr></thead><tbody>";
    rows.forEach(function (r) {
      h += '<tr data-filter="' + filterKind + '" data-val="' + esc(r.name) + '"' + (r.name === selected ? ' class="sel"' : "") + '><td class="l nm"><b>' + esc(r.name) + "</b></td>" +
        '<td class="gs">' + n0(r.tw) + "</td><td>" + dpct(r.tw, r.a6) + "</td><td>" + n1(r.a6) + "</td><td>" + n0(r.per) + "</td><td>" + dpct(r.per, r.perLY) + "</td>" +
        '<td class="sp stw-hide-sm">' + spark(r.spark) + "</td>" +
        (si ? '<td class="gs">' + n0(r.si_tw) + "</td><td>" + n0(r.si_per) + "</td>" + netCell(r.net) : "") +
        '<td class="gs">' + n0(r.soh) + "</td><td>" + wocCell(r.woc) + "</td>" +
        (mg ? "<td>" + n0(r.mg_av) + "</td><td>" + n0(r.mg_po) + "</td>" : "") +
        '<td class="gs">' + n0(r.ytd) + "</td><td>" + dpct(r.ytd, r.ytdLY) + "</td></tr>";
    });
    h += "</tbody>";
    if (T) {
      h += '<tfoot><tr><td class="l">Total · ' + int(rows.length) + "</td>" +
        '<td class="gs">' + n0(T.tw) + "</td><td>" + dpct(T.tw, T.a6) + "</td><td>" + n1(T.a6) + "</td><td>" + n0(T.per) + "</td><td>" + dpct(T.per, T.perLY) + "</td>" +
        '<td class="sp stw-hide-sm">' + spark(T.spark) + "</td>" +
        (si ? '<td class="gs">' + n0(T.si_tw) + "</td><td>" + n0(T.si_per) + "</td>" + netCell(T.net) : "") +
        '<td class="gs">' + n0(T.soh) + "</td><td>" + wocCell(T.woc) + "</td>" +
        (mg ? "<td>" + n0(TM && TM.av) + "</td><td>" + n0(TM && TM.po) + "</td>" : "") +
        '<td class="gs">' + n0(T.ytd) + "</td><td>" + dpct(T.ytd, T.ytdLY) + "</td></tr></tfoot>";
    }
    return h + "</table></div>";
  }
  function brands() {
    var rows = summaryRows(group(function (r) { return D.items[r[1]][2]; }), true);
    if (!rows.length) return '<div class="stw-empty">Nothing to show for this selection.</div>';
    return '<p class="stw-note" style="margin:0 0 8px">Click a brand to filter the whole page by it.</p>' +
      summaryTable("brands", rows, "Brand", true, "brand", S.brand);
  }
  function customers() {
    var rows = summaryRows(group(function (r) { return D.customers[r[2]].name; }), false);
    if (!rows.length) return '<div class="stw-empty">Nothing to show for this selection.</div>';
    return '<p class="stw-note" style="margin:0 0 8px">' + (S.cust
      ? "Showing <b>" + esc(S.cust) + "</b> only — no other retailer's numbers are on this page."
      : "Click a customer to show only their numbers (safe to share in a meeting with them).") + "</p>" +
      summaryTable("customers", rows, "Customer", false, "cust", S.cust);
  }

  /* Stores — store-level sell-through, loaded on demand per retailer or per product */
  var SC = {}, SCwait = {};
  function storeRetailers() { return (D.store_retailers || {})[S.mk] || []; }
  function storeRetailerText() { return storeRetailers().join(", ") || "none"; }
  function loadStores(q, done) {
    var key = S.mk + "|" + (q.retailer || "") + "|" + (q.code || "");
    if (SC[key]) { done(SC[key]); return; }
    if (SCwait[key]) { SCwait[key].push(done); return; }
    SCwait[key] = [done];
    var u = "/reports/weekly/stores.json?region=" + encodeURIComponent(S.mk) +
      (q.retailer ? "&retailer=" + encodeURIComponent(q.retailer) : "") + (q.code ? "&code=" + encodeURIComponent(q.code) : "");
    fetch(u, {credentials: "same-origin"}).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; });
    }).then(function (j) {
      var code = {}; D.items.forEach(function (it, i) { code[it[0]] = i; });
      j.rows.forEach(function (r) { r.push(code[r[1]] == null ? -1 : code[r[1]]); });   // r[4] = item index
      SC[key] = j;
    }).catch(function (e) { SC[key] = {error: e.message || String(e), rows: [], stores: []}; })
      .then(function () { var cbs = SCwait[key]; delete SCwait[key]; cbs.forEach(function (f) { f(SC[key]); }); });
  }
  function storeLineOk(r) { return !S.brand || (r[4] >= 0 && D.items[r[4]][2] === S.brand); }
  /* per store: weekly units, SOH, products selling, selling products with no stock */
  function storeStats(sd, storeOk, store) {
    var by = {};
    sd.rows.forEach(function (r) {
      var st = sd.stores[r[0]];
      if (!storeOk(st) || !storeLineOk(r) || (store != null && r[0] !== store)) return;
      var x = by[r[0]];
      if (!x) x = by[r[0]] = {idx: r[0], retailer: st[0], store: st[1], st: new Float64Array(N), si: new Float64Array(N), soh: null, ro: null, val: {}, selling: 0, out: 0};
      addInto(x.st, r[2]);
      if (r[3] != null) x.soh = (x.soh || 0) + r[3];
      if (sumP(r[2], P0(), N - 1) > 0) x.selling++;
      if (sumP(r[2], N - 6, N - 1) > 0 && r[3] != null && r[3] <= 0) x.out++;
    });
    return Object.keys(by).map(function (k) {
      var x = by[k], s = stats(x);
      s.idx = x.idx; s.retailer = x.retailer; s.store = x.store; s.name = x.store;
      s.selling = x.selling; s.out = x.out;
      s.vs6 = s.a6 ? (s.tw - s.a6) / s.a6 : null;
      s.vsLY = s.perLY ? (s.per - s.perLY) / s.perLY : null;
      s.wocS = s.woc === Infinity ? 1e9 : s.woc;
      return s;
    });
  }
  function stores() {
    var list = storeRetailers();
    if (!list.length) return '<div class="stw-empty">No retailer sends store-level data in ' + esc(S.mk) + ".</div>";
    if (!S.cust || list.indexOf(S.cust) < 0) {
      return '<div class="stw-card"><h2>Stores <small>store-level sell-through in ' + esc(S.mk) + "</small></h2>" +
        (S.cust ? "<p>" + esc(S.cust) + " doesn't send store-level data. These retailers do:</p>"
                : "<p>Pick a retailer to see its stores. This selects it as the customer, so the whole page then shows only that retailer.</p>") +
        '<div class="r-tabs">' + list.map(function (n) {
          return '<button type="button" class="r-tab" data-filter="cust" data-val="' + esc(n) + '">' + esc(n) + "</button>";
        }).join("") + "</div></div>";
    }
    var mk = S.mk, cust = S.cust, sd = SC[mk + "|" + cust + "|"];
    if (!sd) {
      loadStores({retailer: cust}, function () { if (S.tab === "stores" && S.mk === mk && S.cust === cust) render(); });
      return '<div class="stw-loading">Loading ' + esc(cust) + "'s stores… (a big retailer's first load can take ~20 seconds)</div>";
    }
    if (sd.error) return '<div class="stw-err">' + esc(sd.error) + "</div>";
    var all = storeStats(sd, function () { return true; }, null).filter(function (x) { return x.per || x.soh || x.tw; });
    var words = S.q.toLowerCase().split(/\s+/).filter(Boolean);
    var rows = !words.length ? all.slice() : all.filter(function (x) {
      var h = x.store.toLowerCase(); return words.every(function (w) { return h.indexOf(w) >= 0; });
    });
    var s = sorter("stores", rows, {k: "per", dir: -1});
    var T = {tw: 0, per: 0, a6: 0, soh: 0, perLY: 0, ly: true};
    rows.forEach(function (x) {
      T.tw += x.tw; T.per += x.per; T.a6 += x.a6; T.soh += x.soh || 0;
      if (x.perLY == null) T.ly = false; else T.perLY += x.perLY;
    });
    var h = '<div class="stw-tools"><input class="r-input" id="stwQ" type="search" placeholder="Search store…" value="' + esc(S.q) + '">' +
      '<span class="r-muted" style="font-size:12.5px">' + int(rows.length) + " " + esc(cust) + " stores · click one for its products</span></div>";
    h += '<div class="stw-tablewrap"><table class="stw-t"><thead><tr class="grp"><th class="l"></th><th colspan="6" class="gs">Sell-through</th>' +
      '<th colspan="2" class="gs">Stock</th><th colspan="2" class="gs">Products</th></tr><tr>' +
      th("Store", "name", s, "l") + th("This wk", "tw", s, "gs") + th("vs 6-wk avg", "vs6", s) + th("6-wk avg", "a6", s) + th(perLabel(), "per", s) +
      th("vs LY", "vsLY", s) + '<th class="stw-hide-sm" style="cursor:default">Trend</th>' +
      th("SOH", "soh", s, "gs") + th("Wks cover", "wocS", s) +
      th("Selling", "selling", s, "gs", "Products with sales in the period") +
      th("Out of stock", "out", s, "", "Products sold in the last 6 weeks with no stock this week") + "</tr></thead><tbody>";
    rows.forEach(function (r) {
      h += '<tr data-store="' + r.idx + '"><td class="l nm"><b>' + esc(r.store) + "</b></td>" +
        '<td class="gs">' + n0(r.tw) + "</td><td>" + dpct(r.tw, r.a6) + "</td><td>" + n1(r.a6) + "</td><td>" + n0(r.per) + "</td><td>" + dpct(r.per, r.perLY) + "</td>" +
        '<td class="sp stw-hide-sm">' + spark(r.spark) + "</td>" +
        '<td class="gs">' + n0(r.soh) + "</td><td>" + wocCell(r.woc) + "</td>" +
        '<td class="gs">' + n0(r.selling) + "</td><td" + (r.out ? ' class="neg"' : "") + ">" + n0(r.out) + "</td></tr>";
    });
    h += '</tbody><tfoot><tr><td class="l">Total · ' + int(rows.length) + ' stores</td><td class="gs">' + n0(T.tw) + "</td><td>" + dpct(T.tw, T.a6) + "</td><td>" + n1(T.a6) +
      "</td><td>" + n0(T.per) + "</td><td>" + dpct(T.per, T.ly ? T.perLY : null) + '</td><td class="stw-hide-sm"></td><td class="gs">' + n0(T.soh) + "</td><td>" +
      wocCell(woc(T.soh, T.a6)) + '</td><td class="gs"></td><td></td></tr></tfoot></table></div>';
    return h;
  }
  /* one store's products, in the side panel */
  function openStore(idx) {
    var sd = SC[S.mk + "|" + S.cust + "|"];
    if (!sd) return;
    var st = sd.stores[idx], lines = [];
    sd.rows.forEach(function (r) {
      if (r[0] !== idx || !storeLineOk(r)) return;
      var x = {st: new Float64Array(N), si: new Float64Array(N), soh: r[3], ro: null, val: {}};
      addInto(x.st, r[2]);
      var s = stats(x);
      s.code = r[1];
      s.name = r[4] >= 0 ? D.items[r[4]][1] : r[1]; s.brand = r[4] >= 0 ? D.items[r[4]][2] : "";
      if (s.per || s.soh || s.tw) lines.push(s);
    });
    lines.sort(function (a, b) { return b.per - a.per || (b.soh || 0) - (a.soh || 0); });
    var tot = storeStats(sd, function () { return true; }, idx)[0];
    panel.dataset.item = "";
    var h = '<div class="ph"><div><h2>' + esc(st[1]) + '</h2><div class="pc">' + esc(st[0]) + " · " + esc(S.mk) + (S.brand ? " · " + esc(S.brand) : "") +
      '</div></div><button type="button" class="stw-x" data-close aria-label="Close">×</button></div>';
    if (tot) h += '<div class="stw-kpis">' + kpi("This week", n0(tot.tw), "vs 6-wk avg " + dpct(tot.tw, tot.a6)) +
      kpi(perLabel(), n0(tot.per), "vs LY " + dpct(tot.per, tot.perLY)) + kpi("SOH", n0(tot.soh), "cover " + wocCell(tot.woc)) +
      kpi("Products selling", n0(tot.selling), tot.out ? '<span class="down">' + int(tot.out) + " out of stock</span>" : "none out of stock") + "</div>";
    h += '<div class="stw-card" style="margin-top:12px"><h2>Products <small>' + int(lines.length) + '</small></h2><table class="stw-t"><thead><tr><th class="l">Product</th>' +
      "<th>This wk</th><th>6-wk avg</th><th>" + perLabel() + "</th><th>SOH</th><th>Wks cover</th></tr></thead><tbody>" +
      lines.map(function (s) {
        var out = s.a6 > 0 && s.soh != null && s.soh <= 0;
        return '<tr style="cursor:default"><td class="l nm"><b>' + esc(s.name) + "</b><span>" + esc(s.brand) + " · " + esc(s.code) + "</span></td><td>" + n0(s.tw) + "</td><td>" + n1(s.a6) +
          "</td><td>" + n0(s.per) + "</td><td" + (out ? ' class="neg"' : "") + ">" + n0(s.soh) + "</td><td>" + wocCell(s.woc) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    panel.innerHTML = h;
    panel.classList.add("open"); panel.setAttribute("aria-hidden", "false"); scrim.hidden = false;
    panel.focus();
  }

  /* Products */
  function productRows() {
    return group(function (r) { return r[1]; }).map(function (g) {
      var s = stats(g), it = D.items[g.key], m = mgOf(g.items);
      s.item = g.key; s.name = it[1]; s.code = it[0]; s.brand = it[2];
      s.vsLY = s.perLY ? (s.per - s.perLY) / s.perLY : null;
      s.wocS = s.woc === Infinity ? 1e9 : s.woc;
      if (m) { s.mg_oh = m.oh; s.mg_av = m.av; s.mg_po = m.po; s.mg_oo = m.oo; s.eta = m.eta ? m.eta.eta : null; s.mg_cover = m.cover; s.mg_rate = m.rate; }
      return s;
    }).filter(function (s) { return s.per || s.si_per || s.soh || s.tw; });
  }
  function products() {
    var rows = productRows();
    var words = S.q.toLowerCase().split(/\s+/).filter(Boolean);
    if (words.length) rows = rows.filter(function (s) {
      var hay = (s.name + " " + s.code + " " + s.brand).toLowerCase();
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
    var s = sorter("products", rows, {k: "per", dir: -1});
    var si = D.sell_in, mg = !!D.mg, oo = mg && !S.cust;
    var shown = S.more ? rows : rows.slice(0, 200);
    var h = '<div class="stw-tools"><input class="r-input" id="stwQ" type="search" placeholder="Search product, code or brand…" value="' + esc(S.q) + '">' +
      '<span class="r-muted" style="font-size:12.5px">' + int(rows.length) + " products · click one for its weekly chart and retailers</span></div>";
    h += '<div class="stw-tablewrap"><table class="stw-t"><thead><tr class="grp"><th class="l"></th><th colspan="5" class="gs">Sell-through</th>' +
      (si ? '<th colspan="2" class="gs">Sell-in</th>' : "") + '<th colspan="2" class="gs">Retail stock</th>' +
      (mg ? '<th colspan="' + (oo ? 5 : 4) + '" class="gs">Macgear</th>' : "") + "</tr><tr>" +
      th("Product", "name", s, "l") + th("This wk", "tw", s, "gs") + th("6-wk avg", "a6", s) + th(perLabel(), "per", s) +
      th("vs LY", "vsLY", s) + '<th class="stw-hide-sm" style="cursor:default">Trend</th>' +
      (si ? th(perLabel(), "si_per", s, "gs") + th("Net", "net", s, "", "Sell-in − sell-through over the period") : "") +
      th("SOH", "soh", s, "gs") + th("Wks cover", "wocS", s) +
      (mg ? th("MG SOH", "mg_oh", s, "gs") + th("MG avail", "mg_av", s) + th("MG on PO", "mg_po", s) +
        th("Next ETA", "eta", s, "", "Earliest expected receipt date on an open PO line") +
        (oo ? th("Open orders", "mg_oo", s, "", "Macgear open sales orders, all customers (committed + back ordered)") : "") : "") +
      "</tr></thead><tbody>";
    shown.forEach(function (r) {
      h += '<tr data-item="' + r.item + '"><td class="l nm"><b>' + esc(r.name) + "</b><span>" + esc(r.brand) + " · " + esc(r.code) + "</span></td>" +
        '<td class="gs">' + n0(r.tw) + "</td><td>" + n1(r.a6) + "</td><td>" + n0(r.per) + "</td><td>" + dpct(r.per, r.perLY) + "</td>" +
        '<td class="sp stw-hide-sm">' + spark(r.spark) + "</td>" +
        (si ? '<td class="gs">' + n0(r.si_per) + "</td>" + netCell(r.net) : "") +
        '<td class="gs">' + n0(r.soh) + "</td><td>" + wocCell(r.woc) + "</td>" +
        (mg ? '<td class="gs">' + n0(r.mg_oh) + "</td><td>" + n0(r.mg_av) + "</td><td>" + n0(r.mg_po) + "</td><td>" + etaCell(r) + "</td>" +
          (oo ? "<td>" + n0(r.mg_oo) + "</td>" : "") : "") + "</tr>";
    });
    h += "</tbody></table></div>";
    if (rows.length > shown.length) h += '<button type="button" class="r-btn ghost stw-more" id="stwMore">Show all ' + int(rows.length) + " products</button>";
    return h;
  }
  function etaCell(r) {
    if (!D.eta) return '<span class="muted" title="PO ETAs not loaded yet">·</span>';
    if (r.eta) return dmy(r.eta);
    return r.mg_po ? '<span class="muted" title="Stock on PO but no open PO line with a date">No ETA</span>' : '<span class="muted">—</span>';
  }
  function bindSearch() {
    var q = document.getElementById("stwQ");
    if (!q) return;
    var t = null;
    q.addEventListener("input", function () {
      clearTimeout(t);
      t = setTimeout(function () {
        S.q = q.value; S.more = false;
        var pos = q.selectionStart;
        render();
        var q2 = document.getElementById("stwQ"); q2.focus(); q2.setSelectionRange(pos, pos);
      }, 180);
    });
  }

  /* Stock watch */
  function stock() {
    var low = [], heavy = [], idle = [];
    F.forEach(function (r) {
      if (r[5] == null) return;
      var a6 = sumP(r[3], N - 6, N - 1) / 6, w = woc(r[5], a6);
      var x = {r: r, a6: a6, w: w, soh: r[5]};
      if (a6 >= 1 && w < 3) low.push(x);
      if (a6 > 0 && w > 26) heavy.push(x);
      if (r[5] > 0 && a6 === 0) idle.push(x);
    });
    low.sort(function (a, b) { return a.w - b.w || b.a6 - a.a6; });
    heavy.sort(function (a, b) { return b.soh - a.soh; });
    idle.sort(function (a, b) { return b.soh - a.soh; });
    var mgLow = [];
    if (D.mg) productRows().forEach(function (s) {
      if (s.mg_cover != null && s.mg_cover < 6) mgLow.push(s);
    });
    mgLow.sort(function (a, b) { return a.mg_cover - b.mg_cover || b.mg_rate - a.mg_rate; });

    function lineHead(extra) {
      return '<table class="stw-t"><thead><tr><th class="l">Product</th><th class="l">Customer</th><th>6-wk avg</th><th>SOH</th><th>Wks cover</th>' + (extra || "") + "</tr></thead><tbody>";
    }
    function lineRow(x, extra) {
      var it = D.items[x.r[1]];
      return '<tr data-item="' + x.r[1] + '"><td class="l nm"><b>' + esc(it[1]) + "</b><span>" + esc(it[2]) + " · " + esc(it[0]) + "</span></td>" +
        '<td class="l">' + esc(D.customers[x.r[2]].name) + "</td>" +
        "<td>" + n1(x.a6) + "</td><td>" + n0(x.soh) + "</td><td>" + wocCell(x.w) + "</td>" + (extra || "") + "</tr>";
    }
    function card(title, sub, n, body) {
      return '<div class="stw-card"><h2>' + esc(title) + " <small>" + sub + " · " + int(n) + "</small></h2>" +
        (n ? '<div class="stw-tablewrap">' + body + "</div>" : '<div class="stw-empty">None.</div>') +
        (n > 100 ? '<p class="stw-note">First 100 shown.</p>' : "") + "</div>";
    }
    function refill(x) {
      if (!D.mg) return "";
      var m = D.mg.by_region[REG[x.r[0]]][x.r[1]], e = nextEta(x.r[0], x.r[1]);
      if (m && m[1] > 0) return '<td><span class="stw-pill ok">MG can refill · ' + int(m[1]) + " avail</span></td>";
      if (m && m[2] > 0) return '<td><span class="stw-pill warn">MG out · ' + int(m[2]) + " on PO" + (e ? " · ETA " + dmy(e.eta) : "") + "</span></td>";
      return '<td><span class="stw-pill bad">MG out · nothing on PO</span></td>';
    }
    var h = '<div class="stw-grid stw-gstock" style="margin-top:0">';
    h += card("Running low at retail", "selling 1+ a week, under 3 weeks cover", low.length,
      lineHead(D.mg ? '<th class="l">Macgear</th>' : "") + low.slice(0, 100).map(function (x) { return lineRow(x, refill(x)); }).join("") + "</tbody></table>");
    if (D.mg) h += card("Macgear stock cover under 6 weeks", "available ÷ 6-wk avg sell-in, all channels", mgLow.length,
      '<table class="stw-t"><thead><tr><th class="l">Product</th><th>MG avail</th><th>Sell-in / wk</th><th>Wks cover</th><th>MG on PO</th><th>Next ETA</th></tr></thead><tbody>' +
      mgLow.slice(0, 100).map(function (s) {
        return '<tr data-item="' + s.item + '"><td class="l nm"><b>' + esc(s.name) + "</b><span>" + esc(s.brand) + " · " + esc(s.code) + "</span></td>" +
          "<td>" + n0(s.mg_av) + "</td><td>" + n1(s.mg_rate) + "</td>" +
          "<td>" + wocCell(s.mg_cover) + "</td><td>" + n0(s.mg_po) + "</td><td>" + etaCell(s) + "</td></tr>";
      }).join("") + "</tbody></table>");
    h += card("Heavy stock at retail", "over 26 weeks cover", heavy.length,
      lineHead() + heavy.slice(0, 100).map(function (x) { return lineRow(x); }).join("") + "</tbody></table>");
    h += card("Stock with no sales in 6 weeks", "retail SOH, zero units sold", idle.length,
      lineHead() + idle.slice(0, 100).map(function (x) { return lineRow(x); }).join("") + "</tbody></table>");
    return h + "</div>";
  }

  /* How it works */
  function how() {
    return '<div class="stw-card stw-how"><h2>How it works</h2>' +
      "<p>This page is the consolidated weekly sell-through report, built straight from the reporting warehouse — there is no file to load. " +
      "Retailer files land through Monday; the page picks them up within about 30 minutes, and sell-in and Macgear stock follow NetSuite every half hour.</p>" +
      "<ul><li><b>Market</b> switches between AU and NZ — never added together; values are in the market's own currency.</li>" +
      "<li><b>Stores</b> shows store-level sell-through for the retailers that send it (" + esc(storeRetailerText()) + "). Pick one of them as the customer; the product panel also lists that product's stores.</li>" +
      "<li><b>Period</b> (13 / 26 / 52 weeks / All) drives the chart, trend sparklines, period totals, vs-LY and channel balance on every tab. This week, 6-week average and YTD don't move.</li>" +
      (D.sell_in ? "<li><b>Sell-in counts:</b> <i>Reporting retailers</i> only counts customers that also send sell-through, so the two compare like for like. <i>All channels</i> adds Air NZ, loyalty, online and other customers.</li>" : "") +
      "<li><b>Selecting a customer</b> limits every number on every tab (and the product panel) to that customer — safe to show in a meeting with them. The customer list still names other retailers, so pick before you share your screen." +
      (D.mg ? " Macgear's open sales orders are hidden while a customer is selected (they total every customer)." : "") + "</li></ul>" +
      "<dl>" +
      "<dt>6-week average</dt><dd>Mean of the latest 6 weeks including this week.</dd>" +
      "<dt>Weeks of cover</dt><dd>Retail SOH ÷ 6-week average. Under 2 low, 2–4 getting low, over 26 heavy. \"No sales\" = stock but nothing sold in 6 weeks.</dd>" +
      "<dt>Sell-through value</dt><dd>Units × the item list's RRP (inc GST), this week only, in the market's currency.</dd>" +
      "<dt>vs LY</dt><dd>The week ending 364 days earlier; for a period, the same weeks a year earlier.</dd>" +
      "<dt>Calendar YTD</dt><dd>Weeks ending on or after 1 January vs the same weeks a year earlier. Sell-through is reported on the calendar year.</dd>" +
      (D.sell_in ? "<dt>Sell-in</dt><dd>NetSuite invoiced units less credits (sales account, intercompany excluded), by the week the invoice falls in (Mon–Sun).</dd>" +
        "<dt>Channel balance / Net</dt><dd>Sell-in − sell-through over the period. Positive = retail stock building.</dd>" +
        "<dt>MG SOH / avail / on PO</dt><dd>Macgear stock from the latest NetSuite snapshot, saleable locations, counted once per SKU per market.</dd>" +
        "<dt>Macgear cover</dt><dd>MG available ÷ 6-week average sell-in across all channels.</dd>" +
        "<dt>Open orders</dt><dd>Macgear open sales orders for the SKU, all customers (committed + back ordered).</dd>" +
        "<dt>Next ETA</dt><dd>The earliest expected receipt date on an open PO line. \"No ETA\" = stock on PO but no dated open line." +
        (D.eta ? "" : " <b>PO ETAs are not loaded into the warehouse yet</b> — the column fills in once they are.") + "</dd>" : "") +
      "<dt>Customer names</dt><dd>NetSuite customers are matched to the retailer that sends sell-through via the warehouse's retailer map, or by name ignoring case, punctuation, \"(NZ)\", Limited/Ltd and Computers.</dd>" +
      "</dl></div>";
  }

  /* ---------- product side panel ---------- */
  var panel = document.getElementById("stwPanel"), scrim = document.getElementById("stwScrim");
  function openPanel(item) {
    var it = D.items[item];
    panel.dataset.item = item;
    var rows = F.filter(function (r) { return r[1] === item; });
    if (!rows.length) return;
    var all = group(function () { return "p"; }, rows)[0], o = stats(all), m = mgOf(all.items);
    var byCust = summaryRows(group(function (r) { return D.customers[r[2]].name; }, rows), false)
      .sort(function (a, b) { return b.per - a.per || b.si_per - a.si_per; });
    var h = '<div class="ph"><div><h2>' + esc(it[1]) + '</h2><div class="pc">' + esc(it[2]) + " · " + esc(it[0]) +
      (S.cust ? " · <b>" + esc(S.cust) + " only</b>" : "") + " · " + esc(S.mk) + '</div></div><button type="button" class="stw-x" data-close aria-label="Close">×</button></div>';
    h += '<div class="stw-kpis">' + kpi("This week", n0(o.tw), "vs 6-wk avg " + dpct(o.tw, o.a6)) +
      kpi(perLabel(), n0(o.per), "vs LY " + dpct(o.per, o.perLY)) +
      kpi("Retail SOH", n0(o.soh), "cover " + wocCell(o.woc)) +
      (D.sell_in ? kpi("Sell-in " + perLabel(), n0(o.si_per), "net " + (o.net > 0 ? "+" : "") + n0(o.net)) : "") +
      (m ? kpi("Macgear", n0(m.av) + "<small>avail</small>", "SOH " + n0(m.oh) + " · on PO " + n0(m.po) + (m.eta ? "<br>next ETA <b>" + dmy(m.eta.eta) + "</b>" : "")) : "") + "</div>";
    h += '<div class="stw-card" style="margin-top:12px"><h2>Weekly units <small>' + perLabel() + '</small></h2><div class="stw-chart" id="stwPChart"></div>' +
      '<div class="stw-legend"><span><i style="background:var(--r-accent)"></i>Sell-through</span>' +
      (D.sell_in ? '<span><i style="background:var(--r-amber)"></i>Sell-in</span>' : "") + "</div></div>";
    h += '<div class="stw-card" style="margin-top:12px"><h2>By customer</h2><table class="stw-t"><thead><tr><th class="l">Customer</th><th>This wk</th><th>6-wk avg</th><th>' + perLabel() + "</th>" +
      (D.sell_in ? "<th>Sell-in</th>" : "") + "<th>SOH</th><th>Wks cover</th></tr></thead><tbody>" +
      byCust.map(function (r) {
        return '<tr style="cursor:default"><td class="l">' + esc(r.name) + "</td><td>" + n0(r.tw) + "</td><td>" + n1(r.a6) + "</td><td>" + n0(r.per) + "</td>" +
          (D.sell_in ? "<td>" + n0(r.si_per) + "</td>" : "") + "<td>" + n0(r.soh) + "</td><td>" + wocCell(r.woc) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    if (D.sell_in) {
      var lines = [];
      regionsOn().forEach(function (ri) { etaLines(ri, item).forEach(function (l) { lines.push([REG[ri]].concat(l)); }); });
      h += '<div class="stw-card" style="margin-top:12px"><h2>Incoming Macgear stock</h2>' +
        (!D.eta ? '<div class="stw-empty">PO ETAs are not loaded into the warehouse yet.</div>'
          : !lines.length ? '<div class="stw-empty">' + (m && m.po ? "Stock on PO but no open PO line with a date." : "Nothing on order.") + "</div>"
          : '<table class="stw-t"><thead><tr><th class="l">PO</th>' +
            '<th class="l">ETA</th><th class="l">Status</th><th>Open qty</th></tr></thead><tbody>' +
            lines.map(function (l) {
              return '<tr style="cursor:default"><td class="l">' + esc(l[1]) + "</td>" +
                '<td class="l">' + (l[2] ? dmy(l[2]) : '<span class="muted">No date</span>') + '</td><td class="l">' + esc(l[3] || "") + "</td><td>" + n0(l[4]) + "</td></tr>";
            }).join("") + "</tbody></table>") + "</div>";
    }
    h += '<div class="stw-card" style="margin-top:12px" id="stwPStores"><h2>By store</h2><div class="stw-empty">Loading stores…</div></div>';
    panel.innerHTML = h;
    panel.classList.add("open"); panel.setAttribute("aria-hidden", "false"); scrim.hidden = false;
    panel.focus();
    var a = P0(), series = [{name: "Sell-through", type: "area", color: R.css("--r-accent"), values: Array.prototype.slice.call(all.st, a)}];
    if (D.sell_in) series.push({name: "Sell-in", type: "line", color: R.css("--r-amber"), values: Array.prototype.slice.call(all.si, a)});
    R.chart(document.getElementById("stwPChart"), {labels: WK.slice(a), series: series});
    panelStores(item);
  }
  /* the product's stores (every retailer that sends stores, or only the selected customer) */
  function panelStores(item) {
    var box = document.getElementById("stwPStores"), code = D.items[item][0], mk = S.mk;
    if (!storeRetailers().length) { box.innerHTML = "<h2>By store</h2>" + '<div class="stw-empty">No retailer sends store-level data in ' + esc(mk) + ".</div>"; return; }
    if (S.cust && storeRetailers().indexOf(S.cust) < 0) { box.innerHTML = "<h2>By store</h2>" + '<div class="stw-empty">' + esc(S.cust) + " doesn't send store-level data.</div>"; return; }
    loadStores({code: code}, function (sd) {
      var b = document.getElementById("stwPStores");
      if (!b || S.mk !== mk || +panel.dataset.item !== item) return;
      if (sd.error) { b.innerHTML = '<h2>By store</h2><div class="stw-err">' + esc(sd.error) + "</div>"; return; }
      var list = storeStats(sd, function (st) { return !S.cust || st[0] === S.cust; }, null)
        .filter(function (x) { return x.per || x.soh; })
        .sort(function (x, y) { return y.per - x.per || (y.soh || 0) - (x.soh || 0); });
      var show = b.dataset.all ? list : list.slice(0, 25);
      b.innerHTML = "<h2>By store <small>" + int(list.length) + " stores" + (S.cust ? "" : " · " + esc(storeRetailerText())) + "</small></h2>" +
        (!list.length ? '<div class="stw-empty">No store-level sales or stock for this product.</div>'
          : '<table class="stw-t"><thead><tr><th class="l">Store</th><th>This wk</th><th>6-wk avg</th><th>' + perLabel() + "</th><th>SOH</th><th>Wks cover</th></tr></thead><tbody>" +
            show.map(function (x) {
              return '<tr style="cursor:default"><td class="l nm"><b>' + esc(x.store) + "</b>" + (S.cust ? "" : "<span>" + esc(x.retailer) + "</span>") + "</td><td>" + n0(x.tw) + "</td><td>" + n1(x.a6) +
                "</td><td>" + n0(x.per) + "</td><td>" + n0(x.soh) + "</td><td>" + wocCell(x.woc) + "</td></tr>";
            }).join("") + "</tbody></table>" +
            (list.length > show.length ? '<button type="button" class="r-btn ghost stw-more" data-allstores>Show all ' + int(list.length) + " stores</button>" : ""));
    });
  }
  function closePanel() { panel.classList.remove("open"); panel.setAttribute("aria-hidden", "true"); scrim.hidden = true; }

  /* ---------- sticky offsets ---------- */
  var bar = document.getElementById("stwBar"), top = document.querySelector(".r-top");
  function measure() {
    var full = document.body.classList.contains("is-full");
    var t = full || !top ? 0 : top.offsetHeight;
    document.body.style.setProperty("--stw-top", t + "px");
    document.body.style.setProperty("--stw-stick", (t + bar.offsetHeight) + "px");
    var g = view.querySelector(".stw-t thead tr.grp");
    if (g) document.body.style.setProperty("--stw-grp-h", g.offsetHeight + "px");
  }
  if (window.ResizeObserver) new ResizeObserver(measure).observe(bar);
  window.addEventListener("resize", measure);

  /* ---------- events ---------- */
  document.getElementById("stwMarket").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    S.mk = b.dataset.mk; S.more = false; fillSelects(); render();
  });
  document.getElementById("stwPeriod").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return; S.per = +b.dataset.per; render();
  });
  var siSeg = document.getElementById("stwSi");
  if (siSeg) siSeg.addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return; S.si = b.dataset.si; fillSelects(); render();
  });
  document.getElementById("stwTabs").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return; S.tab = b.dataset.tab; S.q = ""; render(); window.scrollTo(0, 0);
  });
  document.getElementById("stwBrand").addEventListener("change", function (e) { S.brand = e.target.value; S.more = false; render(); });
  document.getElementById("stwCust").addEventListener("change", function (e) { S.cust = e.target.value; S.more = false; closePanel(); render(); });
  document.getElementById("stwClear").addEventListener("click", function (e) {
    var b = e.target.closest("[data-clear]"); if (!b) return;
    S[b.dataset.clear] = ""; fillSelects(); render();
  });
  view.addEventListener("click", function (e) {
    var t = e.target.closest("[data-sort]");
    if (t) {
      var k = t.dataset.sort, cur = S.sort[S.tab];
      S.sort[S.tab] = {k: k, dir: cur && cur.k === k ? -cur.dir : (k === "name" ? 1 : -1)};
      render(); return;
    }
    if (e.target.id === "stwMore") { S.more = true; render(); return; }
    var tab = e.target.closest("[data-tab]");
    if (tab) { S.tab = tab.dataset.tab; render(); window.scrollTo(0, 0); return; }
    var f = e.target.closest("[data-filter]");
    if (f) {
      var kind = f.dataset.filter, v = f.dataset.val;
      S[kind] = S[kind] === v ? "" : v;
      fillSelects(); render(); return;
    }
    var sr = e.target.closest("[data-store]");
    if (sr) { openStore(+sr.dataset.store); return; }
    var it = e.target.closest("[data-item]");
    if (it) openPanel(+it.dataset.item);
  });
  panel.addEventListener("click", function (e) {
    if (e.target.closest("[data-close]")) closePanel();
    if (e.target.closest("[data-allstores]")) { document.getElementById("stwPStores").dataset.all = "1"; panelStores(+panel.dataset.item); }
  });
  scrim.addEventListener("click", closePanel);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closePanel();
    if ((e.key === "f" || e.key === "F") && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) && !e.ctrlKey && !e.metaKey) toggleFull();
  });

  /* full screen: the browser's own, plus hiding the site chrome */
  function toggleFull() {
    if (document.fullscreenElement) { document.exitFullscreen(); return; }
    var el = document.documentElement;
    if (el.requestFullscreen) el.requestFullscreen().catch(function () { setFull(!document.body.classList.contains("is-full")); });
    else setFull(!document.body.classList.contains("is-full"));
  }
  function setFull(on) {
    document.body.classList.toggle("is-full", on);
    var b = document.querySelector("#stwFull span"); if (b) b.textContent = on ? "Exit full screen" : "Full screen";
    measure();
  }
  document.getElementById("stwFull").addEventListener("click", toggleFull);
  document.addEventListener("fullscreenchange", function () { setFull(!!document.fullscreenElement); });

  /* redraw charts when the theme flips (colours are read at draw time) */
  new MutationObserver(function () { if (D) render(); }).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});

  /* ---------- load ---------- */
  var YTD = {a: 0, la: -1, lb: -1};
  function prepare() {
    WK = D.weeks; N = WK.length; REG = D.regions;
    var idx = {}; WK.forEach(function (w, i) { idx[w] = i; });
    function iso(d) { return d.toISOString().slice(0, 10); }
    LYI = WK.map(function (w) { var d = R_date(w); d.setUTCDate(d.getUTCDate() - 364); var j = idx[iso(d)]; return j == null ? -1 : j; });
    var last = R_date(WK[N - 1]), y = last.getUTCFullYear();
    YTD.a = WK.findIndex(function (w) { return w >= y + "-01-01"; });
    var lyEnd = new Date(last); lyEnd.setUTCDate(lyEnd.getUTCDate() - 364);
    var la = WK.findIndex(function (w) { return w >= (y - 1) + "-01-01"; });
    var lb = -1; WK.forEach(function (w, i) { if (w <= iso(lyEnd)) lb = i; });
    // LY YTD only when the data reaches back to 1 Jan last year
    YTD.la = la >= 0 && WK[0] <= (y - 1) + "-01-07" ? la : -1; YTD.lb = lb;
    custReports = D.customers.map(function (c) { var o = {}; c.reports.forEach(function (r) { o[r] = true; }); return o; });
    siAll6 = {};
    D.rows.forEach(function (r) {
      var v = sumP(r[4], N - 6, N - 1);
      if (v) siAll6[r[0] + ":" + r[1]] = (siAll6[r[0] + ":" + r[1]] || 0) + v;
    });
    if (REG.indexOf(S.mk) < 0) S.mk = REG.indexOf("AU") >= 0 ? "AU" : REG[0];   // AU first; no ANZ (user, 2026-10-20)
    if ([0, 13, 26, 52].indexOf(S.per) < 0) S.per = 13;
    if (!D.sell_in) S.si = "rep";
  }
  function R_date(iso) { var p = iso.split("-"); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])); }

  readHash();
  fetch("/reports/weekly/data.json", {credentials: "same-origin"}).then(function (r) {
    return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; });
  }).then(function (j) {
    D = j;
    if (!D.weeks.length) { view.innerHTML = '<div class="stw-empty">No sell-through loaded yet.</div>'; return; }
    prepare(); chipsHead(); fillSelects(); render();
  }).catch(function (e) {
    view.innerHTML = '<div class="stw-err">' + esc(e.message || e) + "</div>";
  });
})();
