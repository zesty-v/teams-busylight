// Teams busy light — ESP8266, currently configured for a NodeMCU driving
// an LC Tech ESP-01 relay board (works on any ESP8266 board; see the pin
// notes below and in README.md)
//
// HTTP endpoints:
//   GET /                          status page (auto-refreshes, mode toggle)
//   GET /set?state=free|idle|busy  called by the PC bridge
//   GET /mode?set=traffic|single   switch display mode (persisted in EEPROM)
//   GET /status                    JSON state
//
// Modes:
//   traffic — green = free, orange = idle, red = busy
//   single  — red lamp only: on when busy, off when free/idle
//
// Failsafe: if no update arrives for STALE_AFTER_MS (PC asleep, Teams
// closed, bridge crashed) the lamps go dark and give a short blip every
// 3 s (orange in traffic mode, red in single mode) to say "no data" —
// the light never shows a stale colour.

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <EEPROM.h>

// WiFi credentials live in wifi_credentials.h (gitignored) — copy
// wifi_credentials.h.example next to this sketch and fill in your own.
#include "wifi_credentials.h"

// ---- output configuration -----------------------------------------------
const uint8_t PIN_UNUSED = 255;   // assign to skip a channel entirely

// Configured for an LC Tech ESP-01 relay board in single mode, driven
// from a NodeMCU wired into the board's ESP-01 socket (wiring in
// README.md). The common
// transistor-driven boards switch the relay with GPIO0. If your relay is
// ON at power-up and turns OFF when busy, it is a low-level-trigger board:
// flip LAMPS_ACTIVE_LOW to true.
//
// LC Tech-style boards (extra 8-pin logic chip next to the relay) ignore
// GPIOs entirely: their chip listens on the serial line at 9600 baud. Set
// LCTECH_SERIAL_RELAY = true for those; pin settings are then ignored and
// serial debug output is disabled (same line).
//
// Note: the blue LED on the ESP-01S module is on GPIO2 (active LOW) — it
// is NOT the relay input.
//
// Bare-LED traffic-light build (no relay board): GREEN=3, ORANGE=0, RED=2,
// LAMPS_ACTIVE_LOW=true, STALE_BLIP=true — wiring in README.md.
const uint8_t PIN_GREEN  = PIN_UNUSED;
const uint8_t PIN_ORANGE = PIN_UNUSED;
const uint8_t PIN_RED    = PIN_UNUSED;  // ignored: relay is serial-controlled
const bool LAMPS_ACTIVE_LOW = false;
const bool LCTECH_SERIAL_RELAY = true;  // LC Tech board: relay chip on serial

// Which serial port carries the LC Tech relay commands. On the ESP-01
// itself the relay chip is hard-wired to the module's TX pin, so it must
// share Serial (set false; debug output is then disabled). A NodeMCU or
// Wemos D1 mini driving the relay board externally can use Serial1
// instead — a TX-only UART on GPIO2 (NodeMCU pin D4): set true, wire D4
// to the relay board socket's TX hole, and USB debug output keeps
// working on the normal serial monitor.
const bool LCTECH_ON_SERIAL1 = true;

// Debug printing is possible unless the relay chip owns the USB serial line.
const bool SERIAL_DEBUG = !LCTECH_SERIAL_RELAY || LCTECH_ON_SERIAL1;

// Blink briefly every 3 s when the bridge stops updating ("no signal").
// Good for LED builds; keep false for a relay, which would audibly click
// every 3 seconds. With false, "no signal" simply switches everything off.
const bool STALE_BLIP = false;

const unsigned long STALE_AFTER_MS = 60UL * 1000UL;

ESP8266WebServer server(80);

String currentState = "none";   // free | idle | busy | none
String lightMode = "traffic";   // traffic | single
unsigned long lastUpdateMs = 0;
bool haveUpdate = false;

// EEPROM byte 0: 1 = single mode, anything else = traffic mode
const int MODE_EEPROM_ADDR = 0;

void loadMode() {
  EEPROM.begin(8);
  lightMode = (EEPROM.read(MODE_EEPROM_ADDR) == 1) ? "single" : "traffic";
}

void saveMode() {
  EEPROM.write(MODE_EEPROM_ADDR, lightMode == "single" ? 1 : 0);
  EEPROM.commit();
}

void writeLamp(uint8_t pin, bool on) {
  if (pin == PIN_UNUSED) return;
  digitalWrite(pin, (on != LAMPS_ACTIVE_LOW) ? HIGH : LOW);
}

void sendLcTechRelay(bool on) {
  static int lastSent = -1;
  if ((int)on == lastSent) return;
  lastSent = on;
  const uint8_t cmdOn[]  = {0xA0, 0x01, 0x01, 0xA2};
  const uint8_t cmdOff[] = {0xA0, 0x01, 0x00, 0xA1};
  if (LCTECH_ON_SERIAL1) Serial1.write(on ? cmdOn : cmdOff, 4);
  else                   Serial.write(on ? cmdOn : cmdOff, 4);
}

