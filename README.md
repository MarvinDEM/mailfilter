# MailFilter

Třídění pošty účtu bezouska podle pravidel + web pro revizi/správu pravidel.

**Web:** https://mailfilter.bezouska.cz (fallback: https://rules-review.srv1479985.hstgr.cloud)

## Architektura

```
┌─ Pipeline (host cron) ─────────────────────────────────────────┐
│ triage_bezouska_mail.py   → deterministická klasifikace INBOX  │
│   (každých 15 min, flock)                                       │
│   → enqueue kandidátů bez pravidla do LLM fronty               │
│ bezouska_llm_worker.py    → LLM klasifikace (needs_llm fronta) │
│ bezouska_llm_second_pass_worker.py → aplikace rozhodnutí       │
│ learn_from_zatridil_tomas.py → učení z ručního třídění         │
│ mailfilter-run-rule.py    → manuální spuštění jednoho pravidla │
└────────────────────────────────────────────────────────────────┘
┌─ Web (Docker) ─────────────────────────────────────────────────┐
│ server.py (Python http.server, port 8000)                      │
│ site/index.html (revize pravidel, CRUD, ⚡ Zpracovat INBOX)    │
│ Auth: shared-auth JWT (app_key=mailfilter)                     │
└────────────────────────────────────────────────────────────────┘
```

## Klíčové komponenty

| Soubor | Účel |
|---|---|
| `bin/triage_bezouska_mail.py` | Hlavní triage: pravidla → přesun + labely; bez pravidla → enqueue |
| `bin/mail_rules.py` | Datová vrstva pravidel (learned_rules v `state/bezouska-llm-queue.sqlite3`) |
| `bin/mailfilter-run-rule.py` | Manuální aplikace jednoho pravidla (přesun + labely) |
| `scripts/rules-review/server.py` | Web API (rules CRUD, review, run, inbox process) |
| `mail-sorting-rules.md` | Pravidla třídění (sémantika pro LLM + lidi) |
| `docker/docker-compose.yml` | Web kontejner (traefik, shared-auth volume) |

## Provozní pravidla (label vyresit)

- **`vyresit` label je vyhrazený jen pro emaily, kterým ho pravidlo přiřadí
  explicitně** (t, 2026-09-01). Emaily bez pravidla zůstávají v INBOX bez labelu,
  enqueue do LLM fronty se zachovává.
- Nezařazený mail se **nepřesouvá** do 99_nezatrideno (zrušeno t, 2026-08-25).

## Vývoj

Kanonická pracovní kopie skriptů je `/root/.openclaw/workspace/bin/` a
`/root/.openclaw/workspace/scripts/rules-review/` (běží z nich host crony a Docker
mount). Toto repo je verzovaný mirror — změny se sem kopírují a pushují.

Docs (Obsidian MD): `docs/` + kanonicky `project-hub/mailfilter/` na Proton Drive.
