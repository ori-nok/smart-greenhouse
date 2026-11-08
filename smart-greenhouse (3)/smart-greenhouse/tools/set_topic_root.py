#!/usr/bin/env python3
"""Меняет корень MQTT-топиков (уникальный префикс на публичном брокере broker.hivemq.com, чтобы не пересекаться
с чужими устройствами) в прошивках, .env.example и docker-compose, затем синхронизирует копии в папках узлов.

    python tools/set_topic_root.py my-team-42/greenhouse
"""
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{3,40}/[a-z0-9_-]+", sys.argv[1]):
        sys.exit("usage: set_topic_root.py <prefix>/<name>   (строчные латинские буквы, цифры, - и _; пример: my-team-42/greenhouse)")
    new = sys.argv[1]
    targets = [
        ("firmware/shared/gh_common.h", r'(#define GH_TOPIC_ROOT ")[^"]+(")'),
        (".env.example", r"(?m)^(TOPIC_ROOT=).*()$"),
    ]
    for rel, pat in targets:
        p = os.path.join(ROOT, rel)
        s = open(p, encoding="utf-8").read()
        s2, n = re.subn(pat, lambda m: m.group(1) + new + m.group(2), s)
        if n != 1:
            sys.exit(f"не удалось заменить в {rel}")
        open(p, "w", encoding="utf-8").write(s2)
        print("обновлено:", rel)
    env = os.path.join(ROOT, ".env")
    if os.path.exists(env):
        s = open(env, encoding="utf-8").read()
        s = re.sub(r"(?m)^TOPIC_ROOT=.*$", "TOPIC_ROOT=" + new, s)
        open(env, "w", encoding="utf-8").write(s)
        print("обновлено: .env")
    subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", "sync_firmware.py")])
    print("Готово. Перезапустите backend (docker compose up -d) и заново вставьте код в Wokwi.")


if __name__ == "__main__":
    main()
