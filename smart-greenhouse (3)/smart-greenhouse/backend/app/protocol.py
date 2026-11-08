"""Протокол обмена: топики, разбор и проверка сообщений, схемы команд и конфигурации.
Документация формата - docs/PROTOCOL.md. Модуль не зависит от внешних библиотек."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

DEVICE_TYPES = ("soil", "tank", "climate")
SEVERITIES = ("info", "warning", "critical")
DEV_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
KINDS = ("telemetry", "status", "event", "cmd/ack", "config/ack")


class ProtocolError(ValueError):
    """Сообщение не соответствует протоколу."""


class ValidationError(ValueError):
    """Неверный запрос оператора (команда/конфигурация)."""


class Topics:
    def __init__(self, root: str):
        self.root = root.strip("/")

    def subscriptions(self) -> List[str]:
        return [f"{self.root}/+/{k}" for k in KINDS]

    def cmd(self, dev: str) -> str:
        return f"{self.root}/{dev}/cmd"

    def config(self, dev: str) -> str:
        return f"{self.root}/{dev}/config"

    def parse(self, topic: str) -> Optional[Tuple[str, str]]:
        """Возвращает (device_id, kind) для входящих топиков устройства или None."""
        prefix = self.root + "/"
        if not topic.startswith(prefix):
            return None
        rest = topic[len(prefix):]
        if "/" not in rest:
            return None
        dev, kind = rest.split("/", 1)
        if kind not in KINDS or not DEV_RE.match(dev):
            return None
        return dev, kind


@dataclass
class Telemetry:
    dev: str
    type: str
    boot: int
    seq: int
    ts_ms: int
    up_ms: int
    buffered: bool
    dropped: int
    mode: str
    itv_ms: int
    rssi: Optional[int]
    metrics: Dict[str, float]


@dataclass
class Status:
    dev: str
    state: str
    type: Optional[str] = None
    fw: Optional[str] = None
    boot: Optional[int] = None
    up_ms: Optional[int] = None
    rssi: Optional[int] = None
    buf_n: Optional[int] = None
    cfg_ver: Optional[int] = None
    reason: Optional[str] = None


@dataclass
class DeviceEvent:
    dev: str
    type: str
    severity: str
    message: str
    ts_ms: int = 0


@dataclass
class CmdAck:
    dev: str
    cmd_id: str
    status: str
    reason: str


@dataclass
class CfgAck:
    dev: str
    cfg_ver: int
    status: str


def _obj(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ProtocolError("payload is not UTF-8") from e
    if len(payload) > 4096:
        raise ProtocolError("payload too large")
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError) as e:
        raise ProtocolError(f"invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ProtocolError("JSON object expected")
    if obj.get("v") != 1:
        raise ProtocolError("unsupported protocol version")
    return obj


def _int(obj: Dict[str, Any], key: str, required: bool = True, lo: int = 0, hi: int = 2**53) -> Optional[int]:
    if key not in obj:
        if required:
            raise ProtocolError(f"missing field {key}")
        return None
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not v.is_integer()):
        raise ProtocolError(f"field {key} must be an integer")
    v = int(v)
    if not lo <= v <= hi:
        raise ProtocolError(f"field {key} out of range")
    return v


def _str(obj: Dict[str, Any], key: str, required: bool = True, maxlen: int = 200) -> Optional[str]:
    if key not in obj:
        if required:
            raise ProtocolError(f"missing field {key}")
        return None
    v = obj[key]
    if not isinstance(v, str):
        raise ProtocolError(f"field {key} must be a string")
    return v[:maxlen]


def _dev(obj: Dict[str, Any]) -> str:
    d = _str(obj, "dev")
    if not DEV_RE.match(d or ""):
        raise ProtocolError("invalid device id")
    return d


def parse_telemetry(payload: Any) -> Telemetry:
    o = _obj(payload)
    dtype = _str(o, "type")
    if dtype not in DEVICE_TYPES:
        raise ProtocolError(f"unknown device type {dtype}")
    m = o.get("m")
    if not isinstance(m, dict) or not m or len(m) > 32:
        raise ProtocolError("field m must be a non-empty object")
    metrics: Dict[str, float] = {}
    for k, v in m.items():
        if not KEY_RE.match(k):
            raise ProtocolError(f"invalid metric name {k}")
        if isinstance(v, bool):
            v = int(v)
        if not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise ProtocolError(f"metric {k} must be a finite number")
        metrics[k] = v
    return Telemetry(
        dev=_dev(o), type=dtype, boot=_int(o, "boot"), seq=_int(o, "seq"), ts_ms=_int(o, "ts"),
        up_ms=_int(o, "up", required=False) or 0, buffered=bool(_int(o, "buf", required=False, hi=1) or 0),
        dropped=_int(o, "drop", required=False) or 0, mode=_str(o, "mode", required=False, maxlen=12) or "auto",
        itv_ms=_int(o, "itv", required=False) or 0, rssi=_int(o, "rssi", required=False, lo=-200, hi=0),
        metrics=metrics)


def parse_status(payload: Any) -> Status:
    o = _obj(payload)
    state = _str(o, "state")
    if state not in ("online", "offline"):
        raise ProtocolError("state must be online|offline")
    dtype = _str(o, "type", required=False)
    if dtype is not None and dtype not in DEVICE_TYPES:
        raise ProtocolError(f"unknown device type {dtype}")
    return Status(dev=_dev(o), state=state, type=dtype, fw=_str(o, "fw", required=False, maxlen=24),
                  boot=_int(o, "boot", required=False), up_ms=_int(o, "up", required=False),
                  rssi=_int(o, "rssi", required=False, lo=-200, hi=0), buf_n=_int(o, "buf_n", required=False),
                  cfg_ver=_int(o, "cfg_ver", required=False), reason=_str(o, "reason", required=False, maxlen=40))


def parse_event(payload: Any) -> DeviceEvent:
    o = _obj(payload)
    etype = _str(o, "type", maxlen=40)
    if not KEY_RE.match(etype or ""):
        raise ProtocolError("invalid event type")
    sev = _str(o, "sev")
    if sev not in SEVERITIES:
        raise ProtocolError("invalid severity")
    return DeviceEvent(dev=_dev(o), type=etype, severity=sev, message=_str(o, "msg", required=False) or "",
                       ts_ms=_int(o, "ts", required=False) or 0)


def parse_cmd_ack(payload: Any) -> CmdAck:
    o = _obj(payload)
    st = _str(o, "status")
    if st not in ("ok", "rejected"):
        raise ProtocolError("invalid ack status")
    return CmdAck(dev=_dev(o), cmd_id=_str(o, "cmd_id", maxlen=32), status=st, reason=_str(o, "reason", required=False) or "")


def parse_cfg_ack(payload: Any) -> CfgAck:
    o = _obj(payload)
    return CfgAck(dev=_dev(o), cfg_ver=_int(o, "cfg_ver"), status=_str(o, "status", maxlen=12))


CONFIG_SCHEMA: Dict[str, Dict[str, Any]] = {
    "common": {"interval_ms": (1000, 60000, int), "mode": ("auto", "manual")},
    "soil": {"moist_low": (0, 100, float), "moist_high": (0, 100, float), "pump_enabled": (0, 1, int),
             "pump_max_run_s": (5, 600, int), "pump_cooldown_s": (0, 3600, int)},
    "tank": {"tank_height_cm": (10, 400, float), "level_low_pct": (0, 100, float), "level_crit_pct": (0, 100, float)},
    "climate": {"temp_open_c": (-20, 60, float), "temp_close_c": (-20, 60, float),
                "lux_low": (0, 100000, float), "lux_high": (0, 100000, float)},
}
DEFAULT_CONFIG: Dict[str, Dict[str, Any]] = {
    "common": {"interval_ms": 5000, "mode": "auto"},
    "soil": {"moist_low": 30.0, "moist_high": 60.0, "pump_enabled": 1, "pump_max_run_s": 30, "pump_cooldown_s": 20},
    "tank": {"tank_height_cm": 100.0, "level_low_pct": 25.0, "level_crit_pct": 10.0},
    "climate": {"temp_open_c": 28.0, "temp_close_c": 25.0, "lux_low": 200.0, "lux_high": 400.0},
}
COMMANDS: Dict[str, Dict[str, Dict[str, Tuple[int, int]]]] = {
    "common": {"ping": {}, "reboot": {}},
    "soil": {"pump_on": {"duration_s": (1, 600)}, "pump_off": {"duration_s": (1, 3600)}},
    "tank": {"buzzer_mute": {"duration_s": (1, 600)}},
    "climate": {"vent_open": {"duration_s": (1, 3600)}, "vent_close": {"duration_s": (1, 3600)},
                "light_on": {"duration_s": (1, 3600)}, "light_off": {"duration_s": (1, 3600)}},
}


def default_config(dtype: str) -> Dict[str, Any]:
    return {**DEFAULT_CONFIG["common"], **DEFAULT_CONFIG[dtype]}


def validate_config(dtype: str, current: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
    """Проверяет изменения и возвращает полную новую конфигурацию."""
    if not isinstance(changes, dict) or not changes:
        raise ValidationError("no config fields given")
    schema = {**CONFIG_SCHEMA["common"], **CONFIG_SCHEMA[dtype]}
    new = dict(current)
    for k, v in changes.items():
        if k not in schema:
            raise ValidationError(f"unknown config field {k} for type {dtype}")
        spec = schema[k]
        if k == "mode":
            if v not in spec:
                raise ValidationError("mode must be auto|manual")
            new[k] = v
            continue
        lo, hi, typ = spec
        if isinstance(v, bool):
            v = int(v)
        if not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise ValidationError(f"{k} must be a number")
        if not lo <= v <= hi:
            raise ValidationError(f"{k} must be within [{lo}, {hi}]")
        new[k] = typ(v)
    pairs = {"soil": ("moist_low", "moist_high"), "tank": ("level_crit_pct", "level_low_pct"),
             "climate": ("temp_close_c", "temp_open_c")}
    if dtype in pairs and not new[pairs[dtype][0]] < new[pairs[dtype][1]]:
        raise ValidationError(f"{pairs[dtype][0]} must be less than {pairs[dtype][1]}")
    if dtype == "climate" and not new["lux_low"] < new["lux_high"]:
        raise ValidationError("lux_low must be less than lux_high")
    return new


def validate_command(dtype: str, cmd: str, args: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {**COMMANDS["common"], **COMMANDS[dtype]}
    if cmd not in allowed:
        raise ValidationError(f"command {cmd} is not supported by {dtype} (allowed: {sorted(allowed)})")
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k not in allowed[cmd]:
            raise ValidationError(f"unknown argument {k} for {cmd}")
        lo, hi = allowed[cmd][k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValidationError(f"{k} must be an integer within [{lo}, {hi}]")
        out[k] = v
    return out


def build_command_payload(cmd_id: str, cmd: str, args: Dict[str, Any]) -> str:
    return json.dumps({"cmd_id": cmd_id, "cmd": cmd, **args}, separators=(",", ":"))


def build_config_payload(cfg_ver: int, config: Dict[str, Any]) -> str:
    return json.dumps({"cfg_ver": cfg_ver, **config}, separators=(",", ":"))
