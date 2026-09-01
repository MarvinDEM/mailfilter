#!/usr/bin/env python3
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT='bezouska'
ROOT=Path('/root/.openclaw/workspace')
DB_PATH=ROOT/'state'/'bezouska-llm-queue.sqlite3'
STATE_PATH=ROOT/'state'/'bezouska-mail-triage.json'
RUN_LOG_PATH=ROOT/'state'/'bezouska-llm-second-pass-runs.jsonl'
REQUIRED_LABEL=None

HIMALAYA_TIMEOUT_SECONDS=45

NEWSLETTER_SENDERS={
    'patrick@vibecoding.cz',
    'hi@mail.benmeer.com',
    'hello@carnimeal.com',
    'newsletter@asociace.ai',
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
    'info@supertip.cz',
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
KEYWORD_TRANSACTION=['billing','payment','invoice','subscription','receipt','renew','renewal','order','objednávka','objednavka','faktura','výpis z účtu','vypis z uctu','výpis k hypotéce','vypis k hypotéce','vyúčtování','vyuctovani','připomenutí výzvy','pripomenuti vyzvy','výzva k platbě','vyzva k platbe','platební výzva','platba']
KEYWORD_ALERT=['battery','warning','alert','upozornění','upozorneni','response required','action required','needs your response','out of credits','přihlásil jste se právě','prihlasil jste se prave']
KEYWORD_IPSD=['ipsd','eximex','indoc','veřejných zakáz','verejnych zakaz']
KEYWORD_NEWSLETTER=['newsletter','digest','novinky','weekly','monthly','connect 2026','che succede']
KEYWORD_DOMAIN_ADMIN=['dns','domény','domeny','domény ','registrace','prihlášení do webglobe','prihlaseni do webglobe']


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run(*args):
    return subprocess.check_output(list(args), text=True, timeout=HIMALAYA_TIMEOUT_SECONDS)


def list_env(folder='INBOX', page_size='500'):
    return json.loads(run('himalaya','envelope','list','-a',ACCOUNT,'-f',folder,'--page-size',page_size,'--output','json'))


def move(src, dst, mid):
    subprocess.run(['himalaya','message','move','-a',ACCOUNT,'-f',src,dst,str(mid)], check=True, capture_output=True, text=True)


def copy_to_label(src, label, mid):
    subprocess.run(['himalaya','message','copy','-a',ACCOUNT,'-f',src,f'Labels/{label}',str(mid)], check=True, capture_output=True, text=True)


def has_any(text, needles):
    text=(text or '').lower()
    return any(n in text for n in needles)


def uniq(seq):
    return list(dict.fromkeys(x for x in seq if x))


def db_connect():
    conn=sqlite3.connect(DB_PATH)
    conn.row_factory=sqlite3.Row
    return conn


def queue_counts(conn):
    counts={}
    for status in ('needs_llm','manual_review','applied','llm_in_progress'):
        counts[status]=conn.execute('SELECT COUNT(*) FROM queue WHERE status=?',(status,)).fetchone()[0]
    return counts


def learned_rule_lookup(conn, sender, recipient=None, subject=None):
    """Nejkonkrétnější shoda plného pravidla (sender × recipient × subject_pattern).
    FIX 2026-08-24 (t): automatika jela jen podle senderu a ignorovala subject_pattern
    — pravidlo s předmětem se nikdy nepoužilo. Delegováno na mail_rules.learned_rule_lookup
    (precizní > wildcard > libovolný, subject > bez subjectu, jen review_status='ok')."""
    import mail_rules
    try:
        return mail_rules.learned_rule_lookup(conn, sender, recipient, subject, confirmed_only=True)
    except Exception:
        return None


def classify_folder(subj, sender):
    if 'calendar.proton.me' in sender or sender == 'no-reply@calendar.proton.me':
        return 'Folders/10_osobni/11_tomas', 'proton-calendar-notification'
    if sender == 'notifications@fibaro.com' or 'fibaro' in sender:
        return 'Folders/10_osobni/31_zvole', 'fibaro-alert'
    if sender in NEWSLETTER_SENDERS or 'newsletter' in sender or ('bloomberg' in sender and 'news' in sender) or 'substack.com' in sender or 'convertkit' in sender or 'novinky.' in sender or 'promo' in sender:
        return 'Folders/90_ostatni/91_newsletter', 'newsletter-signal'
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
    if '@eximex.cz' in sender or '@ipsd.cz' in sender or 'info@indoc.cz' in sender or has_any(subj, KEYWORD_IPSD):
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
    return None, 'fallback-low-confidence'


def collect_labels(subj, sender):
    proton_labels=[]

    if 'calendar.proton.me' in sender or sender == 'no-reply@calendar.proton.me' or sender == 'news@ana-white.com' or 'mojeid' in sender or sender == 'podpora@mojeid.cz' or '@bezouskova.cz' in sender:
        proton_labels += ['50_osobni']

    if sender == 'notifications@fibaro.com' or 'fibaro' in sender or sender in {'noreply@business-updates.facebook.com', 'security@facebookmail.com'} or 'facebookmail.com' in sender or 'business-updates.facebook.com' in sender or sender == 'podpora@nic.cz' or 'nic.cz' in sender or has_any(subj, KEYWORD_DOMAIN_ADMIN) or has_any(subj, KEYWORD_ALERT):
        proton_labels += ['vyresit']

    if sender in NEWSLETTER_SENDERS or 'newsletter' in sender or ('bloomberg' in sender and 'news' in sender) or 'substack.com' in sender or 'convertkit' in sender or 'novinky.' in sender or 'promo' in sender or sender == 'info@sparovky.eu' or has_any(subj, KEYWORD_NEWSLETTER) or has_any(sender, ['klaviyomail.com','convertkit-mail','linkedin.com','smartemailing.cz','smartsupp.email']):
        proton_labels += ['newsletter']

    is_transaction = (
        ('mojeid' in sender or sender == 'podpora@mojeid.cz' or '@bezouskova.cz' in sender) and has_any(subj, KEYWORD_TRANSACTION)
    ) or sender in PERSONAL_TRANSACTION_SENDERS or has_any(sender, ['subscriptions_at_message_bloomberg_com_', 'gpwebpay@b2b.gpe.cz']) or has_any(subj, KEYWORD_TRANSACTION)
    if is_transaction:
        proton_labels += ['faktury', '00_platby']

    if 'hypotecni.zona@csobhypotecni.cz' in sender or 'rb.cz' in sender or 'airbank.cz' in sender:
        proton_labels += ['finance']

    if '@eximex.cz' in sender or '@ipsd.cz' in sender or 'info@indoc.cz' in sender or has_any(subj, KEYWORD_IPSD):
        proton_labels += ['03_ipsd']
        if has_any(subj, ['žádost', 'zadost', 'chybějící', 'chybejici']):
            proton_labels += ['vyresit']

    for domain, _mapped_folder, extra_labels in WORK_DOMAIN_MAP:
        if domain in sender:
            proton_labels += extra_labels
            break

    return uniq(proton_labels)


def classify_pending(conn, subject, sender, recipient=None):
    subj=(subject or '').lower()
    sender=(sender or '').lower()

    learned=learned_rule_lookup(conn, sender, recipient, subj)
    folder, reason = classify_folder(subj, sender)
    proton_labels = collect_labels(subj, sender)
    if learned is not None:
        folder = learned.get('folder') or folder
        proton_labels = uniq(proton_labels + learned.get('proton_labels', []))
        reason = learned.get('reason') or reason
    return {'folder':folder,'proton_labels':proton_labels,'reason':reason,'confidence':0.9 if learned is not None else 0.7}


def load_state():
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding='utf-8'))


