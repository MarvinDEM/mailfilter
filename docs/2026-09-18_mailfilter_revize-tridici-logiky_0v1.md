# MailFilter — detailní revize třídící logiky

**Datum:** 2026-09-18
**Autor:** Marvin
**Zadavatel:** t
**Status:** revize hotová, návrh opravy čeká na odsouhlasení (zásah do mailboxu/pravidel)
**Prostředí:** host srv1479985, účet IMAP `bezouska`, DB `state/bezouska-llm-queue.sqlite3`

---

## 1. Executive summary

MailFilter technicky běží (web 200, kontejner Up, crony tikají), **ale od 2. 8. 2026 reálně nepřesunul ani jeden mail**. Není to výpadek — je to **návrhová chyba v pipeline**, která se sama uzamkla:

1. **„Druhý pass = model based" neexistuje.** `bezouska_llm_second_pass_worker.py` neobsahuje žádné volání LLM — používá hardcoded kopii heuristiky z prvního passu. Skutečný LLM worker (`bezouska_llm_worker.py`) existuje, ale **není v žádném cronu** → mrtvý kód.
2. **„Návrh nových pravidel" neexistuje nikde.** Žádná komponenta negeneruje nová pravidla z nezařazených mailů. `upsert_learned_rule()` volá výhradně `learn_from_zatridil_tomas.py`.
3. **Smyčka learn-from-zatridil je mrtvá**, protože label `Labels/zatridil tomas` je prázdný (0 mailů) — učí se jen z ručního třídění t, a to ustalo.
4. **Fronta se sama zamkla:** 848 z 921 záznamů ve stavu `applied` je „no-op" (`fallback-low-confidence`, folder `INBOX`). `enqueue_candidate()` odmítá re-enqueue stavu `applied` → tyto maily se **už nikdy nezkusí znovu**.
5. **Triage každých 15 min hlásí „200 kandidátů, enqueued 0"** — což je přesně ten zaseknutý stav.

Výsledek: 585 mailů leží v INBOX, z toho 539 ve frontě označeno jako „vyřešené", ale ve skutečnosti nezařazené.

---

## 2. Jak má logika fungovat (design intent)

Podle dokumentace a `mail-sorting-rules.md`:

- **1. pass — rules based** (`triage_bezouska_mail.py`, cron `*/15`): deterministická klasifikace podle `learned_rules` (sender × recipient × subject_pattern) + heuristiky. Co sedne → přesun do `Folders/*` + labely. Co nesedne → do LLM fronty.
- **2. pass — model based** (`bezouska_llm_second_pass_worker.py`, cron `10,40`): modelové rozhodnutí nad zbytkem **+ návrh nových pravidel**.
- **Learn loop** (`learn_from_zatridil_tomas.py`, cron `25,55`): sleduje label `Labels/zatridil tomas`, z ručního třídění t generuje/posiluje pravidla.

Priorita shody pravidla:
`sender+recipient+subject` > `sender+subject` > `sender+recipient` > `sender` > heuristika.

---

## 3. Co reálně běží (měřená evidence, 2026-09-18 ~10:00 UTC)

### 3.1 Fronta (`state/bezouska-llm-queue.sqlite3` → tabulka `queue`)

| status | počet | poznámka |
|---|---:|---|
| `applied` | 921 | z toho **848 = `fallback-low-confidence` (no-op)** |
| `manual_review` | 34 | všechny důvod `message not found in INBOX` |
| `needs_llm` | 0 | fronta je prázdná |
| `llm_in_progress` | 0 | — |

Rozpad `applied` podle důvodu (top): `fallback-low-confidence` 848, `generic-alert` 16, `transaction-keyword` 11, `webglobe-admin-alert` 6, `gov-cz-domain` 5, `domain-admin-keyword` 4, `newsletter-signal` 3, zbytek ~1–2.

**Reálné přesuny do složky: 293. No-op (zůstalo INBOX): 628.**
**Poslední reálný přesun: 2026-08-02 21:09 UTC** (a i ty poslední šly do `Folders/99_nezatrideno`, které bylo 2026-08-25 zrušeno).

### 3.2 Triage (`logs/bezouska-inbox-triage.log`, `state/bezouska-mail-triage-runs.jsonl`)

