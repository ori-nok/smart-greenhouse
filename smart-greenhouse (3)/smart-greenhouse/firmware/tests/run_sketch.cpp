// Хост-прогон скетча: имитация времени, АЦП, Wi-Fi/брокера и входящих сообщений.
#include "Arduino.h"
#include "WiFi.h"
uint32_t g_millis = 0, g_subus = 0, g_echo_us = 0;
int g_adc[64] = {0}, g_dig[64] = {0}, g_servo_deg = -1;
bool g_restart = false, g_wifi_ok = true, g_broker_up = true;
SerialStub Serial;
EspStub ESP;
WiFiClass WiFi;
#include "PubSubClient.h"
#include SKETCH

static void run(uint32_t ms) { uint32_t end = g_millis + ms; while (g_millis < end) loop(); }
#define U __attribute__((unused)) static
U void step(const char* n) { printf("STEP\t%s\t%u\n", n, g_millis); }
U void show(const char* n, int v) { printf("STATE\t%s\t%d\n", n, v); }
#define ROOT "gh-k7q2x9/greenhouse/"
U void cmd(const char* dev, const char* json) { char t[128]; snprintf(t, sizeof t, ROOT "%s/cmd", dev); PubSubClient::inst()->inject(t, json); }
U void cfgmsg(const char* dev, const char* json) { char t[128]; snprintf(t, sizeof t, ROOT "%s/config", dev); PubSubClient::inst()->inject(t, json); }

int main() {
  setup();
  run(1000);
#if defined(SCEN_SOIL)
  g_adc[34] = 2048; step("normal"); run(12000); show("pump", g_dig[26]);
  g_adc[34] = 600; step("dry"); run(3000); show("pump", g_dig[26]); show("led", g_dig[25]);
  g_adc[34] = 3000; step("wet"); run(1000); show("pump", g_dig[26]);
  g_adc[34] = 600; run(1000); show("pump", g_dig[26]);
  cmd("gh-soil-01", "{\"cmd_id\":\"c1\",\"cmd\":\"pump_off\"}"); step("cmd_pump_off"); run(500); show("pump", g_dig[26]);
  cmd("gh-soil-01", "{\"cmd_id\":\"c2\",\"cmd\":\"ping\"}");
  cmd("gh-soil-01", "{\"cmd_id\":\"c2\",\"cmd\":\"ping\"}");  // дубликат
  cmd("gh-soil-01", "{\"cmd_id\":\"c3\",\"cmd\":\"bogus\"}");
  cfgmsg("gh-soil-01", "{\"cfg_ver\":5,\"interval_ms\":3000,\"mode\":\"auto\",\"moist_low\":30,\"moist_high\":60,\"pump_enabled\":0,\"pump_max_run_s\":30,\"pump_cooldown_s\":20}");
  step("locked"); run(4000); show("pump", g_dig[26]);
  cmd("gh-soil-01", "{\"cmd_id\":\"c4\",\"cmd\":\"pump_on\",\"duration_s\":10}");
  cfgmsg("gh-soil-01", "{\"cfg_ver\":6,\"interval_ms\":3000,\"mode\":\"auto\",\"moist_low\":30,\"moist_high\":60,\"pump_enabled\":1,\"pump_max_run_s\":30,\"pump_cooldown_s\":20}");
  run(70000);  // переопределение pump_off (60 с) истекает, насос стартует и упирается в защиту 30 с
  show("pump", g_dig[26]);
  step("offline"); g_broker_up = false; g_adc[34] = 2000; run(20000); g_adc[34] = 1500; run(12000);
  step("back"); g_broker_up = true; g_adc[34] = 2000; run(15000);
  cmd("gh-soil-01", "{\"cmd_id\":\"c9\",\"cmd\":\"reboot\"}"); printf("RESTART\t%d\n", (int)g_restart);
#elif defined(SCEN_TANK)
  g_echo_us = (uint32_t)(40.0 * 2 / 0.0343); step("level60"); run(8000);
  g_echo_us = (uint32_t)(85.0 * 2 / 0.0343); step("low"); run(3000); show("alarm_led", g_dig[25]);
  g_echo_us = (uint32_t)(95.0 * 2 / 0.0343); step("crit"); run(3000);
  cmd("gh-tank-01", "{\"cmd_id\":\"m1\",\"cmd\":\"buzzer_mute\",\"duration_s\":30}");
  g_echo_us = 0; step("sensor_fault"); run(5000);
  g_echo_us = (uint32_t)(40.0 * 2 / 0.0343); step("recovered"); run(5000); show("alarm_led", g_dig[25]);
#elif defined(SCEN_CLIMATE)
  g_adc[35] = 2048; g_adc[32] = 2048; step("normal"); run(6000); show("servo", g_servo_deg);
  g_adc[35] = 1500; step("hot"); run(2000); show("servo", g_servo_deg);
  g_adc[35] = 1980; run(2000); show("servo", g_servo_deg);   // 26,5 C - зона гистерезиса: остаётся открыто
  g_adc[35] = 2600; step("cool"); run(2000); show("servo", g_servo_deg);
  g_adc[32] = 3500; step("dark"); run(2000); show("grow", g_dig[25]);
  cmd("gh-climate-01", "{\"cmd_id\":\"v1\",\"cmd\":\"vent_open\",\"duration_s\":5}"); run(1000); show("servo", g_servo_deg);
  run(6000); show("servo", g_servo_deg);
#endif
  return 0;
}
