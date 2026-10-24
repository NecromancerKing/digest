// Optional comfort (spec section 10): every page reads without this file.
(function () {
  "use strict";
  var root = document.documentElement;
  var S = {};
  try { S = JSON.parse(document.getElementById("strings").textContent); } catch (e) { /* defaults */ }

  function say(key, vars) {
    var s = (vars && vars.n === 1 && S[key + "_one"]) || S[key] || "";
    return s.replace(/\{(\w+)\}/g, function (_, k) { return vars && k in vars ? vars[k] : ""; });
  }
  function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function save(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }

  // The edition's local day (Europe/Stockholm): the briefing's reading list resets with it.
  function today() {
    try {
      return new Intl.DateTimeFormat("en-CA", { timeZone: root.dataset.tz || undefined }).format(new Date());
    } catch (e) { return new Date().toISOString().slice(0, 10); }
  }
  var readKey = "briefing:" + today();
  function readIds() {
    try { return JSON.parse(load(readKey) || "[]"); } catch (e) { return []; }
  }
  function markRead(id) {
    var ids = readIds();
    if (ids.indexOf(id) < 0) { ids.push(id); save(readKey, JSON.stringify(ids)); }
  }
  try { // earlier days' lists go
    for (var i = localStorage.length - 1; i >= 0; i--) {
      var k = localStorage.key(i);
      if (k && k.indexOf("briefing:") === 0 && k !== readKey) localStorage.removeItem(k);
    }
  } catch (e) { /* no storage */ }

  // ------------------------------------------------------------- theme
  var toggle = document.querySelector("[data-theme-toggle]");
  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", function () {
      var dark = root.dataset.theme
        ? root.dataset.theme === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      save("theme", root.dataset.theme);
    });
  }

  // ------------------------------------------- relative times and greeting
  function minutesSince(iso) { return Math.floor((Date.now() - Date.parse(iso)) / 60000); }
  function relative() {
    document.querySelectorAll("time[data-ago]").forEach(function (el) {
      var m = minutesSince(el.getAttribute("datetime"));
      if (m < 0 || m >= 24 * 60) return; // older: the build's day or date is friendlier
      var short = el.dataset.ago === "short";
      el.textContent = m < 1 ? S.just_now
        : m < 60 ? say(short ? "min_short" : "min_ago", { n: m })
        : say(short ? "h_short" : "h_ago", { n: Math.floor(m / 60) });
    });
    var up = document.querySelector("[data-updated]");
    if (up) {
      var m = minutesSince(up.dataset.updated);
      if (m >= 0 && m < 24 * 60) up.textContent = m < 1 ? S.updated_now : say("updated_ago", { n: m });
    }
  }
  var greet = document.querySelector("[data-greeting]");
  if (greet) {
    var h = new Date().getHours();
    greet.textContent = h >= 5 && h < 12 ? S.morning : h >= 12 && h < 18 ? S.afternoon : S.evening;
  }
  relative();
  setInterval(relative, 60000);

  // --------------------------------------- home: "Continue your briefing"
  var card = document.querySelector("[data-briefing-card]");
  if (card && card.dataset.ids) {
    var ids = card.dataset.ids.split(",").map(Number);
    var read = readIds();
    var done = ids.filter(function (id) { return read.indexOf(id) >= 0; }).length;
    if (done > 0) {
      var left = ids.length - done;
      var ring = card.querySelector(".done");
      ring.setAttribute("stroke-dashoffset", String(138.2 * (1 - done / ids.length)));
      card.querySelector(".ring b").textContent = done + "/" + ids.length;
      card.querySelector("[data-title]").textContent = S.continue;
      card.querySelector("[data-sub]").textContent = left === 0 ? S.all_read
        : say("left", { n: left, min: Math.max(1, Math.ceil(left * card.dataset.seconds / 60)) });
    }
  }

  // ------------------------------------------------- phone carousel dots
  var carousel = document.querySelector("[data-carousel]");
  if (carousel && "IntersectionObserver" in window) {
    var cards = carousel.children;
    var dots = document.createElement("div");
    dots.className = "dots";
    dots.setAttribute("aria-hidden", "true");
    for (var d = 0; d < cards.length; d++) dots.appendChild(document.createElement("span"));
    carousel.after(dots);
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.intersectionRatio > 0.6) {
          var at = Array.prototype.indexOf.call(cards, en.target);
          Array.prototype.forEach.call(dots.children, function (dot, j) { dot.classList.toggle("on", j === at); });
        }
      });
    }, { root: carousel, threshold: [0.6] });
    Array.prototype.forEach.call(cards, function (c) { seen.observe(c); });
  }

  // -------------------------------------- Today: one story per screen
  var briefRoot = document.querySelector("[data-briefing-root]");
  if (briefRoot) pager(briefRoot);

  function pager(box) {
    var items = Array.prototype.slice.call(box.querySelectorAll("[data-briefing] > li"));
    var doneBox = box.querySelector("[data-done]");
    var seconds = Number(box.dataset.seconds) || 16;
    if (!items.length) return;
    var speech = "speechSynthesis" in window ? window.speechSynthesis : null;
    var listening = false;

    var bar = document.createElement("div");
    bar.className = "pager-bar";
    bar.innerHTML = '<div class="segments" role="progressbar" aria-valuemin="0"></div>'
      + '<p class="counter" aria-live="polite"></p>';
    var segs = bar.firstChild;
    segs.setAttribute("aria-valuemax", String(items.length));
    segs.setAttribute("aria-label", S.progress);
    items.forEach(function () { segs.appendChild(document.createElement("span")); });
    var counter = bar.lastChild;

    var controls = document.createElement("div");
    controls.className = "pager-controls";
    controls.innerHTML = '<button type="button" class="btn" data-prev></button>'
      + '<button type="button" class="btn primary" data-next></button>'
      + '<button type="button" class="btn" data-listen hidden></button>';
    var prevBtn = controls.children[0], nextBtn = controls.children[1], listenBtn = controls.children[2];
    prevBtn.textContent = S.back;
    listenBtn.textContent = S.listen;
    if (speech) listenBtn.hidden = false;

    box.insertBefore(bar, box.firstChild);
    box.appendChild(controls);
    box.classList.add("pager-on");
    box.tabIndex = 0;

    var restart = document.createElement("button");
    restart.type = "button";
    restart.className = "btn";
    restart.textContent = S.again;
    doneBox.appendChild(restart);

    var at = 0;
    var match = /^#s(\d+)$/.exec(location.hash);
    if (match) at = Math.min(items.length - 1, Number(match[1]) - 1);
    else {
      var read = readIds();
      var first = items.findIndex(function (li) { return read.indexOf(Number(li.querySelector("[data-story]").dataset.story)) < 0; });
      at = first < 0 ? items.length : first;
    }

    function show(i, moved) {
      at = Math.max(0, Math.min(items.length, i));
      var finished = at >= items.length;
      items.forEach(function (li, j) { li.classList.toggle("current", j === at); });
      doneBox.classList.toggle("current", finished);
      Array.prototype.forEach.call(segs.children, function (s, j) {
        s.className = j < at ? "seen" : j === at ? "now" : "";
      });
      segs.setAttribute("aria-valuenow", String(Math.min(at + 1, items.length)));
      counter.textContent = finished ? S.all_read : say("counter", {
        i: at + 1, n: items.length, min: Math.max(1, Math.ceil((items.length - at) * seconds / 60)),
      });
      prevBtn.disabled = at === 0;
      nextBtn.hidden = finished;
      nextBtn.textContent = at === items.length - 1 ? S.finish : S.next;
      if (!finished) {
        markRead(Number(items[at].querySelector("[data-story]").dataset.story));
        if (moved) history.replaceState(null, "", "#s" + (at + 1));
      }
      if (moved && box.getBoundingClientRect().top < 0) box.scrollIntoView({ block: "start" });
    }

    function speak() {
      if (!listening || at >= items.length) { stopListening(); return; }
      var li = items[at];
      var text = [li.querySelector("h2"), li.querySelector(".summary, .reported")]
        .filter(Boolean).map(function (el) { return el.textContent.trim(); }).join(". ");
      var u = new SpeechSynthesisUtterance(text);
      u.lang = root.lang;
      u.onend = function () { if (listening) { show(at + 1, true); speak(); } };
      speech.cancel();
      speech.speak(u);
    }
    function stopListening() {
      listening = false;
      if (speech) speech.cancel();
      listenBtn.textContent = S.listen;
      listenBtn.setAttribute("aria-pressed", "false");
    }

    prevBtn.addEventListener("click", function () { stopListening(); show(at - 1, true); });
    nextBtn.addEventListener("click", function () { if (listening) speech.cancel(); show(at + 1, true); if (listening) speak(); });
    restart.addEventListener("click", function () { show(0, true); });
    listenBtn.addEventListener("click", function () {
      if (listening) { stopListening(); return; }
      listening = true;
      listenBtn.textContent = S.stop;
      listenBtn.setAttribute("aria-pressed", "true");
      if (at >= items.length) show(0, true);
      speak();
    });
    box.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight") { e.preventDefault(); nextBtn.click(); }
      if (e.key === "ArrowLeft" && at > 0) { e.preventDefault(); prevBtn.click(); }
    });
    var x0 = null;
    box.addEventListener("touchstart", function (e) { x0 = e.touches[0].clientX; }, { passive: true });
    box.addEventListener("touchend", function (e) {
      if (x0 === null) return;
      var dx = e.changedTouches[0].clientX - x0;
      x0 = null;
      if (dx < -50) nextBtn.click();
      else if (dx > 50 && at > 0) prevBtn.click();
    });
    window.addEventListener("pagehide", stopListening);
    show(at);
  }

  // ------------------------------------------------------------- search
  // Pagefind's API without its worker: our index is small, and shared workers are
  // missing on some phones. Excerpts are Pagefind's escaped text of our own pages.
  var searchBox = document.querySelector("[data-search]");
  if (searchBox) {
    var input = searchBox.querySelector("[data-search-input]");
    var list = searchBox.querySelector("[data-search-results]");
    var status = searchBox.querySelector("[data-search-status]");
    var chips = searchBox.querySelector("[data-search-filters]");
    var pf = null, filter = null, timer = null, seq = 0;
    var loading = import("/pagefind/pagefind.js").then(function (mod) {
      pf = mod;
      return pf.options({ noWorker: true }).then(function () { return pf.filters(); });
    }).then(function (filters) {
      var cats = Object.keys((filters && filters.category) || {}).sort();
      if (!cats.length) return;
      [""].concat(cats).forEach(function (name) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "chip";
        b.textContent = name || searchBox.dataset.all;
        b.setAttribute("aria-pressed", name === "" ? "true" : "false");
        b.addEventListener("click", function () {
          filter = name || null;
          chips.querySelectorAll(".chip").forEach(function (c) { c.setAttribute("aria-pressed", String(c === b)); });
          run();
        });
        chips.appendChild(b);
      });
      chips.hidden = false;
    });
    var run = function () {
      var q = input.value.trim();
      var mine = ++seq;
      if (!q) { list.innerHTML = ""; status.textContent = ""; return; }
      loading.then(function () {
        return pf.search(q, filter ? { filters: { category: filter } } : {});
      }).then(function (res) {
        return Promise.all(res.results.slice(0, 20).map(function (r) { return r.data(); }))
          .then(function (data) { return { total: res.results.length, data: data }; });
      }).then(function (found) {
        if (mine !== seq) return;
        status.textContent = found.total === 0 ? searchBox.dataset.none
          : (found.total === 1 ? searchBox.dataset.countOne : searchBox.dataset.count).replace("{n}", found.total);
        list.innerHTML = "";
        found.data.forEach(function (d) {
          var li = document.createElement("li");
          li.className = "card result";
          var meta = document.createElement("p");
          meta.className = "meta";
          meta.textContent = [d.filters && d.filters.category && d.filters.category[0], d.meta.date]
            .filter(Boolean).join(" · ");
          var h = document.createElement("h2");
          h.className = "serif";
          var a = document.createElement("a");
          a.href = d.url.replace(/\.html$/, "");
          a.textContent = d.meta.title;
          h.appendChild(a);
          var p = document.createElement("p");
          p.className = "summary";
          p.innerHTML = d.excerpt;
          li.append(meta, h, p);
          list.appendChild(li);
        });
      });
    };
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(run, 180); });
    var q0 = new URLSearchParams(location.search).get("q");
    if (q0) { input.value = q0; run(); }
  }

  // ------------------------------------------------------ copy and share
  var copy = document.querySelector("[data-copy]");
  if (copy && navigator.clipboard) {
    copy.hidden = false;
    copy.addEventListener("click", function () {
      navigator.clipboard.writeText(location.href).then(function () {
        copy.querySelector("span").textContent = copy.dataset.done;
      });
    });
  }
  var share = document.querySelector("[data-share]");
  if (share && navigator.share) {
    share.hidden = false;
    share.addEventListener("click", function () {
      navigator.share({ title: document.title, url: location.href }).catch(function () {});
    });
  }
})();
