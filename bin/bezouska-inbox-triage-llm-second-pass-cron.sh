#!/usr/bin/env bash
set -euo pipefail

ROOT="/root/.openclaw/workspace"

# FÁZE 1 (bezpečný režim, t 2026-09-18):
#   MAILFILTER_APPLY=0  → mailbox se NEMĚNÍ; rozhodnutí se ukládají jako
#                         'pending_apply' a aplikují se až po přepnutí na 1.
#   MAILFILTER_LLM_ENABLED=0 → cron NEPÁLÍ kredity. LLM je implementovaný
#                         (MAILF-001) a ověřený, ale zapne se až na pokyn t.
# Pro zapnutí: MAILFILTER_APPLY=1 (přesuny) a/nebo MAILFILTER_LLM_ENABLED=1 (LLM).
export MAILFILTER_APPLY="${MAILFILTER_APPLY:-0}"
export MAILFILTER_LLM_ENABLED="${MAILFILTER_LLM_ENABLED:-0}"
# Stropy proti nekontrolovanému pálení kreditů (platí jen když je LLM zapnutý).
export MAILFILTER_MAX_LLM_CALLS="${MAILFILTER_MAX_LLM_CALLS:-2}"
export MAILFILTER_MAX_MESSAGES="${MAILFILTER_MAX_MESSAGES:-20}"

"$ROOT/bin/cron-exec.sh" \
  "bezouska-inbox-triage-llm-second-pass" \
  "10m" \
  "/tmp/bezouska-inbox-triage-llm-second-pass.lock" \
  "$ROOT" \
  "python3 $ROOT/bin/bezouska_llm_second_pass_worker.py"
