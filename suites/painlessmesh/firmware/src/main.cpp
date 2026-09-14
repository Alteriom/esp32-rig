//************************************************************
// painlessMesh HIL agent
//
// Flashed onto every board in the farm for a test run. Joins the test
// mesh and exposes a newline-JSON serial REPL that the pytest suite
// drives (see alteriom_hil.protocol.BoardClient — keep both sides and
// alteriom_hil/sim.py in lockstep).
//
// host -> board commands:
//   {"cmd":"info"}
//   {"cmd":"node_list"}
//   {"cmd":"send_single","dest":123,"msg":"x","ack":true,"ackTimeoutMs":5000}
//   {"cmd":"send_broadcast","msg":"x","ack":true,"ackTimeoutMs":5000,
//    "includeSelf":false}
//   {"cmd":"gateway_start","ssid":"x","password":"y"}
//   {"cmd":"shared_gateway_start","ssid":"x","password":"y"}
//   {"cmd":"gateway_failover_start","ssid":"x","password":"y"}
//   {"cmd":"gateway_status"}
//   {"cmd":"internet_send","tag":"case-1","url":"http://...","payload":""}
//   {"cmd":"mesh_configure","prefix":"run-x","password":"..."}
//   {"cmd":"mesh_start"}       // return a gateway to regular mesh mode
//   {"cmd":"stall","ms":3000}     // stop servicing mesh.update() for ms
//
// board -> host events (one JSON object per line):
//   boot, info, node_list, send_result, recv, ack, connection, stalled,
//   error
//   mesh_log — painlessMesh's own log lines, framed so they cannot splice
//   into a protocol frame; the HAL keeps them in the serial log only
//   capacity_warning — ESP8266 only: below 12 KB free with more than one
//   AP child; the part is specified as a leaf in meshes that size
//************************************************************
#include <painlessMesh.h>

#include <ArduinoJson.h>
#include <MD5Builder.h>

#include "hil_platform.h"

#ifndef HIL_MESH_PREFIX
#define HIL_MESH_PREFIX "AlteriomHILMesh"
#endif
#ifndef HIL_MESH_PASSWORD
#define HIL_MESH_PASSWORD "hil-not-secret"
#endif
#ifndef HIL_MESH_PORT
#define HIL_MESH_PORT 5555
#endif
#ifndef HIL_AGENT_VERSION
#define HIL_AGENT_VERSION "0.1.0"
#endif
#ifndef HIL_ARTIFACT_TARGET
#define HIL_ARTIFACT_TARGET "unknown"
#endif
#ifndef HIL_PAINLESSMESH_REF
#define HIL_PAINLESSMESH_REF "unknown"
#endif
#ifndef HIL_AGENT_SHA
#define HIL_AGENT_SHA "unknown"
#endif
#ifndef HIL_OTA_GENERATION
#define HIL_OTA_GENERATION 1
#endif

constexpr size_t HIL_OTA_PART_SIZE = 1024;
constexpr const char *HIL_OTA_FILE = "/hil-ota.bin";

Scheduler userScheduler;
painlessMesh mesh;

// Every reboot the host asks for is an orderly one: the mesh is stopped
// first, so a bridge announces that it is stepping down and the candidates
// hold their election now rather than a minute after its last status. A
// node that loses power says nothing, and that path stays as slow as the
// status timeout makes it.
void restartAgent() {
  mesh.stop();
  delay(200);
  ESP.restart();
}

uint32_t stallUntil = 0;
uint32_t bootId = 0;
// One line buffer per console: bytes from two ports interleaved into a
// single buffer would splice two commands into nonsense. lastByteMs is
// what lets a frame that lost its newline be reported instead of sitting
// in the buffer until the next command's newline turns both into one
// "bad json" — or, worse, sitting there silently.
struct ConsoleLine {
  String buffer;
  uint32_t lastByteMs = 0;
};
ConsoleLine serialLine;
#if HIL_HAS_SECOND_CONSOLE
ConsoleLine secondLine;
#endif
// Longer than any command but an OTA chunk, which is chunked by the host.
constexpr size_t CONSOLE_LINE_RESERVE = 768;
constexpr uint32_t CONSOLE_LINE_STALE_MS = 500;
RoleStore rolePreferences;
String activeMeshPrefix = HIL_MESH_PREFIX;
String activeMeshPassword = HIL_MESH_PASSWORD;
File otaUploadFile;
File otaSourceFile;
size_t otaExpectedSize = 0;
size_t otaUploadedSize = 0;
size_t otaLastChunkOffset = 0;
size_t otaLastChunkLength = 0;
String otaExpectedMd5;
String otaSourceMd5;
String otaSourceRole;
String otaLastChunkMd5;
std::shared_ptr<Task> otaOfferTask;

void receivedCallback(uint32_t from, String &msg);
void newConnectionCallback(uint32_t nodeId);

// Every console this board exposes, so the rig sees the protocol whichever
// socket is cabled. The native USB CDC is skipped while nothing is attached
// to it: HWCDC blocks for its transmit timeout when no host is listening, and
// paying that on every frame would slow the agent to a crawl on a board wired
// through its UART socket.
// flushNow=false is for diagnostics. flush() blocks until the bytes are out:
// on a real UART that is ~9 ms per frame at 115200, and on HWCDC up to the
// transmit timeout. Paying that on every mesh_log line stalls loop() — and a
// loop that is not running is not reading commands, which is how the C5 came
// to miss a mesh_configure it answers in 60 ms when idle. A reply the host is
// waiting on still flushes; diagnostics leave with the next one.
void consoleWriteLine(const char *frame, bool flushNow = true) {
#if HIL_HAS_SECOND_CONSOLE
  if (Serial) {
    Serial.println(frame);
    if (flushNow) Serial.flush();
  }
  HIL_SECOND_CONSOLE.println(frame);
  if (flushNow) HIL_SECOND_CONSOLE.flush();
#else
  Serial.println(frame);
  if (flushNow) Serial.flush();
#endif
}

