import json
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.service import GreenhouseService, Publisher
from app.storage import InMemoryStorage

ROOT = "gh-k7q2x9/greenhouse"


class FakePub(Publisher):
    def __init__(self):
        self.sent = []
        self.connected = True

    def publish(self, topic, payload, retain=False, qos=1):
        self.sent.append((topic, json.loads(payload), retain))

    def is_connected(self):
        return self.connected

    def of(self, suffix):
        return [(t, p, r) for t, p, r in self.sent if t.endswith(suffix)]


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def adv(self, s):
        self.t += timedelta(seconds=s)


def make_service(**kw):
    clock, pub, st = FakeClock(), FakePub(), InMemoryStorage()
    svc = GreenhouseService(st, pub, Settings(topic_root=ROOT, **kw), clock=clock)
    return svc, pub, clock, st


_seq = {}


def tele(svc, clock, dev, dtype, m, boot=1, seq=None, buf=0, itv=5000):
    key = (dev, boot, id(svc))
    seq = seq if seq is not None else _seq.get(key, 0) + 1
    _seq[key] = seq
    msg = {"v": 1, "dev": dev, "type": dtype, "boot": boot, "seq": seq, "ts": int(clock().timestamp() * 1000), "up": 1,
           "buf": buf, "drop": 0, "mode": "auto", "itv": itv, "rssi": -55, "m": m}
    svc.handle_message(f"{ROOT}/{dev}/telemetry", json.dumps(msg))
