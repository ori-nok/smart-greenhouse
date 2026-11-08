# Протокол обмена и API

Документ описывает формат сообщений между узлами ESP32 и backend, правила надёжности и REST API.
Все примеры взяты из реальных логов прошивок (`backend/tests/fixtures/*.log`) и проверяются контрактным тестом
`backend/tests/test_firmware_contract.py`: если формат разойдётся с кодом, тест упадёт.

## 1. Архитектура

```
 ESP32 gh-soil-01 ──┐                                         ┌── TimescaleDB (telemetry, events, commands, devices)
 ESP32 gh-tank-01 ──┼── MQTT (QoS 1, JSON) ── брокер ── backend (FastAPI) ──┼── Prometheus ── Grafana
 ESP32 gh-climate-01┘      ▲ команды/конфигурация ▼          │            └── Telegram (тревоги)
                                                         REST API, web UI /ui
```

* Узлы автономны: локальная логика (гистерезис, защиты) работает и без связи; сервер только настраивает и наблюдает.
* Backend подписан на `<root>/+/{telemetry,status,event,cmd/ack,config/ack}`, публикует `cmd` и `config`.
* `<root>` по умолчанию `gh-k7q2x9/greenhouse`; его нужно сделать уникальным на публичном брокере
  (`python tools/set_topic_root.py my-team-42/greenhouse`).

## 2. Топики

| Топик | Направление | QoS | retain | Назначение |
|---|---|---|---|---|
| `<root>/<dev>/telemetry` | узел → сервер | 0 | нет | периодические измерения |
| `<root>/<dev>/status` | узел → сервер | 1 | да | `online`-heartbeat каждые 10 с; `offline` публикует брокер (LWT) |
| `<root>/<dev>/event` | узел → сервер | 0 | нет | события узла (неисправность, аварийная остановка…) |
| `<root>/<dev>/cmd` | сервер → узел | 1 | нет | команда управления |
| `<root>/<dev>/cmd/ack` | узел → сервер | 0 | нет | подтверждение команды |
| `<root>/<dev>/config` | сервер → узел | 1 | да | удалённая конфигурация (версионируется) |
| `<root>/<dev>/config/ack` | узел → сервер | 0 | нет | подтверждение применения конфигурации |

Идентификаторы узлов: `gh-soil-01` (тип `soil`), `gh-tank-01` (`tank`), `gh-climate-01` (`climate`).

## 3. Общие поля

Все сообщения — JSON (UTF-8), поле `v` = версия формата (сейчас `1`). Неизвестные поля игнорируются, чтобы
новые версии прошивок оставались совместимыми.

| Поле | Тип | Смысл |
|---|---|---|
| `v` | int | версия формата |
| `dev` | string | идентификатор узла (должен совпадать с `<dev>` в топике) |
| `boot` | int | случайный идентификатор запуска; меняется при каждой перезагрузке |
| `seq` | int | порядковый номер телеметрии внутри `boot`, начиная с 1, без пропусков |
| `ts` | int | время измерения, мс Unix epoch UTC (по NTP). Если часы узла не синхронизированы — `ts` не используется, backend ставит время приёма |
| `up` | int | время работы с запуска, мс |

Пара `(boot, seq)` однозначно идентифицирует сообщение: по ней backend находит потери, дубликаты и перезапуски.

## 4. Телеметрия

```json
{"v":1,"dev":"gh-soil-01","type":"soil","boot":3969145544,"seq":1,"ts":1793736345794,"up":100,
 "buf":0,"drop":0,"mode":"auto","itv":2000,"rssi":-55,
 "m":{"soil_moisture_pct":0.0,"soil_raw":0,"pump_on":1,"pump_locked":0,"pump_cooldown":0,"override":0}}
```

| Поле | Смысл |
|---|---|
| `buf` | 1 — сообщение отправлено из буфера после восстановления связи (`ts` — время реального измерения) |
| `drop` | сколько сообщений узел потерял из-за переполнения буфера (накопительно за `boot`) |
| `mode` | `auto` (локальная автоматика) или `manual` |
| `itv` | текущий интервал телеметрии, мс (адаптивный) |
| `rssi` | уровень Wi-Fi, дБм |
| `m` | метрики узла (см. ниже) |

### Метрики по типам узлов

| Узел | Метрика | Единицы | Датчик / исполнитель |
|---|---|---|---|
| soil | `soil_moisture_pct`, `soil_raw` | %, АЦП 0..4095 | **датчик влажности почвы** (новый) |
| soil | `pump_on`, `pump_locked`, `pump_cooldown`, `override` | 0/1 | реле насоса |
| tank | `distance_cm`, `level_pct`, `sensor_ok` | см, %, 0/1 | **ультразвуковой HC-SR04** (новый); `level_pct = -1`, `distance_cm = -1` при отказе |
| tank | `tank_state`, `muted` | 0 ок / 1 мало / 2 критично, 0/1 | светодиод и зуммер |
| climate | `temperature_c` | °C | **NTC-термистор** (новый) |
| climate | `light_lux` | lux | **фоторезистор** (новый) |
| climate | `vent_open`, `grow_light_on`, `override` | 0/1 | сервопривод форточки, светодиод досветки |

