import { $, $$, h, api, toast, fmtAgo } from "./app.js";

let saved = null; // settings as last loaded from the server

const get = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
function set(obj, path, value) {
  const keys = path.split(".");
  let o = obj;
  for (const k of keys.slice(0, -1)) o = o[k] ??= {};
  o[keys.at(-1)] = value;
}

const PERCENT = { pct: (v) => `${Math.round(v * 100)} %`, pct2: (v) => `${(v * 100).toFixed(2)} %` };
function syncOutput(input) {
  const out = input.parentElement.querySelector("output");
  if (out) out.textContent = PERCENT[input.dataset.fmt]?.(Number(input.value)) ?? input.value;
}

function readInput(el) {
  if (el.dataset.type === "bool") return el.checked;
  if (el.dataset.type === "number") return el.value === "" ? null : Number(el.value);
  if (el.dataset.type === "lines") return el.value.split("\n").map((s) => s.trim()).filter(Boolean);
  return el.value;
}
function writeInput(el, v) {
  if (el.dataset.type === "bool") el.checked = !!v;
  else if (el.dataset.type === "lines") el.value = (v || []).join("\n");
  else el.value = v ?? "";
  if (el.type === "range") syncOutput(el);
}

// ---- cameras ----
function cameraFieldset(cam, index) {
  const fs = $("#camera-tpl").content.firstElementChild.cloneNode(true);
  fs.dataset.camId = cam.id;
  fs.querySelector("legend").textContent = `Camera ${cam.id + 1}`;
  fs.querySelectorAll("[data-cam]").forEach((el) => {
    const id = `cam${index}-${el.dataset.cam}`;
    el.id = id;
    const label = el.closest(".field").querySelector("label");
    if (label) label.htmlFor = id;
    if (el.dataset.cam === "resolution") {
      const val = cam.resolution.join("x");
      if (![...el.options].some((o) => o.value === val)) el.append(h("option", { value: val }, cam.resolution.join(" × ")));
      el.value = val;
    } else writeInput(el, cam[el.dataset.cam]);
    if (el.dataset.cam === "enabled") el.dataset.type = "bool";
    if (["hflip", "vflip"].includes(el.dataset.cam)) el.dataset.type = "bool";
    if (el.type === "checkbox") el.checked = !!cam[el.dataset.cam];
  });
  updateConditional(fs);
  return fs;
}

function readCamera(fs) {
  const cam = { id: Number(fs.dataset.camId) };
  fs.querySelectorAll("[data-cam]").forEach((el) => {
    const k = el.dataset.cam;
    if (k === "resolution") cam[k] = el.value.split("x").map(Number);
    else if (el.type === "checkbox") cam[k] = el.checked;
    else cam[k] = readInput(el);
  });
  return cam;
}

function updateConditional(fs) {
  fs.querySelectorAll("[data-show]").forEach((f) => {
    const [k, v] = f.dataset.show.split("=");
    f.hidden = fs.querySelector(`[data-cam="${k}"]`).value !== v;
  });
}

function renderCameras(cams) {
  $("#cameras").replaceChildren(...cams.map(cameraFieldset));
  $("#cam-actions").replaceChildren(cams.length < 2
    ? h("button", { type: "button", onclick: addCamera }, "Add a second camera")
    : h("button", { type: "button", class: "btn-danger", onclick: removeCamera }, "Remove camera 2"));
}
function addCamera() {
  const cams = currentCameras();
  cams.push({ ...saved.cameras[0], id: 1, name: "Camera 2", enabled: true });
  renderCameras(cams); markDirty();
}
function removeCamera() {
  renderCameras(currentCameras().filter((c) => c.id === 0)); markDirty();
}
const currentCameras = () => $$("#cameras fieldset.camera").map(readCamera);

// ---- form state ----
function fill(s) {
  saved = s;
  $$("[data-path]").forEach((el) => writeInput(el, get(s, el.dataset.path)));
  $$("[data-class]").forEach((el) => { el.checked = s.detection.classes.includes(el.dataset.class); });
  renderCameras(s.cameras);
  clearErrors();
  markDirty();
}

function buildPatch() {
  const patch = {};
  for (const el of $$("[data-path]")) {
    const v = readInput(el);
    if (JSON.stringify(v) !== JSON.stringify(get(saved, el.dataset.path))) set(patch, el.dataset.path, v);
  }
  const classes = $$("[data-class]").filter((e) => e.checked).map((e) => e.dataset.class);
  const extra = saved.detection.classes.filter((c) => !$$("[data-class]").some((e) => e.dataset.class === c));
  const all = [...classes, ...extra];
  if (JSON.stringify([...all].sort()) !== JSON.stringify([...saved.detection.classes].sort()))
    set(patch, "detection.classes", all);
  const cams = currentCameras();
  const norm = (list) => JSON.stringify(list.map((c) => ({ ...c })));
  if (norm(cams) !== norm(saved.cameras.map((c) => Object.fromEntries(Object.keys(cams[0] || c).map((k) => [k, c[k]])))))
    patch.cameras = cams;
  return patch;
}

function markDirty() {
  if (!saved) return;
  const n = Object.keys(buildPatch()).length;
  $("#save").disabled = $("#revert").disabled = !n;
  $(".savebar").hidden = !n;
  $("#dirty").textContent = n ? "You have unsaved changes" : "No unsaved changes";
}

