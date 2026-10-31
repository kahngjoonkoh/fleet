// On every page: registers the service worker that makes Fleet installable, closes the ☰ menu and the
// Terminal tab's device switcher on a tap outside them or Escape, and keeps live parts of pages fresh.
"use strict";
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

const menus = document.querySelectorAll("details.menu");
document.addEventListener("click", (e) => {
  for (const m of menus) if (m.open && !m.contains(e.target)) m.open = false;
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") for (const m of menus) m.open = false; });

// Copy buttons next to commands. Clipboard access needs HTTPS, which Fleet always has behind its tunnel;
// if it's refused, select the text so a long-press or Ctrl+C still works.
document.addEventListener("click", async (e) => {
  const button = e.target.closest("button.copy");
  if (!button) return;
  const code = button.parentElement.querySelector("code");
  try {
    await navigator.clipboard.writeText(code.textContent);
    button.textContent = "Copied";
    button.classList.add("done");
    setTimeout(() => { button.textContent = "Copy"; button.classList.remove("done"); }, 1500);
  } catch {
    getSelection().selectAllChildren(code);
  }
});

// Forms that end something (stopping or removing an agent) ask first; so does renaming a running agent.
document.addEventListener("submit", (e) => {
  const form = e.target;
  let message = form.dataset.confirm;
  const renamed = form.querySelector("[data-confirm-if-renamed]");
  if (renamed && form.elements.name && form.elements.name.value.trim() !== renamed.dataset.confirmIfRenamed) {
    message = "Renaming restarts the agent if it's running. Claude Code and Pi pick their conversation back up. Rename it?";
  }
  if (message && !confirm(message)) e.preventDefault();
});

// --- live pages -------------------------------------------------------------------------
// Parts of a page marked data-live (agent cards, devices) refresh quietly every 10 seconds while the
// page is in view: the page is fetched again and only those parts are swapped, and only when they
// changed and you're not in the middle of something there (typing, an open menu, a button at work).
const live = document.querySelectorAll("[data-live]");
let refreshing = false;

function busy(region) {
  return region.contains(document.activeElement) && document.activeElement !== document.body
    || region.querySelector("details[open], [aria-busy=true]");
}

function swap(html, force = false) {
  const fresh = new DOMParser().parseFromString(html, "text/html");
  for (const region of document.querySelectorAll("[data-live]")) {
    const next = fresh.querySelector(`[data-live="${region.dataset.live}"]`);
    if (next && next.innerHTML !== region.innerHTML && (force || !busy(region))) region.innerHTML = next.innerHTML;
  }
}

async function refresh() {
  if (refreshing || document.hidden || !live.length) return;
  refreshing = true;
  try {
    const r = await fetch(location.href, { headers: { "X-Fleet-Live": "1" }, cache: "no-store" });
    if (r.ok && new URL(r.url).pathname === location.pathname) swap(await r.text());
  } catch {
    // offline for a moment; the next tick tries again
  } finally {
    refreshing = false;
  }
}
if (live.length) {
  setInterval(refresh, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}

// Buttons in live parts (Start, Stop, Pin, Remove, ...) act in place: the button shows it's working,
// and the parts update from the page the server answers with, without reloading.
document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (e.defaultPrevented || form.method !== "post" || !form.closest("[data-live]")) return;
  e.preventDefault();
  const button = form.querySelector("button");
  const text = button && !button.querySelector("svg");  // icon buttons just dim (style.css)
  if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
  if (text) { button.dataset.label = button.textContent; button.textContent += "…"; }
  try {
    const r = await fetch(form.action, { method: "POST", body: new FormData(form) });
    const html = await r.text();
    if (r.ok && new URL(r.url).pathname === location.pathname) {
      swap(html, true);
    } else {  // an error page, or somewhere else: show what the server said
      const page = new DOMParser().parseFromString(html, "text/html");
      document.querySelector("main").replaceWith(page.querySelector("main"));
    }
  } catch {
    form.submit();  // fall back to an ordinary submit
  } finally {
    if (button && button.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); if (text) button.textContent = button.dataset.label; }
  }
});
