// Forms with a device picker (New agent, New project): offers only the agent types and folder
// suggestions of the chosen device, shows the command field for a custom command, and names the
// agent after its folder until you type a name. Folder suggestions open in Fleet's own list rather
// than the browser's; without this script the <datalist> still works. Also Settings > Brain, which
// shows only the fields the chosen writer needs.
"use strict";

const form = document.getElementById("new-agent") || document.querySelector("form[data-picker]");
if (form) {
  const { name, folder } = form.elements;
  const commandField = document.getElementById("command-field");
  const typeInputs = [...form.querySelectorAll('input[name="type"]')];
  let named = name.value !== "";
  let suggestionsId = "folders-1";

  const checked = (field) => form.querySelector(`input[name="${field}"]:checked`);

  function update() {
    const device = checked("device");
    suggestionsId = device.dataset.list;
    for (const note of form.querySelectorAll(".linger")) note.hidden = note.dataset.device !== device.value;
    if (!typeInputs.length) return;
    const types = device.dataset.types.split(" ");
    for (const input of typeInputs) input.closest(".chip").hidden = input.disabled = !types.includes(input.value);
    if (!checked("type") || checked("type").disabled) typeInputs.find((i) => !i.disabled).checked = true;
    const type = checked("type").value;
    commandField.hidden = type !== "custom";
    const rc = document.getElementById("rc-field");
    if (rc) rc.hidden = type !== "claude";  // only Claude Code has Remote Control
    form.elements.command.required = type === "custom";
  }

  name.addEventListener("input", () => { named = name.value !== ""; });
  folder.addEventListener("input", () => {
    if (named) return;
    const base = folder.value.replace(/\/+$/, "").split("/").pop();
    let suggestion = base.replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^[-_]+/, "").slice(0, 32);
    if (name.pattern.startsWith("[a-z0-9]")) suggestion = suggestion.toLowerCase().replace(/_/g, "-");  // project names
    name.value = suggestion;
  });
  form.addEventListener("change", (e) => { if (e.target.name === "device" || e.target.name === "type") update(); });
  update();

  // --- folder suggestions -----------------------------------------------------
  const list = document.createElement("ul");
  list.className = "combo-list";
  list.id = "folder-suggestions";
  list.setAttribute("role", "listbox");
  list.hidden = true;
  folder.after(list);
  folder.removeAttribute("list");
  folder.setAttribute("role", "combobox");
  folder.setAttribute("aria-autocomplete", "list");
  folder.setAttribute("aria-controls", list.id);
  folder.setAttribute("aria-expanded", "false");
  let items = [], active = -1;

  function open() {
    const q = folder.value.trim().toLowerCase();
    const all = [...document.getElementById(suggestionsId).options].map((o) => o.value);
    items = (q && q !== "~" ? all.filter((f) => f.toLowerCase().includes(q) && f !== folder.value.trim()) : all).slice(0, 50);
    active = -1;
    list.replaceChildren(...items.map((path, i) => {
      const li = document.createElement("li");
      li.id = `folder-option-${i}`;
      li.setAttribute("role", "option");
      const cut = path.lastIndexOf("/") + 1;
      const dir = document.createElement("span");
      dir.className = "dir";
      dir.textContent = path.slice(0, cut);
      li.append(dir, path.slice(cut));
      return li;
    }));
    list.hidden = items.length === 0;
    folder.setAttribute("aria-expanded", String(!list.hidden));
  }
  function close() {
    list.hidden = true;
    folder.setAttribute("aria-expanded", "false");
    folder.removeAttribute("aria-activedescendant");
  }
  function highlight(i) {
    active = (i + items.length) % items.length;
    list.querySelectorAll("li").forEach((li, n) => li.setAttribute("aria-selected", String(n === active)));
    const li = list.children[active];
    li.scrollIntoView({ block: "nearest" });
    folder.setAttribute("aria-activedescendant", li.id);
  }
  function choose(i) {
    folder.value = items[i];
    folder.dispatchEvent(new Event("input"));
    close();
  }

  folder.addEventListener("focus", open);
  folder.addEventListener("input", (e) => { if (e.isTrusted) open(); });
  folder.addEventListener("blur", close);
  folder.addEventListener("keydown", (e) => {
    if (list.hidden) { if (e.key === "ArrowDown") { open(); e.preventDefault(); } return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { highlight(active + (e.key === "ArrowDown" ? 1 : -1)); e.preventDefault(); }
    else if (e.key === "Enter" && active >= 0) { choose(active); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
  // pointerdown + preventDefault keeps focus in the field, so the list doesn't close before the tap lands.
  list.addEventListener("pointerdown", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    e.preventDefault();
    choose([...list.children].indexOf(li));
  });
  for (const chip of form.querySelectorAll('input[name="device"]')) chip.addEventListener("change", close);
}

const brainForm = document.getElementById("brain-settings");
if (brainForm) {
  const sync = () => {
    const mode = brainForm.querySelector('input[name="mode"]:checked').value;
    for (const el of brainForm.querySelectorAll("[data-mode]")) el.hidden = !el.dataset.mode.split(" ").includes(mode);
  };
  brainForm.addEventListener("change", sync);
  sync();
}
