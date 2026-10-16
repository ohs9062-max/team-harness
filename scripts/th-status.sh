#!/usr/bin/env bash
# th-status: 요청받았을 때만 등록 작업을 한 번 요약 표로 출력한다.
set -u

script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

usage() {
  cat <<'EOF'
사용법: th-status.sh [-a|--all] [-w|--watch [초]] [-h|--help]
  기본: 실행 중 및 최근 24시간 실패·검수실패만 짧게 표시
  -a, --all       전체 상태 표 표시
  -w, --watch [초] 기본 짧은 출력을 반복 갱신 (기본 10초)
EOF
}

all_mode=0
watch_seconds=""
while (($#)); do
  case $1 in
    -a|--all) all_mode=1 ;;
    -w|--watch)
      watch_seconds=10
      if [[ ${2:-} =~ ^[1-9][0-9]*$ ]]; then watch_seconds=$2; shift; fi
      ;;
    -h|--help) usage; exit 0 ;;
    *) : ;; # Preserve the former behavior: unrecognized arguments were ignored.
  esac
  shift
done

render() {
  python3 - "$script_dir/../src/team_harness/worker_digest.py" "$all_mode" "${1:-0}" << 'EOF'
import sys, os, time, subprocess, re, unicodedata
from datetime import datetime
from pathlib import Path
import importlib.util

spec = importlib.util.spec_from_file_location("worker_digest", sys.argv[1])
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)

all_mode = len(sys.argv) > 2 and sys.argv[2] == "1"
watch_mode = len(sys.argv) > 3 and sys.argv[3] == "1"
NOTICE = "[ths] 사용자 확인용 화면 — 에이전트는 이 표를 다시 출력·요약하지 말 것(이상 있을 때만 한 줄)."

def print_notice():
    if not watch_mode:
        print(NOTICE)

def char_width(c):
    return 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1

def str_width(s):
    return sum(char_width(c) for c in s)

def fit_width(s, width):
    cur_w = 0
    res = []
    for c in s:
        cw = char_width(c)
        if cur_w + cw > width:
            break
        res.append(c)
        cur_w += cw
    truncated = "".join(res)
    padding = " " * (width - cur_w)
    return truncated + padding

