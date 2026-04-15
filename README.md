# SDR Ilha Ar (pré-vendas)

Amostra alinhada ao plano **1 agente LLM robusto + Postgres + automações em workers**: triagem e qualificação no ADK; agenda, follow-up, NPS e notificações internas via fila `automation_jobs` e cron.

## Componentes

| Peça | Descrição |
|------|-----------|
| [`agent.py`](agent.py) | Exporta `root_agent` para `adk run` / deploy. |
| [`sdr_ilha_ar/llm_app.py`](sdr_ilha_ar/llm_app.py) | `Agent` com instruções em PT-BR e tools (default: `gemini-3.1-flash-lite-preview`). |
| [`sdr_ilha_ar/tools_impl.py`](sdr_ilha_ar/tools_impl.py) | `get_pricing_quote`, `save_lead_field`, `set_lead_stage`, `enqueue_automation_job`, `request_human_handoff`, `mark_quote_sent`, `register_appointment_request`. |
| [`db/schema.sql`](db/schema.sql) | Tabelas `leads`, `messages`, `appointments`, `automation_jobs`, `outbox_events` + idempotência em `idempotency_key`. |
| [`sdr_ilha_ar/workers/processor.py`](sdr_ilha_ar/workers/processor.py) | Processa jobs: `notify_internal`, `send_followup`, `nps`, `check_calendar` (stub). |
| [`sdr_ilha_ar/channel.py`](sdr_ilha_ar/channel.py) | Adaptador inbound: texto e webhook Evolution (texto/áudio), com transcrição para fluxo do agente. |

## Pré-requisitos

