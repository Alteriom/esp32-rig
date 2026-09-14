// The farm's I/O instrument, on an ESP32 (esp32dev / WROOM-32).
//
// Its pins are test equipment: they press a button on a board under test,
// read its LED, measure a pulse. It speaks newline-delimited JSON on its USB
// serial port, framed like a HIL agent, and answers every command once.
//
// host -> instrument:
//   {"cmd":"info"}
//   {"cmd":"mode","ch":25,"mode":"input|pullup|pulldown|output"}
//   {"cmd":"write","ch":25,"level":1}            // makes the channel an output
//   {"cmd":"read","ch":34}
//   {"cmd":"adc","ch":34,"samples":8}
//   {"cmd":"pulse","ch":25,"level":1,"ms":100}   // level for ms, then the other
//   {"cmd":"count","ch":34,"ms":1000,"edge":"rising|falling|any"}
//   {"cmd":"wait_edge","ch":34,"edge":"any","timeoutMs":2000}
//   {"cmd":"release"}                            // every channel a plain input
//
// instrument -> host: {"evt":"<cmd>",...} for each, {"evt":"edge",...} for
// wait_edge, {"evt":"boot",...} once at start, and
// {"evt":"error","cmd":"<cmd>","error":"..."} for a command refused -- which
// did not run. A line that is not JSON is answered {"evt":"error","error":"bad
// json"}, which the host takes as permission to resend.
//
// Every channel starts as an input with no pull, and `release` puts them back:
// the one state nothing wired to it can be harmed by. A command runs to
// completion before the next is read, so `pulse`, `count` and `wait_edge`
// block for as long as they were asked to (at most ten seconds, one minute
// for wait_edge).
//
// The channel table is the farm's too (alteriom_hil.instrument.KINDS); a test
// keeps the two in agreement.

#include <Arduino.h>
#include <ArduinoJson.h>

#ifndef INSTRUMENT_SHA
#define INSTRUMENT_SHA ""
#endif

static const int PROTOCOL_VERSION = 1;
static const char *KIND = "esp32-io";

struct Channel {
  uint8_t pin;
  bool drive;
  bool pull;
  bool adc;
};

// Left out: strapping 0 2 5 12 15, console UART 1 3, flash 6-11. 34-39 are
// input-only and have no pulls.
static const Channel CHANNELS[] = {
    {4, true, true, false},   {13, true, true, false}, {14, true, true, false},
    {16, true, true, false},  {17, true, true, false}, {18, true, true, false},
    {19, true, true, false},  {21, true, true, false}, {22, true, true, false},
    {23, true, true, false},  {25, true, true, false}, {26, true, true, false},
    {27, true, true, false},  {32, true, true, true},  {33, true, true, true},
    {34, false, false, true}, {35, false, false, true}, {36, false, false, true},
    {39, false, false, true},
};
static const size_t CHANNEL_COUNT = sizeof(CHANNELS) / sizeof(CHANNELS[0]);

static volatile uint32_t edgeCount = 0;
static volatile bool edgeSeen = false;

static void IRAM_ATTR onEdge() {
  edgeCount++;
  edgeSeen = true;
}

const Channel *findChannel(int pin) {
  for (size_t i = 0; i < CHANNEL_COUNT; i++) {
    if (CHANNELS[i].pin == pin) return &CHANNELS[i];
  }
  return nullptr;
}

String macAddress() {
  const uint64_t mac = ESP.getEfuseMac();  // byte 0 is the first octet
  char text[18];
  snprintf(text, sizeof(text), "%02x:%02x:%02x:%02x:%02x:%02x", (uint8_t)(mac), (uint8_t)(mac >> 8),
           (uint8_t)(mac >> 16), (uint8_t)(mac >> 24), (uint8_t)(mac >> 32), (uint8_t)(mac >> 40));
  return String(text);
}

