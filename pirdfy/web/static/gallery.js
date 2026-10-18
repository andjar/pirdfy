import { $, $$, h, api, on, toast, fmtDay, fmtTime, fmtDuration, plural, boxesFor } from "./app.js";

const state = { favorites: false, day: "", camera: "", next: null, loading: false, lastDay: null, cameras: [] };
let current = null; // {visit, photos, videos, index}

function tile(v) {
  return h("button", { type: "button", class: "visit", onclick: () => openVisit(v.id),
      "aria-label": `Visit at ${fmtTime(v.started_at)}, ${plural(v.max_count, "bird")}` },
    h("img", { src: `/media/photos/${v.cover_photo_id}.jpg?thumb=1`, alt: "", loading: "lazy",
      width: v.cover_width, height: v.cover_height }),
    h("span", { class: "meta" },
      h("span", { class: "grow" }, fmtTime(v.started_at)),
      v.labels.includes("snapshot") ? "photo" : (v.max_count > 1 ? `×${v.max_count}` : null),
      v.video_id ? h("span", { title: "Has video", "aria-label": "has video" }, "▶") : null,
      v.favorite ? h("span", { class: "fav", "aria-label": "favourite" }, "★") : null));
}

async function load(reset = false) {
  if (state.loading) return;
  state.loading = true;
  const list = $("#list");
  if (reset) { state.next = null; state.lastDay = null; list.replaceChildren(); }
  const q = new URLSearchParams({ limit: 48 });
  if (state.next) q.set("before", state.next);
  if (state.favorites) q.set("favorites", "true");
  if (state.day) q.set("day", state.day);
  if (state.camera !== "") q.set("camera", state.camera);
  try {
    const { visits, next } = await api(`/api/visits?${q}`);
    let grid = list.lastElementChild?.classList.contains("visits") ? list.lastElementChild : null;
    for (const v of visits) {
      const day = fmtDay(v.started_at);
      if (day !== state.lastDay) {
        list.append(h("div", { class: "day-head" }, h("h2", {}, day)));
        grid = h("div", { class: "visits" });
        list.append(grid);
        state.lastDay = day;
      }
      grid.append(tile(v));
    }
    state.next = next;
    $("#more").hidden = !next;
    if (reset && !visits.length) {
      list.append(h("div", { class: "empty" },
        h("p", {}, state.favorites ? "No favourites yet. Open a visit and tap the star to keep it forever."
          : state.day ? "No visits on this day." : "No visits yet. When a bird is detected, its photos and video appear here."),
        state.favorites || state.day ? h("button", { type: "button", onclick: clearFilters }, "Show all visits") : null));
    }
  } catch (e) { toast(e.message, "error"); }
  finally { state.loading = false; }
}

