# Presence-log bridge + autostart service — design

Date: 2026-08-07
Status: approved by Sytze

## Problem

The bridge was built on the Teams local third-party app API
(`ws://localhost:8124`). On Sytze's machine Microsoft's server-delivered
ECS config has `thirdPartyDevicesManagerEnabled: false`, so new Teams
(26198.304.4946.9672) never starts that API server and hides the
Settings → Privacy → Third-party app API section. Port 8124 is closed;
pairing can never happen. This is server-side and not fixable locally.

## Decision

Add a second, presence-based source: tail new Teams' own log files for
`SetTaskbarIconOverlay ... status <X>` lines (verified present on this
build). Keep the websocket code; the bridge probes port 8124 once at
startup and picks the websocket source if it ever becomes available,
otherwise the log tailer.

## Bridge changes (`bridge/teams_light_bridge.py`)

- Global `in_meeting` becomes `teams_busy`, set by whichever source runs.
- Log tailer:
  - Directory: `%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache\Microsoft\MSTeams\Logs`, newest `MSTeams_*.log` by mtime.
  - Regex `SetTaskbarIconOverlay.*status (\w+)`.
  - Busy statuses: Busy, BusyIdle, DoNotDisturb, DoNotDisturbIdle,
    InACall, InAConferenceCall, InAMeeting, OnThePhone, Presenting.
    Everything else (Available, Away, BeRightBack, Offline, Unknown…) → not busy.
  - On startup, scan the few newest log files for the most recent status
    so the light is right immediately.
  - Poll ~1 s; on each poll re-check which file is newest (Teams rotates
    logs on restart) and switch to it.
- Idle detection, 10 s heartbeat, ESP push protocol: unchanged.
- When running under `pythonw.exe` (no console, `sys.stderr is None`),
  log to a rotating `bridge.log` next to the script instead.

## Service setup (agreed earlier)

- A real Windows service is wrong here: session 0 can't see
  `GetLastInputInfo` idle state. Use Task Scheduler at logon instead.
- Script copied to `C:\Tools\teams-busylight\` (WSL repo stays source of
  truth; `bridge/sync-to-windows.sh` re-copies after edits).
- Scheduled task "TeamsBusyLight": at logon, `pythonw.exe
  C:\Tools\teams-busylight\teams_light_bridge.py`, restart on failure.

## Error handling

- Teams log dir missing / no status lines yet → treated as not busy;
  bridge keeps polling. ESP-side 60 s failsafe still covers a dead bridge.
- Log format change after a Teams update → no more matches → light shows
  free/idle rather than stale red; documented in README troubleshooting.

## Testing

- Pure parsing/file-selection helpers unit-tested (runnable in WSL).
- Live: run bridge on Windows, flip presence (DND / Meet now), verify via
  the ESP status page at http://192.168.2.26/.