def get_latest_output(task):
    try:
        res = subprocess.run(
            ["find", task, "-type", "f", "!", "-name", ".th_*", "-printf", "%T@\n"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5
        )
        lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
        if not lines:
            return None
        times = []
        for l in lines:
            try:
                times.append(float(l.split(".")[0]))
            except ValueError:
                pass
        return max(times) if times else None
    except Exception:
        return None

def get_task_title(task_dir):
    task_md = os.path.join(task_dir, "task.md")
    if not os.path.isfile(task_md):
        return "-"
    try:
        with open(task_md, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("# "):
                    title = line[2:].strip()
                    if not title:
                        return "-"
                    if len(title) > 40:
                        title = title[:40]
                    return title
    except Exception:
        pass
    return "-"

def get_progress(task_dir):
    status_file = os.path.join(task_dir, "STATUS")
    if not os.path.isfile(status_file):
        return "-"
    last_line = ""
    try:
        with open(status_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                l = line.strip()
                if l:
                    last_line = l
    except Exception:
        pass
    if not last_line:
        return "-"
    
    # 1. 날짜·시각 제거
    s = re.sub(r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z| KST)?\]?[:\s]*", "", last_line).strip()
    s = re.sub(r"^\[?\d{4}-\d{2}-\d{2}\]?[:\s]*", "", s).strip()
    
    # 2. RUN 명령 경로 정제: "RUN .../src/gdino_finetune.py" -> "실행: gdino_finetune.py"
    if re.match(r"^RUN\b", s):
        rest = re.sub(r"^RUN\s+", "", s).strip()
        tokens = rest.split()
        target = ""
        for tok in tokens:
            if tok.startswith("-") or "=" in tok:
                continue
            bname = os.path.basename(tok)
            if bname.lower() in ("python", "python3", "bash", "sh", "python3.10"):
                continue
            target = bname
            break
        if not target and tokens:
            target = os.path.basename(tokens[0])
        s = f"실행: {target}"
    
    # 3. 50자 이내
    if len(s) > 50:
        s = s[:50]
    return s if s else "-"

def get_task_name(task):
    parts = [p for p in os.path.normpath(task).split(os.sep) if p]
    if len(parts) >= 2 and parts[-1].lower() in ("lanea", "laneb", "lanec", "worker", "fix1", "fix2", "fix3", "_smoke", "_smoke2", "smoke"):
        return f"{parts[-2]}/{parts[-1]}"
    if len(parts) >= 1:
        return parts[-1]
    return task

def estimate_eta(task_dir, state, now_ts):
    if state in ('완료', '실패'):
        return "-"
    
    status_file = os.path.join(task_dir, "STATUS")
    if not os.path.isfile(status_file):
        return "-"
    
    try:
        with open(status_file, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception:
        return "-"
    
    if not lines:
        return "-"

    def parse_ts_and_text(line):
        m = re.match(r'^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]?', line)
        if not m:
            return None, line
        ts_str = m.group(1).replace('T', ' ').split('.')[0]
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            t = dt.timestamp()
            rest = line[m.end():].lstrip(': \t')
            return t, rest
        except Exception:
            return None, line

    def is_multi_stage_task(lines_list, last_line_text):
        for l in lines_list:
            m = re.search(r'\[(\d+)\s*/\s*(\d+)\]', l)
            if not m:
                m = re.search(r'(?:단계|step|phase)\s*(\d+)\s*/\s*(\d+)', l, re.IGNORECASE)
            if m:
                cur, tot = int(m.group(1)), int(m.group(2))
                if cur < tot:
                    return True

        m_acc = re.search(r'누적.*?(\d+)\s*/\s*(\d+)', last_line_text)
        if m_acc:
            acc_tot = int(m_acc.group(2))
            m_first = re.search(r'(\d+)\s*/\s*(\d+)', last_line_text)
            if m_first:
                first_m = int(m_first.group(2))
                if first_m < acc_tot:
                    return True

        full_text = "\n".join(lines_list)
        if re.search(r'\bSTART\s+full:.*?\bthen\b', full_text, re.IGNORECASE):
            return True

        batches = set(re.findall(r'batch_(\d+)', full_text))
        if len(batches) > 1:
            cur_b = re.search(r'batch_(\d+)', last_line_text)
            if cur_b:
                cur_num = int(cur_b.group(1))
                max_num = max(int(b) for b in batches)
                if cur_num < max_num:
                    return True
                if m_acc and int(m_acc.group(1)) < int(m_acc.group(2)):
                    return True

        if re.search(r'(?:남은\s*단계|남은\s*작업|다음\s*단계):', full_text):
            return True

        return False

    def parse_progress_nm(text):
        cleaned = re.sub(r'\[\d+/\d+\]', '', text)
        cleaned = re.sub(r'\(\d+/\d+\)', '', cleaned)
        m = re.search(r'(\d+)\s*/\s*(\d+)장', cleaned)
        if not m:
            m = re.search(r'(\d+)\s*/\s*(\d+)', cleaned)
        return m

    last_line = lines[-1]
    _, last_text = parse_ts_and_text(last_line)
    
    eta_ts = None

    # 규칙 1: 마지막 줄에 "N/M장" 또는 "N/M" 과 "X s/장"(또는 "Xs/장") 이 있으면 지금 시각 + (M-N)×X.
    speed_match = re.search(r'([0-9.]+)\s*s/장', last_text)
    nm_match = parse_progress_nm(last_text)
    if speed_match and nm_match:
        try:
            n = int(nm_match.group(1))
            m = int(nm_match.group(2))
            speed = float(speed_match.group(1))
            if m > 0:
                rem_sec = max(0, m - n) * speed
                eta_ts = now_ts + rem_sec
        except Exception:
            eta_ts = None

    # 규칙 2: 속도가 없으면 같은 단계 이름의 이전 줄들의 N 과 시각 변화로 속도를 계산해 추정.
    if eta_ts is None and nm_match:
        try:
            n_last = int(nm_match.group(1))
            m_last = int(nm_match.group(2))
            prefix = last_text[:nm_match.start()].strip()
            stage_key = prefix.split(':')[0].strip() if ':' in prefix else prefix.strip()
            
            history = []
            for l in lines:
                t, txt = parse_ts_and_text(l)
                if t is None:
                    continue
                if stage_key and stage_key not in txt:
                    continue
                m_cur = parse_progress_nm(txt)
                if m_cur:
                    c_n = int(m_cur.group(1))
                    c_m = int(m_cur.group(2))
                    if c_m == m_last:
                        history.append((t, c_n))
            
            if len(history) >= 2:
                t_first, n_first = history[0]
                t_end, n_end = history[-1]
                dn = n_end - n_first
                dt = t_end - t_first
                if dn > 0 and dt > 0:
                    speed = dt / dn
                    rem_sec = max(0, m_last - n_last) * speed
                    eta_ts = now_ts + rem_sec
        except Exception:
            eta_ts = None

    # 규칙 3: "epoch=k" 와 최대 epoch(같은 파일의 "--epochs E" 또는 "max E" 등)가 있으면 epoch 간 평균 시간으로 추정.
    if eta_ts is None:
        try:
            def find_max_epoch(lines_rev):
                for l in lines_rev:
                    m = re.search(r'--epochs[=\s]+(\d+)', l, re.IGNORECASE)
                    if m: return int(m.group(1))
                    m = re.search(r'\bepochs\s*=\s*(\d+)', l, re.IGNORECASE)
                    if m: return int(m.group(1))
                    m = re.search(r'\bmax[_\s]+epochs?[:\s=]+(\d+)', l, re.IGNORECASE)
                    if m: return int(m.group(1))
                    m = re.search(r'\bmax\s+(\d+)\s*epochs?', l, re.IGNORECASE)
                    if m: return int(m.group(1))
                    m = re.search(r'\bepoch\s*=\s*\d+\s*/\s*(\d+)', l, re.IGNORECASE)
                    if m: return int(m.group(1))
                return None

            def parse_epoch_val(line):
                m = re.search(r'\bepoch\s*=\s*(\d+)', line, re.IGNORECASE)
                if not m:
                    m = re.search(r'\bepoch\s+(\d+)\b', line, re.IGNORECASE)
                if not m:
                    m = re.search(r'\bepoch:\s*(\d+)', line, re.IGNORECASE)
                if m:
                    return int(m.group(1))
                return None

            max_epoch = find_max_epoch(reversed(lines))
            if max_epoch is not None and max_epoch > 0:
                epoch_history = []
                for l in reversed(lines):
                    t, txt = parse_ts_and_text(l)
                    k_val = parse_epoch_val(txt)
                    if k_val is not None and t is not None:
                        epoch_history.append((t, k_val))
                    if re.match(r'^(?:RUN\b|START\b)', txt) or re.search(r'\bTRAIN\s+\w+\s+start\b', txt):
                        break

                if epoch_history:
                    epoch_history.reverse()
                    k_last = epoch_history[-1][1]
                    if len(epoch_history) >= 2:
                        t1, k1 = epoch_history[0]
                        t2, k2 = epoch_history[-1]
                        dk = k2 - k1
                        dt = t2 - t1
                        if dk > 0 and dt > 0:
                            avg_sec = dt / dk
                            rem_epochs = max(0, max_epoch - k_last)
                            eta_ts = now_ts + rem_epochs * avg_sec
                    elif len(epoch_history) == 1:
                        t_ep, k_ep = epoch_history[0]
                        t_start = None
                        for l in reversed(lines):
                            t, txt = parse_ts_and_text(l)
                            if t and (re.match(r'^(?:RUN\b|START\b)', txt) or 'start' in txt.lower()):
                                if t <= t_ep:
                                    t_start = t
                                    break
                        if t_start and t_ep > t_start and k_ep > 0:
                            avg_sec = (t_ep - t_start) / k_ep
                            rem_epochs = max(0, max_epoch - k_last)
                            eta_ts = now_ts + rem_epochs * avg_sec
        except Exception:
            eta_ts = None

    # 규칙 4: 위로 안 되면 "-".
    if eta_ts is None:
        return "-"

    eta_str = datetime.fromtimestamp(eta_ts).strftime("%H:%M")
    if is_multi_stage_task(lines, last_text):
        return f"{eta_str}(현 단계)"
    return eta_str

watch_dir = os.environ.get("TH_WATCH_DIR", os.path.expanduser("~/.team-harness/watch"))
registry = os.path.join(watch_dir, "tasks.txt")

now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

if not os.path.isfile(registry):
    if all_mode:
        print(now_str)
        print("등록 작업 없음")
    else:
        print("실행 0 · 실패(24h) 0 · 완료(24h) 0 · 숨김 0")
    print_notice()
    sys.exit(0)

try:
    with open(registry, "r", encoding="utf-8", errors="replace") as f:
        raw_tasks = [l.strip() for l in f if l.strip()]
except Exception:
    raw_tasks = []

valid_tasks = [t for t in raw_tasks if os.path.isdir(t)]
if not valid_tasks:
    if all_mode:
        print(now_str)
        print("등록 작업 없음")
    else:
        print("실행 0 · 실패(24h) 0 · 완료(24h) 0 · 숨김 0")
    print_notice()
    sys.exit(0)

def get_verify_status(task_dir):
    verified_file = os.path.join(task_dir, "VERIFIED")
    failed_file = os.path.join(task_dir, "VERIFY_FAILED")
    verify_sh = os.path.join(task_dir, "verify.sh")

    if os.path.isfile(os.path.join(task_dir, "verify_result.json")):
        return "VERIFY_FAILED" if os.path.isfile(failed_file) else "VERIFIED"
    if os.path.isfile(verified_file):
        return "통과"
    elif os.path.isfile(failed_file):
        return "실패"
    elif os.path.isfile(verify_sh):
        return "대기"
    return "-"

sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent.parent))
try:
    from team_harness.config import load_worker_context_settings
    from team_harness.worker_context import (
        ContextRequest,
        WorkerContextRegistry,
        decide,
        parse_task_front_matter,
        registry_path_for_workspace,
    )
except Exception:
    load_worker_context_settings = None

_CLI_QUOTA_PREFIX_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[:\d.+-]*\s+)?"
    r"(?:error[: ]|api error[: ]|api response[: ]|agy_error[: ]|stream error[: ]|fatal[: ]|resource_exhausted[: ]|\{\"(?:type\":\"error|error\":))",
    re.IGNORECASE,
)
_CLI_QUOTA_BODY_RE = re.compile(
    r"(?:hit your usage limit|usage limit reached|individual quota reached|rate limit (?:exceeded|reached)|"
    r"resource_exhausted|quota exceeded|at capacity|capacity exceeded|\b429\b)",
    re.IGNORECASE,
)
_CODE_FALSE_POS_RE = re.compile(
    r"(?:if\s*\(|re\.search|awk\s|grep\s|~\s*/|last\s*=\s*\$0)",
    re.IGNORECASE,
)

