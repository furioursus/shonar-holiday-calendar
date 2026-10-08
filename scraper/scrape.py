"""Scrape nationaltoday.com month pages into data/events.json.

Each month page (/<month>-holidays/) lists the next upcoming occurrence of every
holiday in that month, grouped under per-day headers. Week- and month-long
observances are listed under their start day with no end date, so those
entries are resolved by reading the date range from the holiday's own page
heading (e.g. "National Walk Your Dog Week — October 1–7, 2026").

Past events from earlier runs are kept, so the calendar keeps its history even
after the site rolls a month forward to next year.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://nationaltoday.com"
ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "events.json"

MONTHS = [m.lower() for m in calendar.month_name[1:]]
MONTH_INDEX = {name: i + 1 for i, name in enumerate(MONTHS)}
WEEKDAYS = [d.lower() for d in calendar.day_name]  # monday first

# Titles that suggest a multi-day observance. Anything listed on the 1st of a
# month is also checked, since month-long observances don't always say "Month"
# (e.g. "Unblocktober").
MULTIDAY_HINT = re.compile(
    r"\b(week|weeks|month|days|fortnight|season|festival|fest|weekend|year)\b|tober\b",
    re.I,
)

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 shonar-holiday-calendar"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
)

REQUEST_DELAY = 0.3
WORKERS = 6
# Unresolved observances carry over to the next run via the cache, so cap how
# long one run spends on them.
RESOLVE_BUDGET_SECONDS = 15 * 60
STATUS = Counter()


def annotate(level: str, message: str) -> None:
    """Surface a message as a GitHub Actions annotation (and plain log line)."""
    print(f"::{level}::{message}" if os.environ.get("GITHUB_ACTIONS") else message, flush=True)


def fetch(path: str) -> str | None:
    url = path if path.startswith("http") else BASE + path
    for attempt in range(3):
        try:
            resp = SESSION.get(url, timeout=30)
        except requests.RequestException as exc:
            STATUS[type(exc).__name__] += 1
            print(f"  ! {url}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
            continue
        time.sleep(REQUEST_DELAY)
        STATUS[resp.status_code] += 1
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 404:
            return None
        print(f"  ! {url}: HTTP {resp.status_code}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return None


def slug_from_href(href: str) -> str:
    href = re.sub(r"[?#].*", "", href)
    href = re.sub(r"^https?://[^/]+", "", href)
    return href.strip("/")


def infer_year(month: int, day: int, weekday: str | None, today: dt.date) -> int:
    """Month pages don't print the year; match the printed weekday instead."""
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(dt.date(year, month, day))
        except ValueError:
            continue
    if weekday:
        wd = WEEKDAYS.index(weekday.strip().lower())
        matching = [d for d in candidates if d.weekday() == wd]
        if matching:
            return min(matching, key=lambda d: abs((d - today).days)).year
    # Fallback: next occurrence on or after (roughly) a month ago.
    for d in candidates:
        if d >= today - dt.timedelta(days=31):
            return d.year
    return today.year


def parse_month_page(html: str, month: int, today: dt.date) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.archive-table")
    if table is None:
        raise RuntimeError(f"no archive-table on month page {month}")

    events: list[dict] = []
    current: dt.date | None = None
    for row in table.select("tr"):
        anchor = row.select_one(".event-date a[id]")
        if anchor is not None:
            m = re.match(r"([a-z]{3})-(\d{1,2})$", anchor["id"])
            if not m:
                current = None
                continue
            day = int(m.group(2))
            weekday_el = row.select_one(".event-day")
            weekday = weekday_el.get_text(strip=True) if weekday_el else None
            current = dt.date(infer_year(month, day, weekday, today), month, day)
            continue

        title_link = row.select_one("td.title a")
        if title_link is None or current is None:
            continue
        categories = [a.get_text(" ", strip=True) for a in row.select("td.category a")]
        if not categories and row.select_one("td.category"):
            categories = [row.select_one("td.category").get_text(" ", strip=True)]
        categories = [c for c in categories if c]
        tags = [a.get_text(strip=True) for a in row.select("td.tags a")]
        events.append(
            {
                "title": title_link.get_text(" ", strip=True),
                "slug": slug_from_href(title_link.get("href", "")),
                "start": current.isoformat(),
                "end": current.isoformat(),
                "category": categories[0] if categories else "",
                "categories": categories,
                "tags": tags,
                "kind": "day",
            }
        )
    return events


