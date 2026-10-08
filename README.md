# Shonar holiday calendar

Subscribable calendars of every holiday and observance listed on [nationaltoday.com](https://nationaltoday.com), for planning promo assets for the [Shonar stream](https://twitch.tv/djshonar).

A GitHub Action scrapes the site every Monday, keeps the results in `data/events.json`, and publishes `.ics` feeds plus a landing page to GitHub Pages.

## Feeds

| Feed | What it contains |
| --- | --- |
| `daily.ics` | One all-day event per day; every holiday for that day is in the notes. |
| `weekly.ics` | A Monday planning event: weeks starting that week, each day's holidays, and observances still running. |
| `observances.ics` | Week-long and month-long observances as multi-day events. |
| `all.ics` | Every holiday as its own event. |
| `categories/*.ics` | One feed per National Today category (Food & Beverage, Animal, Cause, ...). |

## How it works

- `scraper/scrape.py` reads the twelve `/<month>-holidays/` pages. Those pages show the next occurrence of each holiday but omit the year, so the year is inferred from the printed weekday.
- Week- and month-long observances appear under their start day with no end date. For those, the scraper reads the range from the holiday's own page heading (for example "National Walk Your Dog Week — October 1–7, 2026") and caches it.
- Past events from earlier runs are kept, so the calendar keeps its history after the site rolls a month forward to next year.
- `scraper/build_ics.py` turns `data/events.json` into the feeds in `public/`.

Run locally:

```sh
pip install -r scraper/requirements.txt
python scraper/scrape.py
python scraper/build_ics.py
```

Trigger a refresh from the Actions tab with **Run workflow**.
