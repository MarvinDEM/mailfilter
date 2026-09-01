#!/usr/bin/env python3
import json
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
STATE_PATH=ROOT/'state'/'bezouska-mail-triage.json'
DB_PATH=ROOT/'state'/'bezouska-llm-queue.sqlite3'
RULES_PATH=ROOT/'mail-sorting-rules.md'
RUN_LOG_PATH=ROOT/'state'/'bezouska-mail-triage-runs.jsonl'
HIMALAYA_TIMEOUT_SECONDS=45
HIMALAYA_RETRY_DELAY_SECONDS=5

NEWSLETTER_SENDERS={
    'newsletter@asociace.ai',
    'magazin@egovernment.cz',
    'bingo@patreon.com',
    'contact@blacktailstudio.com',
    'insidercz@substack.com',
    'e-resident@gov.ee',
    'team@mails.zeleznakoule.cz',
    'novinky@software.602.cz',
    'update@digital.metamail.com',
    'news@quotidiano.idealista.it',
    'hi@plaud.ai',
    'team@mail.perplexity.ai',
}
PERSONAL_TRANSACTION_SENDERS={
    'no-reply@revolut.com',
    'support@foreignaffairs.com',
    'payments@comgate.cz',
    'faktura@nordictelecom.cz',
    'info@nordictelecom.cz',
    'fakturace@webglobe.cz',
    'no_reply@email.apple.com',
    'no-reply@notify.proton.me',
    'payments-noreply@google.com',
    'hypotecni.zona@csobhypotecni.cz',
}
WORK_DOMAIN_MAP=[
    ('@bezouska.cz', 'Folders/50_pracovni/51_bezouska', []),
    ('@inadvisors.cz', 'Folders/50_pracovni/52_inadvisors', []),
    ('@ipsd.cz', 'Folders/50_pracovni/53_ipsd', ['03_ipsd']),
    ('@mmr.gov.cz', 'Folders/50_pracovni/54_mmr', []),
    ('@prazske-noviny.cz', 'Folders/50_pracovni/70_prazske-noviny', []),
    ('@deltaadvisory.cz', 'Folders/50_pracovni/80_delta', []),
]
KEYWORD_TRANSACTION=['billing','payment','invoice','subscription','receipt','renew','renewal','order','objednávka','objednavka','faktura','výpis z účtu','vypis z uctu','výpis k hypotéce','vypis k hypotéce','vyúčtování','vyuctovani','připomenutí výzvy','pripomenuti vyzvy','výzva k platbě','vyzva k platbe','platební výzva']
KEYWORD_ALERT=['battery','warning','alert','upozornění','upozorneni','action required','needs your response','out of credits','přihlásil jste se právě','prihlasil jste se prave']
KEYWORD_IPSD=['ipsd','eximex','indoc','veřejných zakáz','verejnych zakaz']
KEYWORD_NEWSLETTER=['newsletter','digest','novinky','weekly','monthly','connect 2026','che succede']
KEYWORD_DOMAIN_ADMIN=['dns','domény','domeny','domény ','registrace','prihlášení do webglobe','prihlaseni do webglobe']


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
    _run_himalaya(['himalaya','message','move','-a',ACCOUNT,'-f',src,dst,str(mid)], src, dst)


def copy_to_label(src, label, mid):
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
    return conn


def queue_counts(conn):
    counts={}
    for status in ('needs_llm','manual_review','applied','llm_in_progress'):
        counts[status]=conn.execute('SELECT COUNT(*) FROM queue WHERE status=?',(status,)).fetchone()[0]
    return counts


def classify_folder(subj, sender):
    if 'calendar.proton.me' in sender or sender == 'no-reply@calendar.proton.me':
        return 'Folders/10_osobni/11_tomas', 'proton-calendar-notification'
    if sender == 'notifications@fibaro.com' or 'fibaro' in sender:
        return 'Folders/10_osobni/31_zvole', 'fibaro-alert'
    if sender in NEWSLETTER_SENDERS or 'newsletter' in sender or ('bloomberg' in sender and 'news' in sender) or 'substack.com' in sender or 'convertkit' in sender or 'novinky.' in sender or 'promo' in sender:
        return 'Folders/90_ostatni/91_newsletter', 'newsletter-sender'
    if sender == 'news@ana-white.com':
        return 'Folders/10_osobni/11_tomas', 'known-personal-sender'
    if 'mojeid' in sender or sender == 'podpora@mojeid.cz' or '@bezouskova.cz' in sender:
        if has_any(subj, KEYWORD_TRANSACTION):
            return 'Folders/90_ostatni/92_transakce', 'personal-transaction-sender'
        return 'Folders/10_osobni/11_tomas', 'personal-service-sender'
    if sender in PERSONAL_TRANSACTION_SENDERS or has_any(sender, ['subscriptions_at_message_bloomberg_com_', 'gpwebpay@b2b.gpe.cz']):
        return 'Folders/90_ostatni/92_transakce', 'known-transaction-sender'
    if sender in {'noreply@business-updates.facebook.com', 'security@facebookmail.com'} or 'facebookmail.com' in sender or 'business-updates.facebook.com' in sender:
        return None, 'facebook-security-or-business-alert'
    if sender == 'podpora@nic.cz' or 'nic.cz' in sender:
        return None, 'domain-admin-alert'
    if 'webglobe.cz' in sender:
        if has_any(subj, KEYWORD_TRANSACTION):
            return 'Folders/90_ostatni/92_transakce', 'webglobe-billing'
        return None, 'webglobe-admin-alert'
    if sender == 'info@sparovky.eu':
        return 'Folders/90_ostatni/91_newsletter', 'ecommerce-newsletter'
    if '@eximex.cz' in sender or '@ipsd.cz' in sender or has_any(sender, ['info@indoc.cz']) or has_any(subj, KEYWORD_IPSD):
        return 'Folders/50_pracovni/53_ipsd', 'ipsd-signal'
    for domain, mapped_folder, _extra_labels in WORK_DOMAIN_MAP:
        if domain in sender:
            return mapped_folder, f'work-domain:{domain}'
    if '.gov.cz' in sender or '.mvcr.cz' in sender or '.mfcr.cz' in sender:
        return None, 'gov-cz-domain'
    if has_any(subj, KEYWORD_NEWSLETTER) or has_any(sender, ['klaviyomail.com','convertkit-mail','linkedin.com','smartemailing.cz','smartsupp.email']):
        return 'Folders/90_ostatni/91_newsletter', 'newsletter-pattern'
    if has_any(subj, KEYWORD_TRANSACTION):
        return 'Folders/90_ostatni/92_transakce', 'transaction-keyword'
    if has_any(subj, KEYWORD_DOMAIN_ADMIN):
        return None, 'domain-admin-keyword'
    if has_any(subj, KEYWORD_ALERT):
        return None, 'generic-alert'
    return None, 'fallback-unclassified'


