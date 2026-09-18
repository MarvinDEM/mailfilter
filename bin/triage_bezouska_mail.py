#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_rules

ACCOUNT='bezouska'
ROOT=Path('/root/.openclaw/workspace')
STATE_PATH=Path(os.environ.get('MAILFILTER_STATE_PATH', str(ROOT/'state'/'bezouska-mail-triage.json')))
RUN_LOG_PATH=Path(os.environ.get('MAILFILTER_RUN_LOG', str(ROOT/'state'/'bezouska-mail-triage-runs.jsonl')))
HIMALAYA_TIMEOUT_SECONDS=45
HIMALAYA_RETRY_DELAY_SECONDS=5
# DB_PATH respektuje MAIL_RULES_DB (testovací izolace); default viz mail_rules.
DB_PATH=mail_rules.DB_PATH

# FÁZE 1 (t 2026-09-18): dokud t neodsouhlasí, triage NEMĚNÍ mailbox —
# jen znovu zařadí maily do fronty a zapíše stav. Přesuny/labely se provedou
# jen s MAILFILTER_APPLY=1.
APPLY=os.environ.get('MAILFILTER_APPLY','0')=='1'

# MAILF-013 (t 2026-09-18): klasifikační heuristika (classify_folder /
# collect_labels / konstanty) už není zkopírovaná tady — jediný zdroj pravdy
# je bin/mail_rules.py (classify_folder, collect_labels, classify_message).
# Dřív byla duplikovaná tady i v bezouska_llm_second_pass_worker.py → drift.

# MAILF-010 (t 2026-09-18): kolik INBOX stránek maximálně projít (pojistka
# proti nekonečné smyčce; 1 stránka = HIMALAYA_PAGE_SIZE mailů).
HIMALAYA_PAGE_SIZE=200
HIMALAYA_MAX_PAGES=20


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run(*args):
    return subprocess.check_output(list(args), text=True, timeout=HIMALAYA_TIMEOUT_SECONDS)


def list_env(folder='INBOX', page_size='200'):
    last_error=None
    for attempt in range(2):
        try:
            return json.loads(run('himalaya','envelope','list','-a',ACCOUNT,'-f',folder,'--page-size',page_size,'--output','json'))
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            last_error=e
            if attempt == 0:
                time.sleep(HIMALAYA_RETRY_DELAY_SECONDS)
                continue
            raise
    raise last_error


def list_env_all(folder='INBOX', page_size=None, max_pages=None):
    """MAILF-011 (t 2026-09-18): projde CELÝ mailbox po stránkách.

    Dřív se četla jen první stránka (200/500 mailů) → 385 mailů z INBOXu se
    nikdy nezhodnotilo. himalaya stránkuje 1-indexovaně přes --page; dokud
    stránka vrátí plný počet, jdeme dál (max HIMALAYA_MAX_PAGES jako pojistka).
    """
    page_size=int(page_size or HIMALAYA_PAGE_SIZE)
    max_pages=int(max_pages or HIMALAYA_MAX_PAGES)
    out=[]
    for page in range(1, max_pages + 1):
        try:
            batch=json.loads(run('himalaya','envelope','list','-a',ACCOUNT,'-f',folder,
                                 '--page-size',str(page_size),'--page',str(page),
                                 '--output','json'))
        except subprocess.CalledProcessError:
            # stránka za koncem mailboxu → konec
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
    return out


def _run_himalaya(args, src, dst):
    """Run a himalaya mutation with one retry; raise with stderr in message."""
    last_error = None
    for attempt in range(2):
        try:
            subprocess.run(args, check=True, capture_output=True, text=True, timeout=HIMALAYA_TIMEOUT_SECONDS)
            return
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            stderr = (getattr(e, 'stderr', None) or '').strip()
            stdout = (getattr(e, 'stdout', None) or '').strip()
            last_error = f'{e.__class__.__name__}: {e} stderr={stderr!r} stdout={stdout!r}'
            if attempt == 0:
                time.sleep(HIMALAYA_RETRY_DELAY_SECONDS)
                continue
            raise
    raise RuntimeError(f'himalaya failed for {src} -> {dst}: {last_error}')


def move(src, dst, mid):
    if not APPLY:
        return
    _run_himalaya(['himalaya','message','move','-a',ACCOUNT,'-f',src,dst,str(mid)], src, dst)


def copy_to_label(src, label, mid):
    if not APPLY:
        return
    _run_himalaya(['himalaya','message','copy','-a',ACCOUNT,'-f',src,f'Labels/{label}',str(mid)], src, f'Labels/{label}')


def has_any(text, needles):
    text=(text or '').lower()
    return any(n in text for n in needles)


