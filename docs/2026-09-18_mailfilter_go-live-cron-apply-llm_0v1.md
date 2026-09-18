# MailFilter — GO-LIVE: cron APPLY=1 + LLM=1, progressive capy, MAILF-019

- Datum: 2026-09-18
- Autor: Marvin (main session)
- Kontext: t schválil (a) reálné přesuny `APPLY=1`, (b) LLM v cronu s rozumnými stropy
  („na začátku větší využití, postupně klesá"), po úspěšném řízeném vzorku 5 mailů.

## Rozhodnutí t → implementace

| Otázka | Rozhodnutí t | Implementace |
|---|---|---|
| APPLY=1 v cronu | ano (po vzorku) | triage wrapper + second-pass wrapper: `MAILFILTER_APPLY=1` |
| LLM_ENABLED=1 v cronu | ano, se stropy | second-pass wrapper: `MAILFILTER_LLM_ENABLED=1` |
| Stropy | „zvaž, jestli má smysl stropovat" | progresivní: `MAX_LLM_CALLS=5`, `MAX_MESSAGES=100`, `MIN_CONF=0.6` |

**Proč stropy ano:** 1 LLM call = až 20 mailů (batch). 5 volání × 20 = 100 mailů/běh,
2 běhy/hod → ~200 mailů/hod. Počáteční nápor ~540 mailů se vyřeší za ~3 h, pak
využití samo klesne (fronta se vyprázdní). Až nápor pomine, stropy snížit.
Ceny: `deepseek/deepseek-chat` je hluboko pod centem za běh — strop je pojistka
proti runaway (bug/prázdný mail), ne úzké hrdlo.

**Vypnutí (safe režim FÁZE 1):** `MAILFILTER_APPLY=0` a/nebo `MAILFILTER_LLM_ENABLED=0`.

## MAILF-019 — reconciliation přepisovala `applied` → `gone` (P1, ostrý běh)

**Symptom:** 14 mailů mělo po ostrém běhu stav `gone`, ačkoli byly fyzicky správně
v cílových složkách (ověřeno himalaya čtením: 14/14 OK).

**Příčina:** ruční běh (můj vzorek) obcházel cron `flock` → dva souběžné běhy:
1. Ruční běh: přesunul mail (`applied`, `applied_at`).
2. Cron běh: měl tentýž mail ještě v `pending` jako `needs_llm`, v INBOXu ho
   nenašel (už přesunut) → rekonciliace nastavila terminální `gone`.

Kód nerozlišoval **„zmizelo z INBOXu"** od **„bylo přesunuto"**.

**Fix:**
1. Guard v obou reconcile updatech: `... where id=? and status='needs_llm'`
   resp. `and status='manual_review'` → terminální stav (`applied`) se nepřepíše.
2. In-process `flock` v `main()` (drží se po celý běh, `MAILFILTER_SP_LOCK`,
   default `/tmp/bezouska-triage-llm-second-pass.agentlock`) → ruční běh a cron
   se navzájem vyloučí i mimo wrapper.
3. Smoke `D5`: `applied` záznam, jehož zpráva v INBOXu není, zůstává `applied`.
4. Jednorázová oprava 14 špatně označených řádků zpět na `applied`
   (záloha DB před opravou).

**Live důkaz:** 14/14 „gone" zpráv fyzicky v cíli (95_registrace, 94_notifikace,
91_newsletter, 92_transakce). Po opravě fronta konzistentní: `applied=398`, `gone=34`
(původní zombie), `needs_llm=544`.

## Ověřený wrapper běh (end-to-end, s LLM)

Příkaz: `bash bin/bezouska-inbox-triage-llm-second-pass-cron.sh`
(APPLY=1, LLM=1, CALLS=5, MSGS=100, MIN_CONF=0.6)

- `pending_before`: 100, `applied_count`: 100, `failed_count`: 0
- `llm_calls`: 5, `proposed_rules`: 51
- Reálné přesuny (non-INBOX): 86 → ověřeno 12/12 namátkou v cíli
- Cíle: 91_newsletter 40, 94_notifikace 26, 95_registrace 6, 92_transakce 5,
  20_zvirata 4, 96_spammers_fun 2, 97_pozvanky 1, 60_domekumore 1, 35_italie 1
- 14 mailů zůstalo v INBOX (LLM si nebyl jistý / fallback) → správně zůstávají
- INBOX live: 547 → 461
- Fronta po běhu: `needs_llm=444`, `applied=498`, `gone=34`

## Stav learned_rules

- `llm-second-pass | pending | 0`: 82 (nové návrhy, NEAKTIVNÍ — čekají na t)
- `zatridil tomas | ok | 1`: 42, `manual-web | ok | 1`: 31,
  `learned from Labels/zatridil tomas | ok | 1`: 20, `manual | ok | 1`: 3
- `pending` celkem: 84, aktivní `ok` pravidla: 98
- Model NIKDY neaktivuje pravidlo sám — jen `pending`/`active=0`.

## Neaktivované / čeká

- 82+ nových `pending` návrhů pravidel k odsouhlasení t (kandidáti pro hromadné
  schválení ve webu mailfilter.bezouska.cz).
- MAILF-016 (mrtvá učící smyčka z `Labels/zatridil tomas`), MAILF-017 (metriky +
  alert na ticho) — P2, stále otevřené.

## Artefakty

- Kód: `bin/bezouska_llm_second_pass_worker.py` (lock + guardy),
  `bin/bezouska-inbox-triage-cron.sh`, `bin/bezouska-inbox-triage-llm-second-pass-cron.sh`,
  `bin/tests/mailfilter-smoke.py` (18/18 OK)
- Zálohy: `tmp/mailfilter-apply-full-20260918/` (queue-before-*.sqlite3)
- Runbook log: `logs/bezouska-inbox-triage-llm-second-pass.log`
