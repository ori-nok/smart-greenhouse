-- Схема хранилища. Выполняется при первом старте БД и идемпотентно повторно - backend при запуске.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS devices (
    device_id      TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    fw             TEXT,
    state          TEXT NOT NULL DEFAULT 'unknown',
    mode           TEXT,
    boot           BIGINT,
    last_seq       BIGINT,
    first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen      TIMESTAMPTZ,
    last_telemetry TIMESTAMPTZ,
    rssi           INTEGER,
    itv_ms         INTEGER,
    config         JSONB NOT NULL DEFAULT '{}',
    cfg_ver        INTEGER NOT NULL DEFAULT 0,
    cfg_acked      INTEGER NOT NULL DEFAULT 0,
    meta           JSONB NOT NULL DEFAULT '{}'
);

-- Телеметрия: гипертаблица TimescaleDB, партиционирование по времени.
CREATE TABLE IF NOT EXISTS telemetry (
    ts          TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    device_id   TEXT        NOT NULL,
    boot        BIGINT      NOT NULL,
    seq         BIGINT      NOT NULL,
    buffered    BOOLEAN     NOT NULL DEFAULT FALSE,
    metrics     JSONB       NOT NULL,
    PRIMARY KEY (device_id, boot, seq, ts)
);
SELECT create_hypertable('telemetry', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS telemetry_dev_ts_idx ON telemetry (device_id, ts DESC);

CREATE TABLE IF NOT EXISTS events (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    device_id TEXT,
    type      TEXT NOT NULL,
    severity  TEXT NOT NULL,
    message   TEXT,
    data      JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts DESC);

CREATE TABLE IF NOT EXISTS commands (
    cmd_id    TEXT PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    device_id TEXT NOT NULL,
    cmd       TEXT NOT NULL,
    args      JSONB NOT NULL DEFAULT '{}',
    source    TEXT,
    status    TEXT NOT NULL,
    acked_at  TIMESTAMPTZ,
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS commands_ts_idx ON commands (ts DESC);
