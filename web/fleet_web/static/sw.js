// Fleet's service worker. Fleet is live data and a live shell, so nothing is cached: pages always
// come from the network, and only when that fails does it show a short offline notice.
"use strict";

const OFFLINE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#151514;
color:#ecebe6;font:15px/1.5 system-ui,sans-serif;text-align:center;padding:16px">
<div><p style="font-weight:700;font-size:18px;margin:0 0 6px">Fleet is offline</p>
<p style="color:#a09f99;margin:0">Can't reach fleet-web. Check your connection, then pull down or reopen to retry.</p></div>`;

self.addEventListener("install", () => self.skipWaiting());
// Navigation preload: the browser starts loading the page while this worker starts up, instead of after.
self.addEventListener("activate", (e) => e.waitUntil(Promise.all([
  self.clients.claim(),
  self.registration.navigationPreload ? self.registration.navigationPreload.enable() : null,
])));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith((async () => {
    try {
      return (await e.preloadResponse) || (await fetch(e.request));
    } catch {
      return new Response(OFFLINE, { headers: { "Content-Type": "text/html" } });
    }
  })());
});
