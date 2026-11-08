"""Слой хранения. Две реализации одного интерфейса:
  * InMemoryStorage - для тестов и офлайн-симулятора (без БД);
  * PostgresStorage - PostgreSQL + TimescaleDB (гипертаблица telemetry), используется в Docker.
Все временные метки - timezone-aware datetime (UTC)."""
from __future__ import annotations

import copy
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEVICE_FIELDS = ("device_id", "type", "fw", "state", "mode", "boot", "last_seq", "first_seen", "last_seen",
                 "last_telemetry", "rssi", "itv_ms", "config", "cfg_ver", "cfg_acked", "meta")
CMD_UPDATABLE = ("status", "acked_at", "reason")


def _utc_min() -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc_max() -> datetime:
    return datetime(2999, 1, 1, tzinfo=timezone.utc)


# =============================================================================== in-memory
class InMemoryStorage:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.devices: Dict[str, Dict[str, Any]] = {}
        self.telemetry: List[Dict[str, Any]] = []
        self._tkeys = set()
        self.events: List[Dict[str, Any]] = []
        self.commands: Dict[str, Dict[str, Any]] = {}
        self._eid = 0

    def init(self) -> None: ...

    def close(self) -> None: ...

    def ping(self) -> bool:
        return True

    def upsert_device(self, dev: Dict[str, Any]) -> None:
        with self._lock:
            self.devices[dev["device_id"]] = copy.deepcopy({k: dev.get(k) for k in DEVICE_FIELDS})

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            d = self.devices.get(device_id)
            return copy.deepcopy(d) if d else None

    def list_devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(d) for _, d in sorted(self.devices.items())]

    def insert_telemetry(self, row: Dict[str, Any]) -> bool:
        key = (row["device_id"], row["boot"], row["seq"], row["ts"])
        with self._lock:
            if key in self._tkeys:
                return False
            self._tkeys.add(key)
            self.telemetry.append(copy.deepcopy(row))
            return True

    def query_telemetry(self, device_id: Optional[str], start: Optional[datetime], end: Optional[datetime],
                        limit: int = 1000, order: str = "asc") -> List[Dict[str, Any]]:
        start, end = start or _utc_min(), end or _utc_max()
        with self._lock:
            rows = [r for r in self.telemetry if (device_id is None or r["device_id"] == device_id)
                    and start <= r["ts"] <= end]
        rows.sort(key=lambda r: (r["ts"], r["seq"]), reverse=(order == "desc"))
        return copy.deepcopy(rows[:limit])

    def latest_telemetry(self, device_id: str) -> Optional[Dict[str, Any]]:
        rows = self.query_telemetry(device_id, None, None, 1, "desc")
        return rows[0] if rows else None

    def aggregate(self, device_id: str, metric: str, bucket_s: int, start: Optional[datetime],
                  end: Optional[datetime]) -> List[Dict[str, Any]]:
        buckets: Dict[int, List[float]] = {}
        for r in self.query_telemetry(device_id, start, end, 10**9):
            v = r["metrics"].get(metric)
            if v is None:
                continue
            buckets.setdefault(int(r["ts"].timestamp() // bucket_s) * bucket_s, []).append(float(v))
        return [{"bucket": datetime.fromtimestamp(b, timezone.utc), "avg": sum(v) / len(v), "min": min(v),
                 "max": max(v), "n": len(v)} for b, v in sorted(buckets.items())]

    def insert_event(self, ev: Dict[str, Any]) -> int:
        with self._lock:
            self._eid += 1
            self.events.append({"id": self._eid, **copy.deepcopy(ev)})
            return self._eid

    def query_events(self, limit: int = 100, severity: Optional[str] = None, device_id: Optional[str] = None,
                     etype: Optional[str] = None, start: Optional[datetime] = None) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [e for e in self.events if (severity is None or e["severity"] == severity)
                    and (device_id is None or e["device_id"] == device_id)
                    and (etype is None or e["type"] == etype) and (start is None or e["ts"] >= start)]
        rows.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
        return copy.deepcopy(rows[:limit])

    def insert_command(self, c: Dict[str, Any]) -> None:
        with self._lock:
            self.commands[c["cmd_id"]] = copy.deepcopy(c)

    def update_command(self, cmd_id: str, **fields: Any) -> bool:
        with self._lock:
            c = self.commands.get(cmd_id)
            if not c:
                return False
            for k, v in fields.items():
                if k in CMD_UPDATABLE:
                    c[k] = v
            return True

    def get_command(self, cmd_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            c = self.commands.get(cmd_id)
            return copy.deepcopy(c) if c else None

    def pending_commands(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(c) for c in self.commands.values() if c["status"] == "sent"]

    def list_commands(self, limit: int = 100, device_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [c for c in self.commands.values() if device_id is None or c["device_id"] == device_id]
        rows.sort(key=lambda c: c["ts"], reverse=True)
        return copy.deepcopy(rows[:limit])

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {"devices": len(self.devices), "telemetry": len(self.telemetry), "events": len(self.events),
                    "commands": len(self.commands)}


# =============================================================================== PostgreSQL / TimescaleDB
class PostgresStorage:
    def __init__(self, dsn: str, schema_path: Optional[str] = None) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.schema_path = schema_path or os.path.join(os.path.dirname(__file__), "schema.sql")
        self.pool = ConnectionPool(dsn, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=False)

    def init(self, retries: int = 30, delay_s: float = 2.0) -> None:
        """Открывает пул (ждёт готовности БД) и идемпотентно применяет схему."""
        last: Optional[Exception] = None
        for _ in range(retries):
            try:
                self.pool.open(wait=True, timeout=5)
                with open(self.schema_path, encoding="utf-8") as f:
                    sql = f.read()
                with self.pool.connection() as conn:
                    conn.execute(sql)
                return
            except Exception as e:  # БД ещё не готова
                last = e
                time.sleep(delay_s)
        raise RuntimeError(f"database is not available: {last}")

    def close(self) -> None:
        self.pool.close()

    def ping(self) -> bool:
        try:
            with self.pool.connection(timeout=3) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    @staticmethod
    def _j(v: Any):
        from psycopg.types.json import Jsonb

        return Jsonb(v if v is not None else {})

    def upsert_device(self, dev: Dict[str, Any]) -> None:
        p = {k: dev.get(k) for k in DEVICE_FIELDS}
        p["config"], p["meta"] = self._j(p["config"]), self._j(p["meta"])
        p["cfg_ver"], p["cfg_acked"] = p["cfg_ver"] or 0, p["cfg_acked"] or 0
        cols = ", ".join(DEVICE_FIELDS)
        vals = ", ".join(f"%({k})s" for k in DEVICE_FIELDS)
        upd = ", ".join(f"{k} = EXCLUDED.{k}" for k in DEVICE_FIELDS if k not in ("device_id", "first_seen"))
        with self.pool.connection() as conn:
            conn.execute(f"INSERT INTO devices ({cols}) VALUES ({vals}) ON CONFLICT (device_id) DO UPDATE SET {upd}", p)

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,)).fetchone()

    def list_devices(self) -> List[Dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM devices ORDER BY device_id").fetchall()

    def insert_telemetry(self, row: Dict[str, Any]) -> bool:
        with self.pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO telemetry (ts, received_at, device_id, boot, seq, buffered, metrics) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (row["ts"], row["received_at"], row["device_id"], row["boot"], row["seq"], row["buffered"],
                 self._j(row["metrics"])))
            return cur.rowcount == 1

    def query_telemetry(self, device_id: Optional[str], start: Optional[datetime], end: Optional[datetime],
                        limit: int = 1000, order: str = "asc") -> List[Dict[str, Any]]:
        direction = "DESC" if order == "desc" else "ASC"
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT ts, received_at, device_id, boot, seq, buffered, metrics FROM telemetry "
                "WHERE (%(dev)s::text IS NULL OR device_id = %(dev)s::text) AND ts >= %(s)s AND ts <= %(e)s "
                f"ORDER BY ts {direction}, seq {direction} LIMIT %(lim)s",
                {"dev": device_id, "s": start or _utc_min(), "e": end or _utc_max(), "lim": limit}).fetchall()

    def latest_telemetry(self, device_id: str) -> Optional[Dict[str, Any]]:
        rows = self.query_telemetry(device_id, None, None, 1, "desc")
        return rows[0] if rows else None

    def aggregate(self, device_id: str, metric: str, bucket_s: int, start: Optional[datetime],
                  end: Optional[datetime]) -> List[Dict[str, Any]]:
        # time_bucket - функция TimescaleDB: агрегирование временного ряда на стороне БД.
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT time_bucket(make_interval(secs => %(b)s::float8), ts) AS bucket, "
                "avg((metrics ->> %(m)s::text)::float8) AS avg, min((metrics ->> %(m)s::text)::float8) AS min, "
                "max((metrics ->> %(m)s::text)::float8) AS max, count(*)::int AS n "
                "FROM telemetry WHERE device_id = %(dev)s AND ts >= %(s)s AND ts <= %(e)s "
                "AND (metrics ->> %(m)s::text) IS NOT NULL GROUP BY bucket ORDER BY bucket",
                {"b": float(bucket_s), "m": metric, "dev": device_id, "s": start or _utc_min(),
                 "e": end or _utc_max()}).fetchall()

    def insert_event(self, ev: Dict[str, Any]) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO events (ts, device_id, type, severity, message, data) VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id", (ev["ts"], ev["device_id"], ev["type"], ev["severity"], ev["message"],
                                 self._j(ev.get("data")))).fetchone()
            return int(row["id"])

    def query_events(self, limit: int = 100, severity: Optional[str] = None, device_id: Optional[str] = None,
                     etype: Optional[str] = None, start: Optional[datetime] = None) -> List[Dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT id, ts, device_id, type, severity, message, data FROM events "
                "WHERE (%(sev)s::text IS NULL OR severity = %(sev)s::text) "
                "AND (%(dev)s::text IS NULL OR device_id = %(dev)s::text) "
                "AND (%(typ)s::text IS NULL OR type = %(typ)s::text) "
                "AND (%(s)s::timestamptz IS NULL OR ts >= %(s)s::timestamptz) "
                "ORDER BY ts DESC, id DESC LIMIT %(lim)s",
                {"sev": severity, "dev": device_id, "typ": etype, "s": start, "lim": limit}).fetchall()

    def insert_command(self, c: Dict[str, Any]) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO commands (cmd_id, ts, device_id, cmd, args, source, status, acked_at, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (c["cmd_id"], c["ts"], c["device_id"], c["cmd"], self._j(c.get("args")), c.get("source"),
                 c["status"], c.get("acked_at"), c.get("reason")))

    def update_command(self, cmd_id: str, **fields: Any) -> bool:
        items = [(k, v) for k, v in fields.items() if k in CMD_UPDATABLE]
        if not items:
            return False
        sets = ", ".join(f"{k} = %s" for k, _ in items)
        with self.pool.connection() as conn:
            cur = conn.execute(f"UPDATE commands SET {sets} WHERE cmd_id = %s", [v for _, v in items] + [cmd_id])
            return cur.rowcount == 1

    def get_command(self, cmd_id: str) -> Optional[Dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM commands WHERE cmd_id = %s", (cmd_id,)).fetchone()

    def pending_commands(self) -> List[Dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM commands WHERE status = 'sent'").fetchall()

    def list_commands(self, limit: int = 100, device_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM commands WHERE (%(dev)s::text IS NULL OR device_id = %(dev)s::text) "
                "ORDER BY ts DESC LIMIT %(lim)s", {"dev": device_id, "lim": limit}).fetchall()

    def counts(self) -> Dict[str, int]:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT (SELECT count(*) FROM devices)::int AS devices, "
                "(SELECT count(*) FROM telemetry)::int AS telemetry, (SELECT count(*) FROM events)::int AS events, "
                "(SELECT count(*) FROM commands)::int AS commands").fetchone()
            return dict(row)