function clearErrors() { $$(".field .err").forEach((e) => e.remove()); }
function showErrors(errors) {
  clearErrors();
  let first = null;
  for (const { field, message } of errors) {
    const path = field.replace(/^body\./, "");
    let el = $(`[data-path="${path}"]`);
    const m = path.match(/^cameras\.(\d+)\.(\w+)/);
    if (m) el = $$("#cameras fieldset.camera")[Number(m[1])]?.querySelector(`[data-cam="${m[2]}"]`);
    if (path.startsWith("detection.classes")) el = $("#classes");
    if (!el) el = $(`[data-path^="${path}."]`)?.closest("fieldset")?.querySelector(".fields");
    const field_ = el?.closest(".field") || el;
    if (field_) { field_.append(h("span", { class: "err" }, message.replace(/^Value error, /, ""))); first ??= field_; }
    else toast(`${path}: ${message}`, "error");
  }
  first?.scrollIntoView({ behavior: "smooth", block: "center" });
}

$("#form").addEventListener("input", (e) => {
  if (e.target.type === "range") syncOutput(e.target);
  const fs = e.target.closest("fieldset.camera");
  if (fs) updateConditional(fs);
  markDirty();
});
$("#form").addEventListener("change", markDirty);
$("#f-updates.channel")?.addEventListener("change", markDirty);

$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const patch = buildPatch();
  if (!Object.keys(patch).length) return;
  $("#save").disabled = true;
  try {
    fill(await api("/api/settings", { method: "PATCH", body: patch }));
    toast("Settings saved");
  } catch (err) {
    if (err.data?.errors) { showErrors(err.data.errors); toast("Some settings need fixing", "error"); }
    else toast(err.message, "error");
    markDirty();
  }
});
$("#revert").addEventListener("click", () => fill(saved));

$("#test-notify").addEventListener("click", async (e) => {
  if (Object.keys(buildPatch().notifications || {}).length) { toast("Save your changes first", "error"); return; }
  e.target.disabled = true;
  try { await api("/api/notifications/test", { method: "POST" }); toast("Test notification sent"); }
  catch (err) { toast(err.message, "error"); }
  finally { e.target.disabled = false; }
});

// ---- password ----
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/auth/password", { method: "POST", body: { current: $("#pw-current").value, new: $("#pw-new").value } });
    e.target.reset(); toast("Password changed. Other devices are signed out.");
  } catch (err) {
    toast(err.data?.detail?.[0]?.msg ? "New password must be at least 8 characters" : err.message, "error");
  }
});
$("#logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  location.href = "/login";
});

// ---- system ----
async function loadVersion(force = false) {
  const v = await api(`/api/system/version${force ? "?force=1" : ""}`);
  const rows = [h("dt", {}, "Installed"), h("dd", {}, `${v.current.version}${v.current.commit ? ` (${v.current.commit})` : ""}`)];
  if (v.latest) rows.push(h("dt", {}, "Latest"), h("dd", {}, v.latest));
  if (v.checked_at) rows.push(h("dt", {}, "Checked"), h("dd", {}, fmtAgo(v.checked_at)));
  $("#version-info").replaceChildren(...rows);
  $("#do-update").hidden = !v.available;
  if (v.error) $("#update-status").textContent = v.error;
  else if (force) $("#update-status").textContent = v.available ? "An update is available." : "Pirdfy is up to date.";
}

async function pollUpdate() {
  const { pending, status } = await api("/api/system/update").catch(() => ({}));
  const el = $("#update-status");
  if (pending) el.textContent = "Waiting for the updater to start…";
  else if (status?.state === "running") el.textContent = `Updating: ${status.message || "working"}…`;
  else if (status) el.textContent = `Last update ${fmtAgo(status.finished_at || status.started_at)}: ${status.message}`;
  return pending || status?.state === "running";
}

$("#check-update").addEventListener("click", () => loadVersion(true).catch((e) => toast(e.message, "error")));
$("#do-update").addEventListener("click", async () => {
  if (!confirm("Install the update now? Pirdfy restarts and is unavailable for a minute or two.")) return;
  try { await api("/api/system/update", { method: "POST" }); } catch (e) { toast(e.message, "error"); return; }
  $("#do-update").hidden = true;
  const timer = setInterval(async () => {
    try { if (!(await pollUpdate())) { clearInterval(timer); loadVersion(true); } }
    catch { $("#update-status").textContent = "Restarting…"; }
  }, 3000);
});
$("#restart").addEventListener("click", async () => {
  if (!confirm("Restart Pirdfy? The camera stops for a few seconds.")) return;
  await api("/api/system/restart", { method: "POST" }).catch(() => {});
  toast("Restarting…");
  setTimeout(() => location.reload(), 6000);
});

window.addEventListener("beforeunload", (e) => { if (!$("#save").disabled) e.preventDefault(); });

try {
  fill(await api("/api/settings"));
  const st = await api("/api/status");
  $("#disk-info").textContent = `${st.disk.media_gb} GB used by Pirdfy, ${st.disk.free_gb} GB free of ${st.disk.total_gb} GB.`;
  loadVersion().catch(() => {});
  pollUpdate();
  if (location.hash) document.querySelector(location.hash)?.scrollIntoView();
} catch (e) { toast(e.message, "error"); }
