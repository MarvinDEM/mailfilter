#!/usr/bin/env python3
"""MailFilter — manuální spuštění jednoho pravidla (t, 2026-08-23).

Aplikuje pravidlo z learned_rules na aktuální INBOX účtu bezouska:
najde shodné zprávy (sender × recipient × subject_pattern) a přesune je
do složky pravidla + nakopíruje na labely (Labels/<name>).

Volání: mailfilter-run-rule.py <request_file.json>
  request_file: {rule_id, requested_at} (z state/mailfilter-run-requests/)
Výstup: state/mailfilter-run-results/run-<rule_id>-<request_ts>.json
"""
import email
import imaplib
import json
import os
import re
import smtplib
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/root/.openclaw/workspace')
DB_PATH = ROOT / 'state' / 'bezouska-llm-queue.sqlite3'
REQ_DIR = ROOT / 'state' / 'mailfilter-run-requests'
RES_DIR = ROOT / 'state' / 'mailfilter-run-results'
ACCOUNT = 'bezouska'
HIMALAYA_TIMEOUT_SECONDS = 45
HIMALAYA_RETRY_DELAY_SECONDS = 5

sys.path.insert(0, str(ROOT / 'bin'))
import mail_rules  # noqa: E402


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run(*args):
    return subprocess.check_output(list(args), text=True, timeout=HIMALAYA_TIMEOUT_SECONDS)


def _run_himalaya(args, src, dst):
    last_error = None
    for attempt in range(2):
        try:
            subprocess.run(args, check=True, capture_output=True, text=True, timeout=HIMALAYA_TIMEOUT_SECONDS)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            last_error = e
            if attempt == 0:
                time.sleep(HIMALAYA_RETRY_DELAY_SECONDS)
                continue
            raise RuntimeError(f'himalaya failed for {src} -> {dst}: {last_error}')


def list_inbox():
    return json.loads(run('himalaya', 'envelope', 'list', '-a', ACCOUNT, '-f', 'INBOX',
                          '--page-size', '500', '--output', 'json'))


def _msg_sender_recipient_subject(msg):
    """Vytáhni (sender, recipient, subject) z envelope pro guard specifických pravidel."""
    sender = ((msg.get('from') or {}).get('addr') or '').lower()
    to_list = msg.get('to') or []
    recipient = ''
    if isinstance(to_list, list) and to_list:
        first = to_list[0]
        recipient = ((first.get('addr') if isinstance(first, dict) else str(first)) or '').lower()
    elif isinstance(to_list, dict):
        recipient = (to_list.get('addr') or '').lower()
    return sender, recipient, (msg.get('subject') or '').lower()


def _skip_for_specific_rule(conn, rule, msg):
    """Guard: běžící pravidlo BEZ předmětu nesmí ukrást emaily, které matchuje
    nějaké konkrétnější aktivní pravidlo S předmětem (t, 2026-08-24 — t: dvě pravidla
    pro stejnou adresu, jedno s předmětem a jedno bez; široké spuštěné později by
    vrátilo práci specifického). Vrací True = přeskočit zprávu."""
    if rule.get('subject_pattern'):
        return False
    try:
        sender, recipient, subj = _msg_sender_recipient_subject(msg)
        best = mail_rules.learned_rule_lookup(conn, sender, recipient, subj, confirmed_only=True)
        return bool(best and best.get('rule_id') != rule['id'] and best.get('subject_pattern'))
    except Exception:
        return False


def list_folder_paged(folder, page_size=1000, max_pages=50):
    """Projdi celou složku po stránkách — himalaya vrací jen první stránku
    (--page-size), takže velké složky (91_newsletter ~6700 mailů) se dřív
    usekly na prvních 1000 a starší maily nikdy nebyly naskenované
    (t, 2026-08-24: pravidlo 93 nematchlo uid 217 z července).
    Vrací seznam envelope dictů (bez duplicit napříč stránkami)."""
    out = []
    seen = set()
    for page in range(1, max_pages + 1):
        try:
            chunk = json.loads(run('himalaya', 'envelope', 'list', '-a', ACCOUNT, '-f', folder,
                                   '--page', str(page), '--page-size', str(page_size),
                                   '--output', 'json'))
        except Exception:
            break
        if not chunk:
            break
        new = [e for e in chunk if e.get('id') not in seen]
        if not new:
            break
        out.extend(new)
        seen.update(e['id'] for e in new)
        if len(chunk) < page_size:
            break
    return out


GLUON_DB = '/home/protonbridge/.local/share/protonmail/bridge-v3/gluon/backend/db/f02b04c0-29bd-4d4e-9f70-b4e216bb1c55.db'  # BEZOUSKA gluon store!


