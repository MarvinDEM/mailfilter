#!/usr/bin/env python3
import json
import re
import sqlite3
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_rules

ACCOUNT='bezouska'
ROOT=Path('/root/.openclaw/workspace')
OUT_PATH=ROOT/'tmp'/'bezouska-learning-report.json'
DEBUG_LOG_PATH=ROOT/'tmp'/'learn-from-tomas-debug.log'
RULES_PATH=ROOT/'mail-sorting-rules.md'
DB_PATH=ROOT/'state'/'bezouska-llm-queue.sqlite3'
LABEL_FOLDER='Labels/zatridil tomas'
LEARNINGFAIL_LABEL='learningfail'
BATCH_LIMIT=50
SEND_EMAIL_SCRIPT=ROOT/'bin'/'send-email.py'
NOTIFY_TO='tomas@bezouska.cz'
NOTIFY_FROM='lodivod@protonmail.ch'
HIMALAYA_TIMEOUT_SECONDS=30
HIMALAYA_RETRY_DELAY_SECONDS=3
HIMALAYA_TRANSPORT_RETRIES=3
HIMALAYA_TRANSPORT_RETRY_DELAY_SECONDS=2
QUICK_MAILBOXES=[
    'Labels/zatridil tomas',
    'Labels/reading-required',
    'Labels/newsletter',
    'Labels/vyresit',
    'Labels/faktury',
    'Labels/finance',
    'Labels/03_ipsd',
    'Labels/50_osobni',
    'Folders/90_ostatni/91_newsletter',
    'Folders/90_ostatni/92_transakce',
    'Folders/10_osobni/11_tomas',
    'Folders/10_osobni/31_zvole',
    'Folders/50_pracovni/51_bezouska',
    'Folders/50_pracovni/52_inadvisors',
    'Folders/50_pracovni/53_ipsd',
    'Folders/50_pracovni/54_mmr',
    'Folders/50_pracovni/70_prazske-noviny',
    'Folders/50_pracovni/80_delta',
]
LABEL_TO_SEMANTIC={
    'Labels/newsletter': 'newsletters',
    'Labels/vyresit': '00_vyresit',
    'Labels/faktury': 'transactions',
    'Labels/finance': 'transactions',
    'Labels/03_ipsd': 'work',
    'Labels/50_osobni': 'personal',
    'Labels/reading-required': 'reading-required',
}
KNOWN_LABELS=['newsletter','vyresit','faktury','finance','03_ipsd','50_osobni']
TRANSPORT_FAILURE_MARKERS=(
    'cannot connect to imap server',
    'cannot receive greeting from server',
    'stream was closed',
    'timed out',
)


class ImapTransportError(RuntimeError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run(*args):
    try:
        proc=subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=HIMALAYA_TIMEOUT_SECONDS)
        return proc.stdout
    except subprocess.TimeoutExpired as e:
        raise ImapTransportError(f"timeout running {' '.join(args)}") from e
    except subprocess.CalledProcessError as e:
        detail='\n'.join(part for part in [(e.stdout or '').strip(), (e.stderr or '').strip()] if part).strip()
        msg=detail or str(e)
        if is_transport_failure(msg):
            raise ImapTransportError(msg)
        raise RuntimeError(msg) from e


def run_read_with_retry(*args):
    """Read-only himalaya call with retries on transient IMAP transport failures.
    The Proton Bridge intermittently drops connections under rapid sequential load;
    observed failure rate is low and retry almost always succeeds.
    Only for READ operations (list/read) — mutations (move/copy) stay single-attempt."""
    last_error = None
    for attempt in range(HIMALAYA_TRANSPORT_RETRIES):
        try:
            return run(*args)
        except ImapTransportError as e:
            last_error = e
            if attempt + 1 < HIMALAYA_TRANSPORT_RETRIES:
                time.sleep(HIMALAYA_TRANSPORT_RETRY_DELAY_SECONDS * (attempt + 1))
    raise last_error


def is_transport_failure(message):
    text=(message or '').lower()
    return any(marker in text for marker in TRANSPORT_FAILURE_MARKERS)


def list_env(folder, page='1', page_size='200'):
    last_error=None
    for attempt in range(2):
        try:
            return json.loads(run_read_with_retry('himalaya','envelope','list','-a',ACCOUNT,'-f',folder,'-p',page,'--page-size',page_size,'--output','json'))
        except ImapTransportError:
            raise
        except Exception as e:
            last_error=e
            if attempt == 0:
                time.sleep(HIMALAYA_RETRY_DELAY_SECONDS)
                continue
            raise
    raise last_error


