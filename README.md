# Teams Busy Light

A traffic light for your door: **green** = free, **orange** = idle/away,
**red** = in a Teams call. No Azure, no cloud — everything runs on your PC
and your LAN.

```
┌─────────────── Your PC ────────────────┐         LAN          ┌──── ESP8266 ────┐
│                                        │                      │                 │
│  Teams client ──ws:8124 / log tail──►  │                      │  Web server     │
│                    bridge script  ─────┼── HTTP GET ─────────►│  /set?state=... │
│  OS idle timer ──────────────►│        │  every change +      │       │         │
│                                        │  10 s heartbeat      │  GPIO ─┴─ G O R │
└────────────────────────────────────────┘                      └─────────────────┘
```

- `bridge/teams_light_bridge.py` — runs on the Windows machine that runs
  Teams. Watches Teams for busy/free state, checks the OS keyboard/mouse
  idle timer, and pushes `free | idle | busy` to the ESP8266. It has two
  Teams sources and picks one automatically at startup:
  - **Third-party app API websocket** (`ws://localhost:8124`) — used when
    Teams exposes it. Reports in-call state only. Needs one-time pairing.
  - **Log tailing** — used when port 8124 is closed (Microsoft gates the
    API behind a server-side ECS flag, `thirdPartyDevicesManagerEnabled`,
    which is off for many accounts and also hides the Settings toggle).
    The bridge then follows the presence Teams writes to its own log
    files (`SetTaskbarIconOverlay ... status <X>`): a call, "Busy" or
    "Do not disturb" all turn the light red. No pairing needed.
- `esp8266/teams_busylight/teams_busylight.ino` — Arduino sketch. Serves a
  tiny website showing the current state, accepts state updates, drives
  the GPIO pins. If no update arrives for 60 s it goes dark with a short
  blip every 3 s ("no signal") so it can never show a stale colour.

## Display modes

The lamp has two display modes, switchable from its web page (or via
`GET /mode?set=...`) and remembered across power cycles (EEPROM):

| Mode      | Behaviour                                                  |
|-----------|------------------------------------------------------------|
| `traffic` | green = free, orange = idle, red = busy (default)          |
| `single`  | red lamp only: **on** when busy, **off** when free or idle |

Single mode is for builds with just one red lamp — only the red pin needs
to be wired. The bridge always sends the full state; the ESP decides how
to display it, so switching modes never requires touching the PC side.
In single mode the "no signal" failsafe shows as a brief red blip every
3 s (clearly distinct from the solid red of a call).

## Hardware (ESP-01)

- ESP-01 module
- Green, orange/amber and red LEDs + one 220–330 Ω resistor each
- 2× 10 kΩ resistors (pull-ups for GPIO0 and GPIO2)
- Stable 3.3 V supply, 250 mA or better — the ESP-01 has **no onboard
  regulator**; 5 V will kill it. An AMS1117-3.3 module or an
  ESP-01 power adapter board works well.

The ESP-01 exposes only four GPIOs, and two of them (GPIO0, GPIO2) are
boot-strap pins that must read HIGH at power-on or the chip won't boot
into the sketch. The lamps are therefore wired **active-LOW**:

    3.3 V ── 220–330 Ω ── LED (cathode toward pin) ── GPIO pin

The sketch drives a pin LOW to switch its lamp ON.

| Colour | ESP-01 pin | Notes                                        |
|--------|------------|----------------------------------------------|
| Green  | GPIO3 (RX) | freed by TX-only serial in the sketch        |
| Orange | GPIO0      | add 10 kΩ from pin to 3.3 V                  |
| Red    | GPIO2      | add 10 kΩ from pin to 3.3 V                  |

Also wire **CH_PD (EN) to 3.3 V** — the module won't start without it.

For a **single-red-lamp build** (see display modes below) you only need
the red LED on GPIO2 plus its pull-up, and GPIO0/RX stay unconnected.

Expect a brief flicker of the lamps at power-on while the boot ROM reads
the strap pins — harmless. For anything bigger than a plain LED (12 V
lamp, LED strip) put a transistor or relay module in between, and pick
one that switches on a LOW input or invert `LAMPS_ACTIVE_LOW`.

Using a NodeMCU/Wemos board instead? Set the pin constants at the top of
the sketch to e.g. 5/4/14, set `LAMPS_ACTIVE_LOW = false`, and wire each
LED pin → resistor → LED → GND. No pull-ups needed.

## Relay board builds (ESP-01)

Snap-on ESP-01 relay boards come in two flavours that are driven
completely differently — check yours before configuring:

- **Transistor boards** (just the relay and a small transistor next to
  the ESP-01 socket): the relay input is **GPIO0**. This is the sketch
  default (`PIN_RED = 0`, `LAMPS_ACTIVE_LOW = false`). If the relay is
  ON at power-up and switches OFF when busy, your board is
  low-level-trigger: set `LAMPS_ACTIVE_LOW = true`.
