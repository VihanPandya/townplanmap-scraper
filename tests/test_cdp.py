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
