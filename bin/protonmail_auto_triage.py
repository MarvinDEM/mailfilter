#!/usr/bin/env python3
import json, os, re, shlex, socket, subprocess, sys, time
from pathlib import Path

ACCOUNT = 'lodivod'
WORKSPACE = Path('/root/.openclaw/workspace')
STATE_DIR = WORKSPACE / 'state'
STATE_FILE = STATE_DIR / 'protonmail-auto-triage.json'
LOG_DIR = WORKSPACE / 'tmp'
LOG_FILE = LOG_DIR / 'protonmail-auto-triage.log'
BRIDGE_CMD = ['/usr/lib/protonmail/bridge/bridge', '--noninteractive']

LABEL_REVIEW = 'Labels/revidovat'
CATEGORY_MAP = {
    'alerts': 'alert',
    'newsletters': 'newsletter',
    'transactions': 'transaction',
    'social': 'social',
    'work': 'work',
    'personal': 'personal',
    'other': 'other',
}


def log(msg):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] {msg}\n"
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(line)
    print(msg)


def run(cmd, check=True, capture=True):
    res = subprocess.run(cmd, text=True, capture_output=capture)
    if check and res.returncode != 0:
        raise RuntimeError(f"command failed ({res.returncode}): {' '.join(map(shlex.quote, cmd))}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
    return res


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def ensure_bridge():
    if port_open(1143) and port_open(1025):
        return
    subprocess.Popen(BRIDGE_CMD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if port_open(1143) and port_open(1025):
            log('Proton Bridge is up')
            return
        time.sleep(2)
    raise RuntimeError('Proton Bridge did not start in time')


def ensure_folder(name):
    run(['himalaya', 'folder', 'add', '-a', ACCOUNT, name], check=False)


def himalaya_json(args):
    res = run(['himalaya', *args], capture=True)
    out = res.stdout.strip()
    return json.loads(out) if out else None


def message_text(msg_id):
    res = run(['himalaya', 'message', 'read', '-a', ACCOUNT, '-f', 'INBOX', msg_id], check=False)
    return (res.stdout or '')[:8000]


def classify(env, body):
    sender = ((env.get('from') or {}).get('addr') or '').lower()
    sender_name = ((env.get('from') or {}).get('name') or '').lower()
    subject = (env.get('subject') or '').lower()
    text = ' '.join([sender, sender_name, subject, body.lower()[:3000]])

    if any(x in text for x in ['battery status', 'notification of the battery status', 'fibaro', 'alert', 'alarm', 'security alert', 'critical', 'warning']):
        return 'alerts'
    if any(x in text for x in ['invoice', 'receipt', 'order', 'payment', 'účten', 'faktur', 'vyúčtování', 'bill', 'subscription receipt']):
        return 'transactions'
    if any(x in text for x in ['newsletter', 'unsubscribe', 'digest', 'mail.ft.com', 'stories-features', 'smart money is buying', 'breaking news in the last 24hrs']):
        return 'newsletters'
    if any(x in text for x in ['linkedin', 'facebook', 'instagram', 'x.com', 'twitter', 'polymarket', 'login code', 'notification', 'mention']):
        return 'social'
    if any(x in text for x in ['meeting', 'projekt', 'proposal', 'contract', 'smlouva', 'client', 'customer', 'invoice due']):
        return 'work'
    if any(x in text for x in ['family', 'friend', 'osobní', 'personal']):
        return 'personal'
    return 'other'


def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    return {'processed': {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def key_for(env):
    frm = (env.get('from') or {}).get('addr') or ''
    return f"{env.get('date','')}|{frm}|{env.get('subject','')}"


def main():
    ensure_bridge()
    ensure_folder(LABEL_REVIEW)
    for cat, tag in CATEGORY_MAP.items():
        ensure_folder(f'Folders/openclaw-{cat}')
        ensure_folder(f'Labels/openclaw-{tag}')

    envs = himalaya_json(['envelope', 'list', '-a', ACCOUNT, '-f', 'INBOX', '-o', 'json', 'not', 'flag', 'seen']) or []
    state = load_state()
    processed = state.setdefault('processed', {})
    changed = 0

    for env in envs:
        msg_id = str(env.get('id'))
        key = key_for(env)
        if processed.get(key):
            continue
        body = message_text(msg_id)
        category = classify(env, body)
        tag = CATEGORY_MAP[category]
        target_folder = f'Folders/openclaw-{category}'
        target_label = f'Labels/openclaw-{tag}'

        run(['himalaya', 'message', 'copy', '-a', ACCOUNT, '-f', 'INBOX', LABEL_REVIEW, msg_id], check=False)
        run(['himalaya', 'message', 'copy', '-a', ACCOUNT, '-f', 'INBOX', target_label, msg_id], check=False)
        run(['himalaya', 'message', 'move', '-a', ACCOUNT, '-f', 'INBOX', target_folder, msg_id], check=False)

        processed[key] = {
            'id': msg_id,
            'category': category,
            'label': target_label,
            'folder': target_folder,
            'processedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        changed += 1
        log(f"Processed {msg_id}: {env.get('subject','')} -> {target_folder} + {target_label} + {LABEL_REVIEW}")

    save_state(state)
    log(f'Run complete, processed {changed} message(s)')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(f'ERROR: {e}')
        sys.exit(1)
