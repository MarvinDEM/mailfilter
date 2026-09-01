#!/usr/bin/env bash
set -euo pipefail

ROOT="/root/.openclaw/workspace"
"$ROOT/bin/cron-exec.sh" \
  "bezouska-inbox-triage" \
  "10m" \
  "/tmp/bezouska-inbox-triage.lock" \
  "$ROOT" \
  "python3 $ROOT/bin/triage_bezouska_mail.py"
