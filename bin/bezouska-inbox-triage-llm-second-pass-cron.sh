#!/usr/bin/env bash
set -euo pipefail

ROOT="/root/.openclaw/workspace"

# GO LIVE (t 2026-09-18): reálné přesuny + model pass.
#   MAILFILTER_APPLY=1        → mailbox se MĚNÍ (přesuny do cílových složek).
#   MAILFILTER_LLM_ENABLED=1  → second pass volá levný model (deepseek/deepseek-chat)
#                               přes lokální LiteLLM router. Ceny jsou hluboko
#                               pod centem na běh; stropy drží runaway.
# Vypnutí: MAILFILTER_APPLY=0 a/nebo MAILFILTER_LLM_ENABLED=0 (safe režim FÁZE 1).
export MAILFILTER_APPLY="${MAILFILTER_APPLY:-1}"
export MAILFILTER_LLM_ENABLED="${MAILFILTER_LLM_ENABLED:-1}"

# Progresivní stropy (t 2026-09-18: „na začátku větší využití, postupně klesá").
# 1 LLM call = až MAILFILTER_LLM_BATCH (20) mailů. 5×20 = 100 mailů/běh,
# 2 běhy/hod → ~200 mailů/hod → počáteční nápor (~540) se vyřeší za ~3 h,
# pak využití samo klesne (fronta se vyprázdní). Až nápor pomine, stropy snížit.
export MAILFILTER_MAX_LLM_CALLS="${MAILFILTER_MAX_LLM_CALLS:-5}"
export MAILFILTER_MAX_MESSAGES="${MAILFILTER_MAX_MESSAGES:-100}"
export MAILFILTER_LLM_MIN_CONF="${MAILFILTER_LLM_MIN_CONF:-0.6}"

"$ROOT/bin/cron-exec.sh" \
  "bezouska-inbox-triage-llm-second-pass" \
  "10m" \
  "/tmp/bezouska-inbox-triage-llm-second-pass.lock" \
  "$ROOT" \
  "python3 $ROOT/bin/bezouska_llm_second_pass_worker.py"