def get_message_id(folder, mid):
    txt=run_read_with_retry('himalaya','message','read','-a',ACCOUNT,'-f',folder,'-p','-H','Message-ID',str(mid))
    m=re.search(r'^Message-ID:\s*(.+)$', txt, re.MULTILINE)
    return m.group(1).strip() if m else None


def debug_log(message):
    # Rotate: cap the debug log so it can never fill the disk again
    # (it grows ~10-50 MB per full run with per-comparison lines).
    try:
        if DEBUG_LOG_PATH.exists() and DEBUG_LOG_PATH.stat().st_size > 100 * 1024 * 1024:
            DEBUG_LOG_PATH.unlink()
    except OSError:
        pass
    with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(f"{now_iso()} - {message}\n")


def safe_message_id(folder, mid):
    try:
        return get_message_id(folder, mid)
    except ImapTransportError:
        raise
    except Exception as e:
        debug_log(f"Error getting Message-ID for mailbox={folder} id={mid}: {e}")
        return None


def ensure_folder(folder):
    try:
        subprocess.run(['himalaya','folder','add','-a',ACCOUNT,folder], check=True, capture_output=True, text=True, timeout=HIMALAYA_TIMEOUT_SECONDS)
        debug_log(f"Created folder: {folder}")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or '').lower()
        stdout = (e.stdout or '').lower()
        if 'already exists' in stderr or 'already exists' in stdout:
            return
        debug_log(f"Error ensuring folder {folder}: {e}")
    except Exception as e:
        debug_log(f"Error ensuring folder {folder}: {e}")


def copy_to_label(src_folder, label, mid):
    # Sekvenční IMAP COPY — UID COPY je v Proton Bridge rozbitý (2026-08-24)
    subprocess.run(['python3','/root/.openclaw/workspace/bin/imap-label-copy.py',src_folder,f'Labels/{label}',str(mid)],
                   check=True, capture_output=True, text=True, timeout=60)


def mark_learning_fail(mid):
    # t (2026-08-10): při zařazení do learningfail odeber label 'zatridil tomas',
    # jinak se stejné maily točí ve smyčce každých 15 min (no_folder_hit loop).
    # t (2026-08-25): zrušen return-loop label 'marvintest2807' — kanonický
    # návrat do editační smyčky je teď jen 'learningfail' (a notifikační email).
    marking_errors=[]
    # přesun z 'zatridil tomas' do learningfail = odebrání zdrojového labelu
    try:
        run('himalaya','message','move','-a',ACCOUNT,'-f',LABEL_FOLDER,f'Labels/{LEARNINGFAIL_LABEL}',str(mid))
    except Exception as e:
        marking_errors.append({'label': LEARNINGFAIL_LABEL, 'error': str(e)})
        debug_log(f"Error moving message {mid} from {LABEL_FOLDER} to Labels/{LEARNINGFAIL_LABEL}: {e}")
    return marking_errors


def notify_learningfail(events):
    # t (2026-08-12): kdykoli se mail dostane do labelu 'learningfail', pošli
    # mail se zdůvodněním na tomas@bezouska.cz, aby mohl prozkoumat problém
    # a případně upravit pravidla/instrukce. Jeden souhrnný mail za běh.
    if not events:
        return
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    lines = []
    for ev in events[:60]:
        detail = ev.get('detail') or ''
        lines.append(f"- {ev.get('sender') or '?'} | {ev.get('subject') or '?'} | {ev.get('status')}{detail}")
    if len(events) > 60:
        lines.append(f"- … a dalších {len(events) - 60} mail(ů)")
    body = (
        f"Učící smyčka přesunula {len(events)} mail(ů) do labelu 'learningfail'.\n\n"
        + "\n".join(lines)
        + "\n\nCo to znamená: u těchto mailů se nepodařilo najít/naučit pravidlo "
          "třídění (nejčastěji no_folder_hit — žádná složka neodpovídá odesílateli).\n"
          "Maily zůstávají v Protonu pod labelem 'learningfail' (nic se nemaže).\n"
          "Pravidla žijí v mail-sorting-rules.md + state/bezouska-llm-queue.sqlite3.\n\n"
          "— Marvin\n"
    )
    raw = (
        f"From: {NOTIFY_FROM}\nTo: {NOTIFY_TO}\n"
        f"Subject: 🔁 learningfail: {len(events)} mailů z učící smyčky ({now})\n\n"
        + body
    )
    try:
        proc = subprocess.run(
            ['python3', str(SEND_EMAIL_SCRIPT)],
            input=raw.encode('utf-8'), capture_output=True, timeout=60,
        )
        debug_log(f"learningfail notify rc={proc.returncode}: {proc.stdout.decode(errors='replace')[:200]}")
    except Exception as e:
        debug_log(f"learningfail notify failed: {e}")


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn=sqlite3.connect(DB_PATH)
    conn.row_factory=sqlite3.Row
    conn.execute('''
        CREATE TABLE IF NOT EXISTS learned_rules (
            sender TEXT PRIMARY KEY,
            folder TEXT NOT NULL,
            labels_json TEXT NOT NULL,
            source TEXT NOT NULL,
            learned_at TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 1,
            notes TEXT
        )
    ''')
    return conn


