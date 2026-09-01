#!/usr/bin/env python3
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT='bezouska'
ROOT=Path('/root/.openclaw/workspace')
DB_PATH=ROOT/'state'/'bezouska-llm-queue.sqlite3'
STATE_PATH=ROOT/'state'/'bezouska-mail-triage.json'
RULES_PATH=ROOT/'mail-sorting-rules.md'

ALLOWED_FOLDERS={
    'Folders/50_pracovni/51_bezouska',
    'Folders/50_pracovni/52_inadvisors',
    'Folders/50_pracovni/53_ipsd',
    'Folders/50_pracovni/54_mmr',
    'Folders/50_pracovni/70_prazske-noviny',
    'Folders/50_pracovni/80_delta',
    'Folders/10_osobni/11_tomas',
    'Folders/10_osobni/31_zvole',
    'Folders/90_ostatni/91_newsletter',
    'Folders/90_ostatni/92_transakce',
}
ALLOWED_LABELS={'newsletter','vyresit','faktury','finance','03_ipsd','50_osobni','označil marvin'}
LOW_CONFIDENCE_THRESHOLD=0.75
MAX_ATTEMPTS=3

PROMPT_TEMPLATE='''You classify Proton Mail messages for account bezouska. Return only JSON with keys folder, labels, reason, confidence.\n\nAllowed folders: {folders}\nAllowed labels: {labels}\nLow confidence rule: if not confident, leave folder empty (\"\") so the message stays in INBOX for manual review. Never invent folders outside the allowed list.\nKeep labels minimal and useful. Always include label "označil marvin".\n\nMail sorting rules excerpt:\n{rules}\n\nMessage:\nFrom: {sender}\nSubject: {subject}\nDate: {date}\nMessage-ID: {message_id}\nPreview:\n{preview}\n'''


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run(*args):
    return subprocess.check_output(list(args), text=True)


def list_env(folder='INBOX', page_size='500'):
    return json.loads(run('himalaya','envelope','list','-a',ACCOUNT,'-f',folder,'--page-size',page_size,'--output','json'))


def read_header(folder, mid, header):
    txt=run('himalaya','message','read','-a',ACCOUNT,'-f',folder,'-p','-H',header,str(mid))
    m=re.search(rf'^{re.escape(header)}:\s*(.+)$', txt, re.MULTILINE)
    return m.group(1).strip() if m else None


def read_preview(folder, mid):
    txt=run('himalaya','message','read','-a',ACCOUNT,'-f',folder,'-p','--no-headers',str(mid))
    txt='\n'.join(line for line in txt.splitlines() if not line.lower().startswith(('from:','to:','cc:','bcc:','date:','subject:')))
    return txt[:4000]


def move(src, dst, mid):
    subprocess.run(['himalaya','message','move','-a',ACCOUNT,'-f',src,dst,str(mid)], check=True, capture_output=True, text=True)


def copy_to_label(src, label, mid):
    # Sekvenční IMAP COPY — UID COPY je v Proton Bridge rozbitý (2026-08-24)
    subprocess.run(['python3','/root/.openclaw/workspace/bin/imap-label-copy.py',src,f'Labels/{label}',str(mid)],
                   check=True, capture_output=True, text=True, timeout=60)


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


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


def load_rules_excerpt():
    try:
        return RULES_PATH.read_text(encoding='utf-8')[:6000]
    except Exception:
        return ''


def build_prompt(item, preview):
    return PROMPT_TEMPLATE.format(
        folders='\n'.join(sorted(ALLOWED_FOLDERS)),
        labels='\n'.join(sorted(ALLOWED_LABELS)),
        rules=load_rules_excerpt(),
        sender=item['sender'] or '',
        subject=item['subject'] or '',
        date=item['date'] or '',
        message_id=item['message_id'] or '',
        preview=preview or ''
    )


