#!/usr/bin/env python3
"""Fail-open PreToolUse guard for destructive coordinator shell commands."""
import json
import os
import re
import shlex
import sys
from pathlib import Path


def without_heredocs(command):
    lines = command.splitlines()
    result = []
    pending = []
    for line in lines:
        if pending:
            delimiter, strip_tabs = pending[0]
            if (line.lstrip('\t') if strip_tabs else line) == delimiter:
                pending.pop(0)
            continue
        result.append(line)
        # Recognize shell heredoc operators outside quotes and comments.
        lexer = shlex.shlex(line, posix=False, punctuation_chars='<>')
        lexer.whitespace_split = True
        tokens = list(lexer)
        for i, token in enumerate(tokens[:-1]):
            if token in ('<<', '<<-'):
                delimiter = tokens[i + 1]
                strip_tabs = delimiter.startswith('-')
                if strip_tabs:
                    delimiter = delimiter[1:]
                pending.append((delimiter.strip("'\""), strip_tabs))
    return '\n'.join(result)


def find_pattern(command):
    lexer = shlex.shlex(without_heredocs(command), posix=False, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    tokens = list(lexer)
    segments = [[]]
    for token in tokens:
        if token and all(c in ';&|()\n' for c in token):
            segments.append([])
        else:
            segments[-1].append(token)
    protected = ('/outputs/', '/_outputs/', '/dataset/', '/다운로드/생성물/')
    for raw_args in segments:
        args = [x[1:-1] if len(x) >= 2 and x[0] == x[-1] and x[0] in ('\"', "'") else x for x in raw_args]
        if not args:
            continue
        # Assignments preceding a command do not change its position.
        while args and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', args[0]):
            args = args[1:]
            raw_args = raw_args[1:]
        if not args:
            continue
        name = os.path.basename(args[0])
        rest = args[1:]
        if name in ('kill', 'pkill', 'killall') and not (name == 'kill' and '-0' in rest):
            return name
        if name == 'systemctl' and any(x in ('stop', 'restart') for x in rest):
            return 'systemctl stop/restart'
        if name == 'tmux' and any(x.startswith('kill-') for x in rest):
            return 'tmux kill-*'
        if name == 'git' and any(rest[i:i+2] == pair for i in range(len(rest)) for pair in (["reset", "--hard"], ["clean", "-f"], ["worktree", "remove"], ["branch", "-D"])):
            return 'git ' + ' '.join(rest)
        if name == 'git' and 'clean' in rest and any(x.startswith('-') and not x.startswith('--') and 'f' in x[1:] for x in rest):
            return 'git clean -f'
        has_path = any(any(p in ('/' + x.lstrip('/')) for p in protected) for x in rest)
        if has_path and (name in ('rm', 'mv') or (name == 'rsync' and any(x == '--delete' or x.startswith('--delete=') or x.startswith('--delete-') for x in rest)) or (name == 'find' and '-delete' in rest)):
            return name + ' protected path'
        if name in ('rm', 'tee') and any(os.path.basename(x) == 'job.pid' for x in rest):
            return name + ' job.pid'
        if name == 'touch' and any(os.path.basename(x) in ('FAILED', 'STOPPED', 'STOPPED_BY_USER') for x in rest):
            return 'touch status file'
        for i, token in enumerate(args[:-1]):
            if raw_args[i] in ('>', '>>') and os.path.basename(args[i+1]) in ('job.pid', 'FAILED', 'STOPPED', 'STOPPED_BY_USER'):
                return token + ' ' + os.path.basename(args[i+1])
    return None


def main():
    if os.getenv('TH_GUARD') == 'off' or os.getenv('TH_ROLE') == 'worker' or 'TH_JOB_TASK' in os.environ:
        return
    try:
        payload = json.load(sys.stdin)
        pattern = find_pattern(payload['tool_input']['command'])
        if not pattern:
            return
        try:
            watch = Path(os.getenv('TH_WATCH_DIR', str(Path.home() / '.team-harness' / 'watch')))
            watch.mkdir(parents=True, exist_ok=True)
            with (watch / 'guard.log').open('a', encoding='utf-8') as log:
                log.write(json.dumps({'pattern': pattern, 'command': payload['tool_input']['command']}, ensure_ascii=False) + '\n')
        except OSError:
            pass
        deny = os.getenv('TH_GUARD_MODE') == 'deny'
        reason = '[가드] 실행 중 작업 중단·결과 삭제/이동은 사용자 승인 필요: ' + pattern
        if deny:
            reason += ' — 사용자에게 이유를 말하고 승인받을 것'
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny' if deny else 'ask', 'permissionDecisionReason': reason}}, ensure_ascii=False))
    except Exception:
        return


if __name__ == '__main__':
    main()
