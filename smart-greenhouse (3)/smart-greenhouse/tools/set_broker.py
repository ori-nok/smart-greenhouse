#!/usr/bin/env python3
"""Меняет адрес публичного MQTT-брокера в прошивках (GH_MQTT_HOST) и в .env/.env.example (MQTT_HOST),
затем синхронизирует копии в папках узлов.

    python tools/set_broker.py broker.emqx.io                       # и прошивки, и .env
    python tools/set_broker.py host.wokwi.internal --firmware-only   # только прошивки (backend остаётся на своём брокере)
"""
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def sub(rel: str, pat: str, repl) -> bool:
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return False
    s = open(p, encoding="utf-8").read()
    s2, n = re.subn(pat, repl, s)
    if n != 1:
        sys.exit(f"не удалось заменить в {rel}")
    open(p, "w", encoding="utf-8").write(s2)
    print("обновлено:", rel)
    return True


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--firmware-only"]
    fw_only = "--firmware-only" in sys.argv[1:]
    if len(args) != 1 or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{1,60})[a-z0-9]", args[0]):
        sys.exit("usage: set_broker.py <host> [--firmware-only]   (например: broker.emqx.io, broker.hivemq.com, host.wokwi.internal)")
    host = args[0]
    sub("firmware/shared/gh_common.h", r'(#define GH_MQTT_HOST ")[^"]+(")', lambda m: m.group(1) + host + m.group(2))
    if not fw_only:
        for rel in (".env.example", ".env"):
            sub(rel, r"(?m)^(MQTT_HOST=).*()$", lambda m: m.group(1) + host + m.group(2))
    subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", "sync_firmware.py")])
    print("Готово. Пересоберите прошивки (pio run -d firmware/<узел>) и перезапустите backend (docker compose up -d).")


if __name__ == "__main__":
    main()
