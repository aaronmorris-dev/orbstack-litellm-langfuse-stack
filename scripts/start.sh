#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# Validate .env exists
if [[ ! -f .env ]]; then
  echo "Error: .env not found. Copy .env.example and fill in your values:"
  echo "  cp .env.example .env"
  exit 1
fi

# Validate litellm config exists
if [[ ! -f litellm/config.yaml ]]; then
  echo "Error: litellm/config.yaml not found. Copy the example and add your models:"
  echo "  cp litellm/config.example.yaml litellm/config.yaml"
  exit 1
fi

echo "Starting AI Gateway stack..."
docker compose up -d

echo "Stack is running."
echo "  LiteLLM Proxy:   http://localhost:4000"
echo "  LiteLLM Admin:   http://localhost:4000/ui"
echo "  Langfuse:        http://localhost:5002"
echo "  MinIO Console:   http://localhost:9091"