void consoleWriteLine(const String &frame, bool flushNow = true) {
  consoleWriteLine(frame.c_str(), flushNow);
}

void consoleFlush() {
#if HIL_HAS_SECOND_CONSOLE
  if (Serial) Serial.flush();
  HIL_SECOND_CONSOLE.flush();
#else
  Serial.flush();
#endif
}

void emitEvent(JsonDocument &doc, bool flushNow = true) {
  // Build one complete frame before writing it. This avoids damaged JSON
  // prefixes on inexpensive USB-UART bridges under concurrent mesh traffic.
  String frame;
  if (!doc.overflowed() && frame.reserve(measureJson(doc) + 1)) {
    serializeJson(doc, frame);
  }
  if (doc.overflowed() || frame.length() <= 2) {
    // Out of memory: the document or its frame could not be allocated. On
    // the rig an ESP8266 printed "{}" in place of an internet_result and the
    // row waited out its timeout for an event the board had already tried to
    // send. Say so from the stack, naming the event where it is readable.
    const char *evt = doc["evt"] | "unknown";
    char line[128];
    snprintf(line, sizeof(line),
             "{\"evt\":\"event_dropped\",\"dropped\":\"%s\",\"reason\":\"out of memory\"}",
             evt);
    consoleWriteLine(line, flushNow);
    return;
  }
  consoleWriteLine(frame, flushNow);
}

void emitError(const char *message) {
  JsonDocument doc;
  doc["evt"] = "error";
  doc["error"] = message;
  emitEvent(doc);
}

#ifdef ESP32
// painlessMesh logs from the Wi-Fi event task as well as from loop().
// Written straight to Serial they splice into a JSON frame byte-for-byte,
// which is why regular nodes used to run at ERROR only — and why a node
// that sat associated-without-an-address for eighty seconds left no trace
// of what its radio was doing. The library hands each line to the sink;
// it is queued under a critical section and framed between protocol
// frames by loop(). The ring is small on purpose: a burst beyond it is
// counted, not paid for in RAM.
constexpr size_t MESH_LOG_LINES = 24;
constexpr size_t MESH_LOG_LINE_MAX = 160;
struct MeshLogRing {
  char lines[MESH_LOG_LINES][MESH_LOG_LINE_MAX];
  uint16_t levels[MESH_LOG_LINES];
  // millis() when the line was produced, not when it is drained: draining is
  // deferred behind commands, so a drain-time stamp would not order these
  // against the protocol events they need to be read alongside.
  uint32_t stamps[MESH_LOG_LINES];
  size_t head = 0;
  size_t count = 0;
  size_t dropped = 0;
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
} meshLog;

void meshLogSink(painlessmesh::logger::LogLevel type, const char *message) {
  portENTER_CRITICAL(&meshLog.mux);
  if (meshLog.count == MESH_LOG_LINES) {
    meshLog.dropped++;
    portEXIT_CRITICAL(&meshLog.mux);
    return;
  }
  size_t slot = (meshLog.head + meshLog.count) % MESH_LOG_LINES;
  strncpy(meshLog.lines[slot], message, MESH_LOG_LINE_MAX - 1);
  meshLog.lines[slot][MESH_LOG_LINE_MAX - 1] = '\0';
  meshLog.levels[slot] = type;
  meshLog.stamps[slot] = millis();
  meshLog.count++;
  portEXIT_CRITICAL(&meshLog.mux);
}

const char *meshLogLevelName(uint16_t type) {
  using namespace painlessmesh::logger;
  switch (type) {
    case ERROR: return "ERROR";
    case STARTUP: return "STARTUP";
    case MESH_STATUS: return "MESH_STATUS";
    case CONNECTION: return "CONNECTION";
    case SYNC: return "SYNC";
    case S_TIME: return "S_TIME";
    case COMMUNICATION: return "COMMUNICATION";
    case GENERAL: return "GENERAL";
    case MSG_TYPES: return "MSG_TYPES";
    case REMOTE: return "REMOTE";
    case APPLICATION: return "APPLICATION";
    case DEBUG: return "DEBUG";
  }
  return "OTHER";
}

// Two lines per pass: a scan logs a dozen at once, and each frame costs a
// flush at 115200 baud that mesh.update() should not wait long for.
void drainMeshLog() {
  for (int n = 0; n < 2; ++n) {
    char line[MESH_LOG_LINE_MAX];
    uint16_t level;
    uint32_t stamp;
    size_t dropped;
    portENTER_CRITICAL(&meshLog.mux);
    if (meshLog.count == 0) {
      portEXIT_CRITICAL(&meshLog.mux);
      return;
    }
    memcpy(line, meshLog.lines[meshLog.head], MESH_LOG_LINE_MAX);
    level = meshLog.levels[meshLog.head];
    stamp = meshLog.stamps[meshLog.head];
    meshLog.head = (meshLog.head + 1) % MESH_LOG_LINES;
    meshLog.count--;
    dropped = meshLog.dropped;
    meshLog.dropped = 0;
    portEXIT_CRITICAL(&meshLog.mux);
    size_t len = strlen(line);
    while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) {
      line[--len] = '\0';
    }
    JsonDocument doc;
    doc["evt"] = "mesh_log";
    doc["ms"] = stamp;
    doc["level"] = meshLogLevelName(level);
    doc["line"] = line;
    if (dropped > 0) doc["dropped"] = dropped;
    emitEvent(doc, /*flushNow=*/false);
  }
}
#endif

