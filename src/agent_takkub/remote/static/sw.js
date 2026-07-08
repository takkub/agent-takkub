"use strict";

/*
 * Offline app-shell cache for the Takkub Remote PWA. Same-origin only —
 * no external hosts, no CDN. Push handling is a P3 stub: notify.py (P1)
 * delivers Lead updates over the SSE stream while the tab is open; actual
 * Web Push (needs VAPID keys) is out of scope for this phase.
 */

var CACHE_NAME = "takkub-remote-shell-v16";
var SHELL_FILES = ["./", "./index.html", "./app.js", "./manifest.webmanifest"];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(SHELL_FILES);
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys
          .filter(function (key) { return key !== CACHE_NAME; })
          .map(function (key) { return caches.delete(key); })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;

  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // never touch cross-origin

  // API calls always go to the network — never serve stale pane/lead data.
  if (url.pathname.indexOf("/api/") !== -1) return;

  event.respondWith(
    caches.match(req).then(function (cached) {
      var network = fetch(req)
        .then(function (res) {
          if (res && res.ok) {
            var copy = res.clone();
            caches.open(CACHE_NAME).then(function (cache) { cache.put(req, copy); });
          }
          return res;
        })
        .catch(function () { return cached; });
      return cached || network;
    })
  );
});

// P3 placeholder — kept inert until Web Push (VAPID) lands.
self.addEventListener("push", function (event) {
  if (!event.data) return;
  var text;
  try {
    text = event.data.json().text || "";
  } catch (e) {
    text = event.data.text();
  }
  if (!text) return;
  event.waitUntil(
    self.registration.showNotification("Takkub Lead", { body: text, icon: undefined })
  );
});
