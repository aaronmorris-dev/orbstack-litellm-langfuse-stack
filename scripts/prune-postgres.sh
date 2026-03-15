#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../" && pwd)"
SQL_FILE="$ROOT_DIR/scripts/prune-postgres.sql"

# Load credentials from .env (same source as docker-compose)
if [[ -f "$ROOT_DIR/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$ROOT_DIR/.env"; set +a
fi

echo "Pruning PostgreSQL (LiteLLM spend/error logs)..."
docker exec -i gateway-postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f - < "$SQL_FILE"

echo "Pruning ClickHouse (Langfuse traces/observations)..."
docker exec -i gateway-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER:-clickhouse}" --password "${CLICKHOUSE_PASSWORD:-clickhouse}" --multiquery <<'SQL'
ALTER TABLE traces DELETE WHERE created_at < now() - INTERVAL 30 DAY;
ALTER TABLE observations DELETE WHERE created_at < now() - INTERVAL 30 DAY;
ALTER TABLE scores DELETE WHERE created_at < now() - INTERVAL 60 DAY;
ALTER TABLE event_log DELETE WHERE created_at < now() - INTERVAL 14 DAY;
SQL

echo "ClickHouse mutations submitted (async — complete in background)."
echo "Prune completed: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
