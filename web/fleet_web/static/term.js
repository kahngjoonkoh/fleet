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
let ws = null;

function connect() {
  statusBox.hidden = true;
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/terminal/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { sendSize(); term.focus(); };
  ws.onmessage = (e) => term.write(new Uint8Array(e.data));
  ws.onclose = () => {
    statusBox.querySelector("span").textContent = "Disconnected.";
    statusBox.hidden = false;
  };
}

function send(data) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
}
function sendSize() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ resize: [term.cols, term.rows] }));
}

document.getElementById("reconnect").addEventListener("click", () => { term.reset(); connect(); });

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

// pointerdown + preventDefault keeps focus in the terminal, so the phone keyboard stays open.
document.getElementById("keys").addEventListener("pointerdown", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  e.preventDefault();
  if (b.dataset.mod) setMod(b.dataset.mod, !mods[b.dataset.mod]);
  else if (b.dataset.font) setFont(Number(b.dataset.font));
  else if (b.dataset.s) send(applyMods(b.dataset.s));
  else if (b.dataset.k === "esc" || b.dataset.k === "tab") send(applyMods(keySequence(b.dataset.k)));
  else send(keySequence(b.dataset.k));
});

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
