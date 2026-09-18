# 2026-09-18 — MailFilter: řízený APPLY vzorek + nálezy z ostrého běhu

**Status:** ✅ provedeno (FÁZE 1 → malý ostrý vzorek s odsouhlasením t)
**Autor:** Marvin ▲
**Navazuje na:** `2026-09-18_mailfilter_revize-tridici-logiky_0v1.md`,
`2026-09-18_mailfilter_p0-p1-implementace_0v1.md`

## Zadání

t (2026-09-18 10:59 UTC): „ad 1: ano, na malém vzorku" → souhlas s **reálnými
přesuny** (APPLY=1) na malém, ručně vybraném vzorku. Otázka 2 (LLM v cronu)
zůstává nezodpovězena → LLM v cronu **vypnutý**.

## Co se spustilo

Ruční jednorázový běh second passu (ne cron):

```
MAILFILTER_APPLY=1 \
MAILFILTER_LLM_ENABLED=1 \
MAILFILTER_MAX_LLM_CALLS=1 \
MAILFILTER_SAMPLE_IDS=2281,2290,2296,2215,2256 \
MAILFILTER_LLM_MODEL=deepseek/deepseek-chat \
python3 bin/bezouska_llm_second_pass_worker.py
```

- Model: `deepseek/deepseek-chat` (1 LLM call, ~2,5 s)
- Záloha před během: `tmp/mailfilter-apply-sample-20260918/queue-before.sqlite3`,
  `triage-state-before.json`

## Výsledek — reálné přesuny

| ID | cílová složka | conf | ověřeno v mailboxu |
|----|---------------|------|--------------------|
| 2281 | `Folders/90_ostatni/91_newsletter` | 0.8 | ✅ (dle předmětu) |
| 2296 | `Folders/90_ostatni/91_newsletter` | 0.7 | ✅ |
| 2290 | `Folders/90_ostatni/96_spammers_fun` | 0.7 | ✅ |
| 2215 | `Folders/90_ostatni/94_notifikace` | 0.6 | ✅ |
| 2256 | `Folders/90_ostatni/92_transakce` | 0.7 | ✅ |

- INBOX: 585 → 580 (5 mailů odešlo).
- Všechny cílové složky obsahují mail dle předmětu; v INBOXu už nejsou.
- Fronta: `applied=364`, `gone=34`, `needs_llm=578`.
- Vzniklo 5 **pending** návrhů pravidel (`source='llm-second-pass'`, `active=0`) —
  čekají na odsouhlasení t, klasifikaci nemění.

> Poznámka k identifikaci: himalaya přiděluje v cílové složce **nové lokální ID**,
> proto se přesun ověřuje podle předmětu/odesílatele, ne podle původního ID.

## Nalezené a opravené bugy (odhalil až ostrý běh)

### B1 — reasoning model vracel prázdný content
`deepseek/deepseek-v4-flash` je reasoning model: s `max_tokens=1500` spálil celý
limit na reasoning (`finish_reason=length`, `reasoning_tokens=1500`) → prázdný
`content`. **Fix:** defaultní model přepnut na `deepseek/deepseek-chat`
(non-reasoning), `max_tokens` 2000 → 3000, a při prázdném contentu worker
vyhodí explicitní chybu `empty LLM response (finish_reason=…, model=…)`.

### B2 — příliš vysoký confidence práh (0.8) zahazoval vše
Model se drží konzervativně (0.5–0.9). Práh 0.8 znamenal, že **prakticky každé**
LLM rozhodnutí spadlo do `fallback-unclassified` → mail zůstal v INBOX a tichý
„no-op applied". **Fix:** `CONFIDENCE_THRESHOLD` je nyní konfigurovatelný přes
`MAILFILTER_LLM_MIN_CONF`, default **0.6**.

### B3 — `propose_rule` bumpoval `model_version` (churn)
Návrh pravidla je `pending`/`active=0` → klasifikaci **nemění**, ale
`propose_rule()` volal `bump_model_version()`. Každý LLM návrh tak vyvolal
re-enqueue celé no-op fronty (2 → 7 během vzorku). **Fix:** `propose_rule()`
už verzi nebumpuje; bump dělá až schválení pravidla (`set_review`/`update_rule`/
`create_rule`). `meta.model_version` vráceno na `2`.

### B4 — `UnboundLocalError` v sample větvi (smoke D1)
Řádek `MAX_MESSAGES_PER_RUN = len(pending)` v `if sample_ids:` větvi dělal
z proměnné lokální → `UnboundLocalError` v `else` větvi. **Fix:** řádek odstraněn;
cap se aplikuje jen na non-sample větev.

## Smoke test

`bin/tests/mailfilter-smoke.py` → **17/17 OK** (přidány G1/G2):

- **G1** `MAILFILTER_LLM_ENABLED=0` → `llm_classify()` nevolá síť (vrací `{}`).
- **G2** confidence práh je konfigurovatelný (`MAILFILTER_LLM_MIN_CONF`).

## Bezpečnostní stav po běhu

- Cron wrappery zůstávají `MAILFILTER_APPLY=0` + `MAILFILTER_LLM_ENABLED=0`.
- Ostrý vzorek byl **jednorázový ruční** běh, ne cron.
- Rollback: `tmp/mailfilter-apply-sample-20260918/` (DB + triage state před).
  Maily v mailboxu lze vrátit `himalaya message move -a bezouska -f <zdroj> INBOX <id>`.

## Otevřené otázky pro t

1. Zapnout `MAILFILTER_APPLY=1` v cronu (přesuny v plném rozsahu)?
2. Zapnout `MAILFILTER_LLM_ENABLED=1` v cronu (stropy: 2 volání / 20 zpráv na běh)?
3. Odsouhlasit/zahodit 5 pending návrhů pravidel z vzorku (web rules-review).
