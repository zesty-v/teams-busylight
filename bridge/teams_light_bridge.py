#!/usr/bin/env python3
"""
Teams busy-light bridge.

Watches Microsoft Teams for busy/free state, adds keyboard/mouse idle
detection, and pushes the combined state to an ESP8266 light over the LAN:

    busy  -> red    (in a Teams meeting/call, or presence Busy/DND)
    idle  -> orange (no keyboard/mouse input for IDLE_AFTER_SECONDS)
    free  -> green

Two Teams sources, picked automatically at startup:

  * websocket — the local "third-party app API" (ws://localhost:8124),
    used when Teams exposes it. Needs one-time pairing: enable Teams
    Settings > Privacy > Third-party app API, join a meeting, approve the
    popup. The token is stored in teams_token.txt next to this script.
  * log tail — new Teams writes its presence to its log files
    (SetTaskbarIconOverlay ... status <X>). Used when port 8124 is closed,
    e.g. when Microsoft's server config disables the API. No pairing
    needed; the light then follows presence, so a manual "Do not disturb"
    also turns it red.

Run this on the Windows machine that runs Teams (NOT inside WSL):

    pip install websockets   (only needed for the websocket source)
    python teams_light_bridge.py
"""

import asyncio
import ctypes
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# ------------------------- configuration -------------------------
ESP_HOST = "192.168.2.4"        # IP address of the ESP8266 light
IDLE_AFTER_SECONDS = 300        # no input for this long -> orange
HEARTBEAT_SECONDS = 10          # push at least this often (ESP watchdog relies on it)
RECONNECT_MAX_SECONDS = 60      # cap for websocket reconnect backoff
LOG_POLL_SECONDS = 1.0          # how often the log tailer looks for new lines

APP_IDENTITY = {
    "protocol-version": "2.0.0",
    "manufacturer": "SytzeLabs",
    "device": "BusyLight",
    "app": "TeamsRobot",
    "app-version": "1.0",
}
TOKEN_FILE = Path(__file__).with_name("teams_token.txt")
BRIDGE_LOG_FILE = Path(__file__).with_name("bridge.log")
# ------------------------------------------------------------------

log = logging.getLogger("bridge")

teams_busy = False
pair_request_sent = False


def load_token() -> str:
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def save_token(token: str) -> None:
    TOKEN_FILE.write_text(token)
    log.info("Stored new Teams token in %s", TOKEN_FILE.name)


if sys.platform == "win32":
    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    def idle_seconds() -> float:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
        ticks = ctypes.windll.kernel32.GetTickCount()
        return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0
else:
    def idle_seconds() -> float:
        # No idle detection off Windows: the light just never turns orange.
        return 0.0


def current_state() -> str:
    if teams_busy:
        return "busy"
    if idle_seconds() >= IDLE_AFTER_SECONDS:
        return "idle"
    return "free"


def _push(state: str) -> None:
    url = f"http://{ESP_HOST}/set?state={state}"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            resp.read()
    except OSError as exc:
        log.warning("Could not reach light at %s: %s", ESP_HOST, exc)


async def push_state(state: str) -> None:
    await asyncio.to_thread(_push, state)


async def set_teams_busy(busy: bool, reason: str) -> None:
    global teams_busy
    if busy != teams_busy:
        teams_busy = busy
        log.info("Teams: %s (%s)", "busy" if busy else "not busy", reason)
        await push_state(current_state())


async def heartbeat() -> None:
    last = None
    while True:
        state = current_state()
        if state != last:
            log.info("State -> %s", state)
            last = state
        await push_state(state)
        await asyncio.sleep(HEARTBEAT_SECONDS)


# ----------------- source 1: Teams log tailing --------------------
#
# New Teams logs its taskbar presence overlay, e.g.:
#   ... TaskbarService: SetTaskbarIconOverlay overlay description:No items, status Available
# The status token is the user's presence. Undocumented but stable across
# recent builds; if a Teams update ever changes it, the bridge simply
# stops seeing matches and the light shows free/idle instead of stale red.

# The status is human-readable and may contain spaces ("Do not disturb",
# "In a call"), so capture to end of line and classify on a normalized
# (lowercased, letters-only) form.
STATUS_RE = re.compile(r"SetTaskbarIconOverlay.*status (.+?)\s*$")

BUSY_STATUSES = {
    "busy", "busyidle", "donotdisturb", "donotdisturbidle",
    "inacall", "inaconferencecall", "inameeting", "onthephone",
    "presenting",
}


def is_busy_status(status: str) -> bool:
    return re.sub(r"[^a-z]", "", status.lower()) in BUSY_STATUSES


