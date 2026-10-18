// Shared helpers for all pages. Plain ES modules, no build step, no CDN.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** Create an element: h("div", {class: "x", onclick: fn}, child, "text") */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) el.append(c);
  return el;
}

export class ApiError extends Error {
  constructor(status, message, data) { super(message); this.status = status; this.data = data; }
}

export async function api(path, { method = "GET", body, signal } = {}) {
  const res = await fetch(path, {
    method, signal, credentials: "same-origin",
    headers: body !== undefined ? { "content-type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    throw new ApiError(401, "Signed out");
  }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    const detail = typeof data?.detail === "string" ? data.detail : `Request failed (${res.status})`;
    throw new ApiError(res.status, detail, data);
  }
  return data;
}

export function toast(message, kind = "info") {
  const el = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3000);
}

// ---- formatting ----
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" });
const shortDayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric" });

export const fmtTime = (ts) => timeFmt.format(new Date(ts * 1000));
export const fmtShortDay = (d) => shortDayFmt.format(d);

export function fmtDay(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return dayFmt.format(d);
}

export function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return s ? `${m} min ${s} s` : `${m} min`;
  const hh = Math.floor(m / 60);
  return `${hh} h ${m % 60} min`;
}

export function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return fmtDay(ts);
}

export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export const localDay = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

// ---- live updates (Server-Sent Events) ----
const listeners = new Map();
let source;
export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  if (source) source.addEventListener(event, (e) => fn(JSON.parse(e.data)));
}

function connect() {
  const dot = $("#conn");
  source = new EventSource("/api/events");
  const label = (t) => { if (dot) { dot.title = t; dot.setAttribute("aria-label", t); } };
  source.onopen = () => { dot?.classList.add("on"); label("Live updates: connected"); };
  source.onerror = () => { dot?.classList.remove("on"); label("Live updates: reconnecting"); };
  for (const [event, fns] of listeners)
    for (const fn of fns) source.addEventListener(event, (e) => fn(JSON.parse(e.data)));
}

// ---- system notices ----
export function renderNotices(warnings) {
  const box = $("#notices");
  if (!box) return;
  box.replaceChildren(...warnings.map((w) => h("div", { class: "notice", role: "status" }, w.message)));
}

export function boxesFor(detections, container) {
  container.replaceChildren(...detections.map((d) =>
    h("div", { class: "box", style: { left: `${d.x * 100}%`, top: `${d.y * 100}%`, width: `${d.w * 100}%`,
      height: `${d.h * 100}%` } }, h("span", {}, `${d.label} ${Math.round(d.confidence * 100)}%`))));
}

if ($("#conn")) {
  // Defer so page modules can register listeners first.
  queueMicrotask(connect);
}
