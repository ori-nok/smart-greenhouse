"""HTTP API (FastAPI): состояние системы, телеметрия, события, команды, конфигурация, метрики, веб-панель."""
from __future__ import annotations

import csv
import hmac
import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .engine import RateLimiter
from .protocol import ValidationError
from .service import Conflict, GreenhouseService, NotFound

log = logging.getLogger("greenhouse.api")
STATIC = os.path.join(os.path.dirname(__file__), "static")


class CommandRequest(BaseModel):
    cmd: str = Field(..., examples=["pump_on"], description="Команда: pump_on, pump_off, vent_open, light_on, buzzer_mute, reboot, ping ...")
    duration_s: Optional[int] = Field(None, examples=[20], description="Длительность действия, с (если применимо)")


class Watchdog(threading.Thread):
    def __init__(self, service: GreenhouseService, interval_s: float) -> None:
        super().__init__(daemon=True, name="watchdog")
        self.service, self.interval = service, interval_s
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                self.service.tick()
            except Exception:
                log.exception("watchdog tick failed")

    def stop(self) -> None:
        self._stop_evt.set()


def create_app(service: Optional[GreenhouseService] = None, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    holder: Dict[str, Any] = {"service": service}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        parts = []
        if holder["service"] is None:  # боевой запуск: БД + MQTT + watchdog
            from .mqtt_bridge import MqttBridge
            from .notify import TelegramNotifier
            from .storage import PostgresStorage

            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            storage = PostgresStorage(settings.database_url)
            storage.init()
            bridge = MqttBridge(settings)
            notifier = TelegramNotifier(settings.telegram_token, settings.telegram_chat_id) if settings.telegram_token else None
            svc = GreenhouseService(storage, bridge, settings, notifier=notifier)
            bridge.set_handlers(svc.handle_message, svc.on_mqtt_connected)
            bridge.start()
            wd = Watchdog(svc, settings.watchdog_interval_s)
            wd.start()
            holder["service"] = svc
            parts = [wd, bridge, storage]
        yield
        if parts:
            parts[0].stop()
            parts[1].stop()
            parts[2].close()

    app = FastAPI(title="Smart Greenhouse API", version="1.0.0", lifespan=lifespan,
                  description="API распределённой микроконтроллерной системы «Умная теплица». Авторизация: заголовок X-API-Key.")
    limiter = RateLimiter(settings.rate_limit_per_min)
    started = time.time()

    def svc() -> GreenhouseService:
        s = holder["service"]
        if s is None:
            raise HTTPException(503, "service is starting")
        return s

    def auth(request: Request, x_api_key: Optional[str] = Header(default=None)) -> None:
        if not hmac.compare_digest((x_api_key or "").encode(), settings.api_key.encode()):
            raise HTTPException(401, "invalid or missing X-API-Key")
        ok, retry = limiter.allow(request.client.host if request.client else "?", time.monotonic())
        if not ok:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(int(retry) + 1)})

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound):
        return JSONResponse({"detail": f"not found: {e}"}, status_code=404)

    @app.exception_handler(Conflict)
    async def _cf(_: Request, e: Conflict):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(ValidationError)
    async def _ve(_: Request, e: ValidationError):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.get("/health", tags=["system"], summary="Проверка работоспособности (БД, MQTT)")
    def health():
        s = holder["service"]
        db = bool(s and s.st.ping())
        mqtt_ok = bool(s and s.pub.is_connected())
        body = {"status": "ok" if db else "degraded", "db": db, "mqtt": mqtt_ok, "uptime_s": round(time.time() - started)}
        return JSONResponse(body, status_code=200 if db else 503)

    @app.get("/metrics", tags=["system"], response_class=PlainTextResponse, summary="Метрики Prometheus")
    def metrics():
        return PlainTextResponse(svc().render_metrics(), media_type="text/plain; version=0.0.4")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui")

    @app.get("/ui", include_in_schema=False)
    def ui():
        return FileResponse(os.path.join(STATIC, "index.html"))

    api = "/api/v1"

    @app.get(f"{api}/summary", tags=["system"], dependencies=[Depends(auth)])
    def summary():
        return svc().summary()

    @app.get(f"{api}/devices", tags=["devices"], dependencies=[Depends(auth)], summary="Список устройств и их состояние")
    def devices():
        return svc().list_devices()

    @app.get(f"{api}/devices/{{device_id}}", tags=["devices"], dependencies=[Depends(auth)])
    def device(device_id: str):
        return svc().get_device(device_id)

    @app.get(f"{api}/devices/{{device_id}}/telemetry", tags=["telemetry"], dependencies=[Depends(auth)])
    def telemetry(device_id: str, start: Optional[datetime] = None, end: Optional[datetime] = None,
                  limit: int = Query(1000, ge=1, le=10000), order: str = Query("asc", pattern="^(asc|desc)$")):
        return svc().query_telemetry(device_id, start, end, limit, order)

    @app.get(f"{api}/devices/{{device_id}}/aggregate", tags=["telemetry"], dependencies=[Depends(auth)],
             summary="Агрегаты по интервалам (time_bucket в TimescaleDB)")
    def aggregate(device_id: str, metric: str = Query(..., pattern="^[a-z][a-z0-9_]{0,39}$"),
                  bucket_s: int = Query(60, ge=1, le=86400), start: Optional[datetime] = None, end: Optional[datetime] = None):
        return svc().aggregate(device_id, metric, bucket_s, start, end)

    @app.get(f"{api}/telemetry/latest", tags=["telemetry"], dependencies=[Depends(auth)])
    def latest():
        return {d["device_id"]: {"ts": d["latest_ts"], "metrics": d["latest"]} for d in svc().list_devices()}

    @app.post(f"{api}/devices/{{device_id}}/commands", tags=["control"], dependencies=[Depends(auth)], status_code=202,
              summary="Отправить команду устройству (подтверждение приходит асинхронно, см. GET /commands)")
    def command(device_id: str, body: CommandRequest):
        args = {} if body.duration_s is None else {"duration_s": body.duration_s}
        return svc().send_command(device_id, body.cmd, args, source="api")

    @app.put(f"{api}/devices/{{device_id}}/config", tags=["control"], dependencies=[Depends(auth)],
             summary="Удалённое изменение конфигурации (retained-сообщение, применяется и после перезагрузки)")
    def config(device_id: str, changes: Dict[str, Any] = Body(..., examples=[{"moist_low": 25, "interval_ms": 3000}])):
        return svc().set_config(device_id, changes, source="api")

    @app.get(f"{api}/events", tags=["events"], dependencies=[Depends(auth)])
    def events(limit: int = Query(100, ge=1, le=1000), severity: Optional[str] = None, device_id: Optional[str] = None):
        return svc().events(limit, severity, device_id)

    @app.get(f"{api}/commands", tags=["control"], dependencies=[Depends(auth)], summary="История управляющих команд")
    def commands(limit: int = Query(100, ge=1, le=1000), device_id: Optional[str] = None):
        return svc().commands(limit, device_id)

    @app.get(f"{api}/export/telemetry.csv", tags=["telemetry"], dependencies=[Depends(auth)], summary="Выгрузка телеметрии в CSV")
    def export_csv(device_id: Optional[str] = None, start: Optional[datetime] = None, end: Optional[datetime] = None,
                   limit: int = Query(100000, ge=1, le=1000000)):
        s = svc()
        rows = s.st.query_telemetry(device_id, start, end, limit, "asc")
        keys = sorted({k for r in rows for k in r["metrics"]})

        def gen():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["ts", "device_id", "boot", "seq", "buffered"] + keys)
            yield buf.getvalue(); buf.seek(0); buf.truncate()
            for r in rows:
                w.writerow([r["ts"].isoformat(), r["device_id"], r["boot"], r["seq"], int(r["buffered"])]
                           + [r["metrics"].get(k, "") for k in keys])
                yield buf.getvalue(); buf.seek(0); buf.truncate()

        return StreamingResponse(gen(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=telemetry.csv"})

    return app


app = create_app()
