import { $, h, api, on, toast, fmtAgo, fmtDuration, fmtTime, localDay, renderNotices, boxesFor, plural } from "./app.js";

const cams = new Map(); // id -> {root, img, boxes, chips, pauseBtn, url}
let paused = false;

function camBlock(cam) {
  const img = h("img", { alt: `Live view from ${cam.name}`, width: 640, height: 360 });
  const boxes = h("div", { class: "boxes" });
  const empty = h("div", { class: "cam-empty" }, "Waiting for the first frame…");
  img.addEventListener("load", () => { frame.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`; });
  const chips = h("div", { class: "chips" });
  const saveBtn = h("button", { type: "button", onclick: () => saveSnapshot(cam.id) }, "Save photo");
  const pauseBtn = h("button", { type: "button", onclick: togglePause }, "Pause detection");
  const frame = h("div", { class: "cam" }, img, boxes, empty);
  const root = h("section", { class: "cam-block", "aria-label": cam.name },
    h("div", { class: "cam-bar" }, h("h2", {}, cam.name), chips), frame,
    h("div", { class: "cam-actions" }, saveBtn, pauseBtn));
  const entry = { root, img, boxes, chips, empty, pauseBtn, url: null, busy: false };
  cams.set(cam.id, entry);
  return root;
}

function renderCam(cam) {
  const c = cams.get(cam.id);
  const chips = [];
  if (cam.error) chips.push(h("span", { class: "chip off" }, "Camera error"));
  else if (cam.paused) chips.push(h("span", { class: "chip" }, "Paused"));
  else chips.push(h("span", { class: "chip", title: `${cam.fps} frames/s analysed` }, "Watching"));
  if (cam.visit) chips.push(h("span", { class: "chip present" },
    plural(cam.visit.count, cam.detections?.[0]?.label || "bird") + " here"));
  if (cam.recording) chips.push(h("span", { class: "chip rec" }, "Recording"));
  c.chips.replaceChildren(...chips);
  c.pauseBtn.textContent = cam.paused ? "Resume detection" : "Pause detection";
  if (cam.error) { c.empty.hidden = false; c.empty.textContent = cam.error; }
}

async function refreshFrame(id) {
  const c = cams.get(id);
  if (!c || c.busy || document.hidden) return;
  c.busy = true;
  try {
    const res = await fetch(`/api/cameras/${id}/snapshot.jpg`, { cache: "no-store" });
    if (res.ok) {
      try { boxesFor(JSON.parse(res.headers.get("x-detections") || "[]"), c.boxes); } catch { /* ignore */ }
      const url = URL.createObjectURL(await res.blob());
      c.img.src = url;
      c.empty.hidden = true;
      if (c.url) URL.revokeObjectURL(c.url);
      c.url = url;
    }
  } catch { /* offline for a moment; try again next tick */ }
  finally { c.busy = false; }
}

async function saveSnapshot(id) {
  try { await api(`/api/cameras/${id}/snapshot`, { method: "POST" }); toast("Photo saved to visits"); }
  catch (e) { toast(e.message, "error"); }
}

async function togglePause() {
  try {
    const r = await api(`/api/capture/${paused ? "resume" : "pause"}`, { method: "POST" });
    paused = r.paused;
    toast(paused ? "Detection paused" : "Detection resumed");
    loadStatus();
  } catch (e) { toast(e.message, "error"); }
}

function renderHealth(s) {
  const sys = s.system, rows = [];
  const add = (k, v, bad = false) => rows.push(h("dt", {}, k), h("dd", { class: bad ? "bad" : null }, v));
  if (sys.temperature != null) add("Temperature", `${sys.temperature.toFixed(0)} °C`, sys.temperature >= 80);
  add("CPU", `${Math.round(sys.cpu_percent)} %`);
  add("Memory", `${Math.round(sys.memory_percent)} % (app ${sys.process_mb} MB)`);
  add("Free space", `${s.disk.free_gb} GB`, s.disk.low_space);
  add("Media stored", `${s.disk.media_gb} GB`);
  if (s.detector.ready) add("Detection time", `${s.detector.last_ms} ms`);
  if (sys.throttled?.now?.length) add("Power", sys.throttled.now.join(", "), true);
  else if (sys.throttled?.since_boot?.length) add("Power", `${sys.throttled.since_boot[0]} since boot`, true);
  add("Running for", fmtDuration(sys.service_uptime));
  add("Version", s.version);
  $("#health").replaceChildren(...rows);
}

function renderSummary(sum) {
  $("#t-visits").textContent = sum.today.visits;
  $("#t-peak").textContent = sum.today.peak;
  $("#t-time").textContent = sum.today.seconds < 60 ? `${Math.round(sum.today.seconds)} s`
    : `${Math.round(sum.today.seconds / 60)} m`;
  $("#t-last").textContent = sum.last_visit_at ? `Last visit ${fmtAgo(sum.last_visit_at)}` : "No visits yet";
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    renderNotices(s.warnings);
    const root = $("#cams");
    const ids = s.cameras.map((c) => c.id).join(",");
    if (root.dataset.ids !== ids) {
      for (const c of cams.values()) if (c.url) URL.revokeObjectURL(c.url);
      cams.clear();
      root.replaceChildren(...s.cameras.map(camBlock));
      if (!s.cameras.length) root.append(h("div", { class: "panel empty" },
        h("p", {}, "No camera is enabled."), h("a", { class: "btn", href: "/settings#cameras" }, "Open camera settings")));
      root.dataset.ids = ids;
    }
    s.cameras.forEach(renderCam);
    paused = s.cameras.some((c) => c.paused);
    renderHealth(s);
    renderSummary(s.summary);
  } catch (e) { if (e.status !== 401) console.warn(e); }
}

async function loadToday() {
  try {
    const now = new Date();
    const { visits } = await api(`/api/visits?day=${localDay(now)}&limit=100`);
    const start = new Date(now); start.setHours(0, 0, 0, 0);
    const pct = (ts) => `${((ts * 1000 - start) / 864e5) * 100}%`;
    const ribbon = $("#ribbon");
    ribbon.replaceChildren(
      ...visits.map((v) => h("div", { class: "tick", title: fmtTime(v.started_at), style: { left: pct(v.started_at) } })),
      h("div", { class: "now", style: { left: pct(now / 1000) } }));
    ribbon.setAttribute("aria-label", `${plural(visits.length, "visit")} today`);
    const recent = visits.length ? visits.slice(0, 6) : (await api("/api/visits?limit=6")).visits;
    $("#recent").replaceChildren(...(recent.length ? recent.map(visitTile)
      : [h("p", { class: "muted" }, "Visits appear here as soon as a bird is detected.")]));
  } catch (e) { if (e.status !== 401) console.warn(e); }
}

export function visitTile(v) {
  return h("a", { class: "visit", href: `/gallery#visit-${v.id}` },
    h("img", { src: `/media/photos/${v.cover_photo_id}.jpg?thumb=1`, alt: `Visit at ${fmtTime(v.started_at)}`, loading: "lazy" }),
    h("span", { class: "meta" }, h("span", { class: "grow" }, fmtTime(v.started_at)),
      v.max_count > 1 ? `×${v.max_count}` : null, v.favorite ? h("span", { class: "fav", "aria-label": "favourite" }, "★") : null));
}

on("visit_started", () => { loadStatus(); });
on("visit_ended", () => { loadStatus(); loadToday(); });
on("photo", () => loadToday());
on("recording", () => loadStatus());
on("camera", () => loadStatus());
on("warning", () => loadStatus());

await loadStatus();
loadToday();
setInterval(loadStatus, 10_000);
setInterval(() => { for (const id of cams.keys()) refreshFrame(id); }, 1000);
setInterval(loadToday, 60_000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { loadStatus(); loadToday(); } });