- Dnes 41 běhů. Poslední 10:00 UTC: `inbox: 200`, `deterministic_processed_count: 0`, `llm_candidate_count: 200`, `enqueued_this_run: 0`, `errors: 0`.
- Stavový řádek: `{"status": "llm_needed", "processed_count": 0, "llm_candidate_count": 200, "enqueued_count": 0, ...}`.
- Tzn. každých 15 min: 0 přesunů, 0 zápisů do fronty, jen přepočítá už-hotové záznamy.

### 3.3 Druhý pass (`state/bezouska-llm-second-pass-runs.jsonl`)

- Poslední běh 10:10 UTC: `pending_before: 0`, `applied: 0`, `note: "queue empty; skipped IMAP fetch"`.
- Worker **nikdy nevolá LLM** — `classify_pending()` = `learned_rule_lookup` (confirmed_only=True) + `classify_folder()` (hardcoded heuristika). Confidence je pevně 0.9 (rule) / 0.7 (fallback).

### 3.4 Learn loop (`logs/bezouska-learn-from-zatridil-tomas.log`)

- Posledních 5 běhů: `{"processed": 0, "learned_rules": 0, "aborted_reason": null}`.
- Zdrojový label `Labels/zatridil tomas`: **0 mailů**.
- `Labels/learningfail`: 11, `Labels/vyresit`: 212.

### 3.5 Stav pravidel (`learned_rules`)

- Celkem 115, aktivních 100; `review_status`: ok 98, discard 15, pending 2.
- Podle zdroje: `zatridil tomas` 45, `learned from Labels/zatridil tomas` 34, `manual-web` 31, `manual` 3, `test` 2.
- Podle složky (top): newsletter 31, transakce 18, notifikace 17, ipsd 7, tomas 7, bezouska 6, mmr 3, zvole 3.

### 3.6 INBOX

- Celkem **585** položek (id 1136–2316).
- Ve frontě: 564 (z toho 539 no-op `fallback`).
- **Nikdy neprošlo frontou: 21** (např. id 1267–1274).
- Druhá pass i triage čtou jen `page-size` 200 / 500 → **starší maily jsou mimo okno** hodnocení.

---

## 4. Nalezené vady

### D1 — KRITICKÁ: „model based" druhý pass není modelový
`bezouska_llm_second_pass_worker.py` obsahuje pouze `classify_folder()`, což je **kopie heuristiky z triage**, + learned rules. Žádné volání LLM. Skutečný LLM klasifikátor `bin/bezouska_llm_worker.py` (volá `openclaw agent --local`) **není v crontabu** → neběží. Tvrzení v docs/backlogu o modelovém druhém passu je neplatné.

### D2 — KRITICKÁ: fronta se sama zamkla (applied ⇒ navždy hotovo)
`enqueue_candidate()` (triage) vrací `False`, pokud `status in {needs_llm, llm_in_progress, applied}`. 848 no-op záznamů má `applied` → nikdy se nepřehodnotí, i když od té doby vznikla nová pravidla. Každý nový nezařazený mail se po prvním `applied` zařadí do stejné pasti.

### D3 — KRITICKÁ: stránkování skrývá většinu INBOXu
Triage čte `--page-size 200`, second pass `500`. Triage tedy vidí jen 200 nejnovějších z 585 → **385 mailů se nikdy nezhodnotí** (a 21 z nich neprošlo frontou vůbec). Reálně to znamená, že i kdyby pravidla fungovala, na starší maily se nedostane.

### D4 — VYSOKÁ: generátor nových pravidel neexistuje
Nikde v kódu (`bezouska_llm_worker.py`, second-pass, triage, server.py) není logika, která by z nezařazených mailů navrhla pravidlo. `upsert_learned_rule()` volá jen `learn_from_zatridil_tomas.py`. „Second pass s návrhem nových pravidel" je čistě dokumentační fikce.

### D5 — VYSOKÁ: mrtvá učící smyčka
`learn_from_zatridil_tomas.py` je funkční, ale závisí na ručním labelu `Labels/zatridil tomas`, který je prázdný. Bez t-ova ručního třídění negeneruje nic. Automatické učení z reálných přesunů neexistuje.

### D6 — STŘEDNÍ: nekonzistence mezi passy (confirmed_only)
Triage volá `learned_rule_lookup(..., confirmed_only=False)` → aplikuje i `pending`/neověřená pravidla. Second pass volá `confirmed_only=True` → jen `review_status='ok'`. Stejný mail tak může dostat různé rozhodnutí podle toho, kdo ho zpracuje.

