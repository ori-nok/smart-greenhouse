import json
import unittest

from app.protocol import (ProtocolError, Topics, ValidationError, default_config, parse_cmd_ack, parse_event,
                          parse_status, parse_telemetry, validate_command, validate_config)

GOOD = {"v": 1, "dev": "gh-soil-01", "type": "soil", "boot": 5, "seq": 3, "ts": 1700000000000, "up": 10, "buf": 0,
        "drop": 0, "mode": "auto", "itv": 5000, "rssi": -60, "m": {"soil_moisture_pct": 41.5, "pump_on": 0}}


class T(unittest.TestCase):
    def test_topics(self):
        t = Topics("a/b")
        self.assertEqual(t.parse("a/b/gh-soil-01/telemetry"), ("gh-soil-01", "telemetry"))
        self.assertEqual(t.parse("a/b/gh-soil-01/cmd/ack"), ("gh-soil-01", "cmd/ack"))
        self.assertIsNone(t.parse("a/b/gh-soil-01/cmd"))        # исходящий топик не принимается
        self.assertIsNone(t.parse("x/gh-soil-01/telemetry"))
        self.assertIsNone(t.parse("a/b/BAD ID/telemetry"))
        self.assertEqual(len(t.subscriptions()), 5)

    def test_telemetry_ok(self):
        t = parse_telemetry(json.dumps(GOOD))
        self.assertEqual((t.dev, t.seq, t.metrics["soil_moisture_pct"], t.buffered), ("gh-soil-01", 3, 41.5, False))
        self.assertEqual(parse_telemetry(json.dumps(GOOD).encode()).boot, 5)

    def test_telemetry_bad(self):
        def bad(**ch):
            d = {**GOOD, **ch}
            d = {k: v for k, v in d.items() if v is not None}
            with self.assertRaises(ProtocolError):
                parse_telemetry(json.dumps(d))
        bad(v=2); bad(type="robot"); bad(seq=None); bad(seq=-1); bad(seq=True); bad(seq=1.5); bad(m={}); bad(m=[1])
        bad(m={"x": "str"}); bad(m={"Bad-Key": 1}); bad(dev="UPPER"); bad(buf=2)
        for raw in ("not json", "[1]", "{\"v\":1}", b"\xff\xfe", "x" * 5000):
            with self.assertRaises(ProtocolError):
                parse_telemetry(raw)
        with self.assertRaises(ProtocolError):
            parse_telemetry("{\"v\":1,\"dev\":\"gh-soil-01\",\"type\":\"soil\",\"boot\":1,\"seq\":1,\"ts\":1,\"m\":{\"a\":NaN}}")

    def test_other_messages(self):
        s = parse_status("{\"v\":1,\"dev\":\"gh-soil-01\",\"type\":\"soil\",\"fw\":\"1.0.0\",\"state\":\"online\",\"rssi\":-50}")
        self.assertEqual((s.state, s.fw), ("online", "1.0.0"))
        self.assertEqual(parse_status("{\"v\":1,\"dev\":\"gh-soil-01\",\"state\":\"offline\",\"reason\":\"lwt\"}").reason, "lwt")
        with self.assertRaises(ProtocolError):
            parse_status("{\"v\":1,\"dev\":\"gh-soil-01\",\"state\":\"zombie\"}")
        e = parse_event("{\"v\":1,\"dev\":\"gh-tank-01\",\"type\":\"tank_state\",\"sev\":\"warning\",\"msg\":\"low\",\"ts\":5}")
        self.assertEqual((e.type, e.severity), ("tank_state", "warning"))
        with self.assertRaises(ProtocolError):
            parse_event("{\"v\":1,\"dev\":\"gh-tank-01\",\"type\":\"x\",\"sev\":\"fatal\"}")
        a = parse_cmd_ack("{\"v\":1,\"dev\":\"gh-soil-01\",\"cmd_id\":\"c-1\",\"status\":\"rejected\",\"reason\":\"pump_locked\"}")
        self.assertEqual((a.cmd_id, a.reason), ("c-1", "pump_locked"))

    def test_config_validation(self):
        cur = default_config("soil")
        new = validate_config("soil", cur, {"moist_low": 20, "mode": "manual", "interval_ms": 2000})
        self.assertEqual((new["moist_low"], new["mode"], new["interval_ms"]), (20.0, "manual", 2000))
        for ch in ({}, {"nope": 1}, {"moist_low": 70}, {"moist_low": -1}, {"mode": "x"}, {"pump_enabled": 2},
                   {"interval_ms": "5"}, {"moist_low": float("nan")}):
            with self.assertRaises(ValidationError):
                validate_config("soil", cur, ch)
        with self.assertRaises(ValidationError):
            validate_config("tank", default_config("tank"), {"level_crit_pct": 30})
        with self.assertRaises(ValidationError):
            validate_config("climate", default_config("climate"), {"lux_low": 500})
        with self.assertRaises(ValidationError):
            validate_config("soil", cur, {"level_low_pct": 1})   # чужой параметр

    def test_command_validation(self):
        self.assertEqual(validate_command("soil", "pump_on", {"duration_s": 20}), {"duration_s": 20})
        validate_command("soil", "reboot", {})
        for typ, cmd, args in (("soil", "vent_open", {}), ("soil", "pump_on", {"duration_s": 0}),
                               ("soil", "pump_on", {"x": 1}), ("soil", "pump_on", {"duration_s": True}),
                               ("tank", "pump_on", {})):
            with self.assertRaises(ValidationError):
                validate_command(typ, cmd, args)


if __name__ == "__main__":
    unittest.main()
