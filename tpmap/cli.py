"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import BASE_URL, __version__
from .net import DEFAULT_UA, Fetcher

log = logging.getLogger("tpmap")


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)-7s %(name)s: %(message)s")


def _fetcher(args) -> Fetcher:
    return Fetcher(rate=args.rate, timeout=args.timeout, retries=args.retries,
                   user_agent=args.user_agent, obey_robots=not args.ignore_robots,
                   cache_dir=args.cache)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--rate", type=float, default=1.0,
                   help="max requests per second per host (default: 1.0)")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    p.add_argument("--retries", type=int, default=4, help="retries per request")
    p.add_argument("--user-agent", default=DEFAULT_UA)
    p.add_argument("--ignore-robots", action="store_true",
                   help="do not consult robots.txt (you are responsible for this)")
    p.add_argument("--cache", default=None, metavar="DIR",
                   help="cache HTTP bodies in DIR so re-runs are cheap")
    p.add_argument("-v", "--verbose", action="count", default=0)


def _browser_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--wait", type=float, default=6.0,
                   help="seconds to let the map settle after load (default: 6)")
    p.add_argument("--headed", action="store_true",
                   help="show the browser; pair with a long --wait and click around "
                        "to make the site load layers you cannot reach otherwise")
    p.add_argument("--click-layers", action="store_true",
                   help="auto-toggle layer controls to surface lazily loaded data")
    p.add_argument("--browser-path", default=None, metavar="EXE",
                   help="Chromium/Chrome binary to drive (also honours $TPMAP_CHROMIUM)")


# --------------------------------------------------------------------------

def cmd_discover(args) -> int:
    from .discover import discover_page

    report, _ = discover_page(args.url, headed=args.headed, wait=args.wait,
                              click_layers=args.click_layers,
                              user_agent=args.user_agent,
                              executable_path=args.browser_path)
    data = report.to_dict()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"\n{args.url}")
        print(f"  {len(report.hits)} endpoint(s), "
              f"{data['inline_layers']} in-page layer(s) "
              f"({data['inline_features']} features)")
        if report.hits:
            print()
            width = max(len(h.kind) for h in report.hits)
            for h in report.hits:
                size = f"  {h.size / 1024:.0f} KB" if h.size else ""
                print(f"  [{h.kind:<{width}}] {h.url}")
                print(f"   {'':<{width}}  via {h.source}{size}"
                      + (f" -- {h.note}" if h.note else ""))
        for err in report.errors:
            print(f"  ! {err}")
        if not report.hits and not report.inline:
            print("\n  Nothing found. Try: --headed --wait 30 (then pan/zoom the map "
                  "yourself), or --click-layers.")

    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.out}")
    return 0 if report.hits or report.inline else 1


def cmd_list(args) -> int:
    from .crawl import discover_pages

    with _fetcher(args) as f:
        urls = discover_pages(f, args.base, max_pages=args.max_pages,
                              map_pages_only=not args.all_pages)
    if args.limit:
        urls = urls[:args.limit]
    for u in urls:
        print(u)
    if args.out:
        Path(args.out).write_text("\n".join(urls) + "\n", encoding="utf-8")
        print(f"\n{len(urls)} url(s) written to {args.out}", file=sys.stderr)
    return 0 if urls else 1


def cmd_fetch(args) -> int:
    from .extract import harvest_page

    urls: list[str] = list(args.urls)
    if args.from_file:
        urls += [l.strip() for l in Path(args.from_file).read_text().splitlines()
                 if l.strip() and not l.startswith("#")]

    with _fetcher(args) as f:
        if args.all:
            from .crawl import discover_pages
            urls += discover_pages(f, args.base, max_pages=args.max_pages,
                                   map_pages_only=not args.all_pages)
        urls = list(dict.fromkeys(urls))
        if not urls:
            print("nothing to fetch: pass URLs, --from-file, or --all", file=sys.stderr)
            return 2
        if args.limit:
            urls = urls[:args.limit]

        outdir = Path(args.out)
        done = failed = 0
        for i, url in enumerate(urls, start=1):
            if args.resume:
                from .net import url_to_stem
                stem = url_to_stem(url)
                if list(outdir.glob(f"{stem}*.km[lz]")):
                    print(f"[{i}/{len(urls)}] skip (already have) {url}")
                    done += 1
                    continue
            print(f"[{i}/{len(urls)}] {url}")
            try:
                res = harvest_page(url, outdir, fetcher=f, headed=args.headed,
                                   wait=args.wait, click_layers=args.click_layers,
                                   split=args.split, fmt=args.format,
                                   arcgis=args.arcgis, user_agent=args.user_agent,
                                   executable_path=args.browser_path)
            except Exception as exc:
                log.exception("harvest failed")
                print(f"    ! {exc}")
                failed += 1
                continue

            if res.ok:
                done += 1
                for path in res.files:
                    print(f"    -> {path}")
                print(f"    {res.placemarks} placemark(s) from {res.hits} endpoint(s)")
            else:
                failed += 1
                for err in res.errors[:3]:
                    print(f"    ! {err}")

    print(f"\n{done} page(s) yielded KML, {failed} did not.")
    return 0 if done else 1


# --------------------------------------------------------------------------

def _read_input(path: Path) -> bytes:
    if str(path) == "-":
        return sys.stdin.buffer.read()
    return path.read_bytes()


