#!/usr/bin/env python3
"""
Auto-updater for "Only Screening".

  python3 update.py --rebuild       Fetch live listings from every theatre
                                    adapter and regenerate data.js from scratch.
  python3 update.py --posters       Fill any missing posters from Wikipedia.
  python3 update.py --all           --rebuild then --posters.
  python3 update.py --dry-run ...   Show what would happen; write nothing.

No third-party packages — standard library + the `curl` that ships with macOS.

HOW THE DATA IS REAL
--------------------
There is no single feed for Toronto rep-cinema showtimes, so this file has one
small ADAPTER per theatre. Each adapter reads that venue's own site and returns a
normalized list of listings. `--rebuild` runs them all, groups the same film
showing at multiple theatres, and writes data.js. The app just renders data.js.

Implemented and live:
  • revue — reads revuecinema.ca (WP REST film list + each film page's showtimes,
            posters, and Agile Ticketing "Buy Tickets" links).
  • fox   — reads foxtheatre.ca (WP REST movie list + each page's showtimes-list
            and Agile Ticketing links).
Add a theatre by writing an adapter that returns the same shape and registering
it in SOURCES.  Listing shape:
  {theatre, title, year, director, runtime, country, lang, why, poster,
   screenings: [{date:"YYYY-MM-DD", time:"HH:MM", url:"<ticket link>"}, ...]}
"""

import argparse, html, json, re, subprocess, sys, time, urllib.parse
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).with_name("data.js")
DATA_JSON = Path(__file__).with_name("data.json")   # machine-readable mirror, for --enrich
UA = "Mozilla/5.0 (OnlyScreening updater)"
MONTHS = {m: i for i, m in enumerate(
    ["January","February","March","April","May","June","July",
     "August","September","October","November","December"], 1)}

# Static venue info (hood / kind / booking page). Adapters attach here by id.
THEATRES = {
    "revue":    {"name": "Revue Cinema", "hood": "Roncesvalles", "kind": "Rep / Non-profit",
                 "book": "https://revuecinema.ca/coming-soon/"},
    "fox":      {"name": "Fox Theatre", "hood": "The Beaches", "kind": "Rep / Independent",
                 "book": "https://www.foxtheatre.ca/whats-on/now-showing/"},
    "paradise": {"name": "Paradise Theatre", "hood": "Bloorcourt", "kind": "Rep / Independent",
                 "book": "https://paradiseonbloor.com/coming-soon/"},
    "tiff":     {"name": "TIFF Lightbox", "hood": "King West", "kind": "Cinematheque",
                 "book": "https://www.tiff.net/calendar"},
    "carlton":  {"name": "Carlton Cinema", "hood": "Church-Wellesley", "kind": "Arthouse / First-run",
                 "book": "https://imaginecinemas.com/cinema/carlton-cinema/"},
}


# ---------------------------------------------------------------------------
def get(url, t=20, retries=2):
    """Fetch a URL, retrying on empty/failed responses (Wikipedia rate-limits
    batches, so a single miss shouldn't drop a film's whole metadata)."""
    for attempt in range(retries + 1):
        r = subprocess.run(["curl", "-sL", "--max-time", str(t), "-A", UA, url],
                           capture_output=True, text=True)
        if r.stdout.strip():
            return r.stdout
        if attempt < retries:
            subprocess.run(["sleep", "0.6"])
    return r.stdout

def og(h, p):
    m = re.search(r'og:%s"\s+content="([^"]*)"' % p, h)
    return html.unescape(m.group(1)).strip() if m else ""

def to24(hour, minute, ap):
    hour = int(hour) % 12
    if ap.upper() == "PM": hour += 12
    return "%02d:%02d" % (hour, int(minute))

def iso_from(month, day):
    """Assume the current year; roll to next year if the date is well past."""
    today = date.today()
    for y in (today.year, today.year + 1):
        try: d = date(y, month, day)
        except ValueError: return None
        if (d - today).days >= -14: return d.isoformat()
    return None

