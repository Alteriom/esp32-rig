//************************************************************
// Rig Health Check firmware (the farm's `canary` profile)
//
// Firmware the farm owns, whose only purpose is to say whether an ESP and
// the rig around it are healthy. It contains nothing from painlessMesh or
// from any consumer -- the Arduino core, ESP-IDF and ArduinoJson only -- so
// a red canary is never a consumer's regression. It is built once per farm
// release, installed as the current canary, and can be run against one
// board or the whole rig at any time.
//
// It speaks the same newline-JSON framing the HAL already reads
// (alteriom_hil.protocol.BoardClient). Keep this file,
// suites/canary/tests/canary_client.py and alteriom_hil/sim.py in lockstep:
// the tests drive all three.
//
// host -> board commands:
//   {"cmd":"info"}
//   {"cmd":"echo","text":"..."}
//   {"cmd":"store_write","key":"k","value":"v"}
//   {"cmd":"store_read","key":"k"}
//   {"cmd":"store_erase","key":"k"}
//   {"cmd":"wifi_scan","ssid":"only-this-one"}      // ssid optional
//   {"cmd":"wifi_join","ssid":"x","password":"y","timeoutMs":20000}
//   {"cmd":"wifi_leave"}
//   {"cmd":"http_get","url":"http://host:port/path","timeoutMs":8000}
//   {"cmd":"mqtt_publish","host":"h","port":1883,"topic":"t","payload":"p"}
//   {"cmd":"reset"}
//   {"cmd":"gpio_mode","pin":18,"mode":"input|pullup|pulldown|output"}
//   {"cmd":"gpio_write","pin":18,"level":1}          // makes it an output
//   {"cmd":"gpio_read","pin":18}
//   {"cmd":"gpio_release","pin":18}                   // an input, no pull
//
// board -> host events (one JSON object per line):
//   boot, info, echo, store, wifi_scan, wifi_join, wifi_leave, http_get,
//   mqtt_publish, gpio, resetting, error
//************************************************************
#include "canary_platform.h"

#ifndef CANARY_TARGET
#define CANARY_TARGET "unknown"
#endif
#ifndef CANARY_SHA
#define CANARY_SHA "unknown"
#endif
// MAJOR.MINOR.PATCH, stamped by canary/build_artifacts.py.
#ifndef CANARY_VERSION
#define CANARY_VERSION "unknown"
#endif

// A whole line of JSON, and the longest echo the serial check sends. The
// check exists to prove the path is clean under load, so the buffer is
// generous -- a truncated line would look like a serial fault.
constexpr size_t kLineMax = 2048;

CanaryStore store;
String line;
String bootId;
uint32_t joinedAtMs = 0;

// ---- framing ---------------------------------------------------------------

void emit(JsonDocument &doc) {
  String out;
  serializeJson(doc, out);
  Serial.println(out);
  Serial.flush();
#if CANARY_HAS_SECOND_CONSOLE
  CANARY_SECOND_CONSOLE.println(out);
  CANARY_SECOND_CONSOLE.flush();
#endif
}

void emitError(const char *error, const char *cmd = nullptr) {
  JsonDocument doc;
  doc["evt"] = "error";
  doc["error"] = error;
  if (cmd) doc["cmd"] = cmd;
  emit(doc);
}

// ---- checks ----------------------------------------------------------------

void replyInfo() {
  JsonDocument doc;
  doc["evt"] = "info";
  doc["version"] = CANARY_VERSION;
  doc["family"] = CANARY_TARGET;
  doc["canarySha"] = CANARY_SHA;
  doc["bootId"] = bootId;
  doc["resetReason"] = canaryResetReason();
  doc["uptimeMs"] = millis();
  doc["freeHeap"] = ESP.getFreeHeap();
  doc["mac"] = WiFi.macAddress();
  doc["store"] = store.backing();
  doc["pinTable"] = kPinTable + 5;  // past "pins:"
  canaryDescribeChip(doc["silicon"].to<JsonObject>());
  emit(doc);
}

