"""Контрактный тест: реальные сообщения прошивок (вывод хост-прогона, tests/fixtures) принимает backend."""
import os
import unittest

from tests.helpers import make_service

FX = os.path.join(os.path.dirname(__file__), "fixtures")


def replay(svc, name):
    n = 0
    with open(os.path.join(FX, name), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for line in lines:
        f = line.split("\t")
        if f[0] == "PUB" and not f[1].endswith(("/cmd/ack", "/config/ack")):
            svc.handle_message(f[1], f[3], retained=(f[2] == "1"))
            n += 1
    return n


class T(unittest.TestCase):
    def test_all_firmware_messages_accepted(self):
        svc, pub, clock, st = make_service()
        total = sum(replay(svc, f"gh-{n}-01.log") for n in ("soil", "tank", "climate"))
        self.assertGreater(total, 60)
        for kind in ("telemetry", "event", "status"):
            self.assertEqual(svc.metrics.get("invalid_messages_total", kind=kind), 0, kind)
        self.assertEqual(sorted(svc.devices), ["gh-climate-01", "gh-soil-01", "gh-tank-01"])
        self.assertGreater(len(st.telemetry), 50)
        types = {e["type"] for e in st.events}
        self.assertTrue({"tank_state", "sensor_fault", "link_restored"} <= types, types)
        self.assertEqual(len([e for e in st.events if e["type"] == "telemetry_gap"]), 0)
        self.assertTrue(any(r["buffered"] for r in st.telemetry))


if __name__ == "__main__":
    unittest.main()
