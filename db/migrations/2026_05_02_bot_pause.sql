-- F — Pausa de bot (automática + manual)
-- Idempotente: pode rodar várias vezes sem erro.
--
-- Observação: as colunas `bot_paused`, `bot_paused_at` e `bot_paused_reason`
-- já foram introduzidas na migration 2026_05_01_kauan_upgrades_2.sql e no
-- db/schema.sql. Este arquivo consolida o contrato mínimo exigido pelo
-- endpoint POST /leads/{id}/pause-bot|resume-bot e pelo frontend, garantindo
-- também o índice parcial para leads atualmente pausados.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS bot_paused BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS bot_paused_reason TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS bot_paused_at TIMESTAMPTZ;

-- Campos auxiliares já existentes (mantidos por idempotência).
ALTER TABLE leads ADD COLUMN IF NOT EXISTS bot_paused_by TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS bot_reactivated_at TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS bot_reactivated_by TEXT;

-- Índice parcial para lookups rápidos de "quem está pausado agora".
CREATE INDEX IF NOT EXISTS leads_bot_paused_idx
    ON leads (bot_paused)
    WHERE bot_paused = true;
