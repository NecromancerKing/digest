// Optional comfort (spec section 10): every page reads without this file.
(function () {
  "use strict";
  var root = document.documentElement;

  function save(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode: no memory */ }
  }

  // Theme: follows the system until the reader picks one.
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

  // Copy link and Share, only where the browser supports them.
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