def year_from(title):
    m = re.search(r'\((19|20)\d{2}\)', title)
    return int(m.group(0).strip("()")) if m else None

# Words kept lowercase when title-casing an ALL-CAPS title.
SMALL = {"a","an","and","as","at","but","by","for","from","in","into","of","on",
         "or","the","to","vs","with","de","des","du","la","le","les","et"}

# Words that flag a leading "<Series>:" fragment as a programme label, not a film.
SERIES_KW = re.compile(
    r'\b(presents?|event|cinema|series|society|club|midnight|throwback|silent|'
    r'rental|festival|staff pick|picks?|reel drag|drag|spectacle|klassic|slumber|'
    r'sexes|bollywood|horror-rama|nerds|political|animals|neon dreams|paid in sweat|'
    r'bob fosse|smoke please|black belt|highway to hell|designing|perfect date|'
    r'hold up|nightmare alley|unsubculture|book and film|sing-along|really like her|'
    r'dumpster|matinee|hooray|first run|screening|members|drunken|of the month|'
    r'restoration|quote along|grlish|staff picks?)\b', re.I)

# Trailing notes after a separator (— : + ) that are programme fluff, not title.
FLUFF_TAIL = re.compile(
    r'\s*[:+–—-]\s*(?:canadian|toronto|world|north american|u\.?s\.?|'
    r'\d+(?:st|nd|rd|th)\s+anniversary|anniversary|screening|premiere|lecture|'
    r'double feature|book launch|presented|restoration|in attendance|sing-?along|'
    r'q\s*&?\s*a|new 4k|new restoration|special|reissue|re-?release|\d+mm|matinee|'
    r'encore|director|featuring|first run|opening night|closing night|gala|'
    r'film society|with .*(?:attendance|q\s*&?\s*a)).*$', re.I)

def _cap(word):
    return word[:1].upper() + word[1:] if word else word

def titlecase(t):
    words = t.split()
    out = []
    after_break = True                           # start, or just after a ":" → capitalize
    for w in words:
        # split off attached punctuation ("TRASH:" -> Trash:, hyphens handled below)
        m = re.match(r"^([^A-Za-z]*)([A-Za-z][A-Za-z'\-]*)([^A-Za-z]*)$", w)
        if not m:                                # keep WALL·E, Se7en, 13 as-is
            out.append(w); after_break = w.endswith(":"); continue
        pre, core, suf = m.groups()
        lw = core.lower()
        if out and not after_break and lw in SMALL:  # small word mid-title stays lower
            core = lw
        else:                                        # else title-case each hyphen segment
            core = "-".join(_cap(p.lower()) for p in core.split("-"))
        out.append(pre + core + suf)
        after_break = w.endswith(":")
    return " ".join(out)

def _is_caps(tok):
    letters = [c for c in tok if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)

def caps_run(t):
    """Revue writes the real film title in ALL CAPS amid title-case programme
    text ("Silent Revue: FAUST – 100th Anniversary!"). Pull the longest caps run."""
    toks = t.split()
    runs, cur = [], []
    for tok in toks:
        neutral = not any(c.isalpha() for c in tok) and tok not in "–—-|"
        if _is_caps(tok) or (cur and neutral):
            cur.append(tok)
        else:
            if cur: runs.append(cur); cur = []
    if cur: runs.append(cur)
    def ok(run):
        caps = [w for w in run if _is_caps(w)]
        return len(caps) >= 2 or any(len([c for c in w if c.isalpha()]) >= 4 for w in caps)
    runs = [r for r in runs if ok(r)]
    if not runs: return None
    best = max(runs, key=lambda r: (sum(_is_caps(w) for w in r), len(r)))
    while best and not any(c.isalnum() for c in best[-1]): best.pop()  # drop trailing punct, keep digits
    return " ".join(best) if best else None

