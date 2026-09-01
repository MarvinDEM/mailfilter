#!/usr/bin/env bash
set -euo pipefail

ROOT="/root/.openclaw/workspace"
"$ROOT/bin/cron-exec.sh" \
  "bezouska-inbox-triage-llm-second-pass" \
  "10m" \
  "/tmp/bezouska-inbox-triage-llm-second-pass.lock" \
  "$ROOT" \
  "python3 $ROOT/bin/bezouska_llm_second_pass_worker.py"
