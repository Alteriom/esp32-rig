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
//************************************************************
#include <painlessMesh.h>

#include <ArduinoJson.h>
#include <MD5Builder.h>
#include <Preferences.h>
#include <SPIFFS.h>

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

uint32_t stallUntil = 0;
uint32_t bootId = 0;
String serialBuffer;
Preferences rolePreferences;
String activeMeshPrefix = HIL_MESH_PREFIX;
String activeMeshPassword = HIL_MESH_PASSWORD;
File otaUploadFile;
File otaSourceFile;
size_t otaExpectedSize = 0;
size_t otaUploadedSize = 0;
String otaExpectedMd5;
String otaSourceMd5;
String otaSourceRole;
std::shared_ptr<Task> otaOfferTask;

void receivedCallback(uint32_t from, String &msg);
void newConnectionCallback(uint32_t nodeId);

void emitEvent(JsonDocument &doc) {
  // Build one complete frame before writing it. This avoids damaged JSON
  // prefixes on inexpensive USB-UART bridges under concurrent mesh traffic.
  String frame;
  frame.reserve(measureJson(doc) + 1);
  serializeJson(doc, frame);
  Serial.println(frame);
  Serial.flush();
}

void emitError(const char *message) {
  JsonDocument doc;
  doc["evt"] = "error";
  doc["error"] = message;
  emitEvent(doc);
}

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
  SPIFFS.begin(true);
  SPIFFS.remove("/ota_fw.json");
  rolePreferences.begin("hil-role", false);
  rolePreferences.putBool("otaReceive", true);
  rolePreferences.putString("otaRole", role);
  rolePreferences.end();
  JsonDocument doc;
  doc["evt"] = "ota_receiver_restarting";
  doc["role"] = role;
  emitEvent(doc);
  Serial.flush();
  delay(200);
  ESP.restart();
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
  if (!SPIFFS.begin(true)) {
    emitError("unable to mount SPIFFS for OTA source");
    return;
  }
  if (otaUploadFile) otaUploadFile.close();
  SPIFFS.remove(HIL_OTA_FILE);
  otaUploadFile = SPIFFS.open(HIL_OTA_FILE, FILE_WRITE);
  if (!otaUploadFile) {
    emitError("unable to create OTA source file");
    return;
  }
  otaExpectedSize = size;
  otaUploadedSize = 0;
  otaExpectedMd5 = md5;
  otaSourceRole = role;
  emitOtaEvent("ota_upload_ready");
}

void handleOtaUploadChunk(JsonDocument &cmd) {
  String encoded = cmd["data"].as<String>();
  size_t offset = cmd["offset"] | (size_t)-1;
  if (!otaUploadFile || encoded.length() == 0 || offset != otaUploadedSize) {
    emitError("invalid OTA upload chunk or offset");
    return;
  }
  auto decoded = painlessmesh::base64::decode(encoded);
  size_t written = otaUploadFile.write(
      reinterpret_cast<const uint8_t *>(decoded.c_str()), decoded.length());
  if (written != decoded.length()) {
    emitError("OTA source write failed");
    return;
  }
  otaUploadedSize += written;
  emitOtaEvent("ota_upload_chunk");
}

void handleOtaUploadFinish() {
  if (otaUploadFile) otaUploadFile.close();
  if (otaUploadedSize != otaExpectedSize) {
    emitError("OTA source size mismatch");
    return;
  }
  auto source = SPIFFS.open(HIL_OTA_FILE, FILE_READ);
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
  otaSourceFile = SPIFFS.open(HIL_OTA_FILE, FILE_READ);
  if (!otaSourceFile) {
    emitError("unable to open OTA source for mesh transfer");
    return;
  }
  mesh.initOTASend(
      [](painlessmesh::plugin::ota::DataRequest pkg, char *buffer) {
        size_t offset = HIL_OTA_PART_SIZE * pkg.partNo;
        if (offset >= otaSourceFile.size()) return (size_t)0;
        otaSourceFile.seek(offset);
        return otaSourceFile.readBytes(
            buffer,
            min(HIL_OTA_PART_SIZE, otaSourceFile.size() - offset));
      },
      HIL_OTA_PART_SIZE);
  size_t partCount =
      (otaSourceFile.size() + HIL_OTA_PART_SIZE - 1) / HIL_OTA_PART_SIZE;
  otaOfferTask = mesh.offerOTA(otaSourceRole, "ESP32", otaSourceMd5,
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
  doc["primaryGateway"] = mesh.getPrimaryGateway();
  JsonArray gateways = doc["gateways"].to<JsonArray>();
  for (auto gatewayId : mesh.getGateways()) gateways.add(gatewayId);
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
  Serial.flush();
  delay(200);
  ESP.restart();
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
  Serial.flush();
  delay(200);
  ESP.restart();
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
  Serial.flush();
  delay(200);
  ESP.restart();
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
  delay(200);
  ESP.restart();
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
  Serial.flush();
  delay(200);
  ESP.restart();
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
  Serial.flush();
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

void pumpSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      serialBuffer.trim();
      if (serialBuffer.length() > 0) handleCommandLine(serialBuffer);
      serialBuffer = "";
    } else {
      serialBuffer += c;
      if (serialBuffer.length() > 4096) serialBuffer = "";  // runaway guard
    }
  }
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
  bootId = esp_random();
  Serial.setRxBufferSize(2048);
  Serial.begin(115200);
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
    mesh.init(activeMeshPrefix, activeMeshPassword, &userScheduler,
              HIL_MESH_PORT, WIFI_AP_STA, 0);
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
  pumpSerial();
}