- **LC Tech-style boards** (an extra small 8-pin logic chip on the
  board): the relay is switched by that chip, which listens on the ESP's
  serial line at 9600 baud — no GPIO involved. Set
  `LCTECH_SERIAL_RELAY = true`; the sketch then sends the chip's on/off
  commands (`A0 01 01 A2` / `A0 01 00 A1`) and disables serial debug
  output, since it shares the same line.

Other notes for relay builds:

- The blue LED on the ESP-01S module itself is on **GPIO2, active LOW** —
  it is *not* the relay input. A sketch toggling GPIO2 blinks that LED
  and leaves the relay alone.
- Keep `STALE_BLIP = false` on relay builds: the "no signal" blink
  pattern would click the relay every 3 seconds. With it off, losing the
  bridge signal simply releases the relay.
- Use `single` display mode (the default red-only mode) so the relay
  follows busy/free.

### Driving the relay board from a NodeMCU / Wemos D1 mini

The relay board doesn't care what feeds it — a NodeMCU can stand in for
the ESP-01 with three jumper wires into the board's empty ESP-01 socket
(module removed):

| NodeMCU pin      | Relay board                  | Purpose                  |
|------------------|------------------------------|--------------------------|
| `Vin`            | 5 V input (`+`/`DC5V`)       | powers relay + regulator |
| `GND`            | GND input (`-`)              | common ground            |
| `D4` (GPIO2)     | ESP-01 **socket, TX hole**   | commands to relay chip   |

Male dupont jumpers press straight into the socket's female holes. `Vin`
carries the NodeMCU's USB 5 V (minus a diode drop, ~4.7 V) — enough for
the relay coil; if the relay ever chatters or drops out, feed the board
from its own 5 V supply instead and keep only GND + D4 to the NodeMCU.

**Finding the TX hole** (socket pinout mirrors the ESP-01): the four
corner holes are GND, TX at one short end and RX, VCC at the other, with
GND diagonal to VCC and RX diagonal to TX. Anchor the orientation with a
multimeter: the GND hole has continuity to the power input's negative
terminal (and the corner diagonal to it reads 3.3 V when powered — that's
VCC). TX is then the *other* corner at GND's end of the socket.

Sketch config for this setup (the current defaults):
`LCTECH_SERIAL_RELAY = true`, `LCTECH_ON_SERIAL1 = true` — relay commands
go out on Serial1 (TX-only on D4), so unlike the ESP-01 build, USB serial
debug output works normally at 115200. For a **transistor-type** relay
board instead, wire e.g. `D1` to the socket's GPIO0 hole and set
`PIN_RED = 5`, `LCTECH_SERIAL_RELAY = false`.

Quirks: the relay may click once as the NodeMCU boots (the ESP8266 emits
boot-ROM noise on GPIO2 — the ESP-01 build has the same quirk on its TX
pin), and D4 doubles as the NodeMCU's onboard blue LED, so it flickers
faintly whenever a relay command is sent. Both are harmless.

Board settings: **NodeMCU 1.0 (ESP-12E Module)**, defaults are fine, and
uploads go over plain USB — no GPIO0-to-GND flash-mode dance needed.

## ESP8266 setup

1. Arduino IDE → install the ESP8266 board package
   (Boards Manager URL: `http://arduino.esp8266.com/stable/package_esp8266com_index.json`).
2. Copy `esp8266/teams_busylight/wifi_credentials.h.example` to
   `wifi_credentials.h` (same folder) and fill in your WiFi name and
   password — the file is gitignored, so your credentials stay out of
   the repository. Select board **Generic ESP8266 Module**,
   flash size 1 MB (older blue ESP-01s: 512 KB) — or **NodeMCU 1.0
   (ESP-12E Module)** with default settings for a NodeMCU build.
3. Flashing an ESP-01 needs a USB-serial adapter (3.3 V!): hold **GPIO0
   to GND while powering on** to enter flash mode, then upload. Adapters
   with a built-in programming switch do this for you. Disconnect the
   LEDs from GPIO0/RX while flashing if the upload is unreliable.
4. Power-cycle with GPIO0 released. The serial monitor (115200 baud)
   shows the IP address it got — note it and give the device a DHCP
   reservation in your router so it stays fixed. (Serial output still
   works: the sketch only gives up the RX direction.)
5. Browse to `http://<esp-ip>/` — you should see the status page saying
   NO SIGNAL, with a link to switch between traffic-light and
   single-red-lamp mode. Test the lamps manually:
   `http://<esp-ip>/set?state=busy` (red), `...=idle` (orange),
   `...=free` (green). After 60 s without updates it returns to NO SIGNAL.