def strip_series_prefix(t):
    for _ in range(3):
        m = re.match(r'^(.{2,55}?)\s*[:!]\s+(.+)$', t)
        if m and SERIES_KW.search(m.group(1)): t = m.group(2)
        else: break
    return t

def clean_title(raw):
    """Strip series prefixes, format/premiere notes and trailing year; normalize
    the ALL-CAPS film titles that the theatres list."""
    t = html.unescape(raw).replace("’", "'").replace("‘", "'")
    t = re.sub(r'\s*[-–—]\s*(Fox Theatre|Revue Cinema).*$', '', t, flags=re.I)
    t = re.sub(r'\s*\((?:19|20)\d{2}\)', '', t)          # year stored separately
    t = strip_series_prefix(t)                            # drop known series prefixes first
    cr = caps_run(t)
    if cr:
        t = cr                                            # Revue caps-title case
    else:
        t = FLUFF_TAIL.sub('', t)
    t = re.sub(r'\s+w/\s*.*$', '', t, flags=re.I)         # "w/ Shadowcast!"
    t = re.sub(r"\s+(?:with\s+|[-–—+]\s*)?(?:a\s+|the\s+)?(?:director\s+|cast\s+)?q\s*&?\s*a\b.*$", '', t, flags=re.I)
    t = re.sub(r'\s+', ' ', t).strip(" :–—-!+")
    letters = re.sub(r'[^A-Za-z]', '', t)
    if letters and t == t.upper() and len(letters) > 3:
        t = titlecase(t)
    return t


# ===========================================================================
# ADAPTERS
# ===========================================================================
def revue():
    films = json.loads(get("https://revuecinema.ca/wp-json/wp/v2/films"
                           "?per_page=100&_fields=title,link"))
    out = []
    for f in films:
        link = f["link"]; h = get(link)
        raw = og(h, "title") or f["title"]["rendered"]
        title = clean_title(raw)
        st = re.findall(r'brxe-text-basic">([^<]*@[^<]*(?:AM|PM)[^<]*)</div>', h)
        tix = [html.unescape(t) for t in re.findall(
            r'href="(https://prod3\.agileticketing\.net/websales/pages/info\.aspx\?evtinfo=[^"]+)"', h)]
        screenings = []
        for i, txt in enumerate(st):
            m = re.search(r'(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*@\s*(\d{1,2}):(\d{2})\s*(AM|PM)',
                          html.unescape(txt))
            if not m or m.group(1) not in MONTHS: continue
            iso = iso_from(MONTHS[m.group(1)], int(m.group(2)))
            if iso: screenings.append({"date": iso, "time": to24(m.group(3), m.group(4), m.group(5)),
                                       "url": tix[i] if i < len(tix) else link})
        if not screenings: continue
        meta = lambda lbl: (lambda mm: html.unescape(mm.group(1)).strip() if mm else "")(
            re.search(lbl + r':</strong>\s*([^<|]+)', h))
        rt = re.search(r'(\d+)\s*mins', meta("Runtime") or "")
        yr = meta("Year")
        out.append({"theatre": "revue", "title": title,
                    "year": year_from(og(h, "title")) or (int(yr) if yr.isdigit() else None),
                    "director": meta("Director").split("(")[0].strip(),
                    "runtime": int(rt.group(1)) if rt else None,
                    "country": meta("Country"), "lang": meta("Language"),
                    "why": og(h, "description"), "poster": og(h, "image"),
                    "tags": tags_from(raw), "screenings": screenings})
        print(f"    · {title} — {len(screenings)} showtime(s)", file=sys.stderr)
    return out

