-- I/1 — Idempotência de mensagens WhatsApp
-- Guarda provider_message_id (da Evolution/Meta) para detectar webhooks duplicados.
-- Idempotente: pode rodar várias vezes sem erro.

CREATE TABLE IF NOT EXISTS processed_messages (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_message_id     TEXT NOT NULL,
    lead_id                 UUID REFERENCES leads(id) ON DELETE SET NULL,
    received_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at            TIMESTAMPTZ,
    CONSTRAINT processed_messages_provider_id_unique UNIQUE (provider_message_id)
);

CREATE INDEX IF NOT EXISTS processed_messages_received_at_idx
    ON processed_messages (received_at DESC);
