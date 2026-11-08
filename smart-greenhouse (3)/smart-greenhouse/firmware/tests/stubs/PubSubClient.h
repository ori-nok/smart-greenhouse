#pragma once
#include "Arduino.h"
#include "WiFi.h"
// Заглушка с сигнатурами реальной PubSubClient 2.8 (ESP32); буфер по умолчанию 256 - проверяем setBufferSize.
class PubSubClient {
 public:
  typedef std::function<void(char*, uint8_t*, unsigned int)> CB;
  PubSubClient(WiFiClient&) { inst() = this; }
  static PubSubClient*& inst() { static PubSubClient* p = nullptr; return p; }
  PubSubClient& setServer(const char*, uint16_t) { return *this; }
  PubSubClient& setCallback(CB cb) { cb_ = cb; return *this; }
  PubSubClient& setKeepAlive(uint16_t) { return *this; }
  PubSubClient& setSocketTimeout(uint16_t) { return *this; }
  bool setBufferSize(uint16_t s) { bufsz_ = s; return true; }
  bool connect(const char* id, const char*, const char*, const char* wt, uint8_t wq, boolean wr, const char* wm) {
    if (!g_broker_up) return false;
    connected_ = true;
    printf("CONN\t%s\t%s\t%d\t%d\t%s\n", id, wt, wq, wr, wm);
    return true;
  }
  boolean connected() { if (!g_broker_up) connected_ = false; return connected_; }
  boolean loop() { return connected(); }
  boolean subscribe(const char* t, uint8_t q = 0) { if (!connected_) return false; printf("SUB\t%s\t%d\n", t, q); return true; }
  boolean publish(const char* t, const char* p) { return publish(t, p, false); }
  boolean publish(const char* t, const char* p, boolean r) {
    if (!connected()) return false;
    if (strlen(t) + strlen(p) + 5 > bufsz_) { printf("OVERSIZE\t%s\n", t); return false; }
    printf("PUB\t%s\t%d\t%s\n", t, (int)r, p);
    return true;
  }
  void inject(const char* topic, const char* payload) {
    char t[160]; strncpy(t, topic, sizeof t - 1); t[sizeof t - 1] = 0;
    cb_(t, (uint8_t*)payload, (unsigned)strlen(payload));
  }
 private:
  CB cb_; bool connected_ = false; uint16_t bufsz_ = 256;
};
