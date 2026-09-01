#!/usr/bin/env python3
import argparse
import importlib.util
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/root/.openclaw/workspace')
ACCOUNT = 'bezouska'
PRAGUE = ZoneInfo('Europe/Prague')
START_DEFAULT = datetime(2026, 6, 1, 0, 0, 0, tzinfo=PRAGUE)
LABELS_TO_INDEX = [
    '00_platby',
    'faktury',
    'finance',
    'newsletter',
    'vyresit',
    '03_ipsd',
    '50_osobni',
]


def load_triage_module():
    path = ROOT / 'bin' / 'triage_bezouska_mail.py'
    spec = importlib.util.spec_from_file_location('triage_mod', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_json(*args):
    try:
        out = subprocess.check_output(list(args), text=True, stderr=subprocess.STDOUT, timeout=60)
    except subprocess.CalledProcessError:
        # read-only listing: prázdná stránka / konec paginace → himalaya exit 1
        return []
    # himalaya/proton bridge píší WARN řádky s ANSI kódy na stderr — po mergi
    # by rozbily JSON parsing; ANSI odstraníme a JSON hledáme od prvního '[' / '{'
    out = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', out)
    if not out.strip():
        return []
    starts = [i for i in (out.find('['), out.find('{')) if i != -1]
    if not starts:
        raise RuntimeError(f'could not parse JSON from command output: {" ".join(args)}\n{out[:800]}')
    return json.loads(out[min(starts):])


def list_folders():
    rows = run_json('himalaya', 'folder', 'list', '-a', ACCOUNT, '--output', 'json')
    return [row['name'] for row in rows]


def list_envelopes(folder, page_size=100):
    page = 1
    while True:
        rows = run_json(
            'himalaya', 'envelope', 'list',
            '-a', ACCOUNT,
            '-f', folder,
            '--page', str(page),
            '--page-size', str(page_size),
            '--output', 'json',
        )
        if not rows:
            break
        for row in rows:
            yield row
        if len(rows) < page_size:
            break
        page += 1


def iter_messages_in_window(folder, start_dt, end_dt, page_size=100):
    page = 1
    while True:
        rows = run_json(
            'himalaya', 'envelope', 'list',
            '-a', ACCOUNT,
            '-f', folder,
            '--page', str(page),
            '--page-size', str(page_size),
            '--output', 'json',
        )
        if not rows:
            break
        for row in rows:
            try:
                dt_local = parse_dt(row.get('date')).astimezone(PRAGUE)
            except Exception:
                continue
            if dt_local < start_dt or dt_local > end_dt:
                continue
            yield row, dt_local
        page += 1


def parse_dt(raw):
    return datetime.fromisoformat(raw)


def norm_key(msg):
    return (
        msg.get('date') or '',
        ((msg.get('from') or {}).get('addr') or '').lower(),
        msg.get('subject') or '',
    )


def source_priority(folder):
    if folder == 'INBOX':
        return 0
    if folder.startswith('Folders/'):
        return 1
    if folder == 'Archive':
        return 2
    return 9


def is_source_folder(name):
    if name in {'INBOX', 'Archive', 'Folders/99_nezatrideno'}:
        return True
    return (
        name.startswith('Folders/10_osobni/')
        or name.startswith('Folders/50_pracovni/')
        or name.startswith('Folders/90_ostatni/')
    )


def build_source_inventory(start_dt, end_dt):
    allowed = getattr(build_source_inventory, '_allowed_folders', None)
    if allowed:
        folders = [name for name in allowed if is_source_folder(name)]
    else:
        folders = [name for name in list_folders() if is_source_folder(name)]
    indexed = {}
    scanned = 0
    for folder in folders:
        for msg, dt_local in iter_messages_in_window(folder, start_dt, end_dt):
            scanned += 1
            key = norm_key(msg)
            row = {'folder': folder, 'msg': msg, 'dt': dt_local}
            prev = indexed.get(key)
            if prev is None or source_priority(folder) < source_priority(prev['folder']):
                indexed[key] = row
    return scanned, list(indexed.values())


def build_label_index(start_dt, end_dt):
    label_index = {label: set() for label in LABELS_TO_INDEX}
    for label in LABELS_TO_INDEX:
        folder = f'Labels/{label}'
        try:
            for msg, _dt in iter_messages_in_window(folder, start_dt, end_dt):
                label_index[label].add(norm_key(msg))
        except Exception:
            continue
    return label_index


def build_folder_index(start_dt, end_dt):
    folder_index = defaultdict(set)
    allowed = getattr(build_folder_index, '_allowed_folders', None)
    folders = [name for name in allowed if is_source_folder(name)] if allowed else [name for name in list_folders() if is_source_folder(name)]
    for name in folders:
        try:
            for msg, _dt in iter_messages_in_window(name, start_dt, end_dt):
                folder_index[name].add(norm_key(msg))
        except Exception:
            continue
    return folder_index


def copy_label(src_folder, label, mid):
    subprocess.run(
        ['himalaya', 'message', 'copy', '-a', ACCOUNT, '-f', src_folder, f'Labels/{label}', str(mid)],
        check=True, capture_output=True, text=True,
    )


def move_message(src_folder, dst_folder, mid):
    subprocess.run(
        ['himalaya', 'message', 'move', '-a', ACCOUNT, '-f', src_folder, dst_folder, str(mid)],
        check=True, capture_output=True, text=True,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2026-06-01')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--folder', action='append', default=[])
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=PRAGUE)
    end_dt = datetime.now(PRAGUE)
    triage = load_triage_module()
    allowed = set(args.folder) if args.folder else None
    build_source_inventory._allowed_folders = allowed
    build_folder_index._allowed_folders = allowed
    scanned, items = build_source_inventory(start_dt, end_dt)
    label_index = None
    folder_index = None
    db = triage.db_connect()

    summary = {
        'mode': 'apply' if args.apply else 'dry-run',
        'window': {'start': start_dt.isoformat(), 'end': end_dt.isoformat()},
        'scanned_source_rows': scanned,
        'selected_unique_messages': len(items),
        'moved': 0,
        'labeled': 0,
        'unchanged': 0,
        'skipped_folder_duplicate': 0,
        'errors': 0,
        'by_target_folder': Counter(),
        'by_label': Counter(),
        'samples': [],
        'failures': [],
    }
    processed_actions = 0

    for row in sorted(items, key=lambda x: (x['dt'], source_priority(x['folder']), int(x['msg'].get('id') or 0))):
        source_folder = row['folder']
        msg = row['msg']
        key = norm_key(msg)
        decision = triage.deterministic_classify(db, msg)
        target_folder = decision['folder']
        target_labels = triage.uniq(decision['proton_labels'])

        # t (2026-09-01): folder=None (žádné pravidlo) → zůstává v INBOX BEZ automatického labelu vyresit;
        # aplikují se jen labely, které pravidlo přiřadilo explicitně
        if target_folder is None:
            target_folder = 'INBOX'

        needs_move = source_folder != target_folder
        needs_labels = list(target_labels)

        if args.limit and processed_actions >= args.limit:
            continue

        summary['by_target_folder'][target_folder] += 1
        for label in needs_labels:
            summary['by_label'][label] += 1

        if len(summary['samples']) < 25:
            summary['samples'].append({
                'status': 'pending' if not args.apply else 'applied',
                'subject': msg.get('subject'),
                'from': (msg.get('from') or {}).get('addr'),
                'source_folder': source_folder,
                'target_folder': target_folder,
                'labels_to_add': needs_labels,
            })

        if not args.apply:
            processed_actions += 1
            continue

        try:
            for label in needs_labels:
                copy_label(source_folder, label, msg['id'])
                summary['labeled'] += 1
            if needs_move:
                move_message(source_folder, target_folder, msg['id'])
                summary['moved'] += 1
            processed_actions += 1
        except Exception as e:
            summary['errors'] += 1
            summary['failures'].append({
                'subject': msg.get('subject'),
                'from': (msg.get('from') or {}).get('addr'),
                'source_folder': source_folder,
                'target_folder': target_folder,
                'error': str(e),
            })

    db.close()
    summary['by_target_folder'] = dict(summary['by_target_folder'])
    summary['by_label'] = dict(summary['by_label'])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
