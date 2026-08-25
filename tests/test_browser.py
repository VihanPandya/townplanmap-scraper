"""The Chrome tpmap starts and manages for itself."""

import os
import signal
import subprocess
import time

import pytest

from tpmap import browser as B


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TPMAP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TPMAP_BROWSER_HEADLESS", "1")
    return tmp_path


def _chrome():
    exe = B.find_chrome()
    if not exe:
        pytest.skip("no chrome-family browser available")
    return exe


def _kill(profile: str):
    try:
        out = subprocess.run(["pgrep", "-f", f"user-data-dir={profile}"],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        return
    for pid in [p for p in out.split() if p.isdigit()]:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass


def test_explicit_executable_wins(monkeypatch, tmp_path):
    fake = tmp_path / "my-chrome"
    fake.write_text("")
    monkeypatch.setenv("TPMAP_CHROME", str(fake))
    assert B.find_chrome() == str(fake)


def test_a_nonexistent_override_is_ignored(monkeypatch):
    monkeypatch.setenv("TPMAP_CHROME", "/no/such/chrome")
    monkeypatch.delenv("TPMAP_CHROMIUM", raising=False)
    assert B.find_chrome() != "/no/such/chrome"


def test_paths_follow_tpmap_home(home):
    assert str(home / "home") in str(B.default_profile_dir())


def test_port_is_live_is_false_for_a_dead_port():
    assert not B.port_is_live(1)


def test_launch_then_reuse(home):
    """A second call must attach to the running browser, not start another."""
    _chrome()
    profile = str(home / "home" / "chrome-profile")
    try:
        port, exe = B.launch(wait_seconds=45)
        assert B.port_is_live(port)

        started = subprocess.run(["pgrep", "-fc", f"user-data-dir={profile}"],
                                 capture_output=True, text=True).stdout.strip()
        port2, _ = B.launch(wait_seconds=45)
        assert port2 == port
        again = subprocess.run(["pgrep", "-fc", f"user-data-dir={profile}"],
                               capture_output=True, text=True).stdout.strip()
        assert again == started, "a second browser was started"

        assert B.endpoint() == f"http://127.0.0.1:{port}"
    finally:
        _kill(profile)
        time.sleep(0.5)


def test_the_port_is_remembered_between_runs(home):
    _chrome()
    profile = str(home / "home" / "chrome-profile")
    try:
        port, _ = B.launch(wait_seconds=45)
        import json
        state = json.loads((home / "home" / "browser.json").read_text())
        assert state["port"] == port
        assert state["profile"].endswith("chrome-profile")
    finally:
        _kill(profile)
        time.sleep(0.5)


def test_a_missing_browser_is_reported_clearly(home, monkeypatch):
    monkeypatch.setattr(B, "find_chrome", lambda: None)
    with pytest.raises(RuntimeError) as err:
        B.launch(wait_seconds=1)
    assert "TPMAP_CHROME" in str(err.value)
