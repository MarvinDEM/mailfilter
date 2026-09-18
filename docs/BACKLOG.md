# BACKLOG — mailfilter

**Last update:** 2026-09-18 11:05 UTC
**Revize:** `01-docs/2026-09-18_mailfilter_revize-tridici-logiky_0v1.md` (detailní revize třídící logiky, 10 vad D1–D10)
**Implementace P0/P1:** `01-docs/2026-09-18_mailfilter_p0-p1-implementace_0v1.md` (MAILF-010/011/012/013/014/015/001)

---

## Projekt

MailFilter = třídění pošty účtu bezouska podle pravidel + web pro revizi/správu
pravidel (https://mailfilter.bezouska.cz).

- **Pipeline:** `triage_bezouska_mail.py` (deterministická klasifikace, host cron
  každých 15 min) → LLM fronta (`state/bezouska-llm-queue.sqlite3`) → second-pass
  worker + `learn_from_zatridil_tomas.py` (učení z ručního třídění)
- **Web:** Python http.server kontejner (`/docker/mailfilter`), shared-auth JWT,
  pravidla v `learned_rules` tabulce (`MAIL_RULES_DB=/data/bezouska-llm-queue.sqlite3`)
- **Dokumentace:** `project-hub/mailfilter/01-docs/`

## ⚠️ Aktuální stav (2026-09-18 11:05 UTC)

**P0/P1 opraveny, běží v režimu FÁZE 1 (mailbox se NEMĚNÍ — `MAILFILTER_APPLY=0`).**

- Triage nyní přečte **celý INBOX** (585 položek, paginace) — dřív viděla jen
  první okno 200.
- Fronta se odmrazila: 583 no-op mailů se re-enqueue-ovalo **jednorázově** přes
  `model_version` gate (žádný churn každých 15 min). 34 zombie `manual_review`
  záznamů → terminální `gone`.
- Oba passy používají **jednu** klasifikaci (`mail_rules.py`), `confirmed_only=True`.
- Druhý pass reálně volá LLM (deepseek-v4-flash přes lokální router), batch ≤20,
  cap volání/zpráv za běh, a z vysoko-konfidenčních rozhodnutí **navrhuje pravidla**
  (`pending`, čeká na odsouhlasení t ve webu).
- **FÁZE 1 (bezpečný režim):** při `MAILFILTER_APPLY=0` se rozhodnutí s cílem uloží
  jako `pending_apply` a **mailbox se nemění**; po přepnutí na `APPLY=1` se aplikují
  z uloženého rozhodnutí bez dalšího LLM volání. Nevyřešené maily zůstávají ve frontě.
- Smoke test: `python3 bin/tests/mailfilter-smoke.py` → 15/15 OK.

**Přesuny/labely v mailboxu se rozjedou až po explicitním odsouhlasení t**
(`MAILFILTER_APPLY=1` v cron wrapperu).

---

## FÁZE 1 — Rozmrazit zaseknuté maily (priorita P0, bez zásahu do mailboxu)

- [x] **MAILF-010** Fronta uzamkla no-op maily navždy  — *(D2)*  ✅ 2026-09-18
  - priority: P0
  - `enqueue_candidate()` vrací False pro `status='applied'`. 848 no-op záznamů
    (`decision.folder=='INBOX'`) se nikdy nepřehodnotí, i když od té doby vznikla
    nová pravidla.
  - **Řešení:** místo nového stavu `retry` zaveden `model_version` gate —
    no-op `applied` se re-enqueue-uje **jen když se změní klasifikační model**
    (`meta.model_version`, bump při každé změně pravidel). Bez churn každých 15 min.

- [x] **MAILF-011** Stránkování skrývá většinu INBOXu — *(D3)*  ✅ 2026-09-18
  - priority: P0
  - Triage čte `--page-size 200`, second pass `500`; INBOX má 585 položek →
    385 mailů se nikdy nezhodnotí (21 neprošlo frontou vůbec).
  - **Řešení:** `list_env_all()` v obou passech — loop přes `himalaya --page`
    (`HIMALAYA_PAGE_SIZE=200`/`500`, `HIMALAYA_MAX_PAGES=20`), stop na krátké stránce/chybě.
    Živé ověření: triage přečetl 585/585.

- [x] **MAILF-012** Zombie `manual_review` záznamy — *(D7)*  ✅ 2026-09-18
  - priority: P1
  - 34 záznamů, všechny důvod `message not found in INBOX`; bez rekonciliace
    zůstávají navěky a zkreslují stav.
  - **Řešení:** second pass při každém běhu rekonciluje `manual_review` proti
    INBOXu — zpráva v INBOX není → terminální stav `gone`; zpráva se vrátila →
    re-enqueue `needs_llm`.

## FÁZE 2 — Zapojit skutečný modelový pass (priorita P1)

- [x] **MAILF-001** Skutečný LLM klasifikační worker nemá cron  — *(D1, obnoveno)*  ✅ 2026-09-18
  - priority: P1
  - `bin/bezouska_llm_worker.py` (volá `openclaw agent --local`) **není v crontabu**
    → mrtvý kód. „Druhý pass" v cronu (`bezouska_llm_second_pass_worker.py`) byl
    jen hardcoded kopie heuristiky z triage, **žádné LLM nevolal**.
  - **Rozhodnutí: varianta (b)** — second pass přepsán na reálné LLM volání.
    Volá lokální LiteLLM router (`http://127.0.0.1:4000/v1`), model
    `deepseek/deepseek-v4-flash`, batch ≤20 zpráv/call, capy
    `MAILFILTER_MAX_LLM_CALLS` (default 2) a `MAILFILTER_MAX_MESSAGES` (default 20)
    na běh. Feature flag `MAILFILTER_LLM_ENABLED`, dry-run `MAILFILTER_LLM_DRYRUN=1`.
    Model smí vybrat jen existující složku (`state/mailfilter-folders.json`).
    Při HTTP 402 / „Insufficient Balance" / „insufficient_quota" letí
    deterministický Telegram alert (`bin/escalate.py`, case `mailfilter-llm-quota`).
    `bezouska_llm_worker.py` zůstává mrtvý — nedržet obojí.

- [x] **MAILF-013** Duplikovaná klasifikační logika (drift) — *(D8)*  ✅ 2026-09-18
  - priority: P1
  - `classify_folder()`/`collect_labels()`/konstanty zkopírované v triage i
    second-pass; sdílený `mail_rules.py` držel jen matcher pravidel.
  - **Řešení:** kanonická klasifikace (`classify_folder`, `collect_labels`,
    `classify_message`) je v `mail_rules.py`; triage i second pass delegují.
    Diferenční test na fixture 200 mailů: **0 drift** folder/reason/labels.
    Ověřeno grepem: v `bin/` existuje jen jediná definice.

- [x] **MAILF-014** Nekonzistentní `confirmed_only` mezi passy — *(D6)*  ✅ 2026-09-18
  - priority: P2 (vyřešeno spolu s MAILF-013)
  - Triage: `confirmed_only=False` (bere i pending pravidla); second pass: `True`
    (jen `review_status='ok'`). Stejný mail → různé rozhodnutí.
  - **Řešení:** sjednoceno na `confirmed_only=True` v obou passech — aplikovat
    nereviewovaná pravidla na živou poštu je riskantnější.

## FÁZE 3 — Generátor nových pravidel (priorita P1–P2)

- [x] **MAILF-015** Chybí návrh nových pravidel z nezařazených mailů — *(D4)*  ✅ 2026-09-18
  - priority: P1
  - „Second pass s návrhem nových pravidel" nebyl v kódu nikde;
    `upsert_learned_rule()` volal jen `learn_from_zatridil_tomas.py`.
  - **Řešení (dvě cesty, obě bez LLM kreditů pro clustering):**
    1. `mail_rules.propose_rule()` — z vysoko-konfidenčních LLM rozhodnutí
       (≥0.8) založí návrh pravidla `review_status='pending'`, `active=0`.
       Model NIKDY nezapisuje aktivní pravidlo.
    2. `bin/mailfilter-rule-proposals.py` — deterministický clustering:
       stejný sender N× (default 3) ve stejné složce a žádné existující pravidlo
       → návrh `pending` k odsouhlasení ve webu. Běží dry-run/JSON.

- [ ] **MAILF-016** Mrtvá učící smyčka (`Labels/zatridil tomas` prázdný) — *(D5)*
  - priority: P2
  - `learn_from_zatridil_tomas.py` je funkční, ale zdrojový label má **0 mailů**;
    bez ručního třídění t negeneruje nic.
  - Řešení: (a) upozornit t, (b) doplnit fallback — učit i z již provedených
    přesunů, ne jen z ručního labelu.

## FÁZE 4 — Observabilita (priorita P2)

- [ ] **MAILF-017** Chybí metriky a alert na ticho v třídění — *(D10)*
  - priority: P2
  - Nikde není „počet nezařazených v INBOX", „poslední úspěšný přesun", ani alert.
    7 týdnů bez přesunu by odhalil jeden alert.
  - Řešení: metriky + Telegram alert, pokud X dní neproběhl reálný přesun.

## NÍZKÉ / úklid

- [x] **MAILF-018** Mrtvé větve v triage — *(D9)*  ✅ 2026-09-18 (částečně)
  - priority: P3
  - `if decision is None` je nedosažitelné → **odstraněno** v triage.
  - Zbývá: maily s `vyresit` bez shody se přeskočí bez zápisu stavu (vědomě, jde
    o dedup proti re-labelu každých 15 min).

---

## Otevřené položky (starší, stále platné)

- [ ] **MAILF-003** Účet lodivod má 2FA → tokeny rclone časem expirují/ztratí se
  - priority: P3
  - Postup: `runbooks/proton-drive.md` sekce "Re-auth protondrive (2FA)".
  - Healthcheck každé 4 h alertuje — dostatečné.

## Vyřešené / uzavřené

- 2026-09-18: detailní revize třídící logiky (10 vad D1–D10), report v `01-docs/`
- 2026-09-01: `MAILF-002` mailfilter zaveden jako plnohodnotný projekt v project-hub
  (canonical struktura) + přidán do `PROJECTS` v `scripts/sync-proton-projects.sh`
- 2026-08-23: deploy webu mailfilter.bezouska.cz + fallback rules-review.srv1479985.hstgr.cloud
- 2026-08-24: IMAP label copy fix (Proton Bridge UID COPY rozbitý → sekvenční COPY),
  label sync helpery
- 2026-08-25: zrušena pravidla pro 99_nezatrideno → nezařazený mail zůstává v INBOX
- 2026-09-01: label `vyresit` jen z explicitního pravidla (+ cleanup 390 INBOX emailů)
- 2026-09-01: manuální spuštění kompletního zpracování INBOX z webu (⚡ Zpracovat INBOX)