- Python 3.10+
- Conta/credenciais Google GenAI conforme ADK ([documentação ADK](https://google.github.io/adk-docs/))
- Docker (opcional) para Postgres local

## Setup rápido

```bash
cd python/agents/sdr-ilha-ar
cp .env.example .env
# Edite .env: DATABASE_URL, GOOGLE_API_KEY / Vertex, opcional TELEGRAM_*
# SDR_MODEL=gemini-3.1-flash-lite-preview
# SDR_AUDIO_TRANSCRIBE_MODEL=gemini-3.1-flash-lite-preview
# EVOLUTION_BASE_URL=http://localhost:8080
# EVOLUTION_API_KEY=seu_token
# EVOLUTION_INSTANCE=sua_instancia
# EVOLUTION_WEBHOOK_SECRET=segredo_opcional
# PUBLIC_WEBHOOK_URL=https://seu-subdominio.ngrok.app
# DB_CONNECT_TIMEOUT_SECONDS=5
# DB_CONNECT_RETRIES=2
# DB_RETRY_BACKOFF_SECONDS=0.75

docker compose up -d
psql "postgresql://sdr:sdr@127.0.0.1:5433/sdr" -f db/schema.sql

python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -e ".[dev]"
pytest tests/
```

## Chat local (ADK)

```bash
adk run .
# ou: adk web .  (conforme sua instalação ADK)
```

Sem `DATABASE_URL`, as tools de persistência retornam erro orientando a configurar o Postgres.
Se o banco estiver temporariamente indisponível, o agente devolve fallback amigável e não encerra a rodada com erro técnico cru.

## Workers (cron)

Processe a fila a cada 1–5 minutos (um único processo por ambiente):

```bash
set DATABASE_URL=postgresql://sdr:sdr@127.0.0.1:5433/sdr
python -m sdr_ilha_ar.workers tick
# ou, após pip install -e .:
sdr-workers tick
```

### Notificação interna (MVP)

Defina `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`. Jobs `notify_internal` enviam texto formatado (ex.: após `register_appointment_request` ou `request_human_handoff`).

### Fase 2 (plano)

- **`check_calendar`**: hoje só registra stub em `messages`; substituir por Google Calendar API.
- **`nps`**: worker já monta mensagem e usa `GOOGLE_REVIEW_URL` quando definido; conectar envio ao mesmo canal WhatsApp do cliente.

## Adaptador de canal (WhatsApp / HTTP)

1. Webhook HTTP recebe o payload do provedor.
2. Opcional: `parse_meta_whatsapp_example(body)` como ponto de partida.
3. `await handle_inbound_text(external_user_id=wa_id, text=..., external_channel="whatsapp")`.
4. Resposta ao cliente: use o retorno string no envelope HTTP do provedor (`build_http_response_envelope`).

Produção: troque `InMemoryRunner` por `Runner` + serviço de sessão persistente e carregue histórico de `messages` no Postgres se necessário.

## Evolution API inbound (texto + áudio)

O módulo [`sdr_ilha_ar/channel.py`](sdr_ilha_ar/channel.py) já expõe utilitários para webhook inbound:

- `parse_evolution_inbound(body)` para extrair usuário/canal/texto/áudio.
- `handle_evolution_inbound(body)` para processar payload completo e retornar envelope com `reply`.
- `remoteJid` é obrigatório e vira o identificador do cliente por telefone.
- O telefone é normalizado e salvo sem `+55` (a partir do DDD), por exemplo `98999998888`.
- Quando chegar áudio (`audioMessage.url` ou base64), o fluxo tenta transcrever com `SDR_AUDIO_TRANSCRIBE_MODEL` e segue o atendimento normalmente.
- Em falha de transcrição, retorna mensagem curta pedindo reenvio em áudio/texto.
- Quando disponível, o pré-nome da Evolution (`pushName`/`senderName`) é salvo automaticamente em `display_name` apenas se ainda estiver vazio.

Campos mínimos esperados no payload (com variações aceitas):

- `data.key.remoteJid` (id externo do usuário)
- `data.message.conversation` ou `data.message.extendedTextMessage.text` (texto)
- `data.message.audioMessage.url` e/ou `data.message.audioMessage.base64` (áudio)
- `data.message.audioMessage.mimetype` (opcional; default `audio/ogg`)

### Teste local com ngrok + Evolution

1. Suba a API local:
   - `python -m uvicorn sdr_ilha_ar.webhook_api:app --host 0.0.0.0 --port 8000 --reload`
2. Rode o ngrok na mesma porta:
   - `ngrok http 8000`
3. Configure `PUBLIC_WEBHOOK_URL` no `.env` com a URL https do ngrok.
4. Na Evolution, configure o webhook inbound para `${PUBLIC_WEBHOOK_URL}/webhook/whatsapp`.
5. (Opcional) Se definir `EVOLUTION_WEBHOOK_SECRET`, envie o header `x-webhook-secret` com o mesmo valor na Evolution.
6. Teste rápido de saúde:
   - `GET ${PUBLIC_WEBHOOK_URL}/health`

## Dashboard Web (Next.js)

O projeto inclui um painel visual em `dashboard-web/` para operação e financeiro.

### Subir backend (FastAPI)

```bash
python -m uvicorn sdr_ilha_ar.webhook_api:app --host 0.0.0.0 --port 8000 --reload
```

### Subir frontend (Next.js)

```bash
cd dashboard-web
# Opcional: para dev local com backend fora do docker
# set BACKEND_INTERNAL_URL=http://127.0.0.1:8000
npm run dev
```

Abra `http://localhost:3000`.

### Endpoints principais do dashboard

- `GET /api/dashboard/overview`
- `GET /api/dashboard/funnel`
- `GET /api/dashboard/appointments`
- `GET /api/dashboard/jobs`
- `GET /api/dashboard/callbacks`
- `GET /api/dashboard/messages`
- `GET /api/dashboard/finance/summary`
- `GET /api/dashboard/finance/entries`
- `POST /api/dashboard/finance/entries`

## Deploy local com Docker (front + back + banco)

O `docker-compose.yml` agora sobe três serviços:

- `postgres` (porta `5433` local -> `5432` container)
- `api` FastAPI (porta `8000`)
- `dashboard-web` Next.js (porta `3000`)

```bash
cd python/agents/sdr-ilha-ar
docker compose up --build -d
```

URLs:

- Dashboard: `http://localhost:3000`
- API health: `http://localhost:8000/health`

No modo Docker, o front usa proxy interno (`/api/dashboard/*`) e conversa com o backend via `BACKEND_INTERNAL_URL=http://api:8000`, sem depender de `NEXT_PUBLIC_API_BASE_URL` no browser.

## MVP fechado (escopo)

- SDR com tools + Postgres.
- Notificação interna via Telegram quando configurado.
- Follow-up agendado por `mark_quote_sent` (4h) e processado pelo worker.
- Agenda real e NPS outbound no WhatsApp: **fora do MVP** (stubs e README).

## Licença

Apache 2.0