def cmd_convert(args) -> int:
    """Turn geodata files already on disk into KML -- no browser, no network."""
    from . import kml as K
    from .coerce import coerce_to_feature_collection

    inputs = [Path(p) for p in args.inputs]
    missing = [p for p in inputs if str(p) != "-" and not p.exists()]
    if missing:
        for p in missing:
            print(f"no such file: {p}", file=sys.stderr)
        return 2

    single_out = Path(args.out) if args.out else None
    merge = single_out is not None and not args.split
    written: list[Path] = []
    pending: list[tuple[Path, dict]] = []     # (source, FeatureCollection)

    def emit(dest: Path, blob: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix.lower() == ".kmz":
            dest.write_bytes(K.kml_to_kmz(blob.decode("utf-8", "replace")))
        else:
            dest.write_bytes(blob)
        written.append(dest)
        print(f"  -> {dest}  ({K.count_placemarks(blob)} placemark(s))")

    for src in inputs:
        try:
            blob = _read_input(src)
        except OSError as exc:
            print(f"  ! {src}: {exc}", file=sys.stderr)
            continue

        stem = "stdin" if str(src) == "-" else src.stem
        ext = "" if str(src) == "-" else src.suffix.lower()

        # KMZ and KML need no conversion, only unwrapping or copying.
        if ext == ".kmz" or K.is_kmz_bytes(blob):
            try:
                emit(single_out if (single_out and len(inputs) == 1)
                     else src.with_suffix(".kml"), K.kmz_to_kml(blob))
            except Exception as exc:
                print(f"  ! {src}: {exc}", file=sys.stderr)
            continue
        if ext == ".kml" or K.is_kml_bytes(blob):
            print(f"  = {src} is already KML, nothing to do")
            continue

        try:
            obj = json.loads(blob.decode("utf-8", errors="replace"))
        except ValueError as exc:
            print(f"  ! {src}: not valid JSON ({exc})", file=sys.stderr)
            continue

        try:
            fc = coerce_to_feature_collection(obj, latlon=args.latlon)
        except Exception as exc:
            print(f"  ! {src}: {exc}", file=sys.stderr)
            if _is_discovery_report(obj):
                print("    This looks like a tpmap discovery report, not geodata. "
                      "It lists the endpoints found; fetch one of those URLs instead.",
                      file=sys.stderr)
            continue

        print(f"  {src}: {len(fc['features'])} feature(s)")
        if merge:
            pending.append((src, fc))
        else:
            dest = single_out if (single_out and len(inputs) == 1) \
                else src.with_suffix(".kml")
            emit(dest, K.feature_collection_to_kml(fc, args.name or stem).encode("utf-8"))

    if merge and pending:
        combined = K.merge_feature_collections([fc for _, fc in pending])
        emit(single_out, K.feature_collection_to_kml(
            combined, args.name or single_out.stem).encode("utf-8"))

    if not written:
        print("\nnothing was converted.", file=sys.stderr)
        return 1
    print(f"\n{len(written)} file(s) written.")
    return 0


def _is_discovery_report(obj) -> bool:
    return isinstance(obj, dict) and "page_url" in obj and "hits" in obj


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tpmap",
        description="Scrape KML geodata out of townplanmap.com.",
        epilog="Start with 'tpmap discover <page-url>' to see what a page actually serves.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="report the geodata endpoints a page uses")
    d.add_argument("url")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--out", help="also write the report to this file")
    _browser_opts(d)
    _common(d)
    d.set_defaults(func=cmd_discover)

    l = sub.add_parser("list", help="enumerate scrapable pages")
    l.add_argument("--base", default=BASE_URL)
    l.add_argument("--all-pages", action="store_true",
                   help="do not filter down to map pages")
    l.add_argument("--max-pages", type=int, default=300, help="link-crawl budget")
    l.add_argument("--limit", type=int, default=0)
    l.add_argument("--out", help="write the URL list to this file")
    _common(l)
    l.set_defaults(func=cmd_list)

    g = sub.add_parser("fetch", help="download KML for one or more pages")
    g.add_argument("urls", nargs="*")
    g.add_argument("--all", action="store_true", help="fetch every discoverable map page")
    g.add_argument("--from-file", help="read page URLs from a file, one per line")
    g.add_argument("--base", default=BASE_URL)
    g.add_argument("--all-pages", action="store_true")
    g.add_argument("--max-pages", type=int, default=300)
    g.add_argument("--limit", type=int, default=0)
    g.add_argument("-o", "--out", default="output", help="output directory")
    g.add_argument("--format", choices=["kml", "kmz"], default="kml")
    g.add_argument("--split", action="store_true",
                   help="one file per layer instead of a merged document")
    g.add_argument("--arcgis", action="store_true",
                   help="page through any ArcGIS service found, not just the viewport")
    g.add_argument("--resume", action="store_true", help="skip pages already downloaded")
    _browser_opts(g)
    _common(g)
    g.set_defaults(func=cmd_fetch)

    c = sub.add_parser("convert", help="convert JSON/GeoJSON/KMZ files on disk to KML")
    c.add_argument("inputs", nargs="+", metavar="FILE",
                   help=".json, .geojson, .kmz (or - for stdin)")
    c.add_argument("-o", "--out", help="output .kml file; without it each input "
                                       "becomes <input>.kml")
    c.add_argument("--split", action="store_true",
                   help="one .kml per input even when --out is given")
    c.add_argument("--latlon", action="store_true",
                   help="coordinates are [lat, lon]; swap them (default is GeoJSON "
                        "order, [lon, lat])")
    c.add_argument("--name", help="document name written into the KML")
    c.add_argument("-v", "--verbose", action="count", default=0)
    c.set_defaults(func=cmd_convert)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