void handleInfo() {
  JsonDocument doc;
  doc["evt"] = "info";
  doc["nodeId"] = mesh.getNodeId();
  doc["version"] = HIL_AGENT_VERSION;
  doc["target"] = HIL_ARTIFACT_TARGET;
  doc["painlessMeshRef"] = HIL_PAINLESSMESH_REF;
  doc["hilAgentSha"] = HIL_AGENT_SHA;
  doc["bootId"] = bootId;
  doc["freeHeap"] = ESP.getFreeHeap();
  doc["meshPrefix"] = activeMeshPrefix;
  doc["otaGeneration"] = HIL_OTA_GENERATION;
  emitEvent(doc);
}

void emitOtaEvent(const char *eventName, bool ok = true) {
  JsonDocument doc;
  doc["evt"] = eventName;
  doc["ok"] = ok;
  doc["bytes"] = otaUploadedSize;
  emitEvent(doc);
}

void handleOtaReceiveEnable(JsonDocument &cmd) {
  String role = cmd["role"].as<String>();
  if (role.length() == 0 || role.length() > 31) {
    emitError("ota_receive_enable requires a 1..31 character role");
    return;
  }
  HIL_FS_BEGIN();
  HIL_FS.remove("/ota_fw.json");
  rolePreferences.begin("hil-role", false);
  rolePreferences.putBool("otaReceive", true);
  rolePreferences.putString("otaRole", role);
  rolePreferences.end();
  JsonDocument doc;
  doc["evt"] = "ota_receiver_restarting";
  doc["role"] = role;
  emitEvent(doc);
  consoleFlush();
  restartAgent();
}

void handleOtaUploadBegin(JsonDocument &cmd) {
  size_t size = cmd["size"] | (size_t)0;
  String md5 = cmd["md5"].as<String>();
  String role = cmd["role"].as<String>();
  if (size == 0 || md5.length() != 32 || role.length() == 0 ||
      role.length() > 31) {
    emitError("ota_upload_begin requires size, MD5, and role");
    return;
  }
  if (!HIL_FS_BEGIN()) {
    emitError("unable to mount the filesystem for OTA source");
    return;
  }
  if (otaUploadFile) otaUploadFile.close();
  HIL_FS.remove(HIL_OTA_FILE);
  otaUploadFile = HIL_FS.open(HIL_OTA_FILE, HIL_FILE_WRITE);
  if (!otaUploadFile) {
    emitError("unable to create OTA source file");
    return;
  }
  otaExpectedSize = size;
  otaUploadedSize = 0;
  otaLastChunkOffset = 0;
  otaLastChunkLength = 0;
  otaExpectedMd5 = md5;
  otaSourceRole = role;
  otaLastChunkMd5 = "";
  emitOtaEvent("ota_upload_ready");
}

void handleOtaUploadChunk(JsonDocument &cmd) {
  String encoded = cmd["data"].as<String>();
  size_t offset = cmd["offset"] | (size_t)-1;
  size_t expectedLength = cmd["length"] | (size_t)0;
  String expectedMd5 = cmd["md5"].as<String>();
  if (!otaUploadFile || encoded.length() == 0) {
    emitError("invalid OTA upload chunk or offset");
    return;
  }
  auto decoded = painlessmesh::base64::decode(encoded);
  MD5Builder chunkMd5;
  chunkMd5.begin();
  chunkMd5.add(reinterpret_cast<uint8_t *>(const_cast<char *>(decoded.c_str())),
               decoded.length());
  chunkMd5.calculate();
  if (decoded.length() != expectedLength || expectedMd5.length() != 32 ||
      !chunkMd5.toString().equalsIgnoreCase(expectedMd5)) {
    emitOtaEvent("ota_upload_retry", false);
    return;
  }
  // A USB-UART frame can be damaged after the board has committed the chunk
  // but before the host parses its acknowledgement.  Treat an exact retry of
  // the last committed chunk as idempotent; the final whole-image MD5 remains
  // authoritative and the file is never written twice.
  if (offset == otaLastChunkOffset && expectedLength == otaLastChunkLength &&
      expectedMd5.equalsIgnoreCase(otaLastChunkMd5) &&
      offset + expectedLength == otaUploadedSize) {
    emitOtaEvent("ota_upload_chunk");
    return;
  }
  if (offset != otaUploadedSize) {
    emitError("invalid OTA upload chunk or offset");
    return;
  }
  size_t written = otaUploadFile.write(
      reinterpret_cast<const uint8_t *>(decoded.c_str()), decoded.length());
  if (written != decoded.length()) {
    emitError("OTA source write failed");
    return;
  }
  otaLastChunkOffset = offset;
  otaLastChunkLength = written;
  otaLastChunkMd5 = expectedMd5;
  otaUploadedSize += written;
  emitOtaEvent("ota_upload_chunk");
}

