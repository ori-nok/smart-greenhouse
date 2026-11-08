#!/usr/bin/env python3
"""Собирает и ВЫПОЛНЯЕТ notebooks/analysis.ipynb без зависимости от jupyter/nbconvert.
Ячейки исполняются в одном пространстве имён, stdout и графики сохраняются в ячейки как outputs.
Запуск:  python tools/build_notebook.py
(в Jupyter ноутбук открывается и запускается обычным образом: Kernel -> Restart & Run All)"""
import ast
import base64
import contextlib
import io
import json
import os
import sys
import traceback

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "notebooks", "analysis.ipynb")

CELLS = []


def md(s):
    CELLS.append(("markdown", s.strip("\n")))


def code(s):
    CELLS.append(("code", s.strip("\n")))


md("""
# Умная теплица: распределённая микроконтроллерная система
### Рабочий ноутбук проекта: описание, запуск, проверка и анализ данных

Три виртуальных ESP32 (`gh-soil-01`, `gh-tank-01`, `gh-climate-01`) измеряют и регулируют параметры теплицы, обмениваются
данными по MQTT с backend, который хранит телеметрию, применяет правила и отдаёт API. Ноутбук **выполняет работу целиком
внутри Python**, без Docker и Wokwi: прогоняет систему на симуляторе (те же JSON-сообщения проходят через настоящий код
backend), проверяет протокол по логам настоящих прошивок, запускает автотесты и анализирует полученные данные.

| Часть | Что делает |
|---|---|
| A | описание системы, протокола и правил |
| B | запуск системы на 72 часа симуляции, 9 сценариев |
| C | проверка протокола по логам прошивок |
| D | автоматические тесты |
| E | анализ данных (качество, полив, частота, связь, инциденты, аномалии) |
| F | демонстрация REST API |
| G | что нужно запустить вручную (Docker, Wokwi) |

**Как запускать:** `jupyter notebook notebooks/analysis.ipynb` → *Kernel → Restart & Run All* (около минуты).
Нужны `pandas numpy matplotlib scikit-learn scipy`; для части F дополнительно `pip install -r backend/requirements-dev.txt`.
""")

code("""
import os, sys, json, time, subprocess, shutil, tempfile, hashlib
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
pd.set_option("display.width", 160); pd.set_option("display.max_columns", 30)
plt.rcParams.update({"figure.dpi": 100, "axes.grid": True, "grid.alpha": .3, "axes.spines.top": False, "axes.spines.right": False})

ROOT = os.path.abspath(os.path.join(os.getcwd(), "..")) if os.path.basename(os.getcwd()) == "notebooks" else os.getcwd()
assert os.path.exists(os.path.join(ROOT, "docker-compose.yml")), "Запустите ноутбук из корня проекта или из папки notebooks/"
for p in (ROOT, os.path.join(ROOT, "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)
DATA = os.path.join(ROOT, "data")
print("корень проекта:", ROOT)
""")

md("""
# Часть A. Система, протокол и правила

**Узлы и датчики.** Новые по сравнению с семинаром датчики: влажность почвы, ультразвуковой HC-SR04, NTC-термистор,
фоторезистор. Исполнители: реле насоса, сервопривод форточки, досветка, зуммер.

| Узел | Измеряет | Управляет | Локальная автоматика |
|---|---|---|---|
| `gh-soil-01` | влажность почвы | насос | вкл. при < 30 %, выкл. при ≥ 60 %, защита насоса по времени |
| `gh-tank-01` | уровень воды (HC-SR04) | зуммер, светодиод | состояния ок / мало (≤ 25 %) / критично (≤ 10 %) с гистерезисом |
| `gh-climate-01` | температура, освещённость | форточка, досветка | форточка > 28 °C, досветка < 200 lux |

**Протокол.** MQTT, JSON v1, топики `<root>/<dev>/{telemetry,status,event,cmd,cmd/ack,config,config/ack}`.
Каждая телеметрия несёт `ts` (время измерения, мс), `boot` (идентификатор запуска) и `seq` (номер без пропусков),
команды подтверждаются по `cmd_id`, конфигурация версионируется (`cfg_ver`). Полное описание: `docs/PROTOCOL.md`.

**Надёжность.** Last Will + heartbeat, кольцевой буфер на 24 сообщения при обрыве, безопасный режим узла,
watchdog backend с автоперезагрузкой (не более 3 раз в час), повтор конфигурации при расхождении `cfg_ver`.

**Правила backend.** Блокировка насоса при критическом уровне воды; неэффективный полив → отказ насоса;
жара → автоматическое открытие форточки; заморозки; утечка; «залипший» датчик; аномалии (EWMA z-score).
""")