DASHES = "–—-"
DATE_PART = re.compile(
    r"^(?P<month>[A-Za-z]+)\s*(?P<day>\d{1,2})?(?:,\s*(?P<year>\d{4}))?$"
)


def parse_heading_range(heading: str, fallback_year: int) -> tuple[dt.date, dt.date, str] | None:
    """Parse the date part of a holiday page H1.

    Handles: "October 8, 2026", "October 1–7, 2026", "October 2026",
    "September 28 – October 4, 2026", "December 28, 2026 – January 3, 2027".
    Returns (start, end, kind) with kind in {"day", "range", "month"}.
    """
    if "—" not in heading:
        return None
    date_text = heading.split("—", 1)[1].strip()
    date_text = re.sub(r"\s+", " ", date_text)

    # Month only: "October 2026"
    m = re.fullmatch(r"([A-Za-z]+) (\d{4})", date_text)
    if m and m.group(1).lower() in MONTH_INDEX:
        month, year = MONTH_INDEX[m.group(1).lower()], int(m.group(2))
        last = calendar.monthrange(year, month)[1]
        return dt.date(year, month, 1), dt.date(year, month, last), "month"

    # Same-month range: "October 1–7, 2026"
    m = re.fullmatch(r"([A-Za-z]+) (\d{1,2}) ?[–—-] ?(\d{1,2}), (\d{4})", date_text)
    if m and m.group(1).lower() in MONTH_INDEX:
        month, year = MONTH_INDEX[m.group(1).lower()], int(m.group(4))
        return (
            dt.date(year, month, int(m.group(2))),
            dt.date(year, month, int(m.group(3))),
            "range",
        )

    # Cross-month range: "September 28 – October 4, 2026" (optionally with a
    # year on both sides).
    parts = re.split(r"\s*[–—]\s*|\s+-\s+", date_text)
    if len(parts) == 2:
        end_m = DATE_PART.match(parts[1].strip())
        start_m = DATE_PART.match(parts[0].strip())
        if (
            end_m
            and start_m
            and end_m.group("day")
            and start_m.group("day")
            and end_m.group("month").lower() in MONTH_INDEX
            and start_m.group("month").lower() in MONTH_INDEX
        ):
            end_year = int(end_m.group("year") or fallback_year)
            end = dt.date(end_year, MONTH_INDEX[end_m.group("month").lower()], int(end_m.group("day")))
            start_year = int(start_m.group("year") or end_year)
            start = dt.date(start_year, MONTH_INDEX[start_m.group("month").lower()], int(start_m.group("day")))
            if start > end:
                start = start.replace(year=start.year - 1)
            return start, end, "range"

    # Single day: "October 8, 2026"
    m = DATE_PART.match(date_text)
    if m and m.group("day") and m.group("month").lower() in MONTH_INDEX:
        year = int(m.group("year") or fallback_year)
        d = dt.date(year, MONTH_INDEX[m.group("month").lower()], int(m.group("day")))
        return d, d, "day"
    return None


def resolve_multiday(event: dict, cache: dict) -> None:
    key = f"{event['slug']}@{event['start']}"
    if key in cache:
        event.update(cache[key])
        return
    result = lookup_range(event)
    if result is not None:
        cache[key] = result
        event.update(result)


