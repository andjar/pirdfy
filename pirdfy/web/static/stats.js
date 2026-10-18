import { $, h, api, toast, fmtShortDay, fmtDay, localDay, plural } from "./app.js";

function days(n) {
  const out = [];
  const d = new Date(); d.setHours(12, 0, 0, 0);
  for (let i = n - 1; i >= 0; i--) { const x = new Date(d); x.setDate(d.getDate() - i); out.push(x); }
  return out;
}

function render(data) {
  const n = data.days, list = days(n);
  const byDay = new Map(data.daily.map((d) => [d.day, d]));
  const heat = new Map(data.heatmap.map((c) => [`${c.day} ${c.hour}`, c.visits]));
  const total = data.daily.reduce((a, d) => a + d.visits, 0);
  const peak = data.daily.reduce((a, d) => Math.max(a, d.peak || 0), 0);
  const hours = Array(24).fill(0);
  for (const c of data.heatmap) hours[c.hour] += c.visits;
  const busiest = hours.indexOf(Math.max(...hours));

  $("#s-visits").textContent = total;
  $("#s-peak").textContent = peak;
  $("#s-busiest").textContent = total ? `${String(busiest).padStart(2, "0")}–${String(busiest + 1).padStart(2, "0")}` : "–";
  $("#s-total").textContent = data.summary.total_visits;
  const first = data.summary.first_visit_at;
  $("#s-total-l").textContent = first && fmtDay(first) !== "Today"
    ? `visits since ${new Date(first * 1000).toLocaleDateString()}` : "visits in total";

  // Heatmap: rows = days (newest on top), columns = hours.
  const max = Math.max(1, ...heat.values());
  const cells = [h("span"), ...Array.from({ length: 24 }, (_, i) => h("span", { class: "hr" }, i % 3 ? "" : String(i).padStart(2, "0")))];
  for (const d of [...list].reverse()) {
    const key = localDay(d);
    cells.push(h("span", { class: "lbl" }, fmtShortDay(d)));
    for (let hr = 0; hr < 24; hr++) {
      const v = heat.get(`${key} ${hr}`) || 0;
      const a = v ? 0.2 + 0.8 * (v / max) : 0;
      cells.push(h("span", { class: "cell", title: `${fmtShortDay(d)} ${hr}:00 – ${plural(v, "visit")}`,
        style: v ? { background: `color-mix(in srgb, var(--accent) ${Math.round(a * 100)}%, var(--surface-2))` } : {} }));
    }
  }
  $("#heat").replaceChildren(...cells);

  // Daily bars
  const maxDay = Math.max(1, ...data.daily.map((d) => d.visits));
  $("#bars").replaceChildren(...list.map((d) => {
    const v = byDay.get(localDay(d))?.visits || 0;
    return h("div", { class: "bar", title: `${fmtShortDay(d)}: ${plural(v, "visit")}`, style: { height: `${(v / maxDay) * 100}%` } });
  }));
  $("#bars").setAttribute("aria-label", `Visits per day, ${total} in total over ${n} days`);
  const every = n > 30 ? 7 : n > 14 ? 3 : 1;
  $("#bars-x").replaceChildren(...list.map((d, i) => h("span", {}, i % every ? "" : String(d.getDate()))));
  $("#bars").style.minWidth = $("#bars-x").style.minWidth = `${n * 10}px`;
}

async function load() {
  try { render(await api(`/api/stats?days=${$("#range").value}`)); }
  catch (e) { toast(e.message, "error"); }
}
$("#range").addEventListener("change", load);
load();
