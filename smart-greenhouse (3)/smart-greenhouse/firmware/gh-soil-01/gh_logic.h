// gh_logic.h - чистая логика устройств БЕЗ зависимостей от Arduino.
// Тестируется на хосте (firmware/tests/test_logic.cpp), используется всеми тремя узлами.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

namespace gh {

inline float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

// ---------------------------------------------------------------- мини-JSON
// Команды и конфигурация - ПЛОСКИЕ JSON-объекты, поэтому достаточно лёгкого разбора без ArduinoJson.
inline const char* jsonFindKey(const char* json, const char* key) {
  if (!json || !key) return nullptr;
  char pat[48];
  int n = snprintf(pat, sizeof(pat), "\"%s\"", key);
  if (n <= 0 || n >= (int)sizeof(pat)) return nullptr;
  const char* p = json;
  while ((p = strstr(p, pat)) != nullptr) {
    const char* q = p + n;
    while (*q == 32 || *q == 9) q++;
    if (*q == 58) {
      q++;
      while (*q == 32 || *q == 9) q++;
      return q;
    }
    p += n;  // это было значением строки, а не ключом
  }
  return nullptr;
}

inline bool jsonGetNum(const char* json, const char* key, double& out) {
  const char* v = jsonFindKey(json, key);
  if (!v) return false;
  if (*v == 34) v++;
  char* end = nullptr;
  double d = strtod(v, &end);
  if (end == v || !isfinite(d)) return false;
  out = d;
  return true;
}

inline bool jsonGetStr(const char* json, const char* key, char* out, size_t n) {
  if (!out || n == 0) return false;
  out[0] = 0;
  const char* v = jsonFindKey(json, key);
  if (!v || *v != 34) return false;
  v++;
  size_t i = 0;
  while (*v && *v != 34 && i + 1 < n) {
    if (*v == 92 && v[1]) v++;
    out[i++] = *v++;
  }
  out[i] = 0;
  return *v == 34;
}

// ---------------------------------------------------------------- датчики
inline float moisturePct(int adc, int adcMax = 4095) {
  return clampf(100.0f * (float)adc / (float)adcMax, 0.0f, 100.0f);
}

// NTC-термистор (модуль Wokwi), параметр B, АЦП 12 бит. Результат ограничен физическим диапазоном.
inline float ntcToCelsius(int adc, float beta = 3950.0f, int adcMax = 4095) {
  float a = clampf((float)adc, 1.0f, (float)(adcMax - 1));
  float r = 1.0f / ((float)adcMax / a - 1.0f);
  float c = 1.0f / (logf(r) / beta + 1.0f / 298.15f) - 273.15f;
  return clampf(c, -55.0f, 150.0f);
}

// Фоторезистор (модуль Wokwi): формула из документации Wokwi, GAMMA=0.7, RL10=50 кОм.
inline float ldrToLux(int adc, int adcMax = 4095) {
  float x = clampf((float)adc / (float)adcMax, 0.001f, 0.999f);
  float r = 10000.0f * x / (1.0f - x);
  float lux = powf(50.0f * 1000.0f * powf(10.0f, 0.7f) / r, 1.0f / 0.7f);
  return clampf(lux, 0.0f, 100000.0f);
}

// HC-SR04: длительность эха (мкс) -> расстояние (см). -1 = нет эха / вне диапазона 2..400 см.
inline float echoUsToCm(uint32_t us) {
  if (us == 0) return -1.0f;
  float cm = (float)us * 0.0343f / 2.0f;
  return (cm < 2.0f || cm > 400.0f) ? -1.0f : cm;
}

inline float levelPct(float distCm, float heightCm) {
  if (distCm < 0 || heightCm <= 0) return -1.0f;
  return clampf((heightCm - distCm) / heightCm * 100.0f, 0.0f, 100.0f);
}

inline float median3(float a, float b, float c) {
  if (a > b) { float t = a; a = b; b = t; }
  if (b > c) { float t = b; b = c; c = t; }
  if (a > b) { float t = a; a = b; b = t; }
  return b;
}

// ---------------------------------------------------------------- гистерезис
inline bool onBelow(bool cur, float v, float low, float high) {
  if (v < low) return true;
  if (v >= high) return false;
  return cur;
}
inline bool onAbove(bool cur, float v, float onThr, float offThr) {
  if (v > onThr) return true;
  if (v <= offThr) return false;
  return cur;
}

// Состояние бака: 0 = норма, 1 = мало, 2 = критично. Выход из состояния - с запасом 3 %.
inline int tankState(int prev, float level, float low, float crit) {
  if (level < 0) return prev;
  int s = level <= crit ? 2 : (level <= low ? 1 : 0);
  if (s < prev) {
    if (prev == 2 && level < crit + 3.0f) s = 2;
    else if (s == 0 && level < low + 3.0f) s = 1;
  }
  return s;
}

inline uint32_t adaptiveInterval(uint32_t base, uint32_t fast, bool active) {
  return (active && fast < base) ? fast : base;
}

// ---------------------------------------------------------------- таймер-переопределение
struct TimedOverride {
  bool active = false;
  bool state = false;
  uint32_t until = 0;  // 0 = бессрочно
  void set(bool s, uint32_t now, uint32_t durMs) {
    active = true;
    state = s;
    until = durMs ? now + durMs : 0;
    if (durMs && until == 0) until = 1;
  }
  void clear() { active = false; }
  void update(uint32_t now) {
    if (active && until != 0 && (int32_t)(now - until) >= 0) active = false;
  }
};

// ---------------------------------------------------------------- насос
class PumpController {
 public:
  enum Event { EV_NONE = 0, EV_FAILSAFE_TIMEOUT = 1, EV_LOCKED_STOP = 2 };
  bool on = false;
  TimedOverride ovr;

