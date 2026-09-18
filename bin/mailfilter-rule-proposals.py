#!/usr/bin/env python3
"""MAILF-015 — deterministický generátor návrhů pravidel.

Z nezařazených / ručně zařazených mailů hledá opakované vzory sender → složka.
Když stejný sender skončil N× (default 3) ve stejné složce a zatím pro něj
není ŽÁDNÉ pravidlo, založí NÁVRH pravidla ve stavu 'pending' (active=0) —
t ho odsouhlasí ve webu (https://mailfilter.bezouska.cz).

Nezapisuje aktivní pravidla, nemění mailbox. Bez LLM (žádné kredity).

Použití:
    python3 bin/mailfilter-rule-proposals.py [--min N] [--dry-run] [--json]
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_rules

DB_PATH = Path(os.environ.get('MAIL_RULES_DB', '/root/.openclaw/workspace/state/bezouska-llm-queue.sqlite3'))
EXCLUDE_FOLDERS = {None, '', 'INBOX'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--min', type=int, default=3)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1) kolik mailů od daného senderu reálně skončilo v které složce
    pairs = defaultdict(int)
    for row in conn.execute("SELECT sender, decision_json FROM queue WHERE decision_json IS NOT NULL"):
        try:
            dec = json.loads(row['decision_json'] or '{}')
        except Exception:
            continue
        folder = dec.get('folder')
        sender = (row['sender'] or '').lower()
        if not sender or folder in EXCLUDE_FOLDERS:
            continue
        pairs[(sender, folder)] += 1

    # 2) co už pravidlo má (jakéhokoli stavu)
    existing = set()
    for row in conn.execute("SELECT sender, folder FROM learned_rules WHERE subject_pattern IS NULL"):
        existing.add((row['sender'], row['folder']))

    proposals = []
    for (sender, folder), count in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if count < args.min:
            continue
        if (sender, folder) in existing:
            continue
        proposals.append({'sender': sender, 'folder': folder, 'count': count})

    created = []
    if not args.dry_run:
        for p in proposals:
            new_id = mail_rules.propose_rule(
                conn, p['sender'], p['folder'], [],
                source='cluster-detector',
                notes=f"{p['count']}× stejný sender → stejná složka (deterministický clustering)")
            if new_id:
                created.append({'id': new_id, **p})

    result = {
        'pairs_considered': len(pairs),
        'min_occurrences': args.min,
        'proposals': proposals,
        'created': created,
        'dry_run': args.dry_run,
        'pending_total': conn.execute(
            "SELECT COUNT(*) FROM learned_rules WHERE review_status='pending'").fetchone()[0],
    }
    conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Návrhy pravidel (min {args.min}× stejný sender→složka):")
        for p in proposals:
            print(f"  {p['sender']} -> {p['folder']} ({p['count']}×)")
        print(f"Vytvořeno: {len(created)} | pending celkem: {result['pending_total']}")


if __name__ == '__main__':
    main()