md("""
# Часть B. Запуск системы на симуляторе (72 часа)

`Harness` соединяет модель трёх узлов с **настоящим** `GreenhouseService` (тем же, что работает за MQTT и базой данных).
Сообщения проходят через те же парсеры, детекторы и правила. Сценарии: настройка через конфигурацию, команды оператора,
шум и «залипание» датчика, отказ датчика уровня, утечка, жара, обрывы связи, отказ насоса.
""")

code("""
from simulator.make_dataset import Harness, add_scenarios, ALL, export

t0 = time.time()
harness = h = Harness(seed=7)  # harness нужен для части F, h не трогать
add_scenarios(h, ALL)
h.run(72)
print(f"симуляция 72 ч выполнена за {time.time() - t0:.0f} с")
print("измерений:", len(h.st.telemetry), "| событий:", len(h.st.events), "| команд:", len(h.st.commands))
print("сценарии:", ", ".join(ALL))
""")

code("""
# воспроизводимость: заново полученные данные должны совпасть с файлами в data/ побайтно
tmp = tempfile.mkdtemp()
export(h, tmp)
md5 = lambda p: hashlib.md5(open(p, "rb").read()).hexdigest()
same = {f: md5(os.path.join(tmp, f)) == md5(os.path.join(DATA, f)) for f in ("telemetry.csv", "events.csv", "commands.csv")}
print(same)
assert all(same.values()), "данные расходятся с data/"
""")

md("""
# Часть C. Протокол на логах настоящих прошивок

Скетчи трёх узлов собраны на обычном компьютере (g++, заглушки Arduino/WiFi/MQTT) и выполнены в сценариях; их MQTT-обмен
записан в `backend/tests/fixtures/*.log`. Ниже — какие сообщения публикуют узлы, и по одному примеру каждого типа.
""")

code("""
fx = os.path.join(ROOT, "backend", "tests", "fixtures")
rows, samples = [], {}
for fn in sorted(os.listdir(fx)):
    if not fn.endswith(".log"):
        continue
    for line in open(os.path.join(fx, fn), encoding="utf-8"):
        p = line.rstrip("\\n").split("\\t")
        if p[0] == "PUB":
            kind = "/".join(p[1].split("/")[3:])
            rows.append({"узел": fn[:-4], "тип": kind})
            samples.setdefault(kind, p[-1])
pub = pd.DataFrame(rows)
print("публикаций в логах:", len(pub))
pub.groupby(["узел", "тип"]).size().unstack(fill_value=0)
""")

code("""
for kind, payload in sorted(samples.items()):
    print(f"--- {kind}")
    print(json.dumps(json.loads(payload), ensure_ascii=False)[:300])
""")

md("""
# Часть D. Автоматические проверки

Тесты backend (протокол, детекторы, правила, контракт с прошивками, замкнутый цикл на 72 часа) и хост-тесты прошивок.
""")

code("""
t0 = time.time()
r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                   cwd=os.path.join(ROOT, "backend"), capture_output=True, text=True)
out = (r.stdout + r.stderr).strip().splitlines()
print("\\n".join(out[-5:]))
print(f"код возврата: {r.returncode}, время: {time.time() - t0:.0f} с")
assert r.returncode == 0
""")

code("""
# хост-тесты прошивок: нужен g++ и bash (Linux/macOS; на Windows - WSL или Git Bash)
if shutil.which("g++") and shutil.which("bash"):
    r = subprocess.run(["bash", "firmware/tests/run_tests.sh"], cwd=ROOT, capture_output=True, text=True)
    print("\\n".join((r.stdout + r.stderr).strip().splitlines()[-8:]))
    assert r.returncode == 0
else:
    print("g++/bash не найдены - пропуск. Запустите вручную: bash firmware/tests/run_tests.sh")
""")

md("""
# Часть E. Анализ данных

Данные: 72 часа работы трёх узлов (результат части B, сохранён в `data/`). Разделы: качество данных и целостность
последовательностей; динамика процессов; эффективность полива; адаптивная частота; связь и буферизация; инциденты;
аномалии (IsolationForest против правил backend); суточная сводка; выводы.
""")

