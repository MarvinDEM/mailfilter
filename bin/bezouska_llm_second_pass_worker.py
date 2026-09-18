#!/usr/bin/env python3
"""Druhý pass třídění pošty accountu bezouska.

Historie (t 2026-09-18, MAILF-001/011/013/014/015):
  - Původně to byl jen hardcoded klon heuristiky z triage — ŽÁDNÉ LLM nevolalo
    (dokumentace o "modelovém passu" byla fikce).
  - Teď: klasifikace je v `mail_rules.py` (single source of truth, MAILF-013),
    INBOX se čte po stránkách (MAILF-011), a pokud heuristika nic nevybere,
    volá se REÁLNĚ levný model (deepseek-v4-flash přes lokální LiteLLM router)
    v přísně omezeném režimu (MAILF-001): batch po MAX_LLM_BATCH zprávách,
    MAX_LLM_CALLS_PER_RUN volání za běh, MAX_MESSAGES_PER_RUN zpráv za běh.
  - Z vysoko-konfidenčních LLM rozhodnutí se navrhují nová pravidla ve stavu
    'pending' k odsouhlasení (MAILF-015) — model sám NIKDY nezapisuje aktivní
    pravidlo.

Kredity: LLM je zapnutý jen s `MAILFILTER_LLM_ENABLED=1` (default 1), dry-run
`MAILFILTER_LLM_DRYRUN=1`. Mailbox se mění jen s `MAILFILTER_APPLY=1`.
"""
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_rules

ACCOUNT = 'bezouska'
ROOT = Path('/root/.openclaw/workspace')
DB_PATH = mail_rules.DB_PATH
STATE_PATH = Path(os.environ.get('MAILFILTER_STATE_PATH2', str(ROOT / 'state' / 'bezouska-mail-triage.json')))
RUN_LOG_PATH = Path(os.environ.get('MAILFILTER_SP_RUN_LOG', str(ROOT / 'state' / 'bezouska-llm-second-pass-runs.jsonl')))
REQUIRED_LABEL = None

HIMALAYA_TIMEOUT_SECONDS = 45
HIMALAYA_PAGE_SIZE = 500
HIMALAYA_MAX_PAGES = 20

APPLY = os.environ.get('MAILFILTER_APPLY', '0') == '1'
LLM_ENABLED = os.environ.get('MAILFILTER_LLM_ENABLED', '1') == '1'
LLM_DRYRUN = os.environ.get('MAILFILTER_LLM_DRYRUN', '0') == '1'
MAX_LLM_CALLS_PER_RUN = int(os.environ.get('MAILFILTER_MAX_LLM_CALLS', '2'))
MAX_MESSAGES_PER_RUN = int(os.environ.get('MAILFILTER_MAX_MESSAGES', '20'))
MAX_LLM_BATCH = int(os.environ.get('MAILFILTER_LLM_BATCH', '20'))

LLM_MODEL = os.environ.get('MAILFILTER_LLM_MODEL', 'deepseek/deepseek-v4-flash')
LITELLM_BASE = os.environ.get('MAILFILTER_LITELLM_BASE', 'http://127.0.0.1:4000/v1')
LITELLM_ENV_PATH = ROOT / 'state' / 'litellm-router' / 'litellm-router.env'
CONFIDENCE_THRESHOLD = 0.8

ALLOWED_FOLDERS = [
    'Folders/00_marvin',
    'Folders/10_osobni/11_tomas',
    'Folders/10_osobni/12_andrejka',
    'Folders/10_osobni/13_rodina',
    'Folders/10_osobni/20_zvirata',
    'Folders/10_osobni/31_zvole',
    'Folders/10_osobni/32_imrychova',
    'Folders/10_osobni/33_zahalka',
    'Folders/10_osobni/35_italie',
    'Folders/50_pracovni/51_bezouska',
    'Folders/50_pracovni/52_inadvisors',
    'Folders/50_pracovni/53_ipsd',
    'Folders/50_pracovni/54_mmr',
    'Folders/50_pracovni/60_domekumore',
    'Folders/50_pracovni/70_prazske-noviny',
    'Folders/50_pracovni/80_delta',
    'Folders/90_ostatni/91_newsletter',
    'Folders/90_ostatni/92_transakce',
    'Folders/90_ostatni/93_knowhow',
    'Folders/90_ostatni/94_notifikace',
    'Folders/90_ostatni/95_registrace',
    'Folders/90_ostatni/96_spammers_fun',
    'Folders/90_ostatni/97_pozvanky',
    'Folders/99_nezatrideno',
]
FOLDERS_PATH = ROOT / 'state' / 'mailfilter-folders.json'


