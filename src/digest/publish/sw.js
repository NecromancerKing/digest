// Offline reading for the installed app (issue #19). Pages come from the network first, so
// updates reach readers as soon as they're published; the cache answers only when the network
// can't. After the front page or Today loads, the stories they link to are saved (listed in
// /offline.json), and stories that dropped off are removed. No push, no tracking.
"use strict";
var PAGES = "pages-1";
var ASSETS = "assets-1";
var SHELL = ["/", "/today", "/offline"];
var STATIC = [
  "/assets/css/site.css", "/assets/js/site.js", "/assets/img/icon.svg",
  "/assets/fonts/fraunces.woff2", "/assets/fonts/figtree.woff2",
];

self.addEventListener("install", function (e) {
  e.waitUntil(Promise.all([
    caches.open(PAGES).then(function (c) { return c.addAll(SHELL); }),
    caches.open(ASSETS).then(function (c) { return c.addAll(STATIC); }),
  ]).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== PAGES && k !== ASSETS; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  var url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (req.mode === "navigate") e.respondWith(page(e, url));
  else if (url.pathname.indexOf("/assets/") === 0) e.respondWith(asset(req, url));
});

function page(e, url) {
  var key = url.origin + url.pathname;
  return fetch(e.request).then(function (res) {
    if (res.ok && !res.redirected) { // a redirected response can't answer a navigation
      var copy = res.clone();
      e.waitUntil(caches.open(PAGES).then(function (c) { return c.put(key, copy); }));
      if (url.pathname === "/" || url.pathname === "/today") e.waitUntil(saveToday());
    }
    return res;
  }, function () {
    return caches.match(key).then(function (hit) { return hit || caches.match("/offline"); });
  });
}

// Fonts and images never change: cache first. CSS and scripts: network first.
function asset(req, url) {
  return caches.open(ASSETS).then(function (c) {
    if (/\.(woff2|png|svg)$/.test(url.pathname)) {
      return c.match(req).then(function (hit) { return hit || fetchAndPut(c, req); });
    }
    return fetchAndPut(c, req).catch(function () {
      return c.match(req).then(function (hit) { return hit || Response.error(); });
    });
  });
}

function fetchAndPut(c, req) {
  return fetch(req).then(function (res) {
    if (res.ok) c.put(req, res.clone());
    return res;
  });
}

function saveToday() {
  if (navigator.connection && navigator.connection.saveData) return Promise.resolve();
  return fetch("/offline.json", { cache: "no-store" })
    .then(function (res) { return res.json(); })
    .then(function (list) {
      return caches.open(PAGES).then(function (c) {
        return c.keys().then(function (reqs) {
          var have = {};
          var drops = reqs.filter(function (r) {
            var p = new URL(r.url).pathname;
            have[p] = true;
            return p.indexOf("/story/") === 0 && list.pages.indexOf(p) < 0;
          }).map(function (r) { return c.delete(r); });
          var adds = list.pages.filter(function (p) { return !have[p]; })
            .map(function (p) { return c.add(p).catch(function () {}); });
          return Promise.all(drops.concat(adds));
        });
      });
    })
    .catch(function () {});
}