def is_cli_quota_error_line(raw_line):
    s = raw_line.rstrip("\r\n")
    if not s:
        return False
    if re.match(r"^\s*(?:[-*+#]|>|\d+[.)])\s", s):
        return False
    if re.match(r"^(?:[+-][^+-]|@@ )", s):
        return False
    if re.match(r"^ERROR:\s*test_", s, re.IGNORECASE):
        return False
    if re.search(r"→\s*`?failed`?|error\s*→\s*failed", s, re.IGNORECASE):
        return False
    if _CODE_FALSE_POS_RE.search(s):
        return False
    return bool(_CLI_QUOTA_PREFIX_RE.search(s) and _CLI_QUOTA_BODY_RE.search(s))

def get_context_summary_line(task_dir, prog):
    if load_worker_context_settings is None:
        return None
    tpath = Path(task_dir)
    ctx_file = tpath / ".th_context.json"
    meta = None
    if ctx_file.is_file():
        try:
            import json
            meta = json.loads(ctx_file.read_text(encoding="utf-8"))
        except Exception:
            meta = None
    workspace = None
    reg_path = None
    task_key = None
    follow_up = "normal"
    session_id = None
    if isinstance(meta, dict):
        ws_raw = meta.get("workspace")
        if isinstance(ws_raw, str) and ws_raw:
            workspace = Path(ws_raw)
        rp_raw = meta.get("registry_path")
        if isinstance(rp_raw, str) and rp_raw:
            reg_path = Path(rp_raw)
        tk_raw = meta.get("task_key")
        if isinstance(tk_raw, str) and tk_raw:
            task_key = tk_raw
        fu_raw = meta.get("follow_up")
        if isinstance(fu_raw, str) and fu_raw in ("short", "normal"):
            follow_up = fu_raw
        sid_raw = meta.get("session_id")
        if isinstance(sid_raw, str) and sid_raw:
            session_id = sid_raw
    if workspace is None:
        owner_file = tpath / ".th_owner"
        if owner_file.is_file():
            try:
                ow = owner_file.read_text(encoding="utf-8").strip()
                if ow and os.path.isdir(ow):
                    workspace = Path(ow)
            except Exception:
                pass
    if workspace is None:
        env_ws = os.environ.get("TH_WORKSPACE", "")
        if env_ws and os.path.isdir(env_ws):
            workspace = Path(env_ws)
    if (tpath / "task.md").is_file():
        try:
            task_key, follow_up = parse_task_front_matter(tpath / "task.md")
        except Exception:
            pass
    if reg_path is None and workspace is not None:
        reg_path = registry_path_for_workspace(workspace)
    if not task_key or reg_path is None or not reg_path.is_file():
        return None
    try:
        registry = WorkerContextRegistry(reg_path)
        sessions = registry.sessions()
    except Exception:
        return None
    entry = None
    if session_id:
        entry = next((item for item in sessions if item.get("session_id") == session_id), None)
    if entry is None:
        key_matches = [item for item in sessions if item.get("task_key") == task_key]
        if key_matches:
            entry = key_matches[-1]
    if entry is None:
        return None
    stage = entry.get("stage") or entry.get("lane") or (prog[:24] if prog and prog != "-" else "-")
    tokens = entry.get("input_tokens")
    m_status = entry.get("measurement_status") or "unknown"
    if isinstance(tokens, int) and not isinstance(tokens, bool):
        tok_str = f"{tokens // 1000}k"
    else:
        tok_str = "unknown"
    entry_state = str(entry.get("state") or "unknown")
    try:
        settings = load_worker_context_settings(workspace)
        phase = "boundary" if entry_state == "active" else "dispatch"
        long_wait = bool(
            entry.get("long_running_wait_requested")
            or (tpath / "context_exit_requested.json").is_file()
        )
        req = ContextRequest(
            task_key=task_key,
            agent_type=str(entry.get("agent_type") or "codex"),
            follow_up=follow_up,
            phase=phase,
            session_id=str(entry.get("session_id")) if entry.get("session_id") else None,
            long_running_wait_requested=long_wait,
        )
        snap = dict(entry)
        snap["candidates"] = sessions
        next_action = decide(req, snap, settings).action
    except Exception:
        next_action = "unknown"
    wl_path = entry.get("worklog_path") or (meta.get("worklog_path") if isinstance(meta, dict) else None) or str(tpath / "WORKLOG.md")
    return f"{task_key} | {stage} | 맥락 {tok_str}({m_status}) | {entry_state} | {next_action} | {wl_path}"