void setLamps(bool g, bool o, bool r) {
  if (LCTECH_SERIAL_RELAY) {
    sendLcTechRelay(r);   // the red/busy channel drives the relay
    return;
  }
  writeLamp(PIN_GREEN, g);
  writeLamp(PIN_ORANGE, o);
  writeLamp(PIN_RED, r);
}

void applyState() {
  if (lightMode == "single") {
    setLamps(false, false, currentState == "busy");
    return;
  }
  if (currentState == "busy")      setLamps(false, false, true);
  else if (currentState == "idle") setLamps(false, true, false);
  else if (currentState == "free") setLamps(true, false, false);
  else                             setLamps(false, false, false);
}

bool isStale() {
  return !haveUpdate || (millis() - lastUpdateMs) > STALE_AFTER_MS;
}

void handleSet() {
  String s = server.arg("state");
  if (s != "free" && s != "idle" && s != "busy") {
    server.send(400, "text/plain", "state must be free, idle or busy");
    return;
  }
  currentState = s;
  lastUpdateMs = millis();
  haveUpdate = true;
  applyState();
  server.send(200, "text/plain", "OK " + s);
}

void handleMode() {
  String m = server.arg("set");
  if (m == "traffic" || m == "single") {
    lightMode = m;
    saveMode();
    applyState();
  } else if (m.length() > 0) {
    server.send(400, "text/plain", "mode must be traffic or single");
    return;
  }
  if (server.hasArg("ui")) {  // mode switched from the status page
    server.sendHeader("Location", "/");
    server.send(303);
    return;
  }
  server.send(200, "text/plain", "mode " + lightMode);
}

void handleStatus() {
  unsigned long age = haveUpdate ? (millis() - lastUpdateMs) / 1000UL : 0;
  String json = "{\"state\":\"" + currentState + "\",\"mode\":\"" + lightMode +
                "\",\"stale\":" + (isStale() ? "true" : "false") +
                ",\"secondsSinceUpdate\":" + String(age) + "}";
  server.send(200, "application/json", json);
}

void handleRoot() {
  // The circle mirrors what the physical lamp shows in the current mode.
  String colour;
  if (isStale()) {
    colour = "#666";
  } else if (lightMode == "single") {
    colour = currentState == "busy" ? "#d33" : "#333";
  } else {
    colour = currentState == "busy" ? "#d33" :
             currentState == "idle" ? "#e90" :
             currentState == "free" ? "#2a2" : "#666";
  }
  String label = isStale() ? "NO SIGNAL" : currentState;
  String modeLine = (lightMode == "single")
    ? "Mode: single red lamp &mdash; <a href='/mode?set=traffic&ui=1'>switch to traffic light</a>"
    : "Mode: traffic light &mdash; <a href='/mode?set=single&ui=1'>switch to single red lamp</a>";
  String html =
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta http-equiv='refresh' content='5'>"
    "<title>Teams Busy Light</title></head>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:3em'>"
    "<h1>Teams Busy Light</h1>"
    "<div style='width:120px;height:120px;border-radius:50%;margin:1em auto;"
    "background:" + colour + "'></div>"
    "<p style='font-size:1.5em'>" + label + "</p>"
    "<p style='color:#888'>" + modeLine + "</p></body></html>";
  server.send(200, "text/html", html);
}

void setup() {
  if (LCTECH_SERIAL_RELAY && !LCTECH_ON_SERIAL1) {
    // The relay logic chip listens on this line — 9600 baud, no debug
    // output allowed.
    Serial.begin(9600);
  } else {
    // TX-only serial keeps debug output on the TX pin while leaving
    // GPIO3 (RX) available as an output.
    Serial.begin(115200, SERIAL_8N1, SERIAL_TX_ONLY);
  }
  if (LCTECH_SERIAL_RELAY) {
    if (LCTECH_ON_SERIAL1) Serial1.begin(9600);  // relay chip on GPIO2 (D4)
  } else {
    if (PIN_GREEN  != PIN_UNUSED) pinMode(PIN_GREEN, OUTPUT);
    if (PIN_ORANGE != PIN_UNUSED) pinMode(PIN_ORANGE, OUTPUT);
    if (PIN_RED    != PIN_UNUSED) pinMode(PIN_RED, OUTPUT);
  }
  setLamps(false, false, false);
  loadMode();

  WiFi.mode(WIFI_STA);
  WiFi.hostname("teams-busylight");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  if (SERIAL_DEBUG) Serial.print("\nConnecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    if (SERIAL_DEBUG) Serial.print(".");
  }
  if (SERIAL_DEBUG)
    Serial.printf("\nConnected, IP: %s\n", WiFi.localIP().toString().c_str());

  server.on("/", handleRoot);
  server.on("/set", handleSet);
  server.on("/mode", handleMode);
  server.on("/status", handleStatus);
  server.begin();
}

void loop() {
  server.handleClient();

  if (isStale()) {
    if (STALE_BLIP) {
      // "no data" blip: orange in traffic mode, red in single mode —
      // clearly distinct from a solid colour.
      unsigned long phase = millis() % 3000UL;
      bool blip = phase < 100;
      if (lightMode == "single") setLamps(false, false, blip);
      else                       setLamps(false, blip, false);
    } else {
      setLamps(false, false, false);
    }
  } else {
    applyState();
  }
}
