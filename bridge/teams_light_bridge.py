#!/usr/bin/env python3
"""
Teams busy-light bridge.

Watches Microsoft Teams for busy/free state, adds keyboard/mouse idle
detection, and pushes the combined state to an ESP8266 light over the LAN:

    busy  -> red    (on a real call — Teams/WhatsApp holding the mic —
                     or presence explicitly Do not disturb)
    idle  -> orange (no keyboard/mouse input for IDLE_AFTER_SECONDS)
    free  -> green  (calendar "Busy"/"In a meeting" deliberately stays
                     green: it outlives meetings that finish early)

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
MIC_POLL_SECONDS = 2.0          # how often to check microphone use

# Packaged (MSIX) apps whose microphone use means "on a call". Windows
# tracks per-app mic use in the ConsentStore registry key; while an app
# holds the mic its LastUsedTimeStop is 0. Muting inside the app doesn't
# release the mic, so a muted call still counts as busy.
MIC_APPS = {
    "MSTeams_8wekyb3d8bbwe": "Teams",
    "5319275A.WhatsAppDesktop_cv1g1gvanyjgm": "WhatsApp",
}
MIC_CONSENT_KEY = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                   r"\CapabilityAccessManager\ConsentStore\microphone")

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

teams_busy = False              # presence says DND / in a call
mic_busy = False                # a monitored app is holding the microphone
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
    if teams_busy or mic_busy:
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


async def set_mic_busy(busy: bool, reason: str) -> None:
    global mic_busy
    if busy != mic_busy:
        mic_busy = busy
        log.info("Microphone: %s", reason)
        await push_state(current_state())


def mic_in_use_apps() -> list[str]:
    """Names of monitored apps currently holding the microphone."""
    import winreg  # Windows-only, imported lazily

    active = []
    for package, name in MIC_APPS.items():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                MIC_CONSENT_KEY + "\\" + package) as key:
                stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
        except OSError:
            continue  # app not installed / never used the mic
        if stop == 0:
            active.append(name)
    return active


async def mic_watcher() -> None:
    if sys.platform != "win32":
        log.info("Mic watcher disabled (not Windows)")
        return
    log.info("Watching microphone use by: %s", ", ".join(MIC_APPS.values()))
    while True:
        apps = await asyncio.to_thread(mic_in_use_apps)
        if apps:
            await set_mic_busy(True, "in use by " + ", ".join(apps))
        else:
            await set_mic_busy(False, "released")
        await asyncio.sleep(MIC_POLL_SECONDS)


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

# Primary signal (new Teams, still present after web-client updates that
# broke the taskbar-overlay format in Aug 2026): the native module logs
# every presence change per signed-in cloud/account, e.g.
#   ... UserDataCrossCloudModule: Received Action: UserPresenceAction:
#       {cloud_context: https://teams.microsoft.com, availability: Busy}
PRESENCE_ACTION_RE = re.compile(
    r"UserPresenceAction: \{cloud_context: ([^,]+), availability: ([A-Za-z]+)")

# Legacy signal (older web client builds): taskbar overlay with a
# human-readable status, possibly multi-word ("Do not disturb").
OVERLAY_RE = re.compile(r"SetTaskbarIconOverlay.*status (.+?)\s*$")

# Presence that turns the light red: explicit Do-not-disturb plus
# unambiguous in-a-call statuses. Deliberately NOT "busy"/"in a meeting":
# those are calendar-driven, so they keep the light red long after a
# meeting that finished early — actual calls are caught by the mic
# watcher instead.
BUSY_STATUSES = {
    "donotdisturb", "donotdisturbidle",
    "inacall", "inaconferencecall", "onthephone", "presenting",
}


def is_busy_status(status: str) -> bool:
    return re.sub(r"[^a-z]", "", status.lower()) in BUSY_STATUSES


def parse_presence(line: str) -> tuple[str, str] | None:
    """Return (cloud, status) for a presence log line, else None.

    Teams can be signed in to several accounts (work + personal); each
    reports presence for its own cloud_context. Legacy overlay lines
    carry no cloud and get the pseudo-cloud "taskbar-overlay".
    """
    m = PRESENCE_ACTION_RE.search(line)
    if m:
        return m.group(1), m.group(2)
    m = OVERLAY_RE.search(line)
    if m:
        return "taskbar-overlay", m.group(1)
    return None


def any_busy(clouds: dict[str, str]) -> bool:
    """Busy if any signed-in account reports a busy-ish presence."""
    return any(is_busy_status(status) for status in clouds.values())


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


def scan_file(path: Path, clouds: dict[str, str]) -> None:
    """Feed every presence line of a log file into the per-cloud state."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = parse_presence(line)
                if parsed:
                    clouds[parsed[0]] = parsed[1]
    except OSError:
        pass


def initial_clouds(log_dir: Path, max_files: int = 3) -> dict[str, str]:
    """Replay the newest few log files (oldest first) into cloud state."""
    try:
        files = sorted(log_dir.glob("MSTeams_*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return {}
    clouds: dict[str, str] = {}
    for path in reversed(files[:max_files]):
        scan_file(path, clouds)
    return clouds


def describe(clouds: dict[str, str]) -> str:
    return ", ".join(
        f"{status} ({cloud.removeprefix('https://')})"
        for cloud, status in sorted(clouds.items())) or "none"


async def teams_log_listener() -> None:
    log_dir = teams_log_dir()
    log.info("Watching Teams logs in %s", log_dir)

    clouds = initial_clouds(log_dir)
    if clouds:
        log.info("Teams presence at startup: %s", describe(clouds))
        await set_teams_busy(any_busy(clouds),
                             f"presence {describe(clouds)}")
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

            changed = False
            while f:
                pos = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    f.seek(pos)  # partial line still being written
                    break
                parsed = parse_presence(line)
                if parsed:
                    clouds[parsed[0]] = parsed[1]
                    changed = True
            if changed:
                await set_teams_busy(any_busy(clouds),
                                     f"presence {describe(clouds)}")
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
    await asyncio.gather(heartbeat(), source, mic_watcher())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
