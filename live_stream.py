"""Временный текст текущего ответа agy; итог остаётся в обычной ленте хаба."""
import hashlib
import os
import time

import store

LIMIT = 30000
TTL = 120


def path(session):
    key = hashlib.sha256(session.encode('utf-8')).hexdigest()
    return os.path.join(store.data_dir(), 'stream', key + '.json')


def write(session, text):
    if not session:
        return
    target = path(session)
    if not text:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    store.write_json(target, {'session': session, 'text': text[-LIMIT:], 'at': time.time()})


def read(session, now=None):
    if not isinstance(session, str) or not session or len(session) > 200:
        return ''
    row = store.read_json(path(session))
    if not isinstance(row, dict) or row.get('session') != session or not isinstance(row.get('at'), (int, float)):
        return ''
    age = (time.time() if now is None else now) - row['at']
    return row.get('text', '') if 0 <= age < TTL and isinstance(row.get('text'), str) else ''
