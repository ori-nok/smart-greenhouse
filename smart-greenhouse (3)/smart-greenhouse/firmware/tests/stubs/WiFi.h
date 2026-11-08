#pragma once
#include "Arduino.h"
#define WL_CONNECTED 3
#define WL_DISCONNECTED 6
#define WIFI_STA 1
struct WiFiClient {};
struct WiFiClass {
  int status() { return g_wifi_ok ? WL_CONNECTED : WL_DISCONNECTED; }
  void mode(int) {}
  void begin(const char*, const char*, int) {}
  int RSSI() { return -55; }
};
extern WiFiClass WiFi;
inline void configTime(long, int, const char*, const char*) {}
