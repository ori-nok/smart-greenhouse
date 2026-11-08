"""Тесты HTTP API. Требуют fastapi+httpx (есть в Docker/CI); без них пропускаются."""
import unittest

try:
    from fastapi.testclient import TestClient
    from app.main import create_app
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

from app.config import Settings
from tests.helpers import ROOT, make_service, tele


@unittest.skipUnless(HAVE, "fastapi/httpx not installed")
class T(unittest.TestCase):
    def setUp(self):
        self.svc, self.pub, self.clock, self.st = make_service()
        settings = Settings(topic_root=ROOT, api_key="k", rate_limit_per_min=600)
        self.c = TestClient(create_app(self.svc, settings))
        self.h = {"X-API-Key": "k"}
        tele(self.svc, self.clock, "gh-soil-01", "soil", {"soil_moisture_pct": 40.0, "pump_on": 0, "pump_locked": 0})

    def test_public(self):
        self.assertEqual(self.c.get("/health").json()["status"], "ok")
        self.assertIn("greenhouse_telemetry_total", self.c.get("/metrics").text)
        self.assertEqual(self.c.get("/ui").status_code, 200)

    def test_auth(self):
        self.assertEqual(self.c.get("/api/v1/devices").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/devices", headers={"X-API-Key": "bad"}).status_code, 401)
        self.assertEqual(self.c.get("/api/v1/devices", headers=self.h).status_code, 200)

    def test_read_endpoints(self):
        d = self.c.get("/api/v1/devices", headers=self.h).json()
        self.assertEqual(d[0]["device_id"], "gh-soil-01")
        self.assertEqual(self.c.get("/api/v1/devices/gh-soil-01", headers=self.h).json()["latest"]["soil_moisture_pct"], 40.0)
        self.assertEqual(self.c.get("/api/v1/devices/nope", headers=self.h).status_code, 404)
        self.assertEqual(len(self.c.get("/api/v1/devices/gh-soil-01/telemetry", headers=self.h).json()), 1)
        self.assertEqual(self.c.get("/api/v1/devices/gh-soil-01/telemetry?limit=0", headers=self.h).status_code, 422)
        self.assertEqual(self.c.get("/api/v1/devices/gh-soil-01/aggregate?metric=soil_moisture_pct&bucket_s=60", headers=self.h).json()[0]["n"], 1)
        self.assertIn("gh-soil-01", self.c.get("/api/v1/telemetry/latest", headers=self.h).json())
        self.assertEqual(self.c.get("/api/v1/events?severity=bogus", headers=self.h).status_code, 422)
        self.assertEqual(self.c.get("/api/v1/summary", headers=self.h).json()["devices_online"], 1)
        csv = self.c.get("/api/v1/export/telemetry.csv", headers=self.h).text.splitlines()
        self.assertTrue(csv[0].startswith("ts,device_id,boot,seq,buffered") and len(csv) == 2)

    def test_commands_and_config(self):
        r = self.c.post("/api/v1/devices/gh-soil-01/commands", json={"cmd": "pump_on", "duration_s": 10}, headers=self.h)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.pub.of("/gh-soil-01/cmd")[0][1]["cmd"], "pump_on")
        self.assertEqual(self.c.post("/api/v1/devices/gh-soil-01/commands", json={"cmd": "vent_open"}, headers=self.h).status_code, 422)
        self.assertEqual(self.c.post("/api/v1/devices/zzz/commands", json={"cmd": "ping"}, headers=self.h).status_code, 404)
        self.assertEqual(len(self.c.get("/api/v1/commands", headers=self.h).json()), 1)
        r = self.c.put("/api/v1/devices/gh-soil-01/config", json={"moist_low": 22}, headers=self.h)
        self.assertEqual((r.status_code, r.json()["cfg_ver"]), (200, 2))
        self.assertEqual(self.c.put("/api/v1/devices/gh-soil-01/config", json={"moist_low": 95}, headers=self.h).status_code, 422)
        self.clock.adv(60); self.svc.tick()
        self.assertEqual(self.c.post("/api/v1/devices/gh-soil-01/commands", json={"cmd": "ping"}, headers=self.h).status_code, 409)

    def test_rate_limit(self):
        s = Settings(topic_root=ROOT, api_key="k", rate_limit_per_min=60)
        c = TestClient(create_app(self.svc, s))
        codes = [c.get("/api/v1/devices", headers=self.h).status_code for _ in range(40)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 200)


if __name__ == "__main__":
    unittest.main()
