"""Build subscribable .ics feeds and a landing page from data/events.json."""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "events.json"
OUT = ROOT / "public"
SITE = "https://nationaltoday.com"
UID_DOMAIN = "shonar-holiday-calendar"
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")

KIND_LABEL = {"month": "Month", "range": "Week", "day": "Day"}


# --- iCalendar helpers -------------------------------------------------------

def esc(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold to 75 octets per RFC 5545 without splitting UTF-8 sequences."""
    out, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        limit = 75 if not out else 74  # continuation lines start with a space
        if size + n > limit:
            out.append(current)
            current, size = ch, n
        else:
            current += ch
            size += n
    out.append(current)
    return "\r\n ".join(out)


def ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "other"


class Calendar:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.events: list[list[str]] = []

    def add(self, uid: str, start: dt.date, end_inclusive: dt.date, summary: str,
            description: str = "", url: str = "", categories: list[str] | None = None) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}@{UID_DOMAIN}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{ymd(start)}",
            f"DTEND;VALUE=DATE:{ymd(end_inclusive + dt.timedelta(days=1))}",
            f"SUMMARY:{esc(summary)}",
            "TRANSP:TRANSPARENT",
        ]
        if description:
            lines.append(f"DESCRIPTION:{esc(description)}")
        if url:
            lines.append(f"URL:{url}")
        if categories:
            lines.append("CATEGORIES:" + ",".join(esc(c) for c in categories if c))
        lines.append("END:VEVENT")
        self.events.append(lines)

    def render(self) -> str:
        head = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//furioursus//shonar-holiday-calendar//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            f"X-WR-CALNAME:{esc(self.name)}",
            f"X-WR-CALDESC:{esc(self.description)}",
            "X-PUBLISHED-TTL:PT12H",
            "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        ]
        body = [line for ev in self.events for line in ev]
        return "\r\n".join(fold(l) for l in head + body + ["END:VCALENDAR"]) + "\r\n"


# --- content helpers ---------------------------------------------------------

def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def link(e: dict) -> str:
    return f"{SITE}/{e['slug']}/" if e.get("slug") else SITE


def meta_line(e: dict) -> str:
    bits = [e.get("category", "")] + e.get("tags", [])
    return " · ".join(b for b in bits if b)


def event_description(e: dict) -> str:
    parts = []
    if e.get("summary"):
        parts.append(e["summary"])
    if meta_line(e):
        parts.append(meta_line(e))
    parts.append(link(e))
    return "\n\n".join(parts)


def range_label(e: dict) -> str:
    start, end = d(e["start"]), d(e["end"])
    if e["kind"] == "month":
        return start.strftime("%B")
    if start == end:
        return start.strftime("%b %-d")
    if start.month == end.month:
        return f"{start.strftime('%b %-d')}–{end.day}"
    return f"{start.strftime('%b %-d')}–{end.strftime('%b %-d')}"


def digest_summary(names: list[str], limit: int = 90) -> str:
    text = ""
    for i, name in enumerate(names):
        candidate = name if not text else f"{text}, {name}"
        remaining = len(names) - i - 1
        suffix = f" +{remaining}" if remaining else ""
        if len(candidate + suffix) > limit and text:
            return f"{text} +{len(names) - i}"
        text = candidate
    return text


def short_name(title: str) -> str:
    return re.sub(r"^(National|International|World)\s+", "", title)


# --- feeds -------------------------------------------------------------------

def build(events: list[dict]) -> list[dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "categories").mkdir(exist_ok=True)

    days = [e for e in events if e["kind"] == "day"]
    spans = [e for e in events if e["kind"] in ("range", "month")]
    by_day: dict[dt.date, list[dict]] = defaultdict(list)
    for e in days:
        by_day[d(e["start"])].append(e)

    feeds: list[dict] = []

    def save(cal: Calendar, path: str, blurb: str, group: str = "main") -> None:
        (OUT / path).write_text(cal.render(), encoding="utf-8")
        feeds.append({"path": path, "name": cal.name, "blurb": blurb,
                      "count": len(cal.events), "group": group})

    # 1. Daily digest: one tidy event per day.
    daily = Calendar("National Today · Daily digest",
                     "One event per day listing every holiday from nationaltoday.com.")
    for day, items in sorted(by_day.items()):
        items = sorted(items, key=lambda e: e["title"].lower())
        lines = [f"• {e['title']} — {meta_line(e)}\n  {link(e)}" for e in items]
        daily.add(f"daily-{ymd(day)}", day, day,
                  f"🗓 {digest_summary([short_name(e['title']) for e in items])}",
                  f"{len(items)} holidays on {day.strftime('%A, %B %-d')}:\n\n" + "\n".join(lines),
                  f"{SITE}/{day.strftime('%B').lower()}-{day.day}-holidays/")
    save(daily, "daily.ics", "One all-day event per day with every holiday in the notes. The cleanest view.")

    # 2. Weekly planner: a Monday event covering Mon–Sun plus active observances.
    weekly = Calendar("National Today · Weekly planner",
                      "A Monday event summarizing the week's holidays and active weeks/months, for media planning.")
    if by_day:
        first, last = min(by_day), max(by_day)
        monday = first - dt.timedelta(days=first.weekday())
        while monday <= last:
            sunday = monday + dt.timedelta(days=6)
            active = [s for s in spans if d(s["start"]) <= sunday and d(s["end"]) >= monday]
            starting = [s for s in active if d(s["start"]) >= monday]
            ongoing = [s for s in active if d(s["start"]) < monday]
            body = [f"Week of {monday.strftime('%B %-d')} – {sunday.strftime('%B %-d, %Y')}"]
            weeks = [s for s in starting if s["kind"] == "range"]
            if weeks:
                body.append("\nWEEKS STARTING THIS WEEK")
                body += [f"• {s['title']} ({range_label(s)}) — {link(s)}" for s in weeks]
            months = [s for s in starting if s["kind"] == "month"]
            if months:
                body.append("\nMONTHS STARTING")
                body += [f"• {s['title']} — {link(s)}" for s in months]
            for i in range(7):
                day = monday + dt.timedelta(days=i)
                items = sorted(by_day.get(day, []), key=lambda e: e["title"].lower())
                if items:
                    body.append(f"\n{day.strftime('%a %b %-d').upper()}")
                    body += [f"• {e['title']} [{e.get('category', '')}]" for e in items]
            if ongoing:
                body.append("\nSTILL RUNNING")
                body.append(", ".join(f"{s['title']} (thru {d(s['end']).strftime('%b %-d')})" for s in ongoing))
            n_days = sum(len(by_day.get(monday + dt.timedelta(days=i), [])) for i in range(7))
            headline = [short_name(s["title"]) for s in weeks][:3]
            summary = f"📋 Week of {monday.strftime('%b %-d')}: {n_days} holidays"
            if weeks:
                summary += f", {len(weeks)} weeks"
            if headline:
                summary += " — " + ", ".join(headline)
            weekly.add(f"week-{ymd(monday)}", monday, monday, summary, "\n".join(body))
            monday += dt.timedelta(days=7)
    save(weekly, "weekly.ics", "A Monday planning event: weeks starting, every day's holidays, and what's still running.")

    # 3. Observances: weeks and months as multi-day events.
    obs = Calendar("National Today · Weeks & months",
                   "Week-long and month-long observances as multi-day events.")
    for s in spans:
        tag = "📆" if s["kind"] == "month" else "🗓"
        obs.add(f"{s['slug']}-{s['start']}", d(s["start"]), d(s["end"]),
                f"{tag} {s['title']}", event_description(s), link(s),
                [s.get("category", "")] + s.get("tags", []))
    save(obs, "observances.ics", "Week- and month-long observances as spanning events, so you can plan around a whole week.")

    # 4. Everything, one event per holiday.
    full = Calendar("National Today · Everything",
                    "Every holiday from nationaltoday.com as its own event. Busy!")
    for e in events:
        full.add(f"{e['slug']}-{e['start']}", d(e["start"]), d(e["end"]), e["title"],
                 event_description(e), link(e), [e.get("category", "")] + e.get("tags", []))
    save(full, "all.ics", "Every holiday as its own event (~20+ a day). Best for searching, noisy for browsing.")

    # 5. Per-category feeds.
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_cat[e.get("category") or "Other"].append(e)
    for cat, items in sorted(by_cat.items()):
        cal = Calendar(f"National Today · {cat}", f"{cat} holidays from nationaltoday.com.")
        for e in items:
            cal.add(f"{e['slug']}-{e['start']}", d(e["start"]), d(e["end"]), e["title"],
                    event_description(e), link(e), [cat] + e.get("tags", []))
        save(cal, f"categories/{slugify(cat)}.ics", f"Only {cat} holidays.", "category")

    return feeds


# --- landing page ------------------------------------------------------------

def landing(feeds: list[dict], scraped_at: str, total: int) -> str:
    def row(f: dict) -> str:
        https_url = f"{PAGES_URL}/{f['path']}" if PAGES_URL else f["path"]
        webcal = re.sub(r"^https?://", "webcal://", https_url) if PAGES_URL else f["path"]
        return f"""
      <li class="feed">
        <div class="feed-text">
          <h3>{html.escape(f['name'].split(' · ', 1)[-1])}</h3>
          <p>{html.escape(f['blurb'])} <span class="count">{f['count']:,} events</span></p>
        </div>
        <div class="feed-actions">
          <a class="btn primary" href="{html.escape(webcal)}">Subscribe</a>
          <button class="btn" type="button" data-copy="{html.escape(https_url)}">Copy URL</button>
        </div>
      </li>"""

    main = "".join(row(f) for f in feeds if f["group"] == "main")
    cats = "".join(row(f) for f in feeds if f["group"] == "category")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shonar Holiday Calendar</title>
<style>
  :root {{ --bg:#faf8f5; --fg:#1d1b19; --muted:#6b645c; --card:#fff; --line:#e7e1d8; --accent:#7c3aed; --accent-fg:#fff; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#141217; --fg:#eeeaf2; --muted:#9d96a6; --card:#1d1a22; --line:#2e2a35; --accent:#a78bfa; --accent-fg:#141217; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width:760px; margin:0 auto; padding:48px 16px 64px; }}
  h1 {{ font-size:2rem; margin:0 0 .25rem; letter-spacing:-.02em; }}
  h2 {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:2.5rem 0 .75rem; }}
  h3 {{ margin:0; font-size:1.05rem; }}
  p {{ margin:.25rem 0 0; color:var(--muted); }}
  .lede {{ font-size:1.05rem; }}
  ul {{ list-style:none; padding:0; margin:0; display:grid; gap:10px; }}
  .feed {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; display:flex; gap:16px; align-items:center; justify-content:space-between; flex-wrap:wrap; }}
  .feed-text {{ flex:1 1 320px; }}
  .feed-actions {{ display:flex; gap:8px; }}
  .count {{ white-space:nowrap; font-variant-numeric:tabular-nums; opacity:.8; }}
  .btn {{ font:inherit; font-size:.9rem; padding:7px 12px; border-radius:8px; border:1px solid var(--line); background:transparent; color:var(--fg); text-decoration:none; cursor:pointer; }}
  .btn.primary {{ background:var(--accent); border-color:var(--accent); color:var(--accent-fg); }}
  details {{ margin-top:2.5rem; }}
  summary {{ cursor:pointer; color:var(--muted); font-size:.8rem; text-transform:uppercase; letter-spacing:.08em; }}
  details ul {{ margin-top:.75rem; }}
  footer {{ margin-top:3rem; font-size:.85rem; color:var(--muted); }}
  a {{ color:var(--accent); }}
</style>
</head>
<body>
<main>
  <h1>Shonar Holiday Calendar</h1>
  <p class="lede">Every holiday and observance from <a href="{SITE}">National Today</a>, as calendars you can subscribe to for planning stream promo assets. Refreshed weekly.</p>
  <h2>Feeds</h2>
  <ul>{main}
  </ul>
  <details>
    <summary>By category</summary>
    <ul>{cats}
    </ul>
  </details>
  <footer>{total:,} events · last scraped {html.escape(scraped_at)} · data from nationaltoday.com</footer>
</main>
<script>
  document.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', async () => {{
    try {{ await navigator.clipboard.writeText(b.dataset.copy); b.textContent = 'Copied'; }}
    catch (e) {{ b.textContent = 'Copy failed'; }}
    setTimeout(() => b.textContent = 'Copy URL', 1500);
  }}));
</script>
</body>
</html>
"""


def main() -> None:
    data = json.loads(DATA_FILE.read_text())
    events = data["events"]
    feeds = build(events)
    (OUT / "index.html").write_text(landing(feeds, data.get("scraped_at", ""), len(events)), encoding="utf-8")
    (OUT / ".nojekyll").write_text("")
    for f in feeds:
        print(f"{f['path']}: {f['count']} events")


if __name__ == "__main__":
    main()
