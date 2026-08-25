"""A Chrome the tool starts and keeps for itself.

Attaching over CDP normally means the user must quit Chrome entirely and
relaunch it with --remote-debugging-port, because Chrome silently ignores that
flag when an instance is already running. That is a poor thing to ask of anyone
with tabs open, and it is the step people get wrong.

Instead, run a second Chrome against a profile directory of our own. It does not
touch the everyday browser, it is launched as a plain subprocess rather than
under automation (so reCAPTCHA and SMS logins behave normally), and because the
profile persists, a sign-in survives between runs -- IndexedDB included, which a
saved session file could never carry.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("tpmap.browser")

DEFAULT_PORT = 9222

WINDOWS_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"~\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
MACOS_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
LINUX_BINARIES = ["google-chrome", "google-chrome-stable", "chromium",
                  "chromium-browser", "microsoft-edge"]


def state_dir() -> Path:
    return Path(os.environ.get("TPMAP_HOME", Path.home() / ".tpmap"))


def default_profile_dir() -> Path:
    return state_dir() / "chrome-profile"


def _state_file() -> Path:
    return state_dir() / "browser.json"


def find_chrome() -> str | None:
    """Locate a Chrome-family browser to drive."""
    explicit = os.environ.get("TPMAP_CHROME") or os.environ.get("TPMAP_CHROMIUM")
    if explicit and Path(explicit).exists():
        return explicit

    if sys.platform.startswith("win"):
        candidates = WINDOWS_CANDIDATES
    elif sys.platform == "darwin":
        candidates = MACOS_CANDIDATES
    else:
        candidates = []
        for name in LINUX_BINARIES:
            found = shutil.which(name)
            if found:
                return found

    for path in candidates:
        expanded = Path(os.path.expanduser(path))
        if expanded.exists():
            return str(expanded)

    for name in LINUX_BINARIES + ["chrome", "chrome.exe", "msedge"]:
        found = shutil.which(name)
        if found:
            return found
    return None


def port_is_live(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """True if something answers the CDP version endpoint on this port."""
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/json/version", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _remembered_port() -> int | None:
    try:
        data = json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return None
    port = data.get("port")
    return int(port) if isinstance(port, int) else None


def _remember(port: int, profile: Path, exe: str) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    try:
        _state_file().write_text(json.dumps(
            {"port": port, "profile": str(profile), "executable": exe}, indent=2))
    except OSError as exc:
        log.debug("could not write browser state: %s", exc)


def launch(profile_dir=None, port=None, *, executable=None, headless=False,
           wait_seconds=30) -> tuple[int, str]:
    """Start our Chrome and wait for its debugging port. Returns (port, exe)."""
    exe = executable or find_chrome()
    if not exe:
        raise RuntimeError(
            "no Chrome or Edge found. Install Google Chrome, or point at one with "
            "TPMAP_CHROME=/path/to/chrome")

    profile = Path(profile_dir or default_profile_dir())
    profile.mkdir(parents=True, exist_ok=True)

    if os.environ.get("TPMAP_BROWSER_HEADLESS", "").lower() in ("1", "true", "yes"):
        headless = True     # for servers and CI, where there is no display

    chosen = port or _remembered_port() or DEFAULT_PORT
    if port_is_live(chosen):
        log.info("reusing the browser already listening on %d", chosen)
        _remember(chosen, profile, exe)
        return chosen, exe
    # DEFAULT_PORT may be taken by something else entirely.
    if chosen == DEFAULT_PORT and not port:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", chosen)) == 0:
                chosen = _free_port()

    args = [
        exe,
        f"--remote-debugging-port={chosen}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]
    if headless:
        args.append("--headless=new")
        args.append("--no-sandbox")

    log.info("starting %s on port %d (profile %s)", exe, chosen, profile)
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=creationflags)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if port_is_live(chosen):
            _remember(chosen, profile, exe)
            return chosen, exe
        time.sleep(0.4)

    raise RuntimeError(
        f"started {exe} but nothing answered on port {chosen} within "
        f"{wait_seconds}s. Try running it by hand:\n"
        f'  "{exe}" --remote-debugging-port={chosen} --user-data-dir="{profile}"')


def endpoint(profile_dir=None, port=None, *, executable=None,
             headless=False) -> str:
    """A CDP URL for our managed browser, starting it if it is not up."""
    chosen, _ = launch(profile_dir, port, executable=executable, headless=headless)
    return f"http://127.0.0.1:{chosen}"
