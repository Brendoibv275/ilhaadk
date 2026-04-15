-- SDR Ilha Ar: fonte da verdade para leads, mensagens, agendamentos e fila de automações.
-- Execute com: psql $DATABASE_URL -f db/schema.sql

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Estágios: new, qualified, quoted, awaiting_slot, scheduled, completed, lost, emergency_handoff
CREATE TABLE IF NOT EXISTS leads (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_channel    TEXT NOT NULL DEFAULT 'whatsapp',
    external_user_id    TEXT NOT NULL,
    phone               TEXT,
    display_name        TEXT,
    address             TEXT,
    preferred_window    TEXT,
    service_type        TEXT,
    btus                INTEGER,
    floor_level         INTEGER,
    tubing_complex      TEXT,
    quoted_amount       NUMERIC(12, 2),
    quote_notes         TEXT,
    stage               TEXT NOT NULL DEFAULT 'new',
    last_inbound_at     TIMESTAMPTZ,
    last_outbound_at  TIMESTAMPTZ,
    quote_sent_at       TIMESTAMPTZ,
    visit_done_at       TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT leads_channel_user_unique UNIQUE (external_channel, external_user_id)
);

CREATE INDEX IF NOT EXISTS leads_stage_idx ON leads (stage);
CREATE INDEX IF NOT EXISTS leads_quote_sent_idx ON leads (quote_sent_at) WHERE quote_sent_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id     UUID NOT NULL REFERENCES leads (id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    body        TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_lead_created_idx ON messages (lead_id, created_at);

CREATE TABLE IF NOT EXISTS appointments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id             UUID NOT NULL REFERENCES leads (id) ON DELETE CASCADE,
    window_label        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'proposed',
    calendar_event_id   TEXT,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS appointments_lead_idx ON appointments (lead_id);

-- Fila de automações: idempotency_key UNIQUE evita WhatsApp/Telegram duplicado ao reprocessar.
CREATE TABLE IF NOT EXISTS automation_jobs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id           UUID NOT NULL REFERENCES leads (id) ON DELETE CASCADE,
    job_type          TEXT NOT NULL,
    run_at            TIMESTAMPTZ NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    payload           JSONB NOT NULL DEFAULT '{}',
    idempotency_key   TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT automation_jobs_idempotency_unique UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS automation_jobs_pending_run_idx
    ON automation_jobs (status, run_at)
    WHERE status = 'pending';

-- Eventos opcionais para auditoria / integrações externas (CRM, webhooks).
CREATE TABLE IF NOT EXISTS outbox_events (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id       UUID REFERENCES leads (id) ON DELETE SET NULL,
    event_type    TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS outbox_events_unprocessed_idx
    ON outbox_events (created_at)
    WHERE processed_at IS NULL;

-- Financeiro dedicado (dashboard)
CREATE TABLE IF NOT EXISTS finance_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         UUID REFERENCES leads (id) ON DELETE SET NULL,
    appointment_id  UUID REFERENCES appointments (id) ON DELETE SET NULL,
    entry_type      TEXT NOT NULL, -- income | expense
    category        TEXT NOT NULL,
    description     TEXT,
    amount          NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    due_date        DATE,
    paid_at         TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | paid | cancelled
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS finance_entries_status_idx ON finance_entries (status, entry_type);
CREATE INDEX IF NOT EXISTS finance_entries_due_idx ON finance_entries (due_date);

CREATE OR REPLACE FUNCTION trg_leads_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS leads_updated_at ON leads;
CREATE TRIGGER leads_updated_at
    BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE PROCEDURE trg_leads_set_updated_at();

DROP TRIGGER IF EXISTS appointments_updated_at ON appointments;
CREATE TRIGGER appointments_updated_at
    BEFORE UPDATE ON appointments
    FOR EACH ROW EXECUTE PROCEDURE trg_leads_set_updated_at();

DROP TRIGGER IF EXISTS automation_jobs_updated_at ON automation_jobs;
CREATE TRIGGER automation_jobs_updated_at
    BEFORE UPDATE ON automation_jobs
    FOR EACH ROW EXECUTE PROCEDURE trg_leads_set_updated_at();

DROP TRIGGER IF EXISTS finance_entries_updated_at ON finance_entries;
CREATE TRIGGER finance_entries_updated_at
    BEFORE UPDATE ON finance_entries
    FOR EACH ROW EXECUTE PROCEDURE trg_leads_set_updated_at();
