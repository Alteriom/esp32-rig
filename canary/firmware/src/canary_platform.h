// Platform shims so the canary is one source for every farm family.
//
// The canary carries nothing from painlessMesh or from any consumer: the
// Arduino core, ESP-IDF and ArduinoJson only. A red canary must never be a
// consumer's regression, which it could be if it shared a line of their code.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

// Console transport. The C3/C5/C6/S3 devkits carry two USB-C sockets: the
// chip's own USB-Serial/JTAG and a UART bridge. Either may be the one cabled
// to the rig, and the board gives no hint which. With ARDUINO_USB_CDC_ON_BOOT
// the Arduino `Serial` is the native USB CDC and `Serial0` is UART0, so a
// board plugged into its UART socket sees none of the protocol -- the port
// carries ESP-IDF's own logging and nothing else, which looks exactly like a
// dead board. The canary exists to tell a dead board from a miscabled one,
// so it speaks on both and accepts commands from either.
#if defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
#define CANARY_HAS_SECOND_CONSOLE 1
#define CANARY_SECOND_CONSOLE Serial0
#else
#define CANARY_HAS_SECOND_CONSOLE 0
#endif

#if defined(ESP8266)
#include <ESP8266WiFi.h>
#include <LittleFS.h>
#else
#include <Preferences.h>
#include <WiFi.h>
#include <esp_system.h>
#endif

// ---- the key/value store the flash check writes through ---------------------
//
// NVS on the ESP32 families, a LittleFS file on the ESP8266, which has no
// NVS. Either way the check is the same: write a key, read it back from a
// store that was closed and reopened in between, then erase it.
class CanaryStore {
 public:
#if defined(ESP8266)
  static constexpr const char *kPath = "/canary-store.json";

  bool begin() {
    doc_.clear();
    if (!LittleFS.begin()) return false;
    File file = LittleFS.open(kPath, "r");
    if (file) {
      deserializeJson(doc_, file);
      file.close();
    }
    return true;
  }
  bool put(const char *key, const String &value) {
    doc_[key] = value;
    File file = LittleFS.open(kPath, "w");
    if (!file) return false;
    const bool ok = serializeJson(doc_, file) > 0;
    file.close();
    return ok;
  }
  bool get(const char *key, String &out) {
    if (doc_[key].isNull()) return false;
    out = doc_[key].as<String>();
    return true;
  }
  bool erase(const char *key) {
    if (doc_[key].isNull()) return true;
    doc_.remove(key);
    File file = LittleFS.open(kPath, "w");
    if (!file) return false;
    const bool ok = serializeJson(doc_, file) > 0;
    file.close();
    return ok;
  }
  const char *backing() const { return "littlefs"; }

 private:
  JsonDocument doc_;
#else
  bool begin() { return true; }
  bool put(const char *key, const String &value) {
    Preferences prefs;
    if (!prefs.begin("canary", false)) return false;
    const bool ok = prefs.putString(key, value) == value.length();
    prefs.end();
    return ok;
  }
  bool get(const char *key, String &out) {
    Preferences prefs;
    if (!prefs.begin("canary", true)) return false;
    const bool found = prefs.isKey(key);
    if (found) out = prefs.getString(key, "");
    prefs.end();
    return found;
  }
  bool erase(const char *key) {
    Preferences prefs;
    if (!prefs.begin("canary", false)) return false;
    const bool ok = !prefs.isKey(key) || prefs.remove(key);
    prefs.end();
    return ok;
  }
  const char *backing() const { return "nvs"; }
#endif
};

// ---- what the part says about itself ---------------------------------------
// Fields a family does not have are omitted rather than reported as zero: an
// ESP8266 has no die revision and no PSRAM, and "0" would read as a part that
// answered.
inline void canaryDescribeChip(JsonObject out) {
#if defined(ESP8266)
  out["chip"] = "ESP8266";
  out["chipId"] = ESP.getChipId();
  out["cpuMhz"] = ESP.getCpuFreqMHz();
  out["flashBytes"] = ESP.getFlashChipRealSize();
  out["flashId"] = ESP.getFlashChipId();
  out["sdk"] = ESP.getSdkVersion();
  out["core"] = ESP.getCoreVersion();
#else
  out["chip"] = ESP.getChipModel();
  out["revision"] = ESP.getChipRevision();
  out["cores"] = ESP.getChipCores();
  out["cpuMhz"] = getCpuFrequencyMhz();
  out["flashBytes"] = ESP.getFlashChipSize();
  out["sdk"] = ESP.getSdkVersion();
  const uint32_t psram = ESP.getPsramSize();
  if (psram > 0) out["psramBytes"] = psram;
#if defined(ESP_ARDUINO_VERSION_STR)
  out["core"] = ESP_ARDUINO_VERSION_STR;
#endif
#endif
}

// Why it last started. The reset check asserts this changes to a host-driven
// reset, so the string matters as much as the boot id.
inline const char *canaryResetReason() {
#if defined(ESP8266)
  static String reason = ESP.getResetReason();
  return reason.c_str();
#else
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_EXT: return "external";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "interrupt-watchdog";
    case ESP_RST_TASK_WDT: return "task-watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    default: return "unknown";
  }
#endif
}

inline uint32_t canaryRandom() {
#if defined(ESP8266)
  return RANDOM_REG32;
#else
  return esp_random();
#endif
}
