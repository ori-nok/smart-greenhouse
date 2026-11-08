#!/usr/bin/env python3
"""Замкнутый офлайн-прогон: симулятор устройств <-> настоящий GreenhouseService (без MQTT и БД).
Те же JSON-сообщения проходят тот же код обработки, что и в боевом режиме, поэтому прогон одновременно
является интеграционным тестом и генератором датасета (data/telemetry.csv, events.csv, commands.csv).

    python simulator/make_dataset.py --hours 72 --out data
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Tuple

ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "backend"))

from app.config import Settings  # noqa: E402
from app.service import GreenhouseService, Publisher  # noqa: E402
from app.storage import InMemoryStorage  # noqa: E402
from simulator.model import ClimateNode, SoilNode, TankNode, World  # noqa: E402

TOPIC_ROOT = "gh-k7q2x9/greenhouse"


class Bus(Publisher):
    """Подменяет MQTT-брокер: команды и конфигурация backend -> устройствам (с учётом связи и retained)."""

    def __init__(self) -> None:
        self.queue: List[Tuple[str, str, Dict[str, Any]]] = []
        self.retained_cfg: Dict[str, Dict[str, Any]] = {}

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 1) -> None:
        dev, kind = topic[len(TOPIC_ROOT) + 1:].split("/", 1)
        p = json.loads(payload)
        if kind == "config":
            self.retained_cfg[dev] = p
        self.queue.append((dev, kind, p))

    def is_connected(self) -> bool:
        return True


class Harness:
    def __init__(self, seed: int = 7, start: datetime = datetime(2026, 10, 30, tzinfo=timezone.utc)) -> None:
        self.start = start
        self.now = start
        self._ids = itertools.count(1)
        self.world = World(seed, start)
        self.bus = Bus()
        self.st = InMemoryStorage()
        self.svc = GreenhouseService(self.st, self.bus, Settings(topic_root=TOPIC_ROOT), clock=lambda: self.now,
                                   id_factory=lambda: f"{next(self._ids):010x}")
        self.devs = {
            "gh-soil-01": SoilNode(self.world, "gh-soil-01", 1001),
            "gh-tank-01": TankNode(self.world, "gh-tank-01", 1002),
            "gh-climate-01": ClimateNode(self.world, "gh-climate-01", 1003),
        }
        self.lwt_at: Dict[str, float] = {}
        self.t = 0
        self.timeline: List[Tuple[int, Callable[[], None]]] = []

    def ms(self) -> int:
        return int(self.now.timestamp() * 1000)

    def feed(self, dev: str, msgs) -> None:
        for kind, payload, retain in msgs:
            self.svc.handle_message(f"{TOPIC_ROOT}/{dev}/{kind}", json.dumps(payload), retained=False)

    def link(self, dev: str, up: bool) -> None:
        d = self.devs[dev]
        if up:
            self.lwt_at.pop(dev, None)
            self.feed(dev, d.set_link(True, self.ms()))
            if dev in self.bus.retained_cfg:
                self.bus.queue.append((dev, "config", self.bus.retained_cfg[dev]))
        else:
            d.set_link(False, self.ms())
            self.lwt_at[dev] = self.t + 22  # брокер публикует LWT после ~1,5 keepalive

    def deliver(self) -> None:
        while self.bus.queue:
            dev, kind, p = self.bus.queue.pop(0)
            d = self.devs.get(dev)
            if d is None or not d.link:
                continue  # команды доставляются только при наличии связи; retained-конфиг - при восстановлении
            if kind == "cmd":
                self.feed(dev, [("cmd/ack", d.handle_cmd(p, self.ms()), False)])
            elif kind == "config":
                ack = d.handle_cfg(p)
                if ack:
                    self.feed(dev, [("config/ack", ack, False)])

    def at(self, sec: int, fn: Callable[[], None]) -> None:
        self.timeline.append((sec, fn))

    def run(self, hours: float) -> None:
        total = int(hours * 3600)
        pending = sorted(self.timeline, key=lambda x: x[0])
        for _ in range(total):
            self.t += 1
            self.now = self.start + timedelta(seconds=self.t)
            self.world.step(1.0)
            while pending and pending[0][0] <= self.t:
                pending.pop(0)[1]()
            for name, d in self.devs.items():
                self.feed(name, d.tick(self.ms()))
            self.deliver()
            for name, when in list(self.lwt_at.items()):
                if self.t >= when:
                    self.lwt_at.pop(name)
                    self.svc.handle_message(f"{TOPIC_ROOT}/{name}/status",
                                            json.dumps({"v": 1, "dev": name, "state": "offline", "reason": "lwt"}), retained=True)
            if self.t % 5 == 0:
                self.svc.tick()
            if self.world.leak_rate and self.world.tank < 6:   # утечка прекращается, когда бак почти пуст
                self.world.leak_rate = 0.0


def H(day: int, hour: int, minute: int = 0) -> int:
    return day * 86400 + hour * 3600 + minute * 60


def add_scenarios(h: Harness, names: List[str]) -> None:
    w, d = h.world, h.devs
    if "config" in names:   # удалённая настройка: опрос раз в минуту (адаптивно ускоряется при поливе)
        h.at(60, lambda: [h.svc.set_config(i, {"interval_ms": 60000}, source="operator") for i in d])
    if "operator" in names:
        h.at(H(0, 14), lambda: h.svc.send_command("gh-soil-01", "pump_on", {"duration_s": 15}))
        h.at(H(1, 21), lambda: h.svc.send_command("gh-climate-01", "vent_open", {"duration_s": 120}))
        h.at(H(2, 12), lambda: h.svc.set_config("gh-soil-01", {"moist_low": 35, "moist_high": 65}, source="operator"))
    if "sensor_noise" in names:
        h.at(H(0, 11, 30), lambda: setattr(d["gh-soil-01"], "sensor_noise_boost", 6.0))
        h.at(H(0, 11, 40), lambda: setattr(d["gh-soil-01"], "sensor_noise_boost", 0.0))
    if "tank_fault" in names:
        h.at(H(0, 20), lambda: setattr(d["gh-tank-01"], "sensor_fault", True))
        h.at(H(0, 20, 4), lambda: setattr(d["gh-tank-01"], "sensor_fault", False))
    if "leak" in names:
        h.at(H(1, 3), lambda: setattr(w, "leak_rate", 0.02))
        h.at(H(1, 5, 10), lambda: setattr(w, "tank", 90.0))
    if "heat" in names:
        # узел переведён в ручной режим (обслуживание): локальная автоматика не открывает форточку,
        # поэтому защиту обеспечивает правило backend (тревога + команда vent_open)
        h.at(H(1, 12, 30), lambda: h.svc.set_config("gh-climate-01", {"mode": "manual"}, source="operator"))
        h.at(H(1, 13), lambda: setattr(w, "heat_offset", 14.0))
        h.at(H(1, 16), lambda: setattr(w, "heat_offset", 0.0))
        h.at(H(1, 17), lambda: h.svc.set_config("gh-climate-01", {"mode": "auto"}, source="operator"))
    if "outage" in names:
        h.at(H(1, 20), lambda: h.link("gh-climate-01", False))
        h.at(H(1, 20, 12), lambda: h.link("gh-climate-01", True))
        h.at(H(1, 22), lambda: h.link("gh-soil-01", False))
        h.at(H(1, 22, 4), lambda: h.link("gh-soil-01", True))
    if "stuck" in names:
        h.at(H(2, 9), lambda: setattr(d["gh-soil-01"], "sensor_stuck_value", 41.0))
        h.at(H(2, 11, 20), lambda: setattr(d["gh-soil-01"], "sensor_stuck_value", None))
    if "no_water" in names:
        def dry():
            w.pump_works = False
            w.soil = 26.0
        h.at(H(2, 15), dry)
        h.at(H(2, 17), lambda: setattr(w, "pump_works", True))
        # оператор устранил неисправность и вернул насос в работу
        h.at(H(2, 17, 5), lambda: h.svc.set_config("gh-soil-01", {"pump_enabled": 1}, source="operator"))


ALL = ["config", "operator", "sensor_noise", "tank_fault", "leak", "heat", "outage", "stuck", "no_water"]


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def export(h: Harness, out: str) -> Dict[str, int]:
    os.makedirs(out, exist_ok=True)
    rows = sorted(h.st.telemetry, key=lambda r: (r["ts"], r["device_id"], r["seq"]))
    keys = sorted({k for r in rows for k in r["metrics"]})
    with open(os.path.join(out, "telemetry.csv"), "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["ts", "device_id", "boot", "seq", "buffered"] + keys)
        for r in rows:
            wr.writerow([_ts(r["ts"]), r["device_id"], r["boot"], r["seq"], int(r["buffered"])]
                        + [r["metrics"].get(k, "") for k in keys])
    with open(os.path.join(out, "events.csv"), "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["ts", "device_id", "type", "severity", "message"])
        for e in sorted(h.st.events, key=lambda e: (e["ts"], e["id"])):
            wr.writerow([_ts(e["ts"]), e["device_id"], e["type"], e["severity"], e["message"]])
    with open(os.path.join(out, "commands.csv"), "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["ts", "cmd_id", "device_id", "cmd", "args", "source", "status", "reason"])
        for c in sorted(h.st.commands.values(), key=lambda c: c["ts"]):
            wr.writerow([_ts(c["ts"]), c["cmd_id"], c["device_id"], c["cmd"], json.dumps(c["args"]),
                         c["source"], c["status"], c["reason"] or ""])
    return {"telemetry": len(rows), "events": len(h.st.events), "commands": len(h.st.commands)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--out", default=os.path.join(ROOT_DIR, "data"))
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    h = Harness(seed=a.seed)
    add_scenarios(h, ALL)
    h.run(a.hours)
    print("exported:", export(h, a.out))
    for (typ, sev), n in sorted(Counter((e["type"], e["severity"]) for e in h.st.events).items()):
        print(f"  {n:4d}  {sev:8s} {typ}")


if __name__ == "__main__":
    main()
