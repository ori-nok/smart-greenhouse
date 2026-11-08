#pragma once
#include "Arduino.h"
extern int g_servo_deg;
struct Servo {
  void attach(int, int, int) {}
  void write(int d) { g_servo_deg = d; }
};
