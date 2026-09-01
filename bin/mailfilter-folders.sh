#!/usr/bin/env bash
# MailFilter — dump realných SLOŽEK (Folders/*) bezouska účtu do state/mailfilter-folders.json.
# Labels/* se sem NEpatří — labely se zadávají v poli „Labely“, ne v poli „Složka“ (t, 2026-08-23).
# Čte to web mailfilter (GET /api/folders) místo zastaralého hardcoded seznamu.
set -euo pipefail

ROOT="/root/.openclaw/workspace"
OUT="$ROOT/state/mailfilter-folders.json"
TMP="$ROOT/tmp/mailfilter-folders.json.tmp"
mkdir -p "$ROOT/tmp"

timeout 120 himalaya -c /root/.config/himalaya/config.toml folder list --account bezouska 2>/dev/null \
  | python3 -c '
import sys, json
from datetime import datetime, timezone
folders = []
for line in sys.stdin:
    if not line.startswith("|"):
        continue
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2:
        continue
    name = parts[1]
    if not name or name in ("NAME",):
        continue
    if name.startswith("Folders/"):
        folders.append(name)
folders = sorted(set(folders))
out = {"folders": folders, "generated_at": datetime.now(timezone.utc).isoformat()}
json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
' "$TMP"

mv "$TMP" "$OUT"
echo "mailfilter folders: $(python3 -c "import json; print(len(json.load(open('$OUT'))['folders']))")"
