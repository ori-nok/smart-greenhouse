"""Замкнутый тест: модель устройств <-> настоящий GreenhouseService, 72 ч симулированного времени."""
import os
import sys
import unittest
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from simulator.make_dataset import ALL, Harness, add_scenarios  # noqa: E402


class ClosedLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness(seed=7)
        add_scenarios(cls.h, ALL)
        cls.h.run(72)
        cls.ev = Counter(e["type"] for e in cls.h.st.events)

    def test_all_scenarios_detected(self):
        for t in ("heat_alarm", "heat_cleared", "leak_suspected", "sensor_stuck", "sensor_fault", "sensor_recovered",
                  "device_offline", "device_online", "link_restored", "pump_failsafe", "pump_fault", "pump_locked",
                  "pump_unlocked", "irrigation_ineffective", "tank_state", "config_changed"):
            self.assertGreaterEqual(self.ev[t], 1, t)

    def test_no_seq_loss_after_buffering(self):
        for dev in self.h.devs:
            rows = [r for r in self.h.st.telemetry if r["device_id"] == dev]
            by_boot = {}
            for r in rows:
                by_boot.setdefault(r["boot"], []).append(r["seq"])
            for seqs in by_boot.values():
                s = sorted(seqs)
                self.assertEqual(s, list(range(s[0], s[0] + len(s))), dev)

    def test_commands_acked(self):
        cmds = list(self.h.st.commands.values())
        self.assertGreaterEqual(len(cmds), 3)
        self.assertTrue(all(c["status"] == "acked" for c in cmds), [c["status"] for c in cmds])

    def test_deterministic(self):
        h2 = Harness(seed=7)
        add_scenarios(h2, ALL)
        h2.run(72)
        self.assertEqual(len(h2.st.telemetry), len(self.h.st.telemetry))
        self.assertEqual(Counter(e["type"] for e in h2.st.events), self.ev)


if __name__ == "__main__":
    unittest.main()