def _gluon_mailboxes():
    db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
    try:
        return {r[0]: r[2] for r in db.execute("SELECT id, remote_id, name FROM mailboxes_v2")}
    finally:
        db.close()


def _gluon_msgid_index(db):
    """message_id -> Message-ID header (poslední quoted string v envelope) + reverzní index.
    Divergence: label-kopie (IMAP COPY) = samostatný message objekt se stejným
    Message-ID headerem (t, 2026-08-24)."""
    fwd, rev = {}, {}
    for mid, env in db.execute('SELECT id, envelope FROM messages_v2'):
        if not env:
            continue
        m = re.findall(r'"([^"]*)"', env)
        if not m:
            continue
        h = m[-1]
        if h.startswith('<'):
            fwd[mid] = h
            rev.setdefault(h, []).append(mid)
    return fwd, rev


def _msg_in_any_folder(db, mailboxes, msg_id, fwd, rev):
    """message_id NEBO sibling (stejný Message-ID) je v nějakém Folders/* mailboxu?
    → pak ho zpracuje folder sken a label sken ho má přeskočit (t, 2026-08-24)."""
    ids = [msg_id]
    h = fwd.get(msg_id)
    if h and h in rev:
        ids = rev[h]
    for mid in ids:
        for mbox_id, name in mailboxes.items():
            if not name.startswith('Folders/'):
                continue
            try:
                if db.execute(f'SELECT 1 FROM "mailbox_message_{mbox_id}" WHERE message_id=? AND deleted=0 LIMIT 1', (mid,)).fetchone():
                    return True
            except sqlite3.OperationalError:
                continue
    return False


def move(src, dst, mid):
    _run_himalaya(['himalaya', 'message', 'move', '-a', ACCOUNT, '-f', src, dst, str(mid)], src, dst)


def copy_to_label(src, label, mid):
    # (zastaralé — labely se teď řeší přes imap-label-sync.py, t 2026-08-24)
    subprocess.run(
        ['python3', str(ROOT / 'bin' / 'imap-label-copy.py'), src, f'Labels/{label}', str(mid)],
        check=True, capture_output=True, text=True, timeout=60)


def forward_message(src, mid, to_addr):
    """Přepošli zprávu (raw RFC822) na zadanou adresu (t, 2026-08-24).
    From = tomas@bezouska.cz (kvůli SPF/DMARC), Subject prefix 'Fwd: ',
    tělo i přílohy zůstávají beze změny."""
    pw = open('/root/.config/himalaya/tomas-bezouska-bridge.pass').read().strip()
    m = imaplib.IMAP4('127.0.0.1', 1143, timeout=30)
    m.login('tomas@bezouska.cz', pw)
    try:
        m._simple_command('SELECT', '"' + src.replace('"', '\\"') + '"')
        m.state = 'SELECTED'
        typ, data = m.uid('fetch', mid, '(BODY.PEEK[])')
        if typ != 'OK' or not data:
            raise RuntimeError(f'raw fetch selhal pro {src}/{mid}')
        raw = b''.join(p[1] for p in data if isinstance(p, tuple) and isinstance(p[1], bytes))
        msg = email.message_from_bytes(raw)
        subject = str(msg.get('Subject', '')).strip()
        if not subject.lower().startswith('fwd:'):
            msg.replace_header('Subject', f'Fwd: {subject}')
        msg.replace_header('From', 'tomas@bezouska.cz')
        msg.replace_header('To', to_addr)
        for h in ['Return-Path', 'DKIM-Signature', 'X-Pm-Gluon-Id', 'X-Pm-Original-Author', 'X-Pm-Content-Encryption']:
            if h in msg:
                del msg[h]
        server = smtplib.SMTP('127.0.0.1', 1025, timeout=30)
        server.starttls()
        server.login('tomas@bezouska.cz', pw)
        server.send_message(msg, from_addr='tomas@bezouska.cz', to_addrs=[to_addr])
        server.quit()
    finally:
        try:
            m.logout()
        except Exception:
            pass


def sync_labels(src, labels, mid):
    """Nastaví EXAKTNÍ sadu labelů zprávy — přidá chybějící, ostatní odstraní
    (t, 2026-08-24): mailu zůstanou JEN labely definované ve filtru;
    filtr bez labelů = všechny labely se odstraní."""
    subprocess.run(
        ['python3', str(ROOT / 'bin' / 'imap-label-sync.py'), src, str(mid)] + list(labels),
        check=True, capture_output=True, text=True, timeout=120)