def teams_log_dir() -> Path:
    return (Path(os.environ.get("LOCALAPPDATA", ""))
            / "Packages" / "MSTeams_8wekyb3d8bbwe"
            / "LocalCache" / "Microsoft" / "MSTeams" / "Logs")


def parse_status(line: str) -> str | None:
    m = STATUS_RE.search(line)
    return m.group(1) if m else None


def newest_log(log_dir: Path) -> Path | None:
    try:
        files = list(log_dir.glob("MSTeams_*.log"))
        return max(files, key=lambda p: p.stat().st_mtime) if files else None
    except OSError:
        return None


def last_status_in_file(path: Path) -> str | None:
    status = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                status = parse_status(line) or status
    except OSError:
        pass
    return status


def initial_status(log_dir: Path, max_files: int = 3) -> str | None:
    """Most recent presence in the newest few log files (newest first)."""
    try:
        files = sorted(log_dir.glob("MSTeams_*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files[:max_files]:
        status = last_status_in_file(path)
        if status:
            return status
    return None


async def teams_log_listener() -> None:
    log_dir = teams_log_dir()
    log.info("Watching Teams logs in %s", log_dir)

    status = initial_status(log_dir)
    if status:
        log.info("Teams presence at startup: %s", status)
        await set_teams_busy(is_busy_status(status), f"presence {status}")
    else:
        log.info("No Teams presence found yet (is Teams running?)")

    current = None
    f = None
    try:
        while True:
            newest = await asyncio.to_thread(newest_log, log_dir)
            if newest is not None and newest != current:
                if f:
                    f.close()
                f = open(newest, encoding="utf-8", errors="replace")
                if current is None:
                    f.seek(0, os.SEEK_END)  # startup: history handled above
                else:
                    log.info("Teams log rotated -> %s", newest.name)
                current = newest

            status = None
            while f:
                pos = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    f.seek(pos)  # partial line still being written
                    break
                status = parse_status(line) or status
            if status:
                await set_teams_busy(is_busy_status(status),
                                     f"presence {status}")
            await asyncio.sleep(LOG_POLL_SECONDS)
    finally:
        if f:
            f.close()


# ------------- source 2: Teams third-party app API ----------------

def teams_api_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8124), timeout=1):
            return True
    except OSError:
        return False


async def handle_message(ws, raw: str) -> None:
    global pair_request_sent
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("Ignoring non-JSON message: %r", raw)
        return

    if "tokenRefresh" in msg:
        save_token(msg["tokenRefresh"])

    update = msg.get("meetingUpdate")
    if not update:
        return

    state = update.get("meetingState") or {}
    if "isInMeeting" in state:
        await set_teams_busy(bool(state["isInMeeting"]), "meeting state")

    perms = update.get("meetingPermissions") or {}
    if perms.get("canPair") and not load_token() and not pair_request_sent:
        # The first command triggers the "Allow BusyLight?" popup in Teams;
        # approving it makes Teams send us a token via tokenRefresh.
        pair_request_sent = True
        log.info("Pairing: check Teams for the approval popup")
        await ws.send(json.dumps(
            {"action": "query-meeting-state", "parameters": {}, "requestId": 1}))


async def teams_listener() -> None:
    import websockets

    backoff = 5
    while True:
        params = dict(APP_IDENTITY)
        token = load_token()
        if token:
            params["token"] = token
        url = "ws://localhost:8124?" + urllib.parse.urlencode(params)
        try:
            async with websockets.connect(url) as ws:
                log.info("Connected to Teams local API")
                backoff = 5
                async for raw in ws:
                    await handle_message(ws, raw)
        except Exception as exc:
            log.warning("Teams connection lost (%s) - retrying in %ss", exc, backoff)
        # With Teams unreachable we can't know the call state; report not-in-call
        # rather than freezing the light on a stale red.
        await set_teams_busy(False, "Teams API unreachable")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)


# ------------------------------------------------------------------

def setup_logging() -> None:
    if sys.stderr is None:
        # Running under pythonw.exe: no console, log to a file instead.
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            BRIDGE_LOG_FILE, maxBytes=512 * 1024, backupCount=1,
            encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


async def main() -> None:
    setup_logging()
    log.info("Bridge starting: light at %s, idle threshold %ss",
             ESP_HOST, IDLE_AFTER_SECONDS)
    if teams_api_available():
        log.info("Teams source: third-party app API (ws://localhost:8124)")
        source = teams_listener()
    else:
        log.info("Teams source: log tailing (third-party API not available)")
        source = teams_log_listener()
    await asyncio.gather(heartbeat(), source)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
