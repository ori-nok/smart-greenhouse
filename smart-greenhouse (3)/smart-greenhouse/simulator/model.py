"""Программная модель теплицы и трёх узлов. Протокол сообщений - тот же, что у прошивок ESP32
(см. docs/PROTOCOL.md), поэтому симулятор можно подключить к настоящему backend вместо Wokwi
(simulator/run_mqtt.py) либо прогнать офлайн для генерации датасета (simulator/make_dataset.py)."""
from __future__ import annotations

import math
import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

Msg = Tuple[str, Dict[str, Any], bool]  # (kind, payload, retain)
MAX_BUF = 24


class World:
    """Физическая модель: суточный цикл света/температуры, высыхание почвы, расход воды."""

    def __init__(self, seed: int, start: datetime, fast: bool = False) -> None:
        self.tau = 20.0 if fast else 600.0       # постоянная времени температуры, с (fast - для живой демонстрации)
        self.dry_mult = 30.0 if fast else 1.0    # множитель скорости высыхания почвы
        self.rng = random.Random(seed)
        self.start = start
        self.t = 0.0
        self.soil, self.tank, self.temp = 48.0, 85.0, 21.0
        self.lux = 0.0
        self.heat_offset = 0.0     # сценарий «жара»
        self.leak_rate = 0.0       # сценарий «утечка», %/с
        self.pump_works = True     # сценарий «нет воды в магистрали»
        self.pump_on = self.vent_open = self.grow_on = False

    def hour(self) -> float:
        return ((self.start.hour * 3600 + self.start.minute * 60 + self.t) % 86400) / 3600.0

    def step(self, dt: float = 1.0) -> None:
        self.t += dt
        h = self.hour()
        sun = max(0.0, math.sin((h - 6.0) / 12.0 * math.pi))
        target = 21.0 + 6.0 * math.sin((h - 9.0) / 24.0 * 2 * math.pi) + self.heat_offset
        target += (-3.5 if self.vent_open else 0.0) + (0.8 if self.grow_on else 0.0)
        self.temp += (target - self.temp) * dt / self.tau + self.rng.gauss(0, 0.01)
        self.lux = 900.0 * sun + (150.0 if self.grow_on else 0.0)  # вклад досветки должен быть меньше ширины гистерезиса (lux_high - lux_low), иначе дребезг
        rate = (0.0006 + 0.0012 * sun) * (1 + 0.04 * (self.temp - 20))
        self.soil -= rate * self.dry_mult * dt
        if self.pump_on and self.pump_works:  # нет воды в магистрали - нет ни полива, ни расхода из бака
            self.soil += 1.3 * dt
            self.tank -= 0.12 * dt
        self.tank -= self.leak_rate * dt
        self.soil = min(100.0, max(0.0, self.soil))
        self.tank = min(100.0, max(0.0, self.tank))


class Override:
    def __init__(self) -> None:
        self.active, self.state, self.until = False, False, None

    def set(self, state: bool, now_ms: int, dur_ms: int) -> None:
        self.active, self.state = True, state
        self.until = now_ms + dur_ms if dur_ms else None

    def update(self, now_ms: int) -> None:
        if self.active and self.until is not None and now_ms >= self.until:
            self.active = False