void replyEcho(JsonDocument &cmd) {
  // Returned byte for byte: anything the UART mangles shows as a difference
  // the host can print, not as a missing reply.
  const char *text = cmd["text"] | "";
  JsonDocument doc;
  doc["evt"] = "echo";
  doc["text"] = text;
  doc["len"] = strlen(text);
  emit(doc);
}

void replyStore(JsonDocument &cmd, const char *op) {
  const char *key = cmd["key"] | "canary";
  JsonDocument doc;
  doc["evt"] = "store";
  doc["op"] = op;
  doc["key"] = key;
  doc["backing"] = store.backing();
  if (strcmp(op, "write") == 0) {
    const String value = String(cmd["value"] | "");
    doc["ok"] = store.put(key, value);
    doc["bytes"] = value.length();
  } else if (strcmp(op, "read") == 0) {
    String value;
    const bool found = store.get(key, value);
    doc["found"] = found;
    doc["ok"] = found;
    if (found) doc["value"] = value;
  } else {
    doc["ok"] = store.erase(key);
  }
  emit(doc);
}

void replyScan(JsonDocument &cmd) {
  // The rig's own AP is what the host asks about, so one SSID may be named
  // and the rest reported only as a count: a rig in a block of flats sees
  // forty networks and the line would not fit a frame.
  // nullptr here means "no SSID named", which is a different answer from
  // the empty string, so this one keeps its check rather than using `|`.
  const char *wanted = cmd["ssid"].is<const char *>() ? cmd["ssid"].as<const char *>() : nullptr;
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  const uint32_t started = millis();
  const int found = WiFi.scanNetworks();
  JsonDocument doc;
  doc["evt"] = "wifi_scan";
  doc["ms"] = millis() - started;
  doc["count"] = found < 0 ? 0 : found;
  if (found < 0) {
    doc["ok"] = false;
    doc["error"] = "scan failed";
    emit(doc);
    return;
  }
  doc["ok"] = true;
  JsonArray networks = doc["networks"].to<JsonArray>();
  for (int i = 0; i < found; i++) {
    const String ssid = WiFi.SSID(i);
    if (wanted && ssid != wanted) continue;
    JsonObject entry = networks.add<JsonObject>();
    entry["ssid"] = ssid;
    entry["rssi"] = WiFi.RSSI(i);
    entry["channel"] = WiFi.channel(i);
  }
  if (wanted) doc["seen"] = networks.size() > 0;
  WiFi.scanDelete();
  emit(doc);
}

void replyJoin(JsonDocument &cmd) {
  const char *ssid = cmd["ssid"] | "";
  const char *password = cmd["password"] | "";
  const uint32_t budget = cmd["timeoutMs"] | 20000;
  // Station only, never an AP: the ESP8266 is specified as a leaf and a
  // board that brought up an AP would change what every other board on the
  // rig can see.
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  const uint32_t started = millis();
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED && millis() - started < budget) {
    delay(100);
  }
  const bool joined = WiFi.status() == WL_CONNECTED;
  JsonDocument doc;
  doc["evt"] = "wifi_join";
  doc["ssid"] = ssid;
  doc["joined"] = joined;
  doc["ok"] = joined;
  doc["ms"] = millis() - started;
  doc["status"] = static_cast<int>(WiFi.status());
  if (joined) {
    joinedAtMs = millis();
    doc["ip"] = WiFi.localIP().toString();
    doc["rssi"] = WiFi.RSSI();
    doc["channel"] = WiFi.channel();
    doc["gateway"] = WiFi.gatewayIP().toString();
  }
  emit(doc);
}

void replyLeave() {
  WiFi.disconnect();
  WiFi.mode(WIFI_OFF);
  joinedAtMs = 0;
  JsonDocument doc;
  doc["evt"] = "wifi_leave";
  doc["ok"] = true;
  emit(doc);
}

