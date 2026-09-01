---
tags: [mailfilter, bezouska, triage, manual-run, inbox]
date: 2026-09-01
projekt: mailfilter
verze: 0v1
---

# Manuální spuštění kompletního zpracování INBOX (2026-09-01)

## Zadání (t)

> Na web https://mailfilter.bezouska.cz/ přidej možnost manuálně spustit komplet
> zpracování inboxu podle všech pravidel.

## Řešení

Tlačítko **„⚡ Zpracovat INBOX"** v headeru webu (vedle „↻ Obnovit"). Spustí stejný
pipeline jako automatický triage cron (`triage_bezouska_mail.py`): projede celý INBOX,
aplikuje všechna aktivní pravidla (přesun + labely), nezařazené enqueue do LLM fronty.

### Architektura (stejný vzor jako run-rule)

```
Web (kontejner)                        Host cron (každou minutu)
─────────────────                      ─────────────────────────
POST /api/inbox/process                mailfilter-run-requests-cron.sh
  → marker inbox-<ts>.json               → mv *.processing
    v state/mailfilter-inbox-requests/    → triage_bezouska_mail.py
                                         → výsledek inbox-<ts>.json
GET /api/inbox/process                    v state/mailfilter-inbox-results/
  → {running, last}                     ← frontend poll (5s)
```

### API

| Endpoint | Chování |
|---|---|
| `GET /api/inbox/process` | `{running: bool, last: {status, summary, errors, finished_at}}` |
| `POST /api/inbox/process` | 202 + marker; **409** pokud běží jiné pravidlo nebo inbox processing |

### Frontend

- Tlačítko ⚡ Zpracovat INBOX (modré) + potvrzení
- Během běhu: ⏳ Zpracovávám INBOX… (disabled)
- Po dokončení: ✅ zpracováno · přesunuto N · kandidátů LLM N · chyb N + čas
- Polling 5 s (stejný vzor jako run-rule `_waiting`)

## Ověřeno (E2E)

1. `POST /api/inbox/process` → `{"ok": true, "queued": true}` (202)
2. Host cron (běží každou minutu) → `triage_bezouska_mail.py` → EXIT 0
3. Result JSON: `status: done`, summary `{processed_count: 0, llm_candidate_count: 200, enqueued_count: 2}`
4. `GET /api/inbox/process` vrací `running: false` + `last` se summary

## Poznámky

- **errors: 3** v prvním běhu = IMAP timeouty himalaya (bridge dočasně pomalý, viz
  incident 13:30–13:45) — triage je přežil (retry logika), status done
- Žádná změna v `triage_bezouska_mail.py` — manuální běh = stejný kód jako cron
- Sdílený lock: cron-exec (flock) chrání před souběhem s automatickým během; webová
  blokace 409 chrání před souběhem s run-rule

## Lekce

1. **Python scoping past:** `from datetime import datetime` lokálně uvnitř jedné větve
   `do_POST` způsobí `UnboundLocalError` v jiné větvi téže funkce (Python vidí jméno
   jako lokální pro celou funkci). Fix: alias import `from datetime import datetime as _dt`.
2. **Stejný vzor marker → cron → result** jako run-rule je osvědčený (fronta běží
   každou minutu, frontend polluje) — nová funkce nemusela vymýšlet nic nového.
