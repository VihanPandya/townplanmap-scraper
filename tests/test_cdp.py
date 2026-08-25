"""Attaching to a browser the user started themselves.

This is the route past logins that refuse to run under automation (reCAPTCHA,
Firebase phone auth) and past auth tokens kept in IndexedDB, which a saved
storage_state cannot carry.
"""

import socket
import subprocess
import time
import urllib.request

import pytest

from tpmap.discover import chromium_executable, discover_page


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def user_chrome(tmp_path_factory):
    """A Chrome started the way a person would, with remote debugging on."""
    exe = chromium_executable()
    if not exe:
        pytest.skip("no chromium available")
    port = _free_port()
    proc = subprocess.Popen(
        [exe, "--headless=new", f"--remote-debugging-port={port}", "--no-sandbox",
         f"--user-data-dir={tmp_path_factory.mktemp('profile')}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{endpoint}/json/version", timeout=1).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("chrome did not expose a debugging port")
    try:
        yield endpoint, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_attaches_and_scrapes(site, user_chrome):
    endpoint, _ = user_chrome
    report, _ = discover_page(f"{site}/tp/scheme-c.html", wait=2.5, cdp_url=endpoint)
    assert [h.kind for h in report.hits] == ["embedded"]


def test_the_users_browser_is_left_running(site, user_chrome):
    """Closing a browser we did not start would kill the user's session."""
    endpoint, proc = user_chrome
    discover_page(f"{site}/tp/scheme-c.html", wait=2.0, cdp_url=endpoint)
    assert proc.poll() is None
    urllib.request.urlopen(f"{endpoint}/json/version", timeout=5).read()


def test_a_dead_endpoint_is_reported_not_raised(site):
    report, _ = discover_page(f"{site}/tp/scheme-c.html", wait=1.0,
                              cdp_url=f"http://127.0.0.1:{_free_port()}")
    assert not report.hits
    assert any("no Chrome is listening" in e for e in report.errors)


def test_localhost_is_tried_as_ipv4_first():
    """Chrome binds IPv4 only; localhost resolves to ::1 first on Windows."""
    from tpmap.discover import cdp_endpoints
    assert cdp_endpoints("http://localhost:9222")[0] == "http://127.0.0.1:9222"
    assert "http://[::1]:9222" in cdp_endpoints("http://localhost:9222")
    # a bare host:port is accepted
    assert cdp_endpoints("localhost:9222")[0] == "http://127.0.0.1:9222"
    # a real remote host is left alone
    assert cdp_endpoints("http://10.0.0.4:9222") == ["http://10.0.0.4:9222"]


def test_attaching_via_localhost_spelling_works(site, user_chrome):
    """The exact invocation that failed with ECONNREFUSED ::1."""
    endpoint, _ = user_chrome
    port = endpoint.rsplit(":", 1)[1]
    report, _ = discover_page(f"{site}/tp/scheme-c.html", wait=2.5,
                              cdp_url=f"http://localhost:{port}")
    assert [h.kind for h in report.hits] == ["embedded"]


def test_unreachable_endpoint_explains_the_fix(site):
    report, _ = discover_page(f"{site}/tp/scheme-c.html", wait=1.0,
                              cdp_url=f"http://localhost:{_free_port()}")
    assert not report.hits
    msg = "\n".join(report.errors)
    assert "no Chrome is listening" in msg
    assert "Quit Chrome completely" in msg
    assert "127.0.0.1" in msg


def _open_tab(endpoint, url):
    """Open a tab in the attached browser and leave it there, as a user would."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        return page.url


def test_current_tab_reads_the_active_page(site, user_chrome):
    from tpmap.discover import current_tab
    endpoint, _ = user_chrome
    _open_tab(endpoint, f"{site}/tp/scheme-c.html")
    url, _links = current_tab(endpoint)
    assert url == f"{site}/tp/scheme-c.html"


def test_current_tab_follows_the_most_recent_tab(site, user_chrome):
    """Playwright's page order is not most-recently-used; Chrome's listing is."""
    from tpmap.discover import current_tab
    endpoint, _ = user_chrome
    _open_tab(endpoint, f"{site}/tp/scheme-c.html")
    _open_tab(endpoint, f"{site}/")
    url, links = current_tab(endpoint)
    assert url == f"{site}/"
    assert f"{site}/tp/scheme-a.html" in links


def test_fetch_current_scrapes_the_open_page(site, user_chrome, tmp_path):
    from tpmap.cli import main
    endpoint, _ = user_chrome
    _open_tab(endpoint, f"{site}/tp/scheme-c.html")
    rc = main(["fetch", "--current", "-o", str(tmp_path), "--wait", "2.5",
               "--cdp", endpoint])
    assert rc == 0
    assert list(tmp_path.glob("*.kml"))


def test_current_without_a_browser_is_rejected(tmp_path):
    from tpmap.cli import main
    assert main(["fetch", "--current", "-o", str(tmp_path)]) == 2


def test_list_tabs_reports_what_chrome_has(site, user_chrome):
    from tpmap.discover import list_tabs
    endpoint, _ = user_chrome
    _open_tab(endpoint, f"{site}/tp/scheme-c.html")
    urls = [t.get("url") for t in list_tabs(endpoint)]
    assert any("scheme-c.html" in (u or "") for u in urls)


def test_open_tab_then_read_it_back(site, user_chrome):
    from tpmap.discover import current_tab, open_tab
    endpoint, _ = user_chrome
    opened = open_tab(endpoint, f"{site}/tp/scheme-d.html")
    assert opened.endswith("scheme-d.html")
    assert current_tab(endpoint)[0].endswith("scheme-d.html")


def test_blank_browser_says_what_to_do(tmp_path_factory):
    """A window showing only chrome://newtab must not read as a broken tool."""
    from tpmap.discover import CdpUnreachable, current_tab
    exe = chromium_executable()
    if not exe:
        pytest.skip("no chromium available")
    port = _free_port()
    proc = subprocess.Popen(
        [exe, "--headless=new", f"--remote-debugging-port={port}", "--no-sandbox",
         f"--user-data-dir={tmp_path_factory.mktemp('blank')}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{endpoint}/json/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("chrome did not come up")

        with pytest.raises(CdpUnreachable) as err:
            current_tab(endpoint)
        msg = str(err.value)
        assert "nothing is loaded" in msg or "no tabs open" in msg
        assert "tpmap browser" in msg or "Open the scheme" in msg
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_normalise_target_rejects_a_pasted_placeholder():
    """A placeholder copied verbatim must not silently open about:blank."""
    from tpmap.discover import normalise_target
    for bad in ["<url it printed>", "<scheme url>", ""]:
        with pytest.raises(ValueError):
            normalise_target(bad)


def test_normalise_target_accepts_what_people_paste():
    from tpmap.discover import normalise_target
    assert normalise_target("https://townplanmap.com/tp/x") == "https://townplanmap.com/tp/x"
    assert normalise_target("townplanmap.com/tp/x") == "https://townplanmap.com/tp/x"
    assert normalise_target('  "https://townplanmap.com"  ') == "https://townplanmap.com"
    # loopback is not TLS
    assert normalise_target("127.0.0.1:8000/a").startswith("http://")
    # a host:port must not be mistaken for a scheme
    assert normalise_target("localhost:8000/a") == "http://localhost:8000/a"
    assert normalise_target("townplanmap.com:8443/x") == "https://townplanmap.com:8443/x"


def test_normalise_target_rejects_non_http_schemes():
    from tpmap.discover import normalise_target
    for bad in ["file:///etc/passwd", "ftp://x/y", "javascript:alert(1)",
                "mailto:a@b.c", "data:text/html,x"]:
        with pytest.raises(ValueError):
            normalise_target(bad)


def test_open_tab_raises_on_an_unloadable_url(site, user_chrome):
    from tpmap.discover import open_tab
    endpoint, _ = user_chrome
    with pytest.raises(ValueError):
        open_tab(endpoint, "<url it printed>")
    # and a good one still works
    assert open_tab(endpoint, f"{site}/tp/scheme-c.html").endswith("scheme-c.html")