## 5. Статус и LWT

Узел при подключении к брокеру регистрирует Last Will (`qos=1`, `retain=true`):

```json
{"v":1,"dev":"gh-soil-01","state":"offline","reason":"lwt"}
```

После подключения и далее каждые 10 с узел публикует retained-сообщение:

```json
{"v":1,"dev":"gh-soil-01","type":"soil","fw":"1.0.0","state":"online","boot":3969145544,"up":0,
 "rssi":-55,"heap":200000,"buf_n":0,"cfg_ver":0}
```

`cfg_ver` — версия применённой конфигурации: если она отстаёт от версии на сервере, backend заново публикует конфигурацию
(**самовосстановление настроек**). Узел считается недоступным, если брокер прислал LWT или нет сообщений дольше
`OFFLINE_AFTER_S` (35 с).

## 6. События узла

```json
{"v":1,"dev":"gh-tank-01","boot":3969145544,"ts":1793736346119,"type":"sensor_fault","sev":"critical",
 "msg":"HC-SR04: no echo (3 consecutive failures)"}
```

`sev`: `info` | `warning` | `critical`. Типы: `sensor_fault`, `sensor_recovered`, `tank_state`, `pump_failsafe`,
`pump_locked_stop`, `link_restored` (содержит число отправленных из буфера сообщений).

Backend формирует свои события (`events`): `device_online/offline`, `anomaly`, `sensor_stuck`, `leak_suspected`,
`irrigation_ineffective`, `pump_fault`, `pump_locked/unlocked`, `heat_alarm/heat_cleared`, `frost_alarm`,
`config_changed`, `command_timeout`, `command_rejected`, `telemetry_gap`, `telemetry_stalled`,
`auto_reboot_suppressed`, `device_restart`, `invalid_message` и др.

## 7. Команды

Сервер → узел (`<root>/<dev>/cmd`, QoS 1):

```json
{"cmd_id":"c-1a2b3c4d5e","cmd":"pump_on","duration_s":15}
```

Ответ узла (`cmd/ack`):

```json
{"v":1,"dev":"gh-soil-01","cmd_id":"c1","status":"ok","reason":""}
```

`status`: `ok` | `rejected`; `reason` при отказе: `unknown_cmd`, `pump_locked`, `bad_request`, `unsupported`… При повторной доставке QoS 1
узел узнаёт уже выполненный `cmd_id` и отвечает `reason:"duplicate"`, не выполняя команду второй раз.
Если ack не пришёл за `CMD_TIMEOUT_S` (15 с), команда помечается `timeout`.

| Команда | Узлы | Аргументы | Действие |
|---|---|---|---|
| `ping` | все | — | проверка связи |
| `reboot` | все | — | перезагрузка узла |
| `pump_on` / `pump_off` | soil | `duration_s` 1–600 / 1–3600 | принудительное включение/выключение насоса на время (потом снова автоматика) |
| `buzzer_mute` | tank | `duration_s` 1–600 | заглушить зуммер |
| `vent_open` / `vent_close` | climate | `duration_s` 1–3600 | открыть/закрыть форточку |
| `light_on` / `light_off` | climate | `duration_s` 1–3600 | включить/выключить досветку |

Защиты исполнителей: насос не работает дольше `pump_max_run_s` (failsafe, затем пауза `pump_cooldown_s`), даже по команде;
при `pump_locked` команда `pump_on` отклоняется.

## 8. Конфигурация

`<root>/<dev>/config` (retained, QoS 1):

```json
{"cfg_ver":5,"interval_ms":60000,"moist_low":35,"moist_high":65}
```

Узел применяет только валидные значения, сохраняет `cfg_ver` и отвечает:

```json
{"v":1,"dev":"gh-soil-01","cfg_ver":5,"status":"ok"}
```

Параметры и допустимые диапазоны (проверяются и на сервере, и на узле):

| Группа | Параметр | Диапазон | По умолчанию |
|---|---|---|---|
| все | `interval_ms` | 1000–60000 | 5000 |
| все | `mode` | `auto` / `manual` | `auto` |
| soil | `moist_low`, `moist_high` | 0–100 % | 30 / 60 |
| soil | `pump_enabled` | 0/1 | 1 |
| soil | `pump_max_run_s` | 5–600 | 30 |
| soil | `pump_cooldown_s` | 0–3600 | 20 |
| tank | `tank_height_cm` | 10–400 | 100 |
| tank | `level_low_pct`, `level_crit_pct` | 0–100 % | 25 / 10 |
| climate | `temp_open_c`, `temp_close_c` | −20…60 °C | 28 / 25 |
| climate | `lux_low`, `lux_high` | 0–100000 | 200 / 400 |

