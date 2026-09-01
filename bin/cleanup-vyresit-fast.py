#!/usr/bin/env python3
"""Rychlé dávkové odebrání labelu vyresit z Labels/vyresit mailboxu (t, 2026-09-01).

Pomalá varianta (imap-label-sync-batch.py) dělá per-message skeny gluon DB —
na 390 zpráv to trvá hodiny. Tahle verze:
  1. jeden SQL dotaz: UID zpráv (message_id → uid v mailbox_message_66)
  2. IMAP: SELECT Labels/vyresit → UID STORE \\Deleted → UID EXPUNGE
     (odebrání labelu = smazání z label mailboxu; mail zůstává v INBOX)

Vstup: JSON soubor s message_id, které mají label ztratit.
Použití: python3 bin/cleanup-vyresit-fast.py <message_ids.json>
"""
import imaplib
import json
import sqlite3
import sys

sys.path.insert(0, '/root/.openclaw/workspace/bin')
from imap_utils import quote_mailbox, select_mailbox  # noqa: E402

HOST = '127.0.0.1'
PORT = 1143
ACCOUNT = 'tomas@bezouska.cz'
PASS_FILE = '/root/.config/himalaya/tomas-bezouska-bridge.pass'
GLUON_DB = '/home/protonbridge/.local/share/protonmail/bridge-v3/gluon/backend/db/f02b04c0-29bd-4d4e-9f70-b4e216bb1c55.db'
LABEL_MBOX = 66  # Labels/vyresit


def main():
    if len(sys.argv) < 2:
        print('použití: cleanup-vyresit-fast.py <message_ids.json>')
        return 2
    ids = json.load(open(sys.argv[1]))
    if not ids:
        print('nic k odebrání')
        return 0

    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    placeholders = ','.join('?' * len(ids))
    rows = db.execute(
        f'SELECT uid FROM mailbox_message_{LABEL_MBOX} WHERE message_id IN ({placeholders}) AND deleted=0',
        ids).fetchall()
    uids = [r[0] for r in rows]
    print(f'požadováno: {len(ids)}, nalezeno UID v Labels/vyresit: {len(uids)}')

    if not uids:
        return 0

    pw = open(PASS_FILE).read().strip()
    m = imaplib.IMAP4(HOST, PORT, timeout=60)
    m.login(ACCOUNT, pw)
    try:
        typ, _ = select_mailbox(m, 'Labels/vyresit')
        if typ != 'OK':
            print('SELECT Labels/vyresit selhal', file=sys.stderr)
            return 1
        # STORE po dávkách po 100 UID
        removed = 0
        for i in range(0, len(uids), 100):
            chunk = uids[i:i + 100]
            uid_list = ','.join(str(u) for u in chunk)
            typ, resp = m.uid('store', uid_list, '+FLAGS.SILENT', r'(\Deleted)')
            if typ != 'OK':
                print(f'STORE selhal: {resp}', file=sys.stderr)
                continue
            typ, resp = m.uid('expunge', uid_list)
            if typ != 'OK':
                # fallback: plný EXPUNGE
                try:
                    m.expunge()
                except Exception as e:
                    print(f'EXPUNGE fallback selhal: {e}', file=sys.stderr)
            removed += len(chunk)
            print(f'  ... {removed}/{len(uids)}')
        print(f'hotovo: {removed} labelů odebráno')
        return 0
    finally:
        try:
            m.logout()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
