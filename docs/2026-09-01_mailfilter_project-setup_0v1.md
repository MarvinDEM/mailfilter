---
tags: [mailfilter, project-setup, sync, project-hub]
date: 2026-09-01
projekt: mailfilter
verze: 0v1
---

# Založení mailfilter jako plnohodnotného projektu (2026-09-01)

## Zadání (t)

> Zaveď mailfilter jako nový projekt včetně zahrnutí do sync scope a dalších náležitostí.

## Co bylo provedeno

### 1. Canonical struktura project-hub/mailfilter/

```
project-hub/mailfilter/
├── 01-docs/          (existovala — 3 dokumenty)
├── 02-backlog/       (nový — BACKLOG.md)
├── 03-status-reports/ (nový)
├── 04-releases/      (nový)
├── 05-deliverables/  (nový)
└── 99-archive-links/ (nový)
```

### 2. BACKLOG.md

Založen se známými položkami:
- **MAILF-001 (HIGH):** LLM worker (`bezouska_llm_worker.py`) nemá cron — známá díra
  od 2026-08-03, čeká na rozhodnutí t
- **MAILF-002 (MEDIUM, vyřešeno):** mailfilter nebyl v sync scope
- **MAILF-003 (NÍZKÉ):** opakovaný 2FA re-auth protondrive (runbook existuje)

### 3. Sync scope (lodivod ↔ alzbeta)

- `scripts/sync-proton-projects.sh` → `PROJECTS=(... "mailfilter")`
- Proton Drive: adresáře vytvořeny na `protondrive:project-hub/mailfilter/` i
  `proton-alzbeta:project-hub/mailfilter/` (všechny canonical podsložky)
- Sync ověřen: `bash scripts/sync-proton-projects.sh` → OK bez chyb
  (`directory not found` se neobjevil — bootstrap checklist splněn)
- BACKLOG.md + 3 docs nahrány na oba účty
- `TOOLS.md` aktualizováno (sync scope sekce)

### 4. Git

- project-hub: commit `e0af456` (struktura + BACKLOG)
- workspace: commit `894a98b4` (sync skript) + `aee70d0b` (TOOLS.md)
- Vše pushnuto (MarvinDEM/project-hub + MarvinDEM/openclaw-workspace)

## Co se NEzměnilo (a proč)

- **Git backup targets** (`config/project_git_backup_targets.json`): mailfilter nemá
  vlastní git repo (žije ve workspace + project-hub), takže tam nepatří — project-hub
  se zálohuje jako celek.
- **MEMORY.md definice „projekty" pro git backup** (2026-04-28): týká se samostatných
  repozitářů — mailfilter nepřidán.

## Ověření

- `rclone lsf protondrive:project-hub/mailfilter/` → 01-docs/, 02-backlog/, 03-status-reports/, 04-releases/, 05-deliverables/, 99-archive-links/
- `rclone lsf proton-alzbeta:project-hub/mailfilter/02-backlog/` → BACKLOG.md
- Sync skript EXIT 0, žádný email o selhání

## Dodatek 2026-09-01 21:42 — samostatné GitHub repo

- **Repo:** `MarvinDEM/mailfilter` (https://github.com/MarvinDEM/mailfilter), public
- **Obsah:** mirror pracovního kódu — `bin/` (triage, LLM worker, second-pass, run-rule,
  label helpery, cleanup), `scripts/rules-review/` (web), `docker/` (compose+Dockerfile),
  `docs/` (Obsidian MD), `mail-sorting-rules.md`, README
- **Pozn.:** kanonická pracovní kopie zůstává v `/root/.openclaw/workspace/bin/` a
  `scripts/rules-review/` (host crony + Docker mount z nich běží); repo je verzovaný
  mirror — změny se kopírují a pushují
- **Denní push:** přidán do `bin/github-push-projects-cron.sh` (5:10 Prague)
- **MEMORY.md:** mailfilter přidán do seznamu MarvinDEM repo
