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

headers = ["작업", "내용", "상태", "마지막 변화", "완료 예상", "진행"]
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
    eta = estimate_eta(task, state, now_ts)
    
    rows.append([task_name, title, state, age_str, eta, prog])

raw_w = [max(str_width(headers[i]), max((str_width(r[i]) for r in rows), default=0)) for i in range(6)]

w0 = min(max(raw_w[0], str_width(headers[0])), 26)
w2 = min(max(raw_w[2], str_width(headers[2])), 8)
w3 = min(max(raw_w[3], str_width(headers[3])), 11)
w4 = min(max(raw_w[4], str_width(headers[4])), 15)

budget = 101 - w0 - w2 - w3 - w4
w1_req = raw_w[1]
w5_req = raw_w[5]

if w1_req + w5_req <= budget:
    w1 = w1_req
    w5 = w5_req
else:
    w1_cap = max(10, int(budget * 0.40))
    w5_cap = budget - w1_cap
    if w1_req < w1_cap:
        w1 = w1_req
        w5 = min(w5_req, budget - w1)
    elif w5_req < w5_cap:
        w5 = w5_req
        w1 = min(w1_req, budget - w5)
    else:
        w1 = w1_cap
        w5 = w5_cap

widths = [w0, w1, w2, w3, w4, w5]

def render_row(cols):
    cells = [fit_width(c, w) for c, w in zip(cols, widths)]
    return "| " + " | ".join(cells) + " |"

sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"

print(render_row(headers))
print(sep)
for r in rows:
    print(render_row(r))
EOF