def gather_hits(sender, subj, msgid):
    hits=[]
    for mailbox in QUICK_MAILBOXES:
        debug_log(f"Checking mailbox: {mailbox}")
        try:
            envs=list_env(mailbox)
        except ImapTransportError:
            raise
        except Exception:
            debug_log(f"Error listing envelopes for mailbox {mailbox}")
            continue
        time.sleep(0.2)
        for e in envs:
            debug_log(f"  Comparing: (e.subject='{e.get('subject')}', e.from='{(e.get('from') or {}).get('addr')}', e.id='{e.get('id')}') vs (subj='{subj}', sender='{sender}', msgid='{msgid}')")
            if e.get('subject')==subj and ((e.get('from') or {}).get('addr')==sender):
                debug_log(f"    Subject and sender match. Getting Message-ID for id={e['id']}")
                try:
                    m2=get_message_id(mailbox, e['id'])
                    debug_log(f"    Found Message-ID m2='{m2}'")
                except ImapTransportError:
                    raise
                except Exception as ex:
                    debug_log(f"    Error getting Message-ID: {ex}")
                    m2=None
                if m2 == msgid:
                    debug_log(f"    Message-ID also matches. Hit found in {mailbox}")
                    hits.append({'mailbox': mailbox, 'id': e['id']})
    return hits


def derive_labels_from_hits(label_hits):
    labels=[]
    for h in label_hits:
        mailbox=h['mailbox']
        if mailbox.startswith('Labels/'):
            name=mailbox.split('/',1)[1]
            if name in KNOWN_LABELS and name not in labels:
                labels.append(name)
    return labels


def derive_rule_impacts(sender, actual_folder, proton_labels):
    lines=[f'- sender `{sender}` -> `{actual_folder}`']
    if proton_labels:
        lines.append(f'- sender `{sender}` -> labels `{", ".join(proton_labels)}`')
    return lines


def update_rules_and_upload(suggestions):
    if not suggestions:
        return False
    if not RULES_PATH.exists():
        return False
    text=RULES_PATH.read_text(encoding='utf-8')
    marker='## Learning updates from `zatridil tomas` corrections\n'
    uniq=[]
    for s in suggestions:
        if s not in uniq:
            uniq.append(s)
    block=marker + '\n'.join(uniq[:80]) + '\n'
    if marker in text:
        new_text=text.split(marker)[0].rstrip() + '\n\n' + block
    else:
        new_text=text + '\n\n' + block
    changed = (new_text != text)
    if changed:
        RULES_PATH.write_text(new_text, encoding='utf-8')
        subprocess.run(['/root/.openclaw/workspace/bin/rclone-protonfix','deletefile','protondrive:mail-sorting-rules.md'], check=True, capture_output=True, text=True, timeout=60)
        subprocess.run(['/root/.openclaw/workspace/bin/rclone-protonfix','copy',str(RULES_PATH),'protondrive:'], check=True, capture_output=True, text=True, timeout=120)
    return changed


