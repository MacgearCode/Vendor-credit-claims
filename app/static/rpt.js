/* Report-page helpers: formatting, sparklines, an interactive bar/line chart,
   the item search box and the watchlist. Plain browser JS, no build step.
   Everything hangs off window.RPT. */
(function () {
  "use strict";
  var NF = new Intl.NumberFormat("en-AU");
  var MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function int(n) { return n == null ? "—" : NF.format(Math.round(n)); }
  function compact(n) {
    if (n == null) return "—";
    var a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 1 : 2) + "M";
    if (a >= 1e4) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "k";
    return NF.format(Math.round(n));
  }
  function money(n, cur, full) {
    if (n == null) return "—";
    var p = cur === "NZD" ? "NZ$" : "$";
    return (n < 0 ? "-" : "") + p + (full ? NF.format(Math.round(Math.abs(n))) : compact(Math.abs(n)));
  }
  function pct(cur, prev) {
    if (!prev) return null;
    return (cur - prev) / Math.abs(prev) * 100;
  }
  function delta(p) {
    if (p == null) return {txt: "—", cls: "flat", arrow: ""};
    var cls = p > 0.05 ? "up" : (p < -0.05 ? "down" : "flat");
    return {txt: (p > 0 ? "+" : "") + p.toFixed(1) + "%", cls: cls, arrow: cls === "up" ? "▲" : cls === "down" ? "▼" : ""};
  }
  function date(iso) { var p = iso.split("-"); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])); }
  function wk(iso, withYear) {
    var d = date(iso);
    return d.getUTCDate() + " " + MON[d.getUTCMonth()] + (withYear ? " " + d.getUTCFullYear() : "");
  }
  function initials(name) {
    var w = String(name || "?").replace(/[^A-Za-z0-9 ]/g, " ").trim().split(/\s+/);
    return ((w[0] || "?")[0] + (w[1] ? w[1][0] : "")).toUpperCase();
  }
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

  /* ---- sparkline: svg string ---- */
  function spark(values, w, h, color) {
    w = w || 110; h = h || 32;
    var v = (values || []).map(function (x) { return x == null ? 0 : x; });
    if (!v.length) return "";
    var max = Math.max.apply(null, v), min = Math.min.apply(null, v), rng = (max - min) || 1, n = v.length;
    var pts = v.map(function (y, i) {
      return [(n === 1 ? w / 2 : 2 + i * (w - 4) / (n - 1)), 3 + (h - 6) * (1 - (y - min) / rng)];
    });
    var d = pts.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); }).join(" ");
    var area = d + " L" + pts[n - 1][0].toFixed(1) + " " + h + " L" + pts[0][0].toFixed(1) + " " + h + " Z";
    var id = "sg" + Math.random().toString(36).slice(2, 8);
    color = color || "var(--r-accent)";
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true">' +
      '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + color + '" stop-opacity=".28"/>' +
      '<stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + area + '" fill="url(#' + id + ')"/>' +
      '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>' +
      '<circle cx="' + pts[n - 1][0].toFixed(1) + '" cy="' + pts[n - 1][1].toFixed(1) + '" r="2.4" fill="' + color + '"/></svg>';
  }

  /* ---- chart ----
     opts: {labels:[iso], series:[{name, values, color, type:'bar'|'line'|'area', dash}],
            fmt: fn(value) -> text, highlightLast: bool, partialLast: bool} */
  function chart(el, opts) {
    var tip = document.createElement("div");
    tip.className = "r-tip";
    el.innerHTML = "";
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    el.appendChild(svg); el.appendChild(tip);

    function draw() {
      var W = el.clientWidth || 600, H = el.clientHeight || 300;
      var padL = 54, padR = 12, padT = 14, padB = 28;
      var labels = opts.labels, n = labels.length, series = opts.series;
      var all = [], stacks = {};
      series.forEach(function (s) {
        s.values.forEach(function (v, i) {
          if (v == null) return;
          all.push(v);
          if (s.type === "bar" && s.stack) { var k = s.stack + ":" + i + (v >= 0 ? "+" : "-"); stacks[k] = (stacks[k] || 0) + v; }
        });
      });
      Object.keys(stacks).forEach(function (k) { all.push(stacks[k]); });
      var max = Math.max.apply(null, all.concat([0])), min = Math.min.apply(null, all.concat([0]));
      if (max === min) max = min + 1;
      max = niceMax(max);
      var plotW = W - padL - padR, plotH = H - padT - padB;
      function y(v) { return padT + plotH * (1 - (v - min) / (max - min)); }
      var step = plotW / Math.max(n, 1);
      function x(i) { return padL + step * i + step / 2; }
      var grid = css("--r-line"), faint = css("--r-faint");
      var h = "";
      for (var t = 0; t <= 4; t++) {
        var tv = min + (max - min) * t / 4, ty = y(tv);
        h += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + ty.toFixed(1) + '" y2="' + ty.toFixed(1) + '" stroke="' + grid + '"/>';
        h += '<text x="' + (padL - 10) + '" y="' + (ty + 4).toFixed(1) + '" text-anchor="end" font-size="11" fill="' + faint + '">' + esc(opts.axis ? opts.axis(tv) : compact(tv)) + "</text>";
      }
      if (min < 0) h += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y(0) + '" y2="' + y(0) + '" stroke="' + css("--r-line-2") + '"/>';
      var every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 70))));
      for (var i = 0; i < n; i++) {
        if ((n - 1 - i) % every === 0) {
          h += '<text x="' + x(i).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle" font-size="11" fill="' + faint + '">' + esc(opts.xLabel ? opts.xLabel(labels[i], i) : wk(labels[i])) + "</text>";
        }
      }
      // Bars side by side; bars sharing a `stack` key stack in one slot.
      var bars = series.filter(function (s) { return s.type === "bar"; });
      var slots = [];
      bars.forEach(function (s) { var k = s.stack || s.name; if (slots.indexOf(k) < 0) slots.push(k); });
      var bw = Math.max(2, Math.min(34, step * 0.72 / Math.max(slots.length, 1)));
      var base = {};
      bars.forEach(function (s) {
        var si = slots.indexOf(s.stack || s.name);
        s.values.forEach(function (v, i) {
          if (v == null || v === 0) return;
          var key = si + ":" + i + ":" + (v >= 0 ? "+" : "-"), from = base[key] || 0, to = from + v;
          base[key] = to;
          var bx = x(i) - (bw * slots.length) / 2 + si * bw;
          var y0 = y(Math.max(from, to)), y1 = y(Math.min(from, to));
          var partial = opts.partialLast && i === n - 1;
          h += '<rect x="' + bx.toFixed(1) + '" y="' + y0.toFixed(1) + '" width="' + (bw - 1.5).toFixed(1) + '" height="' + Math.max(1, y1 - y0).toFixed(1) +
            '" rx="' + Math.min(4, bw / 3).toFixed(1) + '" fill="' + s.color + '" opacity="' + (partial ? 0.35 : 0.92) + '"/>';
        });
      });
      // the axis must fit stacked totals, not just single series
      series.filter(function (s) { return s.type !== "bar"; }).forEach(function (s) {
        var pts = [];
        s.values.forEach(function (v, i) { if (v != null) pts.push([x(i), y(v)]); });
        if (!pts.length) return;
        var d = pts.map(function (p, k) { return (k ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); }).join(" ");
        if (s.type === "area") {
          var gid = "cg" + Math.random().toString(36).slice(2, 8);
          h += '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + s.color + '" stop-opacity=".30"/><stop offset="1" stop-color="' + s.color + '" stop-opacity="0"/></linearGradient></defs>';
          h += '<path d="' + d + " L" + pts[pts.length - 1][0].toFixed(1) + " " + y(Math.max(min, 0)).toFixed(1) + " L" + pts[0][0].toFixed(1) + " " + y(Math.max(min, 0)).toFixed(1) + ' Z" fill="url(#' + gid + ')"/>';
        }
        h += '<path d="' + d + '" fill="none" stroke="' + s.color + '" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"' + (s.dash ? ' stroke-dasharray="5 5"' : "") + "/>";
      });
      h += '<line id="rcx" y1="' + padT + '" y2="' + (padT + plotH) + '" stroke="' + css("--r-line-2") + '" opacity="0"/>';
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      svg.innerHTML = h;
      var cx = svg.querySelector("#rcx");

      function move(clientX) {
        var r = svg.getBoundingClientRect();
        var i = Math.floor((clientX - r.left - padL) / step);
        if (i < 0 || i >= n) { leave(); return; }
        cx.setAttribute("x1", x(i)); cx.setAttribute("x2", x(i)); cx.setAttribute("opacity", "1");
        var rows = series.map(function (s) {
          return '<div class="row"><span class="sw" style="background:' + s.color + '"></span>' + esc(s.name) + ": " +
            esc(s.values[i] == null ? "—" : (opts.fmt ? opts.fmt(s.values[i], s) : int(s.values[i]))) + "</div>";
        }).join("");
        var partial = opts.partialLast && i === n - 1 ? " · week to date" : "";
        var dl = opts.tipLabel ? opts.tipLabel(labels[i], i) : "w/e " + wk(labels[i], true);
        tip.innerHTML = '<div class="d">' + esc(dl) + partial + "</div>" + rows;
        tip.style.opacity = "1";
        var tx = x(i) / W * r.width, tw = tip.offsetWidth;
        tip.style.left = Math.min(Math.max(tx - tw / 2, 0), r.width - tw) + "px";
        tip.style.top = "0px";
      }
      function leave() { tip.style.opacity = "0"; cx.setAttribute("opacity", "0"); }
      svg.onmousemove = function (e) { move(e.clientX); };
      svg.onmouseleave = leave;
      svg.ontouchstart = svg.ontouchmove = function (e) { if (e.touches[0]) move(e.touches[0].clientX); };
    }
    draw();
    if (window.ResizeObserver) new ResizeObserver(function () { draw(); }).observe(el);
    new MutationObserver(function () { if (el.isConnected) draw(); })
      .observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme", "data-palette"]});
    return {redraw: draw};
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    var e = Math.pow(10, Math.floor(Math.log10(v))), m = v / e;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * e;
  }

  /* ---- item search box: <div class="r-search" data-search="/reports/items/search"><input></div> ---- */
  function searchBox(box) {
    var input = box.querySelector("input"), url = box.getAttribute("data-search");
    var region = box.getAttribute("data-region") || "";
    var list = document.createElement("div");
    list.className = "r-results"; list.hidden = true; box.appendChild(list);
    var timer = null, seq = 0, act = -1;
    function render(items, q) {
      act = -1;
      if (!items.length) { list.innerHTML = '<div class="empty">No items match “' + esc(q) + '”.</div>'; list.hidden = false; return; }
      list.innerHTML = items.map(function (it) {
        return '<a href="' + itemUrl(it.code, region) + '"><span class="r-thumb">' + esc(initials(it.name)) + '</span><span><span class="nm">' +
          esc(it.name) + '</span><br><span class="sub">' + esc(it.code) + " · " + esc(it.brand || "") +
          (it.inactive ? " · inactive" : "") + "</span></span></a>";
      }).join("");
      list.hidden = false;
      prefetch(itemUrl(items[0].code, region));    // Enter opens the top hit: have it ready
    }
    input.addEventListener("input", function () {
      var q = input.value.trim();
      clearTimeout(timer);
      if (q.length < 2) { list.hidden = true; return; }
      timer = setTimeout(function () {
        var my = ++seq;
        fetch(url + "?q=" + encodeURIComponent(q), {credentials: "same-origin"})
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (items) { if (my === seq) render(items, q); })
          .catch(function () {});
      }, 180);
    });
    input.addEventListener("keydown", function (e) {
      var links = list.querySelectorAll("a");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!links.length) return;
        e.preventDefault();
        act = (act + (e.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
        links.forEach(function (a, i) { a.classList.toggle("act", i === act); });
      } else if (e.key === "Enter") {
        var a = links[act >= 0 ? act : 0];
        if (a) { e.preventDefault(); location.href = a.href; }
      } else if (e.key === "Escape") { list.hidden = true; }
    });
    document.addEventListener("click", function (e) { if (!box.contains(e.target)) list.hidden = true; });
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "SELECT") {
        e.preventDefault(); input.focus();
      }
    });
  }
  function itemUrl(code, region) {
    return "/reports/items/i/" + encodeURIComponent(code) + (region ? "?region=" + region : "");
  }

  /* ---- watchlist (this browser only) ---- */
  var WKEY = "rpt.watch";
  function watchList() { try { return JSON.parse(localStorage.getItem(WKEY) || "[]"); } catch (e) { return []; } }
  function watchToggle(code) {
    var l = watchList(), i = l.indexOf(code);
    if (i >= 0) l.splice(i, 1); else l.unshift(code);
    try { localStorage.setItem(WKEY, JSON.stringify(l.slice(0, 100))); } catch (e) {}
    return i < 0;
  }

  /* ---- feel: prefetch item pages on hover, and show progress while a page loads ---- */
  var prefetched = {};
  function prefetch(href) {
    if (prefetched[href]) return;
    prefetched[href] = true;
    var l = document.createElement("link"); l.rel = "prefetch"; l.href = href; document.head.appendChild(l);
  }
  var hoverTimer = null;
  document.addEventListener("mouseover", function (e) {
    var a = e.target.closest && e.target.closest('a[href^="/reports/items/i/"]');
    clearTimeout(hoverTimer);
    if (a) hoverTimer = setTimeout(function () { prefetch(a.getAttribute("href")); }, 90);
  });
  document.addEventListener("touchstart", function (e) {
    var a = e.target.closest && e.target.closest('a[href^="/reports/items/i/"]');
    if (a) prefetch(a.getAttribute("href"));
  }, {passive: true});
  function busy() { document.documentElement.classList.add("r-busy"); }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (a && !a.target && !e.ctrlKey && !e.metaKey && !e.shiftKey && a.origin === location.origin &&
        a.getAttribute("href").charAt(0) !== "#" && !a.hasAttribute("download") && a.pathname.slice(-5) !== ".xlsx") busy();
  });
  document.addEventListener("submit", busy);
  window.addEventListener("pageshow", function () { document.documentElement.classList.remove("r-busy"); });

  window.RPT = {esc: esc, int: int, compact: compact, money: money, pct: pct, delta: delta, wk: wk,
    initials: initials, spark: spark, chart: chart, searchBox: searchBox, itemUrl: itemUrl,
    watchList: watchList, watchToggle: watchToggle, css: css};
  document.querySelectorAll(".r-search[data-search]").forEach(searchBox);
})();
