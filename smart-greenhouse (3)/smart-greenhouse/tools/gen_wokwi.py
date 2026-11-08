#!/usr/bin/env python3
"""Генерирует diagram.json и libraries.txt для трёх проектов Wokwi (плата ESP32 DevKit V1)."""
import json, os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware")
SER = [["esp:TX0", "$serialMonitor:RX", "", []], ["esp:RX0", "$serialMonitor:TX", "", []]]


def esp():
    return {"type": "wokwi-esp32-devkit-v1", "id": "esp", "top": 0, "left": 0, "attrs": {}}


def led(i, color, top, left):
    return {"type": "wokwi-led", "id": i, "top": top, "left": left, "attrs": {"color": color}}


def res(i, top, left):
    return {"type": "wokwi-resistor", "id": i, "top": top, "left": left, "attrs": {"value": "220"}}


def led_chain(r, l, pin):
    # GPIO -> резистор -> анод светодиода, катод -> GND
    return [[f"esp:{pin}", f"{r}:1", "green", []], [f"{r}:2", f"{l}:A", "green", []],
            [f"{l}:C", "esp:GND.1", "black", []]]


def write(name, parts, conns, libs):
    d = {"version": 1, "author": "Smart Greenhouse project", "editor": "wokwi",
         "parts": parts, "connections": SER + conns, "dependencies": {}}
    path = os.path.join(ROOT, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "diagram.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(path, "libraries.txt"), "w") as f:
        f.write("\n".join(libs) + "\n")


write("gh-soil-01", [
    esp(),
    {"type": "wokwi-slide-potentiometer", "id": "soil", "top": -40, "left": 260, "attrs": {"travelLength": "30"}},
    {"type": "wokwi-relay-module", "id": "relay1", "top": 90, "left": 260, "attrs": {}},
    led("ledPump", "green", 200, 270), res("r1", 250, 270),
    led("ledNet", "blue", 200, 340), res("r2", 250, 340),
], [
    ["soil:VCC", "esp:3V3", "red", []], ["soil:GND", "esp:GND.1", "black", []], ["soil:SIG", "esp:D34", "green", []],
    ["relay1:VCC", "esp:VIN", "red", []], ["relay1:GND", "esp:GND.1", "black", []], ["relay1:IN", "esp:D26", "green", []],
] + led_chain("r1", "ledPump", "D25") + led_chain("r2", "ledNet", "D27"), ["PubSubClient"])

write("gh-tank-01", [
    esp(),
    {"type": "wokwi-hc-sr04", "id": "sonar", "top": -80, "left": 260, "attrs": {"distance": "40"}},
    {"type": "wokwi-buzzer", "id": "bz1", "top": 110, "left": 260, "attrs": {"volume": "0.1"}},
    led("ledAlarm", "red", 200, 270), res("r1", 250, 270),
    led("ledNet", "blue", 200, 340), res("r2", 250, 340),
], [
    ["sonar:VCC", "esp:VIN", "red", []], ["sonar:GND", "esp:GND.1", "black", []],
    ["sonar:TRIG", "esp:D5", "green", []], ["sonar:ECHO", "esp:D18", "orange", []],
    ["bz1:1", "esp:GND.1", "black", []], ["bz1:2", "esp:D27", "green", []],
] + led_chain("r1", "ledAlarm", "D25") + led_chain("r2", "ledNet", "D26"), ["PubSubClient"])

write("gh-climate-01", [
    esp(),
    {"type": "wokwi-ntc-temperature-sensor", "id": "ntc", "top": -60, "left": 260, "attrs": {"temperature": "24"}},
    {"type": "wokwi-photoresistor-sensor", "id": "ldr", "top": 40, "left": 260, "attrs": {}},
    {"type": "wokwi-servo", "id": "servo1", "top": 150, "left": 260, "attrs": {}},
    led("ledGrow", "yellow", 260, 270), res("r1", 310, 270),
    led("ledNet", "blue", 260, 340), res("r2", 310, 340),
], [
    ["ntc:VCC", "esp:3V3", "red", []], ["ntc:GND", "esp:GND.1", "black", []], ["ntc:OUT", "esp:D35", "green", []],
    ["ldr:VCC", "esp:3V3", "red", []], ["ldr:GND", "esp:GND.1", "black", []], ["ldr:AO", "esp:D32", "green", []],
    ["servo1:V+", "esp:VIN", "red", []], ["servo1:GND", "esp:GND.1", "black", []], ["servo1:PWM", "esp:D13", "orange", []],
] + led_chain("r1", "ledGrow", "D25") + led_chain("r2", "ledNet", "D26"), ["PubSubClient", "ESP32Servo"])
print("wokwi projects generated")
