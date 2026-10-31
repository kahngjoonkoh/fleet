// The Terminal tab: xterm.js talking to fleetd through /terminal/ws. Binary frames are terminal
// bytes; a text frame {"resize": [cols, rows]} tells fleetd the window size. On touch screens a
// Termux-style key bar adds the keys a phone keyboard lacks; CTRL and ALT apply to the next key.
"use strict";

const FONT_KEY = "fleet.term.fontSize";
const touch = matchMedia("(pointer: coarse)").matches;

function load(key, fallback) {
  try { return Number(localStorage.getItem(key)) || fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, String(value)); } catch {}
}

const term = new Terminal({
  fontSize: load(FONT_KEY, touch ? 13 : 14),
  fontFamily: 'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, "Liberation Mono", monospace',
  cursorBlink: true,
  scrollback: 5000,
  theme: { background: "#111111", foreground: "#e6e6e6" },
});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open(document.getElementById("term"));
// Phone keyboards otherwise autocorrect and capitalize what you type into the shell.
for (const [k, v] of [["autocorrect", "off"], ["autocapitalize", "off"], ["autocomplete", "off"], ["spellcheck", "false"]]) {
  term.textarea.setAttribute(k, v);
}

const encoder = new TextEncoder();
const statusBox = document.getElementById("term-status");
const statusText = statusBox.querySelector("span");
const statusButton = document.getElementById("reconnect");
const device = new URLSearchParams(location.search).get("device") || "";
// ?agent= or ?tmux=: this page shows that tmux session rather than a shell of its own.
const { agent, tmux } = document.getElementById("term").dataset;

// fleetd keeps a shell running for 30 minutes after its connection drops, under this tab's session
// id, so reconnecting (or reloading the page) resumes it. Each device, and each agent, gets its own id per tab.
const SESSION_KEY = `fleet.term.session.${device || "hub"}${agent ? ".agent." + agent : tmux ? ".tmux." + tmux : ""}`;
let sessionId = null;
try { sessionId = sessionStorage.getItem(SESSION_KEY); } catch {}
function newSession() {
  sessionId = crypto.randomUUID();
  try { sessionStorage.setItem(SESSION_KEY, sessionId); } catch {}
}
if (!sessionId) newSession();

const EXITED = 4000, TAKEN_OVER = 4001;
let ws = null;
let retries = 0;
let retryTimer = null;

function showStatus(text, button) {
  statusText.textContent = text;
  statusButton.hidden = !button;
  if (button) statusButton.textContent = button;
  statusBox.hidden = false;
}

