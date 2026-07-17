# Only Screening

A small web app that shows the films actually playing at Toronto rep / arthouse
cinemas — with real posters, live showtimes, and clickable "buy tickets" links.

`data.js` is generated from live theatre listings by `update.py` (see below); it
is not hand-authored. `data.curated-backup.js` is the earlier hand-curated demo,
kept only as a reference — delete it whenever.

## Run it

```
cd unique-movies
python3 -m http.server 8777
# open http://localhost:8777
```

Or just double-click `index.html`.

## Files

| File | What it is |
|------|-----------|
| `index.html` | The whole app — UI, search, theatre filters, sort, poster art, modal. |
| `data.js` | **Generated** by `update.py` from live listings. Don't edit by hand. |
| `update.py` | Scrapes each theatre and regenerates `data.js` (see below). |
| `com.onlyscreening.update.plist` | macOS schedule to run the updater daily. |
| `data.curated-backup.js` | The old hand-curated demo, kept for reference only. |

## Updating the listings

```
python3 update.py --rebuild    # scrape every theatre, regenerate data.js
python3 update.py --all        # --rebuild, then fill any missing posters
python3 update.py --rebuild --dry-run    # preview; write nothing
```

`--rebuild` runs one **adapter per theatre**, groups the same film showing at
multiple venues, and writes a fresh `data.js`. There is no single feed for Toronto
rep showtimes — each cinema stores them differently — so each theatre is a small
adapter in the `SOURCES` table.

**Live today:**

- **`revue`** — reads `revuecinema.ca` (the WordPress `films` REST list, then each
  film page for its showtimes, director/runtime/country/language, synopsis, poster,
  and Agile Ticketing "Buy Tickets" links).
- **`fox`** — reads `foxtheatre.ca` (the WordPress `movies` REST list, then each
  page's `showtimes-lists` block and Agile Ticketing links).
- **`paradise`** — reads `paradiseonbloor.com`'s JSON showtime API
  (`/wp-json/nj/v1/showtime/listings`): titles, runtime, year, screening datetimes,
  and direct purchase links in one call.
- **`tiff`** — reads `tiff.net/filmlisttemplatejson`: the full Lightbox programme
  with directors, genres, countries, posters, and every screening's venue and
  ticket link. Press/industry and cancelled screenings are filtered out.
- **`carlton`** — Carlton Cinema (Imagine Cinemas) sells through OmniWeb
  Ticketing, which embeds each day's schedule as JSON (`gMovieData`) — title,
  synopsis, runtime, poster, and per-performance ticket links. The adapter
  fetches the next 8 days, one request per day.

**Metadata enrichment:** after scraping, `--all` fills any missing
poster / director / runtime / country / language / synopsis / year and attaches a
**Letterboxd rating** to each film. Posters come from the theatres first (all five
now expose them); metadata comes from **TMDB** when a key is set, otherwise
Wikipedia (infobox + REST summary).

- **TMDB (recommended).** Get a free key at themoviedb.org → Settings → API, then
  `export TMDB_KEY=…` before running (or add a repo secret named `TMDB_KEY` for the
  GitHub Action). Faster and more complete than Wikipedia, and it doesn't get
  rate-limited in CI. Without a key, Wikipedia is used automatically.
- **Letterboxd rating** is scraped from each film's page (no key; there's no public
  API). Matched by URL slug, with a year-suffix fallback.

Each rebuild also writes `data.json` (a machine-readable mirror). You can top up
metadata + ratings **without re-scraping every theatre**:

```
python3 update.py --enrich              # one pass over incomplete films
python3 update.py --enrich --passes 3   # repeat until nothing new fills
```

Both `--all` and `--enrich` print a coverage line (e.g.
`poster:100% · director:92% · … · rating:88%`). Earned metadata and ratings are
**carried forward** across rebuilds (matched by title), so nightly re-scrapes never
lose them. Films that stay sparse are the non-film events on these calendars
(repertory Q&As, festival passes, "public hours") with no film metadata to find.

**Add another theatre** by writing a function that returns a list of listings and
registering it in `SOURCES`. Each listing looks like:

```python
{"theatre": "paradise", "title": "...", "year": 2024,
 "director": "", "runtime": None, "country": "", "lang": "", "why": "",
 "poster": "https://…", "screenings": [{"date": "2026-07-20", "time": "19:00",
                                        "url": "https://…tickets…"}]}
```

Remaining targets: The Royal and Hot Docs — both render their listings entirely
in JavaScript with no accessible feed, so they'd need a headless-browser scrape.

> Heads-up: these adapters parse each theatre's live HTML, so if a venue redesigns
> its site the matching adapter may need a small update. That's inherent to
> scraping — there's no official API.

## Schedule it to run daily (macOS)

```
cp com.onlyscreening.update.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.onlyscreening.update.plist
launchctl start com.onlyscreening.update      # run once now
```

Runs `update.py --all` every day at 06:15; output goes to `update.log`.
On Linux the equivalent cron line is:

```
15 6 * * *  cd /path/to/unique-movies && /usr/bin/python3 update.py --all >> update.log 2>&1
```

## Notes

- Posters hotlink to each theatre's own site (with a Wikipedia fallback). To
  self-host, download them into a `posters/` folder and rewrite the `poster:`
  fields to local paths.
- Showtime pills link to the exact ticketing page for that screening; the theatre
  name links to its listings page. Both open in a new tab.
- The generated art behind each poster is a fallback: if a poster URL ever fails,
  the art shows through instead of a blank box.