code("""
tel = pd.read_csv(os.path.join(DATA, "telemetry.csv"), parse_dates=["ts"])
ev  = pd.read_csv(os.path.join(DATA, "events.csv"), parse_dates=["ts"])
cmd = pd.read_csv(os.path.join(DATA, "commands.csv"), parse_dates=["ts"])
print("telemetry:", tel.shape, "| events:", ev.shape, "| commands:", cmd.shape)
print("период:", tel.ts.min(), "->", tel.ts.max())
tel.head()
""")

md("## E1. Качество данных и целостность последовательностей")
code("""
g = tel.groupby("device_id")
q = pd.DataFrame({
    "строк": g.size(),
    "буферизованных": g.buffered.sum(),
    "доля буфера, %": (g.buffered.mean() * 100).round(2),
    "перезагрузок (boot)": g.boot.nunique() - 1,
})
# пропуски seq внутри одного boot-цикла: должно быть 0, если буферизация и повторная отправка работают
def gaps(df):
    lost = 0
    for _, b in df.groupby("boot"):
        s = np.sort(b.seq.values)
        lost += int((np.diff(s) - 1).clip(min=0).sum())
    return lost
q["потерянных seq"] = g.apply(gaps, include_groups=False)
q["дубликатов seq"] = g.apply(lambda d: int(d.duplicated(["boot", "seq"]).sum()), include_groups=False)
q
""")
code("""
# доля пропусков по метрикам (NaN нормальны: каждый узел присылает только свои метрики)
miss = tel.drop(columns=["ts", "device_id", "boot", "seq", "buffered"]).isna().groupby(tel.device_id).mean().round(2)
miss.loc[:, (miss < 1).any()]
""")

md("## E2. Динамика процессов")
code("""
soil = tel[tel.device_id == "gh-soil-01"].set_index("ts").sort_index()
tank = tel[tel.device_id == "gh-tank-01"].set_index("ts").sort_index()
clim = tel[tel.device_id == "gh-climate-01"].set_index("ts").sort_index()

fig, ax = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
ax[0].plot(soil.index, soil.soil_moisture_pct, lw=.8, color="#2a7")
ax[0].fill_between(soil.index, 0, 100, where=soil.pump_on == 1, color="#39f", alpha=.35, step="post", label="насос включён")
ax[0].axhline(30, ls="--", c="r", lw=.8); ax[0].axhline(60, ls="--", c="g", lw=.8)
ax[0].set_ylabel("влажность почвы, %"); ax[0].set_ylim(0, 100); ax[0].legend(loc="upper right")
ax[1].plot(tank.index, tank.level_pct.where(tank.level_pct >= 0), lw=.8, color="#37c")  # -1 = ошибка датчика
ax[1].axhline(25, ls="--", c="orange", lw=.8); ax[1].axhline(10, ls="--", c="r", lw=.8)
ax[1].set_ylabel("уровень воды в баке, %")
ax[2].plot(clim.index, clim.temperature_c, lw=.8, color="#d52", label="температура, °C")
ax[2].fill_between(clim.index, 10, 55, where=clim.vent_open == 1, color="#fa0", alpha=.25, step="post", label="форточка открыта")
ax[2].set_ylabel("°C"); ax[2].set_ylim(10, 55)
a2 = ax[2].twinx(); a2.plot(clim.index, clim.light_lux, lw=.5, color="#888", alpha=.7); a2.set_ylabel("освещённость, lux"); a2.grid(False)
ax[2].legend(loc="upper left")
ax[2].xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
fig.tight_layout(); plt.show()
""")

md("## E3. Эффективность полива\nДля каждого включения насоса считаем прирост влажности и длительность.")
code("""
s = soil[["soil_moisture_pct", "pump_on"]].copy()
s["run"] = (s.pump_on.diff().fillna(0) == 1).cumsum() * (s.pump_on == 1)
runs = []
for rid, d in s[s.run > 0].groupby("run"):
    before = s.soil_moisture_pct[:d.index[0]].tail(3).mean()
    after = s.soil_moisture_pct[d.index[-1]:].head(5).max()
    runs.append({"начало": d.index[0], "длит., с": (d.index[-1] - d.index[0]).total_seconds(),
                 "до, %": round(before, 1), "после, %": round(after, 1), "прирост, %": round(after - before, 1)})
runs = pd.DataFrame(runs)
print("поливов:", len(runs), "| средний прирост:", round(runs["прирост, %"].mean(), 1), "%")
runs.head(15)
""")
code("""
# неэффективные поливы (прирост < 3 п.п.) - кандидаты на отказ насоса / пустую магистраль
bad = runs[runs["прирост, %"] < 3]
print("неэффективных:", len(bad)); bad
""")

