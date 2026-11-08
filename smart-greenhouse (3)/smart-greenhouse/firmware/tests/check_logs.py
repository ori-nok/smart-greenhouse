#!/usr/bin/env python3
"""Проверка вывода хост-прогонов прошивок: формат протокола, seq, буферизация, ack-и, состояния."""
import json, sys, os

d = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gh_fw_tests"
ROOT = "gh-k7q2x9/greenhouse/"


def load(name):
    pubs, steps, states, conns = [], [], [], []
    with open(os.path.join(d, name + ".log"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for line in lines:
        f = line.split("\t")
        if f[0] == "PUB":
            assert f[1].startswith(ROOT), f[1]
            dev, kind = f[1][len(ROOT):].split("/", 1)
            payload = json.loads(f[3])
            assert len(f[3]) < 480, "слишком длинное сообщение"
            pubs.append((dev, kind, int(f[2]), payload))
        elif f[0] == "STEP": steps.append(f[1])
        elif f[0] == "STATE": states.append((f[1], int(f[2])))
        elif f[0] == "CONN": conns.append(f)
        elif f[0] == "OVERSIZE": raise AssertionError("OVERSIZE publish: " + line)
    return pubs, steps, states, conns


def check_common(pubs, conns, dev):
    assert conns and conns[0][2] == f"{ROOT}{dev}/status", "LWT-топик должен быть <root>/<dev>/status"
    assert conns[0][3] == "1" and conns[0][4] == "1", "LWT должен быть qos1 retained"
    assert json.loads(conns[0][5])["state"] == "offline"
    tel = [p[3] for p in pubs if p[1] == "telemetry"]
    assert tel, "нет телеметрии"
    seqs = [t["seq"] for t in tel]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq с пропусками/не по порядку: {seqs[:50]}"
    assert len({t["boot"] for t in tel}) == 1
    for t in tel:
        for k in ("v", "dev", "type", "boot", "seq", "ts", "up", "buf", "drop", "mode", "itv", "rssi", "m"):
            assert k in t, f"нет поля {k}"
        assert t["dev"] == dev and t["v"] == 1 and isinstance(t["m"], dict) and t["ts"] > 1.7e12
    st = [p for p in pubs if p[1] == "status"]
    assert st and st[0][3]["state"] == "online" and all(p[2] == 1 for p in st), "status должен быть retained"
    return tel


def acks(pubs):
    return [(p[3]["cmd_id"], p[3]["status"], p[3]["reason"]) for p in pubs if p[1] == "cmd/ack"]


pubs, steps, states, conns = load("gh-soil-01")
tel = check_common(pubs, conns, "gh-soil-01")
a = acks(pubs)
assert ("c1", "ok", "") in a and ("c2", "ok", "duplicate") in a and ("c3", "rejected", "unknown_cmd") in a, a
assert ("c4", "rejected", "pump_locked") in a and ("c9", "ok", "rebooting") in a
assert [p[3]["cfg_ver"] for p in pubs if p[1] == "config/ack"] == [5, 6]
exp = [("pump", 1), ("pump", 1), ("led", 1), ("pump", 0), ("pump", 1), ("pump", 0), ("pump", 0), ("pump", 1)]
assert states[:len(exp)] == exp, states
buf = [t for t in tel if t["buf"] == 1]
assert len(buf) >= 10, "буферизация не сработала"
assert "link_restored" in [p[3]["type"] for p in pubs if p[1] == "event"]
assert any(t["m"]["pump_locked"] == 1 for t in tel), "флаг pump_locked не передан"
assert any(t["itv"] == 3000 for t in tel), "удалённый interval_ms не применён"
print(f"soil OK: {len(tel)} сообщений, буферизовано {len(buf)}, ack={len(a)}")

pubs, steps, states, conns = load("gh-tank-01")
tel = check_common(pubs, conns, "gh-tank-01")
ev = [(p[3]["type"], p[3]["sev"]) for p in pubs if p[1] == "event"]
for e in [("tank_state", "warning"), ("tank_state", "critical"), ("sensor_fault", "critical"), ("sensor_recovered", "info"), ("tank_state", "info")]:
    assert e in ev, (e, ev)
levels = [round(t["m"]["level_pct"]) for t in tel if t["m"]["sensor_ok"] == 1 and t["m"]["level_pct"] >= 0]
assert 60 in levels and 15 in levels and 5 in levels, sorted(set(levels))
assert any(t["m"]["sensor_ok"] == 0 and t["m"]["level_pct"] == -1 for t in tel)
assert states == [("alarm_led", 1), ("alarm_led", 0)], states
assert ("m1", "ok", "") in acks(pubs) and any(t["m"]["muted"] == 1 for t in tel)
print(f"tank OK: {len(tel)} сообщений")

pubs, steps, states, conns = load("gh-climate-01")
tel = check_common(pubs, conns, "gh-climate-01")
assert states == [("servo", 0), ("servo", 90), ("servo", 90), ("servo", 0), ("grow", 1), ("servo", 90), ("servo", 0)], states
assert ("v1", "ok", "") in acks(pubs)
assert all(-55 <= t["m"]["temperature_c"] <= 150 for t in tel)
print(f"climate OK: {len(tel)} сообщений")
print("ALL FIRMWARE HOST CHECKS PASSED")
