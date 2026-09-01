#!/usr/bin/env python3
import json
import subprocess
import traceback
from collections import Counter
from pathlib import Path

ACCOUNT = "bezouska"
REPORT = Path("/root/.openclaw/workspace/tmp/bezouska-inbox-process-2026-07-28.json")

NEWS_SENDERS = {
    "james@jamesclear.com",
    "sahil@sahilbloom.com",
    "updates-noreply@linkedin.com",
    "events@camunda.com",
    "newsletter@asociace.ai",
    "bingo@patreon.com",
    "contact@blacktailstudio.com",
    "insidercz@substack.com",
    "make-events@make.com",
    "anezka@mailing.byro.works",
    "workspace-noreply@google.com",
    "googleworkspace-noreply@google.com",
}

TRANSACTION_SENDERS = {
    "faktura@nordictelecom.cz",
    "payments-noreply@google.com",
    "no_reply@email.apple.com",
    "payments@comgate.cz",
    "hypotecni.zona@csobhypotecni.cz",
    "info@rb.cz",
    "e-bill@eon.cz",
    "shelfie@shelfie.cz",
    "sluzebnicek@alza.cz",
    "service@intl.paypal.com",
    "info@allianz.cz",
    "info@info.koop.cz",
    "no-reply@spotify.com",
    "info@account.netflix.com",
    "upcoming-invoice@calendarbridge.com",
    "objednavky@prodej-slunecnice.cz",
    "postmaster@falcokrmiva.com",
}

ACTION_SENDERS = {
    "hello@crypto.com",
    "info@mail.coinbase.com",
    "support@sc.mail.deepseek.com",
    "no-reply@mail.proton.me",
    "no-reply@moonpay.com",
    "no-reply@mail.privy.io",
    "noreply@business.facebook.com",
    "noreply.services@602.cz",
    "team@tresorit.com",
    "aplikace@vaspraktikpraha.cz",
}

WORK_DOMAIN_MAP = [
    ("@deltaadvisory.cz", "Folders/50_pracovni/80_delta", []),
    ("@inadvisors.cz", "Folders/50_pracovni/52_inadvisors", []),
    ("@ipsd.cz", "Folders/50_pracovni/53_ipsd", ["03_ipsd"]),
    ("@eximex.cz", "Folders/50_pracovni/53_ipsd", ["03_ipsd"]),
    ("@mmr.gov.cz", "Folders/50_pracovni/54_mmr", []),
    ("@bezouska.cz", "Folders/50_pracovni/51_bezouska", []),
]

TRANSACTION_KEYWORDS = [
    "invoice",
    "faktura",
    "vyúčtování",
    "vyuctovani",
    "výpis",
    "vypis",
    "hypotéce",
    "hypotece",
    "payment",
    "platba",
    "subscription",
    "předplatné",
    "predplatne",
    "objednávka",
    "objednavka",
    "zásilka",
    "zasilka",
    "stvrzenka",
    "receipt",
    "renew",
    "billing",
    "pojistné",
    "pojisteni",
]

ACTION_KEYWORDS = [
    "verification code",
    "login code",
    "verify",
    "ověřte",
    "overte",
    "trusted",
    "new device",
    "action required",
    "urgent",
    "proof of address",
    "partner request",
    "paused",
    "notice",
    "vyprší",
    "vyprsi",
    "submit a new",
    "review your",
    "suspended",
]


def run(*args):
    proc = subprocess.run(list(args), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"command failed: {args}")
    return proc.stdout


def list_env(folder="INBOX", page_size="500"):
    return json.loads(run("himalaya", "envelope", "list", "-a", ACCOUNT, "-f", folder, "--page-size", page_size, "--output", "json"))


def move(src, dst, mid):
    run("himalaya", "message", "move", "-a", ACCOUNT, "-f", src, dst, str(mid))


def copy_label(src, label, mid):
    # Sekvenční IMAP COPY — UID COPY je v Proton Bridge rozbitý (2026-08-24)
    run("python3", "/root/.openclaw/workspace/bin/imap-label-copy.py", src, f"Labels/{label}", str(mid))


def has_any(text, needles):
    text = (text or "").lower()
    return any(needle in text for needle in needles)