md("## E4. Адаптивная частота телеметрии\nУзел переходит на более частые измерения во время активных процессов (полив, тревога по баку, быстрая смена температуры).")
code("""
nb = tel[tel.buffered == 0].sort_values(["device_id", "ts"]).copy()
nb["dt"] = nb.groupby(["device_id", "boot"]).ts.diff().dt.total_seconds()
print(nb.groupby("device_id").dt.describe().round(1)[["count", "min", "25%", "50%", "75%", "max"]])
fig, ax = plt.subplots(figsize=(10, 3.5))
for dev, d in nb.groupby("device_id"):
    ax.hist(d.dt.clip(upper=70), bins=np.arange(0, 72, 2), alpha=.55, label=dev)
ax.set_xlabel("интервал между сообщениями, с"); ax.set_ylabel("число сообщений"); ax.set_yscale("log"); ax.legend()
fig.tight_layout(); plt.show()
""")

md("## E5. Связь, буферизация и восстановление\nПри обрыве узел складывает сообщения в кольцевой буфер и после восстановления отправляет с флагом `buf=1` и исходными метками времени.")
code("""
b = tel[tel.buffered == 1]
print("буферизованных сообщений:", len(b))
b.groupby("device_id").agg(первое=("ts", "min"), последнее=("ts", "max"), штук=("seq", "size"))
""")
code("""
off = ev[ev.type.isin(["device_offline", "device_online", "link_restored"])][["ts", "device_id", "type", "severity", "message"]]
off
""")

md("## E6. Инциденты")
code("""
inc = ev[~ev.type.isin(["config_changed", "device_registered", "device_online"])]
print(inc.groupby(["severity", "type"]).size().to_string())
inc[inc.severity.isin(["critical", "warning"]) & (inc.type != "anomaly")][["ts", "device_id", "type", "severity", "message"]].reset_index(drop=True)
""")
code("""
# жара: тревога backend и автоматическая команда vent_open
heat = ev[ev.type.isin(["heat_alarm", "heat_cleared"])]
t0 = heat.ts.iloc[0]
w = clim[t0 - pd.Timedelta("30min"): t0 + pd.Timedelta("3h")]
fig, ax = plt.subplots(figsize=(11, 3.8))
ax.plot(w.index, w.temperature_c, color="#d52"); ax.axhline(35, ls="--", c="r", lw=.8, label="порог 35 °C")
ax.fill_between(w.index, w.temperature_c.min(), w.temperature_c.max(), where=w.vent_open == 1, color="#fa0", alpha=.25, step="post", label="форточка открыта")
for _, r in heat.iterrows(): ax.axvline(r.ts, c="k", ls=":")
ax.legend(); ax.set_ylabel("°C"); fig.tight_layout(); plt.show()
print(cmd[cmd.source.str.startswith("rule")].to_string(index=False))
""")
code("""
# утечка: скорость падения уровня в «обычное» время и в окне инцидента
lv = tank.level_pct.where(tank.level_pct >= 0)
rate = lv.diff() / tank.index.to_series().diff().dt.total_seconds() * 3600
leak_ev = ev[ev.type == "leak_suspected"].iloc[0]
full = lv[leak_ev.ts - pd.Timedelta("3h"): leak_ev.ts + pd.Timedelta("1h")]
win = lv[leak_ev.ts - pd.Timedelta("3min"): leak_ev.ts + pd.Timedelta("25min")]   # окно, на котором сработало правило
x = (win.index - win.index[0]).total_seconds() / 3600
slope, icpt = np.polyfit(x[win.notna()], win.dropna(), 1)
print(f"событие leak_suspected: {leak_ev.ts}")
print(f"медианная скорость изменения уровня за все 72 ч: {rate.median():.2f} %/ч")
print(f"наклон в окне срабатывания правила: {slope:.2f} %/ч -> прогноз до пустого бака: {max(win.dropna().iloc[-1], 0) / abs(slope):.1f} ч" if slope < 0 else "наклон положительный")
fig, ax = plt.subplots(figsize=(11, 3.5))
ax.plot(full.index, full.values, label="уровень"); ax.axvspan(win.index[0], win.index[-1], color="r", alpha=.12, label="окно оценки наклона"); ax.axvline(leak_ev.ts, c="r", ls=":", label="leak_suspected")
ax.set_ylabel("%"); ax.legend(); fig.tight_layout(); plt.show()
""")

