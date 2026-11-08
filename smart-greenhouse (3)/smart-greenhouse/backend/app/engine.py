"""Алгоритмы обработки потока телеметрии (чистый Python, без зависимостей):
контроль последовательности сообщений, обнаружение аномалий и утечки, ограничитель частоты, метрики."""
from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple


class SeqTracker:
    """Отслеживает seq/boot каждого устройства: пропуски, дубликаты, перезагрузки."""

    def __init__(self) -> None:
        self._state: Dict[str, Tuple[int, int]] = {}

    def restore(self, dev: str, boot: Optional[int], seq: Optional[int]) -> None:
        if boot is not None and seq is not None:
            self._state[dev] = (boot, seq)

    def check(self, dev: str, boot: int, seq: int) -> Tuple[str, int]:
        """-> (kind, missing): kind in new|ok|gap|restart|duplicate."""
        prev = self._state.get(dev)
        missing = 0
        if prev is None:
            kind = "new"
        elif prev[0] != boot:
            kind = "restart"
        elif seq == prev[1] + 1:
            kind = "ok"
        elif seq > prev[1] + 1:
            kind, missing = "gap", seq - prev[1] - 1
        else:
            return "duplicate", 0
        self._state[dev] = (boot, seq)
        return kind, missing


class EwmaDetector:
    """EWMA среднего/дисперсии + z-оценка. Аномальные точки ограничиваются (винсоризация), чтобы выброс
    не смещал среднее; после `regime_after` аномалий подряд считаем, что режим изменился."""

    def __init__(self, alpha: float = 0.1, z_thr: float = 4.0, warmup: int = 20, abs_floor: float = 1.0,
                 rel_floor: float = 0.02, regime_after: int = 8) -> None:
        self.alpha, self.z_thr, self.warmup = alpha, z_thr, warmup
        self.abs_floor, self.rel_floor, self.regime_after = abs_floor, rel_floor, regime_after
        self.n = 0
        self.mean = 0.0
        self.var = 0.0
        self.consecutive = 0

    def update(self, x: float) -> Optional[float]:
        """Возвращает z-оценку, если точка аномальна, иначе None."""
        if self.n == 0:
            self.mean, self.var, self.n = x, 0.0, 1
            return None
        std = max(math.sqrt(self.var), self.abs_floor, self.rel_floor * abs(self.mean))
        z = (x - self.mean) / std
        anomalous = self.n >= self.warmup and abs(z) > self.z_thr
        if anomalous:
            self.consecutive += 1
            if self.consecutive >= self.regime_after:
                self.mean, self.var, self.consecutive = x, 0.0, 0
                self.n += 1
                return z
            xc = self.mean + math.copysign(self.z_thr * std, z)
        else:
            self.consecutive = 0
            xc = x
        d = xc - self.mean
        self.mean += self.alpha * d
        self.var = (1 - self.alpha) * (self.var + self.alpha * d * d)
        self.n += 1
        return z if anomalous else None


class StuckDetector:
    """«Залипание» датчика: N одинаковых подряд значений."""

    def __init__(self, limit: int = 120) -> None:
        self.limit = limit
        self.last: Optional[float] = None
        self.count = 0

    def update(self, x: float) -> bool:
        """True ровно один раз - в момент достижения порога."""
        if self.last is not None and x == self.last:
            self.count += 1
        else:
            self.last, self.count = x, 1
        return self.count == self.limit


class LeakDetector:
    """Подозрение на утечку: уровень в баке устойчиво падает, хотя насосы не работали."""

    def __init__(self, window_s: float = 600, min_span_s: float = 180, rate_pct_per_min: float = 0.8) -> None:
        self.window_s, self.min_span_s, self.rate = window_s, min_span_s, rate_pct_per_min
        self.hist: Deque[Tuple[float, float]] = deque()

    def update(self, ts: float, level: float, last_pump_ts: Optional[float]) -> Optional[float]:
        """-> скорость падения (%/мин), если обнаружена утечка."""
        self.hist.append((ts, level))
        while self.hist and ts - self.hist[0][0] > self.window_s:
            self.hist.popleft()
        t0, l0 = self.hist[0]
        span = ts - t0
        if span < self.min_span_s:
            return None
        if last_pump_ts is not None and last_pump_ts >= t0 - 30:  # в окне поливали - падение объяснимо
            return None
        drop_rate = (l0 - level) / (span / 60.0)
        pts = list(self.hist)
        downs = sum(1 for a, b in zip(pts, pts[1:]) if b[1] <= a[1] + 0.5)
        if drop_rate >= self.rate and downs >= 0.9 * (len(pts) - 1):
            return drop_rate
        return None


class RateLimiter:
    """Ограничение частоты запросов к API: «ведро токенов» на ключ (IP клиента)."""

    def __init__(self, per_minute: int, burst: Optional[int] = None) -> None:
        self.rate = per_minute / 60.0
        self.burst = float(burst if burst is not None else max(per_minute // 4, 5))
        self._b: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> Tuple[bool, float]:
        """-> (разрешено, через сколько секунд повторить)."""
        with self._lock:
            tokens, last = self._b.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._b[key] = (tokens - 1.0, now)
                return True, 0.0
            self._b[key] = (tokens, now)
            return False, (1.0 - tokens) / self.rate if self.rate > 0 else 60.0


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


class Metrics:
    """Минимальный экспорт в формате Prometheus (без внешних библиотек)."""

    def __init__(self, prefix: str = "greenhouse_") -> None:
        self.prefix = prefix
        self._c: Dict[Tuple[str, Tuple], float] = defaultdict(float)
        self._g: Dict[Tuple[str, Tuple], float] = {}
        self._lock = threading.Lock()

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._c[(name, tuple(sorted(labels.items())))] += value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._g[(name, tuple(sorted(labels.items())))] = float(value)

    def clear_gauge(self, name: str) -> None:
        with self._lock:
            for k in [k for k in self._g if k[0] == name]:
                del self._g[k]

    def get(self, name: str, **labels: str) -> float:
        key = (name, tuple(sorted(labels.items())))
        return self._c.get(key, self._g.get(key, 0.0))

    def render(self) -> str:
        with self._lock:
            lines: List[str] = []
            for kind, store in (("counter", self._c), ("gauge", self._g)):
                for n in sorted({k[0] for k in store}):
                    full = self.prefix + n
                    lines.append(f"# TYPE {full} {kind}")
                    for (nm, labels), v in sorted(store.items()):
                        if nm != n:
                            continue
                        lab = ",".join(f"{k}=\"{_esc(val)}\"" for k, val in labels)
                        lines.append(f"{full}{{{lab}}} {v:g}" if lab else f"{full} {v:g}")
            return "\n".join(lines) + "\n"