  bool inCooldown(uint32_t now) const { return cooling_ && (int32_t)(now - cooldownUntil_) < 0; }

  Event update(uint32_t now, bool autoMode, bool enabled, float moisture, float low, float high,
               uint32_t maxRunMs, uint32_t cooldownMs) {
    ovr.update(now);
    if (cooling_ && (int32_t)(now - cooldownUntil_) >= 0) cooling_ = false;
    Event ev = EV_NONE;
    bool want;
    if (!enabled) {
      want = false;
      if (on) ev = EV_LOCKED_STOP;
    } else if (ovr.active) {
      want = ovr.state;
    } else if (autoMode) {
      want = onBelow(on, moisture, low, high);
      if (want && !on && cooling_) want = false;
    } else {
      want = false;
    }
    if (want && !on) runStart_ = now;
    if (on && want && maxRunMs > 0 && (now - runStart_) >= maxRunMs) {
      want = false;
      ev = EV_FAILSAFE_TIMEOUT;
      cooling_ = true;
      cooldownUntil_ = now + cooldownMs;
      ovr.clear();
    }
    on = want;
    return ev;
  }

 private:
  uint32_t runStart_ = 0;
  uint32_t cooldownUntil_ = 0;
  bool cooling_ = false;
};

// ---------------------------------------------------------------- кольцевой буфер сообщений
template <size_t N, size_t L>
class MsgRing {
 public:
  uint32_t dropped = 0;
  bool empty() const { return count_ == 0; }
  size_t size() const { return count_; }
  bool push(const char* s) {
    bool ok = true;
    if (count_ == N) {
      head_ = (head_ + 1) % N;
      count_--;
      dropped++;
      ok = false;
    }
    size_t idx = (head_ + count_) % N;
    strncpy(data_[idx], s, L - 1);
    data_[idx][L - 1] = 0;
    count_++;
    return ok;
  }
  const char* front() const { return data_[head_]; }
  void pop() {
    if (count_) {
      head_ = (head_ + 1) % N;
      count_--;
    }
  }

 private:
  char data_[N][L];
  size_t head_ = 0;
  size_t count_ = 0;
};

}  // namespace gh