// A GET written by hand rather than through an HTTP library: one dependency
// fewer to misread as a board fault, and the rig's uplink probe answers a
// status line and a short body.
void replyHttpGet(JsonDocument &cmd) {
  const String url = String(cmd["url"] | "");
  const uint32_t budget = cmd["timeoutMs"] | 8000;
  JsonDocument doc;
  doc["evt"] = "http_get";
  doc["url"] = url;
  if (!url.startsWith("http://")) {
    doc["ok"] = false;
    doc["error"] = "only http:// is probed";
    emit(doc);
    return;
  }
  const int slash = url.indexOf('/', 7);
  String authority = slash < 0 ? url.substring(7) : url.substring(7, slash);
  const String path = slash < 0 ? String("/") : url.substring(slash);
  uint16_t port = 80;
  const int colon = authority.indexOf(':');
  if (colon >= 0) {
    port = authority.substring(colon + 1).toInt();
    authority = authority.substring(0, colon);
  }
  WiFiClient client;
  canarySetClientTimeout(client, budget);
  const uint32_t started = millis();
  if (!client.connect(authority.c_str(), port)) {
    doc["ok"] = false;
    doc["error"] = "connect failed";
    doc["ms"] = millis() - started;
    emit(doc);
    return;
  }
  client.print(String("GET ") + path + " HTTP/1.1\r\nHost: " + authority +
               "\r\nConnection: close\r\nUser-Agent: alteriom-canary\r\n\r\n");
  // Wait for the answer, not for the connection. The rig's probe replies
  // HTTP/1.0 and closes at once, which leaves `connected()` false with the
  // response still in the receive buffer -- so a loop gated on it read
  // nothing and gave up in 28 ms, and the canary reported the rig's uplink
  // unreachable from the ESP8266 while every other board got its 200.
  // readStringUntil waits up to the timeout set above and reads what is
  // buffered whether the peer is still there or not.
  const String status = client.readStringUntil('\n');
  // Whatever else arrived, counted so the answer says how much came back.
  // No `connected()` here either, for the same reason.
  size_t bytes = 0;
  while (millis() - started < budget) {
    if (!client.available()) break;
    client.read();
    bytes++;
  }
  client.stop();
  String line = status;
  line.trim();
  int code = 0;
  const int space = line.indexOf(' ');
  if (space > 0) code = line.substring(space + 1).toInt();
  doc["status"] = code;
  doc["ok"] = code >= 200 && code < 400;
  doc["statusLine"] = line;
  doc["bytes"] = bytes;
  doc["ms"] = millis() - started;
  emit(doc);
}

// MQTT 3.1.1 CONNECT + PUBLISH (QoS 0) + DISCONNECT, packed here rather than
// taken from a client library: the rig's queue is what is under test, and a
// library's own reconnect logic would sit between the answer and the truth.
namespace mqtt {

void writeLength(WiFiClient &client, size_t length) {
  do {
    uint8_t byte = length % 128;
    length /= 128;
    if (length > 0) byte |= 0x80;
    client.write(byte);
  } while (length > 0);
}

void writeString(WiFiClient &client, const String &value) {
  client.write(static_cast<uint8_t>(value.length() >> 8));
  client.write(static_cast<uint8_t>(value.length() & 0xFF));
  client.write(reinterpret_cast<const uint8_t *>(value.c_str()), value.length());
}

bool connect(WiFiClient &client, const String &clientId, uint32_t budget) {
  const size_t payload = 10 + 2 + clientId.length();
  client.write(static_cast<uint8_t>(0x10));
  writeLength(client, payload);
  writeString(client, "MQTT");
  client.write(static_cast<uint8_t>(0x04));  // protocol level 4 = 3.1.1
  client.write(static_cast<uint8_t>(0x02));  // clean session, no will, no auth
  client.write(static_cast<uint8_t>(0x00));  // keep-alive: 60 s
  client.write(static_cast<uint8_t>(0x3C));
  writeString(client, clientId);
  const uint32_t deadline = millis() + budget;
  uint8_t response[4] = {0, 0, 0, 0};
  size_t read = 0;
  while (read < 4 && millis() < deadline) {
    if (!client.available()) {
      delay(5);
      continue;
    }
    response[read++] = client.read();
  }
  // CONNACK is 0x20 0x02 <flags> <return code>; 0 is accepted.
  return read == 4 && response[0] == 0x20 && response[3] == 0x00;
}

void publish(WiFiClient &client, const String &topic, const String &payload) {
  client.write(static_cast<uint8_t>(0x30));  // PUBLISH, QoS 0, not retained
  writeLength(client, 2 + topic.length() + payload.length());
  writeString(client, topic);
  client.write(reinterpret_cast<const uint8_t *>(payload.c_str()), payload.length());
}

void disconnect(WiFiClient &client) {
  client.write(static_cast<uint8_t>(0xE0));
  client.write(static_cast<uint8_t>(0x00));
}

}  // namespace mqtt

