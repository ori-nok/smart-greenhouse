#include "gh_logic.h"
#include <assert.h>
#include <stdio.h>
using namespace gh;
#define NEAR(a, b, e) assert(fabsf((a) - (b)) <= (e))
int main() {
  const char* j = "{\"cmd_id\":\"abc\",\"cmd\":\"mode\",\"mode\":\"manual\",\"x\": 12.5,\"neg\":-3,\"s\":\"7\"}";
  double d; char s[16];
  assert(jsonGetNum(j, "x", d) && d == 12.5);
  assert(jsonGetNum(j, "neg", d) && d == -3);
  assert(jsonGetNum(j, "s", d) && d == 7);
  assert(!jsonGetNum(j, "nope", d));
  assert(jsonGetStr(j, "cmd", s, sizeof s) && !strcmp(s, "mode"));
  assert(jsonGetStr(j, "mode", s, sizeof s) && !strcmp(s, "manual"));  // ключ "mode" после значения "mode"
  assert(!jsonGetStr(j, "x", s, sizeof s));
  assert(!jsonGetNum("{\"a\":nan}", "a", d));
  char tiny[3]; assert(!jsonGetStr(j, "cmd_id", tiny, sizeof tiny));
  NEAR(moisturePct(0), 0, 0.01); NEAR(moisturePct(4095), 100, 0.01); NEAR(moisturePct(2048), 50, 0.1);
  NEAR(ntcToCelsius(2048), 25.0f, 0.2); assert(ntcToCelsius(1500) > 25); assert(ntcToCelsius(2600) < 25);
  assert(ntcToCelsius(0) <= 150 && ntcToCelsius(0) > 100 && ntcToCelsius(4095) >= -55 && ntcToCelsius(4095) < -30);
  assert(ldrToLux(1000) > ldrToLux(3000)); assert(ldrToLux(0) <= 100000 && ldrToLux(4095) >= 0);
  NEAR(ldrToLux(2048), 100.0f, 5.0f);
  assert(echoUsToCm(0) < 0); NEAR(echoUsToCm(2332), 40.0f, 0.1); assert(echoUsToCm(100000) < 0);
  NEAR(levelPct(40, 100), 60, 0.01); NEAR(levelPct(150, 100), 0, 0.01); assert(levelPct(-1, 100) < 0);
  NEAR(median3(5, 1, 3), 3, 0); NEAR(median3(1, 2, 3), 2, 0); NEAR(median3(9, 9, 1), 9, 0);
  assert(onBelow(false, 20, 30, 60)); assert(!onBelow(true, 60, 30, 60)); assert(onBelow(true, 45, 30, 60)); assert(!onBelow(false, 45, 30, 60));
  assert(onAbove(false, 29, 28, 25)); assert(!onAbove(true, 25, 28, 25)); assert(onAbove(true, 26, 28, 25)); assert(!onAbove(false, 26, 28, 25));
  assert(tankState(0, 50, 25, 10) == 0); assert(tankState(0, 20, 25, 10) == 1); assert(tankState(1, 5, 25, 10) == 2);
  assert(tankState(2, 11, 25, 10) == 2); assert(tankState(2, 14, 25, 10) == 1); assert(tankState(1, 26, 25, 10) == 1);
  assert(tankState(1, 29, 25, 10) == 0); assert(tankState(1, -1, 25, 10) == 1);
  assert(adaptiveInterval(5000, 2000, true) == 2000); assert(adaptiveInterval(5000, 2000, false) == 5000); assert(adaptiveInterval(1000, 2000, true) == 1000);
  TimedOverride o; o.set(true, 1000, 500); assert(o.active); o.update(1400); assert(o.active); o.update(1500); assert(!o.active);
  o.set(false, 10, 0); o.update(4000000000u); assert(o.active);
  o.set(true, 0xFFFFFF00u, 1000); o.update(0xFFFFFFF0u); assert(o.active); o.update(800); assert(!o.active);  // переполнение millis()
  PumpController p; uint32_t t = 0;
  auto up = [&](float m, bool en = true, bool au = true) { t += 100; return p.update(t, au, en, m, 30, 60, 5000, 3000); };
  up(50); assert(!p.on); up(20); assert(p.on); up(45); assert(p.on); up(65); assert(!p.on);
  up(20); assert(p.on); assert(up(20, false) == PumpController::EV_LOCKED_STOP); assert(!p.on);
  up(20, false); assert(!p.on);
  PumpController q; t = 0; q.update(t, true, true, 10, 30, 60, 5000, 3000); assert(q.on);
  PumpController::Event ev = PumpController::EV_NONE;
  for (int i = 0; i < 60; i++) { t += 100; ev = q.update(t, true, true, 10, 30, 60, 5000, 3000); if (ev) break; }
  assert(ev == PumpController::EV_FAILSAFE_TIMEOUT); assert(!q.on); assert(q.inCooldown(t));
  t += 1000; q.update(t, true, true, 10, 30, 60, 5000, 3000); assert(!q.on);
  t += 3000; q.update(t, true, true, 10, 30, 60, 5000, 3000); assert(q.on);
  PumpController m2; t = 0; m2.update(t, false, true, 10, 30, 60, 5000, 3000); assert(!m2.on);
  m2.ovr.set(true, t, 0); t += 100; m2.update(t, false, true, 10, 30, 60, 5000, 3000); assert(m2.on);
  MsgRing<3, 8> r; assert(r.empty()); r.push("a"); r.push("b"); r.push("c"); assert(r.size() == 3);
  assert(!r.push("d")); assert(r.dropped == 1); assert(!strcmp(r.front(), "b")); r.pop(); assert(!strcmp(r.front(), "c")); r.pop(); r.pop(); assert(r.empty());
  r.push("0123456789"); assert(strlen(r.front()) == 7);
  printf("test_logic: ALL OK\n");
  return 0;
}