### D7 — STŘEDNÍ: 34 zombie záznamů `manual_review`
Důvod u všech: `message not found in INBOX`. Fronta nemá rekonciliaci (mail byl přesunut/smazán) → záznamy zůstávají navěky a zkreslují stav.

### D8 — STŘEDNÍ: duplikovaná klasifikační logika
`classify_folder()` + `collect_labels()` + konstanty (`NEWSLETTER_SENDERS`, `WORK_DOMAIN_MAP`, …) jsou zkopírované v `triage_bezouska_mail.py` i `bezouska_llm_second_pass_worker.py`. Sdílený modul `mail_rules.py` drží jen matcher pravidel, ne klasifikaci. Riziko driftu (už jednou se stalo u `learned_rule_lookup`, fix 2026-08-24).

### D9 — NÍZKÁ: mrtvé větve
`if decision is None` v triage je nedosažitelné (`deterministic_classify` vždy vrací dict). Maily s labelem `vyresit` a bez shody se přeskočí (`continue`) — záměr proti re-labelingu, ale znamená to, že se pro ně nezapisuje ani stav.

### D10 — NÍZKÁ: chybí observabilita
Nikde není metrika „kolik mailů čeká nezařazeno", „kdy byl poslední přesun", ani alert při dlouhém období bez přesunu. Incident (7 týdnů ticha) by odhalil jediný jednoduchý alert.

---

## 5. Návrh opravy

Návrh je rozdělen do fází; nic z toho nezasahuje do mailboxu, dokud to t neodsouhlasí.

### Fáze 1 — rozmrazit zaseknuté maily (bezpečné, opakovatelné)
1. Přidat stav `retry` (nebo přepnout no-op `applied` záznamy zpět na `needs_llm`), aby `enqueue_candidate()` uměl přehodnotit mail, jehož `decision.folder == 'INBOX'`.
2. Zavést **pagination** v obou passech (projít celý INBOX po dávkách, ne jen 200/500).
3. Rekonciliace `manual_review`: záznamy `message not found` označit terminálně (`gone`) nebo smazat z fronty.

### Fáze 2 — zapojit skutečný modelový pass
4. Rozhodnout: buď (a) přidat cron pro `bezouska_llm_worker.py` (MAILF-001), nebo (b) přepsat second-pass tak, aby reálně volal LLM. Nedržet dva paralelní nesmysly.
5. Sjednotit klasifikaci do jednoho sdíleného modulu (`mail_rules.py`), aby triage i second-pass měly identické chování.
6. Sjednotit politiku `confirmed_only`.

### Fáze 3 — generátor pravidel
7. Nový krok „rule proposal": když model (vysoká confidence) zařadí N mailů od stejného senderu do stejné složky, založí se pravidlo s `review_status='pending'` → objeví se ve webu k odsouhlasení.
8. Oživit learn loop: (a) upozornit t, že `Labels/zatridil tomas` je prázdný, (b) doplnit fallback — generovat pravidla i z již provedených přesunů, ne jen z ručního labelu.

### Fáze 4 — observabilita a dohled
9. Metriky: počet nezařazených v INBOX, poslední úspěšný přesun, počet zaseknutých no-op záznamů.
10. Alert (Telegram): pokud X dní neproběhl žádný reálný přesun.

---

## 6. Dopad / co to znamená

- MailFilter **není rozbitý náhodou** — má 5 nezávislých děr, které se navzájem maskují (prázdná fronta + prázdný learn label + tiché crony), takže navenek „běží".
- Bez Fáze 1 se ani opravená pravidla neprojeví na 848+ zaseknutých mailech.
- Bez Fáze 2/3 zůstane „model based + návrh pravidel" jen na papíře.

---

## 7. Přílohy / důkazy

- Fronta: `state/bezouska-llm-queue.sqlite3` (queue: 921 applied / 848 fallback; learned_rules: 115)
- Triage běhy: `state/bezouska-mail-triage-runs.jsonl`, `logs/bezouska-inbox-triage.log`
- Second pass: `state/bezouska-llm-second-pass-runs.jsonl`
- Learn loop: `logs/bezouska-learn-from-zatridil-tomas.log`
- Crony: `crontab -l` (`bezouska-inbox-triage-cron.sh` */15, `bezouska-inbox-triage-llm-second-pass-cron.sh` 10,40, `bezouska-learn-from-zatridil-tomas-cron.sh` 25,55, `mailfilter-run-requests-cron.sh` * * * * *, `mailfilter-folders.sh` 47 3 * * *)