def main():
    report=None
    ensure_folder(f'Labels/{LEARNINGFAIL_LABEL}')
    labeled=list_env(LABEL_FOLDER)[:BATCH_LIMIT]
    items=[]
    suggestions=[]
    learned_rules=[]
    checked_existing=0
    removed=0
    errors=[]
    learningfail_events=[]
    aborted_reason=None
    conn=mail_rules.db_connect()
    try:
        for e in labeled:
            subj=e['subject']
            sender=(e.get('from') or {}).get('addr')
            try:
                msgid=safe_message_id(LABEL_FOLDER, e['id'])
                if not msgid:
                    marking_errors=mark_learning_fail(e['id'])
                    errors.append({'subject': subj, 'sender': sender, 'id': e['id'], 'status': 'message_id_unavailable', 'marking_errors': marking_errors})
                    items.append({'subject':subj,'sender':sender,'message_id':None,'status':'message_id_unavailable','marking_errors':marking_errors})
                    learningfail_events.append({'sender':sender,'subject':subj,'status':'message_id_unavailable','detail':f' (marking_errors={marking_errors})'})
                    continue
                hits=gather_hits(sender, subj, msgid)
                folder_hits=[h for h in hits if h['mailbox'].startswith('Folders/')]
                label_hits=[h for h in hits if h['mailbox'].startswith('Labels/') and h['mailbox'] != LABEL_FOLDER]
                proton_labels=derive_labels_from_hits(label_hits)
                if folder_hits:
                    actual_folder=folder_hits[0]['mailbox']
                    checked_existing += 1
                    suggestions.extend(derive_rule_impacts(sender, actual_folder, proton_labels))
                    # t (2026-08-12): učit s předmětem — sender sám nestačí (datovka aj.)
                    rule_action = mail_rules.upsert_learned_rule(
                        conn, sender, actual_folder, proton_labels,
                        f'learned from {LABEL_FOLDER}', subject=subj)
                    if rule_action == 'conflict-subject-rule':
                        debug_log(f"Rule conflict for {sender}: subject-scoped rule added (subject='{subj}')")
                    try:
                        run('himalaya','message','move','-a',ACCOUNT,'-f',LABEL_FOLDER,actual_folder,str(e['id']))
                        removed += 1
                        learned_rules.append({'sender':sender,'folder':actual_folder,'labels':proton_labels})
                        items.append({'subject':subj,'sender':sender,'message_id':msgid,'status':'checked_existing','actual_folder':actual_folder,'other_labels':label_hits,'learned_rule_labels':proton_labels})
                    except Exception as ex:
                        marking_errors=mark_learning_fail(e['id'])
                        errors.append({'subject': subj, 'sender': sender, 'id': e['id'], 'status': 'move_failed', 'error': str(ex), 'marking_errors': marking_errors})
                        items.append({'subject':subj,'sender':sender,'message_id':msgid,'status':'move_failed','actual_folder':actual_folder,'other_labels':label_hits,'learned_rule_labels':proton_labels,'error':str(ex),'marking_errors':marking_errors})
                        learningfail_events.append({'sender':sender,'subject':subj,'status':'move_failed','detail':f' → {actual_folder} ({str(ex)[:120]})'})
                else:
                    debug_log(f"No primary folder hit for Subject: '{subj}', Sender: '{sender}', Message-ID: '{msgid}'")
                    marking_errors=mark_learning_fail(e['id'])
                    errors.append({'subject': subj, 'sender': sender, 'id': e['id'], 'status': 'no_folder_hit', 'marking_errors': marking_errors})
                    items.append({'subject':subj,'sender':sender,'message_id':msgid,'status':'no_folder_hit','other_labels':label_hits,'marking_errors':marking_errors})
                    learningfail_events.append({'sender':sender,'subject':subj,'status':'no_folder_hit','detail':f" (other_labels={[h['mailbox'] for h in label_hits]})"})
            except ImapTransportError as ex:
                aborted_reason=f'imap_transport_failure: {ex}'
                marking_errors=mark_learning_fail(e['id'])
                errors.append({'subject': subj, 'sender': sender, 'id': e['id'], 'status': 'imap_transport_failure', 'error': str(ex), 'marking_errors': marking_errors})
                items.append({'subject':subj,'sender':sender,'status':'imap_transport_failure','error':str(ex),'marking_errors':marking_errors})
                learningfail_events.append({'sender':sender,'subject':subj,'status':'imap_transport_failure','detail':f' ({str(ex)[:120]})'})
                break
        rules_updated = update_rules_and_upload(suggestions)
        report={'processed':len(items),'checked_existing':checked_existing,'removed_zatridil_tomas':removed,'rules_updated':rules_updated,'learned_rules':learned_rules,'suggestions':suggestions[:80],'errors':errors,'items':items,'aborted_reason':aborted_reason}
    except Exception as ex:
        report={'processed':len(items),'checked_existing':checked_existing,'removed_zatridil_tomas':removed,'rules_updated':False,'learned_rules':learned_rules,'suggestions':suggestions[:80],'errors':errors,'items':items,'aborted_reason':f'unhandled_exception: {ex}','traceback':traceback.format_exc()}
        raise
    finally:
        conn.close()
        if report is None:
            report={'processed':len(items),'checked_existing':checked_existing,'removed_zatridil_tomas':removed,'rules_updated':False,'learned_rules':learned_rules,'suggestions':suggestions[:80],'errors':errors,'items':items,'aborted_reason':'report_not_built'}
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    # t (2026-08-12): learningfail → vždy mail se zdůvodněním
    notify_learningfail(learningfail_events)
    print(json.dumps({'processed':len(items),'checked_existing':checked_existing,'removed_zatridil_tomas':removed,'rules_updated':report.get('rules_updated', False),'learned_rules':len(learned_rules),'errors':len(errors),'aborted_reason':report.get('aborted_reason')}, ensure_ascii=False))

if __name__=='__main__':
    main()
