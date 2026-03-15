#!/usr/bin/env bash
# Check and refresh expiring gateway credentials.
# Provider checks require browser interaction — run this manually.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  set -a; source "${SCRIPT_DIR}/.env"; set +a
fi

echo "=== Gateway Credential Check ==="
echo

# --- AWS SSO (Bedrock) ---
if [[ -n "${AWS_PROFILE:-}" ]]; then
  echo "AWS SSO (profile: ${AWS_PROFILE})"
  if aws sts get-caller-identity --profile "${AWS_PROFILE}" &>/dev/null; then
    ACCT=$(aws sts get-caller-identity --profile "${AWS_PROFILE}" --query 'Account' --output text 2>/dev/null)
    ok "Session valid (account: ${ACCT})"
  else
    fail "Session expired"
    echo "    Refreshing..."
    aws sso login --profile "${AWS_PROFILE}"
  fi
  echo
else
  echo "AWS SSO: skipped (AWS_PROFILE not set)"
  echo
fi

# --- GCloud ADC (Vertex AI) ---
if [[ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]] || command -v gcloud &>/dev/null; then
  echo "GCloud ADC"
  if gcloud auth application-default print-access-token &>/dev/null; then
    ok "ADC token valid"
  else
    fail "ADC token expired"
    echo "    Refreshing..."
    gcloud auth application-default login
  fi
  echo
else
  echo "GCloud ADC: skipped (gcloud not installed)"
  echo
fi

echo "=== Done ==="