def save_state(summary):
    state=load_state()
    state['llm_second_pass']=summary
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def append_run_log(summary):
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(summary, ensure_ascii=False) + '\n')


def main():
    conn=db_connect()
    pending=conn.execute("select id, subject, sender, message_id, attempts from queue where status='needs_llm' order by updated_at asc, id asc").fetchall()

    if not pending:
        summary={
            'timestamp': now_iso(),
            'pending_before': 0,
            'applied_count': 0,
            'failed_count': 0,
            'skipped_count': 0,
            'applied': [],
            'failed': [],
            'skipped': [],
            'queue_counts': queue_counts(conn),
            'inbox_remaining': None,
            'note': 'queue empty; skipped IMAP fetch',
        }
        conn.close()
        save_state(summary)
        append_run_log(summary)
        print(json.dumps(summary, ensure_ascii=False))
        return

    inbox_by_id={str(m['id']): m for m in list_env('INBOX')}

    applied=[]
    failed=[]
    skipped=[]

    for row in pending:
        local_id=str(row['id'])
        msg=inbox_by_id.get(local_id)
        if msg is None:
            conn.execute("update queue set status='manual_review', attempts=attempts+1, last_error=?, locked_at=NULL, updated_at=? where id=?", ('message not found in INBOX', now_iso(), local_id))
            failed.append({'id': local_id, 'message_id': row['message_id'], 'error': 'message not found in INBOX', 'status': 'manual_review'})
            continue

        # recipient z envelope (pro plný match pravidel — sender+recipient+subject, t 2026-08-24)
        to_list = msg.get('to') or []
        recipient = ''
        if isinstance(to_list, list) and to_list:
            first = to_list[0]
            recipient = ((first.get('addr') if isinstance(first, dict) else str(first)) or '').lower()
        elif isinstance(to_list, dict):
            recipient = (to_list.get('addr') or '').lower()
        decision=classify_pending(conn, row['subject'], row['sender'], recipient)
        try:
            labels = list(dict.fromkeys(decision['proton_labels']))
            for label in labels:
                copy_to_label('INBOX', label, local_id)
            if decision['folder'] is None:
                # t (2026-09-01): žádné pravidlo → mail zůstává v INBOX BEZ automatického labelu vyresit;
                # labely aplikované výše jsou jen ty, které rozhodnutí přiřadilo explicitně
                payload={
                    'id': local_id,
                    'message_id': row['message_id'],
                    'folder': 'INBOX',
                    'labels': labels,
                    'reason': decision['reason'],
                    'confidence': decision['confidence'],
                    'note': 'no rule match — zůstává v INBOX',
                }
            else:
                move('INBOX', decision['folder'], local_id)
                payload={
                    'id': local_id,
                    'message_id': row['message_id'],
                    'folder': decision['folder'],
                    'labels': labels,
                    'reason': decision['reason'],
                    'confidence': decision['confidence'],
                }
            conn.execute("update queue set status='applied', decision_json=?, applied_at=?, locked_at=NULL, last_error=NULL, updated_at=? where id=?", (json.dumps(payload, ensure_ascii=False), now_iso(), now_iso(), local_id))
            applied.append(payload)
        except Exception as e:
            attempts=(row['attempts'] or 0) + 1
            new_status='manual_review' if attempts >= 3 else 'needs_llm'
            conn.execute("update queue set status=?, attempts=?, last_error=?, locked_at=NULL, updated_at=? where id=?", (new_status, attempts, str(e), now_iso(), local_id))
            failed.append({'id': local_id, 'message_id': row['message_id'], 'error': str(e), 'status': new_status})

    conn.commit()
    summary={
        'timestamp': now_iso(),
        'pending_before': len(pending),
        'applied_count': len(applied),
        'failed_count': len(failed),
        'skipped_count': len(skipped),
        'applied': applied,
        'failed': failed,
        'skipped': skipped,
        'queue_counts': queue_counts(conn),
        'inbox_remaining': len(list_env('INBOX')),
    }
    conn.close()
    save_state(summary)
    append_run_log(summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
