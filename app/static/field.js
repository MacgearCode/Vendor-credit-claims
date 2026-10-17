/* Photo viewer for the Field pages: any <a data-lb href="photo-url"> opens the photo
   large over the page, like a shop's product gallery. Arrows / swipe step through
   every photo on the page, click the photo to zoom, Esc or the backdrop closes.
   Optional data-cap (caption) and data-visit (link to the visit page). No library. */
(function () {
  var links = [].slice.call(document.querySelectorAll("a[data-lb]"));
  if (!links.length) return;
  var box = document.createElement("div");
  box.className = "f-lb";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-label", "Photo viewer");
  box.innerHTML =
    '<button type="button" class="f-lb-x" aria-label="Close">&times;</button>' +
    '<button type="button" class="f-lb-nav prev" aria-label="Previous photo">&#8249;</button>' +
    '<div class="f-lb-stage"><img alt=""></div>' +
    '<button type="button" class="f-lb-nav next" aria-label="Next photo">&#8250;</button>' +
    '<div class="f-lb-bar"><span class="f-lb-cap"></span><span class="f-lb-n"></span>' +
    '<a class="f-lb-visit" href="#">Open visit</a><a class="f-lb-orig" href="#" target="_blank" rel="noopener">Full size ↗</a></div>' +
    '<div class="f-lb-strip"></div>';
  document.body.appendChild(box);
  var img = box.querySelector("img"), stage = box.querySelector(".f-lb-stage"),
      cap = box.querySelector(".f-lb-cap"), num = box.querySelector(".f-lb-n"),
      visit = box.querySelector(".f-lb-visit"), orig = box.querySelector(".f-lb-orig"),
      strip = box.querySelector(".f-lb-strip");
  var i = 0, lastFocus = null;

  strip.innerHTML = links.map(function (a, k) {
    return '<button type="button" data-k="' + k + '"><img src="' + a.getAttribute("href") + '" alt="" loading="lazy"></button>';
  }).join("");

  function show(k) {
    i = (k + links.length) % links.length;
    var a = links[i];
    box.classList.remove("zoom");
    img.src = a.getAttribute("href");
    cap.textContent = a.getAttribute("data-cap") || "";
    num.textContent = (i + 1) + " / " + links.length;
    var v = a.getAttribute("data-visit");
    visit.style.display = v ? "" : "none";
    if (v) visit.href = v;
    orig.href = a.getAttribute("href");
    [].forEach.call(strip.children, function (b, n) { b.classList.toggle("on", n === i); });
    var cur = strip.children[i];
    if (cur && cur.scrollIntoView) cur.scrollIntoView({block: "nearest", inline: "center"});
    // warm the neighbours so stepping is instant
    [i + 1, i - 1].forEach(function (n) { new Image().src = links[(n + links.length) % links.length].getAttribute("href"); });
  }
  function open(k) {
    lastFocus = document.activeElement;
    show(k);
    box.classList.add("open");
    document.documentElement.style.overflow = "hidden";
    box.querySelector(".f-lb-x").focus();
  }
  function close() {
    box.classList.remove("open", "zoom");
    document.documentElement.style.overflow = "";
    img.removeAttribute("src");
    if (lastFocus) lastFocus.focus();
  }

  links.forEach(function (a, k) {
    a.addEventListener("click", function (e) {
      if (e.ctrlKey || e.metaKey || e.shiftKey) return;   // let "open in new tab" work
      e.preventDefault();
      open(k);
    });
  });
  box.querySelector(".f-lb-x").onclick = close;
  box.querySelector(".prev").onclick = function () { show(i - 1); };
  box.querySelector(".next").onclick = function () { show(i + 1); };
  strip.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-k]");
    if (b) show(+b.getAttribute("data-k"));
  });
  box.addEventListener("click", function (e) { if (e.target === box || e.target === stage) close(); });
  img.addEventListener("click", function (e) {
    e.stopPropagation();
    box.classList.toggle("zoom");
    if (box.classList.contains("zoom")) {          // zoom in where the click was
      var r = img.getBoundingClientRect();
      requestAnimationFrame(function () {
        stage.scrollLeft = (e.clientX - r.left) / r.width * stage.scrollWidth - stage.clientWidth / 2;
        stage.scrollTop = (e.clientY - r.top) / r.height * stage.scrollHeight - stage.clientHeight / 2;
      });
    }
  });
  document.addEventListener("keydown", function (e) {
    if (!box.classList.contains("open")) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") show(i - 1);
    else if (e.key === "ArrowRight") show(i + 1);
  });
  var x0 = null;                                     // swipe on phones
  stage.addEventListener("touchstart", function (e) { x0 = e.touches[0].clientX; }, {passive: true});
  stage.addEventListener("touchend", function (e) {
    if (x0 === null || box.classList.contains("zoom")) return;
    var dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 50) show(i + (dx < 0 ? 1 : -1));
    x0 = null;
  });
})();