def fox():
    films = json.loads(get("https://www.foxtheatre.ca/wp-json/wp/v2/movies"
                           "?per_page=100&_fields=title,link"))
    out = []
    for f in films:
        link = f["link"]; h = get(link)
        raw = f["title"]["rendered"]
        title = clean_title(raw)
        block = re.search(r'showtimes-lists"(.*?)(?:</section|footer|<!--)', h, re.S)
        screenings = []
        if block:
            for item in re.findall(r'<div class="item">(.*?)</div>\s*</div>', block.group(1), re.S):
                dm = re.search(r'class="date">([^<]+)<', item)
                mm = dm and re.search(r'(\w+)\s+(\d{1,2})', dm.group(1))
                if not mm or mm.group(1) not in MONTHS: continue
                for tm in re.finditer(r'class="time">([^<]+)</span>\s*<a[^>]+href="([^"]+)"', item):
                    tt = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)', tm.group(1), re.I)
                    iso = tt and iso_from(MONTHS[mm.group(1)], int(mm.group(2)))
                    if iso: screenings.append({"date": iso, "time": to24(tt.group(1), tt.group(2), tt.group(3)),
                                               "url": html.unescape(tm.group(2))})
        if not screenings: continue
        out.append({"theatre": "fox", "title": title, "year": year_from(raw),
                    "director": "", "runtime": None, "country": "", "lang": "",
                    "why": og(h, "description"), "poster": og(h, "image"),
                    "tags": tags_from(raw), "screenings": screenings})
        print(f"    · {title} — {len(screenings)} showtime(s)", file=sys.stderr)
    return out

def paradise():
    """Paradise exposes a clean JSON showtime API — movie names, runtime, year,
    each screening's datetime, and a direct purchase URL."""
    d = json.loads(get("https://paradiseonbloor.com/wp-json/nj/v1/showtime/listings"))
    by_id = {m["movie_id"]: m for m in d.get("movies", [])}
    grouped = {}
    for s in d.get("showtimes", []):
        mv = by_id.get(s["movie_id"])
        if not mv: continue
        dt = s.get("datetime", "")
        if len(dt) < 12: continue
        iso = f"{dt[0:4]}-{dt[4:6]}-{dt[6:8]}"
        grouped.setdefault(s["movie_id"], []).append(
            {"date": iso, "time": f"{dt[8:10]}:{dt[10:12]}", "url": s.get("purchase_url", "")})
    out = []
    for mid, screenings in grouped.items():
        mv = by_id[mid]
        raw = html.unescape(mv["movie_name"])
        yr = mv.get("release_year")
        rt = mv.get("runtime")
        out.append({"theatre": "paradise", "title": clean_title(raw),
                    "year": int(yr) if yr and str(yr).isdigit() else year_from(raw),
                    "director": "", "runtime": int(rt) if rt and str(rt).isdigit() else None,
                    "country": "", "lang": "", "why": "", "poster": "",
                    "tags": tags_from(raw), "screenings": screenings})
        print(f"    · {clean_title(raw)} — {len(screenings)} showtime(s)", file=sys.stderr)
    return out

def tiff():
    """TIFF publishes its whole programme as JSON at /filmlisttemplatejson —
    titles, directors, genres, countries, posters, and every screening with
    its venue and ticket link."""
    d = json.loads(get("https://www.tiff.net/filmlisttemplatejson", t=30))
    today = date.today().isoformat()
    out = []
    for it in d.get("items", []):
        screenings = []
        for s in it.get("scheduleItems", []):
            if s.get("cancelled") or s.get("pressAndIndustry") or s.get("industry") \
               or s.get("marketScreening") or s.get("digital"):
                continue
            st = s.get("startTime", "")
            if len(st) < 16 or st[:10] < today:
                continue
            url = s.get("url") or ("https://www.tiff.net" + it.get("url", ""))
            screenings.append({"date": st[:10], "time": st[11:16], "url": url})
        if not screenings:
            continue
        poster = it.get("posterUrl") or it.get("img") or ""
        if poster.startswith("//"): poster = "https:" + poster
        raw = it.get("title", "")
        out.append({"theatre": "tiff", "title": clean_title(raw),
                    "year": year_from(raw),
                    "director": ", ".join(it.get("directors") or []),
                    "runtime": None,
                    "country": (it.get("countries") or "").replace("United States of America", "USA"),
                    "lang": it.get("languages") or "",
                    "why": re.sub(r"<[^>]+>", "", it.get("description") or "").strip()[:220],
                    "poster": poster,
                    "tags": (it.get("genre") or [])[:3] or tags_from(raw),
                    "screenings": screenings})
        print(f"    · {clean_title(raw)} — {len(screenings)} showtime(s)", file=sys.stderr)
    return out

