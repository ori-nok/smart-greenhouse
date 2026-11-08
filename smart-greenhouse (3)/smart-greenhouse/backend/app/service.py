"""Ядро системы: приём телеметрии/статусов/событий, прикладная логика, команды и конфигурация.

Не зависит от FastAPI/paho/psycopg: транспорт (Publisher) и хранилище (Storage) внедряются снаружи,
поэтому вся логика тестируется без брокера и БД (backend/tests, simulator/make_dataset.py)."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from .config import Settings
from .engine import EwmaDetector, LeakDetector, Metrics, SeqTracker, StuckDetector
from .protocol import (CfgAck, CmdAck, DeviceEvent, ProtocolError, Status, Telemetry, Topics, ValidationError,
                       build_command_payload, build_config_payload, default_config, parse_cfg_ack, parse_cmd_ack,
                       parse_event, parse_status, parse_telemetry, validate_command, validate_config)

log = logging.getLogger("greenhouse.service")

# Какие метрики проверяются на аномалии и их минимальный «шум» (абсолютный порог z-оценки).
# Освещённость не проверяется: её скачки - штатная реакция на включение/выключение досветки (+250 лк).
ANOMALY_METRICS: Dict[str, Dict[str, float]] = {
    "soil_moisture_pct": {"abs_floor": 2.0},
    "level_pct": {"abs_floor": 1.5},
    "temperature_c": {"abs_floor": 0.7},
}
STUCK_METRICS = ("soil_moisture_pct", "level_pct", "temperature_c")
TANK_UNLOCK_MARGIN = 10.0  # насосы разблокируются, когда уровень выше критического на столько %


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


class Publisher:  # интерфейс транспорта
    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 1) -> None:  # pragma: no cover
        raise NotImplementedError

    def is_connected(self) -> bool:  # pragma: no cover
        return True


class Notifier:  # интерфейс оповещений
    def notify(self, text: str) -> None:  # pragma: no cover
        pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GreenhouseService:
    def __init__(self, storage, publisher: Publisher, settings: Settings,
                 clock: Callable[[], datetime] = utcnow, notifier: Optional[Notifier] = None,
                 id_factory: Optional[Callable[[], str]] = None) -> None:
        self._new_id = id_factory or (lambda: uuid.uuid4().hex[:10])
        self.st = storage
        self.pub = publisher
        self.s = settings
        self.clock = clock
        self.notifier = notifier or Notifier()
        self.topics = Topics(settings.topic_root)
        self.metrics = Metrics()
        self.lock = threading.RLock()
        self.seq = SeqTracker()
        self.devices: Dict[str, Dict[str, Any]] = {}
        self._det: Dict[tuple, EwmaDetector] = {}
        self._stuck: Dict[tuple, StuckDetector] = {}
        self._leak = LeakDetector()
        self._alarms: Dict[str, bool] = {}
        self._cooldown: Dict[str, datetime] = {}
        self._stalled: Set[str] = set()
        self._reboots: Dict[str, List[datetime]] = {}
        self._pump_prev: Dict[str, Dict[str, float]] = {}
        self._ineffective: Dict[str, List[float]] = {}
        self._cfg_push: Dict[str, datetime] = {}
        self.last_pump_ts: Optional[float] = None
        self.tank_level: Optional[float] = None
        self.tank_crit: float = 10.0
        self.started = clock()
        self._init_event_metrics()
        self._load()

    # ------------------------------------------------------------------ инициализация
    def _load(self) -> None:
        for d in self.st.list_devices():
            d["config"] = d.get("config") or {}
            d["meta"] = d.get("meta") or {}
            self.devices[d["device_id"]] = d
            self._init_device_metrics(d["device_id"])
            self.seq.restore(d["device_id"], d.get("boot"), d.get("last_seq"))

    # Prometheus: increase() не видит первый скачок счётчика, который появился сразу со значением 1,
    # поэтому все известные ряды заводим заранее со значением 0.
    _EVENT_KINDS = (
        ("info", "device_registered"), ("info", "device_online"), ("info", "link_restored"), ("info", "config_changed"),
        ("info", "heat_cleared"), ("info", "frost_cleared"), ("info", "pump_unlocked"),
        ("warning", "anomaly"), ("warning", "device_restart"), ("warning", "telemetry_gap"),
        ("warning", "telemetry_stalled"), ("warning", "sensor_stuck"), ("warning", "irrigation_ineffective"),
        ("warning", "command_rejected"), ("warning", "command_timeout"), ("warning", "invalid_message"),
        ("warning", "rule_action_failed"), ("warning", "auto_reboot_suppressed"),
        ("critical", "device_offline"), ("critical", "heat_alarm"), ("critical", "frost_alarm"),
        ("critical", "leak_suspected"), ("critical", "pump_fault"), ("critical", "pump_locked"),
    )

    def _init_event_metrics(self) -> None:
        for sev, et in self._EVENT_KINDS:
            self.metrics.inc("events_total", 0, severity=sev, type=et)

    def _init_device_metrics(self, dev_id: str) -> None:
        self.metrics.inc("telemetry_lost_total", 0, device=dev_id)
        self.metrics.inc("telemetry_duplicates_total", 0, device=dev_id)
        for b in ("true", "false"):
            self.metrics.inc("telemetry_total", 0, device=dev_id, buffered=b)

    def on_mqtt_connected(self) -> None:
        """После (пере)подключения к брокеру: повторно публикуем сохранённую конфигурацию (retained)."""
        with self.lock:
            for d in self.devices.values():
                if d["cfg_ver"] > 0:
                    self.pub.publish(self.topics.config(d["device_id"]), build_config_payload(d["cfg_ver"], d["config"]),
                                     retain=True)

    # ------------------------------------------------------------------ вспомогательное
    def _now(self) -> datetime:
        return self.clock()

    def _event(self, device_id: Optional[str], etype: str, severity: str, message: str,
               data: Optional[dict] = None, ts: Optional[datetime] = None, cooldown_s: float = 0,
               key: Optional[str] = None) -> Optional[int]:
        now = self._now()
        if cooldown_s > 0:
            k = key or f"{device_id}:{etype}"
            last = self._cooldown.get(k)
            if last and (now - last).total_seconds() < cooldown_s:
                return None
            self._cooldown[k] = now
        eid = self.st.insert_event({"ts": ts or now, "device_id": device_id, "type": etype, "severity": severity,
                                    "message": message, "data": data or {}})
        self.metrics.inc("events_total", severity=severity, type=etype)
        if severity == "critical":
            try:
                self.notifier.notify(f"[{device_id or 'system'}] {etype}: {message}")
            except Exception:  # оповещение не должно ломать обработку
                log.exception("notifier failed")
        return eid

    def _effective_ts(self, ts_ms: int, now: datetime) -> datetime:
        """Метка времени устройства (NTP) используется, если правдоподобна; иначе - время приёма."""
        if ts_ms > 0:
            try:
                ts = datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc)
            except (OverflowError, OSError, ValueError):
                return now
            if now - timedelta(days=7) <= ts <= now + timedelta(seconds=60):
                return ts
        return now

    def _ensure_device(self, dev_id: str, dtype: str, now: datetime) -> Dict[str, Any]:
        d = self.devices.get(dev_id)
        if d is None:
            cfg = default_config(dtype)
            d = {"device_id": dev_id, "type": dtype, "fw": None, "state": "unknown", "mode": cfg["mode"], "boot": None,
                 "last_seq": None, "first_seen": now, "last_seen": None, "last_telemetry": None, "rssi": None,
                 "itv_ms": None, "config": cfg, "cfg_ver": 1, "cfg_acked": 0, "meta": {}}
            self.devices[dev_id] = d
            self._init_device_metrics(dev_id)
            self.st.upsert_device(d)
            self.pub.publish(self.topics.config(dev_id), build_config_payload(1, cfg), retain=True)
            self._event(dev_id, "device_registered", "info", f"new {dtype} device registered", ts=now)
        elif d["type"] != dtype:
            raise ProtocolError(f"device '{dev_id}' type changed from {d['type']} to {dtype}")
        return d

    def _mark_online(self, d: Dict[str, Any], now: datetime) -> None:
        if d["state"] == "offline":
            down = (now - d["last_seen"]).total_seconds() if d.get("last_seen") else 0
            self._event(d["device_id"], "device_online", "info", f"device is back online after {down:.0f} s",
                        {"downtime_s": round(down)}, ts=now)
        d["state"] = "online"

    # ------------------------------------------------------------------ вход: MQTT-сообщения
    def handle_message(self, topic: str, payload: Any, retained: bool = False) -> None:
        parsed = self.topics.parse(topic)
        if parsed is None:
            self.metrics.inc("messages_ignored_total")
            return
        dev, kind = parsed
        with self.lock:
            try:
                if kind == "telemetry":
                    obj = parse_telemetry(payload)
                    self._same_dev(dev, obj.dev)
                    self.ingest_telemetry(obj)
                elif kind == "status":
                    obj = parse_status(payload)
                    self._same_dev(dev, obj.dev)
                    self.ingest_status(obj, retained)
                elif kind == "event":
                    obj = parse_event(payload)
                    self._same_dev(dev, obj.dev)
                    self.ingest_event(obj)
                elif kind == "cmd/ack":
                    obj = parse_cmd_ack(payload)
                    self._same_dev(dev, obj.dev)
                    self.ingest_cmd_ack(obj)
                elif kind == "config/ack":
                    obj = parse_cfg_ack(payload)
                    self._same_dev(dev, obj.dev)
                    self.ingest_cfg_ack(obj)
            except ProtocolError as e:
                self.metrics.inc("invalid_messages_total", kind=kind)
                self._event(dev, "invalid_message", "warning", f"{kind}: {e}", cooldown_s=60, key=f"{dev}:invalid:{kind}")

    @staticmethod
    def _same_dev(topic_dev: str, payload_dev: str) -> None:
        if topic_dev != payload_dev:
            raise ProtocolError(f"device id in payload ('{payload_dev}') does not match topic ('{topic_dev}')")

    # ------------------------------------------------------------------ телеметрия
    def ingest_telemetry(self, t: Telemetry) -> None:
        now = self._now()
        ts = self._effective_ts(t.ts_ms, now)
        d = self._ensure_device(t.dev, t.type, now)
        kind, missing = self.seq.check(t.dev, t.boot, t.seq)
        if kind == "duplicate":
            self.metrics.inc("telemetry_duplicates_total", device=t.dev)
            return
        if kind == "restart":
            self._event(t.dev, "device_restart", "warning", "device restarted (new boot id)", {"boot": t.boot}, ts=ts)
        elif kind == "gap":
            self.metrics.inc("telemetry_lost_total", missing, device=t.dev)
            self._event(t.dev, "telemetry_gap", "warning", f"{missing} message(s) lost before seq {t.seq}",
                        {"missing": missing, "seq": t.seq, "dropped_on_device": t.dropped}, ts=ts,
                        cooldown_s=30, key=f"{t.dev}:gap")
        self._mark_online(d, now)
        self._stalled.discard(t.dev)
        d.update(last_seen=now, last_telemetry=now, boot=t.boot, last_seq=t.seq, rssi=t.rssi, mode=t.mode,
                 itv_ms=t.itv_ms or d.get("itv_ms"))
        inserted = self.st.insert_telemetry({"ts": ts, "received_at": now, "device_id": t.dev, "boot": t.boot,
                                             "seq": t.seq, "buffered": t.buffered, "metrics": t.metrics})
        self.st.upsert_device(d)
        self.metrics.inc("telemetry_total", device=t.dev, buffered=str(t.buffered).lower())
        if not inserted:
            self.metrics.inc("telemetry_duplicates_total", device=t.dev)
        for k, v in t.metrics.items():
            self.metrics.set_gauge("sensor_value", v, device=t.dev, metric=k)
        self._detect_anomalies(t, ts)
        live = not t.buffered  # управляющие воздействия - только по актуальным данным
        if t.type == "tank":
            self._rule_tank(d, t, ts, live)
        elif t.type == "soil":
            self._rule_soil(d, t, ts, live)
        elif t.type == "climate" and live:
            self._rule_climate(d, t, ts)

    # --- обнаружение аномалий: EWMA z-score и «залипание»
    def _detect_anomalies(self, t: Telemetry, ts: datetime) -> None:
        for name, opts in ANOMALY_METRICS.items():
            v = t.metrics.get(name)
            if v is None or (name == "level_pct" and v < 0):
                continue
            det = self._det.setdefault((t.dev, name), EwmaDetector(**opts))
            z = det.update(float(v))
            if z is not None:
                self.metrics.inc("anomalies_total", device=t.dev, metric=name)
                self._event(t.dev, "anomaly", "warning", f"{name}={v:g} deviates from trend (z={z:+.1f})",
                            {"metric": name, "value": v, "z": round(z, 2)}, ts=ts, cooldown_s=60,
                            key=f"{t.dev}:anomaly:{name}")
        for name in STUCK_METRICS:
            v = t.metrics.get(name)
            if v is None or (name == "level_pct" and v < 0):
                continue
            if self._stuck.setdefault((t.dev, name), StuckDetector()).update(float(v)):
                self._event(t.dev, "sensor_stuck", "warning", f"{name} did not change for 120 samples (sensor stuck?)",
                            {"metric": name, "value": v}, ts=ts)

    # --- бак: блокировка насосов при критическом уровне, обнаружение утечки
    def _rule_tank(self, d: Dict[str, Any], t: Telemetry, ts: datetime, live: bool) -> None:
        level = t.metrics.get("level_pct")
        if level is None or level < 0 or not t.metrics.get("sensor_ok", 1):
            return
        self.tank_level = float(level)
        self.tank_crit = float(d["config"].get("level_crit_pct", 10.0))
        rate = self._leak.update(ts.timestamp(), float(level), self.last_pump_ts)
        if rate is not None:
            self._event(d["device_id"], "leak_suspected", "critical",
                        f"tank level falls {rate:.1f} %/min while pumps are idle - possible leak",
                        {"rate_pct_per_min": round(rate, 2)}, ts=ts, cooldown_s=7200, key="leak")
        if live:
            for dev in list(self.devices.values()):
                if dev["type"] == "soil":
                    self._enforce_tank_lock(dev, ts)

    def _enforce_tank_lock(self, soil: Dict[str, Any], ts: datetime) -> None:
        if self.tank_level is None:
            return
        if soil["meta"].get("fault_locked"):
            return  # блокировка по неисправности снимается только оператором
        locked = bool(soil["meta"].get("rule_locked"))
        if not locked and self.tank_level <= self.tank_crit:
            soil["meta"]["rule_locked"] = True
            self.set_config(soil["device_id"], {"pump_enabled": 0}, source="rule:tank_lock")
            self._event(soil["device_id"], "pump_locked", "critical",
                        f"pump locked: tank level {self.tank_level:.0f}% <= {self.tank_crit:.0f}%",
                        {"level_pct": self.tank_level}, ts=ts)
            self.metrics.inc("rule_actions_total", rule="tank_lock")
        elif locked and self.tank_level >= self.tank_crit + TANK_UNLOCK_MARGIN:
            soil["meta"]["rule_locked"] = False
            self.set_config(soil["device_id"], {"pump_enabled": 1}, source="rule:tank_lock")
            self._event(soil["device_id"], "pump_unlocked", "info",
                        f"pump unlocked: tank refilled to {self.tank_level:.0f}%", {"level_pct": self.tank_level}, ts=ts)
            self.metrics.inc("rule_actions_total", rule="tank_unlock")

    # --- полив: учёт запусков насоса и проверка эффективности
    def _rule_soil(self, d: Dict[str, Any], t: Telemetry, ts: datetime, live: bool) -> None:
        pump = int(t.metrics.get("pump_on", 0))
        moist = t.metrics.get("soil_moisture_pct")
        st = self._pump_prev.setdefault(t.dev, {"on": 0, "moist0": moist if moist is not None else 0.0})
        if pump:
            self.last_pump_ts = ts.timestamp()
        if pump and not st["on"] and moist is not None:
            st["moist0"] = float(moist)
        if st["on"] and not pump and moist is not None:
            gain = float(moist) - st["moist0"]
            if gain < 2.0 and not t.metrics.get("pump_locked", 0):
                self._event(t.dev, "irrigation_ineffective", "warning",
                            f"pump ran but soil moisture rose only {gain:+.1f} pp (no water / clogged line?)",
                            {"gain_pp": round(gain, 2)}, ts=ts, cooldown_s=120, key=f"{t.dev}:ineffective")
                self._escalate_pump_fault(d, ts, live)
            elif gain >= 2.0:
                self._ineffective.pop(t.dev, None)  # полив снова эффективен - серия прервана
        st["on"] = pump
        if live and self.tank_level is not None:
            self._enforce_tank_lock(d, ts)

    def _escalate_pump_fault(self, d: Dict[str, Any], ts: datetime, live: bool) -> None:
        """Диагностика: 3 неэффективных полива подряд за 30 мин - насос/магистраль неисправны, насос блокируется
        (чтобы не гонять «вхолостую»); вернуть в работу может только оператор."""
        hist = self._ineffective.setdefault(d["device_id"], [])
        hist.append(ts.timestamp())
        hist[:] = [x for x in hist if ts.timestamp() - x <= 1800]
        if live and len(hist) >= 3 and not d["meta"].get("fault_locked"):
            d["meta"]["fault_locked"] = True
            self.set_config(d["device_id"], {"pump_enabled": 0}, source="rule:pump_fault")
            self._event(d["device_id"], "pump_fault", "critical",
                        "3 ineffective irrigations in a row: pump disabled, check water line (operator must re-enable)",
                        {"count": len(hist)}, ts=ts)
            self.metrics.inc("rule_actions_total", rule="pump_fault")

    # --- микроклимат: тепловая/морозная тревога с гистерезисом и автоматическим действием
    def _latch(self, key: str, on: bool, off: bool) -> Optional[str]:
        cur = self._alarms.get(key, False)
        if not cur and on:
            self._alarms[key] = True
            return "raised"
        if cur and off:
            self._alarms[key] = False
            return "cleared"
        return None

    def _rule_climate(self, d: Dict[str, Any], t: Telemetry, ts: datetime) -> None:
        temp = t.metrics.get("temperature_c")
        if temp is None:
            return
        dev = t.dev
        heat = self._latch(f"{dev}:heat", temp >= self.s.heat_alarm_c, temp <= self.s.heat_alarm_c - 2)
        if heat == "raised":
            self._event(dev, "heat_alarm", "critical", f"temperature {temp:.1f} C >= {self.s.heat_alarm_c:.0f} C",
                        {"temperature_c": temp}, ts=ts)
            if not t.metrics.get("vent_open", 0):
                self._auto_command(dev, "vent_open", {"duration_s": 300}, "rule:heat")
        elif heat == "cleared":
            self._event(dev, "heat_cleared", "info", f"temperature back to {temp:.1f} C", ts=ts)
        frost = self._latch(f"{dev}:frost", temp <= self.s.frost_alarm_c, temp >= self.s.frost_alarm_c + 2)
        if frost == "raised":
            self._event(dev, "frost_alarm", "critical", f"temperature {temp:.1f} C <= {self.s.frost_alarm_c:.0f} C",
                        {"temperature_c": temp}, ts=ts)
            self._auto_command(dev, "vent_close", {"duration_s": 300}, "rule:frost")
            if not t.metrics.get("grow_light_on", 0):
                self._auto_command(dev, "light_on", {"duration_s": 300}, "rule:frost")
        elif frost == "cleared":
            self._event(dev, "frost_cleared", "info", f"temperature back to {temp:.1f} C", ts=ts)

    def _auto_command(self, dev: str, cmd: str, args: dict, source: str) -> None:
        try:
            self.send_command(dev, cmd, args, source=source)
            self.metrics.inc("rule_actions_total", rule=source.split(":")[1])
        except (Conflict, ValidationError, NotFound) as e:
            self._event(dev, "rule_action_failed", "warning", f"{source}: {cmd} not sent: {e}", cooldown_s=60)

    # ------------------------------------------------------------------ статус / события / подтверждения
    def ingest_status(self, s: Status, retained: bool) -> None:
        now = self._now()
        if s.state == "offline":  # LWT: брокер сообщает об обрыве связи
            d = self.devices.get(s.dev)
            if d and d["state"] != "offline":
                d["state"] = "offline"
                self.st.upsert_device(d)
                self._event(s.dev, "device_offline", "critical", f"connection lost ({s.reason or 'lwt'})", ts=now)
            return
        if retained:  # устаревший retained-статус не доказывает, что устройство живо сейчас
            return
        if s.type is None:
            return
        d = self._ensure_device(s.dev, s.type, now)
        self._mark_online(d, now)
        d.update(last_seen=now, fw=s.fw or d.get("fw"), rssi=s.rssi if s.rssi is not None else d.get("rssi"))
        self.st.upsert_device(d)
        self.metrics.inc("heartbeats_total", device=s.dev)
        # Сверка конфигурации: устройство сообщает применённую версию; если она отстаёт (перезагрузка, потеря
        # retained-сообщения) - повторно отправляем актуальную конфигурацию (не чаще раза в 30 с).
        if s.cfg_ver is not None and s.cfg_ver != d["cfg_ver"]:
            last = self._cfg_push.get(s.dev)
            if last is None or (now - last).total_seconds() >= 30:
                self._cfg_push[s.dev] = now
                self.pub.publish(self.topics.config(s.dev), build_config_payload(d["cfg_ver"], d["config"]), retain=True)
                self.metrics.inc("config_resync_total", device=s.dev)

    def ingest_event(self, e: DeviceEvent) -> None:
        now = self._now()
        d = self.devices.get(e.dev)
        if d is None:
            return  # события от неизвестных устройств игнорируем до первой телеметрии
        d["last_seen"] = now
        ts = self._effective_ts(e.ts_ms, now)
        self._event(e.dev, e.type, e.severity, e.message, {"source": "device"}, ts=ts)

    def ingest_cmd_ack(self, a: CmdAck) -> None:
        cmd = self.st.get_command(a.cmd_id)
        if cmd is None or cmd["device_id"] != a.dev:
            return
        now = self._now()
        if cmd["status"] != "sent" and a.reason == "duplicate":
            return
        status = "acked" if a.status == "ok" else "rejected"
        self.st.update_command(a.cmd_id, status=status, acked_at=now, reason=a.reason or None)
        self.metrics.inc("commands_total", status=status)
        if a.status == "rejected":
            self._event(a.dev, "command_rejected", "warning", f"{cmd['cmd']} rejected: {a.reason}",
                        {"cmd_id": a.cmd_id, "source": cmd.get("source")}, ts=now)

    def ingest_cfg_ack(self, a: CfgAck) -> None:
        d = self.devices.get(a.dev)
        if d and a.status == "ok" and a.cfg_ver >= d["cfg_acked"]:
            d["cfg_acked"] = a.cfg_ver
            self.st.upsert_device(d)

    # ------------------------------------------------------------------ операции (API и правила)
    def _device(self, device_id: str) -> Dict[str, Any]:
        d = self.devices.get(device_id)
        if d is None:
            raise NotFound(device_id)
        return d

    def send_command(self, device_id: str, cmd: str, args: Optional[dict] = None, source: str = "api") -> Dict[str, Any]:
        with self.lock:
            d = self._device(device_id)
            args = validate_command(d["type"], cmd, args or {})
            if d["state"] != "online":
                raise Conflict(f"device '{device_id}' is {d['state']}; command not sent")
            now = self._now()
            cmd_id = "c-" + self._new_id()
            rec = {"cmd_id": cmd_id, "ts": now, "device_id": device_id, "cmd": cmd, "args": args, "source": source,
                   "status": "sent", "acked_at": None, "reason": None}
            self.st.insert_command(rec)
            self.pub.publish(self.topics.cmd(device_id), build_command_payload(cmd_id, cmd, args), retain=False)
            self.metrics.inc("commands_total", status="sent")
            return rec

    def set_config(self, device_id: str, changes: dict, source: str = "api") -> Dict[str, Any]:
        with self.lock:
            d = self._device(device_id)
            new = validate_config(d["type"], d["config"], changes)
            d["config"] = new
            d["cfg_ver"] += 1
            d["mode"] = new.get("mode", d.get("mode"))
            if source in ("api", "operator") and changes.get("pump_enabled") == 1:
                d["meta"]["fault_locked"] = False   # оператор вернул насос в работу
                self._ineffective.pop(device_id, None)
            self.st.upsert_device(d)
            self.pub.publish(self.topics.config(device_id), build_config_payload(d["cfg_ver"], new), retain=True)
            self._event(device_id, "config_changed", "info", f"config v{d['cfg_ver']} ({source}): {changes}",
                        {"changes": changes, "source": source, "cfg_ver": d["cfg_ver"]})
            if d["type"] == "tank":
                self.tank_crit = float(new.get("level_crit_pct", self.tank_crit))
            return {"device_id": device_id, "cfg_ver": d["cfg_ver"], "config": new}

    # ------------------------------------------------------------------ watchdog (вызывается периодически)
    def tick(self) -> None:
        """Контроль доступности и «зависаний»: офлайн, остановка телеметрии, тайм-ауты команд, автоперезагрузка."""
        with self.lock:
            now = self._now()
            for d in list(self.devices.values()):
                if d["state"] != "online" or not d.get("last_seen"):
                    continue
                age = (now - d["last_seen"]).total_seconds()
                if age > self.s.offline_after_s:
                    d["state"] = "offline"
                    self.st.upsert_device(d)
                    self._event(d["device_id"], "device_offline", "critical",
                                f"no data for {age:.0f} s (limit {self.s.offline_after_s:.0f} s)", {"age_s": round(age)}, ts=now)
                    continue
                tl = d.get("last_telemetry")
                base_ms = d["config"].get("interval_ms") or d.get("itv_ms") or 5000  # базовый (не адаптивный) интервал
                limit = max(30.0, 4 * base_ms / 1000.0)
                if tl and (now - tl).total_seconds() > limit and d["device_id"] not in self._stalled:
                    self._stalled.add(d["device_id"])
                    self._event(d["device_id"], "telemetry_stalled", "warning",
                                f"heartbeat alive but no telemetry for {(now - tl).total_seconds():.0f} s", ts=now)
                    self._auto_reboot(d, now)
            for c in self.st.pending_commands():
                if (now - c["ts"]).total_seconds() > self.s.cmd_timeout_s:
                    self.st.update_command(c["cmd_id"], status="timeout", reason="no ack from device")
                    self.metrics.inc("commands_total", status="timeout")
                    self._event(c["device_id"], "command_timeout", "warning",
                                f"{c['cmd']} was not acknowledged within {self.s.cmd_timeout_s:.0f} s",
                                {"cmd_id": c["cmd_id"]}, ts=now)

    def _auto_reboot(self, d: Dict[str, Any], now: datetime) -> None:
        """Автоматическое восстановление: перезагрузка зависшего узла (не чаще N раз в час)."""
        hist = [t for t in self._reboots.get(d["device_id"], []) if (now - t).total_seconds() < 3600]
        if len(hist) >= self.s.auto_reboot_max_per_hour:
            self._event(d["device_id"], "auto_reboot_suppressed", "warning", "reboot limit reached", cooldown_s=600)
            return
        try:
            self.send_command(d["device_id"], "reboot", {}, source="auto:watchdog")
            hist.append(now)
            self._reboots[d["device_id"]] = hist
            self.metrics.inc("rule_actions_total", rule="auto_reboot")
        except (Conflict, ValidationError):
            pass

    # ------------------------------------------------------------------ запросы для API
    @staticmethod
    def _locked(d: Dict[str, Any]) -> bool:
        return bool(d["meta"].get("rule_locked") or d["meta"].get("fault_locked"))

    def _public_device(self, d: Dict[str, Any]) -> Dict[str, Any]:
        now = self._now()
        out = {k: v for k, v in d.items() if k != "meta"}
        out["age_s"] = round((now - d["last_seen"]).total_seconds(), 1) if d.get("last_seen") else None
        out["locked_by_rule"] = self._locked(d)
        latest = self.st.latest_telemetry(d["device_id"])
        out["latest"] = latest["metrics"] if latest else None
        out["latest_ts"] = latest["ts"] if latest else None
        return out

    def list_devices(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [self._public_device(d) for _, d in sorted(self.devices.items())]

    def get_device(self, device_id: str) -> Dict[str, Any]:
        with self.lock:
            return self._public_device(self._device(device_id))

    def query_telemetry(self, device_id: str, start=None, end=None, limit: int = 1000, order: str = "asc"):
        self._device(device_id)
        return self.st.query_telemetry(device_id, start, end, max(1, min(limit, 10000)), order)

    def aggregate(self, device_id: str, metric: str, bucket_s: int, start=None, end=None):
        self._device(device_id)
        if bucket_s < 1 or bucket_s > 86400:
            raise ValidationError("bucket_s must be within [1, 86400]")
        return self.st.aggregate(device_id, metric, bucket_s, start, end)

    def events(self, limit: int = 100, severity=None, device_id=None):
        if severity is not None and severity not in ("info", "warning", "critical"):
            raise ValidationError("severity must be info|warning|critical")
        return self.st.query_events(max(1, min(limit, 1000)), severity, device_id)

    def commands(self, limit: int = 100, device_id=None):
        return self.st.list_commands(max(1, min(limit, 1000)), device_id)

    def summary(self) -> Dict[str, Any]:
        with self.lock:
            devs = list(self.devices.values())
            return {"devices_total": len(devs), "devices_online": sum(1 for d in devs if d["state"] == "online"),
                    "pump_locked": any(self._locked(d) for d in devs), "tank_level_pct": self.tank_level,
                    "uptime_s": round((self._now() - self.started).total_seconds()), "counts": self.st.counts(),
                    "mqtt_connected": self.pub.is_connected()}

    def render_metrics(self) -> str:
        with self.lock:
            now = self._now()
            self.metrics.clear_gauge("device_online")
            self.metrics.clear_gauge("device_last_seen_age_seconds")
            for d in self.devices.values():
                self.metrics.set_gauge("device_online", 1 if d["state"] == "online" else 0, device=d["device_id"], type=d["type"])
                if d.get("last_seen"):
                    self.metrics.set_gauge("device_last_seen_age_seconds", (now - d["last_seen"]).total_seconds(),
                                           device=d["device_id"])
            self.metrics.set_gauge("mqtt_connected", 1 if self.pub.is_connected() else 0)
            self.metrics.set_gauge("pump_locked", 1 if any(self._locked(d) for d in self.devices.values()) else 0)
        return self.metrics.render()