def uniq(seq):
    return list(dict.fromkeys(x for x in seq if x))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def append_run_log(data):
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False) + '\n')


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn=sqlite3.connect(DB_PATH)
    conn.row_factory=sqlite3.Row
    conn.execute('''
        CREATE TABLE IF NOT EXISTS queue (
            id TEXT PRIMARY KEY,
            subject TEXT,
            sender TEXT,
            date TEXT,
            message_id TEXT,
            detected_at TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            locked_at TEXT,
            last_error TEXT,
            decision_json TEXT,
            applied_at TEXT,
            updated_at TEXT NOT NULL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status)')
    mail_rules.ensure_meta(conn)
    return conn


def queue_counts(conn):
    counts={}
    for status in ('needs_llm','manual_review','applied','llm_in_progress','pending_apply','gone'):
        counts[status]=conn.execute('SELECT COUNT(*) FROM queue WHERE status=?',(status,)).fetchone()[0]
    return counts


def deterministic_classify(conn, msg, confirmed_only=True):
    # MAILF-013 + MAILF-014 (t 2026-09-18): jediná implementace v mail_rules.
    # confirmed_only=True → aplikují se jen pravidla review_status='ok' (stejná
    # politika jako druhý pass); dřív triage brala i 'pending' pravidla.
    subj=(msg.get('subject') or '').lower()
    sender=((msg.get('from') or {}).get('addr') or '').lower()
    # příjemce (první To: adresa) — t (2026-08-12): odesílatel sám nestačí
    to_list = msg.get('to') or []
    recipient = ''
    if isinstance(to_list, list) and to_list:
        first = to_list[0]
        recipient = ((first.get('addr') if isinstance(first, dict) else str(first)) or '').lower()
    elif isinstance(to_list, dict):
        recipient = ((to_list.get('addr')) or '').lower()
    return mail_rules.classify_message(conn, subject=subj, sender=sender,
                                       recipient=recipient, confirmed_only=confirmed_only)


def load_rule_excerpt():
    try:
        return mail_rules.RULES_PATH.read_text(encoding='utf-8')[:4000]
    except Exception:
        return ''


def enqueue_candidate(conn, msg):
    """Zařadí mail do LLM fronty.

    MAILF-010 (t 2026-09-18): dřív se 'applied' odmítalo vždy → no-op maily
    (rozhodnutí folder==INBOX) se už NIKDY nepřehodnotily, i když vznikla nová
    pravidla. Teď se no-op 'applied' re-queue-uje, ale jen když se změní
    model_version (bump při změně pravidel) — žádný churn každých 15 min.
    """
    mid=str(msg['id'])
    row=conn.execute('SELECT status, decision_json FROM queue WHERE id=?',(mid,)).fetchone()
    if row:
        if row['status'] in {'needs_llm','llm_in_progress','manual_review'}:
            return False
        if row['status'] in {'applied','pending_apply'}:
            try:
                dec=json.loads(row['decision_json'] or '{}')
            except Exception:
                dec={}
            if row['status']=='pending_apply':
                # rozhodnutí čeká na APPLY=1 — přehodnotit jen při změně modelu
                if dec.get('model_version') == mail_rules.get_model_version(conn):
                    return False
            elif dec.get('folder') != 'INBOX':
                # skutečně zařazený mail → nechat být
                return False
            # no-op (zůstal v INBOX) → přehodnotit jen při změně klasifikačního modelu
            elif dec.get('model_version') == mail_rules.get_model_version(conn):
                return False
    ts=now_iso()
    message_id = msg.get('message_id') or msg.get('messageId') or None
    conn.execute('''
        INSERT INTO queue (id, subject, sender, date, message_id, detected_at, status, attempts, locked_at, last_error, decision_json, applied_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'needs_llm', 0, NULL, NULL, NULL, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET
            subject=excluded.subject,
            sender=excluded.sender,
            date=excluded.date,
            message_id=excluded.message_id,
            detected_at=excluded.detected_at,
            status='needs_llm',
            locked_at=NULL,
            last_error=NULL,
            updated_at=excluded.updated_at
    ''',(mid, msg.get('subject'), (msg.get('from') or {}).get('addr'), msg.get('date'), message_id, ts, ts))
    conn.commit()
    return True


def main():
    processed=[]
    errors=[]
    llm_candidates=[]
    enqueued_count=0
    conn=db_connect()

    # t (2026-08-25): maily v INBOX s labelem vyresit = už označené k ručnímu řešení;
    # pokud na ně stále nesedí pravidlo, přeskočit (žádný re-label/re-enqueue každých 15 min)
    try:
        vyresit_ids = {str(m['id']) for m in list_env('Labels/vyresit')}
    except Exception:
        vyresit_ids = set()

    try:
        inbox=list_env_all('INBOX', page_size=HIMALAYA_PAGE_SIZE, max_pages=HIMALAYA_MAX_PAGES)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        counts=queue_counts(conn)
        if isinstance(e, subprocess.TimeoutExpired):
            imap_error = f'himalaya timeout after {HIMALAYA_TIMEOUT_SECONDS}s x2'
        else:
            imap_error = f'himalaya inbox read failed: returncode={e.returncode}'
        state={
            'account':ACCOUNT,
            'processedAt': now_iso(),
            'inbox_count': None,
            'deterministic_processed_count': 0,
            'llm_candidate_count': 0,
            'needs_llm': counts['needs_llm'] > 0,
            'processed': [],
            'llm_candidates': [],
            'errors':[{'stage':'list_env','error':imap_error,'detail':str(e)}],
            'targets':{'label_application':'copy message into existing Labels/<name> mailbox'},
            'queue': {
                'db_path': str(DB_PATH),
                'enqueued_this_run': 0,
                'pending': counts['needs_llm'],
                'manual_review': counts['manual_review'],
                'applied': counts['applied'],
                'in_progress': counts['llm_in_progress'],
            },
            'rule_excerpt_used': '',
            'note': 'imap timeout before inbox read',
        }
        save_json(STATE_PATH, state)
        append_run_log(state)
        conn.close()
        print(json.dumps({'status':'imap_error','errors':1,'llm_needed':counts['needs_llm'] > 0}, ensure_ascii=False))
        raise SystemExit(1)

    for msg in sorted(inbox, key=lambda m:int(m['id'])):
        decision = deterministic_classify(conn, msg)
        mid=msg['id']
        folder=decision['folder']
        if folder is None:
            # t (2026-09-01): žádné pravidlo → mail zůstává v INBOX BEZ automatického labelu vyresit;
            # label vyresit je vyhrazený jen pro emaily, kterým ho pravidlo přiřadí explicitně
            if mid in vyresit_ids:
                # už má label vyresit z pravidla a stále bez folderu → nechat být
                continue
            # aplikovat jen labely, které pravidlo skutečně přiřadilo (žádný implicitní vyresit)
            for label in decision['proton_labels']:
                try:
                    copy_to_label('INBOX', label, mid)
                except subprocess.CalledProcessError as e:
                    errors.append({'id':mid,'subject':msg.get('subject'),'label':label,'error':str(e)})
            if enqueue_candidate(conn, msg):
                enqueued_count += 1
            llm_candidates.append({
                'id': msg.get('id'),
                'subject': msg.get('subject'),
                'from': (msg.get('from') or {}).get('addr'),
                'date': msg.get('date'),
                'queue_status': 'needs_llm',
                'llmLabelFolder': None,
            })
            continue

        semantic_labels=decision['semantic_labels']
        proton_labels=decision['proton_labels']
        for label in proton_labels:
            try:
                copy_to_label('INBOX', label, mid)
            except subprocess.CalledProcessError as e:
                errors.append({'id':mid,'subject':msg.get('subject'),'label':label,'error':str(e)})
        move('INBOX', folder, mid)
        processed.append({
            'id': mid,
            'subject': msg.get('subject'),
            'from': (msg.get('from') or {}).get('addr'),
            'primaryFolder': folder,
            'labels': semantic_labels,
            'labelFolders': [f'Labels/{x}' for x in proton_labels],
            'decisionType': 'deterministic',
            'reason': decision['reason'],
        })

    counts=queue_counts(conn)
    conn.close()
    state={
        'account':ACCOUNT,
        'processedAt': now_iso(),
        'inbox_count': len(inbox),
        'deterministic_processed_count': len(processed),
        'llm_candidate_count': len(llm_candidates),
        'needs_llm': bool(llm_candidates),
        'processed':processed,
        'llm_candidates': llm_candidates,
        'errors':errors,
        'targets':{'label_application':'copy message into existing Labels/<name> mailbox'},
        'queue': {
            'db_path': str(DB_PATH),
            'enqueued_this_run': enqueued_count,
            'pending': counts['needs_llm'],
            'manual_review': counts['manual_review'],
            'applied': counts['applied'],
            'in_progress': counts['llm_in_progress'],
        },
        'rule_excerpt_used': load_rule_excerpt(),
    }
    save_json(STATE_PATH, state)
    append_run_log(state)

    if len(inbox) == 0:
        print(json.dumps({'status':'no_new_mail','processed_count':0,'errors':len(errors),'llm_needed':False}, ensure_ascii=False))
        return

    if not llm_candidates:
        print(json.dumps({'status':'deterministic_only','processed_count':len(processed),'errors':len(errors),'llm_needed':False}, ensure_ascii=False))
        return

    print(json.dumps({
        'status':'llm_needed',
        'processed_count':len(processed),
        'llm_candidate_count':len(llm_candidates),
        'enqueued_count': enqueued_count,
        'errors':len(errors),
        'llm_needed':True,
        'candidates': llm_candidates,
    }, ensure_ascii=False))

if __name__=='__main__':
    main()
