// Минимальная заглушка Arduino-ядра для сборки скетчей на хосте (только для тестов).
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <math.h>
#include <functional>
typedef uint8_t byte;
typedef bool boolean;
#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
extern uint32_t g_millis, g_subus;
extern int g_adc[64], g_dig[64];
extern uint32_t g_echo_us;
extern bool g_restart;
extern bool g_wifi_ok, g_broker_up;
inline uint32_t millis() { return g_millis; }
inline uint32_t micros() { return g_millis * 1000u + g_subus; }
inline void delay(uint32_t ms) { g_millis += ms; }
inline void delayMicroseconds(uint32_t us) { g_subus += us; if (g_subus >= 1000) { g_millis += g_subus / 1000; g_subus %= 1000; } }
inline void pinMode(int, int) {}
inline void digitalWrite(int p, int v) { g_dig[p] = v; }
inline int analogRead(int p) { return g_adc[p]; }
inline void analogReadResolution(int) {}
inline unsigned long pulseIn(int, int, unsigned long) { return g_echo_us; }
inline uint32_t esp_random() { static uint32_t x = 0x1234abcd; x = x * 1664525u + 1013904223u; return x; }
struct SerialStub {
  void begin(int) {}
  void printf(const char* f, ...) { va_list a; va_start(a, f); fprintf(stderr, "[serial] "); vfprintf(stderr, f, a); va_end(a); }
};
extern SerialStub Serial;
struct EspStub {
  uint32_t getFreeHeap() { return 200000; }
  void restart() { g_restart = true; }
};
extern EspStub ESP;