## 9. Надёжность и восстановление

| Ситуация | Механизм |
|---|---|
| Обрыв Wi-Fi/брокера | узел продолжает автоматику, складывает телеметрию в кольцевой буфер (24 сообщения), переподключается с экспоненциальной паузой до 10 с; после восстановления отправляет буфер с `buf=1` и исходными `ts` |
| Узел пропал | брокер публикует LWT → `device_offline`; при возвращении — `device_online` и учёт времени простоя |
| Потеря/дубликат сообщения | `seq` по `boot`: пропуск → метрика `telemetry_lost_total`, дубликат отбрасывается |
| Перезагрузка узла | смена `boot` → `seq` сбрасывается без ложной «потери» |
| Долго нет связи с сервером (60 с) | узел переходит в безопасный режим `auto` и локальные пороги |
| Узел завис (телеметрия не идёт, но онлайн) | watchdog backend отправляет `reboot`, не более 3 раз в час на узел |
| Потеря настроек | сервер сверяет `cfg_ver` из heartbeat и пере-публикует конфигурацию; retained-конфиг при переподключении брокера |
| Некорректное сообщение | отбрасывается и считается в `invalid_messages_total`, не влияет на остальные |

## 10. REST API

Базовый URL `http://localhost:8000`. Интерактивная документация — `/docs` (Swagger UI). Все методы `/api/v1/*`
требуют заголовок `X-API-Key: <API_KEY>`; `/health`, `/metrics`, `/ui` открыты. Есть ограничение частоты
(`RATE_LIMIT_PER_MIN`, по умолчанию 120 запросов/мин на ключ) — при превышении ответ 429.

| Метод и путь | Назначение |
|---|---|
| `GET /health` | состояние БД и MQTT (для healthcheck) |
| `GET /metrics` | метрики Prometheus |
| `GET /api/v1/summary` | сводка: устройства онлайн, активные тревоги, последние значения |
| `GET /api/v1/devices` | список узлов и их состояние |
| `GET /api/v1/devices/{id}` | карточка узла (конфигурация, `cfg_ver`, последние данные) |
| `GET /api/v1/devices/{id}/telemetry?start=&end=&limit=&order=` | сырая телеметрия |
| `GET /api/v1/devices/{id}/aggregate?metric=&bucket_s=&start=&end=` | агрегаты (`time_bucket` в TimescaleDB): avg/min/max |
| `GET /api/v1/telemetry/latest` | последние значения всех узлов |
| `POST /api/v1/devices/{id}/commands` | отправить команду: `{"cmd":"pump_on","duration_s":15}` → 202 и `cmd_id` |
| `GET /api/v1/commands?device_id=&limit=` | история команд и статусов |
| `PUT /api/v1/devices/{id}/config` | изменить конфигурацию: `{"moist_low":35}` |
| `GET /api/v1/events?severity=&device_id=&limit=` | события и тревоги |
| `GET /api/v1/export/telemetry.csv` | выгрузка телеметрии в CSV |

Пример:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/v1/devices
curl -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
     -d '{"cmd":"vent_open","duration_s":120}' \
     http://localhost:8000/api/v1/devices/gh-climate-01/commands
```

Коды ответов: 200/202 — успех; 400/422 — неверные аргументы или значения вне диапазона; 401 — нет/неверный ключ;
404 — неизвестное устройство; 429 — превышен лимит; 503 — нет связи с MQTT/БД.

## 11. Правила прикладной логики (backend)

| Правило | Условие | Действие |
|---|---|---|
| Блокировка насоса при пустом баке | уровень ≤ критического порога (`level_crit_pct`) | `pump_enabled = 0` на soil; разблокировка, когда уровень > крит. + 10 % |
| Неэффективный полив | насос работал, а влажность не выросла | событие `irrigation_ineffective` |
| Отказ насоса | 3 неэффективных полива за 30 мин | `pump_fault`, блокировка до решения оператора |
| Жара | температура > 35 °C | `heat_alarm` + команда `vent_open`; снятие с гистерезисом |
| Заморозки | температура ≤ 3 °C | `frost_alarm` + команды `vent_close` и `light_on` (на 5 мин); снятие при +2 °C |
| Утечка | уровень падает быстро, насосы не работают | `leak_suspected` |
| «Залипший» датчик | значение не меняется при шуме АЦП | `sensor_stuck` |
| Аномалия | EWMA z-score с winsorizing, регулярная смена режима учитывается | `anomaly` |
| Нет телеметрии | узел онлайн, но данных нет | `telemetry_stalled` + авто-`reboot` (≤ 3/ч, иначе `auto_reboot_suppressed`) |
