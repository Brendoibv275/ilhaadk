-- Migration: kauan_upgrades_2 (FIX-MAPS + F2+A4)
-- Data: 2026-05-01
-- Idempotente (pode rodar múltiplas vezes sem quebrar).
--
-- FIX-MAPS: lat/lng como fonte da verdade de localização. O cliente manda o pin
--           do WhatsApp (tipo location) e armazenamos as coordenadas exatas para
--           garantir que o técnico chegue no endereço certo.
--
-- F2+A4: engine de agendamento com 4 slots/dia por equipe.
--        Slots: morning_early (8-10h), morning_late (10-12h),
--               afternoon_early (14-16h), afternoon_late (16-18h).
--        Status novo: pending_team_assignment (humano atribui equipe via frontend).

BEGIN;

-- FIX-MAPS --------------------------------------------------------------------
ALTER TABLE leads ADD COLUMN IF NOT EXISTS latitude  NUMERIC(10, 7);
ALTER TABLE leads ADD COLUMN IF NOT EXISTS longitude NUMERIC(10, 7);

-- F2+A4 -----------------------------------------------------------------------
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS scheduled_date DATE;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS slot           TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS team_id        TEXT;

CREATE INDEX IF NOT EXISTS appointments_date_slot_idx
    ON appointments (scheduled_date, slot)
    WHERE scheduled_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS appointments_status_idx ON appointments (status);

COMMIT;
