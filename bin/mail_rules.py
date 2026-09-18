#!/usr/bin/env python3
"""
Sdílená pravidlová vrstva pro triage pošty + učící smyčku (t, 2026-08-12).

Matchovací model: (sender × recipient × subject_pattern) → složka.
Odesílatel sám nestačí — příklad: notifikace@mojedatovaschranka.cz se třídí
podle předmětu („pro: Tomáš Bezouška" ≠ „podnikající fyzická osoba" ≠
„Institut pro správu dokumentů").

Priorita shody (nejspecifičtější vyhrává):
  1) sender + recipient + subject_pattern
  2) sender + subject_pattern
  3) sender + recipient
  4) sender (bez patternu)
  5) heuristika (classify_folder) — mimo tento modul
"""
import json
import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get('MAIL_RULES_DB', '/root/.openclaw/workspace/state/bezouska-llm-queue.sqlite3'))
RULES_PATH = Path('/root/.openclaw/workspace/mail-sorting-rules.md')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    recipient TEXT,
    subject_pattern TEXT,
    folder TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    source TEXT NOT NULL,
    learned_at TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending',
    active INTEGER NOT NULL DEFAULT 1,
    forward_to TEXT
)
'''


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute('PRAGMA table_info(learned_rules)').fetchall()]
    if not cols:
        conn.execute(SCHEMA)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_rules_lookup ON learned_rules(sender, recipient, subject_pattern)')
    else:
        if 'forward_to' not in cols:
            conn.execute('ALTER TABLE learned_rules ADD COLUMN forward_to TEXT')
            conn.commit()
            cols.append('forward_to')
        if 'subject_pattern' not in cols:
            # Migrace staré tabulky (sender PRIMARY KEY, bez patternu/recipienta)
            conn.execute('ALTER TABLE learned_rules RENAME TO learned_rules_old')
            conn.execute(SCHEMA)
            conn.execute('''
                INSERT INTO learned_rules (sender, recipient, subject_pattern, folder, labels_json, source, learned_at, hits, notes)
                SELECT sender, NULL, NULL, folder, labels_json, source, learned_at, hits, notes FROM learned_rules_old
            ''')
            conn.execute('DROP TABLE learned_rules_old')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_rules_lookup ON learned_rules(sender, recipient, subject_pattern)')
        if 'review_status' not in cols:
            conn.execute("ALTER TABLE learned_rules ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'")
        if 'active' not in cols:
            conn.execute('ALTER TABLE learned_rules ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
    conn.commit()
    return conn


def _to_decision(row, reason):
    labels = json.loads(row['labels_json'] or '[]')
    return {
        'semantic_labels': ['learned_rule'],
        'proton_labels': labels,
        'folder': row['folder'],
        'reason': reason,
        'rule_id': row['id'],
        'subject_pattern': row['subject_pattern'],
    }


def wildcard_match(pattern, value):
    """Wildcard shoda pro sender/recipient: '*' = libovolný text (t, 2026-08-24).
    - pattern ''/None → True (libovolný)
    - pattern bez '*' → přesná shoda (case-insensitive, zachovává staré chování)
    - pattern s '*' → celá hodnota proti regexu (např. '*@bezouska.cz')
    """
    if not pattern:
        return True
    value = (value or '').lower()
    pattern = str(pattern).lower()
    if '*' not in pattern:
        return value == pattern
    regex = '^' + re.escape(pattern).replace(r'\*', '.*') + '$'
    return re.fullmatch(regex, value) is not None


def learned_rule_lookup(conn, sender, recipient=None, subject=None, confirmed_only=False):
    """Nejspecifičtější shoda pravidla pro (sender, recipient, subject).
    confirmed_only=True → ber jen pravidla potvrzená v revizi (review_status='ok').
    sender='' v pravidle = wildcard (libovolný odesílatel) — přesný sender má přednost
    (ORDER BY (sender <> '') DESC), t 2026-08-23."""
    sender = (sender or '').lower()
    recipient = (recipient or '').lower() or None
    subject = subject or ''
    confirmed_sql = "AND review_status='ok'" if confirmed_only else ''
    active_sql = f"AND active=1 {confirmed_sql}"
    # Priorita shody: přesný sender > wildcard ('*') > prázdný (libovolný) (t, 2026-08-24)
    order = ("ORDER BY CASE WHEN sender='' THEN 0 WHEN sender LIKE '%*%' THEN 1 ELSE 2 END DESC, "
             "length(subject_pattern) DESC, id DESC LIMIT 1")
    # sender shoda: přesný / prázdný (libovolný) / wildcard pattern (REPLACE '*' → '%', '_' escapováno)
    sender_match = ("(sender=? OR sender='' OR "
                    "(? LIKE REPLACE(REPLACE(sender, '_', '\\_'), '*', '%') ESCAPE '\\'))")

    # 1) recipient + subject (sender přesný, wildcard nebo libovolný)
    # Subject shoda v Pythonu (pattern.lower() in subject.lower()) — SQLite LIKE je
    # case-insensitive jen pro ASCII, česká diakritika (POZVÁNKA vs pozvánka) by neshodila (t, 2026-08-24)
    if subject and recipient:
        rows = conn.execute(
            f'''SELECT * FROM learned_rules
               WHERE {sender_match} AND recipient=? AND subject_pattern IS NOT NULL {active_sql}
               ORDER BY CASE WHEN sender='' THEN 0 WHEN sender LIKE '%*%' THEN 1 ELSE 2 END DESC,
                        length(subject_pattern) DESC, id DESC''',
            (sender, sender, recipient)).fetchall()
        for row in rows:
            if (row['subject_pattern'] or '').lower() in subject.lower():
                return _to_decision(row, 'learned-rule:sender+recipient+subject')

    # 2) subject (sender přesný, wildcard nebo libovolný)
    if subject:
        rows = conn.execute(
            f'''SELECT * FROM learned_rules
               WHERE {sender_match} AND subject_pattern IS NOT NULL {active_sql}
               ORDER BY CASE WHEN sender='' THEN 0 WHEN sender LIKE '%*%' THEN 1 ELSE 2 END DESC,
                        length(subject_pattern) DESC, id DESC''',
            (sender, sender)).fetchall()
        for row in rows:
            if (row['subject_pattern'] or '').lower() in subject.lower():
                return _to_decision(row, 'learned-rule:sender+subject')

    # 3) recipient (sender přesný, wildcard nebo libovolný)
    if recipient:
        row = conn.execute(
            f'''SELECT * FROM learned_rules
               WHERE {sender_match} AND recipient=? AND subject_pattern IS NULL {active_sql}
               {order}''',
            (sender, sender, recipient)).fetchone()
        if row:
            return _to_decision(row, 'learned-rule:sender+recipient')

    # 4) sender (přesný, wildcard nebo libovolný — plně prázdné pravidlo nejde vytvořit)
    row = conn.execute(
        f'''SELECT * FROM learned_rules
           WHERE ({sender_match} OR sender='') AND subject_pattern IS NULL AND recipient IS NULL {active_sql}
           ORDER BY CASE WHEN sender='' THEN 0 WHEN sender LIKE '%*%' THEN 1 ELSE 2 END DESC, id DESC LIMIT 1''',
        (sender, sender)).fetchone()
    if row:
        return _to_decision(row, 'learned-rule:sender')

    return None


def upsert_learned_rule(conn, sender, folder, proton_labels, source,
                        subject=None, recipient=None, notes=None):
    """Nauč/posil pravidlo.

    - stejná (sender, recipient, subject_pattern, folder) → hits++
    - sender už má pravidlo na JINOU složku a my máme subject →
      přidá se subject-scoped pravidlo (konflikt = sender nestačí)
    - jinak nové pravidlo
    """
    sender = (sender or '').lower()
    recipient = (recipient or '').lower() or None
    subject = subject or None
    labels_json = json.dumps(proton_labels or [], ensure_ascii=False)

    # exact match (včetně patternu) → hits++
    row = conn.execute(
        '''SELECT id FROM learned_rules
           WHERE sender=? AND recipient IS ? AND subject_pattern IS ? AND folder=? LIMIT 1''',
        (sender, recipient, subject, folder)).fetchone()
    if row:
        conn.execute('UPDATE learned_rules SET hits=hits+1, learned_at=? WHERE id=?',
                     (__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), row['id']))
        conn.commit()
        bump_model_version(conn)
        return 'bumped'

    # sender už má obecné pravidlo na jinou složku → konflikt → subject-scoped
    if subject:
        conflict = conn.execute(
            '''SELECT id FROM learned_rules
               WHERE sender=? AND subject_pattern IS NULL AND folder<>? LIMIT 1''',
            (sender, folder)).fetchone()
        if conflict:
            conn.execute(
                '''INSERT INTO learned_rules (sender, recipient, subject_pattern, folder, labels_json, source, learned_at, hits, notes)
                   VALUES (?,?,?,?,?,?,?,1,?)''',
                (sender, recipient, subject, folder, labels_json, source,
                 __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                 notes or 'subject-scoped (sender conflict)'))
            conn.commit()
            bump_model_version(conn)
            return 'conflict-subject-rule'

    conn.execute(
        '''INSERT INTO learned_rules (sender, recipient, subject_pattern, folder, labels_json, source, learned_at, hits, notes)
           VALUES (?,?,?,?,?,?,?,1,?)''',
        (sender, recipient, subject, folder, labels_json, source,
         __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
         notes or None))
    conn.commit()
    bump_model_version(conn)
    return 'inserted'


def list_rules(conn):
    rows = conn.execute(
        '''SELECT id, sender, recipient, subject_pattern, folder, labels_json, source,
                  learned_at, hits, notes, review_status, active
           FROM learned_rules ORDER BY sender, length(subject_pattern) DESC, id''').fetchall()
    out = []
    for r in rows:
        try:
            labels = json.loads(r['labels_json'] or '[]')
        except Exception:
            labels = []
        out.append({
            'id': r['id'],
            'sender': r['sender'],
            'recipient': r['recipient'],
            'subject_pattern': r['subject_pattern'],
            'folder': r['folder'],
            'labels': labels,
            'source': r['source'],
            'learned_at': r['learned_at'],
            'hits': r['hits'],
            'notes': r['notes'],
            'review_status': r['review_status'],
            'active': bool(r['active']),
        })
    return out


def set_review(conn, rule_id, status):
    """status: 'ok' | 'discard' | 'restore'. discard = soft-delete (active=0)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if status == 'discard':
        conn.execute("UPDATE learned_rules SET review_status='discard', active=0, notes=COALESCE(notes||' | ','')||'discarded '||? WHERE id=?", (now, rule_id))
    elif status == 'restore':
        conn.execute("UPDATE learned_rules SET review_status='pending', active=1 WHERE id=?", (rule_id,))
    else:
        conn.execute("UPDATE learned_rules SET review_status='ok', active=1 WHERE id=?", (rule_id,))
    conn.commit()
    bump_model_version(conn)
    return conn.total_changes > 0


def update_rule(conn, rule_id, fields):
    """Uprav pravidlo (t, 2026-08-12). fields: sender?, recipient?,
    subject_pattern?, folder?, labels?. Edit = potvrzení → active=1,
    review_status='ok' (i zahozené pravidlo se editací oživí)."""
    from datetime import datetime, timezone
    row = conn.execute('SELECT id FROM learned_rules WHERE id=?', (rule_id,)).fetchone()
    if not row:
        return False
    sets, params = [], []

    sender = fields.get('sender')
    if sender is not None:
        sender = str(sender).strip().lower()
        # prázdný sender = wildcard (libovolný odesílatel), t 2026-08-23
        sets.append('sender=?'); params.append(sender)

    recipient = fields.get('recipient')
    if recipient is not None:
        recipient = str(recipient).strip().lower() or None
        sets.append('recipient=?'); params.append(recipient)

    subject = fields.get('subject_pattern')
    if subject is not None:
        subject = str(subject).strip() or None
        sets.append('subject_pattern=?'); params.append(subject)

    folder = fields.get('folder')
    if folder is not None:
        folder = str(folder).strip()
        if not folder:
            raise ValueError('Složka nesmí být prázdná')
        sets.append('folder=?'); params.append(folder)

    # validace: aspoň jedno kritérium (t 2026-08-23)
    current = conn.execute(
        'SELECT sender, recipient, subject_pattern FROM learned_rules WHERE id=?', (rule_id,)).fetchone()
    if current:
        new_sender = str(fields.get('sender', current['sender']) or '').strip()
        new_recipient = str(fields.get('recipient', current['recipient'] or '') or '').strip()
        new_subject = str(fields.get('subject_pattern', current['subject_pattern'] or '') or '').strip()
        if not new_sender and not new_recipient and not new_subject:
            raise ValueError('Zadej aspoň jedno kritérium: odesílatel, příjemce nebo předmět')

    labels = fields.get('labels')
    if labels is not None:
        if isinstance(labels, str):
            labels = [x.strip() for x in labels.split(',') if x.strip()]
        sets.append('labels_json=?'); params.append(json.dumps(labels or [], ensure_ascii=False))

    forward_to = fields.get('forward_to')
    if forward_to is not None:
        forward_to = str(forward_to).strip().lower() or None
        if forward_to and '@' not in forward_to:
            raise ValueError('Přeposlání: zadej platnou e-mailovou adresu')
        sets.append('forward_to=?'); params.append(forward_to)

    if not sets:
        return True
    sets.append('active=1')
    sets.append("review_status='ok'")
    sets.append("notes=COALESCE(notes||' | ','')||'edited '||?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(rule_id)
    conn.execute(f'UPDATE learned_rules SET {", ".join(sets)} WHERE id=?', params)
    conn.commit()
    bump_model_version(conn)
    return True


def create_rule(conn, fields):
    """Ruční přidání nového filtru z web UI (t, 2026-08-23).
    Odesílatel je VOLITELNÝ (prázdný = libovolný) — stačí aspoň jedno kritérium
    (sender/recipient/subject). Ručně zadané pravidlo platí rovnou: active=1,
    review_status='ok'."""
    from datetime import datetime, timezone
    sender = str(fields.get('sender') or '').strip().lower()
    recipient = str(fields.get('recipient') or '').strip().lower() or None
    subject = str(fields.get('subject_pattern') or '').strip() or None
    folder = str(fields.get('folder') or '').strip()
    if not folder:
        raise ValueError('Složka je povinná')
    if not sender and not recipient and not subject:
        raise ValueError('Zadej aspoň jedno kritérium: odesílatel, příjemce nebo předmět')
    labels = fields.get('labels')
    if isinstance(labels, str):
        labels = [x.strip() for x in labels.split(',') if x.strip()]
    elif not isinstance(labels, list):
        labels = []
    forward_to = str(fields.get('forward_to') or '').strip().lower() or None
    if forward_to and '@' not in forward_to:
        raise ValueError('Přeposlání: zadej platnou e-mailovou adresu')
    cur = conn.execute(
        '''INSERT INTO learned_rules
           (sender, recipient, subject_pattern, folder, labels_json, source,
            learned_at, hits, notes, review_status, active, forward_to)
           VALUES (?,?,?,?,?,?,?,1,?,'ok',1,?)''',
        (sender, recipient, subject, folder,
         json.dumps(labels, ensure_ascii=False),
         'manual-web', datetime.now(timezone.utc).isoformat(),
         'vytvořeno v mailfilter webu', forward_to))
    conn.commit()
    bump_model_version(conn)
    return cur.lastrowid


def get_rule(conn, rule_id):
    """Vrať jedno pravidlo včetně labels (pro manuální run z webu, t 2026-08-23)."""
    row = conn.execute('SELECT * FROM learned_rules WHERE id=?', (rule_id,)).fetchone()
    if not row:
        return None
    r = dict(row)
    try:
        r['labels'] = json.loads(r.pop('labels_json') or '[]')
    except Exception:
        r['labels'] = []
    return r


def export_rules_md(conn):
    """Vygeneruj mail-sorting-rules.md (čte ho triage jako LLM kontext)."""
    rows = conn.execute(
        '''SELECT sender, recipient, subject_pattern, folder, source, hits
           FROM learned_rules ORDER BY sender, length(subject_pattern) DESC''').fetchall()
    lines = [
        '# Pravidla třídění pošty (bezouska)',
        '',
        'Formát: odesílatel | příjemce (volitelný) | předmět-pattern (volitelný) | složka | zdroj | hitů',
        'Priorita shody: sender+recipient+subject > sender+subject > sender+recipient > sender > heuristika.',
        '',
    ]
    for r in rows:
        lines.append(f"- sender={r['sender']} | recipient={r['recipient'] or '-'} | subject={r['subject_pattern'] or '-'} "
                     f"| folder={r['folder']} | source={r['source']} | hits={r['hits']}")
    lines.append('')
    lines.append('Speciální pravidla (datová schránka, t 2026-08-12):')
    lines.append('- notifikace@mojedatovaschranka.cz | subject obsahuje „podnikající fyzická osoba“ → 50_pracovni/51_bezouska')
    lines.append('- notifikace@mojedatovaschranka.cz | subject obsahuje „Institut pro správu dokumentů“ → 50_pracovni/53_ipsd')
    lines.append('- notifikace@mojedatovaschranka.cz | subject obsahuje „pro: Tomáš Bezouška“ → 10_osobni/11_tomas')
    lines.append('- ostatní datovky bez patternu → obecné pravidlo (dnes 51_bezouska); uprav podle potřeby')
    RULES_PATH.write_text('\n'.join(lines) + '\n', encoding='utf-8')


# ---------------------------------------------------------------------------
# Sdílená klasifikační heuristika (single source of truth, MAILF-013, t 2026-09-18)
# ---------------------------------------------------------------------------
# Dřív byla logika ZKOPÍROVANÁ v triage_bezouska_mail.py i v
# bezouska_llm_second_pass_worker.py → drift (jednou už způsobil bug).
# Teď je kanonicky tady; oba passy volají classify_message().

NEWSLETTER_SENDERS = {
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
PERSONAL_TRANSACTION_SENDERS = {
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
WORK_DOMAIN_MAP = [
    ('@bezouska.cz', 'Folders/50_pracovni/51_bezouska', []),
    ('@inadvisors.cz', 'Folders/50_pracovni/52_inadvisors', []),
    ('@ipsd.cz', 'Folders/50_pracovni/53_ipsd', ['03_ipsd']),
    ('@mmr.gov.cz', 'Folders/50_pracovni/54_mmr', []),
    ('@prazske-noviny.cz', 'Folders/50_pracovni/70_prazske-noviny', []),
    ('@deltaadvisory.cz', 'Folders/50_pracovni/80_delta', []),
]
KEYWORD_TRANSACTION = ['billing', 'payment', 'invoice', 'subscription', 'receipt', 'renew', 'renewal', 'order', 'objednávka', 'objednavka', 'faktura', 'výpis z účtu', 'vypis z uctu', 'výpis k hypotéce', 'vypis k hypotéce', 'vyúčtování', 'vyuctovani', 'připomenutí výzvy', 'pripomenuti vyzvy', 'výzva k platbě', 'vyzva k platbe', 'platební výzva']
KEYWORD_ALERT = ['battery', 'warning', 'alert', 'upozornění', 'upozorneni', 'action required', 'needs your response', 'out of credits', 'přihlásil jste se právě', 'prihlasil jste se prave']
KEYWORD_IPSD = ['ipsd', 'eximex', 'indoc', 'veřejných zakáz', 'verejnych zakaz']
KEYWORD_NEWSLETTER = ['newsletter', 'digest', 'novinky', 'weekly', 'monthly', 'connect 2026', 'che succede']
KEYWORD_DOMAIN_ADMIN = ['dns', 'domény', 'domeny', 'domény ', 'registrace', 'prihlášení do webglobe', 'prihlaseni do webglobe']


def has_any(text, needles):
    text = (text or '').lower()
    return any(n in text for n in needles)


def uniq(seq):
    return list(dict.fromkeys(x for x in seq if x))


def classify_folder(subj, sender):
    """Deterministická heuristika: (subject, sender) -> (folder|None, reason).
    Kanonická verze (původně z triage_bezouska_mail.py, reason 'newsletter-sender')."""
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
    if has_any(subj, KEYWORD_NEWSLETTER) or has_any(sender, ['klaviyomail.com', 'convertkit-mail', 'linkedin.com', 'smartemailing.cz', 'smartsupp.email']):
        return 'Folders/90_ostatni/91_newsletter', 'newsletter-pattern'
    if has_any(subj, KEYWORD_TRANSACTION):
        return 'Folders/90_ostatni/92_transakce', 'transaction-keyword'
    if has_any(subj, KEYWORD_DOMAIN_ADMIN):
        return None, 'domain-admin-keyword'
    if has_any(subj, KEYWORD_ALERT):
        return None, 'generic-alert'
    return None, 'fallback-unclassified'


def collect_labels(subj, sender):
    """-> (semantic_labels, proton_labels). Kanonická verze (z triage)."""
    semantic = []
    proton_labels = []

    if 'calendar.proton.me' in sender or sender == 'no-reply@calendar.proton.me' or sender == 'news@ana-white.com' or 'mojeid' in sender or sender == 'podpora@mojeid.cz' or '@bezouskova.cz' in sender:
        semantic += ['personal']
        proton_labels += ['50_osobni']

    if sender == 'notifications@fibaro.com' or 'fibaro' in sender or sender in {'noreply@business-updates.facebook.com', 'security@facebookmail.com'} or 'facebookmail.com' in sender or 'business-updates.facebook.com' in sender or sender == 'podpora@nic.cz' or 'nic.cz' in sender or has_any(subj, KEYWORD_DOMAIN_ADMIN) or has_any(subj, KEYWORD_ALERT):
        semantic += ['alerts', '00_vyresit']
        proton_labels += ['vyresit']

    if sender in NEWSLETTER_SENDERS or 'newsletter' in sender or ('bloomberg' in sender and 'news' in sender) or 'substack.com' in sender or 'convertkit' in sender or 'novinky.' in sender or 'promo' in sender or sender == 'info@sparovky.eu' or has_any(subj, KEYWORD_NEWSLETTER) or has_any(sender, ['klaviyomail.com', 'convertkit-mail', 'linkedin.com', 'smartemailing.cz', 'smartsupp.email']):
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


def classify_message(conn, *, subject, sender, recipient=None, confirmed_only=True):
    """Jednotné rozhodnutí pro oba passy (MAILF-013/014, t 2026-09-18).

    Pořadí: learned pravidlo (nejkonkrétnější) přebije heuristiku; heuristika
    doplňuje labely. confirmed_only=True → ber jen pravidla review_status='ok'
    (pending pravidla se aplikují až po schválení v revizi — jednotná politika).
    """
    subj = (subject or '').lower()
    sender = (sender or '').lower()
    learned = learned_rule_lookup(conn, sender, recipient, subj, confirmed_only=confirmed_only)
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


def seed_datovka_rules(conn):
    """Manuální pravidla pro datovou schránku (t, 2026-08-12). Idempotentní."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    seeds = [
        ('notifikace@mojedatovaschranka.cz', None, 'podnikající fyzická osoba', 'Folders/50_pracovni/51_bezouska', 'manual'),
        ('notifikace@mojedatovaschranka.cz', None, 'Institut pro správu dokumentů', 'Folders/50_pracovni/53_ipsd', 'manual'),
        ('notifikace@mojedatovaschranka.cz', None, 'pro: Tomáš Bezouška', 'Folders/10_osobni/11_tomas', 'manual'),
    ]
    for sender, rec, pat, folder, source in seeds:
        exists = conn.execute(
            '''SELECT id FROM learned_rules WHERE sender=? AND subject_pattern=? AND folder=?''',
            (sender, pat, folder)).fetchone()
        if not exists:
            conn.execute(
                '''INSERT INTO learned_rules (sender, recipient, subject_pattern, folder, labels_json, source, learned_at, hits, notes)
                   VALUES (?,?,?,?,?,?,?,1,?)''',
                (sender, rec, pat, folder, '[]', source, now, 'datová schránka — subjekt v předmětu'))
    conn.commit()
    export_rules_md(conn)


# ---------------------------------------------------------------------------
# Verze klasifikačního modelu (MAILF-010, t 2026-09-18)
# ---------------------------------------------------------------------------
# Triaging znovu zanořuje no-op 'applied' maily (rozhodnutí folder==INBOX)
# jen když se verze změní — tj. když vznikne/změní se pravidlo. Bez gate by
# se 600+ no-op záznamů re-queue-ovalo každých 15 min (churn).

def _ensure_meta(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')


def ensure_meta(conn):
    """Veřejná varianta pro volající moduly (idempotentní)."""
    _ensure_meta(conn)


def propose_rule(conn, sender, folder, proton_labels, source='llm-second-pass', notes=None):
    """MAILF-015: založí NÁVRH pravidla (review_status='pending', active=0).

    Model nikdy nezapisuje aktivní pravidlo — t ho musí odsouhlasit ve webu.
    Idempotentní: stejná (sender, folder) pending už existuje → nic nového.
    """
    sender = (sender or '').lower()
    if not sender or not folder:
        return None
    row = conn.execute(
        "SELECT id FROM learned_rules WHERE sender=? AND folder=? AND subject_pattern IS NULL "
        "AND review_status='pending' LIMIT 1", (sender, folder)).fetchone()
    if row:
        return None
    cur = conn.execute(
        '''INSERT INTO learned_rules (sender, recipient, subject_pattern, folder, labels_json,
                                     source, learned_at, hits, notes, review_status, active)
           VALUES (?, NULL, NULL, ?, ?, ?, ?, 1, ?, 'pending', 0)''',
        (sender, folder, json.dumps(proton_labels or [], ensure_ascii=False), source,
         __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
         notes or 'LLM second pass — čeká na odsouhlasení'))
    conn.commit()
    bump_model_version(conn)
    return cur.lastrowid


def get_model_version(conn):
    _ensure_meta(conn)
    row = conn.execute("SELECT value FROM meta WHERE key='model_version'").fetchone()
    try:
        return int(row['value']) if row else 0
    except Exception:
        return 0


def bump_model_version(conn):
    """Zvýší verzi klasifikačního modelu. Voláno při každé změně sady pravidel."""
    _ensure_meta(conn)
    cur = get_model_version(conn)
    new = cur + 1
    conn.execute("INSERT INTO meta(key,value) VALUES('model_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(new),))
    conn.commit()
    return new


if __name__ == '__main__':
    c = db_connect()
    seed_datovka_rules(c)
    export_rules_md(c)
    print('rules ready:')
    for r in c.execute('SELECT sender, recipient, subject_pattern, folder, source, hits FROM learned_rules ORDER BY sender').fetchall():
        print(f"  {r['sender']} | {r['recipient'] or '-'} | {r['subject_pattern'] or '-'} | {r['folder']} | {r['source']} | {r['hits']}")
