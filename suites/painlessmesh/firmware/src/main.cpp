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
#include <Preferences.h>

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

Scheduler userScheduler;
painlessMesh mesh;

uint32_t stallUntil = 0;
uint32_t bootId = 0;
String serialBuffer;
Preferences rolePreferences;
String activeMeshPrefix = HIL_MESH_PREFIX;
String activeMeshPassword = HIL_MESH_PASSWORD;

void receivedCallback(uint32_t from, String &msg);
void newConnectionCallback(uint32_t nodeId);

void emitEvent(JsonDocument &doc) {
  serializeJson(doc, Serial);
  Serial.println();
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
  emitEvent(doc);
}

void startRegularMesh() {
  rolePreferences.begin("hil-role", false);
  rolePreferences.remove("bridge");
  rolePreferences.remove("sharedGateway");
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
  } else if (strcmp(name, "internet_send") == 0) {
    handleInternetSend(cmd);
  } else if (strcmp(name, "mesh_configure") == 0) {
    handleMeshConfigure(cmd);
  } else if (strcmp(name, "mesh_start") == 0) {
    startRegularMesh();
  } else if (strcmp(name, "stall") == 0) {
    handleStall(cmd);
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
      if (serialBuffer.length() > 1024) serialBuffer = "";  // runaway guard
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
  Serial.begin(115200);
  // Quiet library logging: JSON protocol lines must dominate the port
  mesh.setDebugMsgTypes(ERROR);
  rolePreferences.begin("hil-role", false);
  bool bridgeRole = rolePreferences.getBool("bridge", false);
  bool sharedGatewayRole = rolePreferences.getBool("sharedGateway", false);
  bool reportMeshStart = rolePreferences.getBool("reportMesh", false);
  String routerSSID = rolePreferences.getString("ssid", "");
  String routerPassword = rolePreferences.getString("password", "");
  String healthHost = rolePreferences.getString("healthHost", "8.8.8.8");
  uint16_t healthPort = rolePreferences.getUShort("healthPort", 53);
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
    mesh.init(activeMeshPrefix, activeMeshPassword, &userScheduler,
              HIL_MESH_PORT);
  }
  mesh.enableSendToInternet();
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
  emitEvent(doc);
  if (sharedGatewayRole) {
    emitGatewayStatus("shared_gateway_started", initialized);
  } else if (bridgeRole) {
    emitGatewayStatus("gateway_started", initialized);
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
