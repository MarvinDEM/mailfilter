# MailFilter — samostatný projekt pro revizi a správu pravidel třídění pošty

**Date:** 2026-08-23
**Status:** deployed (část — čeká na DNS)

## Co to je

Přesun původního `rules-review` (revize pravidel třídění pošty bezouska) do
samostatného projektu na vlastní doméně **https://mailfilter.bezouska.cz**.
Web slouží ke schvalování naučených pravidel (ok / zahodit / vrátit),
úpravě existujících pravidel a — nově — **ručnímu zadávání nových filtrů**.

## Změny (2026-08-23)

### 1. Nový filtr přímo z webu (t: „aby se tam daly zadávat i nové filtry“)

- `bin/mail_rules.py` → nová funkce `create_rule(conn, fields)`:
  validace (sender i folder povinné), ručně zadané pravidlo platí rovnou
  (`review_status='ok'`, `active=1`), `source='manual-web'`.
- `scripts/rules-review/server.py` → `POST /api/rules` (201 + `{id}`),
  400 s českou chybou při nevalidním payloadu.
- `scripts/rules-review/site/index.html` → tlačítko **➕ Nový filtr**,
  modal se přepíná mezi „Nový filtr“ (POST) a „Upravit pravidlo“ (PUT),
  pole: odesílatel (povinné), příjemce, předmět obsahuje, složka (datalist),
  labely čárkou.

Verifikace: py_compile OK, JS `node --check` OK, live API test přes kontejner
(400 bez senderu / 201 + řádek v DB / cleanup testovacího řádku).

### 2. Přesun na mailfilter.bezouska.cz jako samostatný projekt

- Nový compose projekt: `/docker/mailfilter/docker-compose.yml`
  (container `mailfilter`, image python:3.12-slim, stejné bind mounts jako
  rules-review: server.py, site/, bin/mail_rules.py ro + state rw).
- Traefik routy (docker labels, network `deploy_default`):
  - `mailfilter` → `Host('mailfilter.bezouska.cz')`, websecure, letsencrypt
  - `mailfilter-legacy` → `Host('rules-review.srv1479985.hstgr.cloud')`
    (fallback, lesson 2026-05-31: staré adresy nechávat funkční)
- Starý container `rules-review` zastaven a odstraněn
  (`docker compose -f /docker/rules-review/docker-compose.yml down`);
  compose soubor zůstává jako reference.

Ověřeno live: `https://rules-review.srv1479985.hstgr.cloud/` → 200,
`/api/stats` → 49 pravidel (1 pending / 45 ok / 3 discard).

### 3. DNS — ČEKÁ NA ZÁSAH t (bez přístupu do Ignum adminu)

`mailfilter.bezouska.cz` dnes míří na `62.109.151.73` (Ignum wildcard
`*.bezouska.cz`). Pro zprovoznění je potřeba v admin.ignum.cz
vytvořit/změnit A záznam:

| Doména | Typ | Hodnota | TTL |
|--------|-----|---------|-----|
| `mailfilter.bezouska.cz` | A | `187.77.150.38` | 300 |

Stejný postup jako u `eupilot.bezouska.cz` / `polymarket.bezouska.cz`
(doc: `project-hub/shared/03-status-reports/2026-05-31_general_dns-bezouska-cz-setup_0v1.md`).
Explicitní A záznam přebije wildcard. Po propagaci (~5 min) Traefik sám
vydá Let's Encrypt certifikát (httpchallenge) — žádný další zásah netřeba.

## Bezpečnostní poznámka

Frontend je **bez autentizace** (stejně jako původní rules-review) — na
veřejné doméně je vidět struktura složek a adresy odesílatelů a kdokoli
může pravidla měnit. Doporučeno: Basic Auth middleware na Traefik routách
(heslo vygeneruji a předám). Rozhodnutí na t.

## Aktualizace 2026-08-23 (18:44) — auth jako CDprocesy (shared-auth), admin/admin

t: „přidej stejnou autentizaci, jako používáme v projektu cdprocesy, zatím admin/admin“.

- **Mechanismus:** shared-auth (stejná DB i JWT jako CDprocesy) — bcrypt heslo
  v `shared-auth/data/shared-auth.db`, přihlášení `POST /api/auth/login`,
  JWT HS256 Bearer token (24 h, app_key `mailfilter`, stejný secret jako CDprocesy).
- **Backend (scripts/rules-review/server.py):** `/api/auth/config`,
  `/api/auth/login`, `/api/auth/me`; všechny `/api/rules*` a `/api/stats`
  vyžadují platný Bearer token (jinak 401).
- **Frontend (site/index.html):** přihlašovací overlay (uživatel/heslo),
  token v localStorage (`mailfilter_token`), api() přidává Bearer header,
  při 401 automaticky na přihlášení, tlačítko ⎋ Odhlásit.