md("""
## E6b. Обнаружение аномалий

Backend ловит аномалии онлайн правилами (EWMA z-score, «залипание» датчика, утечка). Сравним с офлайн-моделью
**IsolationForest** по признакам: скорость изменения влажности, остаток относительно скользящей медианы, скользящее СКО.
Интервалы работы насоса исключаем: резкий рост влажности при поливе - штатное поведение.
""")
code("""
from sklearn.ensemble import IsolationForest
f = soil[["soil_moisture_pct", "pump_on"]].copy()
f["d1"] = f.soil_moisture_pct.diff()
f["res"] = f.soil_moisture_pct - f.soil_moisture_pct.rolling(15, center=True, min_periods=5).median()
f["std10"] = f.soil_moisture_pct.rolling(10, min_periods=5).std()
pump_zone = f.pump_on.rolling(8, center=True, min_periods=1).max() == 1
X = f[["d1", "res", "std10"]].dropna()
X = X[~pump_zone.reindex(X.index)]
iso = IsolationForest(n_estimators=200, contamination=0.002, random_state=7).fit(X)
f["iso"] = False
f.loc[X.index, "iso"] = iso.predict(X) == -1
print("точек помечено IsolationForest:", int(f.iso.sum()), "из", len(X))
rule = ev[(ev.device_id == "gh-soil-01") & (ev.type.isin(["anomaly", "sensor_stuck"]))]
print("событий backend по почвенному узлу:", len(rule))
fig, ax = plt.subplots(figsize=(12, 3.8))
ax.plot(f.index, f.soil_moisture_pct, lw=.6, color="#2a7")
ax.scatter(f.index[f.iso], f.soil_moisture_pct[f.iso], s=14, c="r", label="IsolationForest", zorder=3)
for i, r in enumerate(rule.itertuples()): ax.axvline(r.ts, c="b", alpha=.4, lw=.8, label="правило backend" if i == 0 else None)
ax.legend(); ax.set_ylabel("влажность, %"); fig.tight_layout(); plt.show()
""")
code("""
# согласие методов: какую долю правил-событий подтверждает офлайн-модель в окне ±5 минут
hits = 0
for r in rule.itertuples():
    near = f.iso[r.ts - pd.Timedelta("5min"): r.ts + pd.Timedelta("5min")]
    hits += int(near.any())
print(f"подтверждено офлайн-моделью: {hits} из {len(rule)}")
""")

md("## E7. Суточная сводка")
code("""
day = pd.DataFrame({
    "влажность, ср.": soil.soil_moisture_pct.resample("1D").mean(),
    "поливов": (soil.pump_on.diff() == 1).resample("1D").sum(),
    "уровень бака, мин.": tank.level_pct.where(tank.level_pct >= 0).resample("1D").min(),
    "T макс, °C": clim.temperature_c.resample("1D").max(),
    "T мин, °C": clim.temperature_c.resample("1D").min(),
}).round(1).dropna(subset=["влажность, ср."])
day
""")
code("""
sev = ev.groupby([ev.ts.dt.date, "severity"]).size().unstack(fill_value=0)
sev.plot(kind="bar", stacked=True, figsize=(7, 3.2), color={"info": "#8ab", "warning": "#fb3", "critical": "#d33"})
plt.xlabel("дата"); plt.ylabel("событий"); plt.tight_layout(); plt.show()
""")

md("""
## E8. Выводы

* Целостность: внутри каждого boot-цикла нет потерянных и дублированных `seq`, в том числе после обрывов связи -
  буфер и повторная отправка работают, а метки времени устройства сохраняются.
* Полив: типичный цикл поднимает влажность до верхнего порога гистерезиса; неэффективные поливы выявляются и
  эскалируются (`irrigation_ineffective` -> `pump_fault` -> блокировка насоса до решения оператора).
* Адаптивная частота: во время активных процессов интервал сокращается, в покое остаётся базовым - трафик экономится.
* Инциденты (жара, утечка, неисправность датчика уровня, «залипание», отключение узлов) обнаружены и описаны событиями;
  при перегреве backend сам отправил команду открытия форточки.
* Аномалии: офлайн-модель IsolationForest подтверждает часть срабатываний правил; различия объясняются тем, что правила
  работают онлайн и знают контекст (режим, состояние насоса), а модель видит только форму сигнала.
* Ограничения: данные синтетические (физическая модель + шум), поэтому пороги нужно перекалибровать на реальной теплице.
""")

