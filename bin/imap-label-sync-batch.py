#!/usr/bin/env python3
"""Dávkový label sync pro mailfilter — exaktní sada labelů pro VÍCE zpráv najednou
(t, 2026-08-24; nahrazuje per-message imap-label-sync.py — 274 IMAP skenů na zprávu
způsobovalo timeout u pravidel s velkým počtem matchů, incident rule #69).

Vstup: JSON soubor (první arg): [{"src": "...", "uid": 123, "labels": ["vyresit"]}, ...]
  - src: složka, kde zpráva je
  - uid: IMAP UID
  - labels: labely, které mají ZŮSTAT (bez prefixu Labels/)

Chování:
  - aktuální labely zprávy se zjistí z gluon SQLite DB bridge (mailbox_message_* ×
    message_id) — rychlé, žádné IMAP skeny
  - chybějící labely se přidají sekvenčním COPY (UID COPY je v bridge rozbitý)
  - labely mimo keep se odstraní (STORE \\Deleted + UID EXPUNGE)
  - pokud zdrojová složka není v gluon DB (bridge nekonzistence, např. nově
    vytvořená složka) → odstranění se přeskočí s varováním (ADD funguje dál)

Exit 0 = vše OK; jinak 1.
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
GLUON_DB = '/home/protonbridge/.local/share/protonmail/bridge-v3/gluon/backend/db/f02b04c0-29bd-4d4e-9f70-b4e216bb1c55.db'  # BEZOUSKA gluon store!


def _db_mailbox_tables():
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    try:
        mailboxes = {r[0]: r[2] for r in db.execute("SELECT id, remote_id, name FROM mailboxes_v2")}
        return mailboxes
    finally:
        db.close()


def _db_message_id(mailboxes, src_folder, uid):
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    try:
        mid = None
        for mbox_id, name in mailboxes.items():
            if name != src_folder:
                continue
            table = f'mailbox_message_{mbox_id}'
            try:
                r = db.execute(f'SELECT message_id FROM "{table}" WHERE uid=? LIMIT 1', (int(uid),)).fetchone()
                if r:
                    mid = r[0]
                    break
            except sqlite3.OperationalError:
                continue
        return mid
    finally:
        db.close()


def _db_labels_of_message(mailboxes, message_id):
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    try:
        labels = []
        for mbox_id, name in mailboxes.items():
            if not name.startswith('Labels/'):
                continue
            table = f'mailbox_message_{mbox_id}'
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


def _db_uid_in_label(mailboxes, message_id, label_folder):
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    try:
        for mbox_id, name in mailboxes.items():
            if name != label_folder:
                continue
            table = f'mailbox_message_{mbox_id}'
            try:
                r = db.execute(
                    f'SELECT uid FROM "{table}" WHERE message_id=? AND deleted=0 LIMIT 1',
                    (message_id,)).fetchone()
                return r[0] if r else None
            except sqlite3.OperationalError:
                return None
        return None
    finally:
        db.close()


def _msgid_index():
    """message_id -> Message-ID header (poslední quoted string v envelope) + reverzní index.
    Divergence gluon DB: IMAP COPY do label mailboxu (learn_from_zatridil_tomas atd.)
    vytvořil pro stejný fyzický mail SAMOSTATNÝ message objekt (jiné message_id i remote_id)
    → DB nezná vazbu folder-kopie ↔ label-kopie → odstranění labelu tichý no-op.
    Párujeme přes Message-ID header, který je u kopií stejný (t, 2026-08-24)."""
    import re
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    try:
        fwd, rev = {}, {}
        for mid, env in db.execute('SELECT id, envelope FROM messages_v2'):
            if not env:
                continue
            m = re.findall(r'"([^"]*)"', env)
            if not m:
                continue
            h = m[-1]
            if not h.startswith('<'):
                continue
            fwd[mid] = h
            rev.setdefault(h, []).append(mid)
        return fwd, rev
    finally:
        db.close()


def _uid_to_seq(m, src, uid):
    select_mailbox(m, src)
    typ, data = m.uid('fetch', uid, '(UID)')
    if typ != 'OK' or not data or data[0] is None:
        return None
    part = data[0]
    head = part[0] if isinstance(part, tuple) else part
    if isinstance(head, bytes):
        return head.split(b' ', 1)[0].decode()
    raw = b' '.join(x for x in part if isinstance(x, bytes))
    return raw.split(b' ', 1)[0].decode()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    items = json.load(open(sys.argv[1]))
    if not items:
        return 0

    mailboxes = _db_mailbox_tables()
    fwd, rev = _msgid_index()
    pw = open(PASS_FILE).read().strip()
    m = imaplib.IMAP4(HOST, PORT, timeout=30)
    m.login(ACCOUNT, pw)

    failures = []
    skipped_removal = 0
    try:
        for it in items:
            src = it['src']
            uid = str(it['uid'])
            keep = set(it.get('labels') or [])
            keep_full = {f'Labels/{l}' for l in keep}
            tag = f'{src} {uid}'

            message_id = _db_message_id(mailboxes, src, uid)
            if message_id is None:
                # zdrojová složka není v gluon DB — odstranění nejde, přidání ano
                skipped_removal += 1
                msg_ids = []
                to_add = keep_full
                to_remove = []
            else:
                # sibling message_id se stejným Message-ID headerem — divergence
                # (label-kopie = samostatný objekt v DB); union labelů přes všechny
                msg_ids = [message_id]
                h = fwd.get(message_id)
                if h and h in rev:
                    msg_ids = rev[h]
                current = set()
                for mid in msg_ids:
                    current |= set(_db_labels_of_message(mailboxes, mid))
                to_add = keep_full - current
                to_remove = [l for l in current if l not in keep_full]

            if to_add:
                seq = _uid_to_seq(m, src, uid)
                for target in to_add:
                    if seq is None:
                        failures.append((tag, 'uid->seq fail'))
                        continue
                    typ, resp = m._simple_command('COPY', f'{seq} {quote_mailbox(target)}')
                    if typ != 'OK':
                        failures.append((target, f'copy fail: {resp}'))

            for folder in to_remove:
                # zdrojový label u label-sourced záznamů NEMAŽ — přesun z labelu
                # do složky ho odstraní sám (batch běží před přesunem, t 2026-08-24)
                if it.get('src', '').startswith('Labels/') and folder == it['src']:
                    continue
                u = None
                for mid in msg_ids:
                    u = _db_uid_in_label(mailboxes, mid, folder)
                    if u is not None:
                        break
                if u is None:
                    continue
                typ, _ = select_mailbox(m, folder)
                if typ != 'OK':
                    failures.append((folder, 'select fail'))
                    continue
                m.uid('store', str(u), '+FLAGS.SILENT', r'(\Deleted)')
                typ, resp = m.uid('expunge', str(u))
                if typ != 'OK':
                    try:
                        m.expunge()
                    except Exception:
                        pass

        m.logout()
        if skipped_removal:
            print(f'POZN: {skipped_removal} zpráv bez DB záznamu — odstranění labelů přeskočeno', file=sys.stderr)
        if failures:
            for tag, err in failures[:10]:
                print(f'FAIL {tag}: {err}', file=sys.stderr)
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
