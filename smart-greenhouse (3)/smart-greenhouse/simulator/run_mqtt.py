#!/usr/bin/env python3
"""Три виртуальных узла теплицы, подключённых к настоящему MQTT-брокеру (вместо Wokwi).
Тот же протокол, что и у прошивок: telemetry/status(LWT+heartbeat)/event/cmd/cmd-ack/config/config-ack.

    python -m simulator.run_mqtt --host localhost            # из корня проекта
    docker compose --profile sim up -d                       # в составе compose
Опция --fast ускоряет физику (почва сохнет и нагревается быстрее), чтобы на демонстрации были видны сценарии."""
import argparse
import json
import os
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from simulator.model import ClimateNode, SoilNode, TankNode, World


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--root", default=os.environ.get("TOPIC_ROOT", "gh-k7q2x9/greenhouse"))
    ap.add_argument("--fast", action="store_true", default=os.environ.get("SIM_FAST", "1") == "1")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    world = World(int(time.time()), now, fast=a.fast)
    boot0 = int(time.time()) % 100000
    devs = {"gh-soil-01": SoilNode(world, "gh-soil-01", boot0 + 1),
            "gh-tank-01": TankNode(world, "gh-tank-01", boot0 + 2),
            "gh-climate-01": ClimateNode(world, "gh-climate-01", boot0 + 3)}

    def ms() -> int:
        return int(time.time() * 1000)

    clients = {}  # у MQTT один LWT на соединение, поэтому на каждый узел - свой клиент

    def make_client(name: str) -> mqtt.Client:
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{name}-sim")
        cl.will_set(f"{a.root}/{name}/status", json.dumps({"v": 1, "dev": name, "state": "offline", "reason": "lwt"}), qos=1, retain=True)

        def on_connect(client, userdata, flags, rc, props=None):
            client.subscribe([(f"{a.root}/{name}/cmd", 1), (f"{a.root}/{name}/config", 1)])
            d = devs[name]
            d.last_hb = -10**9   # сразу опубликовать status (online)

        def on_message(client, userdata, m):
            d = devs[name]
            try:
                p = json.loads(m.payload)
            except ValueError:
                return
            if m.topic.endswith("/cmd"):
                client.publish(f"{a.root}/{name}/cmd/ack", json.dumps(d.handle_cmd(p, ms())), qos=1)
            elif m.topic.endswith("/config"):
                ack = d.handle_cfg(p)
                if ack:
                    client.publish(f"{a.root}/{name}/config/ack", json.dumps(ack), qos=1)

        cl.on_connect, cl.on_message = on_connect, on_message
        cl.connect_async(a.host, a.port, keepalive=15)
        cl.loop_start()
        return cl

    for name in devs:
        clients[name] = make_client(name)
    print(f"simulator: 3 nodes -> mqtt://{a.host}:{a.port}/{a.root} (fast={a.fast})", flush=True)
    try:
        while True:
            t0 = time.time()
            world.step(1.0)
            for name, d in devs.items():
                cl = clients[name]
                up = cl.is_connected()
                msgs = d.set_link(up, ms())      # при восстановлении вернёт status, link_restored и буфер
                for kind, payload, retain in msgs + d.tick(ms()):
                    cl.publish(f"{a.root}/{name}/{kind}", json.dumps(payload), qos=1, retain=retain)
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
    except KeyboardInterrupt:
        for cl in clients.values():
            cl.disconnect()


if __name__ == "__main__":
    main()