def _extract_js_object(h, marker):
    """Return the JSON object assigned right after `marker`, via brace matching."""
    i = h.find(marker)
    if i < 0: return None
    i = h.find("{", i)
    if i < 0: return None
    depth, j, in_str, esc = 0, i, False, False
    while j < len(h):
        c = h[j]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
        else:
            if c == '"': in_str = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0: return h[i:j+1]
        j += 1
    return None

def carlton():
    """Carlton (Imagine Cinemas) sells through OmniWeb Ticketing, which embeds
    the full day's schedule as JSON (`gMovieData`) in each date's page —
    title, synopsis, runtime, poster, and every performance with a ticket link.
    One request per day for the coming week."""
    base = "https://omniwebticketing6.com/imaginecinemas/carlton"
    poster_base = "https://omniwebticketing6.com/imaginecinemas/files-circuit/images/posters/"
    movies = {}
    for i in range(8):
        d = (date.today() + timedelta(days=i)).isoformat()
        h = get(f"{base}?schdate={d}", t=25)
        blob = _extract_js_object(h, "var gMovieData")
        if not blob: continue
        try: data = json.loads(blob)
        except Exception: continue
        for code, mv in data.items():
            m = movies.setdefault(code, {
                "title": html.unescape(mv.get("title") or ""),
                "why": html.unescape(re.sub(r"\*[^*]*\*\s*", "", mv.get("synopsis") or ""))[:220],
                "poster": (poster_base + urllib.parse.quote(mv["posterFileName"]))
                          if mv.get("posterFileName") else "",
                "runtime": None, "screenings": {},
            })
            rt = re.search(r"(\d+)\s*hr\s*(\d+)?", mv.get("runTimeStr") or "")
            if rt and not m["runtime"]:
                m["runtime"] = int(rt.group(1)) * 60 + int(rt.group(2) or 0)
            for aud in (mv.get("schAuds") or {}).values():
                for perfs in (aud.get("schPerfsGeneral"), aud.get("schPerfsReserved")):
                    for p in (perfs or {}).values():
                        dstr, t = p.get("schDateStr"), p.get("startTime")
                        if not dstr or not t: continue
                        m["screenings"][(dstr, t)] = base + p.get("linkStr", "")
    out = []
    for code, m in movies.items():
        if not m["screenings"]: continue
        screenings = [{"date": d, "time": t, "url": u}
                      for (d, t), u in sorted(m["screenings"].items())]
        out.append({"theatre": "carlton", "title": clean_title(m["title"]),
                    "year": None, "director": "", "runtime": m["runtime"],
                    "country": "", "lang": "", "why": m["why"],
                    "poster": m["poster"], "tags": tags_from(m["title"]),
                    "screenings": screenings})
        print(f"    · {clean_title(m['title'])} — {len(screenings)} showtime(s)", file=sys.stderr)
    return out

SOURCES = {"revue": revue, "fox": fox, "paradise": paradise, "tiff": tiff,
           "carlton": carlton}


# ===========================================================================
# GROUP + EMIT
# ===========================================================================
PALETTE = [["#2b3a2f","#e0b34d"],["#3a2b2b","#c94f4f"],["#1f2a44","#f0f0f0"],
           ["#3a2440","#4dd6c4"],["#123a3a","#7fd4e0"],["#2a3550","#e88b6a"],
           ["#40183a","#f24d94"],["#24303a","#9ab0c4"],["#3a3218","#d9c06a"],
           ["#101b2a","#e0562a"],["#3a2a1a","#f0a83c"],["#2a3324","#d4b26a"]]

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "film"

