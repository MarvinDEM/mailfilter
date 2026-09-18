#!/usr/bin/env python3
"""MailFilter smoke test (t 2026-09-18).

Ověřuje RUNTIME chování oprav P0/P1 na izolovaném temp DB a falešném
`himalaya` shimu (žádný reálný IMAP, žádné kredity, žádná změna mailboxu):

  A) MAILF-011 — paginace: triage přečte CELÝ mailbox (všechny stránky).
  B) MAILF-010 — retry gate: no-op 'applied' se re-enqueue jen při bumpu
      model_version; bez bumpu žádný churn.
  C) MAILF-013 — oba passy používají stejnou klasifikaci (mail_rules).
  D) MAILF-012 — zombie 'manual_review' → 'gone'.
  E) MAILF-015 — generátor návrhů pravidel (dry-run, bez LLM).
  F) MAILF-014 — triage i second pass = confirmed_only=True (jen 'ok' pravidla).

Exit 0 = vše OK, 1 = chyba.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path('/root/.openclaw/workspace')
BIN = Path(__file__).resolve().parents[1]  # funguje v workspace/bin i mailfilter/bin (mirror)
FIXTURE = BIN / 'tests/fixtures/inbox-snapshot.json'
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ''))


def make_shim(dirpath):
    shim = Path(dirpath) / 'himalaya'
    shim.write_text(f'''#!/usr/bin/env python3
import json, sys, os
args = sys.argv[1:]
fixture = json.load(open({str(FIXTURE)!r}))
page_size, page, folder = 200, 1, 'INBOX'
for i, a in enumerate(args):
    if a == '--page-size': page_size = int(args[i+1])
    if a == '--page': page = int(args[i+1])
    if a == '-f': folder = args[i+1]
if len(args) >= 2 and args[0] == 'envelope' and args[1] == 'list':
    if folder != 'INBOX':
        print('[]'); sys.exit(0)
    start = (page-1)*page_size
    print(json.dumps(fixture[start:start+page_size], ensure_ascii=False))
    sys.exit(0)
if args and args[0] == 'message':
    sys.exit(0)
print('[]')
''')
    shim.chmod(0o755)
    return shim


def seed_db(db, fixture):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE queue (id TEXT PRIMARY KEY, subject TEXT, sender TEXT, date TEXT,
        message_id TEXT, detected_at TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        locked_at TEXT, last_error TEXT, decision_json TEXT, applied_at TEXT, updated_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE learned_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL,
        recipient TEXT, subject_pattern TEXT, folder TEXT NOT NULL, labels_json TEXT NOT NULL,
        source TEXT NOT NULL, learned_at TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 1, notes TEXT,
        review_status TEXT NOT NULL DEFAULT 'pending', active INTEGER NOT NULL DEFAULT 1, forward_to TEXT)''')
    # Najdi zprávu z fixture, kterou heuristika nechává v INBOX (folder None).
    sys.path.insert(0, str(BIN))
    import mail_rules as _mr
    noop_id = None
    for m in fixture:
        subj = (m.get('subject') or '').lower()
        sender = ((m.get('from') or {}).get('addr') or '').lower()
        f, _r = _mr.classify_folder(subj, sender)
        if f is None:
            noop_id = str(m['id'])
            noop_subj = m.get('subject')
            noop_sender = sender
            break
    assert noop_id, 'fixture nemá mail bez folderu'
    # 1 no-op applied záznam (rozhodnutí INBOX) s model_version=0
    conn.execute("INSERT INTO queue VALUES (?,?,?,NULL,NULL,NULL,'applied',1,NULL,NULL,?,NULL,?)",
                 (noop_id, noop_subj, noop_sender,
                  json.dumps({'folder': 'INBOX', 'model_version': 0}), '2026-08-01T00:00:00+00:00'))
    # 1 zombie manual_review (zpráva '999999' v INBOX není)
    conn.execute("INSERT INTO queue VALUES ('999999','zombie','y@bar.cz',NULL,NULL,NULL,'manual_review',3,NULL,'message not found in INBOX',NULL,NULL,?)",
                 ('2026-08-01T00:00:00+00:00',))
    conn.commit()
    conn.close()
    return noop_id


def run(env, script, extra=None):
    e = dict(os.environ)
    e.update(env)
    cmd = [sys.executable, str(BIN / script)] + (extra or [])
    p = subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=120)
    return p


def main():
    tmp = tempfile.mkdtemp(prefix='mailfilter-smoke-')
    try:
        db = Path(tmp) / 'queue.sqlite3'
        os.environ['MAIL_RULES_DB'] = str(db)  # aby rodičovský import mail_rules nesahal na živou DB
        make_shim(tmp)
        fixture = json.load(open(FIXTURE))
        noop_id = seed_db(db, fixture)

        env = {
            'PATH': f'{tmp}:{os.environ["PATH"]}',
            'MAIL_RULES_DB': str(db),
            'MAILFILTER_APPLY': '0',
            'MAILFILTER_LLM_ENABLED': '0',
            'MAILFILTER_MAX_MESSAGES': '500',
            'MAILFILTER_MAX_LLM_CALLS': '0',
            'MAILFILTER_STATE_PATH': f'{tmp}/state.json',
            'MAILFILTER_STATE_PATH2': f'{tmp}/state.json',
            'MAILFILTER_RUN_LOG': f'{tmp}/runs.jsonl',
            'MAILFILTER_SP_RUN_LOG': f'{tmp}/sp-runs.jsonl',
        }

        # A) triage — paginace: 200 mailů / page-size 200 → 1 stránka nestačí,
        # fixture má 200, takže ověříme totéž s page_size menší přes env override.
        # Použijeme přímo list_env_all přes malý inline skript.
        inline = (
            "import sys; sys.path.insert(0,'%s'); import triage_bezouska_mail as t; "
            "print(len(t.list_env_all('INBOX', page_size=50, max_pages=20)))" % BIN)
        p = subprocess.run([sys.executable, '-c', inline], capture_output=True, text=True, env=env, timeout=120)
        n = int((p.stdout.strip().splitlines() or ['0'])[-1] or 0)
        check('A) MAILF-011 paginace přečte celý mailbox (200 mailů po 50)', n == len(fixture), f'přečteno={n}')

        # B) triage běh #1 — no-op applied se re-enqueue (model_version v DB = 0,
        #    rozhodnutí má model_version 0 → stejná verze → NESMÍ re-enqueue).
        p = run(env, 'triage_bezouska_mail.py')
        check('B1) triage doběhne (rc=0)', p.returncode == 0, p.stderr[-200:])
        conn = sqlite3.connect(db)
        noop_status = conn.execute("select status from queue where id=?", (noop_id,)).fetchone()[0]
        check('B2) no-op applied bez bumpu verze zůstává applied (žádný churn)', noop_status == 'applied', noop_status)

        # B3) bump model_version → no-op se re-enqueue
        sys.path.insert(0, str(BIN))
        import mail_rules
        c2 = mail_rules.db_connect()
        mail_rules.bump_model_version(c2)
        c2.close()
        p = run(env, 'triage_bezouska_mail.py')
        noop_status = conn.execute("select status from queue where id=?", (noop_id,)).fetchone()[0]
        check('B3) po bumpu verze se no-op re-enqueue na needs_llm', noop_status == 'needs_llm', noop_status)

        # D) second pass — zombie manual_review (999999) → gone, no-op dořešen
        p = run(env, 'bezouska_llm_second_pass_worker.py')
        check('D1) second pass doběhne (rc=0)', p.returncode == 0, p.stderr[-200:])
        zombie = conn.execute("select status from queue where id='999999'").fetchone()[0]
        check('D2) MAILF-012 zombie manual_review → gone', zombie == 'gone', zombie)
        noop_status = conn.execute("select status from queue where id=?", (noop_id,)).fetchone()[0]
        check('D3) no-op dořešen second passem (applied)', noop_status == 'applied', noop_status)

        # E) MAILF-015 generátor návrhů — dry-run, bez LLM
        p = run(env, 'mailfilter-rule-proposals.py', ['--dry-run', '--json', '--min', '1'])
        check('E1) rule-proposals běží (rc=0)', p.returncode == 0, p.stderr[-200:])
        try:
            obj = json.loads(p.stdout)
            ok = 'proposals' in obj and 'pending_total' in obj
        except Exception:
            ok = False
        check('E2) rule-proposals vrací strukturovaný návrh', ok, p.stdout[:200])

        # C/F) stejná klasifikace obou passů = mail_rules (single source of truth)
        import inspect
        triage_mod = __import__('triage_bezouska_mail')
        sp_mod = __import__('bezouska_llm_second_pass_worker')
        check('C1) triage deleguje klasifikaci na mail_rules.classify_message',
              'mail_rules.classify_message' in inspect.getsource(triage_mod.deterministic_classify))
        check('C2) second pass deleguje klasifikaci na mail_rules.classify_message',
              'mail_rules.classify_message' in inspect.getsource(sp_mod.main))
        check('F1) triage používá confirmed_only=True',
              'confirmed_only=True' in inspect.getsource(triage_mod.deterministic_classify))
        check('F2) second pass používá confirmed_only=True',
              'confirmed_only=True' in inspect.getsource(sp_mod.main))
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== SMOKE: {len(PASS)} OK, {len(FAIL)} FAIL ===")
    if FAIL:
        print("FAILED:", ', '.join(FAIL))
        sys.exit(1)


if __name__ == '__main__':
    main()