def call_llm(prompt):
    proc=subprocess.run(['openclaw','agent','--local','--agent','main','--json','--timeout','120','--message',prompt], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or 'openclaw agent --local failed')
    payload=json.loads(proc.stdout)
    text=(payload.get('reply') or payload.get('message') or payload.get('output') or '').strip()
    m=re.search(r'\{.*\}\s*$', text, re.S)
    if not m:
        raise RuntimeError(f'No JSON object in model output: {text[:500]}')
    return json.loads(m.group(0))


def normalize_decision(decision):
    folder=decision.get('folder')
    labels=decision.get('labels') or []
    reason=decision.get('reason') or ''
    confidence=float(decision.get('confidence') or 0)
    if folder not in ALLOWED_FOLDERS:
        folder=None
    clean_labels=[]
    for label in labels:
        if label in ALLOWED_LABELS and label not in clean_labels:
            clean_labels.append(label)
    if 'označil marvin' not in clean_labels:
        clean_labels.append('označil marvin')
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        folder=None
    return {
        'folder': folder,
        'labels': clean_labels,
        'reason': reason,
        'confidence': confidence,
        'needs_review': confidence < LOW_CONFIDENCE_THRESHOLD,
    }


def main():
    conn=db_connect()
    state=load_json(STATE_PATH, {'account':ACCOUNT})
    inbox={str(m['id']):m for m in list_env('INBOX')}
    processed=[]
    failed=[]
    rows=conn.execute("SELECT * FROM queue WHERE status='needs_llm' ORDER BY detected_at ASC").fetchall()
    for row in rows:
        mid=str(row['id'])
        if mid not in inbox:
            conn.execute("UPDATE queue SET status='manual_review', last_error=?, updated_at=? WHERE id=?", ('Message no longer in INBOX before second pass', now_iso(), mid))
            conn.commit()
            failed.append({'id':mid,'error':'Message no longer in INBOX before second pass','status':'manual_review'})
            continue
        conn.execute("UPDATE queue SET status='llm_in_progress', attempts=attempts+1, locked_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), mid))
        conn.commit()
        try:
            message_id=row['message_id'] or read_header('INBOX', mid, 'Message-ID')
            preview=read_preview('INBOX', mid)
            item=dict(row)
            item['message_id']=message_id
            raw=call_llm(build_prompt(item, preview))
            decision=normalize_decision(raw)
            if decision['folder']:
                move('INBOX', decision['folder'], mid)
                for label in decision['labels']:
                    copy_to_label(decision['folder'], label, mid)
            else:
                # t (2026-08-25): žádné pravidlo → zůstává v INBOX, jen labely
                for label in decision['labels']:
                    copy_to_label('INBOX', label, mid)
            conn.execute("UPDATE queue SET status='applied', message_id=?, decision_json=?, applied_at=?, locked_at=NULL, last_error=NULL, updated_at=? WHERE id=?", (message_id, json.dumps(decision, ensure_ascii=False), now_iso(), now_iso(), mid))
            conn.commit()
            processed.append({'id':mid, **decision})
        except Exception as e:
            attempts=conn.execute('SELECT attempts FROM queue WHERE id=?',(mid,)).fetchone()[0]
            new_status='manual_review' if attempts >= MAX_ATTEMPTS else 'needs_llm'
            conn.execute("UPDATE queue SET status=?, last_error=?, locked_at=NULL, updated_at=? WHERE id=?", (new_status, str(e), now_iso(), mid))
            conn.commit()
            failed.append({'id':mid,'error':str(e),'status':new_status})
    counts=queue_counts(conn)
    conn.close()
    state['llm_second_pass']={
        'timestamp': now_iso(),
        'processed_ids':[p['id'] for p in processed],
        'left_in_inbox_ids':[f['id'] for f in failed if f.get('status')=='needs_llm'],
        'manual_review_ids':[f['id'] for f in failed if f.get('status')=='manual_review'],
        'queue_counts': counts,
        'notes': f'processed={len(processed)} failed={len(failed)}'
    }
    save_json(STATE_PATH, state)
    print(json.dumps({'processed':len(processed),'failed':len(failed),'queue_counts':counts}, ensure_ascii=False))

if __name__ == '__main__':
    main()