def norm(s):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", s).lower())

def tags_from(title):
    """Format/programme tags — read from the RAW title before it's cleaned,
    since that's where '35mm', '4K Restoration', 'Q&A' etc. live."""
    t = []
    for kw, lab in [("35mm","35mm"),("70mm","70mm"),("16mm","16mm"),("4k","4K"),
                    ("restoration","Restoration"),("premiere","Premiere"),("q&a","Q&A"),
                    ("sing-along","Sing-Along"),("director's cut","Director's Cut"),
                    ("shadowcast","Shadowcast")]:
        if kw in title.lower(): t.append(lab)
    return t[:3]

def group(listings):
    movies = {}
    for L in listings:
        k = norm(L["title"])
        m = movies.get(k)
        if not m:
            m = {"id": slug(L["title"]), "title": L["title"], "year": L["year"],
                 "director": L["director"], "runtime": L["runtime"], "country": L["country"],
                 "lang": L["lang"], "why": L["why"], "poster": L["poster"],
                 "tags": list(L.get("tags", [])), "screenings": []}
            movies[k] = m
        # fill blanks / union tags from later sources
        for fld in ("year","director","runtime","country","lang","why","poster"):
            if not m[fld] and L[fld]: m[fld] = L[fld]
        for tg in L.get("tags", []):
            if tg not in m["tags"]: m["tags"].append(tg)
        m["tags"] = m["tags"][:3]
        for s in L["screenings"]:
            m["screenings"].append({"theatre": L["theatre"], "dates": [s["date"]],
                                    "time": s["time"], "format": "", "url": s["url"]})
    ordered = list(movies.values())
    for i, m in enumerate(ordered):
        m["art"] = PALETTE[i % len(PALETTE)]
    return ordered

def carry_forward(movies):
    """Preserve metadata already earned in a previous build. Re-scraping rebuilds
    the movie list from scratch, so without this every rebuild would drop the
    poster/director/etc. that enrichment filled in — and CI (Wikipedia-throttled)
    could never rebuild them. Matched by normalized title."""
    if not DATA_JSON.exists():
        return movies
    try:
        old = {norm(m["title"]): m for m in json.loads(DATA_JSON.read_text())}
    except Exception:
        return movies
    carried = 0
    for m in movies:
        prev = old.get(norm(m["title"]))
        if not prev:
            continue
        for fld in ("year", "director", "runtime", "country", "lang", "why", "poster"):
            if not m.get(fld) and prev.get(fld):
                m[fld] = prev[fld]; carried = carried + 1
        # union any tags the old record had
        for tg in prev.get("tags", []):
            if tg not in m["tags"]:
                m["tags"].append(tg)
        m["tags"] = m["tags"][:3]
    if carried:
        print(f"Carried forward {carried} field(s) from the previous build.")
    return movies

def js(v):  # JSON is valid JS; keeps quotes/apostrophes safe
    return json.dumps(v, ensure_ascii=False)