headers = ["작업", "내용", "상태", "검수", "마지막 변화", "완료 예상", "진행"]
rows = []
now_ts = time.time()

for task in valid_tasks:
    state = '진행'
    terminal = observer.terminal_status(Path(task))
    if terminal == 'DONE':
        state = '완료'
    elif terminal == 'FAILED':
        state = '실패'
    else:
        is_quota_err = False
        for cand in ("codex.log", "agy.log", "claude.log", "worker.log"):
            cand_path = os.path.join(task, cand)
            if not os.path.isfile(cand_path):
                continue
            try:
                with open(cand_path, "r", encoding="utf-8", errors="replace") as cf:
                    for line in cf:
                        if is_cli_quota_error_line(line):
                            is_quota_err = True
                            break
            except Exception:
                pass
            if is_quota_err:
                break
        
        if is_quota_err:
            state = '워커 오류(쿼터·용량)'
        else:
            if not observer.live_pids(Path(task)):
                state = '중단?'

    latest_ts = get_latest_output(task)
    if latest_ts is not None:
        age_min = max(0, int((now_ts - latest_ts) // 60))
        age_str = f"{age_min}분 전"
    else:
        age_str = "?"
    
    task_name = get_task_name(task)
    title = get_task_title(task)
    v_status = get_verify_status(task)
    prog = get_progress(task)
    eta = estimate_eta(task, state, now_ts)
    ctx_line = get_context_summary_line(task, prog)
    
    rows.append([task_name, title, state, v_status, age_str, eta, prog, latest_ts, ctx_line])

if not all_mode:
    recent_cutoff = now_ts - 24 * 60 * 60
    running = [r for r in rows if r[2] not in ("완료", "실패")]
    recent_failed = [r for r in rows if r[7] is not None and r[7] >= recent_cutoff and (r[2] == "실패" or r[3] in ("실패", "VERIFY_FAILED"))]
    recent_done = [r for r in rows if r[7] is not None and r[7] >= recent_cutoff and r[2] == "완료"]
    selected = []
    for row in running + recent_failed:
        if row not in selected:
            selected.append(row)
    print(f"실행 {len(running)} · 실패(24h) {len(recent_failed)} · 완료(24h) {len(recent_done)} · 숨김 {len(rows) - len(selected)}")
    ctx_Source = selected if selected else rows
    ctx_lines = [r[8] for r in ctx_Source if r[8]]
    max_task_lines = max(1, 10 - len(ctx_lines[:3]))
    for task_name, title, state, verify, age, eta, progress, _, _ in selected[:max_task_lines]:
        print(f"{task_name} | {state} | {age} | {progress[:40]}")
    for cline in ctx_lines[:3]:
        print(cline)
    print_notice()
    sys.exit(0)

raw_w = [max(str_width(headers[i]), max((str_width(r[i]) for r in rows), default=0)) for i in range(7)]

w0 = min(max(raw_w[0], str_width(headers[0])), 24)
w2 = min(max(raw_w[2], str_width(headers[2])), 8)
w3 = max(raw_w[3], str_width(headers[3]))
w4 = min(max(raw_w[4], str_width(headers[4])), 11)
w5 = min(max(raw_w[5], str_width(headers[5])), 15)

budget = 101 - w0 - w2 - w3 - w4 - w5
w1_req = raw_w[1]
w6_req = raw_w[6]

if w1_req + w6_req <= budget:
    w1 = w1_req
    w6 = w6_req
else:
    w1_cap = max(10, int(budget * 0.40))
    w6_cap = budget - w1_cap
    if w1_req < w1_cap:
        w1 = w1_req
        w6 = min(w6_req, budget - w1)
    elif w6_req < w6_cap:
        w6 = w6_req
        w1 = min(w1_req, budget - w6)
    else:
        w1 = w1_cap
        w6 = w6_cap

widths = [w0, w1, w2, w3, w4, w5, w6]

def render_row(cols):
    cells = [fit_width(c, w) for c, w in zip(cols, widths)]
    return "| " + " | ".join(cells) + " |"

sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"

print(render_row(headers))
print(sep)
for r in rows:
    print(render_row(r[:7]))
for r in rows:
    if r[8]:
        print(r[8])
print_notice()
EOF
}

if [[ -n $watch_seconds ]]; then
  while true; do
    clear
    render 1
    sleep "$watch_seconds"
  done
else
  render
fi