- **Účet:** `admin` / `admin` (user_id 24, membership mailfilter/admin active,
  is_platform_admin). Změna hesla = update bcrypt hashe v shared-auth DB
  (skriptem, mailfilter UI zatím hesla nemění).
  **2026-08-23 19:10:** přihlášení změněno na `tomas@bezouska.cz`
  (heslo zadal t — hodnota se neukládá do gitu; staré admin/admin neplatí).
  Ověřeno live: admin/admin → 401, tomas@bezouska.cz → token role admin.
- **Deploy:** Dockerfile (python:3.12-slim + bcrypt + PyJWT), compose přidán
  volume shared-auth (rw) + env SHARED_AUTH_DB_PATH / SHARED_AUTH_JWT_SECRET
  (sdílený s CDprocesy) / SHARED_AUTH_PUBLIC_BASE_URL.
- **Verifikace (live, i přes Traefik):** bez tokenu 401; špatné heslo 401;
  admin/admin → JWT; /me → role admin; /api/rules 49 pravidel;
  create filtr 201; testovací řádky uklizeny.

⚠️ admin/admin je dočasný účet — při publikaci na veřejnou doménu ho změnit.

## Aktualizace 2026-08-23 (18:51) — kompletní seznam složek v dropdownu

t: „v nabídce složek nevidím všechny složky (např. Folders/90_ostatni/94_notifikace). proč? oprav to.“

Příčina: datalist složek byl hardcoded (11 položek, zastaralý) — reálný mailbox má 300 složek.

Fix:
- `bin/mailfilter-folders.sh` (host cron `47 3 * * *`): dump reálných mailboxů bezouska účtu
  (himalaya folder list) → `state/mailfilter-folders.json` (300 položek, Folders/* + Labels/*).
- `server.py`: nový endpoint `GET /api/folders` (auth) — čte dump; fallback = složky z pravidel + statický seznam.
- `site/index.html`: `loadFolders()` načte reálný seznam z API a nahradí jím datalist (fallback = starý seznam).
- Ověřeno live: /api/folders → 300 složek, `Folders/90_ostatni/94_notifikace` přítomna; bez tokenu 401.

### Oprava 18:55 — pole „Složka“ jen Folders/*

t: „v poli Složka se mají zobrazovat jen Folders, nikoliv Labels, ty se zapisují z pole Labely“.
- `bin/mailfilter-folders.sh` dumpuje jen `Folders/*` (26 složek), Labels/* vyřazeny.
- `server.py` `_list_folders()` filtruje `Folders/*` i ve fallbacku (složky z pravidel + statický seznam).
- Ověřeno live: `/api/folders` = 26 složek, 0 Labels, `94_notifikace` přítomna.

### Aktualizace 19:52 — tabulka, datum vytvoření, manuální spuštění filtru

t: „přidej ke každému filtru datum vytvoření, výpis uprav do jednoduché tabulky, ke každému filtru přidej možnost ho manuálně spustit“.

- Výpis převeden z karet na **kompaktní tabulku**: Filtr (odesílatel × příjemce ×
  předmět) / Složka / Labely / Stav / **Vytvořeno** / Poslední běh / Akce.
- **Datum vytvoření** = `learned_at` (nový sloupec).
- **Manuální spuštění (▶):** `POST /api/rules/<id>/run` (auth) → request do
  `state/mailfilter-run-requests/` → host cron `mailfilter-run-requests-host`
  (každou minutu) → `bin/mailfilter-run-rule.py` (himalaya, účet bezouska):
  najde shody v INBOX (sender × recipient × subject_pattern, stejná logika jako
  triage), přesune do složky + nakopíruje na labely (Labels/<name>). Výsledek do
  `state/mailfilter-run-results/`, API ho vrací jako `last_run` (sloupec
  „Poslední běh“). Zahozené pravidlo nelze spustit.
- `mail_rules.get_rule()` přidán. Ověřeno live end-to-end: 202 queued → cron
  zpracoval → result `done` → `last_run` v API; testovací pravidlo uklizeno.

### Zprovoznění domény 18:58 — cert + HTTP→HTTPS

t: „dns záznam už je nějakou dobu upravený, ale doména zatím nefunguje“.
- DNS změna byla živá (veřejné resolvery → 187.77.150.38); lokální resolver hostu
  (Hostinger uplink 153.92.2.6) držel stale wildcard odpověď (62.109.151.73) i po
  `resolvectl flush-caches` → falešný dojem „nefunguje“. Diagnostika přes `dig @1.1.1.1`.
- LE cert: první ACME pokus 18:23 selhal (DNS ještě stará → 403/404 od Ignum);
  po restartu Traefiku cert vydán — CN=mailfilter.bezouska.cz (Let's Encrypt).
- Přidán HTTP→HTTPS redirect router (`mailfilter-http`, 302).
- Ověřeno: https://mailfilter.bezouska.cz/ 200 + validní cert, login + /api/folders OK,
  http → 302 https.

## Související

- Původní implementace: `scripts/rules-review/` (kód zůstává na stejném místě,
  jen deployment je nový projekt)
- Databáze: sdílená `state/bezouska-llm-queue.sqlite3` (tabulka learned_rules)
- Zdrojové soubory projektu: `/docker/mailfilter/docker-compose.yml`