def emit(movies):
    used = {tid for m in movies for s in m["screenings"] for tid in [s["theatre"]]}
    lines = [
        "// AUTO-GENERATED by update.py — do not edit by hand.",
        f"// Generated {date.today().isoformat()} from live theatre listings.",
        "",
        "const THEATRES = [",
    ]
    for tid, t in THEATRES.items():
        if tid not in used: continue
        lines.append(f'  {{ id: {js(tid)}, name: {js(t["name"])}, hood: {js(t["hood"])}, '
                     f'kind: {js(t["kind"])}, book: {js(t["book"])} }},')
    lines += ["];", "",
              "const MOVIES = ["]
    for m in movies:
        lines.append("  {")
        lines.append(f'    id: {js(m["id"])}, title: {js(m["title"])}, year: {js(m["year"])},')
        lines.append(f'    director: {js(m["director"])}, runtime: {js(m["runtime"])}, '
                     f'country: {js(m["country"])}, lang: {js(m["lang"])},')
        lines.append(f'    tags: {js(m["tags"])},')
        lines.append(f'    why: {js(m["why"])},')
        lines.append(f'    poster: {js(m["poster"] or None)}, art: {js(m["art"])},')
        lines.append("    screenings: [")
        for s in m["screenings"]:
            lines.append(f'      {{ theatre: {js(s["theatre"])}, dates: {js(s["dates"])}, '
                         f'time: {js(s["time"])}, format: {js(s["format"])}, url: {js(s["url"])} }},')
        lines.append("    ],")
        lines.append("  },")
    lines += ["];", ""]
    return "\n".join(lines)


# ===========================================================================
# WIKIPEDIA ENRICHMENT — poster + director/runtime/country/language/synopsis
# for films whose theatre page didn't provide them.
# ===========================================================================
def _strip_tags(s):
    s = re.sub(r"</li>\s*<li[^>]*>", ", ", s)           # plainlist items
    s = re.sub(r"</a>\s*<a\b[^>]*>", ", ", s)           # adjacent links = a list
    s = re.sub(r"<br\s*/?>", ", ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\[\d+\]", "", s)                       # footnote markers
    s = html.unescape(re.sub(r"\s+", " ", s))
    s = re.sub(r"\s*,(\s*,)+", ",", s)                  # collapse empty items
    return s.strip(" ,;")

def _infobox_row(h, label_re):
    # Wikipedia wraps labels as e.g. "Running time</div></th><td>…"; allow the
    # 1-3 closing tags that can sit between the label text and the value cell.
    m = re.search(label_re + r"\s*(?:</\w+>\s*){1,3}<td[^>]*>(.*?)</td>", h, re.S | re.I)
    return _strip_tags(m.group(1)) if m else ""

def wiki_film_page(title, year):
    """Resolve (article_html, article_title) for a film, or (None, None)."""
    for q in (f"{title} {year} film" if year else None, f"{title} film", title):
        if not q: continue
        u = ("https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=%s"
             "&srlimit=1&format=json" % urllib.parse.quote(q))
        try: art = json.loads(get(u))["query"]["search"][0]["title"]
        except Exception: continue
        h = get("https://en.wikipedia.org/wiki/" + urllib.parse.quote(art.replace(" ", "_")))
        if 'infobox' in h:
            return h, art
    return None, None

def wiki_enrich(m):
    """Fill any missing fields on movie dict `m` from its Wikipedia article.
    Only touches fields that are empty; only trusts film-infobox pages."""
    h, art = wiki_film_page(m["title"], m["year"])
    if not h: return False
    filled = []
    if not m["poster"]:
        im = re.search(r'infobox[^>]*>.*?<img[^>]+src="([^"]+)"', h, re.S)
        if im:
            src = im.group(1)
            if src.startswith("//"): src = "https:" + src
            m["poster"] = re.sub(r"/thumb/(.+?/[^/]+?\.(?:jpg|jpeg|png|gif))/[^/]+$",
                                 r"/\1", src, flags=re.I)
            filled.append("poster")
    if not m["director"]:
        d = _infobox_row(h, r"Directed\s+by")
        if d: m["director"] = d[:80]; filled.append("director")
    if not m["runtime"]:
        rt = re.search(r"(\d+)\s*min", _infobox_row(h, r"Running\s+time"))
        if rt: m["runtime"] = int(rt.group(1)); filled.append("runtime")
    if not m["country"]:
        c = _infobox_row(h, r"Countr(?:y|ies)")
        if c: m["country"] = c[:60].replace("United States", "USA"); filled.append("country")
    if not m["lang"]:
        l = _infobox_row(h, r"Languages?")
        if l: m["lang"] = l[:40]; filled.append("lang")
    if not m["year"]:
        y = re.search(r"\b(19|20)\d{2}\b", _infobox_row(h, r"Release\s+dates?") or art)
        if y: m["year"] = int(y.group(0)); filled.append("year")
    if not m["why"]:
        try:
            summ = json.loads(get("https://en.wikipedia.org/api/rest_v1/page/summary/" +
                                  urllib.parse.quote(art.replace(" ", "_"))))
            ext = (summ.get("extract") or "").strip()
            if ext: m["why"] = (ext[:217] + "…") if len(ext) > 220 else ext; filled.append("why")
        except Exception: pass
    if filled:
        print(f"    + {m['title']}: {', '.join(filled)}", file=sys.stderr)
    return bool(filled)