function clearFilters() {
  state.favorites = false; state.day = ""; $("#day").value = "";
  $$("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filter === "all")));
  load(true);
}

// ---- viewer ----
function frame(w, h_, child, dets) {
  const box = h("div", { class: "boxes" });
  if (dets) boxesFor(dets, box);
  const ratio = w / h_;
  return h("div", { class: "photo-frame", style: { aspectRatio: `${w} / ${h_}`, width: `min(100%, calc(70vh * ${ratio}))` } }, child, box);
}

function show(index) {
  const { photos, videos } = current;
  current.index = index;
  const stage = $("#stage");
  if (index < photos.length) {
    const p = photos[index];
    stage.replaceChildren(frame(p.width, p.height, h("img", { src: `/media/photos/${p.id}.jpg`,
      alt: `Photo ${index + 1} of ${photos.length}` }), p.detections));
    $("#v-download").href = `/media/photos/${p.id}.jpg?download=1`;
    $("#v-download").hidden = false;
  } else {
    const v = videos[index - photos.length];
    const first = photos[0];
    stage.replaceChildren(frame(first?.width || 16, first?.height || 9, h("video", { src: `/media/videos/${v.id}.mp4`,
      controls: true, autoplay: true, playsinline: true, preload: "metadata" })));
    $("#v-download").href = `/media/videos/${v.id}.mp4`;
  }
  $$("#strip button").forEach((b, i) => b.setAttribute("aria-current", String(i === index)));
}

async function openVisit(id) {
  try {
    current = { ...(await api(`/api/visits/${id}`)), index: 0 };
  } catch (e) { toast(e.message, "error"); return; }
  const { visit, photos, videos } = current;
  $("#viewer-title").textContent = `${fmtDay(visit.started_at)}, ${fmtTime(visit.started_at)}`;
  $("#strip").replaceChildren(
    ...photos.map((p, i) => h("button", { type: "button", onclick: () => show(i), "aria-label": `Photo ${i + 1}` },
      h("img", { src: `/media/photos/${p.id}.jpg?thumb=1`, alt: "", loading: "lazy" }))),
    ...videos.map((v, i) => h("button", { type: "button", onclick: () => show(photos.length + i),
      "aria-label": `Video ${i + 1}` }, h("span", { class: "vid" }, `▶ ${fmtDuration(v.duration)}`))));
  const labels = visit.labels.filter((l) => l !== "snapshot");
  $("#v-foot").replaceChildren(
    h("span", {}, labels.length ? `${plural(visit.max_count, labels.join(" / "))} at most` : "Manual photo"),
    h("span", {}, `Stayed ${fmtDuration(visit.ended_at - visit.started_at)}`),
    visit.best_confidence ? h("span", {}, `Best match ${Math.round(visit.best_confidence * 100)} %`) : null,
    h("span", {}, plural(photos.length, "photo") + (videos.length ? `, ${plural(videos.length, "video")}` : "")));
  setFav(!!visit.favorite);
  show(0);
  const dlg = $("#viewer");
  if (!dlg.open) dlg.showModal();
  history.replaceState(null, "", `#visit-${id}`);
}

function setFav(f) {
  current.visit.favorite = f;
  const b = $("#v-fav");
  b.setAttribute("aria-pressed", String(f));
  b.title = f ? "Favourite: kept forever. Tap to remove." : "Keep forever (favourite)";
  b.querySelector("path").setAttribute("fill", f ? "currentColor" : "none");
  b.classList.toggle("fav", f);
}

$("#v-fav").addEventListener("click", async () => {
  const want = !current.visit.favorite;
  try { await api(`/api/visits/${current.visit.id}/favorite`, { method: "PUT", body: { favorite: want } });
    setFav(want); toast(want ? "Added to favourites" : "Removed from favourites"); }
  catch (e) { toast(e.message, "error"); }
});

$("#v-delete").addEventListener("click", async () => {
  if (!confirm("Delete this visit with all its photos and video?")) return;
  try {
    await api(`/api/visits/${current.visit.id}`, { method: "DELETE" });
    $("#viewer").close(); toast("Visit deleted"); load(true);
  } catch (e) { toast(e.message, "error"); }
});

$("#v-close").addEventListener("click", () => $("#viewer").close());
$("#viewer").addEventListener("close", () => {
  $("#stage").replaceChildren();
  history.replaceState(null, "", location.pathname + location.search);
});
$("#viewer").addEventListener("keydown", (e) => {
  if (!current) return;
  const total = current.photos.length + current.videos.length;
  if (e.key === "ArrowRight") show(Math.min(total - 1, current.index + 1));
  if (e.key === "ArrowLeft") show(Math.max(0, current.index - 1));
});

// ---- filters ----
$$("[data-filter]").forEach((b) => b.addEventListener("click", () => {
  state.favorites = b.dataset.filter === "favorites";
  $$("[data-filter]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  load(true);
}));
$("#day").addEventListener("change", (e) => { state.day = e.target.value; load(true); });
$("#camera").addEventListener("change", (e) => { state.camera = e.target.value; load(true); });
$("#more").addEventListener("click", () => load());

on("visit_ended", () => { if (!state.next && !state.day) load(true); });

(async () => {
  try {
    const s = await api("/api/settings");
    if (s.cameras.length > 1) {
      const sel = $("#camera");
      sel.replaceChildren(h("option", { value: "" }, "All cameras"),
        ...s.cameras.map((c) => h("option", { value: c.id }, c.name)));
      sel.hidden = false;
    }
  } catch { /* filters are optional */ }
})();

await load(true);
const m = location.hash.match(/^#visit-(\d+)$/);
if (m) openVisit(Number(m[1]));