def _load_folders():
    """Aktuální seznam složek z mailfilter-folders.json (refreshuje denní cron).

    Model smí vybrat JEN existující složku; při chybě/absenci fallback na
    statický seznam výše."""
    try:
        data = json.loads(FOLDERS_PATH.read_text(encoding='utf-8'))
        folders = [f for f in data.get('folders', []) if f.count('/') >= 2]
        if folders:
            return folders
    except Exception:
        pass
    return ALLOWED_FOLDERS


ALLOWED_FOLDERS = _load_folders()

PROMPT_TEMPLATE = (
    "Jsi třídič pošty. Pro každý e-mail níže vyber JEDNU cílovou složku z tohoto "
    "seznamu (přesná hodnota, nebo null pokud si nejsi jistý):\n"
    "{folders}\n\n"
    "Vrať POUZE JSON pole objektů [{{\"id\": <id>, \"folder\": <string|null>, "
    "\"labels\": [<string>], \"confidence\": <0..1>}}]. Bez vysvětlování.\n\n"
    "E-maily:\n{items}"
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run(*args):
    return subprocess.check_output(list(args), text=True, timeout=HIMALAYA_TIMEOUT_SECONDS)


def list_env(folder='INBOX', page_size=None, max_pages=None):
    """MAILF-011: projde celý mailbox po stránkách himalaya --page."""
    page_size = int(page_size or HIMALAYA_PAGE_SIZE)
    max_pages = int(max_pages or HIMALAYA_MAX_PAGES)
    out = []
    for page in range(1, max_pages + 1):
        try:
            batch = json.loads(run('himalaya', 'envelope', 'list', '-a', ACCOUNT, '-f', folder,
                                   '--page-size', str(page_size), '--page', str(page),
                                   '--output', 'json'))
        except subprocess.CalledProcessError:
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
    return out


def move(src, dst, mid):
    if not APPLY:
        return False
    subprocess.run(['himalaya', 'message', 'move', '-a', ACCOUNT, '-f', src, dst, str(mid)],
                   check=True, capture_output=True, text=True)
    return True


def copy_to_label(src, label, mid):
    if not APPLY:
        return False
    subprocess.run(['himalaya', 'message', 'copy', '-a', ACCOUNT, '-f', src, f'Labels/{label}', str(mid)],
                   check=True, capture_output=True, text=True)
    return True


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    mail_rules.ensure_meta(conn)
    return conn


def queue_counts(conn):
    return {s: conn.execute('SELECT COUNT(*) FROM queue WHERE status=?', (s,)).fetchone()[0]
            for s in ('needs_llm', 'manual_review', 'applied', 'llm_in_progress', 'gone')}


# ---------------------------------------------------------------------------
# Reálné LLM volání (MAILF-001) — levný model přes lokální LiteLLM router.
# ---------------------------------------------------------------------------

def _litellm_key():
    try:
        for line in LITELLM_ENV_PATH.read_text(encoding='utf-8').splitlines():
            if line.startswith('LITELLM_MASTER_KEY='):
                return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return os.environ.get('LITELLM_MASTER_KEY') or 'sk-local'


def llm_classify(batch):
    """batch: list of {id, subject, from}. Vrací {id: {folder, labels, confidence}}."""
    if not LLM_ENABLED or LLM_DRYRUN:
        return {}
    items = '\n'.join(
        f'- id={m["id"]} | od={m.get("from") or ""} | předmět={m.get("subject") or ""}'
        for m in batch)
    prompt = PROMPT_TEMPLATE.format(folders='\n'.join(ALLOWED_FOLDERS), items=items)
    payload = json.dumps({
        'model': LLM_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0,
        'max_tokens': 1500,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{LITELLM_BASE}/chat/completions', data=payload,
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {_litellm_key()}'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode('utf-8'))
    text = body['choices'][0]['message']['content'].strip()
    if text.startswith('```'):
        text = text.strip('`')
        text = text.split('\n', 1)[1] if '\n' in text else text
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    out = {}
    for d in parsed if isinstance(parsed, list) else []:
        try:
            fid = str(d.get('id'))
            folder = d.get('folder')
            if folder not in ALLOWED_FOLDERS:
                folder = None
            out[fid] = {
                'folder': folder,
                'labels': sanitize_labels(d.get('labels')),
                'confidence': float(d.get('confidence') or 0),
            }
        except Exception:
            continue
    return out


ALLOWED_LABELS = None  # labely nejsou fixní enum — sanitizují se (viz sanitize_labels)


def sanitize_labels(raw):
    """Uklidí labely z LLM: strip, bez '/' a kontrolních znaků, dedupe, max 40 znaků."""
    out = []
    for l in raw or []:
        if not isinstance(l, str):
            continue
        l = l.strip().replace('/', '_')
        if not l or len(l) > 40:
            continue
        if l not in out:
            out.append(l)
    return out


def _alert_quota(err_text):
    """Deterministický Telegram alert při vyčerpaném kreditu/kvótě.

    Na 'Insufficient Balance' / 'insufficient_quota' / HTTP 402 se NESMÍ tiše
    pokračovat — jinak by model pass tiše vracel prázdné výsledky."""
    low = (err_text or '').lower()
    if not any(k in low for k in ('insufficient balance', 'insufficient_quota',
                                  '402', 'quota', 'credit')):
        return False
    try:
        subprocess.run([sys.executable, str(ROOT / 'bin' / 'escalate.py'),
                        '--case', 'mailfilter-llm-quota', '--severity', 'high',
                        '--title', 'MailFilter LLM: vyčerpaný kredit/kvóta',
                        '--detail', f'LLM second pass nemohl klasifikovat: {err_text[:300]}'],
                       check=False, timeout=30)
        return True
    except Exception:
        return False


def propose_rule(conn, sender, folder, labels):
    """MAILF-015: vysoko-konfidenční LLM rozhodnutí → NÁVRH pravidla k review.

    Deleguje na mail_rules.propose_rule — ukládá 'pending' + active=0, takže se
    pravidlo NIKDY neaktivuje bez odsouhlasení t ve webu."""
    try:
        return mail_rules.propose_rule(conn, sender, folder, labels)
    except Exception:
        return None


def load_state():
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding='utf-8'))


def save_state(summary):
    state = load_state()
    state['llm_second_pass'] = summary
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def append_run_log(summary):
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(summary, ensure_ascii=False) + '\n')