void handleOtaUploadFinish() {
  if (otaUploadFile) otaUploadFile.close();
  if (otaUploadedSize != otaExpectedSize) {
    emitError("OTA source size mismatch");
    return;
  }
  auto source = HIL_FS.open(HIL_OTA_FILE, HIL_FILE_READ);
  if (!source) {
    emitError("unable to verify OTA source file");
    return;
  }
  MD5Builder md5;
  md5.begin();
  md5.addStream(source, source.size());
  md5.calculate();
  source.close();
  otaSourceMd5 = md5.toString();
  if (!otaSourceMd5.equalsIgnoreCase(otaExpectedMd5)) {
    emitError("OTA source MD5 mismatch");
    return;
  }
  emitOtaEvent("ota_upload_verified");
}

void handleOtaOffer() {
  if (otaSourceMd5.length() != 32 || otaUploadedSize != otaExpectedSize) {
    emitError("verified OTA source is required before offer");
    return;
  }
  if (otaSourceFile) otaSourceFile.close();
  otaSourceFile = HIL_FS.open(HIL_OTA_FILE, HIL_FILE_READ);
  if (!otaSourceFile) {
    emitError("unable to open OTA source for mesh transfer");
    return;
  }
  mesh.initOTASend(
      [](painlessmesh::plugin::ota::DataRequest pkg, char *buffer) {
        size_t offset = HIL_OTA_PART_SIZE * pkg.partNo;
        if (offset >= otaSourceFile.size()) return (size_t)0;
        otaSourceFile.seek(offset);
        size_t remaining = otaSourceFile.size() - offset;
        return otaSourceFile.readBytes(
            buffer, remaining < HIL_OTA_PART_SIZE ? remaining : HIL_OTA_PART_SIZE);
      },
      HIL_OTA_PART_SIZE);
  size_t partCount =
      (otaSourceFile.size() + HIL_OTA_PART_SIZE - 1) / HIL_OTA_PART_SIZE;
  otaOfferTask = mesh.offerOTA(otaSourceRole, HIL_OTA_HARDWARE, otaSourceMd5,
                               partCount, true);
  JsonDocument doc;
  doc["evt"] = "ota_offered";
  doc["md5"] = otaSourceMd5;
  doc["parts"] = partCount;
  emitEvent(doc);
}

void handleNodeList() {
  JsonDocument doc;
  doc["evt"] = "node_list";
  JsonArray nodes = doc["nodes"].to<JsonArray>();
  for (auto id : mesh.getNodeList(false)) nodes.add(id);
  emitEvent(doc);
}

void deliveryEvent(uint32_t nodeId, bool delivered, uint32_t latencyMs) {
  JsonDocument doc;
  doc["evt"] = "ack";
  doc["node"] = nodeId;
  doc["delivered"] = delivered;
  doc["latencyMs"] = latencyMs;
  emitEvent(doc);
}

void emitSendResult(bool ok) {
  JsonDocument doc;
  doc["evt"] = "send_result";
  doc["ok"] = ok;
  emitEvent(doc);
}

void registerMeshCallbacks() {
  mesh.onReceive(&receivedCallback);
  mesh.onNewConnection(&newConnectionCallback);
}

void emitGatewayStatus(const char *eventName, bool initialized = true) {
  JsonDocument doc;
  doc["evt"] = eventName;
  doc["initialized"] = initialized;
  doc["isBridge"] = mesh.isBridge();
  doc["isSharedGateway"] = mesh.isSharedGatewayMode();
  doc["hasInternet"] = mesh.hasInternetConnection();
  doc["hasLocalInternet"] = mesh.hasLocalInternet();
  doc["wifiStatus"] = (int)WiFi.status();
  doc["localIP"] = WiFi.localIP().toString();
  doc["channel"] = WiFi.channel();
  const uint32_t primaryGateway = mesh.getPrimaryGateway();
  doc["primaryGateway"] = primaryGateway;
  JsonArray gateways = doc["gateways"].to<JsonArray>();
  for (auto gatewayId : mesh.getGateways()) gateways.add(gatewayId);
  // How long ago this node last heard the primary gateway's status, or -1 when
  // it has no record of it. The library trusts a status for bridgeTimeoutMs
  // (60 s); a row that must act inside that window anchors on this. The
  // `mesh_log` line saying the same is diagnostic only -- the host capture
  // keeps it in the raw log and never delivers it as an event.
  long primaryGatewayAgeMs = -1;
  for (const auto &bridge : mesh.getBridges()) {
    if (bridge.nodeId == primaryGateway) {
      primaryGatewayAgeMs = static_cast<long>(millis() - bridge.lastSeen);
      break;
    }
  }
  doc["primaryGatewayAgeMs"] = primaryGatewayAgeMs;
  emitEvent(doc);
}

void startRegularMesh() {
  rolePreferences.begin("hil-role", false);
  rolePreferences.remove("bridge");
  rolePreferences.remove("sharedGateway");
  rolePreferences.remove("failover");
  rolePreferences.remove("ssid");
  rolePreferences.remove("password");
  rolePreferences.remove("healthHost");
  rolePreferences.remove("healthPort");
  rolePreferences.putBool("reportMesh", true);
  rolePreferences.end();
  JsonDocument doc;
  doc["evt"] = "mesh_restarting";
  emitEvent(doc);
  consoleFlush();
  restartAgent();
}

