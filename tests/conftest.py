"""Shared fixtures: a throwaway static server for the fixture site."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)                      # for fixture_server
sys.path.insert(0, os.path.dirname(ROOT))     # for the tpmap package

from fixture_server import start               # noqa: E402


@pytest.fixture(scope="session")
def site():
    """Serve tests/fixture_site on an ephemeral port; yields the base URL."""
    base, httpd = start(0)
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="session")
def browser_ok():
    """Skip browser-dependent tests when no Chromium is usable."""
    try:
        from playwright.sync_api import sync_playwright

        from tpmap.discover import chromium_executable
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            kwargs = {"headless": True}
            exe = chromium_executable()
            if exe:
                kwargs["executable_path"] = exe
            pw.chromium.launch(**kwargs).close()
        return True
    except Exception:
        return False
