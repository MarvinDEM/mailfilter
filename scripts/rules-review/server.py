#!/usr/bin/env python3
"""Rules review API — reads/writes learned_rules (mail triage).

Auth: shared-auth (stejný systém jako CDprocesy, t 2026-08-23) —
bcrypt hesla v shared-auth DB + JWT HS256 Bearer token, app_key 'mailfilter'.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bcrypt
import jwt

sys.path.insert(0, '/app/bin')
import mail_rules  # noqa: E402

SITE_DIR = Path('/app/site')
PORT = int(os.environ.get('PORT', '8000'))

AUTH_DB = os.environ.get('SHARED_AUTH_DB_PATH', '/app/shared-auth/data/shared-auth.db')
JWT_SECRET = os.environ.get('SHARED_AUTH_JWT_SECRET', '')
APP_KEY = 'mailfilter'
JWT_TTL_H = 24

FOLDERS_FILE = '/data/mailfilter-folders.json'
RUN_REQ_DIR = '/data/mailfilter-run-requests'
RUN_RES_DIR = '/data/mailfilter-run-results'
FOLDER_REFRESH_DIR = '/data/mailfilter-folder-refresh'
INBOX_REQ_DIR = '/data/mailfilter-inbox-requests'
INBOX_RES_DIR = '/data/mailfilter-inbox-results'
# Fallback, když dump chybí: složky používané v pravidlech + původní statický seznam.
FALLBACK_FOLDERS = [
    'Folders/10_osobni/11_tomas', 'Folders/10_osobni/31_zvole',
    'Folders/50_pracovni/51_bezouska', 'Folders/50_pracovni/52_inadvisors',
    'Folders/50_pracovni/53_ipsd', 'Folders/50_pracovni/54_mmr',
    'Folders/50_pracovni/70_prazske-noviny', 'Folders/50_pracovni/80_delta',
    'Folders/90_ostatni/91_newsletter', 'Folders/90_ostatni/92_transakce',
    'Folders/90_ostatni/94_notifikace', 'Folders/90_ostatni/95_registrace',
    'Folders/90_ostatni/96_spammers_fun', 'Folders/99_nezatrideno',
]


def _list_folders():
    # Jen Folders/* — Labels/* patří do pole „Labely“, ne do „Složka“ (t, 2026-08-23).
    try:
        data = json.load(open(FOLDERS_FILE, encoding='utf-8'))
        folders = [f for f in (data.get('folders') or []) if f.startswith('Folders/')]
        if folders:
            return folders
    except Exception:
        pass
    conn = mail_rules.db_connect()
    try:
        used = sorted({r['folder'] for r in mail_rules.list_rules(conn)
                       if r['folder'] and r['folder'].startswith('Folders/')})
    finally:
        conn.close()
    return sorted(set(FALLBACK_FOLDERS) | set(used))


def _auth_conn():
    conn = sqlite3.connect(AUTH_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _auth_user_by_identifier(identifier):
    conn = _auth_conn()
    try:
        return conn.execute(
            '''SELECT u.id, u.global_user_id, u.email, u.email_normalized, u.display_name,
                      u.password_hash, u.status, u.is_platform_admin,
                      m.role_key AS role, m.status AS membership_status
               FROM identity_users u
               JOIN identity_app_memberships m ON m.user_id = u.id AND m.app_key = ?
               WHERE u.email_normalized = ?''',
            (APP_KEY, str(identifier).strip().lower())).fetchone()
    finally:
        conn.close()


def _auth_user_by_id(uid):
    conn = _auth_conn()
    try:
        return conn.execute(
            '''SELECT u.id, u.global_user_id, u.email, u.email_normalized, u.display_name,
                      u.password_hash, u.status, u.is_platform_admin,
                      m.role_key AS role, m.status AS membership_status
               FROM identity_users u
               JOIN identity_app_memberships m ON m.user_id = u.id AND m.app_key = ?
               WHERE u.id = ?''',
            (APP_KEY, uid)).fetchone()
    finally:
        conn.close()


def _issue_token(row):
    now = datetime.now(timezone.utc)
    payload = {
        'uid': row['id'],
        'sub': row['email_normalized'],
        'app': APP_KEY,
        'role': row['role'],
        'iat': int(now.timestamp()),
        'exp': int((now + timedelta(hours=JWT_TTL_H)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')


def _user_payload(row):
    return {
        'id': row['id'],
        'email': row['email'],
        'display_name': row['display_name'] or row['email'],
        'role': row['role'],
        'app_key': APP_KEY,
        'membership_status': row['membership_status'],
        'is_platform_admin': bool(row['is_platform_admin']),
    }


def _auth_required(handler):
    """Ověří Bearer JWT; při neúspěchu pošle 401 a vrátí None."""
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        handler._send(401, json.dumps({'detail': 'Chybí autorizační token'}).encode('utf-8'))
        return None
    try:
        payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
    except Exception:
        handler._send(401, json.dumps({'detail': 'Neplatný nebo expirovaný token'}).encode('utf-8'))
        return None
    if payload.get('app') != APP_KEY or not isinstance(payload.get('uid'), int):
        handler._send(401, json.dumps({'detail': 'Neplatný token pro tuto aplikaci'}).encode('utf-8'))
        return None
    row = _auth_user_by_id(payload['uid'])
    if not row or row['status'] != 'active' or row['membership_status'] != 'active':
        handler._send(401, json.dumps({'detail': 'Účet není aktivní'}).encode('utf-8'))
        return None
    return payload


MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/auth/config':
            self._send(200, json.dumps({
                'loginMode': 'email_password',
                'identifierLabel': 'E-mail',
                'localLoginEnabled': True,
                'oidcEnabled': False,
                'inviteOnly': False,
                'passwordResetEnabled': False,
                'appKey': APP_KEY,
                'sharedAuthService': True,
            }).encode('utf-8'))
            return
        if path == '/api/auth/me':
            payload = _auth_required(self)
            if not payload:
                return
            row = _auth_user_by_id(payload['uid'])
            self._send(200, json.dumps({'user': _user_payload(row) if row else None}, ensure_ascii=False).encode('utf-8'))
            return
        if path == '/api/folders':
            if not _auth_required(self):
                return
            self._send(200, json.dumps({'folders': _list_folders()}, ensure_ascii=False).encode('utf-8'))
            return
        if path == '/api/rules':
            if not _auth_required(self):
                return
            conn = mail_rules.db_connect()
            rules = mail_rules.list_rules(conn)
            conn.close()
            # poslední manuální běh pro každé pravidlo (t, 2026-08-23)
            last_runs = {}
            try:
                for f in os.listdir(RUN_RES_DIR):
                    m = re.match(r'^run-(\d+)-.*\.json$', f)
                    if not m:
                        continue
                    rid = int(m.group(1))
                    p = os.path.join(RUN_RES_DIR, f)
                    try:
                        data = json.load(open(p, encoding='utf-8'))
                        data['_mtime'] = os.path.getmtime(p)
                    except Exception:
                        continue
                    if rid not in last_runs or data['_mtime'] > last_runs[rid]['_mtime']:
                        last_runs[rid] = data
            except Exception:
                pass
            for r in rules:
                lr = last_runs.get(r['id'])
                if lr:
                    r['last_run'] = {
                        'status': lr.get('status'),
                        'matched': lr.get('matched', 0),
                        'moved': lr.get('moved', 0),
                        'labels_applied': lr.get('labels_applied', 0),
                        'forwarded': lr.get('forwarded', 0),
                        'finished_at': lr.get('finished_at'),
                        'errors': (lr.get('errors') or [])[:3],
                    }
                else:
                    r['last_run'] = None
            # právě běžící pravidlo (processing requesty) — frontend blokuje další běhy (t, 2026-08-24)
            running = None
            try:
                for f in os.listdir(RUN_REQ_DIR):
                    if f.endswith('.processing'):
                        m = re.match(r'^run-(\d+)-', f)
                        if m:
                            running = int(m.group(1))
                            break
            except Exception:
                pass
            self._send(200, json.dumps({'rules': rules, 'running': running}, ensure_ascii=False).encode('utf-8'))
            return
        if path == '/api/inbox/process':
            # Stav kompletního zpracování INBOX (t, 2026-09-01)
            if not _auth_required(self):
                return
            running = False
            try:
                for f in os.listdir(INBOX_REQ_DIR):
                    if f.endswith('.processing'):
                        running = True
                        break
            except Exception:
                pass
            last = None
            try:
                files = [f for f in os.listdir(INBOX_RES_DIR) if f.startswith('inbox-') and f.endswith('.json')]
                if files:
                    newest = max(files, key=lambda f: os.path.getmtime(os.path.join(INBOX_RES_DIR, f)))
                    data = json.load(open(os.path.join(INBOX_RES_DIR, newest), encoding='utf-8'))
                    data['_file'] = newest
                    data['_mtime'] = os.path.getmtime(os.path.join(INBOX_RES_DIR, newest))
                    last = data
            except Exception:
                pass
            self._send(200, json.dumps({'running': running, 'last': last}, ensure_ascii=False).encode('utf-8'))
            return
        if path == '/api/stats':
            if not _auth_required(self):
                return
            conn = mail_rules.db_connect()
            rules = mail_rules.list_rules(conn)
            conn.close()
            stats = {'total': len(rules), 'pending': 0, 'ok': 0, 'discard': 0}
            for r in rules:
                stats[r['review_status']] = stats.get(r['review_status'], 0) + 1
            self._send(200, json.dumps(stats).encode('utf-8'))
            return
        if path in ('/', ''):
            path = '/index.html'
        f = (SITE_DIR / path.lstrip('/')).resolve()
        if f.is_file() and str(f).startswith(str(SITE_DIR.resolve())):
            body = f.read_bytes()
            self._send(200, body, MIME.get(f.suffix, 'application/octet-stream'))
        else:
            self._send(404, b'not found', 'text/plain')

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/api/folders/refresh':
            # Manuální obnova seznamu složek (t, 2026-08-23): marker zpracuje host cron.
            if not _auth_required(self):
                return
            try:
                from datetime import datetime, timezone as tz
                os.makedirs(FOLDER_REFRESH_DIR, exist_ok=True)
                ts = datetime.now(tz.utc).strftime('%Y%m%dT%H%M%S%f')
                with open(os.path.join(FOLDER_REFRESH_DIR, f'refresh-{ts}.json'), 'w', encoding='utf-8') as f:
                    json.dump({'requested_at': datetime.now(tz.utc).isoformat()}, f)
                self._send(202, json.dumps({'ok': True, 'queued': True}).encode('utf-8'))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}).encode('utf-8'))
            return
        if path == '/api/inbox/process':
            # Manuální spuštění kompletního zpracování INBOX podle všech pravidel (t, 2026-09-01)
            # Marker zpracuje host cron (mailfilter-run-requests) → triage_bezouska_mail.py
            if not _auth_required(self):
                return
            try:
                # blokace: dokud běží jiné pravidlo nebo inbox processing, nový běh nepovolit
                try:
                    for f in os.listdir(RUN_REQ_DIR):
                        if f.endswith('.processing'):
                            self._send(409, json.dumps({'error': 'jiné pravidlo právě běží — počkej na dokončení'}).encode('utf-8'))
                            return
                    for f in os.listdir(INBOX_REQ_DIR):
                        if f.endswith('.processing'):
                            self._send(409, json.dumps({'error': 'zpracování INBOX už běží — počkej na dokončení'}).encode('utf-8'))
                            return
                except Exception:
                    pass
                os.makedirs(INBOX_REQ_DIR, exist_ok=True)
                from datetime import datetime as _dt, timezone as _tz
                ts = _dt.now(_tz.utc).isoformat()
                req_id = ts.replace(':', '').replace('+', '').replace('.', '')
                with open(os.path.join(INBOX_REQ_DIR, f'inbox-{req_id}.json'), 'w', encoding='utf-8') as f:
                    json.dump({'requested_at': ts}, f)
                self._send(202, json.dumps({'ok': True, 'queued': True, 'requested_at': ts}).encode('utf-8'))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}).encode('utf-8'))
            return
        if path == '/api/auth/login':
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                self._send(400, json.dumps({'detail': 'Chybný JSON'}).encode('utf-8'))
                return
            identifier = data.get('email') or data.get('username') or ''
            password = data.get('password') or ''
            if not identifier or not password:
                self._send(400, json.dumps({'detail': 'Chybí e-mail nebo heslo'}).encode('utf-8'))
                return
            row = _auth_user_by_identifier(identifier)
            if not row or not row['password_hash']:
                self._send(401, json.dumps({'detail': 'Neplatné přihlašovací údaje'}).encode('utf-8'))
                return
            if row['status'] != 'active' or row['membership_status'] != 'active':
                self._send(401, json.dumps({'detail': 'Účet není aktivní'}).encode('utf-8'))
                return
            if not bcrypt.checkpw(password.encode('utf-8'), row['password_hash'].encode('utf-8')):
                conn = _auth_conn()
                try:
                    conn.execute("UPDATE identity_users SET failed_login_count = failed_login_count + 1, updated_at = datetime('now') WHERE id = ?", (row['id'],))
                    conn.commit()
                finally:
                    conn.close()
                self._send(401, json.dumps({'detail': 'Neplatné přihlašovací údaje'}).encode('utf-8'))
                return
            conn = _auth_conn()
            try:
                conn.execute("UPDATE identity_users SET last_login_at = datetime('now'), failed_login_count = 0, updated_at = datetime('now') WHERE id = ?", (row['id'],))
                conn.commit()
            finally:
                conn.close()
            self._send(200, json.dumps({'token': _issue_token(row), 'user': _user_payload(row)}, ensure_ascii=False).encode('utf-8'))
            return
        m = re.match(r'^/api/rules/(\d+)/review$', path)
        if m:
            if not _auth_required(self):
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length) or b'{}')
                status = data.get('status')
                if status not in ('ok', 'discard', 'restore'):
                    self._send(400, json.dumps({'error': 'status must be ok|discard|restore'}).encode('utf-8'))
                    return
                conn = mail_rules.db_connect()
                changed = mail_rules.set_review(conn, int(m.group(1)), status)
                conn.close()
                if not changed:
                    self._send(404, json.dumps({'error': 'rule not found'}).encode('utf-8'))
                    return
                self._send(200, json.dumps({'ok': True}).encode('utf-8'))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}).encode('utf-8'))
            return
        m = re.match(r'^/api/rules/(\d+)/run$', path)
        if m:
            # Manuální spuštění pravidla (t, 2026-08-23): zapíšeme request do fronty,
            # host cron (mailfilter-run-requests) ho zpracuje přes himalaya.
            if not _auth_required(self):
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length) if length else b'{}'
                data = json.loads(body or b'{}')
                conn = mail_rules.db_connect()
                rule = mail_rules.get_rule(conn, int(m.group(1)))
                conn.close()
                if not rule:
                    self._send(404, json.dumps({'error': 'rule not found'}).encode('utf-8'))
                    return
                if not rule.get('active') or rule.get('review_status') == 'discard':
                    self._send(400, json.dumps({'error': 'pravidlo není aktivní (zahozené)'}).encode('utf-8'))
                    return
                scope = data.get('scope') or 'inbox'  # inbox | all | konkrétní složka
                # blokace: dokud běží jiné pravidlo, nový běh nepovolit (t, 2026-08-24)
                try:
                    for f in os.listdir(RUN_REQ_DIR):
                        if f.endswith('.processing'):
                            self._send(409, json.dumps({'error': 'jiné pravidlo právě běží — počkej na dokončení'}).encode('utf-8'))
                            return
                except Exception:
                    pass
                from datetime import datetime, timezone as tz
                ts = datetime.now(tz.utc).isoformat()
                req_id = ts.replace(':', '').replace('+', '').replace('.', '')
                os.makedirs(RUN_REQ_DIR, exist_ok=True)
                with open(os.path.join(RUN_REQ_DIR, f'run-{rule["id"]}-{req_id}.json'), 'w', encoding='utf-8') as f:
                    json.dump({'rule_id': rule['id'], 'requested_at': ts, 'scope': scope}, f)
                # requested_at vraci frontendu pro spolehlivou detekci dokonceni (t, 2026-08-24)
                self._send(202, json.dumps({'ok': True, 'queued': True, 'rule_id': rule['id'],
                                             'scope': scope, 'requested_at': ts}).encode('utf-8'))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}).encode('utf-8'))
            return
        if path == '/api/rules':
            # POST /api/rules — nový filtr (t, 2026-08-23)
            if not _auth_required(self):
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length) or b'{}')
                conn = mail_rules.db_connect()
                new_id = mail_rules.create_rule(conn, data)
                conn.close()
                self._send(201, json.dumps({'ok': True, 'id': new_id}).encode('utf-8'))
            except ValueError as e:
                self._send(400, json.dumps({'error': str(e)}).encode('utf-8'))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}).encode('utf-8'))
            return
        self._send(404, json.dumps({'error': 'not found'}).encode('utf-8'))

    def do_PUT(self):
        # PUT /api/rules/<id>  {sender?, recipient?, subject_pattern?, folder?, labels?}
        path = self.path.split('?')[0]
        m = re.match(r'^/api/rules/(\d+)$', path)
        if not m:
            self._send(404, json.dumps({'error': 'not found'}).encode('utf-8'))
            return
        if not _auth_required(self):
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length) or b'{}')
            conn = mail_rules.db_connect()
            changed = mail_rules.update_rule(conn, int(m.group(1)), data)
            conn.close()
            if not changed:
                self._send(404, json.dumps({'error': 'rule not found'}).encode('utf-8'))
                return
            self._send(200, json.dumps({'ok': True}).encode('utf-8'))
        except ValueError as e:
            self._send(400, json.dumps({'error': str(e)}).encode('utf-8'))
        except Exception as e:
            self._send(500, json.dumps({'error': str(e)}).encode('utf-8'))

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    print(f'rules-review server on :{PORT}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
