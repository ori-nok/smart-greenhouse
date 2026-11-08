import random
import unittest

from app.engine import EwmaDetector, LeakDetector, Metrics, RateLimiter, SeqTracker, StuckDetector


class T(unittest.TestCase):
    def test_seq(self):
        s = SeqTracker()
        self.assertEqual(s.check("d", 1, 1), ("new", 0))
        self.assertEqual(s.check("d", 1, 2), ("ok", 0))
        self.assertEqual(s.check("d", 1, 6), ("gap", 3))
        self.assertEqual(s.check("d", 1, 6), ("duplicate", 0))
        self.assertEqual(s.check("d", 1, 5), ("duplicate", 0))
        self.assertEqual(s.check("d", 2, 1), ("restart", 0))
        self.assertEqual(s.check("d", 2, 2), ("ok", 0))

    def test_ewma(self):
        random.seed(1)
        d = EwmaDetector(abs_floor=1.0)
        for _ in range(100):
            self.assertIsNone(d.update(50 + random.gauss(0, 0.5)))
        self.assertIsNotNone(d.update(80))                       # скачок вверх
        self.assertAlmostEqual(d.mean, 50, delta=4)              # выброс не сместил среднее
        self.assertIsNone(d.update(50))
        r = [d.update(90) for _ in range(12)]                    # смена режима: детектор перестраивается
        self.assertIsNotNone(r[0]); self.assertIsNone(r[-1])

    def test_ewma_warmup_and_floor(self):
        d = EwmaDetector(warmup=20, abs_floor=2.0)
        self.assertIsNone(d.update(10))
        for _ in range(5):
            self.assertIsNone(d.update(1000))                    # прогрев: аномалий не фиксируем
        d2 = EwmaDetector(abs_floor=5.0)
        for _ in range(50):
            d2.update(20)
        self.assertIsNone(d2.update(24))                         # в пределах «пола» шума

    def test_stuck(self):
        s = StuckDetector(limit=5)
        hits = [s.update(1.0) for _ in range(8)]
        self.assertEqual(hits, [False] * 4 + [True] + [False] * 3)
        self.assertFalse(s.update(2.0))

    def test_leak(self):
        d = LeakDetector()
        res = [d.update(i * 30.0, 80 - i * 0.5, None) for i in range(20)]       # 1 %/мин, насосы молчат
        self.assertTrue(any(r for r in res))
        d = LeakDetector()
        self.assertTrue(all(d.update(i * 30.0, 80 - i * 0.5, i * 30.0) is None for i in range(20)))  # поливали
        d = LeakDetector()
        self.assertTrue(all(d.update(i * 30.0, 80 - (i % 2) * 0.2, None) is None for i in range(30)))  # шум

    def test_ratelimit(self):
        r = RateLimiter(60, burst=3)
        self.assertEqual([r.allow("a", 0.0)[0] for _ in range(4)], [True, True, True, False])
        ok, retry = r.allow("a", 0.0)
        self.assertFalse(ok); self.assertGreater(retry, 0)
        self.assertTrue(r.allow("a", 2.0)[0])                    # токены восстановились
        self.assertTrue(r.allow("b", 0.0)[0])                    # другой клиент не затронут

    def test_metrics(self):
        m = Metrics()
        m.inc("x_total", device="a"); m.inc("x_total", 2, device="a"); m.set_gauge("g", 1.5)
        txt = m.render()
        self.assertIn("greenhouse_x_total{device=\"a\"} 3", txt)
        self.assertIn("# TYPE greenhouse_g gauge", txt)
        self.assertIn("greenhouse_g 1.5", txt)


if __name__ == "__main__":
    unittest.main()
