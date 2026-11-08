// Узел бака gh-tank-01: ультразвуковой датчик уровня воды HC-SR04 + зуммер/индикатор тревоги.
// Новый датчик: HC-SR04 (в Wokwi расстояние задаётся ползунком у датчика).
#include "gh_logic.h"
#include "gh_common.h"

static const int PIN_TRIG = 5;
static const int PIN_ECHO = 18;  // на реальном железе - через делитель 5 В -> 3,3 В
static const int PIN_BUZZER = 27;
static const int PIN_LED_ALARM = 25;
static const int PIN_LED_NET = 26;

gh::Node node("gh-tank-01", "tank", "1.0.0");

struct Cfg {
  float heightCm = 100.0f;  // расстояние от датчика до дна бака
  float lowPct = 25.0f;     // уровень «мало»
  float critPct = 10.0f;    // уровень «критично»
} cfg;

static float distance = -1, level = -1;
static int state = 0, faults = 0;
static bool sensorOk = true, faultReported = false, buzzerState = false;
static uint32_t muteUntil = 0, tSample = 0, tMeasure = 0, tToggle = 0;
static bool muted = false;
static float lastSent = -1000;

static bool onCommand(const char* cmd, const char* p, char* reason, size_t n) {
  if (strcmp(cmd, "buzzer_mute") == 0) {
    double d = 60;
    gh::jsonGetNum(p, "duration_s", d);
    d = gh::clampf((float)d, 1.0f, 600.0f);
    muted = true;
    muteUntil = millis() + (uint32_t)(d * 1000.0);
    return true;
  }
  snprintf(reason, n, "unknown_cmd");
  return false;
}

static void onConfig(const char* p) {
  double d;
  if (gh::jsonGetNum(p, "tank_height_cm", d)) cfg.heightCm = gh::clampf((float)d, 10.0f, 400.0f);
  if (gh::jsonGetNum(p, "level_low_pct", d)) cfg.lowPct = (float)d;
  if (gh::jsonGetNum(p, "level_crit_pct", d)) cfg.critPct = (float)d;
  if (cfg.critPct >= cfg.lowPct) { cfg.lowPct = 25; cfg.critPct = 10; }
}

static float measureOnce() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  unsigned long us = pulseIn(PIN_ECHO, HIGH, 30000);
  return gh::echoUsToCm((uint32_t)us);
}

// Медиана из трёх замеров подавляет одиночные выбросы датчика.
static void measure() {
  float a = measureOnce(); delay(10);
  float b = measureOnce(); delay(10);
  float c = measureOnce();
  int valid = (a >= 0) + (b >= 0) + (c >= 0);
  if (valid == 0) {
    distance = -1;
    level = -1;
    if (++faults >= 3) sensorOk = false;
  } else {
    if (a < 0) a = (b >= 0 ? b : c);
    if (b < 0) b = a;
    if (c < 0) c = a;
    distance = gh::median3(a, b, c);
    level = gh::levelPct(distance, cfg.heightCm);
    faults = 0;
    if (!sensorOk) { sensorOk = true; faultReported = false; node.publishEvent("sensor_recovered", "info", "HC-SR04 echo restored"); }
  }
  state = gh::tankState(state, level, cfg.lowPct, cfg.critPct);
}

static void sendTelemetry() {
  char m[176];
  snprintf(m, sizeof(m), "{\"distance_cm\":%.1f,\"level_pct\":%.1f,\"tank_state\":%d,\"sensor_ok\":%d,\"muted\":%d}",
           distance, level, state, sensorOk ? 1 : 0, muted ? 1 : 0);
  bool active = state > 0 || fabsf(level - lastSent) >= 3.0f;
  node.publishTelemetry(m, gh::adaptiveInterval(node.intervalMs, 2000, active));
  lastSent = level;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_LED_ALARM, OUTPUT);
  pinMode(PIN_LED_NET, OUTPUT);
  node.onCommand = onCommand;
  node.onConfig = onConfig;
  node.begin();
  measure();
}

void loop() {
  uint32_t now = millis();
  node.loop();
  digitalWrite(PIN_LED_NET, node.connected() ? HIGH : LOW);
  if (muted && (int32_t)(now - muteUntil) >= 0) muted = false;

  if (now - tMeasure >= 500) {
    tMeasure = now;
    int prev = state;
    measure();
    if (!sensorOk && !faultReported) {
      faultReported = true;
      node.publishEvent("sensor_fault", "critical", "HC-SR04: no echo (3 consecutive failures)");
    }
    if (state != prev) {
      const char* sev = state == 2 ? "critical" : (state == 1 ? "warning" : "info");
      node.publishEvent("tank_state", sev, state == 2 ? "tank critical" : (state == 1 ? "tank low" : "tank ok"));
      sendTelemetry();
      tSample = now;
    }
  }

  bool alarm = state >= 1 && !muted;
  digitalWrite(PIN_LED_ALARM, state >= 1 ? HIGH : LOW);
  if (alarm && ((now / 300) % 2 == 0)) {  // прерывистый сигнал ~1 кГц
    if (micros() - tToggle >= 500) { tToggle = micros(); buzzerState = !buzzerState; digitalWrite(PIN_BUZZER, buzzerState); }
  } else {
    digitalWrite(PIN_BUZZER, LOW);
  }

  bool active = state > 0 || fabsf(level - lastSent) >= 3.0f;
  if (now - tSample >= gh::adaptiveInterval(node.intervalMs, 2000, active)) {
    tSample = now;
    sendTelemetry();
  }
  delay(1);
}
