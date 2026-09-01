#!/usr/bin/env python3
"""IMAP label sync helper — nastaví EXAKTNÍ sadu labelů zprávy (t, 2026-08-24).

Chování:
  - chybějící labely ze seznamu PŘIDÁ (sekvenční COPY — UID COPY je v Proton Bridge
    rozbitý, vrací OK bez kopie)
  - labely, které zpráva má a nejsou v seznamu, ODSTRANÍ

Výkon: aktuální labely zprávy se zjistí PŘÍMO z gluon SQLite DB bridge
(mailbox_message_* tabulky × message_id) místo 274× IMAP SEARCH — 274 skenů na
zprávu způsobovalo timeout u pravidel s velkým počtem matchů (incident rule #69,
2026-08-24).

Použití: imap-label-sync.py <src_folder> <uid> <keep_label>...
  - src_folder: složka, kde zpráva je (např. Folders/00_marvin)
  - uid: IMAP UID (= id z himalaya envelope list)
  - keep_label: labely, které mají zůstat (bez prefixu Labels/, např. vyresit)

Exit 0 = vše OK; jinak 1.
"""
import imaplib
import sqlite3
import sys

sys.path.insert(0, '/root/.openclaw/workspace/bin')
from imap_utils import quote_mailbox, select_mailbox  # noqa: E402

HOST = '127.0.0.1'
PORT = 1143
ACCOUNT = 'tomas@bezouska.cz'
PASS_FILE = '/root/.config/himalaya/tomas-bezouska-bridge.pass'
GLUON_DB = '/home/protonbridge/.local/share/protonmail/bridge-v3/gluon/backend/db/f02b04c0-29bd-4d4e-9f70-b4e216bb1c55.db'  # BEZOUSKA gluon store!


def _db_labels_of_message(message_id):
    """Vrátí seznam label složek, ve kterých zpráva je (přes gluon DB)."""
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    try:
        # mailbox id -> nazev
        mailboxes = {r[0]: r[2] for r in db.execute(
            "SELECT id, remote_id, name FROM mailboxes_v2")}
        labels = []
        for mid, name in mailboxes.items():
            if not name.startswith('Labels/'):
                continue
            table = f'mailbox_message_{mid}'
            try:
                row = db.execute(
                    f'SELECT 1 FROM "{table}" WHERE message_id=? AND deleted=0 LIMIT 1',
                    (message_id,)).fetchone()
                if row:
                    labels.append(name)
            except sqlite3.OperationalError:
                continue
        return labels
    finally:
        db.close()


def _db_message_id(src_folder, uid):
    """Najde gluon message_id zprávy (src_folder, uid) přes gluon DB."""
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    try:
        row = db.execute(
            "SELECT id FROM mailboxes_v2 WHERE name=?", (src_folder,)).fetchone()
        if not row:
            return None
        table = f'mailbox_message_{row[0]}'
        r = db.execute(f'SELECT message_id FROM "{table}" WHERE uid=? LIMIT 1', (int(uid),)).fetchone()
        return r[0] if r else None
    finally:
        db.close()


def _uid_to_seq(m, uid):
    typ, data = m.uid('fetch', uid, '(UID)')
    if typ != 'OK' or not data or data[0] is None:
        return None
    part = data[0]
    head = part[0] if isinstance(part, tuple) else part
    if isinstance(head, bytes):
        return head.split(b' ', 1)[0].decode()
    raw = b' '.join(x for x in part if isinstance(x, bytes))
    return raw.split(b' ', 1)[0].decode()


def _copy_seq(m, seq, target):
    return m._simple_command('COPY', f'{seq} {quote_mailbox(target)}')


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src = sys.argv[1]
    uid = sys.argv[2]
    keep = set(sys.argv[3:])

    # 1) aktuální labely zprávy přes gluon DB (rychlé)
    message_id = _db_message_id(src, uid)
    if message_id is None:
        print(f'message {uid} v {src} nenalezen v gluon DB', file=sys.stderr)
        return 1
    current_labels = _db_labels_of_message(message_id)
    keep_full = {f'Labels/{l}' for l in keep}

    to_add = keep_full - set(current_labels)
    to_remove = [l for l in current_labels if l not in keep_full]

    print(f'  message {uid}: {len(current_labels)} labelů, přidat {len(to_add)}, odstranit {len(to_remove)}')

    pw = open(PASS_FILE).read().strip()
    m = imaplib.IMAP4(HOST, PORT, timeout=30)
    m.login(ACCOUNT, pw)

    failures = []
    try:
        # 2) přidej chybějící labely (sekvenční COPY)
        if to_add:
            typ, _ = select_mailbox(m, src)
            if typ != 'OK':
                failures.append(('select src', f'{typ}'))
            seq = _uid_to_seq(m, uid)
            for target in to_add:
                if seq is None:
                    failures.append((target, 'uid->seq fail'))
                    continue
                typ, resp = _copy_seq(m, seq, target)
                if typ != 'OK':
                    failures.append((target, f'copy fail: {resp}'))
                else:
                    print(f'  přidán label {target}')

        # 3) odstraň labely mimo keep (UID z gluon DB → STORE + UID EXPUNGE)
        db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
        try:
            for folder in to_remove:
                row = db.execute("SELECT id FROM mailboxes_v2 WHERE name=?", (folder,)).fetchone()
                if not row:
                    continue
                table = f'mailbox_message_{row[0]}'
                r = db.execute(f'SELECT uid FROM "{table}" WHERE message_id=? AND deleted=0 LIMIT 1',
                               (message_id,)).fetchone()
                if not r:
                    continue
                typ, _ = select_mailbox(m, folder)
                if typ != 'OK':
                    failures.append((folder, 'select fail'))
                    continue
                m.uid('store', str(r[0]), '+FLAGS.SILENT', r'(\Deleted)')
                typ, resp = m.uid('expunge', str(r[0]))
                if typ != 'OK':
                    try:
                        m.expunge()
                    except Exception:
                        pass
                print(f'  odstraněn label {folder}')
        finally:
            db.close()

        m.logout()
        if failures:
            for label, err in failures:
                print(f'FAIL {label}: {err}', file=sys.stderr)
            return 1
        return 0
    except Exception as e:
        print(f'ERR: {e}', file=sys.stderr)
        try:
            m.logout()
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
