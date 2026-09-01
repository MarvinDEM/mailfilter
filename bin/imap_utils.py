#!/usr/bin/env python3
"""Sdílené IMAP utility pro bezouska mailfilter (t, 2026-08-24).

Proton Bridge má buggy IMAP parser:
- mailbox jména s mezerou NESMÍ jít jako holý atom (imaplib je tak posílá)
- non-ASCII jména musí být v modified-UTF7 (např. „označil marvin" → ozna&AQ0-il marvin)

SELECT s řádně quoted jménem funguje (testováno live).
"""

import base64


def _utf7_b64_decode(part: str) -> str:
    s = part.replace(',', '/') + '=' * (-len(part) % 4)
    return base64.b64decode(s.encode()).decode('utf-16-be')


def imap_utf7_decode(s: str) -> str:
    """Decode IMAP modified UTF-7 → Unicode (pro jména z LIST odpovědi)."""
    res = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '&':
            j = s.find('-', i + 1)
            if j == -1:
                res.append('&')
                break
            inner = s[i + 1:j]
            if inner == '':
                res.append('&')
            else:
                res.append(_utf7_b64_decode(inner))
            i = j + 1
        else:
            res.append(ch)
            i += 1
    return ''.join(res)


def imap_utf7_encode(s: str) -> str:
    """Encode string to IMAP modified UTF-7."""
    res = []
    buf = []

    def flush():
        if buf:
            b = ''.join(buf).encode('utf-16-be')
            enc = base64.b64encode(b).decode().rstrip('=').replace('/', ',')
            res.append('&' + enc + '-')
            buf.clear()

    for ch in s:
        if ord(ch) < 128:
            if ch == '&':
                flush()
                res.append('&-')
            else:
                flush()
                res.append(ch)
        else:
            buf.append(ch)
    flush()
    return ''.join(res)


def quote_mailbox(name: str) -> str:
    """Vrátí mailbox jméno připravené do IMAP příkazu (UTF-7 + quoting)."""
    enc = imap_utf7_encode(name)
    # quote, pokud obsahuje speciální znaky (mezera, (){%*"\\] atd.)
    if any(c in enc for c in ' (){%*"\\]'):
        return '"' + enc.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return enc


def select_mailbox(m, name):
    """SELECT s korektním quotingem; vrátí (typ, data) a nastaví state."""
    typ, data = m._simple_command('SELECT', quote_mailbox(name))
    if typ == 'OK':
        m.state = 'SELECTED'
    return typ, data