function connect() {
  clearTimeout(retryTimer);
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  const params = new URLSearchParams({ session: sessionId });
  if (device) params.set("device", device);
  if (agent) params.set("agent", agent);
  else if (tmux) params.set("tmux", tmux);
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/terminal/ws?${params}`);
  socket.binaryType = "arraybuffer";
  let first = true;
  socket.onopen = () => { retries = 0; statusBox.hidden = true; sendSize(); term.focus(); };
  socket.onmessage = (e) => {
    if (first) { term.reset(); first = false; }  // the first message repaints the screen from scratch
    term.write(new Uint8Array(e.data));
  };
  socket.onclose = (e) => {
    if (ws !== socket) return;
    ws = null;
    // For a tmux session, ending the connection only detaches; the agent keeps running.
    if (e.code === EXITED) return agent || tmux ? showStatus("Detached.", "Reattach") : showStatus("Shell exited.", "New shell");
    if (e.code === TAKEN_OVER) return showStatus("Opened in another tab.", "Use here");
    scheduleReconnect();
  };
  ws = socket;
}

// Phones pause pages in the background and drop connections when the network changes, so
// reconnect quietly: quickly at first, then every 10 s, and at once when the page or network returns.
function scheduleReconnect() {
  showStatus("Reconnecting…", "Retry now");
  if (document.hidden || !navigator.onLine) return;
  retryTimer = setTimeout(connect, Math.min(10000, 500 * 2 ** retries++));
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && !ws && statusButton.textContent === "Retry now") connect(); });
window.addEventListener("online", () => { if (!ws && statusButton.textContent === "Retry now") connect(); });

statusButton.addEventListener("click", () => {
  if (statusButton.textContent === "New shell" || statusButton.textContent === "Reattach") newSession();
  connect();
});

function send(data) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
}
function sendSize() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ resize: [term.cols, term.rows] }));
}

// --- sticky modifiers -------------------------------------------------------
const mods = { ctrl: false, alt: false };
const modButtons = {};
for (const b of document.querySelectorAll("[data-mod]")) modButtons[b.dataset.mod] = b;

function setMod(name, on) {
  mods[name] = on;
  modButtons[name].classList.toggle("on", on);
}
function ctrlChar(ch) {
  const c = ch.toLowerCase();
  if (c >= "a" && c <= "z") return String.fromCharCode(c.charCodeAt(0) - 96);
  const map = { " ": "\x00", "@": "\x00", "[": "\x1b", "\\": "\x1c", "]": "\x1d", "^": "\x1e", "_": "\x1f", "?": "\x7f" };
  return map[ch] ?? ch;
}
function applyMods(data) {
  if (!mods.ctrl && !mods.alt) return data;
  let out = data;
  if (mods.ctrl && data.length === 1) out = ctrlChar(data);
  if (mods.alt) out = "\x1b" + out;
  setMod("ctrl", false);
  setMod("alt", false);
  return out;
}

term.onData((data) => send(applyMods(data)));

// Phone keyboards compose a whole word before committing it, so a "c" typed after CTRL would sit
// in the suggestion bar instead of reaching the shell. While a modifier is lit, take the next
// character straight from the input events (captured before xterm's own listeners), then cancel
// the keyboard's composition and ignore its leftover events for a moment.
let swallowUntil = 0;
for (const type of ["beforeinput", "compositionstart", "compositionupdate", "compositionend", "input"]) {
  term.element.addEventListener(type, (e) => {
    const armed = mods.ctrl || mods.alt;
    if (!armed && Date.now() > swallowUntil) return;
    e.stopPropagation();
    if (!armed || type === "compositionstart" || type === "compositionend") return;
    const ch = (e.data || "").slice(-1);
    if (!ch) return;
    e.preventDefault();
    send(applyMods(ch));
    swallowUntil = Date.now() + 400;
    term.textarea.value = "";
    term.textarea.blur();
    term.textarea.focus();
  }, true);
}

// --- key bar ----------------------------------------------------------------
const CSI = { up: "A", down: "B", right: "C", left: "D", home: "H", end: "F" };
const TILDE = { pgup: "5", pgdn: "6" };

function keySequence(name) {
  if (name === "esc") return "\x1b";
  if (name === "tab") return "\t";
  const m = 1 + (mods.alt ? 2 : 0) + (mods.ctrl ? 4 : 0);
  let seq;
  if (name in CSI) {
    const app = term.modes.applicationCursorKeysMode && m === 1 && "ABCD".includes(CSI[name]);
    seq = m > 1 ? `\x1b[1;${m}${CSI[name]}` : (app ? "\x1bO" : "\x1b[") + CSI[name];
  } else {
    seq = m > 1 ? `\x1b[${TILDE[name]};${m}~` : `\x1b[${TILDE[name]}~`;
  }
  setMod("ctrl", false);
  setMod("alt", false);
  return seq;
}

function setFont(delta) {
  const size = Math.min(28, Math.max(8, term.options.fontSize + delta));
  term.options.fontSize = size;
  save(FONT_KEY, size);
  refit();
}

document.getElementById("term-font").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) setFont(Number(b.dataset.font));
});

// Two-finger pinch on the terminal resizes the text, as in Termux.
let pinch = null;
const spread = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
term.element.addEventListener("touchstart", (e) => {
  if (e.touches.length === 2) pinch = { start: spread(e.touches), size: term.options.fontSize };
}, { passive: true });
term.element.addEventListener("touchmove", (e) => {
  if (!pinch || e.touches.length !== 2) return;
  e.preventDefault();
  const size = Math.round(pinch.size * spread(e.touches) / pinch.start);
  if (size !== term.options.fontSize) setFont(size - term.options.fontSize);
}, { passive: false });
term.element.addEventListener("touchend", (e) => { if (e.touches.length < 2) pinch = null; });

// Holding an arrow or page key repeats it, as in Termux.
const REPEATS = new Set(["up", "down", "left", "right", "pgup", "pgdn"]);
let repeat = null;
function stopRepeat() {
  clearTimeout(repeat);
  clearInterval(repeat);
  repeat = null;
}

// pointerdown + preventDefault keeps focus in the terminal, so the phone keyboard stays open.
const keys = document.getElementById("keys");
keys.addEventListener("pointerdown", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  e.preventDefault();
  if (b.dataset.mod) setMod(b.dataset.mod, !mods[b.dataset.mod]);
  else if (b.dataset.s) send(applyMods(b.dataset.s));
  else if (b.dataset.k === "esc" || b.dataset.k === "tab") send(applyMods(keySequence(b.dataset.k)));
  else {
    // Modifiers apply to the first press only; repeats send the plain key.
    send(keySequence(b.dataset.k));
    if (REPEATS.has(b.dataset.k)) {
      stopRepeat();
      repeat = setTimeout(() => { repeat = setInterval(() => send(keySequence(b.dataset.k)), 60); }, 400);
    }
  }
});
for (const type of ["pointerup", "pointercancel", "pointerleave"]) keys.addEventListener(type, stopRepeat);

// --- sizing -----------------------------------------------------------------
// iOS ignores interactive-widget, so follow the visual viewport to stay above the keyboard.
function refit() {
  if (window.visualViewport) {
    document.documentElement.style.setProperty("--app-h", `${window.visualViewport.height}px`);
    window.scrollTo(0, 0);
  }
  fit.fit();
}
term.onResize(sendSize);
window.addEventListener("resize", refit);
window.visualViewport?.addEventListener("resize", refit);

refit();
connect();