void handleMeshConfigure(JsonDocument &cmd) {
  String prefix = cmd["prefix"].as<String>();
  String password = cmd["password"].as<String>();
  if (prefix.length() == 0 || prefix.length() > 31 || password.length() < 8) {
    emitError("mesh_configure requires a 1..31 character prefix and 8+ character password");
    return;
  }
  rolePreferences.begin("hil-role", false);
  rolePreferences.remove("bridge");
  rolePreferences.remove("sharedGateway");
  rolePreferences.remove("failover");
  rolePreferences.remove("ssid");
  rolePreferences.remove("password");
  rolePreferences.remove("healthHost");
  rolePreferences.remove("healthPort");
  rolePreferences.putString("meshSsid", prefix);
  rolePreferences.putString("meshPass", password);
  rolePreferences.putBool("reportMesh", true);
  rolePreferences.end();
  JsonDocument doc;
  doc["evt"] = "mesh_restarting";
  doc["meshPrefix"] = prefix;
  emitEvent(doc);
  consoleFlush();
  restartAgent();
}

void handleGatewayStart(JsonDocument &cmd) {
  String ssid = cmd["ssid"].as<String>();
  String password = cmd["password"].as<String>();
  if (ssid.length() == 0 || password.length() < 8) {
    emitError("gateway_start requires ssid and an 8+ character password");
    return;
  }
  rolePreferences.begin("hil-role", false);
  rolePreferences.putBool("bridge", true);
  rolePreferences.remove("sharedGateway");
  rolePreferences.remove("failover");
  rolePreferences.putString("ssid", ssid);
  rolePreferences.putString("password", password);
  rolePreferences.end();
  JsonDocument doc;
  doc["evt"] = "gateway_restarting";
  doc["ssidLength"] = ssid.length();
  doc["passwordLength"] = password.length();
  emitEvent(doc);
  consoleFlush();
  restartAgent();
}

void handleGatewayFailoverStart(JsonDocument &cmd) {
  String ssid = cmd["ssid"].as<String>();
  String password = cmd["password"].as<String>();
  if (ssid.length() == 0 || password.length() < 8) {
    emitError("gateway_failover_start requires ssid and an 8+ character password");
    return;
  }
  rolePreferences.begin("hil-role", false);
  rolePreferences.remove("bridge");
  rolePreferences.remove("sharedGateway");
  rolePreferences.putBool("failover", true);
  rolePreferences.putString("ssid", ssid);
  rolePreferences.putString("password", password);
  rolePreferences.end();
  JsonDocument doc;
  doc["evt"] = "gateway_failover_restarting";
  doc["ssidLength"] = ssid.length();
  doc["passwordLength"] = password.length();
  emitEvent(doc);
  restartAgent();
}

void handleSharedGatewayStart(JsonDocument &cmd) {
  String ssid = cmd["ssid"].as<String>();
  String password = cmd["password"].as<String>();
  String healthHost = cmd["healthHost"].as<String>();
  uint16_t healthPort = cmd["healthPort"] | (uint16_t)0;
  if (ssid.length() == 0 || password.length() < 8 ||
      healthHost.length() == 0 || healthPort == 0) {
    emitError("shared_gateway_start requires Wi-Fi and health endpoint settings");
    return;
  }
  rolePreferences.begin("hil-role", false);
  rolePreferences.remove("bridge");
  rolePreferences.remove("failover");
  rolePreferences.putBool("sharedGateway", true);
  rolePreferences.putString("ssid", ssid);
  rolePreferences.putString("password", password);
  rolePreferences.putString("healthHost", healthHost);
  rolePreferences.putUShort("healthPort", healthPort);
  rolePreferences.end();
  JsonDocument doc;
  doc["evt"] = "shared_gateway_restarting";
  doc["ssidLength"] = ssid.length();
  doc["passwordLength"] = password.length();
  emitEvent(doc);
  consoleFlush();
  restartAgent();
}

void handleInternetSend(JsonDocument &cmd) {
  String tag = cmd["tag"].as<String>();
  String url = cmd["url"].as<String>();
  String payload = cmd["payload"] | "";
  uint8_t priority = cmd["priority"] | (uint8_t)2;
  if (tag.length() == 0 || url.length() == 0 || priority > 3) {
    emitError("internet_send requires tag, url, and priority 0..3");
    return;
  }
#ifdef PAINLESSMESH_HAS_INTERNET_RESULT
  // painlessMesh #464 and later: the whole result, so a row can read what
  // the service said and how many requests the call took. Rows tell the two
  // event shapes apart by the presence of "attempts".
  uint32_t messageId = mesh.sendToInternet(
      url, payload,
      [tag](const painlessmesh::InternetResult &result) {
        JsonDocument doc;
        doc["evt"] = "internet_result";
        doc["tag"] = tag;
        doc["success"] = result.success;
        doc["httpStatus"] = result.httpStatus;
        doc["error"] = result.error;
        doc["response"] = result.response;
        doc["retryable"] = result.retryable;
        doc["attempts"] = result.attempts;
        emitEvent(doc);
      },
      priority);
#else
  uint32_t messageId = mesh.sendToInternet(
      url, payload,
      [tag](bool success, uint16_t httpStatus, String error) {
        JsonDocument doc;
        doc["evt"] = "internet_result";
        doc["tag"] = tag;
        doc["success"] = success;
        doc["httpStatus"] = httpStatus;
        doc["error"] = error;
        emitEvent(doc);
      },
      priority);
#endif
  JsonDocument doc;
  doc["evt"] = "internet_queued";
  doc["tag"] = tag;
  doc["messageId"] = messageId;
  emitEvent(doc);
}