class SimDevice:
    TYPE = ""
    HEARTBEAT_MS = 10000

    def __init__(self, world: World, dev_id: str, boot: int) -> None:
        self.w, self.dev, self.boot = world, dev_id, boot
        self.seq, self.cfg_ver, self.dropped = 0, 0, 0
        self.interval_ms, self.auto = 5000, True
        self.link = True
        self.buf: List[Dict[str, Any]] = []
        self.last_sample = -10**9
        self.last_hb = -10**9
        self.boot_ms = 0
        self.pending_immediate = False
        self.sensor_noise_boost = 0.0
        self.sensor_stuck_value: Optional[float] = None  # сценарий «датчик завис»
        self.fw_ver = "1.0.0"

    def _tele(self, now_ms: int, buffered: bool, itv: int) -> Dict[str, Any]:
        self.seq += 1
        return {"v": 1, "dev": self.dev, "type": self.TYPE, "boot": self.boot, "seq": self.seq, "ts": now_ms,
                "up": now_ms - self.boot_ms, "buf": int(buffered), "drop": self.dropped,
                "mode": "auto" if self.auto else "manual", "itv": itv, "rssi": -55 + int(self.w.rng.gauss(0, 2)),
                "m": self.metrics()}

    def status_msg(self, now_ms: int) -> Msg:
        return ("status", {"v": 1, "dev": self.dev, "type": self.TYPE, "fw": self.fw_ver, "state": "online",
                           "boot": self.boot, "up": now_ms - self.boot_ms, "rssi": -55, "heap": 200000,
                           "buf_n": len(self.buf), "cfg_ver": self.cfg_ver}, True)

    def event(self, now_ms: int, etype: str, sev: str, text: str) -> List[Msg]:
        if not self.link:
            return []
        return [("event", {"v": 1, "dev": self.dev, "boot": self.boot, "ts": now_ms, "type": etype, "sev": sev, "msg": text}, False)]

    def set_link(self, up: bool, now_ms: int) -> List[Msg]:
        out: List[Msg] = []
        if up and not self.link:
            self.link = True
            out.append(self.status_msg(now_ms))
            out += self.event(now_ms, "link_restored", "info", f"link restored: buffered={len(self.buf)}")
            out += [("telemetry", p, False) for p in self.buf]
            self.buf = []
        elif not up:
            self.link = False
        return out

    def eff_interval(self) -> int:
        return min(self.interval_ms, 2000) if self.active() else self.interval_ms

    def active(self) -> bool:
        return False

    def tick(self, now_ms: int) -> List[Msg]:
        out: List[Msg] = self.control(now_ms)
        if self.pending_immediate or now_ms - self.last_sample >= self.eff_interval():
            self.pending_immediate = False
            self.last_sample = now_ms
            if self.link:
                out.append(("telemetry", self._tele(now_ms, False, self.eff_interval()), False))
            else:
                if len(self.buf) >= MAX_BUF:
                    self.buf.pop(0)
                    self.dropped += 1
                self.buf.append(self._tele(now_ms, True, self.eff_interval()))
        if self.link and now_ms - self.last_hb >= self.HEARTBEAT_MS:
            self.last_hb = now_ms
            out.append(self.status_msg(now_ms))
        return out

    def handle_cmd(self, p: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
        reason, ok = "", True
        cmd = p.get("cmd")
        if cmd == "ping":
            pass
        elif cmd == "reboot":
            reason = "rebooting"
            self.boot += 1
            self.seq, self.boot_ms = 0, now_ms
        else:
            ok, reason = self.device_cmd(cmd, p, now_ms)
        return {"v": 1, "dev": self.dev, "cmd_id": p.get("cmd_id"), "status": "ok" if ok else "rejected", "reason": reason}

    def handle_cfg(self, p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if p.get("cfg_ver") == self.cfg_ver and self.cfg_ver != 0:
            return None
        self.interval_ms = int(min(60000, max(1000, p.get("interval_ms", self.interval_ms))))
        if "mode" in p:
            self.auto = p["mode"] != "manual"
        self.apply_cfg(p)
        self.cfg_ver = int(p["cfg_ver"])
        return {"v": 1, "dev": self.dev, "cfg_ver": self.cfg_ver, "status": "ok"}

    def metrics(self) -> Dict[str, Any]:
        raise NotImplementedError

    def control(self, now_ms: int) -> List[Msg]:
        return []

    def device_cmd(self, cmd: str, p: Dict[str, Any], now_ms: int) -> Tuple[bool, str]:
        return False, "unknown_cmd"

    def apply_cfg(self, p: Dict[str, Any]) -> None: ...


class SoilNode(SimDevice):
    TYPE = "soil"

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.low, self.high, self.enabled, self.max_run, self.cooldown = 30.0, 60.0, True, 30, 20
        self.ovr = Override()
        self.on = False
        self.run_start = 0
        self.cool_until = 0

    def read(self) -> float:
        if self.sensor_stuck_value is not None:
            return self.sensor_stuck_value
        return min(100.0, max(0.0, self.w.soil + self.w.rng.gauss(0, 0.25 + self.sensor_noise_boost)))

    def active(self) -> bool:
        return self.on

    def metrics(self) -> Dict[str, Any]:
        m = round(self.read(), 1)
        return {"soil_moisture_pct": m, "soil_raw": int(m / 100 * 4095), "pump_on": int(self.on),
                "pump_locked": int(not self.enabled), "pump_cooldown": int(self.cool_until > 0), "override": int(self.ovr.active)}

    def control(self, now_ms: int) -> List[Msg]:
        out: List[Msg] = []
        self.ovr.update(now_ms)
        if self.cool_until and now_ms >= self.cool_until:
            self.cool_until = 0
        m = self.read()
        if not self.enabled:
            want = False
            if self.on:
                out += self.event(now_ms, "pump_locked_stop", "info", "pump stopped: disabled by configuration")
        elif self.ovr.active:
            want = self.ovr.state
        elif self.auto:
            want = True if m < self.low else (False if m >= self.high else self.on)
            if want and not self.on and self.cool_until:
                want = False
        else:
            want = False
        if want and not self.on:
            self.run_start = now_ms
        if self.on and want and now_ms - self.run_start >= self.max_run * 1000:
            want = False
            self.cool_until = now_ms + self.cooldown * 1000
            self.ovr.active = False
            out += self.event(now_ms, "pump_failsafe", "warning", "max run time exceeded, pump stopped")
        if want != self.on:
            self.on = want
            self.w.pump_on = want
            self.pending_immediate = True
        return out

    def device_cmd(self, cmd: str, p: Dict[str, Any], now_ms: int) -> Tuple[bool, str]:
        d = p.get("duration_s")
        if cmd == "pump_on":
            if not self.enabled:
                return False, "pump_locked"
            self.ovr.set(True, now_ms, int(min(self.max_run, d if d else 20) * 1000))
            return True, ""
        if cmd == "pump_off":
            self.ovr.set(False, now_ms, int(d * 1000) if d else (60000 if self.auto else 0))
            return True, ""
        return False, "unknown_cmd"

    def apply_cfg(self, p: Dict[str, Any]) -> None:
        self.low, self.high = float(p.get("moist_low", self.low)), float(p.get("moist_high", self.high))
        self.enabled = bool(p.get("pump_enabled", self.enabled))
        self.max_run = int(p.get("pump_max_run_s", self.max_run))
        self.cooldown = int(p.get("pump_cooldown_s", self.cooldown))


class TankNode(SimDevice):
    TYPE = "tank"

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.height, self.low, self.crit = 100.0, 25.0, 10.0
        self.state = 0
        self.sensor_fault = False
        self.fault_reported = False

    def read(self) -> float:
        if self.sensor_fault:
            return -1.0
        return round(min(100.0, max(0.0, self.w.tank + self.w.rng.gauss(0, 0.2))), 1)

    def active(self) -> bool:
        return self.state > 0

    def metrics(self) -> Dict[str, Any]:
        lvl = self.read()
        return {"distance_cm": -1.0 if lvl < 0 else round(self.height * (1 - lvl / 100), 1), "level_pct": lvl,
                "tank_state": self.state, "sensor_ok": int(lvl >= 0), "muted": 0}

    def control(self, now_ms: int) -> List[Msg]:
        out: List[Msg] = []
        lvl = self.read()
        if lvl < 0:
            if not self.fault_reported:
                self.fault_reported = True
                out += self.event(now_ms, "sensor_fault", "critical", "HC-SR04: no echo (3 consecutive failures)")
            return out
        if self.fault_reported:
            self.fault_reported = False
            out += self.event(now_ms, "sensor_recovered", "info", "HC-SR04 echo restored")
        s = 2 if lvl <= self.crit else (1 if lvl <= self.low else 0)
        if s < self.state:
            if self.state == 2 and lvl < self.crit + 3:
                s = 2
            elif s == 0 and lvl < self.low + 3:
                s = 1
        if s != self.state:
            self.state = s
            sev = {2: "critical", 1: "warning", 0: "info"}[s]
            out += self.event(now_ms, "tank_state", sev, {2: "tank critical", 1: "tank low", 0: "tank ok"}[s])
            self.pending_immediate = True
        return out

    def device_cmd(self, cmd: str, p: Dict[str, Any], now_ms: int) -> Tuple[bool, str]:
        return (True, "") if cmd == "buzzer_mute" else (False, "unknown_cmd")

    def apply_cfg(self, p: Dict[str, Any]) -> None:
        self.height = float(p.get("tank_height_cm", self.height))
        self.low, self.crit = float(p.get("level_low_pct", self.low)), float(p.get("level_crit_pct", self.crit))


class ClimateNode(SimDevice):
    TYPE = "climate"

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.t_open, self.t_close, self.lux_low, self.lux_high = 28.0, 25.0, 200.0, 400.0
        self.vent, self.grow = False, False
        self.last_t = self.w.temp
        self.ovr_vent, self.ovr_light = Override(), Override()

    def read_t(self) -> float:
        return round(self.w.temp + self.w.rng.gauss(0, 0.1 + self.sensor_noise_boost), 1)

    def read_l(self) -> float:
        return round(max(0.0, self.w.lux + self.w.rng.gauss(0, 3 + 0.01 * self.w.lux)))

    def active(self) -> bool:
        return abs(self.w.temp - self.last_t) >= 1.0   # ускоряемся только при быстром изменении температуры

    def metrics(self) -> Dict[str, Any]:
        self.last_t = self.w.temp
        return {"temperature_c": self.read_t(), "light_lux": self.read_l(), "vent_open": int(self.vent),
                "grow_light_on": int(self.grow), "override": int(self.ovr_vent.active or self.ovr_light.active)}

    def control(self, now_ms: int) -> List[Msg]:
        self.ovr_vent.update(now_ms)
        self.ovr_light.update(now_ms)
        t, lux = self.read_t(), self.read_l()
        wv, wg = self.vent, self.grow
        if self.ovr_vent.active:
            wv = self.ovr_vent.state
        elif self.auto:
            wv = True if t > self.t_open else (False if t <= self.t_close else self.vent)
        if self.ovr_light.active:
            wg = self.ovr_light.state
        elif self.auto:
            wg = True if lux < self.lux_low else (False if lux >= self.lux_high else self.grow)
        if (wv, wg) != (self.vent, self.grow):
            self.vent, self.grow = wv, wg
            self.w.vent_open, self.w.grow_on = wv, wg
            self.pending_immediate = True
        return []

    def device_cmd(self, cmd: str, p: Dict[str, Any], now_ms: int) -> Tuple[bool, str]:
        d = p.get("duration_s")
        dur = int(d * 1000) if d else (60000 if self.auto else 0)
        table = {"vent_open": (self.ovr_vent, True), "vent_close": (self.ovr_vent, False),
                 "light_on": (self.ovr_light, True), "light_off": (self.ovr_light, False)}
        if cmd in table:
            table[cmd][0].set(table[cmd][1], now_ms, dur)
            return True, ""
        return False, "unknown_cmd"

    def apply_cfg(self, p: Dict[str, Any]) -> None:
        self.t_open, self.t_close = float(p.get("temp_open_c", self.t_open)), float(p.get("temp_close_c", self.t_close))
        self.lux_low, self.lux_high = float(p.get("lux_low", self.lux_low)), float(p.get("lux_high", self.lux_high))
