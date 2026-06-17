"""A vertical, chronological schedule view served at ``/calendar``.

Self-contained HTML page (no build step, no framework). It fetches the static
``/data/events.json`` export and renders a single wide column you scroll down
through time: sticky day headers, a left time rail, one row per event, live
text filtering, and day-jump pills. Each event links into my.vibe.camp.
"""

from __future__ import annotations

from . import __version__, config


def calendar_html(edition: str | None = None) -> str:
    edition = edition or config.CURRENT_EDITION_NAME
    return (
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FiveCamp — Schedule</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #0e1014; color: #e7e9ee;
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header.top {
    position: sticky; top: 0; z-index: 30; background: rgba(14,16,20,.92);
    backdrop-filter: blur(8px); border-bottom: 1px solid #20242e; padding: .8rem 1rem;
  }
  .wrap { max-width: 860px; margin-inline: auto; }
  h1 { font-size: 1.25rem; margin: 0; }
  h1 small { color: #8b93a3; font-weight: 400; font-size: .8rem; margin-left: .5rem; }
  .controls { display: flex; gap: .5rem; align-items: center; margin-top: .6rem; flex-wrap: wrap; }
  #q {
    flex: 1 1 220px; min-width: 0; background: #181b22; border: 1px solid #2a3140;
    color: #e7e9ee; border-radius: 8px; padding: .5rem .7rem; font-size: .95rem;
  }
  .pills { display: flex; gap: .35rem; flex-wrap: wrap; }
  .pill {
    background: #1b212b; border: 1px solid #2a3140; color: #cfd6e4; cursor: pointer;
    border-radius: 999px; padding: .3rem .7rem; font-size: .8rem; white-space: nowrap;
  }
  .pill:hover { background: #232b38; }
  main { max-width: 860px; margin: 0 auto; padding: 0 1rem 5rem; }
  .day { position: sticky; top: 104px; z-index: 20; background: #0e1014;
    padding: 1.1rem 0 .5rem; border-bottom: 1px solid #20242e; margin-bottom: .25rem; }
  .day h2 { margin: 0; font-size: 1.05rem; color: #ffd35c; }
  .day .count { color: #8b93a3; font-size: .8rem; font-weight: 400; }
  .ev {
    display: grid; grid-template-columns: 76px 1fr; gap: .9rem;
    padding: .7rem 0; border-bottom: 1px solid #181b22;
  }
  .ev .time { color: #9fb6d6; font-variant-numeric: tabular-nums; font-size: .9rem; padding-top: .1rem; }
  .ev .time .end { display: block; color: #5d6677; font-size: .78rem; }
  .ev .body { min-width: 0; border-left: 3px solid var(--accent,#2a3140); padding-left: .8rem; }
  .ev .name { font-weight: 600; }
  .ev .name a { color: #e7e9ee; text-decoration: none; }
  .ev .name a:hover { color: #7fd1ff; text-decoration: underline; }
  .ev .meta { color: #8b93a3; font-size: .85rem; margin-top: .15rem; }
  .ev .stars { color: #ffd35c; }
  .t-TEAM_OFFICIAL { --accent: #ff8a5c; }
  .t-CAMPSITE_OFFICIAL { --accent: #5cc8ff; }
  .t-UNOFFICIAL { --accent: #3a4456; }
  .empty, .loading { color: #8b93a3; padding: 3rem 0; text-align: center; }
  footer { max-width: 860px; margin: 0 auto; padding: 2rem 1rem; color: #6b7280; font-size: .82rem; }
  a.home { color: #7fd1ff; text-decoration: none; }
</style>
</head>
<body>
  <header class="top">
    <div class="wrap">
      <h1>🎪 FiveCamp <small>__EDITION__ · vertical schedule</small></h1>
      <div class="controls">
        <input id="q" type="search" placeholder="Filter by name, venue, or host…" autocomplete="off">
        <div class="pills" id="dayPills"></div>
      </div>
    </div>
  </header>
  <main id="main"><div class="loading">Loading the schedule…</div></main>
  <footer>
    <a class="home" href="/">← connect your agent</a> ·
    times are local wall-clock · ★ = stars · data refreshes ~every 5 min ·
    click an event to open it in my.vibe.camp
  </footer>

<script>
const DOW = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
let ALL = [];

function hhmm(dt) {
  if (!dt || dt.indexOf("T") < 0) return "";
  return dt.split("T")[1].slice(0,5);
}
function dayLabel(date) {
  // date = "YYYY-MM-DD" — parse as plain calendar parts (no TZ shifting).
  const [y,m,d] = date.split("-").map(Number);
  const dow = new Date(Date.UTC(y, m-1, d)).getUTCDay();
  return `${DOW[dow]}, ${MONTHS[m-1]} ${d}`;
}
function esc(s){ return (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function render(filter) {
  const main = document.getElementById("main");
  const f = (filter||"").trim().toLowerCase();
  let evs = ALL;
  if (f) {
    evs = ALL.filter(e =>
      (e.name||"").toLowerCase().includes(f) ||
      (e.location||"").toLowerCase().includes(f) ||
      (e.creator_name||"").toLowerCase().includes(f));
  }
  if (!evs.length) { main.innerHTML = '<div class="empty">No events match.</div>'; return; }
  const byDay = {};
  for (const e of evs) (byDay[e.start_date] = byDay[e.start_date] || []).push(e);
  const days = Object.keys(byDay).sort();
  let html = "";
  for (const day of days) {
    const list = byDay[day].slice().sort((a,b) => (a.start_datetime||"").localeCompare(b.start_datetime||""));
    html += `<section><div class="day" id="day-${day}"><h2>${dayLabel(day)} <span class="count">· ${list.length} events</span></h2></div>`;
    for (const e of list) {
      const start = hhmm(e.start_datetime);
      const end = e.end_datetime ? hhmm(e.end_datetime) : "";
      const url = e.url || "#";
      const venue = esc(e.location || "TBA");
      const host = e.creator_name ? ` · ${esc(e.creator_name)}` : "";
      const stars = (e.stars||0) > 0 ? ` · <span class="stars">${e.stars}★</span>` : "";
      const cls = "t-" + (e.event_type || "UNOFFICIAL");
      html += `<div class="ev ${cls}">
        <div class="time">${start}${end ? `<span class="end">–${end}</span>` : ""}</div>
        <div class="body">
          <div class="name"><a href="${esc(url)}" target="_blank" rel="noopener">${esc(e.name)}</a></div>
          <div class="meta">${venue}${host}${stars}</div>
        </div></div>`;
    }
    html += `</section>`;
  }
  main.innerHTML = html;
  // day-jump pills
  const pills = document.getElementById("dayPills");
  pills.innerHTML = days.map(d => `<span class="pill" data-day="${d}">${dayLabel(d).split(",")[0].slice(0,3)} ${d.slice(8)}</span>`).join("");
  pills.querySelectorAll(".pill").forEach(p => p.onclick = () => {
    const el = document.getElementById("day-" + p.dataset.day);
    if (el) el.scrollIntoView({behavior:"smooth", block:"start"});
  });
}

async function load() {
  try {
    const r = await fetch("/data/events.json", {cache: "no-cache"});
    const d = await r.json();
    ALL = (d.events || []).filter(e => e.start_date);
    render("");
  } catch (err) {
    document.getElementById("main").innerHTML =
      '<div class="empty">Could not load the schedule. Try the API at <a href="/events">/events</a>.</div>';
  }
}
document.getElementById("q").addEventListener("input", e => render(e.target.value));
load();
</script>
</body>
</html>"""
    ).replace("__EDITION__", edition)
