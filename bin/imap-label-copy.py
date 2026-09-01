#!/usr/bin/env python3
"""IMAP label copy helper — sekvenční COPY místo UID COPY (bridge bug, 2026-08-24).

Proton Bridge (3.25.0/3.26.0 po rebuildu mailboxu) vrací OK na `UID COPY`,
ale zprávu NEZKOPÍRUJE (rozbitý UIDVALIDITY mapping). Sekvenční `COPY` funguje.

Použití: imap-label-copy.py <src_folder> <label_folder> <uid>...
  - src_folder: např. Folders/90_ostatni/92_transakce nebo INBOX
  - label_folder: např. Labels/vyresit
  - uid: IMAP UID (odpovídá id z `himalaya envelope list`)

Exit 0 = všechny kopie OK; jinak 1.
"""
import imaplib
import sys

HOST = '127.0.0.1'
PORT = 1143
ACCOUNT = 'tomas@bezouska.cz'
PASS_FILE = '/root/.config/himalaya/tomas-bezouska-bridge.pass'


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    src = sys.argv[1]
    label = sys.argv[2]
    uids = sys.argv[3:]

    pw = open(PASS_FILE).read().strip()
    m = imaplib.IMAP4(HOST, PORT, timeout=30)
    m.login(ACCOUNT, pw)

    try:
        typ, _ = m.select(src)
        if typ != 'OK':
            print(f'SELECT {src} selhal: {typ}', file=sys.stderr)
            return 1

        failures = []
        for uid in uids:
            # UID -> sekvenční číslo (odpověď: b'<seq> (UID <uid> ...)')
            typ, data = m.uid('fetch', uid, '(UID)')
            if typ != 'OK' or not data or data[0] is None:
                failures.append((uid, 'fetch fail'))
                continue
            # data[0] může být tuple (část, payload) nebo jen payload; první část může být int
            part = data[0]
            if isinstance(part, tuple):
                head = part[0]
            else:
                head = part
            if isinstance(head, bytes):
                seq = head.split(b' ', 1)[0].decode()
            else:
                # int = počet bytů literálu — sekvenci vezmi z payloadu
                raw = b' '.join(x for x in part if isinstance(x, bytes))
                seq = raw.split(b' ', 1)[0].decode()
            typ, resp = m.copy(seq, label)  # sekvenční COPY (UID COPY je v bridge rozbitý)
            if typ != 'OK':
                failures.append((uid, f'copy fail: {resp}'))
            else:
                print(f'label {uid} -> {label} OK (seq {seq})')
        m.logout()
        if failures:
            for uid, err in failures:
                print(f'FAIL {uid}: {err}', file=sys.stderr)
            return 1
        return 0
    except Exception as e:
        print(f'ERR: {e}', file=sys.stderr)
        try:
            m.logout()
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
