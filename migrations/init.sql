-- COP Engine — Initiala tabeller och index
-- Kör med: psql $DATABASE_URL -f migrations/init.sql
-- Idempotent: alla CREATE är IF NOT EXISTS

-- === USERS ===
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    salt        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'viewer',
    full_name   TEXT,
    email       TEXT,
    is_active   BOOLEAN DEFAULT TRUE,
    password_change_required BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

-- === REVOKED TOKENS ===
CREATE TABLE IF NOT EXISTS revoked_tokens (
    token_hash  TEXT PRIMARY KEY,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires ON revoked_tokens(expires_at);

-- === SCHEDULES ===
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    clinic_id   TEXT,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_schedules_schedule_id ON schedules(schedule_id);
CREATE INDEX IF NOT EXISTS idx_schedules_clinic      ON schedules(clinic_id);
CREATE INDEX IF NOT EXISTS idx_schedules_created     ON schedules(created_at DESC);

-- === JOBS ===
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

-- === CLINIC CONFIGS ===
CREATE TABLE IF NOT EXISTS clinic_configs (
    clinic_id   TEXT PRIMARY KEY,
    data        JSONB NOT NULL,
    version     INT DEFAULT 1,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_configs_clinic ON clinic_configs(clinic_id);

-- === ABSENCE CHAINS ===
CREATE TABLE IF NOT EXISTS absence_chains (
    chain_id    TEXT PRIMARY KEY,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chains_created ON absence_chains(created_at DESC);

-- === AUDIT LOG ===
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    action      TEXT NOT NULL,
    user_id     TEXT,
    details     JSONB,
    timestamp   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user      ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action    ON audit_log(action);

-- === SCHEDULE VERSIONS ===
CREATE TABLE IF NOT EXISTS schedule_versions (
    id              BIGSERIAL PRIMARY KEY,
    schedule_id     TEXT NOT NULL,
    version_number  INT NOT NULL,
    data            JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (schedule_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_versions_schedule ON schedule_versions(schedule_id, version_number DESC);

-- === AI RULES ===
CREATE TABLE IF NOT EXISTS ai_rules (
    id          BIGSERIAL PRIMARY KEY,
    clinic_id   TEXT NOT NULL,
    rule_text   TEXT NOT NULL,
    constraint_data JSONB,
    confidence  REAL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_rules_clinic ON ai_rules(clinic_id);

-- === AI CHAT HISTORY ===
CREATE TABLE IF NOT EXISTS ai_chat_history (
    id          BIGSERIAL PRIMARY KEY,
    clinic_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    message     TEXT NOT NULL,
    response    TEXT,
    action      TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_chat_clinic_user ON ai_chat_history(clinic_id, user_id, created_at DESC);

-- === AI PREDICTIONS ===
CREATE TABLE IF NOT EXISTS ai_predictions (
    id          BIGSERIAL PRIMARY KEY,
    clinic_id   TEXT NOT NULL,
    period_key  TEXT NOT NULL,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (clinic_id, period_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_predictions_clinic ON ai_predictions(clinic_id);

-- === NOTIFICATIONS ===
CREATE TABLE IF NOT EXISTS notifications (
    notif_id    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    data        JSONB NOT NULL,
    is_read     BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at DESC);
