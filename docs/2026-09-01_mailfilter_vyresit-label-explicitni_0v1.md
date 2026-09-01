---
tags: [mailfilter, bezouska, triage, label, vyresit]
date: 2026-09-01
projekt: mailfilter
verze: 0v1
---

# Label vyresit — jen z explicitního pravidla (2026-09-01)

## Zadání (t)

> Emaily, které zůstávají v inboxu, už by neměly dostávat label "vyresit", ten by měl být
> vyhrazený pro emaily které jsou zpracovány podle pravidla které jim label explicitně přiřazuje.

## Původní chování (2026-08-25 → 2026-09-01)

Pravidlo z 25. 8. („zruš pravidla pro 99_nezatrideno"): žádné pravidlo → mail **zůstává v INBOX**
+ automatický label `vyresit` (viditelná značka „k ručnímu řešení").

Důsledek: **všech ~400 emailů v INBOX bez pravidla neslo label vyresit** — label ztratil
výpovědní hodnotu (byl na všem, ne na tom, co opravdu vyžaduje akci).

## Nové chování (2026-09-01)

- **folder=None (žádné pravidlo)** → mail zůstává v INBOX **BEZ automatického labelu vyresit**
- enqueue do LLM fronty zůstává (dedup by message_id)
- label `vyresit` se aplikuje **jen když ho pravidlo/LLM přiřadí explicitně**
  (ACTION_SENDERS/ACTION_KEYWORDS v process_current, learned rules, LLM labels)

### Změněné soubory

| Soubor | Změna |
|---|---|
| `bin/triage_bezouska_mail.py` | folder-None branch: odstraněn `copy_to_label('INBOX','vyresit')`, aplikují se jen `proton_labels` z pravidla |
| `bin/bezouska_llm_second_pass_worker.py` | folder-None branch: odstraněno automatické přidání + copy vyresit |
| `bin/retriage_bezouska_mail_range.py` | folder-None: odstraněno `target_labels.append('vyresit')` |

Poznámka k triage: `if mid in vyresit_ids: continue` zůstává — email, který už vyresit má
(z pravidla), se znovu nezpracovává.

## Cleanup existujících labelů

- **401 emailů v INBOX** mělo label vyresit (automaticky z 25. 8. – 1. 9.)
- Pro každý spuštěna `deterministic_classify` (stejná logika jako triage):
  - **11 emailů** → pravidlo dává vyresit explicitně (Google Cloud action required,
    Coinbase, Česká pošta certifikát, registrace webinářů apod.) → **label zůstal**
  - **390 emailů** → žádné pravidlo → **label odebrán** (ostatní labely zachovány)
- Ověřeno: Labels/vyresit 605 → 215; v INBOX zbývá 11 s vyresit (všechna explicitní)

### Nové nástroje

- `bin/cleanup-vyresit-labels.py` — analýza: gluon DB (INBOX ∩ Labels/vyresit) →
  deterministická klasifikace → keep/remove; `--dry-run` pro náhled
- `bin/cleanup-vyresit-fast.py` — dávkové odebrání labelu: message_id → UID
  (mailbox_message_66) → IMAP UID STORE \Deleted + UID EXPUNGE
  (odebrání labelu = smazání z label mailboxu; mail zůstává v INBOX)

## Lekce

1. **imap-label-sync-batch.py je per-message pomalý** (skeny gluon DB pro každou zprávu) —
   na ~400 zpráv to trvá hodiny. Pro dávkové odebrání labelu je rychlejší cesta:
   UID přímo z gluon tabulky mailbox_message_<id> + jeden IMAP STORE/EXPUNGE po dávkách 100.
2. **Deterministická klasifikace = zdroj pravdy pro cleanup** — stejná funkce
   (`deterministic_classify`) rozhoduje v triage i v cleanupu, takže výsledek sedí
   s tím, co by systém udělal při příštím běhu.
