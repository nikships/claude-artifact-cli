"""Keep the installed CLI current.

Same shape as muse-cli's update check: look at most once a day, in a
background thread with a short timeout, never in CI, never fail or slow the
command. On top of that it upgrades: a uv tool or pipx install is upgraded in
a detached process once the command has finished. A pip install only gets the
notice, because that environment belongs to someone else.

    CLAUDE_ARTIFACT_NO_AUTO_UPDATE=1   notice only, never upgrade
    CLAUDE_ARTIFACT_NO_UPDATE_CHECK=1  no check at all
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from . import __version__

PACKAGE = "claude-artifact-cli"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_INTERVAL = 24 * 60 * 60
FAIL_BACKOFF = 60 * 60
FETCH_TIMEOUT = 2
JOIN_TIMEOUT = 1.5


def _config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, PACKAGE)


STATE_FILE = os.path.join(_config_dir(), "update.json")
LOG_FILE = os.path.join(_config_dir(), "update.log")


def version_key(value) -> tuple[int, ...] | None:
    """Stable x.y.z as a tuple. Pre-releases and junk compare as absent."""
    text = str(value or "").strip().lstrip("v").split("+", 1)[0]
    if not text or any(c.isalpha() for c in text):
        return None
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def is_newer(latest, current) -> bool:
    new, old = version_key(latest), version_key(current)
    return bool(new and old and new > old)


def install_kind() -> str:
    """How this copy was installed: "uv", "pipx" or "pip"."""
    prefix = os.path.realpath(sys.prefix)
    sep = os.sep
    if f"{sep}uv{sep}tools{sep}" in prefix:
        return "uv"
    if f"{sep}pipx{sep}" in prefix:
        return "pipx"
    return "pip"


def upgrade_argv() -> list[str]:
    kind = install_kind()
    if kind == "uv":
        return ["uv", "tool", "upgrade", PACKAGE]
    if kind == "pipx":
        return ["pipx", "upgrade", PACKAGE]
    return [sys.executable, "-m", "pip", "install", "-U", PACKAGE]


def upgrade_command() -> str:
    return " ".join(upgrade_argv())


def _enabled() -> bool:
    return not (os.environ.get("CLAUDE_ARTIFACT_NO_UPDATE_CHECK") or os.environ.get("CI"))


def _auto_enabled() -> bool:
    return not os.environ.get("CLAUDE_ARTIFACT_NO_AUTO_UPDATE") and install_kind() != "pip"


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def _fetch_latest() -> str:
    req = urllib.request.Request(
        PYPI_URL,
        headers={"Accept": "application/json", "User-Agent": f"{PACKAGE}/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return str(payload["info"]["version"])


def start_check() -> dict | None:
    """Begin a check, or use the cached answer. None when checks are off."""
    if not _enabled():
        return None
    state = _load_state()
    latest = state.get("latest") or ""
    checked = float(state.get("checked_at") or 0)
    holder: dict = {"latest": latest or None, "thread": None, "state": state}
    if latest and time.time() - checked < CHECK_INTERVAL:
        return holder

    def work() -> None:
        try:
            found = _fetch_latest()
            holder["latest"] = found
            _save_state({**state, "checked_at": time.time(), "latest": found})
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            # Offline or a PyPI blip: try again in an hour, keep any known version.
            _save_state(
                {**state, "checked_at": time.time() - CHECK_INTERVAL + FAIL_BACKOFF, "latest": latest}
            )

    thread = threading.Thread(target=work, daemon=True)
    holder["thread"] = thread
    thread.start()
    return holder


def finish_check(holder: dict | None, quiet: bool = False) -> None:
    """After the command: upgrade in the background, or print the notice."""
    if not holder:
        return
    thread = holder.get("thread")
    if thread is not None:
        thread.join(timeout=JOIN_TIMEOUT)
    latest = holder.get("latest")
    if not is_newer(latest, __version__):
        return
    if _auto_enabled():
        if _start_upgrade(latest) and not quiet:
            print(
                f"\n{PACKAGE} {__version__} → {latest}: updating in the background "
                f"(log: {LOG_FILE})",
                file=sys.stderr,
            )
        return
    if not quiet:
        print(
            f"\nA new release of {PACKAGE} is available: {__version__} → {latest}\n"
            f"To upgrade, run: claude-artifact update",
            file=sys.stderr,
        )


def _start_upgrade(latest: str) -> bool:
    """Start one detached upgrade per release per day. True if one started."""
    state = _load_state()
    if (
        state.get("upgrade_to") == latest
        and time.time() - float(state.get("upgrade_started_at") or 0) < CHECK_INTERVAL
    ):
        return False
    argv = upgrade_argv()
    if shutil.which(argv[0]) is None:
        return False
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {' '.join(argv)}\n")
            log.flush()
            detach = (
                {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}
            )
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **detach
            )
    except OSError:
        return False
    _save_state({**state, "upgrade_to": latest, "upgrade_started_at": time.time()})
    return True


def cmd_update(args) -> int:
    """Upgrade now, in the foreground."""
    argv = upgrade_argv()
    if argv[0] != sys.executable and shutil.which(argv[0]) is None:
        print(f"{argv[0]} is not on PATH. To upgrade, run: {upgrade_command()}", file=sys.stderr)
        return 1
    print(f"running: {upgrade_command()}", file=sys.stderr)
    return subprocess.call(argv)
