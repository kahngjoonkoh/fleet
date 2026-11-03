// Files tab: uploads (the picker, or files dropped on the page) go one at a time, each in pieces small
// enough for the tunnel, with a progress bar, then the folder reloads; and the editor's Save. No libraries.
"use strict";

const PIECE = 32 * 1024 * 1024;  // Cloudflare refuses a request over 100 MB
const TRIES = 20;  // in a row without progress, about 8 minutes

function writeUrl(device, path, extra) {
  const q = new URLSearchParams({ path, ...extra });
  if (device) q.set("device", device);
  return "/files/write?" + q;
}

// Resolves to the JSON answer, or { error, retry } when trying again could help.
function send(url, body, csrf, onProgress, onStart) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("X-Fleet-CSRF", csrf);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    if (onProgress) xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      const retry = xhr.status >= 500 || xhr.status === 429;
      try {
        resolve({ ...JSON.parse(xhr.responseText), retry });
      } catch {
        resolve({ error: retry ? `Failed (${xhr.status}).` : "Failed. Reload the page and try again.", retry });
      }
    };
    xhr.onerror = () => resolve({ error: "Connection lost.", retry: true });
    xhr.onabort = () => resolve({ error: "Cancelled." });
    if (onStart) onStart(xhr);
    xhr.send(body);
  });
}

function amount(n) {
  if (n < 1 << 20) return Math.round(n / 1024) + " KB";
  return n < 1 << 30 ? Math.round(n / (1 << 20)) + " MB" : (n / (1 << 30)).toFixed(2) + " GB";
}

const pause = (seconds) => new Promise((done) => setTimeout(done, seconds * 1000));

// The same file to the same place gets the same id, so uploading it again continues it.
function uploadId(file, target) {
  let hash = 2166136261;
  for (const c of target) hash = Math.imul(hash ^ c.codePointAt(0), 16777619);
  return [file.size, file.lastModified, hash >>> 0].map((n) => n.toString(36)).join("-");
}

const picker = document.getElementById("upload");
if (picker) {
  const { device, path, csrf } = picker.dataset;
  const list = document.getElementById("uploads");
  let busy = false;

  async function sendFile(file, target, ui) {
    const id = { upload: uploadId(file, target), total: file.size };
    const url = (step, extra) => writeUrl(device, target, { ...id, step, ...extra });
    let tries = 0, offset = 0, lastSent = false;
    const problem = async (r) => {  // true: carry on from wherever the file stands now
      if (ui.cancelled || !r.retry || ++tries > TRIES) return false;
      ui.note(`${r.error} Trying again…`);
      await pause(Math.min(2 ** (tries - 1), 30));
      return !ui.cancelled;
    };
    for (;;) {
      const s = await send(url("start"), null, csrf);
      if (s.error) {
        if (await problem(s)) continue;
        return s;
      }
      // The last piece went out, the answer got lost, and the pieces are gone: it did finish.
      if (lastSent && offset > 0 && s.have === 0) return { finished: true };
      offset = s.have;
      for (;;) {
        const end = Math.min(offset + PIECE, file.size);
        const from = offset;
        lastSent = end === file.size;
        const r = await send(url("piece", { offset: from }), file.slice(from, end), csrf,
          (p) => ui.progress(from + p * (end - from)), (xhr) => { ui.xhr = xhr; });
        if (r.error) {
          if (await problem(r)) break;
          return r;
        }
        tries = 0;
        if (r.done) return r;
        offset = r.have;
        ui.progress(offset);
      }
    }
  }

  async function upload(files) {
    if (busy || !files.length) return;
    busy = true;
    list.hidden = false;
    let failed = 0;
    for (const file of files) {
      const row = document.createElement("div");
      row.className = "upload";
      const name = document.createElement("span");
      name.textContent = file.name;
      const bar = document.createElement("progress");
      bar.max = 1; bar.value = 0;
      const note = document.createElement("span");
      note.className = "muted small";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "small-btn";
      cancel.textContent = "Cancel";
      row.append(name, bar, cancel, note);
      list.append(row);
      const target = path.replace(/\/$/, "") + "/" + file.name;
      const ui = {
        cancelled: false, xhr: null,
        note: (text) => { note.textContent = text; },
        progress: (n) => {
          bar.value = file.size ? n / file.size : 1;
          note.textContent = `${amount(n)} of ${amount(file.size)}`;
        },
      };
      cancel.addEventListener("click", () => { ui.cancelled = true; if (ui.xhr) ui.xhr.abort(); });
      const r = await sendFile(file, target, ui);
      cancel.remove();
      if (ui.cancelled) {
        await send(writeUrl(device, target, { upload: uploadId(file, target), total: file.size, step: "cancel" }), null, csrf);
        row.remove();
      } else if (r.error) {
        failed++;
        row.classList.add("failed");
        note.textContent = r.retry ? `${r.error} Upload the same file again to continue.` : r.error;
      } else {
        bar.value = 1;
        const saved = r.path ? r.path.split("/").pop() : file.name;
        note.textContent = r.finished ? "finished; check it in the folder" : saved !== file.name ? `saved as ${saved}` : "done";
      }
    }
    busy = false;
    if (!failed) location.reload();
  }

  window.addEventListener("beforeunload", (e) => { if (busy) e.preventDefault(); });
  picker.addEventListener("change", () => { upload([...picker.files]); picker.value = ""; });
  // Drag and drop anywhere on the page (desktop).
  let depth = 0;
  document.addEventListener("dragenter", (e) => { if (e.dataTransfer.types.includes("Files")) { depth++; document.body.classList.add("dropping"); } });
  document.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; document.body.classList.remove("dropping"); } });
  document.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); });
  document.addEventListener("drop", (e) => {
    if (!e.dataTransfer.files.length) return;
    e.preventDefault();
    depth = 0;
    document.body.classList.remove("dropping");
    upload([...e.dataTransfer.files]);
  });
}

const editor = document.getElementById("editor");
if (editor) {
  const { device, path, csrf, view } = editor.dataset;
  const text = editor.elements.text;
  const status = document.getElementById("save-status");
  let saved = text.value;
  document.getElementById("wrap").addEventListener("change", (e) => { text.wrap = e.target.checked ? "soft" : "off"; });
  // Tab inserts a tab instead of leaving the box.
  text.addEventListener("keydown", (e) => {
    if (e.key !== "Tab" || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return;
    e.preventDefault();
    text.setRangeText("\t", text.selectionStart, text.selectionEnd, "end");
  });
  document.addEventListener("keydown", (e) => {  // Ctrl+S / Cmd+S saves
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); editor.requestSubmit(); }
  });
  window.addEventListener("beforeunload", (e) => { if (text.value !== saved) e.preventDefault(); });
  editor.addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = document.getElementById("save");
    button.disabled = true;
    status.textContent = "Saving…";
    const r = await send(writeUrl(device, path, { overwrite: "1", mtime: editor.dataset.mtime }),
                         new Blob([text.value], { type: "text/plain" }), csrf);
    button.disabled = false;
    if (r.error) {
      status.textContent = r.error;
      status.classList.add("error-text");
      return;
    }
    saved = text.value;
    location.href = view;
  });
}