void send(JsonDocument &doc, JsonDocument &cmd) {
  // A host may tag a command to match its reply; echo the tag back.
  if (!cmd["seq"].isNull()) doc["seq"] = cmd["seq"];
  serializeJson(doc, Serial);
  Serial.print('\n');
}

void refuse(JsonDocument &cmd, const char *error) {
  JsonDocument doc;
  doc["evt"] = "error";
  if (cmd["cmd"].is<const char *>()) doc["cmd"] = cmd["cmd"].as<const char *>();
  doc["error"] = error;
  send(doc, cmd);
}

void releaseAll() {
  for (size_t i = 0; i < CHANNEL_COUNT; i++) {
    detachInterrupt(digitalPinToInterrupt(CHANNELS[i].pin));
    pinMode(CHANNELS[i].pin, INPUT);
  }
}

void describe(JsonDocument &doc) {
  doc["role"] = "instrument";
  doc["kind"] = KIND;
  doc["protocol"] = PROTOCOL_VERSION;
  doc["fw"] = INSTRUMENT_SHA;
  doc["target"] = "esp32";
  doc["mac"] = macAddress();
}

// The channel a command names, or nullptr after refusing it.
const Channel *channelFor(JsonDocument &cmd) {
  if (!cmd["ch"].is<int>()) {
    refuse(cmd, "ch must be a channel number");
    return nullptr;
  }
  const Channel *channel = findChannel(cmd["ch"].as<int>());
  if (!channel) refuse(cmd, "no such channel");
  return channel;
}

int interruptMode(JsonDocument &cmd, const char *fallback) {
  const char *edge = cmd["edge"] | fallback;
  if (strcmp(edge, "rising") == 0) return RISING;
  if (strcmp(edge, "falling") == 0) return FALLING;
  if (strcmp(edge, "any") == 0) return CHANGE;
  return -1;
}