CORE_FIELDS = ("poster", "director", "runtime", "country", "lang", "why", "year")

def enrich_pass(movies):
    """Fill missing core fields on every incomplete film. Returns how many films
    gained at least one field. Re-runnable (safe to call repeatedly)."""
    need = [m for m in movies if not all(m.get(f) for f in CORE_FIELDS)]
    print(f"Enriching {len(need)} incomplete film(s) from Wikipedia …")
    changed = 0
    for i, m in enumerate(need, 1):
        try:
            if wiki_enrich(m): changed += 1
        except Exception as e:
            print(f"    ! {m['title']}: {e}", file=sys.stderr)
        time.sleep(0.25)                      # be polite; avoids Wikipedia throttling
        if i % 25 == 0:
            print(f"    … {i}/{len(need)}", file=sys.stderr)
    return changed

def coverage(movies):
    n = len(movies) or 1
    return " · ".join(f"{f}:{round(100*sum(1 for m in movies if m.get(f))/n)}%"
                      for f in CORE_FIELDS)

def write_all(movies, dry=False):
    out = emit(movies)
    if dry:
        print("\n--- data.js preview (first 40 lines) ---")
        print("\n".join(out.splitlines()[:40])); return
    DATA.write_text(out)
    DATA_JSON.write_text(json.dumps(movies, ensure_ascii=False, indent=1))
    print(f"Wrote {DATA.name} ({len(out.splitlines())} lines) + {DATA_JSON.name}.")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="re-scrape all theatres")
    ap.add_argument("--enrich", action="store_true",
                    help="top up missing metadata from Wikipedia, using data.json (no re-scrape)")
    ap.add_argument("--passes", type=int, default=1, help="how many enrich passes (--enrich)")
    ap.add_argument("--all", action="store_true", help="--rebuild then enrich")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not any([a.rebuild, a.enrich, a.all]):
        ap.print_help(); return

    if a.rebuild or a.all:
        listings = []
        for tid, adapter in SOURCES.items():
            print(f"Fetching {tid} …", file=sys.stderr)
            try: listings += adapter()
            except Exception as e: print(f"  {tid} failed: {e}", file=sys.stderr)
        movies = carry_forward(group(listings))
        print(f"\n{len(movies)} unique films · "
              f"{sum(len(m['screenings']) for m in movies)} showtimes · {len(SOURCES)} theatres")
        if a.all:
            enrich_pass(movies)
        print("Coverage:", coverage(movies))
        write_all(movies, a.dry_run)

    elif a.enrich:
        if not DATA_JSON.exists():
            print(f"{DATA_JSON.name} not found — run --rebuild first."); return
        movies = json.loads(DATA_JSON.read_text())
        print(f"Loaded {len(movies)} films · coverage before: {coverage(movies)}")
        for p in range(a.passes):
            changed = enrich_pass(movies)
            print(f"Pass {p+1}: filled fields on {changed} film(s).")
            if not changed: break
        print("Coverage after:", coverage(movies))
        write_all(movies, a.dry_run)


if __name__ == "__main__":
    main()
