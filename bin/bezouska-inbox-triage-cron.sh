#!/usr/bin/env bash
set -euo pipefail

ROOT="/root/.openclaw/workspace"

# FÁZE 1 (bezpečný režim, t 2026-09-18): MAILFILTER_APPLY=0 → triage jen čte
# IMAP a plní frontu, v mailboxu NIC nepřesouvá ani neoznačuje. Přesuny se
# rozjedou až po explicitním odsouhlasení t (MAILFILTER_APPLY=1).
export MAILFILTER_APPLY="${MAILFILTER_APPLY:-0}"

"$ROOT/bin/cron-exec.sh" \
  "bezouska-inbox-triage" \
  "10m" \
  "/tmp/bezouska-inbox-triage.lock" \
  "$ROOT" \
  "python3 $ROOT/bin/triage_bezouska_mail.py"
