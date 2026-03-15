# orbstack-litellm-langfuse-stack

Production-ready local AI gateway with full observability. Route all your LLM traffic through a single proxy with automatic tracing, per-tool attribution, and session evaluation.

## What's in the stack

| Service             | Purpose                                       | Port                                                    |
| ------------------- | --------------------------------------------- | ------------------------------------------------------- |
| **Caddy**           | Reverse proxy with SSE streaming              | `localhost:4000` (LiteLLM), `localhost:5002` (Langfuse) |
| **LiteLLM**         | Unified LLM proxy (OpenAI-compatible API)     | —                                                       |
| **Langfuse**        | Observability, tracing, session analytics     | —                                                       |
| **Langfuse Worker** | Background trace processing                   | —                                                       |
| **PostgreSQL 17**   | LiteLLM + Langfuse metadata                   | —                                                       |
| **ClickHouse 26**   | Langfuse trace storage (high-performance)     | —                                                       |
| **Redis 7.4**       | LiteLLM cache + Langfuse job queue            | —                                                       |
| **MinIO**           | S3-compatible object store for Langfuse media | `localhost:9091` (console)                              |

## Features

- **Multi-provider routing** — AWS Bedrock, Google Vertex AI, Gemini API, OpenAI, Anthropic, and more via LiteLLM
- **Per-tool virtual keys** — Create a key per tool (Claude Code, Gemini CLI, etc.) for automatic trace attribution
- **Zero-config trace enrichment** — `langfuse_enrich.py` auto-populates trace names, sessions, and tags from virtual key metadata
- **Daily sessions** — Traces are auto-grouped into `<tool>-YYYY-MM-DD` sessions in Langfuse
- **Session evaluation** — LLM-as-judge script scores sessions on task completion, approach quality, and communication
- **Production hardened** — Memory limits, log rotation, security_opt, graceful shutdown, health checks on all services
- **SSE streaming** — Caddy configured with `flush_interval -1` for zero-latency streaming responses

## Prerequisites

- [OrbStack](https://orbstack.dev/) (or Docker Desktop)
- Provider credentials for at least one LLM provider

## Quick Start

```bash
# Clone
git clone https://github.com/aaronmorris-dev/orbstack-litellm-langfuse-stack.git
cd orbstack-litellm-langfuse-stack

# Configure
cp .env.example .env
cp litellm/config.example.yaml litellm/config.yaml

# Edit .env — set passwords and generate secrets:
#   openssl rand -hex 32  (for SALT, ENCRYPTION_KEY, NEXTAUTH_SECRET)
#   openssl rand -hex 16  (for LITELLM_MASTER_KEY)

# Edit litellm/config.yaml — uncomment your providers and add model names

# Launch
chmod +x scripts/*.sh
./scripts/start.sh
```

## First-Time Setup

After the stack is running:

1. **Create Langfuse account** — Go to `http://localhost:5002`, sign up, create an org and project
2. **Get Langfuse API keys** — Settings → API Keys → Create. Copy the public and secret keys into `.env`
3. **Restart** — `docker compose up -d` to pick up the new Langfuse keys
4. **Create virtual keys** — In LiteLLM Admin (`http://localhost:4000/ui`), go to Virtual Keys and create per-tool keys with descriptive aliases

## Provider Setup

### AWS Bedrock

```bash
# 1. Configure AWS SSO profile
aws configure sso --profile your-profile-name

# 2. Login
aws sso login --profile your-profile-name

# 3. Set in .env
AWS_PROFILE=your-profile-name

# 4. Uncomment in docker-compose.yaml under litellm volumes:
- ~/.aws:/root/.aws    # writable — SSO needs token cache

# 5. Uncomment models in litellm/config.yaml
```

### Google Vertex AI

```bash
# 1. Authenticate
gcloud auth application-default login

# 2. Set in .env
GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json

# 3. Uncomment in docker-compose.yaml under litellm volumes:
- ~/.config/gcloud:/root/.config/gcloud:ro

# 4. Add models to litellm/config.yaml with your project:
#   vertex_project: your-gcp-project
#   vertex_location: us-central1
```

### Gemini API (key-based)

```bash
# 1. Get key from https://aistudio.google.com/apikey
# 2. Set in .env
GEMINI_API_KEY=your-key
```

### OpenAI / Anthropic (direct API)

```bash
# Add to litellm container environment in docker-compose.yaml:
- OPENAI_API_KEY=${OPENAI_API_KEY}
# or
- ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

## Using the Gateway

### Route CLI tools through the proxy

```bash
# Source environment (sets ANTHROPIC_BASE_URL, OPENAI_BASE_URL, etc.)
source scripts/gateway-env.sh

# Now any OpenAI/Anthropic SDK-compatible tool routes through LiteLLM
```

### Per-tool virtual keys

Create virtual keys in LiteLLM Admin UI (`http://localhost:4000/ui`) with:

- **Key Alias**: tool name (e.g., `claude`, `gemini`, `codex`)
- **User ID**: your identifier
- **Metadata**: `{"tags": ["claude"]}` for Langfuse filtering

The `langfuse_enrich.py` hook automatically maps:

- Key alias → trace name
- Key alias + date → session ID (e.g., `claude-2026-03-14`)
- Key tags → Langfuse trace tags

### Check credentials

```bash
./scripts/refresh-credentials.sh
```

## Maintenance

### Prune old data

```bash
# Delete spend logs (30d), error logs (14d), traces (30d), scores (60d)
./scripts/prune-postgres.sh
```

### Evaluate a session

```bash
# Requires: uv (https://docs.astral.sh/uv/)
uv run --script scripts/eval-session.py --today claude
uv run --script scripts/eval-session.py claude-2026-03-14
uv run --script scripts/eval-session.py claude-2026-03-14 --dry-run --verbose
```

### Stop / restart

```bash
docker compose down        # Stop all
docker compose up -d       # Start all
docker compose logs -f     # Follow logs
```

## Architecture

```
┌──────────────┐
│  CLI Tools   │  Claude Code, Gemini CLI, OpenAI SDK, etc.
│  (localhost)  │
└──────┬───────┘
       │ :4000 (OpenAI-compatible API)
┌──────▼───────┐
│    Caddy     │  Reverse proxy (SSE streaming, JSON logging)
└──────┬───────┘
       │
┌──────▼───────┐     ┌─────────────┐
│   LiteLLM    │────▶│   Langfuse   │  Tracing via OTEL
│  (proxy)     │     │  (web + worker)│
└──────┬───────┘     └──────┬───────┘
       │                     │
┌──────▼───────┐     ┌──────▼───────┐     ┌────────┐
│  PostgreSQL  │     │  ClickHouse  │     │  MinIO  │
│  (metadata)  │     │  (traces)    │     │  (S3)   │
└──────────────┘     └──────────────┘     └────────┘
       │                     │                 │
       └─────────┬───────────┘                 │
           ┌─────▼─────┐                       │
           │   Redis    │───────────────────────┘
           │  (cache)   │
           └───────────┘
```

## Endpoints

| URL                        | Service                         |
| -------------------------- | ------------------------------- |
| `http://localhost:4000`    | LiteLLM API (OpenAI-compatible) |
| `http://localhost:4000/ui` | LiteLLM Admin UI                |
| `http://localhost:5002`    | Langfuse Dashboard              |
| `http://localhost:9091`    | MinIO Console                   |

## License

MIT
