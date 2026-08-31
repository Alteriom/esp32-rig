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
//   {"cmd":"stall","ms":3000}     // stop servicing mesh.update() for ms
//
// board -> host events (one JSON object per line):
//   boot, info, node_list, send_result, recv, ack, connection, stalled,
//   error
//************************************************************
#include <painlessMesh.h>

#include <ArduinoJson.h>

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

Scheduler userScheduler;
painlessMesh mesh;

uint32_t stallUntil = 0;
String serialBuffer;

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
  doc["freeHeap"] = ESP.getFreeHeap();
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
  Serial.begin(115200);
  // Quiet library logging: JSON protocol lines must dominate the port
  mesh.setDebugMsgTypes(ERROR);
  mesh.init(HIL_MESH_PREFIX, HIL_MESH_PASSWORD, &userScheduler, HIL_MESH_PORT);
  mesh.onReceive(&receivedCallback);
  mesh.onNewConnection(&newConnectionCallback);

  JsonDocument doc;
  doc["evt"] = "boot";
  doc["nodeId"] = mesh.getNodeId();
  doc["version"] = HIL_AGENT_VERSION;
  doc["target"] = HIL_ARTIFACT_TARGET;
  doc["painlessMeshRef"] = HIL_PAINLESSMESH_REF;
  emitEvent(doc);
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
