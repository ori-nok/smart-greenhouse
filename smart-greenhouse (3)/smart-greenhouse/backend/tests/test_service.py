import json
import unittest

from app.protocol import ValidationError
from app.service import Conflict, NotFound
from tests.helpers import ROOT, make_service, tele

SOIL = ("gh-soil-01", "soil")
TANK = ("gh-tank-01", "tank")
CLIM = ("gh-climate-01", "climate")


def soil_m(m=40.0, pump=0, locked=0):
    return {"soil_moisture_pct": m, "pump_on": pump, "pump_locked": locked}


def tank_m(level=80.0):
    return {"level_pct": level, "distance_cm": 100 - level, "sensor_ok": 1, "tank_state": 0}


def clim_m(t, vent=0, grow=0):
    return {"temperature_c": t, "vent_open": vent, "grow_light_on": grow}


def events(st, etype=None):
    return [e for e in st.events if etype is None or e["type"] == etype]


class T(unittest.TestCase):
    def setUp(self):
        self.svc, self.pub, self.clock, self.st = make_service()

    def test_registration_and_storage(self):
        tele(self.svc, self.clock, *SOIL, soil_m())
        d = self.svc.devices["gh-soil-01"]
        self.assertEqual((d["state"], d["cfg_ver"]), ("online", 1))
        cfgs = self.pub.of("/gh-soil-01/config")
        self.assertTrue(cfgs and cfgs[0][2])                                   # retained
        self.assertEqual(cfgs[0][1]["cfg_ver"], 1)
        self.assertEqual(len(self.st.telemetry), 1)
        self.assertEqual(len(events(self.st, "device_registered")), 1)

    def test_invalid_and_mismatched_messages(self):
        self.svc.handle_message(f"{ROOT}/gh-soil-01/telemetry", "garbage")
        self.assertEqual(self.svc.metrics.get("invalid_messages_total", kind="telemetry"), 1)
        self.assertEqual(len(events(self.st, "invalid_message")), 1)
        self.svc.handle_message(f"{ROOT}/gh-soil-01/telemetry", "garbage")      # cooldown: без спама событий
        self.assertEqual(len(events(self.st, "invalid_message")), 1)
        msg = {"v": 1, "dev": "gh-tank-01", "type": "tank", "boot": 1, "seq": 1, "ts": 0, "m": {"a": 1}}
        self.svc.handle_message(f"{ROOT}/gh-soil-01/telemetry", json.dumps(msg))  # чужой id в payload
        self.assertNotIn("gh-tank-01", self.svc.devices)
        self.svc.handle_message("other/topic", "{}")
        self.assertEqual(self.svc.metrics.get("messages_ignored_total"), 1)

    def test_gap_duplicate_restart_and_buffered(self):
        tele(self.svc, self.clock, *SOIL, soil_m(), seq=1)
        tele(self.svc, self.clock, *SOIL, soil_m(), seq=5)
        self.assertEqual(len(events(self.st, "telemetry_gap")), 1)
        self.assertEqual(self.svc.metrics.get("telemetry_lost_total", device="gh-soil-01"), 3)
        tele(self.svc, self.clock, *SOIL, soil_m(), seq=5)                      # дубликат
        self.assertEqual(len(self.st.telemetry), 2)
        tele(self.svc, self.clock, *SOIL, soil_m(), boot=2, seq=1)
        self.assertEqual(len(events(self.st, "device_restart")), 1)
        tele(self.svc, self.clock, *SOIL, soil_m(), boot=2, seq=2, buf=1)
        self.assertTrue(self.st.telemetry[-1]["buffered"])

    def test_device_timestamp_used_only_if_plausible(self):
        from datetime import timedelta
        tele(self.svc, self.clock, *SOIL, soil_m())
        self.assertEqual(self.st.telemetry[0]["ts"], self.clock())
        msg = {"v": 1, "dev": "gh-soil-01", "type": "soil", "boot": 1, "seq": 99, "ts": 1000, "m": {"a": 1}}
        self.svc.handle_message(f"{ROOT}/gh-soil-01/telemetry", json.dumps(msg))   # 1970 -> время приёма
        self.assertEqual(self.st.telemetry[-1]["ts"], self.clock())
        old = self.clock() - timedelta(minutes=3)
        msg.update(seq=100, ts=int(old.timestamp() * 1000))                          # буферизованный: своё время
        self.svc.handle_message(f"{ROOT}/gh-soil-01/telemetry", json.dumps(msg))
        self.assertEqual(self.st.telemetry[-1]["ts"], old)

    def test_watchdog_offline_and_recovery(self):
        tele(self.svc, self.clock, *SOIL, soil_m())
        self.clock.adv(20); self.svc.tick()
        self.assertEqual(self.svc.devices["gh-soil-01"]["state"], "online")
        self.clock.adv(20); self.svc.tick()
        self.assertEqual(self.svc.devices["gh-soil-01"]["state"], "offline")
        self.assertEqual(len(events(self.st, "device_offline")), 1)
        self.svc.tick(); self.assertEqual(len(events(self.st, "device_offline")), 1)   # без повторов
        with self.assertRaises(Conflict):
            self.svc.send_command("gh-soil-01", "pump_on", {})
        tele(self.svc, self.clock, *SOIL, soil_m())
        self.assertEqual(self.svc.devices["gh-soil-01"]["state"], "online")
        self.assertEqual(len(events(self.st, "device_online")), 1)

    def test_lwt_and_retained_status(self):
        tele(self.svc, self.clock, *SOIL, soil_m())
        self.svc.handle_message(f"{ROOT}/gh-soil-01/status", "{\"v\":1,\"dev\":\"gh-soil-01\",\"state\":\"offline\",\"reason\":\"lwt\"}", True)
        self.assertEqual(self.svc.devices["gh-soil-01"]["state"], "offline")
        online = "{\"v\":1,\"dev\":\"gh-soil-01\",\"type\":\"soil\",\"state\":\"online\",\"fw\":\"1.0.0\"}"
        self.svc.handle_message(f"{ROOT}/gh-soil-01/status", online, True)          # retained не воскрешает
        self.assertEqual(self.svc.devices["gh-soil-01"]["state"], "offline")
        self.svc.handle_message(f"{ROOT}/gh-soil-01/status", online, False)         # живой heartbeat - воскрешает
        self.assertEqual(self.svc.devices["gh-soil-01"]["state"], "online")
        self.assertEqual(self.svc.devices["gh-soil-01"]["fw"], "1.0.0")

    def test_command_flow_ack_reject_timeout(self):
        tele(self.svc, self.clock, *SOIL, soil_m())
        r = self.svc.send_command("gh-soil-01", "pump_on", {"duration_s": 10})
        sent = self.pub.of("/gh-soil-01/cmd")[0][1]
        self.assertEqual((sent["cmd"], sent["duration_s"], sent["cmd_id"]), ("pump_on", 10, r["cmd_id"]))
        self.assertLessEqual(len(sent["cmd_id"]), 31)
        ack = lambda cid, st, rs="": self.svc.handle_message(
            f"{ROOT}/gh-soil-01/cmd/ack", json.dumps({"v": 1, "dev": "gh-soil-01", "cmd_id": cid, "status": st, "reason": rs}))
        ack(r["cmd_id"], "ok")
        self.assertEqual(self.st.get_command(r["cmd_id"])["status"], "acked")
        r2 = self.svc.send_command("gh-soil-01", "pump_on", {})
        ack(r2["cmd_id"], "rejected", "pump_locked")
        self.assertEqual(self.st.get_command(r2["cmd_id"])["status"], "rejected")
        self.assertEqual(len(events(self.st, "command_rejected")), 1)
        r3 = self.svc.send_command("gh-soil-01", "pump_off", {})
        self.clock.adv(16); tele(self.svc, self.clock, *SOIL, soil_m()); self.svc.tick()
        self.assertEqual(self.st.get_command(r3["cmd_id"])["status"], "timeout")
        self.assertEqual(len(events(self.st, "command_timeout")), 1)
        with self.assertRaises(NotFound): self.svc.send_command("nope", "ping", {})
        with self.assertRaises(ValidationError): self.svc.send_command("gh-soil-01", "vent_open", {})

    def test_config_change_published_retained_and_acked(self):
        tele(self.svc, self.clock, *SOIL, soil_m())
        r = self.svc.set_config("gh-soil-01", {"moist_low": 25, "interval_ms": 3000})
        self.assertEqual(r["cfg_ver"], 2)
        t, p, retain = self.pub.of("/gh-soil-01/config")[-1]
        self.assertTrue(retain)
        self.assertEqual((p["cfg_ver"], p["moist_low"], p["interval_ms"], p["pump_enabled"]), (2, 25.0, 3000, 1))
        with self.assertRaises(ValidationError): self.svc.set_config("gh-soil-01", {"moist_low": 99})
        self.assertEqual(self.svc.devices["gh-soil-01"]["cfg_ver"], 2)          # неудачная правка не меняет версию
        self.svc.handle_message(f"{ROOT}/gh-soil-01/config/ack", "{\"v\":1,\"dev\":\"gh-soil-01\",\"cfg_ver\":2,\"status\":\"ok\"}")
        self.assertEqual(self.svc.devices["gh-soil-01"]["cfg_acked"], 2)
        self.pub.sent.clear(); self.svc.on_mqtt_connected()                     # после реконнекта конфиг republish
        self.assertEqual(self.pub.of("/gh-soil-01/config")[0][1]["cfg_ver"], 2)

    def test_config_resync_on_stale_device_version(self):
        tele(self.svc, self.clock, *SOIL, soil_m())
        self.svc.set_config("gh-soil-01", {"moist_low": 25})                           # cfg_ver = 2
        st = lambda v: "{\"v\":1,\"dev\":\"gh-soil-01\",\"type\":\"soil\",\"state\":\"online\",\"cfg_ver\":%d}" % v
        self.pub.sent.clear()
        self.svc.handle_message(f"{ROOT}/gh-soil-01/status", st(0))                    # устройство перезагрузилось
        c = self.pub.of("/gh-soil-01/config")
        self.assertEqual((len(c), c[0][1]["cfg_ver"], c[0][1]["moist_low"], c[0][2]), (1, 2, 25.0, True))
        self.svc.handle_message(f"{ROOT}/gh-soil-01/status", st(0))                    # троттлинг 30 с
        self.assertEqual(len(self.pub.of("/gh-soil-01/config")), 1)
        self.clock.adv(31)
        self.svc.handle_message(f"{ROOT}/gh-soil-01/status", st(0))
        self.assertEqual(len(self.pub.of("/gh-soil-01/config")), 2)
        self.svc.handle_message(f"{ROOT}/gh-soil-01/status", st(2))                    # версия совпала - тишина
        self.assertEqual(len(self.pub.of("/gh-soil-01/config")), 2)

    def test_tank_lock_and_unlock_cross_device(self):
        tele(self.svc, self.clock, *SOIL, soil_m()); tele(self.svc, self.clock, *TANK, tank_m(60))
        self.assertFalse(self.svc.devices["gh-soil-01"]["meta"].get("rule_locked"))
        self.clock.adv(5); tele(self.svc, self.clock, *TANK, tank_m(8))
        self.assertTrue(self.svc.devices["gh-soil-01"]["meta"]["rule_locked"])
        self.assertEqual(self.pub.of("/gh-soil-01/config")[-1][1]["pump_enabled"], 0)
        self.assertEqual(len(events(self.st, "pump_locked")), 1)
        self.clock.adv(5); tele(self.svc, self.clock, *TANK, tank_m(15))        # внутри гистерезиса - остаёмся заблокированы
        self.assertTrue(self.svc.devices["gh-soil-01"]["meta"]["rule_locked"])
        self.clock.adv(5); tele(self.svc, self.clock, *TANK, tank_m(25))
        self.assertFalse(self.svc.devices["gh-soil-01"]["meta"]["rule_locked"])
        self.assertEqual(self.pub.of("/gh-soil-01/config")[-1][1]["pump_enabled"], 1)
        self.assertEqual(len(events(self.st, "pump_unlocked")), 1)

    def test_tank_lock_late_soil_ignores_buffered_and_faults(self):
        tele(self.svc, self.clock, *TANK, tank_m(5))
        tele(self.svc, self.clock, *SOIL, soil_m())                              # насос появился позже - блокируется
        self.assertTrue(self.svc.devices["gh-soil-01"]["meta"]["rule_locked"])
        svc, pub, clock, st = make_service()
        tele(svc, clock, *SOIL, soil_m())
        tele(svc, clock, *TANK, tank_m(5), buf=1)                                # устаревшие данные не блокируют
        self.assertFalse(svc.devices["gh-soil-01"]["meta"].get("rule_locked"))
        bad = {"level_pct": -1, "distance_cm": -1, "sensor_ok": 0}               # неисправный датчик не блокирует
        tele(svc, clock, *TANK, bad)
        self.assertFalse(svc.devices["gh-soil-01"]["meta"].get("rule_locked"))

    def test_heat_alarm_action_and_hysteresis(self):
        tele(self.svc, self.clock, *CLIM, clim_m(25))
        self.clock.adv(5); tele(self.svc, self.clock, *CLIM, clim_m(36))
        self.assertEqual(len(events(self.st, "heat_alarm")), 1)
        c = self.pub.of("/gh-climate-01/cmd")
        self.assertEqual((c[0][1]["cmd"], c[0][1]["duration_s"]), ("vent_open", 300))
        self.assertEqual(self.st.list_commands()[0]["source"], "rule:heat")
        self.clock.adv(5); tele(self.svc, self.clock, *CLIM, clim_m(37, vent=1))
        self.assertEqual(len(events(self.st, "heat_alarm")), 1)                  # без повторной тревоги
        self.clock.adv(5); tele(self.svc, self.clock, *CLIM, clim_m(34, vent=1))
        self.assertEqual(len(events(self.st, "heat_cleared")), 0)                # 34 > 33 - ещё не сброс
        self.clock.adv(5); tele(self.svc, self.clock, *CLIM, clim_m(32, vent=1))
        self.assertEqual(len(events(self.st, "heat_cleared")), 1)

    def test_frost_alarm(self):
        tele(self.svc, self.clock, *CLIM, clim_m(2, vent=1))
        self.assertEqual(sorted(c["cmd"] for c in self.st.list_commands()), ["light_on", "vent_close"])
        self.assertEqual(len(events(self.st, "frost_alarm")), 1)

    def test_irrigation_ineffective(self):
        for m, p in ((20, 0), (20, 1), (20.5, 1), (21, 0)):
            self.clock.adv(5); tele(self.svc, self.clock, *SOIL, soil_m(m, p))
        self.assertEqual(len(events(self.st, "irrigation_ineffective")), 1)
        svc, pub, clock, st = make_service()
        for m, p in ((20, 0), (20, 1), (40, 1), (60, 0)):
            clock.adv(5); tele(svc, clock, *SOIL, soil_m(m, p))
        self.assertEqual(len(events(st, "irrigation_ineffective")), 0)

    def test_pump_fault_escalation_and_operator_reset(self):
        for _ in range(3):                                                           # три «пустых» полива подряд
            for m, p in ((20, 0), (20, 1), (20.3, 1), (20.4, 0)):
                self.clock.adv(60); tele(self.svc, self.clock, *SOIL, soil_m(m, p))
        self.assertEqual(len(events(self.st, "pump_fault")), 1)
        d = self.svc.devices["gh-soil-01"]
        self.assertTrue(d["meta"]["fault_locked"])
        self.assertEqual(self.pub.of("/gh-soil-01/config")[-1][1]["pump_enabled"], 0)
        tele(self.svc, self.clock, *TANK, tank_m(90))                                 # бак полон - всё равно заблокирован
        self.assertEqual(self.pub.of("/gh-soil-01/config")[-1][1]["pump_enabled"], 0)
        self.svc.set_config("gh-soil-01", {"pump_enabled": 1}, source="operator")     # оператор вернул насос
        self.assertFalse(d["meta"]["fault_locked"])
        self.assertEqual(self.pub.of("/gh-soil-01/config")[-1][1]["pump_enabled"], 1)

    def test_anomaly_event_with_cooldown(self):
        import random
        random.seed(2)
        for _ in range(40):
            self.clock.adv(5); tele(self.svc, self.clock, *CLIM, clim_m(24 + random.gauss(0, 0.2)))
        self.clock.adv(5); tele(self.svc, self.clock, *CLIM, clim_m(31))
        self.assertEqual(len(events(self.st, "anomaly")), 1)
        self.clock.adv(5); tele(self.svc, self.clock, *CLIM, clim_m(31.5))
        self.assertEqual(len(events(self.st, "anomaly")), 1)                     # cooldown

    def test_telemetry_stalled_triggers_auto_reboot_with_limit(self):
        tele(self.svc, self.clock, *SOIL, soil_m())
        hb = "{\"v\":1,\"dev\":\"gh-soil-01\",\"type\":\"soil\",\"state\":\"online\",\"fw\":\"1\"}"
        for _ in range(4):                                                        # heartbeat жив, телеметрии нет
            self.clock.adv(10); self.svc.handle_message(f"{ROOT}/gh-soil-01/status", hb)
            self.svc.tick()
        self.assertEqual(len(events(self.st, "telemetry_stalled")), 1)
        reb = [c for c in self.st.list_commands() if c["cmd"] == "reboot"]
        self.assertEqual((len(reb), reb[0]["source"]), (1, "auto:watchdog"))
        for _ in range(8):                                                        # лимит перезагрузок в час
            tele(self.svc, self.clock, *SOIL, soil_m())
            for _ in range(5):
                self.clock.adv(10); self.svc.handle_message(f"{ROOT}/gh-soil-01/status", hb); self.svc.tick()
        self.assertLessEqual(len([c for c in self.st.list_commands(1000) if c["cmd"] == "reboot"]), 3)

    def test_summary_metrics_and_queries(self):
        tele(self.svc, self.clock, *SOIL, soil_m()); tele(self.svc, self.clock, *TANK, tank_m(50))
        s = self.svc.summary()
        self.assertEqual((s["devices_total"], s["devices_online"], s["tank_level_pct"]), (2, 2, 50.0))
        txt = self.svc.render_metrics()
        self.assertIn("greenhouse_device_online{device=\"gh-soil-01\",type=\"soil\"} 1", txt)
        self.assertIn("greenhouse_sensor_value{device=\"gh-tank-01\",metric=\"level_pct\"} 50", txt)
        self.assertEqual(len(self.svc.list_devices()), 2)
        self.assertEqual(self.svc.get_device("gh-tank-01")["latest"]["level_pct"], 50)
        self.assertEqual(len(self.svc.query_telemetry("gh-tank-01")), 1)
        self.assertEqual(self.svc.aggregate("gh-tank-01", "level_pct", 60)[0]["n"], 1)
        with self.assertRaises(ValidationError): self.svc.aggregate("gh-tank-01", "level_pct", 0)
        with self.assertRaises(ValidationError): self.svc.events(severity="bogus")
        with self.assertRaises(NotFound): self.svc.get_device("zzz")

    def test_state_restored_from_storage(self):
        tele(self.svc, self.clock, *SOIL, soil_m(), seq=7)
        from app.config import Settings
        from app.service import GreenhouseService
        svc2 = GreenhouseService(self.st, self.pub, Settings(topic_root=ROOT), clock=self.clock)
        self.assertIn("gh-soil-01", svc2.devices)
        tele(svc2, self.clock, *SOIL, soil_m(), seq=8)                          # продолжение без ложного «new/gap»
        self.assertEqual(len(events(self.st, "telemetry_gap")), 0)


if __name__ == "__main__":
    unittest.main()
