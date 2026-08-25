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
    assert any("could not start a browser" in e for e in report.errors)