def classify(msg):
    sender = ((msg.get("from") or {}).get("addr") or "").lower()
    subject = msg.get("subject") or ""
    subject_l = subject.lower()
    labels = []

    if "zahálka" in subject_l or "zahalka" in subject_l:
        labels.append("50_osobni")
        if has_any(subject, TRANSACTION_KEYWORDS):
            labels.append("faktury")
        return "Folders/10_osobni/33_zahalka", labels, "zahalka"

    if "zvole" in subject_l or "klima" in subject_l:
        labels.append("50_osobni")
        return "Folders/10_osobni/31_zvole", labels, "zvole"

    if sender in NEWS_SENDERS or "newsletter" in sender or "newsletter" in subject_l or "3-2-1:" in subject_l or "reacted to this post" in subject_l:
        labels.append("newsletter")
        return "Folders/90_ostatni/91_newsletter", labels, "newsletter"

    if sender == "info@indoc.cz" or "o veřejných zakázkách" in subject_l or "o verejnych zakazkach" in subject_l:
        labels.append("03_ipsd")
        return "Folders/50_pracovni/53_ipsd", labels, "ipsd-news"

    for domain, folder, extra_labels in WORK_DOMAIN_MAP:
        if domain in sender:
            labels.extend(extra_labels)
            return folder, labels, f"work-domain:{domain}"

    if sender == "support@webglobe.zendesk.com":
        return "Folders/50_pracovni/70_prazske-noviny", labels, "webglobe-pn"

    if sender.endswith("@agenturacas.gov.cz"):
        return "Folders/50_pracovni/51_bezouska", labels, "agenturacas"

    if sender == "petr@stiegler.cz" or subject.strip().lower() == "pn":
        return "Folders/50_pracovni/70_prazske-noviny", labels, "pn-signal"

    if sender in TRANSACTION_SENDERS or has_any(subject, TRANSACTION_KEYWORDS):
        labels.append("faktury")
        if sender in {"shelfie@shelfie.cz", "sluzebnicek@alza.cz", "service@intl.paypal.com", "postmaster@falcokrmiva.com", "objednavky@prodej-slunecnice.cz"}:
            labels.append("50_osobni")
        return "Folders/90_ostatni/92_transakce", labels, "transaction"

    if sender in ACTION_SENDERS or has_any(subject, ACTION_KEYWORDS):
        labels.append("vyresit")
        return None, labels, "action-needed"

    if sender.endswith("@gmail.com") or sender.endswith("@seznam.cz") or sender.endswith("@proton.me") or sender.endswith("@protonmail.com"):
        labels.append("50_osobni")
        return "Folders/10_osobni/11_tomas", labels, "personal-contact"

    if sender.endswith(".gov.cz") or ".gov.cz" in sender or "@psp.cz" in sender:
        return None, labels, "gov-fallback"

    if sender == "m@dpmapps.com":
        labels.append("50_osobni")
        return "Folders/10_osobni/11_tomas", labels, "personal-app"

    return None, labels, "fallback"


def main():
    inbox = list_env("INBOX", "500")
    processed = []
    errors = []
    folder_counts = Counter()
    reason_counts = Counter()

    for msg in sorted(inbox, key=lambda m: int(m["id"])):
        mid = msg["id"]
        folder, labels, reason = classify(msg)
        labels = list(dict.fromkeys(labels))
        try:
            if folder is not None:
                move("INBOX", folder, mid)
            src = folder if folder is not None else "INBOX"
            for label in labels:
                copy_label(src, label, mid)
            processed.append({
                "id": mid,
                "subject": msg.get("subject"),
                "from": (msg.get("from") or {}).get("addr"),
                "folder": folder or "INBOX",
                "labels": labels,
                "reason": reason,
            })
            folder_counts[folder] += 1
            reason_counts[reason] += 1
        except Exception as exc:
            errors.append({
                "id": mid,
                "subject": msg.get("subject"),
                "from": (msg.get("from") or {}).get("addr"),
                "folder": folder,
                "labels": labels,
                "reason": reason,
                "error": str(exc),
            })

    remaining = list_env("INBOX", "500")
    report = {
        "processed_count": len(processed),
        "error_count": len(errors),
        "remaining_inbox": len(remaining),
        "folder_counts": dict(folder_counts),
        "reason_counts": dict(reason_counts),
        "processed_sample": processed[:50],
        "errors": errors[:50],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
