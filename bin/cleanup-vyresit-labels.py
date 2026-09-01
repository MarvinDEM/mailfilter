#!/usr/bin/env python3
"""Odstraní automatický label vyresit z emailů v INBOX (t, 2026-09-01).

Pravidlo: label vyresit je vyhrazený pro emaily, kterým ho pravidlo přiřadí
explicitně. Emaily v INBOX, které ho dostaly automaticky (no rule match), ho
mají ztratit — ostatní labely zůstávají.

Postup:
  1. gluon DB: message_id v INBOX ∩ Labels/vyresit
  2. pro každý email spustit deterministic_classify (stejná logika jako triage)
  3. pokud proton_labels obsahuje 'vyresit' → NECHAT (explicitní pravidlo)
  4. jinak → odebrat vyresit, keep = ostatní aktuální labely
  5. batch přes imap-label-sync-batch.py

Použití: python3 bin/cleanup-vyresit-labels.py [--dry-run]
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path('/root/.openclaw/workspace')
sys.path.insert(0, str(ROOT / 'bin'))

GLUON_DB = '/home/protonbridge/.local/share/protonmail/bridge-v3/gluon/backend/db/f02b04c0-29bd-4d4e-9f70-b4e216bb1c55.db'
VYRESIT_MBOX = 66   # Labels/vyresit
INBOX_MBOX = 205    # INBOX

DRY_RUN = '--dry-run' in sys.argv


def main():
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    vyresit = {r[0] for r in db.execute(f'SELECT message_id FROM mailbox_message_{VYRESIT_MBOX}')}
    inbox = {r[0] for r in db.execute(f'SELECT message_id FROM mailbox_message_{INBOX_MBOX}')}
    both = sorted(vyresit & inbox)
    print(f'Labels/vyresit: {len(vyresit)}, INBOX: {len(inbox)}, průnik: {len(both)}')

    # mailbox id -> název (pro keep labely)
    mailboxes = {r[0]: r[1] for r in db.execute('SELECT id, name FROM mailboxes_v2')}

    # import triage klasifikace
    import triage_bezouska_mail as triage
    conn = sqlite3.connect(ROOT / 'state' / 'bezouska-llm-queue.sqlite3')
    conn.row_factory = sqlite3.Row

    keep_items = []      # pro batch: {src, uid, labels}
    ids_to_remove = []   # message_id k odebrání (rychlá verze)
    keep_count = 0       # vyresit zůstává (explicitní pravidlo)
    remove_count = 0     # vyresit se odebírá

    # message_id -> UID v INBOX
    uid_in_inbox = {}
    for uid, mid in db.execute(f'SELECT uid, message_id FROM mailbox_message_{INBOX_MBOX}'):
        uid_in_inbox[mid] = uid

    # envelope (subject/sender) z messages_v2
    env_by_id = {r[0]: r[1] for r in db.execute('SELECT id, envelope FROM messages_v2')}

    # aktuální labely každé zprávy (přes gluon, jako batch skript)
    def labels_of(mid):
        out = set()
        for mbox_id, name in mailboxes.items():
            if not name.startswith('Labels/'):
                continue
            try:
                if db.execute(f'SELECT 1 FROM mailbox_message_{mbox_id} WHERE message_id=? AND deleted=0 LIMIT 1', (mid,)).fetchone():
                    out.add(name)
            except sqlite3.OperationalError:
                continue
        return out

    import re
    for mid in both:
        env = env_by_id.get(mid) or ''
        m = re.findall(r'"([^"]*)"', env)
        subject = m[1] if len(m) > 1 else ''
        # from: první adresa v první skupině závorek
        fm = re.search(r'\(\([^)]*"[^"]*"[^)]*\)\)', env)
        sender = ''
        if fm:
            parts = re.findall(r'"([^"]*)"', fm.group(0))
            sender = parts[-1] if parts else ''

        msg = {'id': mid, 'subject': subject, 'from': {'addr': sender}}
        decision = triage.deterministic_classify(conn, msg)
        proton = decision.get('proton_labels') or []
        current = labels_of(mid)
        other_labels = {l for l in current if l != 'Labels/vyresit'}

        if 'vyresit' in proton:
            keep_count += 1
            continue

        uid = uid_in_inbox.get(mid)
        if uid is None:
            print(f'  SKIP (no uid in INBOX): {mid[:12]} {subject[:40]}')
            continue
        keep_items.append({
            'src': 'INBOX',
            'uid': int(uid),
            'labels': sorted(l.replace('Labels/', '') for l in other_labels),
        })
        ids_to_remove.append(mid)
        remove_count += 1

    print(f'vyresit zůstává (explicitní pravidlo): {keep_count}')
    print(f'vyresit se odebírá: {remove_count}')

    if DRY_RUN or not keep_items:
        print('DRY RUN — nic se nemění' if DRY_RUN else 'nic k odebrání')
        return 0

    # message_id k odebrání (pro rychlou dávkovou verzi)
    # keep_items obsahuje {src, uid, labels}; potřebujeme message_id — dotaz z gluon
    ids_file = ROOT / 'tmp' / 'vyresit-cleanup-ids.json'
    ids_file.parent.mkdir(parents=True, exist_ok=True)
    ids_file.write_text(json.dumps(ids_to_remove, ensure_ascii=False))
    print(f'{len(ids_to_remove)} message_id → {ids_file}')
    r = subprocess.run(['python3', str(ROOT / 'bin' / 'cleanup-vyresit-fast.py'), str(ids_file)],
                       capture_output=True, text=True)
    print(r.stdout[-2000:])
    print(r.stderr[-1000:] if r.stderr else '')
    return r.returncode


if __name__ == '__main__':
    sys.exit(main())
