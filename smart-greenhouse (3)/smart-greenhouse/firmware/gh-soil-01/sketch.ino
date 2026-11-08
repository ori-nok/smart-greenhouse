// Узел полива gh-soil-01: датчик влажности почвы + насос (реле).
// Новый датчик: влажность почвы (в Wokwi моделируется ползунковым потенциометром, 0..4095 -> 0..100 %).
// Локальная автоматика (гистерезис + защиты) работает автономно, даже без связи с сервером.
#include "gh_logic.h"
#include "gh_common.h"

static const int PIN_SOIL = 34;      // АЦП1, безопасен при работе Wi-Fi
static const int PIN_PUMP = 26;      // реле насоса
static const int PIN_LED_PUMP = 25;  // индикатор работы насоса
static const int PIN_LED_NET = 27;   // индикатор связи с брокером

gh::Node node("gh-soil-01", "soil", "1.0.0");
gh::PumpController pump;

struct Cfg {
  float low = 30.0f;        // включить полив, если влажность ниже
  float high = 60.0f;       // выключить полив, когда достигнута
  bool enabled = true;      // false - насос заблокирован (напр. бак пуст)
  uint32_t maxRunS = 30;    // макс. непрерывная работа насоса
  uint32_t cooldownS = 20;  // пауза после аварийной остановки
} cfg;

static float moisture = 0, lastSent = -1000;
static int raw = 0;
static bool lastPump = false;
static uint32_t tSample = 0, tControl = 0;
static bool fellBack = false;

static bool onCommand(const char* cmd, const char* p, char* reason, size_t n) {
  uint32_t now = millis();
  double d = -1;
  gh::jsonGetNum(p, "duration_s", d);
  if (strcmp(cmd, "pump_on") == 0) {
    if (!cfg.enabled) { snprintf(reason, n, "pump_locked"); return false; }
    if (d < 0) d = 20;
    d = gh::clampf((float)d, 1.0f, (float)cfg.maxRunS);
    pump.ovr.set(true, now, (uint32_t)(d * 1000.0));
    return true;
  }
  if (strcmp(cmd, "pump_off") == 0) {
    uint32_t dur = d > 0 ? (uint32_t)(d * 1000.0) : (node.autoMode ? 60000u : 0u);
    pump.ovr.set(false, now, dur);
    return true;
  }
  snprintf(reason, n, "unknown_cmd");
  return false;
}

static void onConfig(const char* p) {
  double d;
  if (gh::jsonGetNum(p, "moist_low", d)) cfg.low = (float)d;
  if (gh::jsonGetNum(p, "moist_high", d)) cfg.high = (float)d;
  if (gh::jsonGetNum(p, "pump_enabled", d)) cfg.enabled = d != 0;
  if (gh::jsonGetNum(p, "pump_max_run_s", d)) cfg.maxRunS = (uint32_t)gh::clampf((float)d, 5.0f, 600.0f);
  if (gh::jsonGetNum(p, "pump_cooldown_s", d)) cfg.cooldownS = (uint32_t)gh::clampf((float)d, 0.0f, 3600.0f);
  if (cfg.low >= cfg.high) { cfg.low = 30; cfg.high = 60; }  // защита от неверной конфигурации
}

static void readSensor() {
  long sum = 0;
  for (int i = 0; i < 8; i++) sum += analogRead(PIN_SOIL);  // усреднение шума АЦП
  raw = (int)(sum / 8);
  moisture = gh::moisturePct(raw);
}

static void sendTelemetry() {
  char m[176];
  snprintf(m, sizeof(m),
           "{\"soil_moisture_pct\":%.1f,\"soil_raw\":%d,\"pump_on\":%d,\"pump_locked\":%d,"
           "\"pump_cooldown\":%d,\"override\":%d}",
           moisture, raw, pump.on ? 1 : 0, cfg.enabled ? 0 : 1, pump.inCooldown(millis()) ? 1 : 0,
           pump.ovr.active ? 1 : 0);
  bool active = pump.on || fabsf(moisture - lastSent) >= 3.0f;  // адаптивная частота
  node.publishTelemetry(m, gh::adaptiveInterval(node.intervalMs, 2000, active));
  lastSent = moisture;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_PUMP, OUTPUT);
  pinMode(PIN_LED_PUMP, OUTPUT);
  pinMode(PIN_LED_NET, OUTPUT);
  analogReadResolution(12);
  node.onCommand = onCommand;
  node.onConfig = onConfig;
  node.begin();
  readSensor();
}

void loop() {
  uint32_t now = millis();
  node.loop();
  digitalWrite(PIN_LED_NET, node.connected() ? HIGH : LOW);

  // Безопасный режим: при длительной потере связи ручной режим сбрасывается в автоматический.
  if (!node.autoMode && node.offlineMs(now) > 60000) {
    node.autoMode = true;
    pump.ovr.clear();
    fellBack = true;
  }
  if (fellBack && node.connected()) {
    node.publishEvent("safe_mode", "warning", "manual mode reset to auto after link loss");
    fellBack = false;
  }

  if (now - tControl >= 100) {  // контур управления - 10 Гц
    tControl = now;
    readSensor();
    gh::PumpController::Event ev = pump.update(now, node.autoMode, cfg.enabled, moisture, cfg.low, cfg.high,
                                               cfg.maxRunS * 1000UL, cfg.cooldownS * 1000UL);
    if (ev == gh::PumpController::EV_FAILSAFE_TIMEOUT)
      node.publishEvent("pump_failsafe", "warning", "max run time exceeded, pump stopped");
    if (ev == gh::PumpController::EV_LOCKED_STOP)
      node.publishEvent("pump_locked_stop", "info", "pump stopped: disabled by configuration");
    digitalWrite(PIN_PUMP, pump.on ? HIGH : LOW);
    digitalWrite(PIN_LED_PUMP, pump.on ? HIGH : LOW);
    if (pump.on != lastPump) {  // смена состояния - немедленная телеметрия
      lastPump = pump.on;
      sendTelemetry();
      tSample = now;
    }
  }

  bool active = pump.on || fabsf(moisture - lastSent) >= 3.0f;
  if (now - tSample >= gh::adaptiveInterval(node.intervalMs, 2000, active)) {
    tSample = now;
    sendTelemetry();
  }
  delay(2);
}