void handle(JsonDocument &cmd) {
  const char *name = cmd["cmd"] | "";
  JsonDocument doc;

  if (strcmp(name, "info") == 0) {
    doc["evt"] = "info";
    describe(doc);
    doc["uptimeMs"] = millis();
    JsonArray channels = doc["channels"].to<JsonArray>();
    for (size_t i = 0; i < CHANNEL_COUNT; i++) {
      JsonObject entry = channels.add<JsonObject>();
      entry["ch"] = CHANNELS[i].pin;
      entry["drive"] = CHANNELS[i].drive;
      entry["pull"] = CHANNELS[i].pull;
      entry["adc"] = CHANNELS[i].adc;
    }
    send(doc, cmd);
    return;
  }

  if (strcmp(name, "release") == 0) {
    releaseAll();
    doc["evt"] = "release";
    doc["ok"] = true;
    send(doc, cmd);
    return;
  }

  if (strcmp(name, "mode") == 0) {
    const Channel *channel = channelFor(cmd);
    if (!channel) return;
    const char *mode = cmd["mode"] | "";
    if (strcmp(mode, "input") == 0) {
      pinMode(channel->pin, INPUT);
    } else if (strcmp(mode, "pullup") == 0 || strcmp(mode, "pulldown") == 0) {
      if (!channel->pull) return refuse(cmd, "this channel has no internal pulls");
      pinMode(channel->pin, strcmp(mode, "pullup") == 0 ? INPUT_PULLUP : INPUT_PULLDOWN);
    } else if (strcmp(mode, "output") == 0) {
      if (!channel->drive) return refuse(cmd, "this channel is input-only");
      pinMode(channel->pin, OUTPUT);
    } else {
      return refuse(cmd, "mode must be input, pullup, pulldown or output");
    }
    doc["evt"] = "mode";
    doc["ch"] = channel->pin;
    doc["mode"] = mode;
    send(doc, cmd);
    return;
  }

  if (strcmp(name, "write") == 0 || strcmp(name, "pulse") == 0) {
    const Channel *channel = channelFor(cmd);
    if (!channel) return;
    if (!channel->drive) return refuse(cmd, "this channel is input-only");
    const int level = (cmd["level"] | 0) ? HIGH : LOW;
    pinMode(channel->pin, OUTPUT);
    digitalWrite(channel->pin, level);
    if (strcmp(name, "pulse") == 0) {
      const uint32_t ms = constrain((uint32_t)(cmd["ms"] | 100), 1u, 10000u);
      delay(ms);
      digitalWrite(channel->pin, level == HIGH ? LOW : HIGH);
      doc["ms"] = ms;
    }
    doc["evt"] = name;
    doc["ch"] = channel->pin;
    doc["level"] = level == HIGH ? 1 : 0;
    send(doc, cmd);
    return;
  }

  if (strcmp(name, "read") == 0) {
    const Channel *channel = channelFor(cmd);
    if (!channel) return;
    doc["evt"] = "read";
    doc["ch"] = channel->pin;
    doc["level"] = digitalRead(channel->pin) == HIGH ? 1 : 0;
    send(doc, cmd);
    return;
  }

  if (strcmp(name, "adc") == 0) {
    const Channel *channel = channelFor(cmd);
    if (!channel) return;
    if (!channel->adc) return refuse(cmd, "this channel cannot measure a voltage");
    const int samples = constrain((int)(cmd["samples"] | 8), 1, 64);
    uint32_t total = 0;
    for (int i = 0; i < samples; i++) total += analogReadMilliVolts(channel->pin);
    doc["evt"] = "adc";
    doc["ch"] = channel->pin;
    doc["mv"] = total / samples;
    doc["samples"] = samples;
    send(doc, cmd);
    return;
  }

  if (strcmp(name, "count") == 0 || strcmp(name, "wait_edge") == 0) {
    const Channel *channel = channelFor(cmd);
    if (!channel) return;
    const bool counting = strcmp(name, "count") == 0;
    const int mode = interruptMode(cmd, counting ? "rising" : "any");
    if (mode < 0) return refuse(cmd, "edge must be rising, falling or any");
    const uint32_t budget = counting ? constrain((uint32_t)(cmd["ms"] | 1000), 1u, 10000u)
                                     : constrain((uint32_t)(cmd["timeoutMs"] | 2000), 1u, 60000u);
    noInterrupts();
    edgeCount = 0;
    edgeSeen = false;
    interrupts();
    attachInterrupt(digitalPinToInterrupt(channel->pin), onEdge, mode);
    const uint32_t started = millis();
    while (millis() - started < budget) {
      if (!counting && edgeSeen) break;
      delay(1);
    }
    detachInterrupt(digitalPinToInterrupt(channel->pin));
    const uint32_t elapsed = millis() - started;
    doc["ch"] = channel->pin;
    if (counting) {
      doc["evt"] = "count";
      doc["edges"] = edgeCount;
      doc["ms"] = budget;
    } else {
      doc["evt"] = "edge";
      if (edgeSeen) {
        doc["level"] = digitalRead(channel->pin) == HIGH ? 1 : 0;
        doc["afterMs"] = elapsed;
      } else {
        doc["timeout"] = true;
      }
    }
    send(doc, cmd);
    return;
  }

  refuse(cmd, "unknown command");
}

static String line;

void setup() {
  Serial.begin(115200);
  releaseAll();
  line.reserve(256);
  JsonDocument doc;
  doc["evt"] = "boot";
  describe(doc);
  serializeJson(doc, Serial);
  Serial.print('\n');
}

void loop() {
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (line.length() < 512) line += c;
      continue;
    }
    if (line.length() == 0) continue;
    JsonDocument cmd;
    const DeserializationError failed = deserializeJson(cmd, line);
    line = "";
    if (failed || !cmd["cmd"].is<const char *>()) {
      JsonDocument doc;
      doc["evt"] = "error";
      doc["error"] = "bad json";
      serializeJson(doc, Serial);
      Serial.print('\n');
      continue;
    }
    handle(cmd);
  }
  delay(1);
}
