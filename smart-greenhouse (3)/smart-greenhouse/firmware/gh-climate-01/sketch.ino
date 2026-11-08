// Узел микроклимата gh-climate-01: NTC-термистор + фоторезистор, форточка (серво) и досветка (светодиод).
// Новые датчики: NTC-термистор (температура), фоторезистор (освещённость).
#include <ESP32Servo.h>
#include "gh_logic.h"
#include "gh_common.h"

static const int PIN_NTC = 35;
static const int PIN_LDR = 32;
static const int PIN_SERVO = 13;
static const int PIN_LED_GROW = 25;
static const int PIN_LED_NET = 26;
static const int VENT_CLOSED_DEG = 0, VENT_OPEN_DEG = 90;

gh::Node node("gh-climate-01", "climate", "1.0.0");
Servo vent;

struct Cfg {
  float tempOpen = 28.0f;   // открыть форточку при температуре выше
  float tempClose = 25.0f;  // закрыть при температуре ниже или равной
  float luxLow = 200.0f;    // включить досветку при освещённости ниже
  float luxHigh = 400.0f;   // выключить при достижении
} cfg;

static float temperature = 25, lux = 0, lastTemp = -1000;
static int rawT = 0, rawL = 0;
static bool ventOpen = false, growOn = false;
static gh::TimedOverride ovrVent, ovrLight;
static uint32_t tSample = 0, tControl = 0;

static bool onCommand(const char* cmd, const char* p, char* reason, size_t n) {
  uint32_t now = millis();
  double d = -1;
  gh::jsonGetNum(p, "duration_s", d);
  // В автоматическом режиме команда действует ограниченное время, в ручном - бессрочно.
  uint32_t dur = d > 0 ? (uint32_t)(d * 1000.0) : (node.autoMode ? 60000u : 0u);
  if (strcmp(cmd, "vent_open") == 0) { ovrVent.set(true, now, dur); return true; }
  if (strcmp(cmd, "vent_close") == 0) { ovrVent.set(false, now, dur); return true; }
  if (strcmp(cmd, "light_on") == 0) { ovrLight.set(true, now, dur); return true; }
  if (strcmp(cmd, "light_off") == 0) { ovrLight.set(false, now, dur); return true; }
  snprintf(reason, n, "unknown_cmd");
  return false;
}

static void onConfig(const char* p) {
  double d;
  if (gh::jsonGetNum(p, "temp_open_c", d)) cfg.tempOpen = (float)d;
  if (gh::jsonGetNum(p, "temp_close_c", d)) cfg.tempClose = (float)d;
  if (gh::jsonGetNum(p, "lux_low", d)) cfg.luxLow = (float)d;
  if (gh::jsonGetNum(p, "lux_high", d)) cfg.luxHigh = (float)d;
  if (cfg.tempClose >= cfg.tempOpen) { cfg.tempOpen = 28; cfg.tempClose = 25; }
  if (cfg.luxLow >= cfg.luxHigh) { cfg.luxLow = 200; cfg.luxHigh = 400; }
}

static void readSensors() {
  long st = 0, sl = 0;
  for (int i = 0; i < 8; i++) { st += analogRead(PIN_NTC); sl += analogRead(PIN_LDR); }
  rawT = (int)(st / 8);
  rawL = (int)(sl / 8);
  temperature = gh::ntcToCelsius(rawT);
  lux = gh::ldrToLux(rawL);
}

static void sendTelemetry() {
  char m[176];
  snprintf(m, sizeof(m), "{\"temperature_c\":%.1f,\"light_lux\":%.0f,\"vent_open\":%d,\"grow_light_on\":%d,\"override\":%d}",
           temperature, lux, ventOpen ? 1 : 0, growOn ? 1 : 0, (ovrVent.active || ovrLight.active) ? 1 : 0);
  bool active = fabsf(temperature - lastTemp) >= 1.0f;  // ускоряемся только при быстром изменении
  node.publishTelemetry(m, gh::adaptiveInterval(node.intervalMs, 2000, active));
  lastTemp = temperature;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED_GROW, OUTPUT);
  pinMode(PIN_LED_NET, OUTPUT);
  analogReadResolution(12);
  vent.attach(PIN_SERVO, 500, 2400);
  vent.write(VENT_CLOSED_DEG);
  node.onCommand = onCommand;
  node.onConfig = onConfig;
  node.begin();
  readSensors();
}

void loop() {
  uint32_t now = millis();
  node.loop();
  digitalWrite(PIN_LED_NET, node.connected() ? HIGH : LOW);

  if (now - tControl >= 200) {
    tControl = now;
    readSensors();
    ovrVent.update(now);
    ovrLight.update(now);
    bool wantVent = ventOpen, wantGrow = growOn;
    if (ovrVent.active) wantVent = ovrVent.state;
    else if (node.autoMode) wantVent = gh::onAbove(ventOpen, temperature, cfg.tempOpen, cfg.tempClose);
    if (ovrLight.active) wantGrow = ovrLight.state;
    else if (node.autoMode) wantGrow = gh::onBelow(growOn, lux, cfg.luxLow, cfg.luxHigh);
    bool changed = false;
    if (wantVent != ventOpen) { ventOpen = wantVent; vent.write(ventOpen ? VENT_OPEN_DEG : VENT_CLOSED_DEG); changed = true; }
    if (wantGrow != growOn) { growOn = wantGrow; digitalWrite(PIN_LED_GROW, growOn ? HIGH : LOW); changed = true; }
    if (changed) { sendTelemetry(); tSample = now; }
  }

  bool active = fabsf(temperature - lastTemp) >= 1.0f;
  if (now - tSample >= gh::adaptiveInterval(node.intervalMs, 2000, active)) {
    tSample = now;
    sendTelemetry();
  }
  delay(2);
}