void handleSendSingle(JsonDocument &cmd) {
  uint32_t dest = cmd["dest"];
  String msg = cmd["msg"].as<String>();
  bool ack = cmd["ack"] | false;
  uint32_t ackTimeoutMs = cmd["ackTimeoutMs"] | (uint32_t)5000;
  bool hasPriority = !cmd["priority"].isNull();
  uint8_t priority = cmd["priority"] | (uint8_t)2;
  bool ok;
  if (ack) {
    ok = mesh.sendSingle(dest, msg, &deliveryEvent, ackTimeoutMs);
  } else if (hasPriority) {
    ok = mesh.sendSingle(dest, msg, priority);
  } else {
    ok = mesh.sendSingle(dest, msg);
  }
  emitSendResult(ok);
}

void handleSendBroadcast(JsonDocument &cmd) {
  String msg = cmd["msg"].as<String>();
  bool ack = cmd["ack"] | false;
  bool includeSelf = cmd["includeSelf"] | false;
  uint32_t ackTimeoutMs = cmd["ackTimeoutMs"] | (uint32_t)5000;
  bool hasPriority = !cmd["priority"].isNull();
  uint8_t priority = cmd["priority"] | (uint8_t)2;
  bool ok;
  if (ack) {
    ok = mesh.sendBroadcast(msg, includeSelf, &deliveryEvent, ackTimeoutMs);
  } else if (hasPriority) {
    ok = mesh.sendBroadcast(msg, priority, includeSelf);
  } else {
    ok = mesh.sendBroadcast(msg, includeSelf);
  }
  emitSendResult(ok);
}

void handleStall(JsonDocument &cmd) {
  uint32_t ms = cmd["ms"] | (uint32_t)1000;
  JsonDocument doc;
  doc["evt"] = "stalled";
  doc["ms"] = ms;
  emitEvent(doc);
  consoleFlush();
  stallUntil = millis() + ms;
}

void handleCommandLine(const String &line) {
  JsonDocument cmd;
  auto err = deserializeJson(cmd, line);
  if (err) {
    emitError("bad json");
    return;
  }
  const char *name = cmd["cmd"] | "";
  if (strcmp(name, "info") == 0) {
    handleInfo();
  } else if (strcmp(name, "node_list") == 0) {
    handleNodeList();
  } else if (strcmp(name, "send_single") == 0) {
    handleSendSingle(cmd);
  } else if (strcmp(name, "send_broadcast") == 0) {
    handleSendBroadcast(cmd);
  } else if (strcmp(name, "gateway_start") == 0) {
    handleGatewayStart(cmd);
  } else if (strcmp(name, "gateway_status") == 0) {
    emitGatewayStatus("gateway_status");
  } else if (strcmp(name, "shared_gateway_start") == 0) {
    handleSharedGatewayStart(cmd);
  } else if (strcmp(name, "gateway_failover_start") == 0) {
    handleGatewayFailoverStart(cmd);
  } else if (strcmp(name, "internet_send") == 0) {
    handleInternetSend(cmd);
  } else if (strcmp(name, "mesh_configure") == 0) {
    handleMeshConfigure(cmd);
  } else if (strcmp(name, "mesh_start") == 0) {
    startRegularMesh();
  } else if (strcmp(name, "stall") == 0) {
    handleStall(cmd);
  } else if (strcmp(name, "ota_receive_enable") == 0) {
    handleOtaReceiveEnable(cmd);
  } else if (strcmp(name, "ota_upload_begin") == 0) {
    handleOtaUploadBegin(cmd);
  } else if (strcmp(name, "ota_upload_chunk") == 0) {
    handleOtaUploadChunk(cmd);
  } else if (strcmp(name, "ota_upload_finish") == 0) {
    handleOtaUploadFinish();
  } else if (strcmp(name, "ota_offer") == 0) {
    handleOtaOffer();
  } else {
    emitError("unknown cmd");
  }
}

// Bounded per call. An uncabled socket leaves its RX pin floating, and a
// floating line delivers framing noise indefinitely: an unbounded drain would
// then never return, starving mesh.update() and the other console. The budget
// is far more than a real command and far less than a stuck port can produce.
// A command that reaches this board damaged must be *said* to have been
// dropped, so the host can resend it: the host's only other signal is a
// timeout, and a timeout cannot tell a lost command from a lost reply. Two
// ways a frame used to vanish without a word — the ESP8266 lost a role
// change to each of them, and the whole module's fixture failed with the
// board healthy:
//   - its tail, newline included, never arrived: the head sat in the buffer
//     until the *next* command's newline fused the two into one "bad json";
//   - String::concat failed on a board down to 8 K of heap, which returns
//     false and keeps the old contents, and the return value was ignored.
void pumpConsole(Stream &port, ConsoleLine &line) {
  for (int budget = 2048; budget > 0 && port.available(); --budget) {
    char c = (char)port.read();
    line.lastByteMs = millis();
    if (c == '\n') {
      line.buffer.trim();
      if (line.buffer.length() > 0) handleCommandLine(line.buffer);
      line.buffer = "";
    } else if (!line.buffer.concat(c)) {
      emitError("frame dropped: no memory");
      line.buffer = "";
    } else if (line.buffer.length() > 4096) {
      emitError("frame dropped: runaway");
      line.buffer = "";
    }
  }
  if (line.buffer.length() > 0 &&
      millis() - line.lastByteMs > CONSOLE_LINE_STALE_MS) {
    emitError("frame dropped: incomplete");
    line.buffer = "";
  }
}