def lookup_range(event: dict) -> dict | None:
    """Returns None when the page couldn't be fetched, so it's retried next run."""
    html = fetch(f"/{event['slug']}/")
    if html is None:
        return None
    resolved: dict = {}
    if html:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.select_one("h1")
        desc = soup.select_one('meta[name="description"]')
        start_year = int(event["start"][:4])
        parsed = parse_heading_range(h1.get_text(" ", strip=True), start_year) if h1 else None
        if parsed:
            start, end, kind = parsed
            # Only trust the page if it describes the same occurrence.
            if abs((start - dt.date.fromisoformat(event["start"])).days) <= 7:
                resolved = {"start": start.isoformat(), "end": end.isoformat(), "kind": kind}
        if desc and desc.get("content"):
            resolved["summary"] = desc["content"].strip()
    return resolved


def load_existing() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"events": [], "range_cache": {}}


def main() -> int:
    today = dt.date.today()
    existing = load_existing()
    cache: dict = existing.get("range_cache", {})

    fresh: list[dict] = []
    for month_num, name in enumerate(MONTHS, start=1):
        print(f"month: {name}")
        html = fetch(f"/{name}-holidays/")
        if html is None:
            print(f"  ! could not fetch {name}", file=sys.stderr)
            continue
        month_events = parse_month_page(html, month_num, today)
        print(f"  {len(month_events)} entries", flush=True)
        fresh.extend(month_events)

    annotate("notice", f"month pages: {len(fresh)} entries; HTTP {dict(STATUS)}")
    if len(fresh) < 1000:
        # A full year is ~8k entries; anything this small means the layout
        # changed or we were blocked. Don't overwrite good data with it.
        annotate("error", f"only {len(fresh)} entries scraped; aborting")
        return 1

    candidates = [
        e for e in fresh if MULTIDAY_HINT.search(e["title"]) or e["start"].endswith("-01")
    ]
    todo = []
    for e in candidates:
        key = f"{e['slug']}@{e['start']}"
        if key in cache:
            e.update(cache[key])
        else:
            todo.append(e)
    print(f"{len(candidates)} possible multi-day observances, {len(todo)} uncached", flush=True)
    started = time.monotonic()
    done = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {}
        for e in todo:
            futures[pool.submit(lookup_range, e)] = e
        for fut in as_completed(futures):
            e = futures[fut]
            if time.monotonic() - started > RESOLVE_BUDGET_SECONDS:
                for f in futures:
                    f.cancel()
                annotate("warning", f"resolve budget hit after {done}/{len(todo)}; rest next run")
                break
            try:
                result = fut.result()
            except Exception as exc:  # keep going; it'll retry next run
                print(f"  ! {e['slug']}: {exc}", file=sys.stderr)
                continue
            done += 1
            if result is None:
                continue
            cache[f"{e['slug']}@{e['start']}"] = result
            e.update(result)
            if done % 100 == 0:
                print(f"  resolved {done}/{len(todo)}", flush=True)

    # Dedupe (the same holiday can appear on two month pages for ranges).
    seen: set[tuple[str, str]] = set()
    deduped = []
    for e in fresh:
        key = (e["slug"], e["start"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)

    # Keep history: events from previous runs that ended before the earliest
    # date the site is now showing.
    earliest_fresh = min(e["start"] for e in deduped)
    history = [
        e
        for e in existing.get("events", [])
        if e["end"] < earliest_fresh and (e["slug"], e["start"]) not in seen
    ]

    events = sorted(history + deduped, key=lambda e: (e["start"], e["title"].lower()))
    # Drop stale cache entries (older than ~14 months) to keep the file small.
    cutoff = (today - dt.timedelta(days=430)).isoformat()
    cache = {k: v for k, v in cache.items() if k.split("@", 1)[1] >= cutoff}

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(
            {"scraped_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "events": events, "range_cache": cache},
            indent=1,
            ensure_ascii=False,
        )
        + "\n"
    )
    kinds = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    annotate("notice", f"wrote {len(events)} events {kinds}; HTTP {dict(STATUS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
