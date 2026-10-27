#!/usr/bin/env python3
"""Read complete inbox lines under the writer's lock, optionally acknowledging them."""
import fcntl
import os
import sys
from pathlib import Path

def th_session_key():
    """Return the inherited coordinator session identity in precedence order."""
    return (os.environ.get('ORCA_PANE_KEY') or os.environ.get('CLAUDE_CODE_SESSION_ID')
            or os.environ.get('TH_SESSION_KEY') or '')

owner, ack, mode = sys.argv[1:]
session = th_session_key()
root = Path(owner) / '.claude'
inbox = root / 'inbox.md'
if not inbox.exists():
    sys.exit(1)
# A session key is a single filename, never a path.
if session in ('.', '..', '*') or '/' in session or '\n' in session or '\t' in session:
    print('Invalid inbox session key', file=sys.stderr)
    sys.exit(2)
cursor = root / '.inbox_cursor.d' / session if session else root / '.inbox_cursor'
lock = root / '.inbox_cursor.lock'
with lock.open('a') as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    data = inbox.read_bytes()
    try:
        start = int(cursor.read_text().strip())
    except (OSError, ValueError):
        if mode == 'wait':
            if ack == '1':
                cursor.parent.mkdir(parents=True, exist_ok=True)
                cursor.write_text(f'{len(data)}\n')
            sys.exit(1)
        start = 0
    if start < 0 or start > len(data):
        start = 0
    routes = {}
    try:
        route_lines = (root / 'inbox.routes').read_text().splitlines()
    except OSError:
        route_lines = []
    for line in route_lines:
        try:
            offset, route = line.split('\t', 1)
            routes[int(offset)] = route
        except ValueError:
            continue
    pos = start
    result = []
    for part in data[start:].split(b'\n')[:-1]:
        line = part + b'\n'
        route = routes.get(pos, '*')
        if route == '*' or (session and route == session):
            result.append(line)
        pos += len(line)
    if result:
        if mode == 'wait':
            header = f'[th-inbox-wait] 새 사건 {len(result)}건 — 처리 후 이 명령을 다시 백그라운드로 실행할 것\n'
            sys.stdout.buffer.write(header.encode())
        sys.stdout.buffer.write(b''.join(result))
        sys.stdout.buffer.flush()
    if ack == '1' and pos != start:
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text(f'{pos}\n')
    sys.exit(0 if result else 1)