## Bridge setup (on Windows, not WSL)

The bridge must run on the Windows side, because that is where Teams
exposes `localhost:8124` and where the idle timer lives.

1. Install Python 3.9+ from python.org.
2. Edit the configuration block at the top of
   `bridge/teams_light_bridge.py`: set `ESP_HOST` to the ESP's IP; adjust
   `IDLE_AFTER_SECONDS` (default 5 minutes) to taste.
3. Run `python teams_light_bridge.py`. It logs which Teams source it
   picked; with the log-tailing source there is nothing more to set up —
   change your Teams status (or join a call) and watch the light.
4. **Websocket source only** (if Teams exposes the API on your machine):
   `pip install websockets`, enable **Settings → Privacy → Third-party
   app API**, then start a "Meet now" meeting; Teams shows a popup asking
   to allow *BusyLight* — click **Allow**. The token is stored in
   `teams_token.txt` next to the script and reused forever after.

### Run it at startup

The repo lives in WSL but the bridge must run from a plain Windows
folder (`C:\Tools\teams-busylight`), because WSL paths aren't reliably
available at logon. From WSL:

```bash
mkdir -p /mnt/c/Tools/teams-busylight
bridge/sync-to-windows.sh          # copies the script + restarts the task
```

One-time task registration (creates scheduled task *TeamsBusyLight*:
runs `pythonw.exe` at logon, no console window, restarts on failure,
never auto-killed):

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass \
    -File '\\wsl.localhost\Ubuntu-24.04\var\www\teams-robot\bridge\register-task.ps1'
```

Because `pythonw.exe` has no console, the bridge writes its log to
`C:\Tools\teams-busylight\bridge.log` (rotated at 512 KB). After editing
the bridge in the repo, run `bridge/sync-to-windows.sh` again.

## Troubleshooting

- **Light stuck on NO SIGNAL** — the bridge isn't reaching the ESP: check
  it's running, check `ESP_HOST`, and try `http://<esp-ip>/set?state=free`
  from a browser on the PC.
- **Light never turns red (log-tailing source)** — check
  `bridge.log`: if the presence lines stopped matching after a Teams
  update (its web client updates itself silently, independent of the
  app version), the log format may have changed; grep the newest
  `%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache\Microsoft\MSTeams\Logs\MSTeams_*.log`
  for `availability` / `status ` and adjust `PRESENCE_ACTION_RE`,
  `OVERLAY_RE` or `BUSY_STATUSES`. This happened once already: an
  Aug 2026 web-client update dropped the status from the
  `SetTaskbarIconOverlay` lines; the bridge now primarily parses
  `UserPresenceAction: {cloud_context: ..., availability: ...}`, which
  is also per-account — busy on any signed-in account wins.
- **Briefly red right after the bridge starts** — the startup scan can
  pick up a stale presence from log history; Teams re-broadcasts presence
  within a minute or two and the light self-corrects.
- **"Third-party app API" missing from Teams Settings → Privacy** — the
  API (and its settings entry) is gated by a Microsoft server-side flag
  (`thirdPartyDevicesManagerEnabled` via ECS). Nothing to fix locally;
  the bridge falls back to log tailing automatically.
- **Bridge logs "Teams connection lost" repeatedly (websocket source)** —
  Teams isn't running, or the third-party app API toggle is off, or an
  update reset it; recheck Settings → Privacy.
- **No pairing popup appeared (websocket source)** — the popup only shows
  while you are in a meeting. Delete `bridge/teams_token.txt`, restart the
  bridge, and join a meeting again.
- **Red works but orange never shows** — `IDLE_AFTER_SECONDS` may be
  longer than you think, or the bridge is running somewhere that isn't
  Windows (idle detection is Windows-only).
- **The ESP-01's blue LED follows busy state but the relay never
  switches** — the sketch is driving GPIO2 (the LED) while the relay
  listens on GPIO0 or on serial. See "Relay board builds" above.

## Notes

- With the websocket source, calls answered on your phone won't trigger
  the light — the local API only sees the desktop client. The log-tailing
  source follows presence, which usually does reflect mobile calls.
- Both Teams sources are community-documented rather than officially
  published; if a Teams update ever changes the format, the failsafe
  means the light goes to NO SIGNAL (or free) rather than lying.
- The repo layout: `bridge/` (Windows-side Python bridge, unit tests,
  deployment scripts), `esp8266/` (Arduino sketch),
  `docs/superpowers/specs/` (design notes, including the investigation
  into why the third-party app API is blocked for many accounts).

## Development

Run the bridge's unit tests (parsing and log-file selection are pure
Python and run anywhere, including WSL/Linux):

```bash
cd bridge && python3 -m unittest test_bridge -v
```

## License

[MIT](LICENSE)
