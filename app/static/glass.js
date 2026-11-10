/* EXPERIMENTAL "liquid glass" look (2026-11-10, off by default).
   Loaded WITHOUT defer in each shell's <head>, after the stylesheets, so the
   saved setting is applied before first paint (like "theme" / "palette").
   Settings live in localStorage "glass" as JSON {on, bg, op, blur, glow, shine};
   html[data-glass="<bg>"] switches glass.css on, the rest are CSS variables.
   The button and popover are in _theme_btn.html. */
(function () {
  var KEY = "glass", DEF = {on: false, bg: "accent", op: 42, blur: 18, glow: 80, shine: 60};
  var BGS = ["accent", "lime", "sunset", "ocean", "mono"];
  var root = document.documentElement;

  function load() {
    var s = null;
    try { s = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
    var o = {};
    for (var k in DEF) o[k] = (s && s[k] != null) ? s[k] : DEF[k];
    if (BGS.indexOf(o.bg) < 0) o.bg = DEF.bg;
    return o;
  }
  function save(o) { try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) {} }
  function apply(o) {
    if (o.on) root.setAttribute("data-glass", o.bg); else root.removeAttribute("data-glass");
    root.style.setProperty("--gl-op", (o.op / 100).toFixed(2));
    root.style.setProperty("--gl-blur", o.blur + "px");
    root.style.setProperty("--gl-glow", (o.glow / 100).toFixed(2));
    root.style.setProperty("--gl-shine", (o.shine / 100).toFixed(2));
  }

  var st = load();
  apply(st);

  function wire() {
    var btns = document.querySelectorAll("[data-glass-open]");
    if (!btns.length) return;
    function pops() { return document.querySelectorAll(".gl-pop"); }
    function paint() {
      pops().forEach(function (p) {
        var sw = p.querySelector("[data-glass-on]");
        if (sw) sw.setAttribute("aria-checked", st.on ? "true" : "false");
        p.classList.toggle("gl-off", !st.on);
        p.querySelectorAll("input[data-gl]").forEach(function (r) {
          var k = r.getAttribute("data-gl");
          r.value = st[k];
          var out = p.querySelector('[data-gl-out="' + k + '"]');
          if (out) out.textContent = st[k] + (k === "blur" ? "px" : "%");
        });
        p.querySelectorAll("[data-gl-bg]").forEach(function (b) {
          b.setAttribute("aria-checked", b.getAttribute("data-gl-bg") === st.bg ? "true" : "false");
        });
      });
      btns.forEach(function (b) { b.setAttribute("aria-pressed", st.on ? "true" : "false"); });
    }
    function set(k, v) { st[k] = v; apply(st); save(st); paint(); }
    paint();

    btns.forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        var pop = b.parentNode.querySelector(".gl-pop"), show = pop.hidden;
        // close the palette picker (and any other popover in the top bar)
        document.querySelectorAll(".tm-pop").forEach(function (p) { p.hidden = true; });
        document.querySelectorAll("[data-palette-open]").forEach(function (x) { x.setAttribute("aria-expanded", "false"); });
        pop.hidden = !show;
        b.setAttribute("aria-expanded", show ? "true" : "false");
        if (show) { var sw = pop.querySelector("[data-glass-on]"); if (sw) sw.focus(); }
      });
    });
    pops().forEach(function (p) {
      // clicks inside must not reach the document handler that shuts popovers
      p.addEventListener("click", function (e) { e.stopPropagation(); });
      var sw = p.querySelector("[data-glass-on]");
      if (sw) sw.addEventListener("click", function () { set("on", !st.on); });
      p.querySelectorAll("input[data-gl]").forEach(function (r) {
        r.addEventListener("input", function () {
          if (!st.on) st.on = true;                 // touching a control switches it on
          set(r.getAttribute("data-gl"), parseInt(r.value, 10));
        });
      });
      p.querySelectorAll("[data-gl-bg]").forEach(function (b) {
        b.addEventListener("click", function () { st.on = true; set("bg", b.getAttribute("data-gl-bg")); });
      });
    });
    document.addEventListener("click", function () {
      pops().forEach(function (p) { p.hidden = true; });
      btns.forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      pops().forEach(function (p) { p.hidden = true; });
      btns.forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
