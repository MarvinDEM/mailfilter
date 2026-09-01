#!/usr/bin/env bash
set -euo pipefail

ROOT="/root/.openclaw/workspace"
"$ROOT/bin/cron-exec.sh" \
  "bezouska-learn-from-zatridil-tomas" \
  "20m" \
  "/tmp/bezouska-learn-from-zatridil-tomas.lock" \
  "$ROOT" \
  "python3 $ROOT/bin/learn_from_zatridil_tomas.py"