def envelope_matches(msg, rule):
    sender = ((msg.get('from') or {}).get('addr') or '').lower()
    # sender '' v pravidle = libovolný odesílatel; '*' = wildcard (t, 2026-08-23/24)
    if rule.get('sender') and not mail_rules.wildcard_match(rule['sender'], sender):
        return False
    if rule.get('recipient'):
        to_list = msg.get('to') or []
        recipient = ''
        if isinstance(to_list, list) and to_list:
            first = to_list[0]
            recipient = ((first.get('addr') if isinstance(first, dict) else str(first)) or '').lower()
        elif isinstance(to_list, dict):
            recipient = (to_list.get('addr') or '').lower()
        if not mail_rules.wildcard_match(rule['recipient'], recipient):
            return False
    if rule.get('subject_pattern'):
        subj = (msg.get('subject') or '').lower()
        if rule['subject_pattern'].lower() not in subj:
            return False
    return True


def main():
    req_file = Path(sys.argv[1])
    req = json.loads(req_file.read_text(encoding='utf-8'))
    rule_id = int(req['rule_id'])
    request_ts = req.get('requested_at', now_iso()).replace(':', '').replace('+', '')
    scope = req.get('scope', 'inbox')  # 'inbox' | 'all' | konkrétní složka (t, 2026-08-23)

    folders_to_scan = ['INBOX']
    if scope == 'all':
        try:
            dump = json.loads((ROOT / 'state' / 'mailfilter-folders.json').read_text(encoding='utf-8'))
            folders_to_scan = ['INBOX'] + [f for f in dump.get('folders', []) if f.startswith('Folders/')]
        except Exception:
            pass
    elif scope.startswith('Folders/') or scope == 'All Mail':
        folders_to_scan = [scope]
    result = {
        'rule_id': rule_id,
        'requested_at': req.get('requested_at'),
        'started_at': now_iso(),
        'status': 'done',
        'matched': 0,
        'skipped_specific': 0,
        'moved': 0,
        'labels_applied': 0,
        'forwarded': 0,
        'errors': [],
        'finished_at': None,
    }

    conn = mail_rules.db_connect()
    try:
        rule = mail_rules.get_rule(conn, rule_id)
    finally:
        conn.close()

    if not rule:
        result['status'] = 'error'
        result['errors'].append('rule not found')
    elif not rule.get('active') or rule.get('review_status') == 'discard':
        result['status'] = 'skipped'
        result['errors'].append('rule is not active (discarded)')
    else:
        try:
            envelopes = list_inbox()
        except Exception as e:
            result['status'] = 'error'
            result['errors'].append(f'inbox read failed: {e}')
            envelopes = []

        # FIX 2026-08-24 (t dotaz na 46/47/48): UID z himalaya envelope listu platí
        # JEN ve zdrojové složce. Dřív batch nesl src=cílová složka + uid ze zdrojové
        # → po přesunu neplatné UID → gluon lookup i IMAP selhaly ("bez DB záznamu",
        # "uid->seq fail") a labely se u přesunutých zpráv neaplikovaly.
        # Řešení: label sync běží PŘED přesunem (UID platná), pak teprve forward+move.
        matches = []  # {'src', 'uid', 'dest', 'fwd', 'labels'}
        if envelopes:
            for msg in envelopes:
                try:
                    if not envelope_matches(msg, rule):
                        continue
                    if _skip_for_specific_rule(conn, rule, msg):
                        result['skipped_specific'] += 1
                        continue
                    result['matched'] += 1
                    mid = str(msg['id'])
                    matches.append({'src': 'INBOX', 'uid': mid, 'dest': rule['folder'],
                                    'fwd': rule.get('forward_to'),
                                    'labels': rule.get('labels') or []})
                except Exception as e:
                    result['errors'].append(f'mid={msg.get("id")}: {e}')
        # scope: projdi všechny složky
        for folder in folders_to_scan[1:]:
            try:
                envs = list_folder_paged(folder)
            except Exception as e:
                result['errors'].append(f'folder {folder} read failed: {e}')
                continue
            for msg in envs:
                try:
                    if not envelope_matches(msg, rule):
                        continue
                    if _skip_for_specific_rule(conn, rule, msg):
                        result['skipped_specific'] += 1
                        continue
                    result['matched'] += 1
                    mid = str(msg['id'])
                    if folder != rule['folder']:
                        matches.append({'src': folder, 'uid': mid, 'dest': rule['folder'],
                                        'fwd': rule.get('forward_to'),
                                        'labels': rule.get('labels') or []})
                    else:
                        matches.append({'src': folder, 'uid': mid, 'dest': None,
                                        'fwd': None,
                                        'labels': rule.get('labels') or []})
                except Exception as e:
                    result['errors'].append(f'mid={msg.get("id")}: {e}')

        # scope=all: navíc projdi Labels/* — emaily žijící JEN v labelech
        # (learningfail/10_shop/reading-required atd., archivované bez složky) se ve
        # Folders skenu nikdy nenajdou → pravidlo je nematchlo a label zůstal
        # (t, 2026-08-24: zasilkovna 22700/22702). Dedup: pokud message (nebo sibling
        # dle Message-ID) existuje v nějaké Folders/* složce, řeší to folder sken.
        if scope == 'all':
            try:
                dump = json.loads((ROOT / 'state' / 'mailfilter-folders.json').read_text(encoding='utf-8'))
                label_mbs = [f for f in dump.get('folders', []) if f.startswith('Labels/')]
            except Exception:
                label_mbs = []
            if not label_mbs:
                # mailfilter-folders.json drží jen Folders/* (pro UI); labely vezmi live
                try:
                    fl = json.loads(run('himalaya', 'folder', 'list', '-a', ACCOUNT, '--output', 'json'))
                    label_mbs = [f.get('name', '') for f in fl if f.get('name', '').startswith('Labels/')]
                except Exception:
                    label_mbs = []
            if label_mbs:
                mailboxes = _gluon_mailboxes()
                db = sqlite3.connect(f'file:{GLUON_DB}?mode=ro', uri=True)
                try:
                    fwd, rev = _gluon_msgid_index(db)
                except Exception:
                    fwd, rev = {}, {}
                try:
                    for folder in label_mbs:
                        try:
                            envs = list_folder_paged(folder)
                        except Exception:
                            continue
                        for msg in envs:
                            try:
                                if not envelope_matches(msg, rule):
                                    continue
                                if _skip_for_specific_rule(conn, rule, msg):
                                    result['skipped_specific'] += 1
                                    continue
                                uid = str(msg['id'])
                                mid_db = None
                                try:
                                    for mbox_id, name in mailboxes.items():
                                        if name != folder:
                                            continue
                                        r = db.execute(f'SELECT message_id FROM "mailbox_message_{mbox_id}" WHERE uid=? LIMIT 1', (int(uid),)).fetchone()
                                        if r:
                                            mid_db = r[0]
                                        break
                                except Exception:
                                    mid_db = None
                                if mid_db and _msg_in_any_folder(db, mailboxes, mid_db, fwd, rev):
                                    continue  # zpracuje folder sken
                                result['matched'] += 1
                                matches.append({'src': folder, 'uid': uid, 'dest': rule['folder'],
                                                'fwd': rule.get('forward_to'),
                                                'labels': rule.get('labels') or [],
                                                'src_is_label': True})
                            except Exception as e:
                                result['errors'].append(f'label {folder} mid={msg.get("id")}: {e}')
                finally:
                    db.close()

        # dávkový label sync — PŘED přesuny (UID platná ve zdrojových složkách);
        # jeden proces na celý běh (274 IMAP skenů na zprávu per-message
        # způsobovalo timeout; gluon DB lookup, t 2026-08-24)
        label_batch = [{'src': m['src'], 'uid': m['uid'], 'labels': m['labels']}
                       for m in matches]
        if label_batch:
            batch_file = RES_DIR / f'labels-{rule_id}-{request_ts}.json'
            batch_file.write_text(json.dumps(label_batch), encoding='utf-8')
            try:
                subprocess.run(
                    ['python3', str(ROOT / 'bin' / 'imap-label-sync-batch.py'), str(batch_file)],
                    check=True, capture_output=True, text=True, timeout=300)
            except subprocess.CalledProcessError as e:
                result['errors'].append(f'label sync batch selhal: {(e.stderr or e.stdout or "")[:300]}')
            finally:
                try:
                    batch_file.unlink()
                except Exception:
                    pass
            result['labels_applied'] += sum(len(m['labels']) for m in label_batch)

        # teprve teď: forward + přesun (label sync už proběhl ve zdrojových složkách)
        for i, m in enumerate(matches, 1):
            try:
                if m['fwd']:
                    forward_message(m['src'], m['uid'], m['fwd'])
                    result['forwarded'] = result.get('forwarded', 0) + 1
                if m['dest'] and m['src'] != m['dest']:
                    move(m['src'], m['dest'], m['uid'])
                    result['moved'] += 1
                if i % 10 == 0 or i == len(matches):
                    print(f'[mailfilter-run] progress {i}/{len(matches)} '
                          f'(fwd={result["forwarded"]}, moved={result["moved"]})', flush=True)
            except Exception as e:
                result['errors'].append(f'uid={m["uid"]}: {e}')

    result['finished_at'] = now_iso()
    print(f'[mailfilter-run] rule {rule_id} done: matched={result["matched"]} '
          f'skipped_specific={result["skipped_specific"]} moved={result["moved"]} '
          f'labels={result["labels_applied"]} fwd={result["forwarded"]} errors={len(result["errors"])}', flush=True)
    RES_DIR.mkdir(parents=True, exist_ok=True)
    out = RES_DIR / f'run-{rule_id}-{request_ts}.json'
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
