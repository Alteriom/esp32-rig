// Platform shims so the HIL agent is one source for every farm family.
//
// ESP32 variants keep their role settings in NVS (Preferences) and stage OTA
// images on SPIFFS. The ESP8266 has neither: its role store is a JSON file and
// its OTA staging area is LittleFS (the painlessMesh OTA plugin is built with
// USE_FS_LITTLEFS on that target, see platformio.ini).
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

#if defined(ESP8266)
#include <LittleFS.h>
#define HIL_FS LittleFS
#define HIL_FS_BEGIN() LittleFS.begin()
#define HIL_OTA_HARDWARE "ESP8266"
#define HIL_FILE_WRITE "w"
#define HIL_FILE_READ "r"
inline uint32_t hilRandom() { return RANDOM_REG32; }
#else
#include <Preferences.h>
#include <SPIFFS.h>
#define HIL_FS SPIFFS
#define HIL_FS_BEGIN() SPIFFS.begin(true)
#define HIL_OTA_HARDWARE "ESP32"
#define HIL_FILE_WRITE FILE_WRITE
#define HIL_FILE_READ FILE_READ
inline uint32_t hilRandom() { return esp_random(); }
#endif

// Preferences-shaped key/value store for the agent's role settings.
class RoleStore {
 public:
#if defined(ESP8266)
  static constexpr const char *kPath = "/hil-role.json";

  void begin(const char *, bool readOnly = false) {
    readOnly_ = readOnly;
    doc_.clear();
    if (!HIL_FS_BEGIN()) return;
    File file = HIL_FS.open(kPath, HIL_FILE_READ);
    if (file) {
      deserializeJson(doc_, file);
      file.close();
    }
  }
  void end() {
    if (readOnly_ || !dirty_) return;
    File file = HIL_FS.open(kPath, HIL_FILE_WRITE);
    if (!file) return;
    serializeJson(doc_, file);
    file.close();
    dirty_ = false;
  }
  bool getBool(const char *key, bool fallback) { return doc_[key] | fallback; }
  String getString(const char *key, const String &fallback) {
    return doc_[key].isNull() ? fallback : doc_[key].as<String>();
  }
  uint16_t getUShort(const char *key, uint16_t fallback) { return doc_[key] | fallback; }
  void putBool(const char *key, bool value) { doc_[key] = value; dirty_ = true; }
  void putString(const char *key, const String &value) { doc_[key] = value; dirty_ = true; }
  void putUShort(const char *key, uint16_t value) { doc_[key] = value; dirty_ = true; }
  void remove(const char *key) {
    if (!doc_[key].isNull()) {
      doc_.remove(key);
      dirty_ = true;
    }
  }

 private:
  JsonDocument doc_;
  bool readOnly_ = false;
  bool dirty_ = false;
#else
  void begin(const char *ns, bool readOnly = false) { prefs_.begin(ns, readOnly); }
  void end() { prefs_.end(); }
  bool getBool(const char *key, bool fallback) { return prefs_.getBool(key, fallback); }
  String getString(const char *key, const String &fallback) { return prefs_.getString(key, fallback); }
  uint16_t getUShort(const char *key, uint16_t fallback) { return prefs_.getUShort(key, fallback); }
  void putBool(const char *key, bool value) { prefs_.putBool(key, value); }
  void putString(const char *key, const String &value) { prefs_.putString(key, value); }
  void putUShort(const char *key, uint16_t value) { prefs_.putUShort(key, value); }
  void remove(const char *key) { prefs_.remove(key); }

 private:
  Preferences prefs_;
#endif
};
