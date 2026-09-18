# MailFilter — implementace P0/P1 (MAILF-010, 011, 012, 013, 014, 015, 001)

**Datum:** 2026-09-18
**Autor:** Marvin (na pokyn t: „ok, vezmi to podle backlogu a vyřeš P0 a P1 položky")
**Rozsah:** FÁZE 1 + FÁZE 2 + generátor pravidel, **bez zásahu do reálného mailboxu**
**Navazující revize:** `2026-09-18_mailfilter_revize-tridici-logiky_0v1.md` (vady D1–D10)

---

## 1. Kontext

Revize třídící logiky odhalila, že třídění reálně neběží od 2026-08-02: pipeline se
sama uzamkla. 848 z 921 záznamů fronty byly no-op (`fallback-low-confidence`,
`folder == INBOX`), a protože `enqueue_candidate()` odmítal stav `applied`, tyto
maily se už nikdy nepřehodnotily. Triage navíc četla jen první okno INBOXu
(200 z 585), takže 385 mailů neviděla vůbec.

Cíl: rozmrazit frontu a zapojit skutečný modelový pass — **tak, aby se v mailboxu
nic nepřesunulo, dokud to t neodsouhlasí**.

---

## 2. Bezpečnostní rámec FÁZE 1

Nový přepínač v obou passech:

```python
APPLY = os.environ.get('MAILFILTER_APPLY', '0') == '1'
```

- `move()` a `copy_to_label()` jsou gate-ované. Při `APPLY=0` jen zapíší rozhodnutí
  do fronty, **mailbox se nemění**.
- `MAILFILTER_APPLY=1` se nastaví teprve po explicitním odsouhlasení t.

LLM je navíc odděleně ovladatelný:
- `MAILFILTER_LLM_ENABLED` (default `1`) — hard off switch.
- `MAILFILTER_LLM_DRYRUN=1` — klasifikace se spočítá, ale LLM se nevolá.

**Stav `pending_apply`:** s `APPLY=0` se rozhodnutí s cílovou složkou NESMÍ zapsat
jako `applied` (mailbox se nemění) — jinak by `model_version` gate mail už nikdy
nepřeřadil a při pozdějším `APPLY=1` by se rozhodnutí ztratilo. Proto se uloží jako
`pending_apply`; po přepnutí na `APPLY=1` se aplikují z uloženého `decision_json`
**bez dalšího LLM volání** (funkce `promoted`).

**Nevyřešené maily se NEztrácí:** pokud LLM nic nevrátí (nebo je vypnutý), mail
zůstává ve frontě `needs_llm` — neparkuje se jako `pending_apply` a po 3 pokusech
jde do `manual_review`. Parkuje se jen rozhodnutí, které má cíl (deterministické
pravidlo/heuristika, nebo vysoko-konfidenční LLM).

---

## 3. MAILF-013 + MAILF-014 — jeden zdroj pravdy pro klasifikaci

**Před:** `classify_folder()` / `collect_labels()` / konstanty (KEYWORD_*,
NEWSLETTER_SENDERS, WORK_DOMAIN_MAP…) byly zkopírované v `triage_bezouska_mail.py`
i v `bezouska_llm_second_pass_worker.py`; `mail_rules.py` držel jen matcher pravidel.
Duplikace už jednou způsobila bug (learned_rule_lookup fix 2026-08-24).

**Po:** kanonická klasifikace žije v `bin/mail_rules.py`:

| Funkce | Význam |
|---|---|
| `classify_folder(subj, sender)` | → `(folder|None, reason)` |
| `collect_labels(subj, sender)` | → `(semantic_labels, proton_labels)` |
| `classify_message(conn, *, subject, sender, recipient, confirmed_only=True)` | jednotné rozhodnutí (learned pravidlo přebije heuristiku) |
| `learned_rule_lookup(...)` | lookup pravidel |

Oba passy delegují na `mail_rules.classify_message(...)`. Grep ověřuje, že v `bin/`
existuje jediná definice `classify_folder`/`collect_labels`.

**MAILF-014:** sjednoceno na `confirmed_only=True` v obou passech (aplikují se jen
pravidla `review_status='ok'`). Dřív triage brala i `pending` pravidla. Zdůvodnění:
aplikovat nereviewovaná pravidla na živou poštu je riskantnější.

**Diferenční test (fixture 200 reálných INBOX obálek):**
`heuristic folder/reason drift: 0/200`, `heuristic labels drift: 0/200`
proti `bin/tests/fixtures/classify-baseline.json`.

---

## 4. MAILF-011 — stránkování INBOXu

`list_env_all(folder, page_size, max_pages)` v obou passech:

```python
for page in range(1, max_pages + 1):
    batch = himalaya envelope list ... --page-size N --page page --output json
    if not batch: break
    out.extend(batch)
    if len(batch) < page_size: break
```

- Triage: `HIMALAYA_PAGE_SIZE=200`, `HIMALAYA_MAX_PAGES=20`.
- Second pass: `HIMALAYA_PAGE_SIZE=500`, `HIMALAYA_MAX_PAGES=20`.

**Živé ověření (2026-09-18 10:44 UTC):** triage přečetl `inbox_count = 585`
(celý INBOX), `errors = 0` — dřív viděla jen 200.

---

## 5. MAILF-010 — odmrazení no-op mailů (model_version gate)

Místo nového stavu `retry` (jak navrhoval backlog) zavedena verze klasifikačního
modelu — **bez churn každých 15 min**:

- `meta(key,value)` tabulka; `get_model_version(conn)` / `bump_model_version(conn)`.
- Verze se bumpuje při každé změně sady pravidel (`create_rule`, `update_rule`,
  `set_review`, `upsert_learned_rule`, `propose_rule`).
- Do `decision_json` se ukládá `model_version` okamžiku rozhodnutí.
- `enqueue_candidate()`:
  - `needs_llm` / `llm_in_progress` / `manual_review` → `False` (už ve frontě)
  - `applied` s `folder != 'INBOX'` → `False` (skutečně zařazený mail)
  - `applied` s `folder == 'INBOX'` (no-op) → re-enqueue **jen když**
    `decision.model_version != get_model_version(conn)`

**Živý dopad:** jednorázově se re-enqueue-ovalo 583 no-op mailů
(`applied` 921 → 359, `needs_llm` 0 → 583). Dál už jen při změně pravidel.

---

## 6. MAILF-012 — rekonciliace zombie `manual_review`

Second pass při každém běhu projde až 200 záznamů ve stavu `manual_review`:

- zpráva v INBOX není → terminální stav `gone` (+ `last_error`);
- zpráva v INBOX je → zůstává `manual_review` (patří do lidské fronty; zpět do
  `needs_llm` se neposílá, jinak by se přepočítávala dokola).

Stav `gone` je terminální — `enqueue_candidate()` i `queue_counts()` ho znají.
Výsledek se hlásí v `summary.reconciled = {gone, still_present}` (živě: 34 → `gone`).

---

## 7. MAILF-001 — skutečný LLM second pass

`bin/bezouska_llm_second_pass_worker.py` byl přepsán: dřív to byl jen hardcoded
klon triage heuristiky, který **žádné LLM nevolal** (dokumentace o „modelovém
passu" byla fikce).

Nyní:

1. **Deterministická část** — `mail_rules.classify_message(...)` (pravidla + heuristika).
2. **LLM dořešení** jen pro maily, které deterministicky neuspěly:
   - volá lokální LiteLLM router `http://127.0.0.1:4000/v1`,
     model `deepseek/deepseek-v4-flash` (`MAILFILTER_LLM_MODEL`),
   - `LITELLM_MASTER_KEY` se čte z `state/litellm-router/litellm-router.env`,
   - **batch ≤ 20 zpráv na call** (`MAILFILTER_LLM_BATCH`),
   - **cap volání na běh** `MAILFILTER_MAX_LLM_CALLS` (default 2),
   - **cap zpráv na běh** `MAILFILTER_MAX_MESSAGES` (default 20),
   - teplota 0, JSON-only odpověď, model smí vybrat jen složku z
     `state/mailfilter-folders.json` (`_load_folders()`), labely se sanitizují.
3. **MAILF-015 rule proposal** — vysoko-konfidenční rozhodnutí (≥0.8) → návrh
   pravidla `pending`, `active=0` (viz §8).
4. **Aplikace** — při `APPLY=0` jen zápis do fronty; `model_version` se ukládá do
   `decision_json`.

**Ověření reálné LLM cesty (2026-09-18):** 2 zprávy → 3,3 s, správné složky
(`92_transakce`, `91_newsletter`) s confidence 0.95/0.85.

**Kvóta/alert:** při `HTTP 402` / `Insufficient Balance` / `insufficient_quota`
volá `_alert_quota()` → `bin/escalate.py --case mailfilter-llm-quota --severity high`
(deterministický Telegram alert). Bez toho by model pass tiše vracel prázdné výsledky.

**Cron wrappery** (`bin/bezouska-inbox-triage-cron.sh`,
`bin/bezouska-inbox-triage-llm-second-pass-cron.sh`) explicitně nastavují
`MAILFILTER_APPLY=0` a `MAILFILTER_LLM_ENABLED=0` — FÁZE 1 (žádné přesuny) a
**žádné tiché pálení kreditů**. LLM i přesuny se zapnou až na pokyn t.
`attempts` se počítá jen když LLM reálně běží — s vypnutým LLM maily zbytečně
nepadají do `manual_review`.

`bin/bezouska_llm_worker.py` (volá `openclaw agent --local`) zůstává **mrtvý kód** —
rozhodnutí t: nedržet obojí.

---

## 8. MAILF-015 — generátor návrhů pravidel

Dvě nezávislé cesty, obě zapisují jen `review_status='pending'` + `active=0`
(model NIKDY nezapisuje aktivní pravidlo — t ho odsouhlasí ve webu):

1. **`mail_rules.propose_rule(conn, sender, folder, labels, source, notes)`** —
   idempotentní; stejná `(sender, folder)` pending už existuje → nic nového.
   Volá ji LLM second pass z vysoko-konfidenčních rozhodnutí.
2. **`bin/mailfilter-rule-proposals.py`** — deterministický clustering **bez LLM**:
   stejný sender N× (default 3, `--min`) ve stejné složce a žádné existující
   pravidlo → návrh `pending`. Podporuje `--dry-run`, `--json`.

---

## 9. MAILF-018 — úklid

Odstraněna nedosažitelná větev `if decision is None` v triage.

---

## 10. Smoke test

`bin/tests/mailfilter-smoke.py` (předtím projekt žádný neměl). Běží na **izolované
temp DB** a **falešném `himalaya` shimu** — žádný reálný IMAP, žádné kredity,
žádná změna mailboxu. Kontroluje runtime chování, ne jen syntaxi:

| # | Check |
|---|---|
| A | MAILF-011 — `list_env_all` přečte celý mailbox (200 mailů po 50) |
| B1–B3 | MAILF-010 — no-op `applied` bez bumpu zůstává (no churn); po bumpu → `needs_llm` |
| C1–C2 | MAILF-013 — triage i second pass delegují na `mail_rules.classify_message` |
| D1–D3 | MAILF-012 — zombie `manual_review` → `gone` |
| D3b | APPLY=0 + LLM off — nevyřešený mail zůstává ve frontě (neztratí se) |
| D4 | APPLY=1 — `pending_apply` → `applied` bez LLM |
| E1–E2 | MAILF-015 — rule-proposals běží a vrací strukturovaný návrh |
| F1–F2 | MAILF-014 — oba passy `confirmed_only=True` |

**Výsledek: 15/15 OK** (workspace `bin/` i mirror `mailfilter/bin/`).

Spuštění: `python3 bin/tests/mailfilter-smoke.py`

---

## 11. Změněné soubory

| Soubor | Změna |
|---|---|
| `bin/mail_rules.py` | + klasifikace (`classify_folder`, `collect_labels`, `classify_message`), + `meta`/`model_version`, + `propose_rule`, + `ensure_meta` |
| `bin/triage_bezouska_mail.py` | − duplikovaná heuristika, + paginace, + model_version gate, + APPLY gate, − dead branch |
| `bin/bezouska_llm_second_pass_worker.py` | kompletní přepis: reálné LLM, paginace, rekonciliace, rule proposals, APPLY gate |
| `bin/mailfilter-rule-proposals.py` | **nový** — deterministický generátor návrhů |
| `bin/tests/mailfilter-smoke.py` | **nový** — smoke test |
| `bin/tests/fixtures/inbox-snapshot.json` | **nový** — 200 reálných INBOX obálek |
| `bin/tests/fixtures/classify-baseline.json` | **nový** — baseline klasifikace (0 drift) |
| `mailfilter/bin/*` | mirror zkopírován (git/GitHub) |

Zálohy před změnami: `tmp/mailfilter-backup-20260918/`.

---

## 12. Otevřené / navazující

- **Rozjetí přesunů** — po odsouhlasení t nastavit `MAILFILTER_APPLY=1`
  v `bin/bezouska-inbox-triage-cron.sh` + second-pass wrapperu.
- **MAILF-016** — mrtvá učící smyčka (`Labels/zatridil tomas` prázdný), P2.
- **MAILF-017** — metriky + alert na ticho v třídění, P2.
- **Kontrola reálných přesunů** na malém vzorku před plošným `APPLY=1`.
