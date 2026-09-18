#!/usr/bin/env bash
set -euo pipefail

ROOT="/root/.openclaw/workspace"

# GO LIVE (t 2026-09-18): triage REÁLNĚ přesouvá maily, které odpovídají
# POTVRZENÝM pravidlům (review_status='ok', active=1). Ověřeno: v INBOXu žádný
# mail nematchuje potvrzené pravidlo → tento pass je sám o sobě ~no-op; hlavní
# objem řeší modelový second pass.
# Vypnutí: MAILFILTER_APPLY=0.
export MAILFILTER_APPLY="${MAILFILTER_APPLY:-1}"

"$ROOT/bin/cron-exec.sh" \
  "bezouska-inbox-triage" \
  "10m" \
  "/tmp/bezouska-inbox-triage.lock" \
  "$ROOT" \
  "python3 $ROOT/bin/triage_bezouska_mail.py"
