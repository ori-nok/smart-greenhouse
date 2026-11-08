"""Настройки backend из переменных окружения."""
import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    topic_root: str = "gh-k7q2x9/greenhouse"
    database_url: str = "postgresql://greenhouse:greenhouse@timescaledb:5432/greenhouse"
    api_key: str = "dev-key-change-me"
    offline_after_s: float = 35.0      # нет сообщений дольше - устройство считается недоступным
    cmd_timeout_s: float = 15.0        # ожидание ack на команду
    watchdog_interval_s: float = 5.0
    rate_limit_per_min: int = 120
    heat_alarm_c: float = 35.0
    frost_alarm_c: float = 3.0
    auto_reboot_max_per_hour: int = 3  # автовосстановление: лимит перезагрузок на устройство
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            mqtt_host=e("MQTT_HOST", cls.mqtt_host),
            mqtt_port=int(e("MQTT_PORT", cls.mqtt_port)),
            topic_root=e("TOPIC_ROOT", cls.topic_root),
            database_url=e("DATABASE_URL", cls.database_url),
            api_key=e("API_KEY", cls.api_key),
            offline_after_s=_f("OFFLINE_AFTER_S", cls.offline_after_s),
            cmd_timeout_s=_f("CMD_TIMEOUT_S", cls.cmd_timeout_s),
            watchdog_interval_s=_f("WATCHDOG_INTERVAL_S", cls.watchdog_interval_s),
            rate_limit_per_min=int(e("RATE_LIMIT_PER_MIN", cls.rate_limit_per_min)),
            heat_alarm_c=_f("HEAT_ALARM_C", cls.heat_alarm_c),
            frost_alarm_c=_f("FROST_ALARM_C", cls.frost_alarm_c),
            auto_reboot_max_per_hour=int(e("AUTO_REBOOT_MAX_PER_HOUR", cls.auto_reboot_max_per_hour)),
            telegram_token=e("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=e("TELEGRAM_CHAT_ID", ""),
        )
