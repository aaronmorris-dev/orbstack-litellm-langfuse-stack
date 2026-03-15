-- Gateway DB pruning policy (safe defaults)
-- Run during low-traffic windows.
-- LiteLLM spend-log data is also managed by maximum_spend_logs_retention_period in config.yaml.
-- Langfuse v3 data (traces, observations, scores) lives in ClickHouse — see prune-postgres.sh.

BEGIN;

-- LiteLLM request-level spend logs (primary growth vector)
DELETE FROM "LiteLLM_SpendLogs"
WHERE "startTime" < NOW() - INTERVAL '30 days';

-- LiteLLM error logs (keep shorter)
DELETE FROM "LiteLLM_ErrorLogs"
WHERE "startTime" < NOW() - INTERVAL '14 days';

COMMIT;

-- Reclaim dead tuples after cleanup.
VACUUM (ANALYZE) "LiteLLM_SpendLogs";
VACUUM (ANALYZE) "LiteLLM_ErrorLogs";
