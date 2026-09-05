-- KidTime 客户端本地库结构（ARCHITECTURE.md §4）
-- 6 张表：local_settings / runtime_state / outbox / credited_ledger / daily_usage / event_logs
-- 🔴 该文件由 migrations.py 在 schema_version=0 时整体执行，必须保持可重复执行（IF NOT EXISTS）。

-- ---------------------------------------------------------------- 键值设置
CREATE TABLE IF NOT EXISTS local_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------- 运行时状态（单行，id 恒为 1）
CREATE TABLE IF NOT EXISTS runtime_state (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    last_effective_date     TEXT,
    last_state              TEXT    NOT NULL DEFAULT 'ACTIVE',
    state_changed_at        TEXT,
    grace_until             TEXT,
    break_end_at            TEXT,
    continuous_use_seconds  REAL    NOT NULL DEFAULT 0,
    fired_reminders_json    TEXT    NOT NULL DEFAULT '[]',
    parent_fail_count       INTEGER NOT NULL DEFAULT 0,
    parent_cooldown_until   TEXT,
    last_flush_at           TEXT,
    last_sync_at            TEXT,
    last_sync_ok            INTEGER NOT NULL DEFAULT 0,
    clean_shutdown          INTEGER NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------- 发件箱
CREATE TABLE IF NOT EXISTS outbox (
    id            TEXT PRIMARY KEY,
    kind          TEXT    NOT NULL,
    payload_json  TEXT    NOT NULL DEFAULT '{}',
    status        TEXT    NOT NULL DEFAULT 'pending',
    retry_count   INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error    TEXT,
    created_at    TEXT    NOT NULL,
    sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_status_kind ON outbox (status, kind);
CREATE INDEX IF NOT EXISTS ix_outbox_next_retry ON outbox (next_retry_at);

-- ---------------------------------------------------------------- 额度入账台账（幂等核心）
CREATE TABLE IF NOT EXISTS credited_ledger (
    request_id   TEXT PRIMARY KEY,
    minutes      INTEGER NOT NULL,
    target_date  TEXT    NOT NULL,
    credited_at  TEXT    NOT NULL,
    confirmed_at TEXT
);

-- ---------------------------------------------------------------- 每日用量
CREATE TABLE IF NOT EXISTS daily_usage (
    usage_date         TEXT PRIMARY KEY,
    used_seconds       REAL    NOT NULL DEFAULT 0,
    bonus_minutes      INTEGER NOT NULL DEFAULT 0,
    parent_seconds     REAL    NOT NULL DEFAULT 0,
    break_count        INTEGER NOT NULL DEFAULT 0,
    base_quota_minutes INTEGER NOT NULL DEFAULT 0,
    synced_at          TEXT,
    updated_at         TEXT    NOT NULL
);

-- ---------------------------------------------------------------- 本地事件流水
CREATE TABLE IF NOT EXISTS event_logs (
    id           TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'info',
    payload_json TEXT NOT NULL DEFAULT '{}',
    occurred_at  TEXT NOT NULL,
    uploaded_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_logs_uploaded ON event_logs (uploaded_at);
CREATE INDEX IF NOT EXISTS ix_event_logs_occurred ON event_logs (occurred_at);