void pumpSerial() {
  pumpConsole(Serial, serialLine);
#if HIL_HAS_SECOND_CONSOLE
  pumpConsole(HIL_SECOND_CONSOLE, secondLine);
#endif
}

// A command already in the receive buffer outranks any diagnostic waiting to
// go out: the host is blocked on the reply, nothing is blocked on a log line.
bool consoleHasInput() {
#if HIL_HAS_SECOND_CONSOLE
  return Serial.available() || HIL_SECOND_CONSOLE.available();
#else
  return Serial.available();
#endif
}

void receivedCallback(uint32_t from, String &msg) {
  JsonDocument doc;
  doc["evt"] = "recv";
  doc["from"] = from;
  doc["msg"] = msg;
  emitEvent(doc);
}

void newConnectionCallback(uint32_t nodeId) {
  JsonDocument doc;
  doc["evt"] = "connection";
  doc["nodeId"] = nodeId;
  emitEvent(doc);
}

void setup() {
  bootId = hilRandom();
  // Reserve once, so a command never needs a reallocation on a board that
  // may be down to a few kilobytes of fragmented heap by the end of a suite.
  serialLine.buffer.reserve(CONSOLE_LINE_RESERVE);
#if HIL_HAS_SECOND_CONSOLE
  secondLine.buffer.reserve(CONSOLE_LINE_RESERVE);
#endif
  Serial.setRxBufferSize(2048);
  Serial.begin(115200);
#if HIL_HAS_SECOND_CONSOLE
  // The UART socket of a dual-port devkit. Started unconditionally: which
  // socket is cabled is not knowable from the firmware, and an unattached
  // UART costs nothing to keep open.
  HIL_SECOND_CONSOLE.setRxBufferSize(2048);
  HIL_SECOND_CONSOLE.begin(115200);
#endif
  // Quiet library logging: JSON protocol lines must dominate the port
  mesh.setDebugMsgTypes(ERROR);
  rolePreferences.begin("hil-role", false);
  bool bridgeRole = rolePreferences.getBool("bridge", false);
  bool sharedGatewayRole = rolePreferences.getBool("sharedGateway", false);
  bool failoverRole = rolePreferences.getBool("failover", false);
  bool reportMeshStart = rolePreferences.getBool("reportMesh", false);
  String routerSSID = rolePreferences.getString("ssid", "");
  String routerPassword = rolePreferences.getString("password", "");
  String healthHost = rolePreferences.getString("healthHost", "8.8.8.8");
  uint16_t healthPort = rolePreferences.getUShort("healthPort", 53);
  bool otaReceive = rolePreferences.getBool("otaReceive", false);
  String otaRole = rolePreferences.getString("otaRole", "");
  activeMeshPrefix = rolePreferences.getString("meshSsid", HIL_MESH_PREFIX);
  activeMeshPassword = rolePreferences.getString("meshPass", HIL_MESH_PASSWORD);
  if (reportMeshStart) {
    rolePreferences.remove("reportMesh");
  }
  rolePreferences.end();

  bool initialized = true;
  if (sharedGatewayRole) {
    mesh.setDebugMsgTypes(ERROR | STARTUP | CONNECTION);
    painlessmesh::gateway::SharedGatewayConfig config;
    config.enabled = true;
    config.routerSSID = routerSSID;
    config.routerPassword = routerPassword;
    config.internetCheckHost = healthHost;
    config.internetCheckPort = healthPort;
    config.internetCheckInterval = 5000;
    config.internetCheckTimeout = 2000;
    initialized = mesh.initAsSharedGateway(
        activeMeshPrefix, activeMeshPassword, routerSSID, routerPassword,
        &userScheduler, HIL_MESH_PORT, config);
  } else if (bridgeRole) {
    // Preserve painlessMesh bridge diagnostics in the serial artifact.  The
    // host parser ignores non-JSON lines, while the raw log makes upstream
    // association and reconnect failures explainable in CI reports.
    mesh.setDebugMsgTypes(ERROR | STARTUP | CONNECTION);
    initialized = mesh.initAsBridge(
        activeMeshPrefix, activeMeshPassword, routerSSID, routerPassword,
        &userScheduler, HIL_MESH_PORT);
  } else {
    // Gateway bridges follow the upstream router's channel.  Channel 0 enables
    // painlessMesh's documented auto-detection path, allowing regular nodes
    // and failover candidates to follow a promoted bridge instead of forming
    // a separate mesh on the default channel 1.
#ifdef ESP8266
    // The ESP8266 is specified as a leaf in a mesh this size, and on this
    // rig that is now a fact rather than a warning: station-only, no AP,
    // no children. With an AP it was an interior node in every run —
    // peers picked it as parent five to fifteen times a suite — and as
    // the relay for a subtree its heap fell to 2.8 KB, below anything
    // that can forward an OTA announce; the receiver behind it never saw
    // one. The AP and its DHCP server were also several KB of that heap.
    // painlessMesh's own leaf configuration is init() with WIFI_STA.
    mesh.init(activeMeshPrefix, activeMeshPassword, &userScheduler,
              HIL_MESH_PORT, WIFI_STA, 0);
#else
    mesh.init(activeMeshPrefix, activeMeshPassword, &userScheduler,
              HIL_MESH_PORT, WIFI_AP_STA, 0);
#endif
    // This mesh is meant to contain a bridge, and painlessMesh asks every
    // node of such a mesh to say so (mesh.hpp, setContainsRoot). The flag is
    // local, not propagated, and it is what lets a node that is still
    // connected — to a partition the bridge has left — notice it has no root
    // and go looking for the channel the bridge moved to. Without it, only
    // nodes that lost their station link ever re-detected, and a suite's
    // bridge start stranded the rest on the old channel.
    mesh.setContainsRoot(true);
    if (failoverRole) {
      mesh.setRouterCredentials(routerSSID, routerPassword);
      mesh.enableBridgeFailover(true);
      mesh.setElectionStartupDelay(5000);
      mesh.setElectionTimeout(5000);
    }
  }
  mesh.enableSendToInternet();
  if (otaReceive && otaRole.length() > 0) {
    mesh.initOTAReceive(otaRole, [](int part, int total) {
      if (part == 0 || part == total - 1 || part % 64 == 0) {
        JsonDocument progress;
        progress["evt"] = "ota_progress";
        progress["part"] = part;
        progress["total"] = total;
        emitEvent(progress);
      }
    });
  }
  registerMeshCallbacks();

  // initAsBridge/initAsSharedGateway diagnostics are useful during the
  // blocking initialization above, but asynchronous Wi-Fi callbacks can
  // otherwise splice text into a JSON control event byte-for-byte.
  mesh.setDebugMsgTypes(ERROR);
#ifdef ESP32
  if (!bridgeRole && !sharedGatewayRole) {
    // A regular node's radio state is the evidence every failover failure
    // has lacked. Through the sink it costs no frame integrity, so the
    // connection log is on; a bridge keeps writing straight to Serial
    // because its blocking init wants the lines as they happen.
    Log.setSink(meshLogSink);
    // SYNC as well as CONNECTION: nodes were seen connecting to one peer
    // after another, each link established and dropped within a second, and
    // CONNECTION alone never says why. The two reasons painlessMesh closes a
    // fresh link — an invalid subtree, or already being connected to that
    // node — are both logged at SYNC. It costs about one line per connection
    // per nodeSync interval when the mesh is settled.
    // GENERAL carries the bridge and gateway diagnostics — "Broadcasting
    // status", "Bridge status received from %u" — which are what separate
    // "the promoted bridge never announced itself" from "it announced and
    // nobody heard". They were unusable while a per-tick scheduler trace
    // also sat at GENERAL; that trace is DEBUG now.
    mesh.setDebugMsgTypes(ERROR | CONNECTION | SYNC | GENERAL);
  }
#endif

  JsonDocument doc;
  doc["evt"] = "boot";
  doc["nodeId"] = mesh.getNodeId();
  doc["version"] = HIL_AGENT_VERSION;
  doc["target"] = HIL_ARTIFACT_TARGET;
  doc["painlessMeshRef"] = HIL_PAINLESSMESH_REF;
  doc["hilAgentSha"] = HIL_AGENT_SHA;
  doc["bootId"] = bootId;
  doc["meshPrefix"] = activeMeshPrefix;
  doc["otaGeneration"] = HIL_OTA_GENERATION;
  emitEvent(doc);
  if (sharedGatewayRole) {
    emitGatewayStatus("shared_gateway_started", initialized);
  } else if (bridgeRole) {
    emitGatewayStatus("gateway_started", initialized);
  } else if (failoverRole) {
    emitGatewayStatus("gateway_failover_started", initialized);
  } else if (reportMeshStart) {
    emitGatewayStatus("mesh_started", initialized);
  }
}

