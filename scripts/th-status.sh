#!/usr/bin/env bash
# th-status: 요청받았을 때만 등록 작업을 한 번 요약 표로 출력한다.
set -u

exec python3 - "$@" << 'EOF'
import sys, os, time, subprocess, re, unicodedata
from datetime import datetime

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

def pid_alive(pid_str):
    if not pid_str:
        return False
    try:
        pid = int(pid_str.strip())
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False

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

watch_dir = os.environ.get("TH_WATCH_DIR", os.path.expanduser("~/.team-harness/watch"))
registry = os.path.join(watch_dir, "tasks.txt")

now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
print(now_str)

if not os.path.isfile(registry):
    print("등록 작업 없음")
    sys.exit(0)

try:
    with open(registry, "r", encoding="utf-8", errors="replace") as f:
        raw_tasks = [l.strip() for l in f if l.strip()]
except Exception:
    raw_tasks = []

valid_tasks = [t for t in raw_tasks if os.path.isdir(t)]
if not valid_tasks:
    print("등록 작업 없음")
    sys.exit(0)

headers = ["작업", "내용", "상태", "마지막 변화", "진행"]
rows = []
now_ts = time.time()

for task in valid_tasks:
    state = '진행'
    if os.path.isfile(os.path.join(task, "DONE")):
        state = '완료'
    elif os.path.isfile(os.path.join(task, "FAILED")):
        state = '실패'
    else:
        is_quota_err = False
        for cand in ("codex.log", "agy.log", "worker.log"):
            cand_path = os.path.join(task, cand)
            if not os.path.isfile(cand_path):
                continue
            try:
                with open(cand_path, "r", encoding="utf-8", errors="replace") as cf:
                    for line in cf:
                        if re.search(r"^ERROR:\s*", line, re.IGNORECASE) and not re.search(r"^ERROR:\s*test_", line, re.IGNORECASE):
                            if re.search(r"(quota|rate limit|usage limit|at capacity|capacity exceeded)", line, re.IGNORECASE):
                                is_quota_err = True
                                break
            except Exception:
                pass
            if is_quota_err:
                break
        
        if is_quota_err:
            state = '워커 오류(쿼터·용량)'
        else:
            job_pid = ""
            worker_pid = ""
            j_file = os.path.join(task, "job.pid")
            w_file = os.path.join(task, "worker.pid")
            if os.path.isfile(j_file):
                try:
                    with open(j_file, "r") as pf:
                        job_pid = pf.read().strip()
                except Exception:
                    pass
            if os.path.isfile(w_file):
                try:
                    with open(w_file, "r") as pf:
                        worker_pid = pf.read().strip()
                except Exception:
                    pass
            
            if job_pid:
                if not pid_alive(job_pid):
                    state = '중단?'
            else:
                if not pid_alive(worker_pid):
                    state = '중단?'

    latest_ts = get_latest_output(task)
    if latest_ts is not None:
        age_min = max(0, int((now_ts - latest_ts) // 60))
        age_str = f"{age_min}분 전"
    else:
        age_str = "?"
    
    task_name = get_task_name(task)
    title = get_task_title(task)
    prog = get_progress(task)
    
    rows.append([task_name, title, state, age_str, prog])

raw_w = [max(str_width(headers[i]), max(str_width(r[i]) for r in rows)) for i in range(5)]

w0 = min(max(raw_w[0], str_width(headers[0])), 28)
w2 = min(max(raw_w[2], str_width(headers[2])), 8)
w3 = min(max(raw_w[3], str_width(headers[3])), 11)

budget = 104 - w0 - w2 - w3
w1_req = raw_w[1]
w4_req = raw_w[4]

if w1_req + w4_req <= budget:
    w1 = w1_req
    w4 = w4_req
else:
    w1_cap = int(budget * 0.48)
    w4_cap = budget - w1_cap
    if w1_req < w1_cap:
        w1 = w1_req
        w4 = min(w4_req, budget - w1)
    elif w4_req < w4_cap:
        w4 = w4_req
        w1 = min(w1_req, budget - w4)
    else:
        w1 = w1_cap
        w4 = w4_cap

widths = [w0, w1, w2, w3, w4]

def render_row(cols):
    cells = [fit_width(c, w) for c, w in zip(cols, widths)]
    return "| " + " | ".join(cells) + " |"

sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"

print(render_row(headers))
print(sep)
for r in rows:
    print(render_row(r))
EOF