md("""
# Часть F. REST API (в процессе, без сервера)

Приложение FastAPI создаётся на том же `GreenhouseService`, в котором после симуляции лежат данные, и опрашивается
тестовым HTTP-клиентом. Так проверяются авторизация по `X-API-Key`, чтение данных, агрегаты и отправка команды.
""")

code("""
try:
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.config import Settings
except ImportError as e:
    print("Часть F пропущена:", e)
    print("Установите зависимости: pip install -r backend/requirements-dev.txt  и запустите ячейку снова.")
else:
    client = TestClient(create_app(harness.svc, Settings(topic_root="gh-k7q2x9/greenhouse", api_key="demo", rate_limit_per_min=600)))
    H = {"X-API-Key": "demo"}
    print("без ключа:", client.get("/api/v1/devices").status_code, "| с ключом:", client.get("/api/v1/devices", headers=H).status_code)
    devs = client.get("/api/v1/devices", headers=H).json()
    print(pd.DataFrame([{k: d[k] for k in ("device_id", "type", "state", "cfg_ver")} for d in devs]))
    agg = client.get("/api/v1/devices/gh-climate-01/aggregate", params={"metric": "temperature_c", "bucket_s": 3600}, headers=H).json()
    print("агрегат температуры по часам (первые 3):", agg[:3] if isinstance(agg, list) else agg)
    r = client.post("/api/v1/devices/gh-soil-01/commands", json={"cmd": "pump_on", "duration_s": 10}, headers=H)
    print("команда pump_on:", r.status_code, r.json())
    print("health:", client.get("/health").json())
""")

md("""
# Часть G. Что нужно запустить вручную

В ноутбуке выполнено всё, что возможно без внешних сервисов. Остальное запускается на вашем компьютере по
пошаговой инструкции **`docs/ИНСТРУКЦИЯ.md`**:

1. `docker compose up` — реальные брокер, база TimescaleDB, backend, Prometheus, Grafana (вариант с симулятором узлов);
2. три проекта Wokwi (`firmware/<узел>/`) — виртуальные ESP32 подключаются к тому же брокеру;
3. сохранить скриншоты Grafana и Wokwi для отчёта и защиты.

Эти шаги проверяют то, что симулятор заменяет: настоящую базу данных, брокер и скетчи на виртуальном железе.
""")


def run_cells():
    ns = {"__name__": "__main__"}
    os.chdir(ROOT)
    out_cells, n = [], 0
    for kind, src in CELLS:
        if kind == "markdown":
            out_cells.append({"cell_type": "markdown", "metadata": {}, "source": src.splitlines(True)})
            continue
        n += 1
        outputs = []
        buf = io.StringIO()
        value = None
        try:
            tree = ast.parse(src)
            last = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last = ast.Expression(tree.body.pop().value)
            with contextlib.redirect_stdout(buf):
                exec(compile(tree, "<cell>", "exec"), ns)
                if last is not None:
                    value = eval(compile(last, "<cell>", "eval"), ns)
        except Exception:
            traceback.print_exc()
            print("Ошибка в ячейке:\n" + src, file=sys.stderr)
            raise SystemExit(1)
        if buf.getvalue():
            outputs.append({"output_type": "stream", "name": "stdout", "text": buf.getvalue().splitlines(True)})
        for num in plt.get_fignums():
            f = plt.figure(num)
            b = io.BytesIO()
            f.savefig(b, format="png", dpi=100, bbox_inches="tight")
            outputs.append({"output_type": "display_data", "metadata": {},
                            "data": {"image/png": base64.b64encode(b.getvalue()).decode(), "text/plain": ["<Figure>"]}})
        plt.close("all")
        if value is not None:
            data = {"text/plain": repr(value).splitlines(True) if not hasattr(value, "to_string") else value.to_string().splitlines(True)}
            if hasattr(value, "to_html"):
                data["text/html"] = value.to_html().splitlines(True)
            outputs.append({"output_type": "execute_result", "execution_count": n, "metadata": {}, "data": data})
        out_cells.append({"cell_type": "code", "execution_count": n, "metadata": {}, "outputs": outputs,
                          "source": src.splitlines(True)})
    return out_cells


def main():
    cells = run_cells()
    nb = {"cells": cells, "nbformat": 4, "nbformat_minor": 5,
          "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                       "language_info": {"name": "python"}}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(nb, fh, ensure_ascii=False, indent=1)
    print("written", OUT, "cells:", len(cells), "figures:",
          sum(1 for c in cells for o in c.get("outputs", []) if o["output_type"] == "display_data"))


if __name__ == "__main__":
    main()
