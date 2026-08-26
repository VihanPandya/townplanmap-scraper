# townplanmap-scraper

Extracts map geodata from [townplanmap.com](https://townplanmap.com) and writes it as
**KML** you can open in Google Earth, QGIS, or ArcGIS.

TownPlanMap renders town-planning schemes, development plans and zoning overlays for
Indian cities in a JavaScript map application — the geometry is never in the served
HTML. Rather than scraping selectors that break on the next front-end deploy, this tool
drives a real browser and watches what the page itself loads.

## Install

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
playwright install chromium
```

**Windows (PowerShell)** — use `py`, not `python3`, and note the different
activate command:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
playwright install chromium
```

If `Activate.ps1` is blocked by execution policy, either run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first, or use
`.\.venv\Scripts\activate.bat`. Skipping the venv entirely also works — the tool
just installs into your global Python.

Without `pip install -e .`, run it as `python -m tpmap` everywhere below.

Already have a browser? Skip the download and point at it:

```bash
export TPMAP_CHROMIUM=/path/to/chrome     # or pass --browser-path
```

## Quickstart

**Always start with `discover`.** It tells you what a page actually serves before you
spend time downloading:

```bash
tpmap discover https://townplanmap.com/tp/town-planning-scheme-map-Gujarat-Ahmedabad-ognaj221
```

```
  3 endpoint(s), 1 in-page layer(s) (412 features)

  [kml    ] https://townplanmap.com/data/ognaj221.kml
            via source-scan -- literal reference in page source
  [geojson] https://api.townplanmap.com/v1/plots?tp=221
            via network  188 KB
```

Then pull the KML down:

```bash
# one page
tpmap fetch https://townplanmap.com/tp/town-planning-scheme-map-Gujarat-Ahmedabad-ognaj221 -o output

# every map page the site advertises
tpmap list --out pages.txt
tpmap fetch --from-file pages.txt -o output --resume
```

## How it works

The geometry can arrive by any of several routes, so discovery runs four channels at
once and merges the results:

| Channel | Catches |
|---|---|
| `network` | Every response the page fetches, classified by URL, content-type and a peek at the body — KML, KMZ, GeoJSON, Esri JSON, tiles, WMS/WFS. |
| `init-hook` | Constructors patched *before* page scripts run. `google.maps.KmlLayer` hands its URL to Google's servers, so that KML never appears in the browser's own network log — the hook is the only way to see it. |
| `source-scan` | Literal `.kml` / `.kmz` references in HTML and JS source, whether or not they are ever fetched. |
| `page-probe` | Live map objects after load, in **every frame**. Leaflet, Mapbox GL / MapLibre, OpenLayers and Google Data layers all hold parsed features in memory. Every property read is guarded: touching a cross-origin iframe's `Window` throws, and one uncaught throw would lose the whole channel. |
| `inline-json` | Page data embedded in the HTML — `__NEXT_DATA__`, `__NUXT__`, `__INITIAL_STATE__`, `<script type="application/json">`. Server-rendered apps never fetch this, so watching the network will never see it. |

Everything non-KML is normalised to GeoJSON and written out as KML. Native KML the site
authored itself is passed through **verbatim**, so its own styling survives.

Because the channels overlap on purpose — a layer fetched over the network is usually
also sitting parsed in the live map object — identical features are deduplicated before
writing.

## Output

```
output/
├── ognaj221-a1b2c3d4-src1.kml        # the site's own KML, untouched
├── ognaj221-a1b2c3d4-converted.kml   # GeoJSON/Esri layers converted and merged
└── _reports/
    └── ognaj221-a1b2c3d4.json        # every endpoint found, for debugging
```

Feature attributes are preserved twice over: as `<ExtendedData>` (machine-readable, and
what QGIS reads into its attribute table) and as an HTML table in `<description>` (what
Google Earth shows in the balloon). Placemark names are promoted from whichever of
`name`, `fp_no`, `plot_no`, `survey_no`, `tp_no`… the data provides.

## Pages behind a sign-in

If `discover` reports **"redirected to …"**, the page is gated and no amount of
waiting will produce a map.

**Use your own Chrome.** Logins that rely on reCAPTCHA or SMS OTP — Firebase phone
auth among them — frequently stall in an automation-controlled browser: the OTP
never sends. And Firebase Auth keeps its token in **IndexedDB**, which a saved
session file cannot carry. Both problems disappear if you drive the browser you
already signed into.

**Option A — let tpmap run its own Chrome (recommended).** One command, no need to
quit anything:

```bash
tpmap browser
```

A Chrome window opens on a profile of tpmap's own (`~/.tpmap/chrome-profile`). Sign in
there and open a scheme map. Your everyday Chrome is untouched, and because the profile
persists on disk the sign-in is remembered — IndexedDB included, which is where Firebase
Auth actually keeps its token. Then, leaving that window open:

```bash
tpmap fetch <url> -o output --cdp auto
```

`--cdp auto` reuses that browser if it is running and starts it if not.

**Don't guess URLs.** Navigate to the scheme you want in that window, zoom to the
extent you care about, then scrape what is on screen:

```bash
tpmap fetch --current -o output --cdp auto
```

`--current` reads that tab **in place** — it does not reload. A map only fetches the
features in view, so reloading would throw away exactly the plots you zoomed to; the
geometry is read out of the live map objects instead. If the open tab holds nothing
useful, it falls back to a fresh load automatically. `--reload` forces the fresh load.

`tpmap links --cdp auto` lists the map pages linked from the open page, so you can
find real scheme URLs instead of inventing them.

If a command says the browser has nothing loaded, see what it has open with
`tpmap browser --tabs`, and put a page in it with
`tpmap browser --open https://townplanmap.com`.

**Option A2 — attach to a Chrome you started yourself.** If you would rather use your
own instance, quit Chrome completely first — check Task Manager for `chrome.exe`, since
Chrome silently ignores `--remote-debugging-port` when an instance is already running,
which is the usual reason this appears not to work:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

```bash
tpmap fetch <url> -o output --cdp http://localhost:9222
```

Your browser is never closed by the tool.

**Option B — reuse a profile on disk.** Quit Chrome completely, then:

```bash
tpmap fetch <url> -o output --profile "C:\Users\you\AppData\Local\Google\Chrome\User Data"
```

**Option C — a saved session file.** Works for ordinary cookie logins, not for
Firebase:

```bash
tpmap login https://townplanmap.com --session session.json
tpmap fetch <url> -o output --session session.json
```

`tpmap login` warns you if it detects a Firebase login it cannot carry.

None of this bypasses authentication — you sign in as yourself, exactly as you would
by hand. Keep `session.json` and profile directories out of version control.

## When the site cannot be entered by URL

Some apps resolve their routes only through in-app navigation: paste a scheme URL
straight into the address bar and you land back on the home screen. There is then
nothing to point a scraper at.

Record instead. Start it, then simply use the site:

```bash
tpmap watch -o output --seconds 180
```

Every page in the browser is listened to, including tabs opened later, and anything
geodata-shaped that goes past is collected — then written as KML when the timer ends.
Browse to the scheme, let its plots draw, zoom to the full extent.

```
  142s left   3 geodata endpoint(s)   87 request(s) seen
    -> output/capture.kml
  412 placemark(s) captured.
```

## Converting JSON you already have

If you have a `.json` from the site (saved from the browser's network tab, or handed
to you by anything else), convert it without touching the network:

```bash
tpmap convert plots.json                       # writes plots.kml alongside it
tpmap convert a.json b.json -o merged.kml      # merge several into one
tpmap convert data.json --latlon               # coordinates are [lat, lon], swap them
cat data.json | tpmap convert - -o out.kml     # from stdin
```

It does not require the JSON to be GeoJSON. Geometry is recovered from:

- **API envelopes** — `{"status":"ok","data":{...}}`, at any nesting depth
- **Record arrays** — `{"plots":[{"id":1,"geometry":{...}}]}`
- **lat/lng objects** — `[{"lat":23.0,"lng":72.5}, ...]`, closed rings become polygons
- **WKT strings** — `POLYGON((72.5 23.0, ...))` from PostGIS-backed APIs
- **Encoded polylines** — under a key that says so (`encodedPath`, `polyline`)
- **Esri REST JSON** — including Web Mercator unprojection
- **Firestore typed values** — `{"doubleValue": 23.0}`, `geoPointValue`, `arrayValue`
  and `mapValue` wrappers, unwrapped before conversion

Ordinary API responses with no geometry are rejected rather than guessed at, so you get
a clear error instead of a KML full of nonsense.

## Commands

```
tpmap browser           start the Chrome tpmap manages (sign in there once)
tpmap watch             record geodata while you browse the site yourself
tpmap links             list map pages linked from the page open in the browser
tpmap login [URL]       sign in yourself, save the session for later runs
tpmap discover URL      what geodata does this page serve?
tpmap list              enumerate scrapable pages (sitemap, else a link crawl)
tpmap fetch URL...      download and convert to KML
tpmap convert FILE...   turn JSON/GeoJSON/KMZ already on disk into KML (offline)
```

Useful flags:

| Flag | Effect |
|---|---|
| `--headed --wait 60` | Show the browser and keep recording while **you** pan, zoom and toggle layers by hand. The single most effective option when nothing is found automatically. |
| `--click-layers` | Auto-toggle layer controls to shake loose lazily loaded overlays. |
| `--arcgis` | If an ArcGIS service is found, page through the **whole** layer instead of just the viewport the map happened to request. |
| `--split` | One file per layer instead of a merged document. |
| `--format kmz` | Write KMZ instead of KML. |
| `--resume` | Skip pages already downloaded. |
| `--cache DIR` | Cache HTTP bodies so re-runs cost nothing. |
| `--rate N` | Requests per second per host (default 1.0). |

## Troubleshooting

**"I got a .json, not a .kml."** The only `.json` `fetch` ever writes is
`output/_reports/<name>.json` — that is a **diagnostic report listing the endpoints
found, not your data**. Getting one on its own means no KML was produced; open it and
look at `hits`. If it lists a `geojson`/`embedded`/`esri` endpoint, fetch that URL
directly and run `tpmap convert` on it. If `hits` is empty, see the next entry.

**"redirected to … /home"** — you *are* signed in; that URL just is not a real page.
Open the scheme you want in the browser and use `--current`.

**"redirected to …" (a login or landing page)** — the page requires sign-in. See *Pages behind a sign-in* above.

**Nothing found at all** — open `output/_reports/<name>.json`. It lists every
response the page made (`responses`), so you can see whether the page even loaded and
what it pulled. Send that file if you want help reading it.

**"no geodata found on this page"** — the map probably loads on interaction. Run
`tpmap discover URL --headed --wait 60`, then pan and zoom the map yourself; every
request you trigger is recorded. Check `_reports/*.json` to see what *was* seen.

**Only tiles were found** (`[tiles]` hits, no vectors) — that layer is rendered
server-side as images. There is no vector geometry to extract; the underlying data
would have to come from the source agency.

**An ArcGIS service was found** — re-run with `--arcgis` to export the full layer.

**Chromium won't launch** — set `TPMAP_CHROMIUM` or pass `--browser-path`.

**`--cdp` says "no Chrome is listening"** — either Chrome was already running when you
passed `--remote-debugging-port` (quit it fully and retry), or nothing is on that port.
Check by opening `http://127.0.0.1:9222/json/version`. The IPv4/IPv6 spelling is
handled for you: `localhost` is tried as `127.0.0.1` first, since Chrome binds IPv4
only while Windows resolves `localhost` to `::1`.

## Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

The suite runs the real scraper end-to-end against a local fixture site
(`tests/fixture_site/`) that reproduces each discovery channel — an XHR-loaded GeoJSON
layer, a `.kml` referenced only in script source, an Esri payload in Web Mercator, and a
live map object holding parsed features. Serve it by hand with
`python3 tests/serve_fixture.py 8777`.

## Status

The pipeline is verified end-to-end against the fixture site. It has **not** been run
against townplanmap.com itself — the sandbox this was built in blocks outbound requests
to that domain — so the exact endpoint shapes it will meet there are unconfirmed. That is
why discovery is structured around observing the site rather than assuming its layout,
and why `tpmap discover` exists: run it first and it will tell you what is really there.

## Please scrape politely

Defaults are conservative: robots.txt is respected, `Crawl-delay` is honoured, requests
are limited to one per second per host with jittered backoff, and responses already
captured by the browser are never re-fetched. `--ignore-robots` exists for cases where
you have permission that robots.txt does not express; using it is your call and your
responsibility.

Planning-scheme data is generally published by government bodies, but TownPlanMap's
compilation of it is their work and their terms of use apply. Check them, and don't
redistribute what you are not entitled to.
