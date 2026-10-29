// On every page: registers the service worker that makes Fleet installable, and closes the ☰ menu
// on a tap outside it or Escape.
"use strict";
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

const menu = document.querySelector("details.menu");
if (menu) {
  document.addEventListener("click", (e) => { if (menu.open && !menu.contains(e.target)) menu.open = false; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.open = false; });
}