def recipient_of(msg):
    to_list = msg.get('to') or []
    if isinstance(to_list, list) and to_list:
        first = to_list[0]
        return ((first.get('addr') if isinstance(first, dict) else str(first)) or '').lower()
    if isinstance(to_list, dict):
        return (to_list.get('addr') or '').lower()
    return ''


def main():
    conn = db_connect()
    pending = conn.execute(
        "select id, subject, sender, message_id, attempts from queue "
        "where status='needs_llm' order by updated_at asc, id asc limit ?",
        (MAX_MESSAGES_PER_RUN,)).fetchall()
    manual_review_n = conn.execute(
        "select count(*) from queue where status='manual_review'").fetchone()[0]
    pending_apply_n = conn.execute(
        "select count(*) from queue where status='pending_apply'").fetchone()[0]

    if not pending and manual_review_n == 0 and not (APPLY and pending_apply_n):
        summary = {
            'timestamp': now_iso(),
            'pending_before': 0, 'applied_count': 0, 'failed_count': 0,
            'skipped_count': 0, 'llm_calls': 0, 'proposed_rules': 0,
            'applied': [], 'failed': [], 'skipped': [],
            'queue_counts': queue_counts(conn), 'inbox_remaining': None,
            'note': 'queue empty; skipped IMAP fetch',
        }
        conn.close()
        save_state(summary)
        append_run_log(summary)
        print(json.dumps(summary, ensure_ascii=False))
        return

    try:
        inbox = list_env('INBOX')
    except Exception as e:
        summary = {
            'timestamp': now_iso(), 'pending_before': len(pending),
            'error': f'inbox read failed: {e}', 'queue_counts': queue_counts(conn),
        }
        conn.close()
        save_state(summary)
        append_run_log(summary)
        print(json.dumps(summary, ensure_ascii=False))
        raise SystemExit(1)

    inbox_by_id = {str(m['id']): m for m in inbox}

    # 1) deterministická/rule-based klasifikace (mail_rules — single source of truth)
    decisions = {}
    unresolved = []
    for row in pending:
        local_id = str(row['id'])
        msg = inbox_by_id.get(local_id)
        if msg is None:
            # MAILF-012: zpráva už v INBOX není → terminální stav 'gone', ne zombie
            conn.execute("update queue set status='gone', attempts=attempts+1, last_error=?, "
                         "locked_at=NULL, updated_at=? where id=?",
                         ('message not found in INBOX', now_iso(), local_id))
            continue
        dec = mail_rules.classify_message(
            conn, subject=row['subject'], sender=row['sender'],
            recipient=recipient_of(msg), confirmed_only=True)
        if dec['folder'] is None and LLM_ENABLED:
            unresolved.append((local_id, row, msg, dec))
        else:
            decisions[local_id] = dec
    conn.commit()

    # 2) LLM dořešení nezařezených (bounded: batch + max calls)
    llm_calls = 0
    proposed = 0
    llm_meta = {}
    for i in range(0, len(unresolved), MAX_LLM_BATCH):
        if llm_calls >= MAX_LLM_CALLS_PER_RUN:
            break
        chunk = unresolved[i:i + MAX_LLM_BATCH]
        batch = [{'id': c[0], 'subject': c[2].get('subject'), 'from': (c[2].get('from') or {}).get('addr')}
                 for c in chunk]
        try:
            llm_calls += 1
            results = llm_classify(batch)
        except Exception as e:
            results = {}
            llm_meta['last_error'] = str(e)[:200]
            if _alert_quota(str(e)):
                llm_meta['quota_alert'] = True
            break
        for local_id, row, msg, dec in chunk:
            r = results.get(local_id) or {}
            if r.get('folder') and r.get('confidence', 0) >= CONFIDENCE_THRESHOLD:
                dec = dict(dec)
                dec['folder'] = r['folder']
                dec['reason'] = 'llm-second-pass'
                dec['proton_labels'] = mail_rules.uniq(dec.get('proton_labels', []) + r.get('labels', []))
                dec['confidence'] = r['confidence']
                dec['llm'] = True
                # MAILF-015: návrh pravidla (pending review)
                if propose_rule(conn, row['sender'], r['folder'], r.get('labels', [])):
                    proposed += 1
            else:
                dec = dict(dec)
                dec['confidence'] = 0.7
                dec['reason'] = dec.get('reason') or 'fallback-low-confidence'
            decisions[local_id] = dec

    # 3) aplikace rozhodnutí
    # FÁZE 1: s APPLY=0 se NESMÍ zapsat 'applied' (mailbox se nemění) — jinak by
    # model_version gate už mail nikdy nepřeřadil a při pozdějším APPLY=1 by se
    # rozhodnutí ztratilo. Proto se uloží jako 'pending_apply'; po přepnutí na
    # APPLY=1 se aplikují z uloženého decision_json BEZ dalšího LLM volání.
    applied, failed, skipped = [], [], []
    pending_apply = []
    model_version = mail_rules.get_model_version(conn)
    for row in pending:
        local_id = str(row['id'])
        if local_id not in decisions:
            continue
        dec = decisions[local_id]
        labels = list(dict.fromkeys(dec.get('proton_labels', [])))
        payload = {'id': local_id, 'message_id': row['message_id'],
                   'folder': dec.get('folder') or 'INBOX', 'labels': labels,
                   'reason': dec.get('reason'), 'confidence': dec.get('confidence', 0.7),
                   'model_version': model_version}
        if dec.get('folder') is None:
            payload['note'] = 'no rule match — zůstává v INBOX'
        try:
            if not APPLY:
                conn.execute("update queue set status='pending_apply', decision_json=?, "
                             "locked_at=NULL, last_error=NULL, updated_at=? where id=?",
                             (json.dumps(payload, ensure_ascii=False), now_iso(), local_id))
                pending_apply.append(payload)
                continue
            for label in labels:
                copy_to_label('INBOX', label, local_id)
            if dec.get('folder') is not None:
                move('INBOX', dec['folder'], local_id)
            conn.execute("update queue set status='applied', decision_json=?, applied_at=?, "
                         "locked_at=NULL, last_error=NULL, updated_at=? where id=?",
                         (json.dumps(payload, ensure_ascii=False), now_iso(), now_iso(), local_id))
            applied.append(payload)
        except Exception as e:
            attempts = (row['attempts'] or 0) + 1
            new_status = 'manual_review' if attempts >= 3 else 'needs_llm'
            conn.execute("update queue set status=?, attempts=?, last_error=?, locked_at=NULL, "
                         "updated_at=? where id=?", (new_status, attempts, str(e), now_iso(), local_id))
            failed.append({'id': local_id, 'message_id': row['message_id'], 'error': str(e), 'status': new_status})

    conn.commit()

    # 3b) APPLY=1: dořešit dříve odložené 'pending_apply' BEZ LLM (uložené rozhodnutí).
    promoted = []
    if APPLY:
        for row in conn.execute("select id, decision_json from queue "
                                "where status='pending_apply' limit 500").fetchall():
            local_id = str(row['id'])
            try:
                dec = json.loads(row['decision_json'] or '{}')
                for label in dec.get('labels', []):
                    copy_to_label('INBOX', label, local_id)
                if dec.get('folder') and dec['folder'] != 'INBOX':
                    move('INBOX', dec['folder'], local_id)
                conn.execute("update queue set status='applied', applied_at=?, updated_at=? "
                             "where id=?", (now_iso(), now_iso(), local_id))
                promoted.append(local_id)
            except Exception as e:
                conn.execute("update queue set status='manual_review', last_error=?, updated_at=? "
                             "where id=?", (str(e), now_iso(), local_id))
        conn.commit()

    # MAILF-012: rekonciliace zombie 'manual_review' záznamů (bounded).
    # Záznam, jehož zpráva už v INBOX není, se označí terminálně 'gone';
    # záznam, jehož zpráva se vrátila, se vrátí do fronty.
    reconciled = {'gone': 0, 'requeued': 0}
    for row in conn.execute(
            "select id from queue where status='manual_review' limit 200").fetchall():
        local_id = str(row['id'])
        if local_id in inbox_by_id:
            conn.execute("update queue set status='needs_llm', locked_at=NULL, updated_at=? "
                         "where id=?", (now_iso(), local_id))
            reconciled['requeued'] += 1
        else:
            conn.execute("update queue set status='gone', locked_at=NULL, updated_at=? "
                         "where id=?", (now_iso(), local_id))
            reconciled['gone'] += 1
    conn.commit()

    summary = {
        'timestamp': now_iso(),
        'pending_before': len(pending),
        'applied_count': len(applied),
        'failed_count': len(failed),
        'skipped_count': len(skipped),
        'pending_apply_count': len(pending_apply),
        'promoted_count': len(promoted),
        'llm_calls': llm_calls,
        'llm_enabled': LLM_ENABLED,
        'llm_dryrun': LLM_DRYRUN,
        'apply': APPLY,
        'proposed_rules': proposed,
        'applied': applied,
        'failed': failed,
        'skipped': skipped,
        'queue_counts': queue_counts(conn),
        'reconciled': reconciled,
        'inbox_remaining': len(inbox),
        'model_version': model_version,
    }
    if llm_meta:
        summary['llm_meta'] = llm_meta
    conn.close()
    save_state(summary)
    append_run_log(summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
