#!/usr/bin/env bash
# MailFilter — zpracování fronty manuálních runů pravidel + obnova složek (host cron, každou minutu).
# Web (kontejner) zapisuje:
#   - run requesty  do state/mailfilter-run-requests/   → zpracuje mailfilter-run-rule.py (himalaya)
#   - folder refresh do state/mailfilter-folder-refresh/ → spustí mailfilter-folders.sh (nový dump)
set -euo pipefail

ROOT="/root/.openclaw/workspace"
REQ_DIR="$ROOT/state/mailfilter-run-requests"
RES_DIR="$ROOT/state/mailfilter-run-results"
FR_DIR="$ROOT/state/mailfilter-folder-refresh"
LOG="$ROOT/logs/mailfilter-run-requests.log"
mkdir -p "$REQ_DIR" "$RES_DIR" "$FR_DIR" "$ROOT/logs"

exec 9>"$RES_DIR/.lock"
if ! /usr/bin/flock -n 9; then
  exit 0
fi

# ── manuální obnova seznamu složek (t, 2026-08-23) ────────────────
if compgen -G "$FR_DIR/refresh-*.json" > /dev/null 2>&1; then
  echo "$(date -Is) [mailfilter-run] folder refresh requested" >> "$LOG"
  if ! "$ROOT/bin/mailfilter-folders.sh" >> "$LOG" 2>&1; then
    echo "$(date -Is) [mailfilter-run] folder refresh FAILED" >> "$LOG"
  fi
  rm -f "$FR_DIR"/refresh-*.json
fi

# ── manuální runy pravidel ─────────────────────────────────────────
for req in "$REQ_DIR"/run-*.json; do
  [ -e "$req" ] || continue
  base="$(basename "$req" .json)"
  # označ jako zpracovávaný, ať web vidí 'running'
  if ! mv "$req" "$REQ_DIR/$base.processing" 2>/dev/null; then
    continue
  fi
  echo "$(date -Is) [mailfilter-run] processing $base" >> "$LOG"
  if ! timeout 1800 python3 "$ROOT/bin/mailfilter-run-rule.py" "$REQ_DIR/$base.processing" >> "$LOG" 2>&1; then
    echo "$(date -Is) [mailfilter-run] FAILED $base (rc=$?)" >> "$LOG"
    # výsledek s chybou, aby frontend viděl stav
    python3 - "$base" <<'PY' >> "$LOG" 2>&1 || true
import json, sys
from datetime import datetime, timezone
base = sys.argv[1]
res = {
    'rule_id': int(base.split('-')[1]),
    'requested_at': None,
    'started_at': datetime.now(timezone.utc).isoformat(),
    'status': 'error',
    'matched': 0, 'moved': 0, 'labels_applied': 0,
    'errors': ['worker failed (timeout/exception)'],
    'finished_at': datetime.now(timezone.utc).isoformat(),
}
out = f"/root/.openclaw/workspace/state/mailfilter-run-results/{base}.json"
json.dump(res, open(out, 'w'), ensure_ascii=False, indent=1)
PY
  fi
  rm -f "$REQ_DIR/$base.processing"
done

# ── kompletní zpracování INBOX podle všech pravidel (t, 2026-09-01) ──
INBOX_REQ_DIR="$ROOT/state/mailfilter-inbox-requests"
INBOX_RES_DIR="$ROOT/state/mailfilter-inbox-results"
mkdir -p "$INBOX_REQ_DIR" "$INBOX_RES_DIR"
for req in "$INBOX_REQ_DIR"/inbox-*.json; do
  [ -e "$req" ] || continue
  base="$(basename "$req" .json)"
  if ! mv "$req" "$INBOX_REQ_DIR/$base.processing" 2>/dev/null; then
    continue
  fi
  echo "$(date -Is) [mailfilter-inbox] processing $base" >> "$LOG"
  STARTED="$(date -u +%Y-%m-%dT%H:%M:%S%z)"
  if timeout 1800 python3 "$ROOT/bin/triage_bezouska_mail.py" >> "$LOG" 2>&1; then
    RC=0
  else
    RC=$?
    echo "$(date -Is) [mailfilter-inbox] FAILED $base (rc=$RC)" >> "$LOG"
  fi
  # poslední stavový řádek z triage logu (JSON summary) → výsledek pro frontend
  LAST_LINE="$(tail -n 200 "$ROOT/logs/bezouska-inbox-triage.log" | grep -E '^\{"status"' | tail -1 || true)"
  python3 - "$base" "$RC" "$STARTED" "$LAST_LINE" <<'PY' >> "$LOG" 2>&1 || true
import json, sys
base, rc, started = sys.argv[1], sys.argv[2], sys.argv[3]
last_line = sys.argv[4] if len(sys.argv) > 4 else ''
res = {
    'requested_at': None,
    'started_at': started,
    'status': 'done' if rc == '0' else 'error',
    'errors': [] if rc == '0' else ['triage worker failed (rc=' + rc + ')'],
    'finished_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
}
try:
    if last_line:
        d = json.loads(last_line)
        res['summary'] = {
            'status': d.get('status'),
            'processed_count': d.get('processed_count'),
            'llm_candidate_count': d.get('llm_candidate_count'),
            'enqueued_count': d.get('enqueued_count'),
            'errors_count': d.get('errors'),
            'needs_llm': d.get('llm_needed'),
        }
        if d.get('errors'):
            res['errors'] = [f'{e.get("id")}: {e.get("error", "")}' for e in d.get('errors', [])][:5]
except Exception:
    pass
out = f"/root/.openclaw/workspace/state/mailfilter-inbox-results/{base}.json"
json.dump(res, open(out, 'w'), ensure_ascii=False, indent=1)
PY
  rm -f "$INBOX_REQ_DIR/$base.processing"
done