void loop() {
  if (stallUntil != 0) {
    if ((int32_t)(millis() - stallUntil) < 0) {
      delay(5);  // deliberately NOT servicing mesh.update() or serial
      return;
    }
    stallUntil = 0;
  }
  mesh.update();
#ifdef ESP8266
  // The ESP8266 is specified for small meshes, or as a leaf in larger ones:
  // as an interior node of a seven-node mesh it runs at 10–13 KB free, and
  // an 8 KB package or an OTA part can then fail to allocate. That is the
  // part's limit, not a defect, and the rig must record when a run has put
  // the board outside its envelope — so the condition is a structured
  // event, not only the library's ERROR line. Computed here rather than
  // read from the library so this agent builds against any painlessMesh
  // ref the farm is asked to validate.
  static uint32_t capacityCheckedAt = 0;
  if (millis() - capacityCheckedAt > 30000) {
    capacityCheckedAt = millis();
    size_t children = 0;
    for (auto &sub : mesh.subs) {
      if (sub && sub->connected() && !sub->station) ++children;
    }
    uint32_t freeHeap = ESP.getFreeHeap();
    if (freeHeap < 12 * 1024 && children > 1) {
      JsonDocument doc;
      doc["evt"] = "capacity_warning";
      doc["freeHeap"] = freeHeap;
      doc["apChildren"] = children;
      doc["spec"] = "esp8266: leaf only in meshes this size";
      emitEvent(doc);
    }
  }
#endif
  // Commands first, then diagnostics, and only while nothing is waiting to be
  // read. Draining before pumping let a burst of scan logging sit in front of
  // a command that had already arrived.
  pumpSerial();
#ifdef ESP32
  if (!consoleHasInput()) drainMeshLog();
#endif
}