void replyMqttPublish(JsonDocument &cmd) {
  const String host = String(cmd["host"] | "");
  const uint16_t port = cmd["port"] | 1883;
  const String topic = String(cmd["topic"] | "");
  const String payload = String(cmd["payload"] | "");
  const String clientId = cmd["clientId"].is<const char *>()
                              ? String(cmd["clientId"].as<const char *>())
                              : String("canary-") + bootId;
  const uint32_t budget = cmd["timeoutMs"] | 8000;
  JsonDocument doc;
  doc["evt"] = "mqtt_publish";
  doc["topic"] = topic;
  doc["clientId"] = clientId;
  const uint32_t started = millis();
  WiFiClient client;
  canarySetClientTimeout(client, budget);
  if (!client.connect(host.c_str(), port)) {
    doc["ok"] = false;
    doc["error"] = "connect failed";
    doc["ms"] = millis() - started;
    emit(doc);
    return;
  }
  if (!mqtt::connect(client, clientId, budget)) {
    client.stop();
    doc["ok"] = false;
    doc["error"] = "broker refused the connection";
    doc["ms"] = millis() - started;
    emit(doc);
    return;
  }
  mqtt::publish(client, topic, payload);
  client.flush();
  mqtt::disconnect(client);
  client.stop();
  doc["ok"] = true;
  doc["bytes"] = payload.length();
  doc["ms"] = millis() - started;
  emit(doc);
}

// ---- pins, for the wiring check ----------------------------------------------
// The board's end of each jumper to an instrument. Only a wireable pin
// (canary_platform.h), and never driven when it is input-only. Every answer is
// {"evt":"gpio","op":...,"pin":n,"ok":...}, with "level" for a read or write
// and "error" when refused -- a refused command did nothing.

void replyGpio(JsonDocument &cmd, const char *op) {
  JsonDocument doc;
  doc["evt"] = "gpio";
  doc["op"] = op;
  const int pin = cmd["pin"] | -1;
  doc["pin"] = pin;
  const char *error = nullptr;
  if (!canaryWireable(pin)) {
    error = "not a wireable pin on this family";
  } else if (strcmp(op, "mode") == 0) {
    const char *mode = cmd["mode"] | "";
    if (strcmp(mode, "input") == 0) {
      pinMode(pin, INPUT);
    } else if (strcmp(mode, "pullup") == 0) {
      pinMode(pin, INPUT_PULLUP);
    } else if (strcmp(mode, "pulldown") == 0) {
#if defined(ESP8266)
      error = "the ESP8266 has no pull-down on these pins";
#else
      pinMode(pin, INPUT_PULLDOWN);
#endif
    } else if (strcmp(mode, "output") == 0) {
      if (canaryInputOnly(pin)) {
        error = "an input-only pin";
      } else {
        pinMode(pin, OUTPUT);
      }
    } else {
      error = "mode must be input, pullup, pulldown or output";
    }
    if (!error) doc["mode"] = mode;
  } else if (strcmp(op, "write") == 0) {
    if (canaryInputOnly(pin)) {
      error = "an input-only pin";
    } else {
      const int level = (cmd["level"] | 0) ? HIGH : LOW;
      pinMode(pin, OUTPUT);
      digitalWrite(pin, level);
      doc["level"] = level == HIGH ? 1 : 0;
    }
  } else if (strcmp(op, "read") == 0) {
    doc["level"] = digitalRead(pin) == HIGH ? 1 : 0;
  } else {  // release
    pinMode(pin, INPUT);
  }
  doc["ok"] = error == nullptr;
  if (error) doc["error"] = error;
  emit(doc);
}

// ---- the loop --------------------------------------------------------------

