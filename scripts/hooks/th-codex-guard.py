#!/usr/bin/env python3
"""Fail-open Codex shell polling and denied-command retry guard."""
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time


def segments(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()\n')
    lexer.whitespace_split = True
    result = [[]]
    for token in lexer:
        if token and all(c in ';&|()\n' for c in token):
            result.append([])
        else:
            result[-1].append(token)
    return [x for x in result if x]


def sleep_block(command, limit):
    for args in segments(command):
        for i, word in enumerate(args[:-1]):
            if os.path.basename(word) in ('bash', 'sh', 'zsh') and args[i + 1] == '-c' and i + 2 < len(args):
                if sleep_block(args[i + 2], limit):
                    return True
            name = os.path.basename(word)
            value = args[i + 1]
            if name == 'watch' and value in ('-n', '--interval') and i + 2 < len(args):
                value = args[i + 2]
            elif name == 'watch' and value.startswith('--interval='):
                value = value.split('=', 1)[1]
            elif name not in ('sleep',):
                continue
            match = re.fullmatch(r'(\d+(?:\.\d+)?)([smhd]?)', value)
            if match and float(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]] >= limit:
                return True
    return False


def targets(command):
    checks = []
    retry = []
    for args in segments(command):
        while args and ('=' in args[0] and re.match(r'^[A-Za-z_][A-Za-z_0-9]*=', args[0])):
            args = args[1:]
        if not args:
            continue
        name = os.path.basename(args[0])
        paths = [x for x in args[1:] if '/' in x and not x.startswith('-')]
        retry.append([name, *paths])
        if name in ('ths', 'th-status', 'th-status.sh', 'th-inbox-peek', 'th-inbox-peek.sh', 'pgrep', 'ps'):
            checks.append([name, *[x for x in args[1:] if not x.startswith('-')]])
        if name in ('cat', 'tail'):
            checks.extend([['file', x] for x in args[1:] if os.path.basename(x) in ('STATUS', 'WORKLOG.md', 'job.pid')])
    return checks, retry


def main():
    if os.environ.get('TH_CODEX_GUARD') == 'off':
        return
    payload = json.load(sys.stdin)
    event = payload.get('hook_event_name')
    if event not in ('PreToolUse', 'PostToolUse', 'UserPromptSubmit'):
        return
    session = payload.get('session_id') or os.environ.get('ORCA_PANE_KEY')
    data = payload.get('tool_input', {})
    if not isinstance(data, dict):
        return
    command = data.get('command', data.get('cmd'))
    if event != 'UserPromptSubmit' and not isinstance(command, str):
        return
    command = command if isinstance(command, str) else ''
    worker = os.environ.get('TH_ROLE') == 'worker' or 'TH_JOB_TASK' in os.environ
    checks, retry = targets(command)
    watch = Path(os.environ.get('TH_WATCH_DIR', str(Path.home() / '.team-harness' / 'watch')))
    folder = watch / 'codex_guard'
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rule = None
    with (folder / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / 'state.json'
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {'checks': [], 'denied': []}
        state['checks'] = [x for x in state['checks'] if now - x[0] < 600]
        state['denied'] = [x for x in state['denied'] if now - x[0] < 1800]
        if event == 'UserPromptSubmit':
            if session:
                state['checks'] = [x for x in state['checks'] if x[2] != session]
                state['denied'] = [x for x in state['denied'] if len(x) < 3 or x[2] != session]
        elif event == 'PostToolUse':
            output = json.dumps(payload.get('tool_response', ''), ensure_ascii=False)
            if re.search(r'permission denied|rejected|not permitted|approval.{0,40}(?:denied|reject|거부)|승인.{0,20}거부', output, re.I):
                state['denied'].extend([[now, x, session] for x in retry])
        else:
            if sleep_block(command, 60 if worker else 10):
                rule = 'sleep'
            elif any(item == x[1] and (len(x) < 3 or x[2] == session) for item in retry for x in state['denied']):
                rule = '권한 거부 재시도'
            elif not worker and session:
                for item in checks:
                    if sum(item == x[1] and session == x[2] for x in state['checks']) >= 2:
                        rule = '반복 상태 확인'
                        break
                    state['checks'].append([now, item, session])
        path.write_text(json.dumps(state, ensure_ascii=False))
    if rule:
        try:
            with (watch / 'guard.log').open('a') as log:
                log.write(json.dumps({'agent': 'codex', 'rule': rule, 'command': command}, ensure_ascii=False) + '\n')
        except OSError:
            pass
        reason = f'[th-codex-guard] {rule}: 폴링·재시도 금지. 지금 상태를 사용자에게 보고하고 턴을 끝낼 것. 완료는 watchd 알림으로 사용자에게 간다.'
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': reason}}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
