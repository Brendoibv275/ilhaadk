-- G — Histórico de estágios do lead (tempo em cada estágio).
-- Cada transição fecha a linha aberta (exited_at = now()) e insere nova linha (entered_at = now()).

CREATE TABLE IF NOT EXISTS lead_stage_history (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id     UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    entered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    exited_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS lead_stage_history_lead_idx
    ON lead_stage_history (lead_id, entered_at DESC);

-- Idx auxiliar para achar linhas abertas rápido
CREATE INDEX IF NOT EXISTS lead_stage_history_open_idx
    ON lead_stage_history (lead_id)
    WHERE exited_at IS NULL;