def collect_labels(subj, sender):
    semantic=[]
    proton_labels=[]

    if 'calendar.proton.me' in sender or sender == 'no-reply@calendar.proton.me' or sender == 'news@ana-white.com' or 'mojeid' in sender or sender == 'podpora@mojeid.cz' or '@bezouskova.cz' in sender:
        semantic += ['personal']
        proton_labels += ['50_osobni']

    if sender == 'notifications@fibaro.com' or 'fibaro' in sender or sender in {'noreply@business-updates.facebook.com', 'security@facebookmail.com'} or 'facebookmail.com' in sender or 'business-updates.facebook.com' in sender or sender == 'podpora@nic.cz' or 'nic.cz' in sender or has_any(subj, KEYWORD_DOMAIN_ADMIN) or has_any(subj, KEYWORD_ALERT):
        semantic += ['alerts', '00_vyresit']
        proton_labels += ['vyresit']

    if sender in NEWSLETTER_SENDERS or 'newsletter' in sender or ('bloomberg' in sender and 'news' in sender) or 'substack.com' in sender or 'convertkit' in sender or 'novinky.' in sender or 'promo' in sender or sender == 'info@sparovky.eu' or has_any(subj, KEYWORD_NEWSLETTER) or has_any(sender, ['klaviyomail.com','convertkit-mail','linkedin.com','smartemailing.cz','smartsupp.email']):
        semantic += ['newsletters']
        proton_labels += ['newsletter']

    is_transaction = (
        ('mojeid' in sender or sender == 'podpora@mojeid.cz' or '@bezouskova.cz' in sender) and has_any(subj, KEYWORD_TRANSACTION)
    ) or sender in PERSONAL_TRANSACTION_SENDERS or has_any(sender, ['subscriptions_at_message_bloomberg_com_', 'gpwebpay@b2b.gpe.cz']) or has_any(subj, KEYWORD_TRANSACTION)
    if is_transaction:
        semantic += ['transactions']
        proton_labels += ['faktury', '00_platby']

    if 'hypotecni.zona@csobhypotecni.cz' in sender or 'rb.cz' in sender or 'airbank.cz' in sender:
        semantic += ['finance']
        proton_labels += ['finance']

    if '@eximex.cz' in sender or '@ipsd.cz' in sender or has_any(sender, ['info@indoc.cz']) or has_any(subj, KEYWORD_IPSD):
        semantic += ['work']
        proton_labels += ['03_ipsd']
        if has_any(subj, ['žádost', 'zadost', 'chybějící', 'chybejici']):
            semantic += ['00_vyresit']
            proton_labels += ['vyresit']

    for domain, _mapped_folder, extra_labels in WORK_DOMAIN_MAP:
        if domain in sender:
            semantic += ['work']
            proton_labels += extra_labels
            break

    if '.gov.cz' in sender or '.mvcr.cz' in sender or '.mfcr.cz' in sender:
        semantic += ['work']

    return uniq(semantic), uniq(proton_labels)


def deterministic_classify(conn, msg, confirmed_only=False):
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
    learned=mail_rules.learned_rule_lookup(conn, sender, recipient, subj, confirmed_only=confirmed_only)
    folder, folder_reason = classify_folder(subj, sender)
    semantic, proton_labels = collect_labels(subj, sender)
    reason = folder_reason

    if learned is not None:
        folder = learned.get('folder') or folder
        proton_labels = uniq(proton_labels + learned.get('proton_labels', []))
        semantic = uniq(semantic + learned.get('semantic_labels', []))
        reason = learned.get('reason') or reason

    return {
        'semantic_labels': semantic,
        'proton_labels': proton_labels,
        'folder': folder,
        'reason': reason,
    }


def load_rule_excerpt():
    try:
        return RULES_PATH.read_text(encoding='utf-8')[:4000]
    except Exception:
        return ''


def enqueue_candidate(conn, msg):
    mid=str(msg['id'])
    row=conn.execute('SELECT status FROM queue WHERE id=?',(mid,)).fetchone()
    if row and row['status'] in {'needs_llm','llm_in_progress','applied'}:
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
        inbox=list_env('INBOX')
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
        if decision is None:
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