void handle(const String &raw) {
  JsonDocument cmd;
  if (deserializeJson(cmd, raw) != DeserializationError::Ok) {
    // The exact words the HAL retries on: a parse failure says the command
    // did not run, which makes resending it safe.
    emitError("bad json");
    return;
  }
  const char *name = cmd["cmd"] | "";
  if (strcmp(name, "info") == 0) {
    replyInfo();
  } else if (strcmp(name, "echo") == 0) {
    replyEcho(cmd);
  } else if (strcmp(name, "store_write") == 0) {
    replyStore(cmd, "write");
  } else if (strcmp(name, "store_read") == 0) {
    replyStore(cmd, "read");
  } else if (strcmp(name, "store_erase") == 0) {
    replyStore(cmd, "erase");
  } else if (strcmp(name, "wifi_scan") == 0) {
    replyScan(cmd);
  } else if (strcmp(name, "wifi_join") == 0) {
    replyJoin(cmd);
  } else if (strcmp(name, "wifi_leave") == 0) {
    replyLeave();
  } else if (strcmp(name, "http_get") == 0) {
    replyHttpGet(cmd);
  } else if (strcmp(name, "mqtt_publish") == 0) {
    replyMqttPublish(cmd);
  } else if (strcmp(name, "gpio_mode") == 0) {
    replyGpio(cmd, "mode");
  } else if (strcmp(name, "gpio_write") == 0) {
    replyGpio(cmd, "write");
  } else if (strcmp(name, "gpio_read") == 0) {
    replyGpio(cmd, "read");
  } else if (strcmp(name, "gpio_release") == 0) {
    replyGpio(cmd, "release");
  } else if (strcmp(name, "reset") == 0) {
    JsonDocument doc;
    doc["evt"] = "resetting";
    doc["bootId"] = bootId;
    emit(doc);
    delay(100);
    ESP.restart();
  } else {
    String message = String("unknown cmd ") + name;
    emitError(message.c_str());
  }
}

// Returns whether anything was read, so the loop can keep draining while a
// long command is still arriving rather than sleeping in the middle of it.
bool pump(Stream &console) {
  bool read = false;
  while (console.available()) {
    read = true;
    const char c = static_cast<char>(console.read());
    if (c == '\r') continue;
    if (c == '\n') {
      String raw = line;
      line = "";
      raw.trim();
      if (raw.length() > 0) handle(raw);
      continue;
    }
    if (line.length() >= kLineMax) {
      // Say so rather than acting on half a command: a dropped frame is a
      // fact the host can retry on, a truncated one is a wrong answer.
      line = "";
      emitError("frame dropped: line too long");
      continue;
    }
    line += c;
  }
  return read;
}

void setup() {
  // Room for a whole line and then some: the serial check sends a kilobyte,
  // and a console whose buffer is smaller loses the end of it before this
  // sketch is ever scheduled.
  canaryOpenConsole(Serial, kLineMax + 512);
#if CANARY_HAS_SECOND_CONSOLE
  canaryOpenConsole(CANARY_SECOND_CONSOLE, kLineMax + 512);
#endif
  delay(200);
  line.reserve(kLineMax + 1);
  // A new id every boot, so a reset is provable: the host compares the id it
  // had with the one it gets, and a board that never restarted says so.
  char buffer[9];
  snprintf(buffer, sizeof(buffer), "%08x", canaryRandom());
  bootId = buffer;
  const bool storeReady = store.begin();
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  JsonDocument doc;
  doc["evt"] = "boot";
  doc["version"] = CANARY_VERSION;
  doc["family"] = CANARY_TARGET;
  doc["canarySha"] = CANARY_SHA;
  doc["bootId"] = bootId;
  doc["resetReason"] = canaryResetReason();
  doc["store"] = store.backing();
  doc["storeReady"] = storeReady;
  emit(doc);
}

void loop() {
  // Drain while anything keeps arriving. A kilobyte-long command arrives in
  // packets, and sleeping in the middle of one is the other half of how a
  // small buffer loses it.
  bool busy = pump(Serial);
#if CANARY_HAS_SECOND_CONSOLE
  busy = pump(CANARY_SECOND_CONSOLE) || busy;
#endif
  if (!busy) delay(2);
}
