#!/usr/bin/env bash
# th-status: 요청받았을 때만 등록 작업을 한 번 요약 표로 출력한다.
set -u

script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

source "$script_dir/th-watch-lib.sh"

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
  # The heredoc imports team_harness (httpx etc.); the system python3 lacks them.
  th_python - "$script_dir/../src/team_harness/worker_digest.py" "$all_mode" "${1:-0}" << 'EOF'
import sys, os, time, subprocess, re, unicodedata, json
from datetime import datetime
from pathlib import Path
import importlib.util

spec = importlib.util.spec_from_file_location("worker_digest", sys.argv[1])
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)

all_mode = len(sys.argv) > 2 and sys.argv[2] == "1"
watch_mode = len(sys.argv) > 3 and sys.argv[3] == "1"
NOTICE = "[ths] 사용자 확인용 화면 — 에이전트는 이 표를 다시 출력·요약하지 말 것(이상 있을 때만 한 줄)."
LOCAL_TZ = datetime.now().astimezone().tzinfo

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

def _scan_latest_mtime(root_path, max_depth=3):
    best = None
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                name = entry.name
                if name.startswith(".th_"):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        mt = int(entry.stat(follow_symlinks=False).st_mtime)
                        if best is None or mt > best:
                            best = mt
                    elif max_depth > 1 and entry.is_dir(follow_symlinks=False):
                        sub = _scan_latest_mtime(entry.path, max_depth - 1)
                        if sub is not None and (best is None or sub > best):
                            best = sub
                except OSError:
                    continue
    except OSError:
        return None
    return float(best) if best is not None else None

def get_latest_output(task):
    return _scan_latest_mtime(task, max_depth=3)

def event_time(rec, kind):
    """Use the files that record the classified event, not unrelated outputs."""
    task = Path(rec["path"])
    paths = []
    if kind == "done":
        paths = [task / "DONE"]
    else:
        if rec["terminal"] == "FAILED":
            paths.extend(task.glob("FAILED*"))
        if rec["v_status"] in ("실패", "VERIFY_FAILED"):
            paths.append(task / "VERIFY_FAILED")
        if rec["is_stalled"]:
            paths.extend([task / ".th_daemon.stall", task / ".th_watch.stall"])
        if rec["has_handoff_stall"]:
            paths.extend(task.glob(".th_daemon.handoff_stall.*"))
            paths.extend(task.glob(".th_notified.handoff_stall.*"))
        if rec["is_quota_err"]:
            quota_ts = quota_event_time(task)
            if quota_ts is not None:
                times_from_quota = quota_ts
            else:
                times_from_quota = 0
                paths.extend(task / name for name in ("codex.log", "agy.log", "claude.log", "worker.log"))
    times = [times_from_quota] if kind != "done" and rec["is_quota_err"] and times_from_quota else []
    for path in paths:
        try:
            if path.is_file():
                times.append(path.stat().st_mtime)
        except OSError:
            pass
    if times:
        return max(times)
    if (rec.get("observation") or {}).get("finished"):
        return rec["observation"]["finished"]
    # STATUS-only terminal events and interrupted workers have no event marker.
    try:
        return (task / "STATUS").stat().st_mtime
    except OSError:
        return rec["latest_ts"] or 0

def format_rel_time(ts, now_ts, include_ago=True):
    if ts is None:
        return "?"
    diff_sec = max(0, int(now_ts - ts))
    suffix = " 전" if include_ago else ""
    if diff_sec < 60:
        return "방금" if include_ago else "1분"
    mins = diff_sec // 60
    if mins < 60:
        return f"{mins}분{suffix}"
    hours = mins // 60
    if hours < 24:
        return f"{hours}시간{suffix}"
    days = hours // 24
    return f"{days}일{suffix}"

def get_task_start_ts(task_dir):
    for fn in ("worker.pid", "job.pid", ".th_owner"):
        p = os.path.join(task_dir, fn)
        if os.path.isfile(p):
            try:
                return os.path.getmtime(p)
            except OSError:
                pass
    status_file = os.path.join(task_dir, "STATUS")
    if os.path.isfile(status_file):
        try:
            with open(status_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    m = re.match(r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)", line)
                    if m:
                        ts_s = m.group(1).replace("T", " ")
                        fmt = "%Y-%m-%d %H:%M:%S" if len(ts_s) == 19 else "%Y-%m-%d %H:%M"
                        try:
                            stamp = datetime.strptime(ts_s, fmt).replace(tzinfo=LOCAL_TZ).timestamp()
                            if stamp <= time.time():
                                return stamp
                        except ValueError:
                            pass
                    break
        except OSError:
            pass
    task_md = os.path.join(task_dir, "task.md")
    if os.path.isfile(task_md):
        try:
            return os.path.getmtime(task_md)
        except OSError:
            pass
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

def clean_status_line(last_line):
    if not last_line:
        return "-"
    # 1. 날짜·시각 제거
    s = re.sub(r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[+-]\d{2}:\d{2}|Z|\s*KST|\s*UTC)?\]?[:\s|]*", "", last_line).strip()
    s = re.sub(r"^\[?\d{4}-\d{2}-\d{2}(?:\s*KST|\s*UTC)?\]?[:\s|]*", "", s).strip()
    s = re.sub(r"^\[?\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*KST|\s*UTC)?\]?[:\s|]*", "", s).strip()
    s = re.sub(r"^(?:KST|UTC)[:\s|]+", "", s).strip()

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
    return s if s else "-"

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
    s = clean_status_line(last_line)
    if len(s) > 50:
        s = s[:50]
    return s if s else "-"

def get_stage_summary(task_dir, state_label):
    status_file = os.path.join(task_dir, "STATUS")
    last_line = ""
    if os.path.isfile(status_file):
        try:
            with open(status_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    l = line.strip()
                    if l:
                        last_line = l
        except Exception:
            pass
    s = clean_status_line(last_line)
    s = re.sub(r"^(?:상태|결과|status)[:\s|]*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(
        r"^(?:(?:[A-Z0-9]+_)?(?:DONE|FAILED)(?:_[A-Z0-9]+)?|COMPLETE|COMPLETED|OK)\b[:\s—|-]*",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    if not s or s == "-":
        for fn in ("DONE", "FAILED"):
            fp = os.path.join(task_dir, fn)
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        first = f.readline().strip()
                        if first and first.upper() not in ("DONE", "FAILED"):
                            s = clean_status_line(first)
                            s = re.sub(r"^(?:DONE|FAILED)\b[:\s—|-]*", "", s, flags=re.IGNORECASE).strip()
                            break
                except Exception:
                    pass
    return s if s else "-"

def get_task_name(task):
    parts = [p for p in os.path.normpath(task).split(os.sep) if p]
    if len(parts) >= 2 and parts[-1].lower() in ("lanea", "laneb", "lanec", "worker", "fix1", "fix2", "fix3", "_smoke", "_smoke2", "smoke"):
        return f"{parts[-2]}/{parts[-1]}"
    if len(parts) >= 1:
        return parts[-1]
    return task

def get_project_short_name(task_dir):
    candidates = [task_dir]
    for fn in (".th_worktree", ".th_owner"):
        fp = os.path.join(task_dir, fn)
        if os.path.isfile(fp):
            try:
                val = open(fp, "r", encoding="utf-8", errors="replace").read().strip()
                if val:
                    candidates.append(val)
            except OSError:
                pass
    joined = " ".join(candidates)
    if "robot_vision3_autolabel" in joined:
        return "robot3"
    if "team-harness" in joined:
        return "th"
    wt = candidates[1] if len(candidates) > 1 else task_dir
    parts = [p for p in os.path.normpath(wt).split(os.sep) if p]
    for idx, part in enumerate(parts):
        if part in ("_outputs", "outputs", "runs") and idx > 0:
            return parts[idx - 1][:10]
    if len(parts) >= 2 and parts[0] == "home":
        if len(parts) >= 4 and parts[2] in ("rang", "orca", "workspaces"):
            return parts[3][:10]
    return parts[-1][:10] if parts else "-"

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
            t = dt.replace(tzinfo=LOCAL_TZ).timestamp()
            if t > now_ts:
                return None, line
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
    last_stamp, last_text = parse_ts_and_text(last_line)
    if last_stamp is None:
        return "-"
    
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

now_dt = datetime.now()
now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
now_hm = now_dt.strftime("%H:%M")

def get_unread_inbox_alerts(w_dir):
    """Return unread inbox entries, using the event's target session cursor."""
    grouped = {}
    for state_file in Path(w_dir, "inbox_unread").glob("*.tsv"):
        try:
            lines = state_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            stamp, size_text, _alerted, owner = parts[:4]
            route = parts[4] if len(parts) > 4 else "*"
            agent = parts[5] if len(parts) > 5 else "unknown"
            try:
                size, stamp_epoch = int(size_text), int(stamp)
            except ValueError:
                continue
            root = Path(owner, ".claude")
            active = []
            for entry in (root / ".inbox_wait.d").glob("*"):
                try:
                    pid = int(entry.name)
                    cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
                    lines_reg = entry.read_text().splitlines()
                    key = lines_reg[1] if len(lines_reg) > 1 else ""
                    if b"th-inbox-wait.sh" in cmd:
                        active.append(key)
                except (OSError, ValueError):
                    continue
            keys = active if route == "*" else [route]
            if route == "*":
                keys = [*keys, ""]
                keys.extend(entry.name for entry in (root / ".inbox_cursor.d").glob("*"))
            cursor = 0
            for key in keys:
                file = root / ".inbox_cursor.d" / key if key else root / ".inbox_cursor"
                try:
                    cursor = max(cursor, int(file.read_text().strip()))
                except (OSError, ValueError):
                    pass
            if cursor < size:
                group = (owner, route, route != "*" and route not in active, agent)
                count, oldest = grouped.get(group, (0, stamp_epoch))
                grouped[group] = (count + 1, min(oldest, stamp_epoch))
    return [(owner, route, missing, count, oldest, agent)
            for (owner, route, missing, agent), (count, oldest) in sorted(grouped.items())]

unread_inbox_alerts = get_unread_inbox_alerts(watch_dir)

def unread_inbox_line():
    if not unread_inbox_alerts:
        return ""
    details = []
    codex = {}
    for owner, route, missing, count, oldest, agent in unread_inbox_alerts:
        if agent == "codex":
            previous_count, previous_oldest = codex.get(owner, (0, oldest))
            codex[owner] = (previous_count + count, min(previous_oldest, oldest))
        elif missing:
            details.append(f"{owner} 세션 {route[:8]} waiter 없음")
        else:
            details.append(f"{owner} 미확인 {count}건 (가장 오래된 {datetime.fromtimestamp(oldest).strftime('%H:%M')})")
    lines = ["⚠ 알림 미수신: " + "; ".join(details)] if details else []
    lines.extend(f"ℹ Codex 창 확인 필요: {owner} {count}건 (가장 오래된 {datetime.fromtimestamp(oldest).strftime('%H:%M')})" for owner, (count, oldest) in codex.items())
    return "\n".join(lines)

def get_footer_line(w_dir):
    # 1. Quota locks
    quota_parts = []
    if os.path.isdir(w_dir):
        try:
            for fn in sorted(os.listdir(w_dir)):
                if not fn.startswith("quota_lock."):
                    continue
                rest = fn[len("quota_lock."):]
                if "." in rest:
                    continue
                qpath = os.path.join(w_dir, fn)
                if not os.path.isfile(qpath):
                    continue
                content = ""
                try:
                    content = open(qpath, "r", encoding="utf-8", errors="replace").read().strip()
                except OSError:
                    pass
                unlock_str = ""
                m_ampm = re.search(r"(?:try again at|until|after)\s+(\d{1,2}):(\d{2})\s*(AM|PM)", content, re.IGNORECASE)
                if m_ampm:
                    hh = int(m_ampm.group(1))
                    mm = m_ampm.group(2)
                    ap = m_ampm.group(3).upper()
                    if ap == "PM" and hh != 12:
                        hh += 12
                    elif ap == "AM" and hh == 12:
                        hh = 0
                    unlock_str = f"{hh:02d}:{mm} 해제"
                else:
                    m_24 = re.search(r"(?:해제|until|try again at)\s*(\d{1,2}:\d{2})", content, re.IGNORECASE)
                    if m_24:
                        unlock_str = f"{m_24.group(1)} 해제"
                if unlock_str:
                    quota_parts.append(f"{rest} 잠김({unlock_str})")
                else:
                    quota_parts.append(f"{rest} 잠김")
        except OSError:
            pass
    quota_summary = f"쿼터: {', '.join(quota_parts)}" if quota_parts else "쿼터: 정상"

    # 2. th-watchd processes
    watchd_procs = {}
    default_wdir = os.path.abspath(os.path.expanduser("~/.team-harness/watch"))
    cur_wdir = os.path.abspath(w_dir)
    is_custom_wdir = cur_wdir != default_wdir
    expected_env = f"TH_WATCH_DIR={w_dir}".encode()
    for entry in Path("/proc").glob("[0-9]*"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cmdline_bytes = (entry / "cmdline").read_bytes()
            if b"th-watchd.sh" not in cmdline_bytes:
                continue
            if is_custom_wdir:
                env_bytes = (entry / "environ").read_bytes().split(b"\0")
                if expected_env not in env_bytes:
                    continue
            ppid = 0
            stat_txt = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            after_comm = stat_txt.rsplit(") ", 1)[-1].split()
            if after_comm and after_comm[0] == "Z":
                continue
            if len(after_comm) >= 2 and after_comm[1].isdigit():
                ppid = int(after_comm[1])
            args = cmdline_bytes.decode("utf-8", errors="replace").split("\0")
            script_arg = next((a for a in args if "th-watchd.sh" in a), "")
            if script_arg and os.path.isabs(script_arg):
                base_dir = os.path.dirname(os.path.dirname(os.path.realpath(script_arg)))
            else:
                cwd = os.readlink(str(entry / "cwd"))
                if script_arg:
                    base_dir = os.path.dirname(os.path.dirname(os.path.realpath(os.path.join(cwd, script_arg))))
                else:
                    base_dir = cwd
            bname = os.path.basename(base_dir)
            label = "main" if bname == "team-harness" else (bname or "기타")
            watchd_procs[pid] = (ppid, label)
        except OSError:
            continue
    root_labels = [lbl for pid, (ppid, lbl) in watchd_procs.items() if ppid not in watchd_procs]
    if not root_labels:
        watchd_summary = "watchd: 없음"
    else:
        counts = {}
        for lbl in root_labels:
            counts[lbl] = counts.get(lbl, 0) + 1
        parts = [f"{lbl} {cnt}개" for lbl, cnt in counts.items()]
        watchd_summary = f"watchd: {', '.join(parts)}"

    return f"{quota_summary} · {watchd_summary}"

try:
    with open(registry, "r", encoding="utf-8", errors="replace") as f:
        raw_tasks = [l.strip() for l in f if l.strip()]
except Exception:
    raw_tasks = []

# Latest spawn wins; a late exit from an older PID must not replace it.
observed = {}
try:
    with open(os.path.join(watch_dir, "status_tasks.jsonl"), encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
                path = entry["path"]
                old = observed.get(path)
                if old is None or entry["time"] >= old["time"]:
                    observed[path] = entry
            except (ValueError, KeyError, TypeError):
                continue
except OSError:
    pass
legacy_tasks = set(raw_tasks)
raw_tasks = list(dict.fromkeys(raw_tasks + list(observed)))
valid_tasks = [t for t in raw_tasks if os.path.isdir(t)]
def print_unverified_alerts():
    counts = {}
    archived = []
    try:
        archived = Path(watch_dir, "tasks_done.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    for task in set(valid_tasks + archived):
        try:
            data = json.loads(Path(task, "verify_result.json").read_text(encoding="utf-8"))
            stamp = Path(task, "DONE").stat().st_mtime
            if (data["overall"] == "WARN 검증 없음" and Path(task, "DONE").is_file()
                    and 0 <= time.time() - stamp <= 7 * 86400):
                project = get_project_short_name(task)
                counts[project] = counts.get(project, 0) + 1
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for project, count in sorted(counts.items()):
        print(f"ℹ 검증 없음: {project} {count}건")

def get_verify_status(task_dir):
    verified_file = os.path.join(task_dir, "VERIFIED")
    failed_file = os.path.join(task_dir, "VERIFY_FAILED")
    verify_sh = os.path.join(task_dir, "verify.sh")

    if os.path.isfile(failed_file):
        return "VERIFY_FAILED" if os.path.isfile(os.path.join(task_dir, "verify_result.json")) else "실패"
    if os.path.isfile(os.path.join(task_dir, "VERIFY_NONE")):
        return "검증 없음"
    if os.path.isfile(os.path.join(task_dir, "verify_result.json")):
        try:
            if json.loads(Path(task_dir, "verify_result.json").read_text(encoding="utf-8"))["overall"] == "WARN 검증 없음":
                return "검증 없음"
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return "VERIFIED"
    if os.path.isfile(verified_file):
        return "통과"
    elif os.path.isfile(failed_file):
        return "실패"
    elif os.path.isfile(verify_sh):
        return "대기"
    return "-"

sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent.parent))
try:
    import json
    from team_harness.agents.session_capture import _read_prefix_and_tail, extract_session_id
    from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
    from team_harness.config import load_worker_context_settings
    from team_harness.worker_context import (
        ContextRequest,
        WorkerContextRegistry,
        decide,
        measure_agent_context,
        parse_task_front_matter,
        parse_task_metadata,
        registry_path_for_workspace,
    )
except Exception as exc:
    print(f"th-status: team_harness import failed: {exc}", file=sys.stderr)
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

def check_task_quota_error(task_dir):
    for cand in ("codex.log", "agy.log", "claude.log", "worker.log"):
        cand_path = os.path.join(task_dir, cand)
        if not os.path.isfile(cand_path):
            continue
        try:
            with open(cand_path, "r", encoding="utf-8", errors="replace") as cf:
                head = cf.read(32768)
                lines = head.splitlines()
                cf.seek(0, os.SEEK_END)
                size = cf.tell()
                if size > 32768:
                    cf.seek(max(32768, size - 65536), os.SEEK_SET)
                    lines.extend(cf.read().splitlines())
                for line in lines:
                    if is_cli_quota_error_line(line):
                        return True
        except Exception:
            pass
    return False

def quota_event_time(task_dir):
    stamps = []
    for name in ("codex.log", "agy.log", "claude.log", "worker.log"):
        try:
            with open(Path(task_dir) / name, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not is_cli_quota_error_line(line):
                        continue
                    match = re.search(r"\b(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?)", line)
                    if match:
                        try:
                            stamps.append(datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).timestamp())
                        except ValueError:
                            pass
        except OSError:
            pass
    return max(stamps) if stamps else None

# Scan /proc once for live agy processes and worker command lines
_PROC_BY_PID = {}
_AGY_PIDS_BY_TASK = {}
for _entry in Path("/proc").glob("[0-9]*/cmdline"):
    try:
        _pid = int(_entry.parent.name)
        _args = _entry.read_bytes().decode("utf-8", errors="replace").split("\0")
        if not _args or not _args[0]:
            continue
        _PROC_BY_PID[_pid] = _args
        _exe = os.path.basename(_args[0])
        _is_agy = _exe == "agy" or (
            _exe in ("bash", "sh", "node", "python3")
            and len(_args) > 1
            and os.path.basename(_args[1]) == "agy"
        )
        if _is_agy and observer.pid_alive(_pid):
            for _a in _args:
                if _a.startswith("/"):
                    _AGY_PIDS_BY_TASK.setdefault(_a, []).append(_pid)
    except (OSError, ValueError):
        continue

liveness_spec = importlib.util.spec_from_file_location("job_liveness", Path(sys.argv[1]).resolve().parents[2] / "scripts/th-job-liveness.py")
liveness = importlib.util.module_from_spec(liveness_spec)
liveness_spec.loader.exec_module(liveness)

def fast_live_pids(task_dir):
    return liveness.live_processes(task_dir)

_REGISTRY_SESSIONS_CACHE = {}
_SETTINGS_CACHE = {}
_WORKLOG_LAUNCH_CACHE = {}

def _get_registry_sessions(reg_path):
    key = str(reg_path)
    if key not in _REGISTRY_SESSIONS_CACHE:
        if reg_path is not None and reg_path.is_file():
            try:
                _REGISTRY_SESSIONS_CACHE[key] = WorkerContextRegistry(reg_path).sessions()
            except Exception:
                _REGISTRY_SESSIONS_CACHE[key] = []
        else:
            _REGISTRY_SESSIONS_CACHE[key] = []
    return _REGISTRY_SESSIONS_CACHE[key]

def _get_workspace_settings(workspace):
    key = str(workspace) if workspace else ""
    if key not in _SETTINGS_CACHE:
        try:
            _SETTINGS_CACHE[key] = load_worker_context_settings(workspace)
        except Exception:
            _SETTINGS_CACHE[key] = None
    return _SETTINGS_CACHE[key]

def _get_worklog_launches(workspace_dir):
    if not workspace_dir:
        return {}
    key = str(workspace_dir)
    if key not in _WORKLOG_LAUNCH_CACHE:
        launches = {}
        claude_dir = os.path.join(str(workspace_dir), ".claude")
        if os.path.isdir(claude_dir):
            try:
                files = sorted(f for f in os.listdir(claude_dir) if f.startswith("work_log") and f.endswith(".md"))
                for fn in files:
                    fp = os.path.join(claude_dir, fn)
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            for line in f:
                                if "worker=" not in line:
                                    continue
                                parts = [p.strip() for p in line.split("|")]
                                if len(parts) >= 4:
                                    tdir = parts[2]
                                    m_w = re.search(r"worker=(\S+)", parts[3])
                                    m_m = re.search(r"model=(.+?)(?:\s+task\.md=|$)", parts[3])
                                    if m_w:
                                        launches[tdir] = (
                                            m_w.group(1).strip(),
                                            m_m.group(1).strip() if m_m else "",
                                        )
                    except OSError:
                        pass
            except OSError:
                pass
        _WORKLOG_LAUNCH_CACHE[key] = launches
    return _WORKLOG_LAUNCH_CACHE[key]

def shorten_model(worker, model_raw, effort_raw=""):
    w = (worker or "").strip().lower()
    m = (model_raw or "").strip()
    e = (effort_raw or "").strip().lower()

    m_paren = re.search(r"\((High|Medium|Low)\)", m, re.IGNORECASE)
    if m_paren and not e:
        e = m_paren.group(1).lower()
        m = re.sub(r"\s*\([^)]*\)", "", m).strip()

    ml = m.lower()
    for tail in ("-high", "-medium", "-med", "-low"):
        if ml.endswith(tail) and not e:
            e = tail[1:]
            m = m[: -len(tail)]
            ml = m.lower()
            break

    short_e = {"medium": "med", "med": "med", "high": "high", "low": "low"}.get(e, "")

    short_m = ""
    if "terra" in ml:
        short_m = "terra"
    elif "sol" in ml:
        short_m = "sol"
    elif "argon" in ml:
        short_m = "argon"
    elif "flash" in ml:
        short_m = "flash"
    elif "sonnet" in ml:
        m_ver = re.search(r"sonnet[- ]?(\d+(?:\.\d+)?)", ml)
        short_m = f"sonnet-{m_ver.group(1)}" if m_ver else "sonnet"
    elif "opus" in ml:
        m_ver = re.search(r"opus[- ]?(\d+(?:\.\d+)?)", ml)
        short_m = f"opus-{m_ver.group(1)}" if m_ver else "opus"
    elif "haiku" in ml:
        short_m = "haiku"
    elif m and m not in ("-", "default", "unknown"):
        s = re.sub(r"^(?:gpt-|claude-|gemini-)", "", ml)
        short_m = s[:12]

    if short_m and short_e and not short_m.endswith(f"-{short_e}"):
        short_m = f"{short_m}-{short_e}"

    if not w:
        return short_m if short_m else "-"
    if short_m:
        return f"{w} {short_m}"
    return w

def get_worker_and_model(task_dir, ctx_info):
    worker = ""
    model = ""
    effort = ""

    if isinstance(ctx_info, dict):
        worker = str(ctx_info.get("agent_type") or "")
        model = str(ctx_info.get("model") or "")

    codex_log = os.path.join(task_dir, "codex.log")
    if os.path.isfile(codex_log):
        if not worker:
            worker = "codex"
        try:
            with open(codex_log, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(2048)
            m_mod = re.search(r"^model:\s*(.+)$", head, re.M)
            if m_mod and not model:
                model = m_mod.group(1).strip()
            m_eff = re.search(r"^reasoning effort:\s*(.+)$", head, re.M)
            if m_eff:
                effort = m_eff.group(1).strip()
        except OSError:
            pass

    if not model:
        for pid_fn in ("worker.pid", "job.pid"):
            pfile = os.path.join(task_dir, pid_fn)
            if os.path.isfile(pfile):
                try:
                    pval = open(pfile, "r", encoding="utf-8", errors="replace").read().strip()
                    if pval.isdigit():
                        args = _PROC_BY_PID.get(int(pval), [])
                        for idx, arg in enumerate(args):
                            if arg in ("-m", "--model") and idx + 1 < len(args):
                                model = args[idx + 1]
                            elif arg.startswith("model_reasoning_effort="):
                                effort = arg.split("=", 1)[1]
                            if not worker and os.path.basename(arg) in ("codex", "agy", "claude"):
                                worker = os.path.basename(arg)
                except OSError:
                    pass
        if not model:
            for apid in _AGY_PIDS_BY_TASK.get(task_dir, ()):
                args = _PROC_BY_PID.get(apid, [])
                if not worker:
                    worker = "agy"
                for idx, arg in enumerate(args):
                    if arg in ("-m", "--model") and idx + 1 < len(args):
                        model = args[idx + 1]

    if not worker or not model:
        owner = ""
        owner_file = os.path.join(task_dir, ".th_owner")
        if os.path.isfile(owner_file):
            try:
                owner = open(owner_file, "r", encoding="utf-8", errors="replace").read().strip()
            except OSError:
                pass
        if not owner:
            owner = os.environ.get("TH_WORKSPACE", "")
        if owner:
            launches = _get_worklog_launches(owner)
            if task_dir in launches:
                lw, lm = launches[task_dir]
                if not worker:
                    worker = lw
                if not model:
                    model = lm

    if not worker:
        for cand_w in ("codex", "agy", "claude"):
            if os.path.isfile(os.path.join(task_dir, f"{cand_w}.log")):
                worker = cand_w
                break

    return shorten_model(worker, model, effort)

def inspect_task_context(task_dir, prog):
    if load_worker_context_settings is None:
        return None, None
    tpath = Path(task_dir)
    ctx_file = tpath / ".th_context.json"
    meta = None
    if ctx_file.is_file():
        try:
            meta = json.loads(ctx_file.read_text(encoding="utf-8"))
        except Exception:
            meta = None
    workspace = None
    reg_path = None
    task_key = None
    follow_up = "normal"
    session_id = None
    context_budget = None
    agent_type = None
    model_name = None
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
        cb_raw = meta.get("context_budget")
        if isinstance(cb_raw, int) and not isinstance(cb_raw, bool):
            context_budget = cb_raw
        at_raw = meta.get("agent_type")
        if isinstance(at_raw, str) and at_raw:
            agent_type = at_raw
        md_raw = meta.get("model")
        if isinstance(md_raw, str) and md_raw:
            model_name = md_raw
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
            tmeta = parse_task_metadata(tpath / "task.md")
            if tmeta.get("task_key"):
                task_key = str(tmeta["task_key"])
            if tmeta.get("follow_up") in ("short", "normal"):
                follow_up = str(tmeta["follow_up"])
            if isinstance(tmeta.get("context_budget"), int):
                context_budget = int(tmeta["context_budget"])
        except Exception:
            pass
    if reg_path is None and workspace is not None:
        reg_path = registry_path_for_workspace(workspace)

    entry = None
    sessions = []
    if task_key and reg_path is not None:
        sessions = _get_registry_sessions(reg_path)
        if session_id:
            entry = next((item for item in sessions if item.get("session_id") == session_id), None)
        if entry is None:
            key_matches = [item for item in sessions if item.get("task_key") == task_key]
            if key_matches:
                entry = key_matches[-1]

    if entry is not None:
        if not agent_type and entry.get("agent_type"):
            agent_type = str(entry.get("agent_type"))
        if not model_name and entry.get("model"):
            model_name = str(entry.get("model"))

    ctx_info = {
        "agent_type": agent_type,
        "model": model_name,
        "context_tag": "",
    }

    if entry is None:
        return None, ctx_info

    stage = entry.get("stage") or entry.get("lane") or (prog[:24] if prog and prog != "-" else "-")
    tokens = entry.get("input_tokens")
    m_status = entry.get("measurement_status") or "unknown"
    entry_state = str(entry.get("state") or "unknown")

    if tokens is None and entry_state == "active":
        try:
            ag = str(entry.get("agent_type") or agent_type or "codex")
            sid = str(entry.get("session_id") or session_id or "")
            log_p = tpath / f"{ag}.log"
            tmpl = DEFAULT_AGENT_TEMPLATES.get(ag)
            if log_p.is_file() and tmpl is not None:
                raw_log = _read_prefix_and_tail(log_p, 65536)
                cap_sid = extract_session_id(tmpl, raw_log, None)
                if cap_sid:
                    sid = cap_sid
            if sid:
                s_root = Path(os.environ.get("HOME", str(Path.home()))) / ".codex" / "sessions"
                meas = measure_agent_context(agent_type=ag, session_id=sid, sessions_root=s_root)
                if meas.input_tokens is not None:
                    tokens = meas.input_tokens
                    m_status = meas.measurement_status
        except Exception:
            pass

    settings = _get_workspace_settings(workspace)
    cap = context_budget or (settings.soft_cap_tokens if settings else 60000)

    if isinstance(tokens, int) and not isinstance(tokens, bool):
        tok_str = f"{tokens // 1000}k"
        cap_str = f"{cap // 1000}k" if isinstance(cap, int) and cap > 0 else "60k"
        ctx_info["context_tag"] = f"맥락 {tok_str}/{cap_str}"
    else:
        tok_str = "unknown"
    try:
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
            context_budget=context_budget,
        )
        snap = dict(entry)
        snap["candidates"] = sessions
        next_action = decide(req, snap, settings).action if settings else "unknown"
    except Exception:
        next_action = "unknown"
    wl_path = entry.get("worklog_path") or (meta.get("worklog_path") if isinstance(meta, dict) else None) or str(tpath / "WORKLOG.md")
    ctx_line = f"{task_key} | {stage} | 맥락 {tok_str}({m_status}) | {entry_state} | {next_action} | {wl_path}"
    return ctx_line, ctx_info

headers = ["작업", "내용", "상태", "검수", "마지막 변화", "완료 예상", "진행"]
rows = []
task_records = []
now_ts = time.time()

for task in valid_tasks:
    tpath = Path(task)
    pids, inferred = fast_live_pids(task)
    entry = observed.get(task)
    if entry and task not in legacy_tasks:
        try:
            raw = Path(f"/proc/{int(entry['pid'])}/stat").read_text().rsplit(")", 1)[1].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            alive = raw[19] == str(entry["start"]) and raw[0] != "Z" and boot == entry["boot"]
        except (OSError, KeyError, ValueError, IndexError, TypeError):
            alive = False
        pids, inferred = ([entry["pid"]] if alive else []), False
    is_alive = bool(pids)
    terminal = liveness.effective_terminal(tpath, observer.terminal_status(tpath), pids)
    if entry and task not in legacy_tasks and not is_alive and not terminal and "exit_code" in entry:
        terminal = "DONE" if entry["exit_code"] == 0 else "FAILED"
    is_quota_err = False
    has_handoff_stall = bool(
        list(tpath.glob(".th_daemon.handoff_stall.*"))
        or list(tpath.glob(".th_notified.handoff_stall.*"))
    )

    if terminal == 'DONE':
        state = '완료'
    elif terminal == 'FAILED':
        state = '실패'
    else:
        is_quota_err = check_task_quota_error(task)
        if is_quota_err:
            state = '워커 오류(쿼터·용량)'
        elif has_handoff_stall and not is_alive:
            state = '인계 없음'
        elif not is_alive:
            state = '중단?'
        else:
            state = '실행(추정)' if inferred else '진행'

    if is_alive and (inferred or terminal in ('DONE', 'FAILED')):
        state = '실행(추정)' if inferred else '실행'

    latest_ts = get_latest_output(task)
    if entry and task not in legacy_tasks:
        latest_ts = max(latest_ts or 0, entry.get("finished", entry["time"]))
    if latest_ts is not None:
        age_min = max(0, int((now_ts - latest_ts) // 60))
        age_str = f"{age_min}분 전"
    else:
        age_str = "?"

    task_name = get_task_name(task)
    v_status = get_verify_status(task)
    if is_alive and "VERIFY_FAILED" not in liveness.current_markers(tpath, pids) and v_status in ("실패", "VERIFY_FAILED"):
        v_status = "-"
    prog = (tpath / "STATUS").read_text(errors="replace").splitlines()[-1] if inferred and (tpath / "STATUS").is_file() and (tpath / "STATUS").stat().st_size else get_progress(task)
    ctx_line, ctx_info = inspect_task_context(task, prog)
    eta = estimate_eta(task, state, now_ts)

    if all_mode:
        title = get_task_title(task)
        rows.append([task_name, title, state, v_status, age_str, eta, prog, latest_ts, ctx_line, task])
    else:
        is_stalled = (
            os.path.isfile(os.path.join(task, ".th_daemon.stall"))
            or os.path.isfile(os.path.join(task, ".th_watch.stall"))
        )
        task_records.append({
            "path": task,
            "observation": entry,
            "name": task_name,
            "is_alive": is_alive,
            "terminal": terminal,
            "state": state,
            "v_status": v_status,
            "is_quota_err": is_quota_err,
            "has_handoff_stall": has_handoff_stall,
            "is_stalled": is_stalled,
            "latest_ts": latest_ts,
            "eta": eta,
            "prog": prog,
            "ctx_info": ctx_info,
        })

observed = []
try:
    from team_harness import proc_observer, state_index
    observed = proc_observer.observe()
    state_index.update(observed)
    import sqlite3
    from team_harness.workspace_archive import watch_dir as shim_watch_dir
    with sqlite3.connect(shim_watch_dir() / "th.db", timeout=0) as db:
        count = db.execute("SELECT count(*) FROM shim_decisions WHERE would_block=1 AND at >= ?", ((datetime.now(__import__("datetime").timezone.utc) - __import__("datetime").timedelta(hours=24)).isoformat(),)).fetchone()[0]
        breaker_count = db.execute("SELECT count(*) FROM breaker_events WHERE at >= ?", ((datetime.now(__import__("datetime").timezone.utc) - __import__("datetime").timedelta(hours=24)).isoformat(),)).fetchone()[0]
    if count:
        print(f"ℹ 실행 전 판정(관찰): {count}건 차단 대상")
    if breaker_count:
        print(f"ℹ 차단기(관찰): 최근 24시간 {breaker_count}건")
except Exception as exc:
    print(f"ths: observation unavailable: {exc}", file=sys.stderr)

def display_observed(tasks, pids):
    try:
        return proc_observer.display_records(observed, tasks, pids) if observed else []
    except Exception as exc:
        print(f"ths: observation unavailable: {exc}", file=sys.stderr)
        return []

if not all_mode:
    recent_cutoff = now_ts - 24 * 60 * 60
    running_recs = []
    attention_recs = []
    done_recs = []
    old_uncleaned_count = 0

    for rec in task_records:
        is_recent = rec["latest_ts"] is not None and rec["latest_ts"] >= recent_cutoff
        is_vfail = rec["v_status"] in ("실패", "VERIFY_FAILED")

        if rec["is_alive"]:
            if rec["terminal"] == "FAILED" or rec["is_quota_err"] or is_vfail or rec["is_stalled"] or (rec["has_handoff_stall"] and not rec["is_alive"]):
                attention_recs.append(rec)
            else:
                running_recs.append(rec)
        else:
            if not is_recent:
                old_uncleaned_count += 1
            elif rec["terminal"] == "FAILED" or rec["is_quota_err"] or is_vfail or rec["is_stalled"] or rec["has_handoff_stall"] or rec["terminal"] != "DONE":
                attention_recs.append(rec)
            else:
                done_recs.append(rec)

    attention_recs.sort(key=lambda r: (-event_time(r, "attention"), r["path"]))
    done_recs.sort(key=lambda r: (-event_time(r, "done"), r["path"]))
    attention_total, done_total = len(attention_recs), len(done_recs)
    attention_recs, done_recs = attention_recs[:5], done_recs[:5]

    from team_harness.coordinator_identity import alive as coordinator_alive, live_coordinators, session_owner, git_common_dir
    live = live_coordinators()
    groups = {}
    unknown = {}
    identities = {}
    repositories = [(meta, git_common_dir(meta["cwd"])) for meta in live]
    owner_repositories = {}
    def coordinator_label(cwd, common):
        worktree = Path(cwd).name or cwd
        if common is None:
            return worktree
        project = common.parent.name
        return project if project == worktree else f"{project} · {worktree}"

    def short_model(model):
        model = model or "모델 미확인"
        match = re.fullmatch(r"claude-(opus|sonnet)-(\d+)(?:-(\d+))?", model)
        if not match:
            return model
        name, major, minor = match.groups()
        return f"{name.title()} {major}" + (f".{minor}" if minor else "")

    for meta, common in repositories:
        key = (coordinator_label(meta["cwd"], common), meta["session"], str(meta["pid"]), str(meta["start"]), meta["model"] or "모델 미확인")
        groups[key] = []
        identities[(str(meta["pid"]), str(meta["start"]), meta["boot"])] = key
    for rec in running_recs:
        try:
            meta = (rec.get("observation") or {}).get("coordinator")
            metadata_exists = bool(meta)
            if not meta:
                meta_file = Path(rec["path"]) / ".th_coordinator.json"
                metadata_exists = meta_file.exists()
                if metadata_exists:
                    try:
                        meta = json.loads(meta_file.read_text())
                    except (OSError, ValueError):
                        meta = None
            if not metadata_exists:
                meta = session_owner(rec["path"], live)
            if not isinstance(meta, dict) or not coordinator_alive(meta):
                owner_file = Path(rec["path"]) / ".th_owner"
                owner = owner_file.read_text().strip() if owner_file.is_file() else ""
                if owner and owner not in owner_repositories:
                    owner_repositories[owner] = git_common_dir(owner)
                common = owner_repositories.get(owner) if owner else git_common_dir(rec["path"])
                matches = [c for c, repo in repositories if common is not None and repo == common]
                count = sum(2 if c.get("ambiguous") else 1 for c in matches)
                if count == 1:
                    candidate = matches[0]
                    key = identities[(str(candidate["pid"]), str(candidate["start"]), candidate["boot"])]
                    rec["adopted"] = True
                    groups[key].append(rec)
                else:
                    project = get_project_short_name(rec["path"])
                    old_pid = meta.get("pid") if isinstance(meta, dict) else None
                    label = (f"코디네이터 종료 — {project} (이전 PID {old_pid})"
                             if count == 0 and old_pid else f"코디네이터 불명 — {project} 후보 {count}개")
                    unknown.setdefault(label, []).append(rec)
                continue
            identity = (str(meta["pid"]), str(meta["start"]), meta["boot"])
            key = identities.get(identity)
            if key is None:
                owner_file = Path(rec["path"]) / ".th_owner"
                owner = owner_file.read_text().strip() if owner_file.is_file() else (rec.get("observation") or {}).get("owner", get_project_short_name(rec["path"]))
                key = (owner, str(meta.get("session") or ""), str(meta["pid"]), str(meta["start"]), str(meta.get("model") or "모델 미확인"))
            groups.setdefault(key, []).append(rec)
        except (OSError, ValueError, TypeError, KeyError):
            project = get_project_short_name(rec["path"])
            unknown.setdefault(f"코디네이터 불명 — {project} 후보 0개", []).append(rec)

    displayed_pids = set()
    for rec in task_records:
        for name in ("worker.pid", "job.pid"):
            try:
                displayed_pids.add(int((Path(rec["path"]) / name).read_text().strip()))
            except (OSError, ValueError):
                pass
    observed_groups = {}
    observed_under = {}
    for item in display_observed({rec["path"] for rec in task_records}, displayed_pids):
        if item["pid"] in displayed_pids:
            continue
        parent = item.get("worker_task") if item["origin"] != "watchd" else None
        if parent and any(rec["path"] == parent for rec in running_recs):
            observed_under.setdefault(parent, []).append(item)
            continue
        matching = next((key for key in groups if item["origin"] != "watchd" and ((item.get("claude_pid") and key[2] == item["claude_pid"]) or ((item["kind"] != "작업" or not item.get("claude_pid")) and item.get("session_id") and key[1] == item["session_id"]))), None)
        if matching is None and item["kind"] == "작업" and item.get("claude_pid"):
            matching = (f"{item.get('workspace') or item['cwd']} · 코디네이터 종료됨", "", "?", "", "")
            groups.setdefault(matching, [])
        if matching is None and item["origin"] in ("worker", "coordinator"):
            matching = (item.get("workspace") or "코디네이터 불명", item.get("session_id") or "", str(item.get("claude_pid") or "?"), "", "모델 미확인")
            groups.setdefault(matching, [])
        if matching is None:
            matching = ("감시 데몬" if item["origin"] == "watchd" else "사용자 직접" if item["origin"] == "user" else "출처 불명", "", "?", "", "")
        observed_groups.setdefault(matching, []).append(item)

    def print_observed(item):
        task = Path(item["task"]) if item.get("task") else None
        title = ""
        if task:
            try:
                title = next((line.lstrip("# ").strip() for line in (task / "task.md").read_text().splitlines() if line.startswith("# ")), "")
            except OSError:
                pass
            if not title:
                try:
                    title = (task / "STATUS").read_text().splitlines()[-1]
                except (OSError, IndexError):
                    pass
        try:
            ticks = int(item["start"].rsplit(":", 1)[1])
            elapsed = max(0, int(float(Path("/proc/uptime").read_text().split()[0]) - ticks / os.sysconf("SC_CLK_TCK")))
            duration = f"{elapsed // 60}분"
        except (OSError, ValueError, IndexError):
            duration = "시간 미상"
        if item["kind"] == "작업":
            count = f" ({item['count']}개)" if item.get("count", 1) > 1 else ""
            print(fit_width(f"    ↳ 작업{count} · {item['cmd'][:60]} · {duration} · {item.get('child_summary', '-')[:60]}", term_cols).rstrip())
            return
        print(fit_width(f"    ↳ {item['kind']} ({item.get('count', 1)}개) · {title or item['cmd']} · {duration} · {task.name if task else '-'} (PID {item['pid']})", term_cols).rstrip())

    term_cols = 110
    env_cols = os.environ.get("COLUMNS", "").strip()
    if env_cols.isdigit() and int(env_cols) > 0:
        term_cols = int(env_cols)
    else:
        try:
            term_cols = os.get_terminal_size().columns
        except OSError:
            if sys.stdout.isatty():
                try:
                    tp = subprocess.run(["tput", "cols"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1)
                    if tp.stdout.strip().isdigit() and int(tp.stdout.strip()) > 0:
                        term_cols = int(tp.stdout.strip())
                except Exception:
                    term_cols = 110

    show_worker_col = term_cols >= 80
    use_color = sys.stdout.isatty() and ("NO_COLOR" not in os.environ)

    def colorize(text, ansi_code):
        if not use_color:
            return text
        return f"\033[{ansi_code}m{text}\033[0m"

    def build_display_row(rec, section_kind):
        proj = get_project_short_name(rec["path"])
        tname = rec["name"]
        obs = rec.get("observation") or {}
        wm = (shorten_model(obs.get("worker", ""), obs.get("model", ""), "")
              if obs and rec["path"] not in legacy_tasks else get_worker_and_model(rec["path"], rec["ctx_info"]))
        updated = format_rel_time(rec["latest_ts"], now_ts, include_ago=True)
        stage = rec["prog"] if rec["state"] == "실행(추정)" else get_stage_summary(rec["path"], rec["state"])
        if rec["state"] in ("실행", "실행(추정)"):
            stage = f"{rec['state']} {stage}"
        eta = rec["eta"] if section_kind == "running" else "-"

        if section_kind == "running":
            if rec.get("adopted"):
                stage = f"인수됨 · {stage}"
            start_ts = get_task_start_ts(rec["path"]) or rec["latest_ts"]
            elapsed = format_rel_time(start_ts, now_ts, include_ago=False)
            ctx_tag = rec["ctx_info"].get("context_tag", "") if rec["ctx_info"] else ""
            if ctx_tag:
                stage = f"{stage} · {ctx_tag}" if stage and stage != "-" else ctx_tag
        elif section_kind == "attention":
            if rec["is_quota_err"]:
                elapsed = "쿼터오류"
                if quota_event_time(rec["path"]) is None:
                    stage = f"{stage} (시각 근사)"
            elif rec["has_handoff_stall"]:
                elapsed = "인계 없음"
            elif rec["terminal"] == "FAILED":
                elapsed = "실패"
            elif rec["v_status"] in ("실패", "VERIFY_FAILED"):
                elapsed = "검수실패"
            elif rec["is_stalled"]:
                elapsed = "멈춤"
            else:
                elapsed = "중단"
        else:
            elapsed = "완료"

        return {
            "proj": proj,
            "task": tname,
            "wm": wm,
            "elapsed": elapsed,
            "updated": updated,
            "eta": eta,
            "stage": stage,
        }

    run_rows = [build_display_row(r, "running") for r in running_recs]
    att_rows = [build_display_row(r, "attention") for r in attention_recs]
    done_rows = [build_display_row(r, "done") for r in done_recs]
    all_disp = run_rows + att_rows + done_rows

    task_cap = 22 if term_cols <= 90 else 28
    w_proj = min(max([str_width("프로젝트")] + [str_width(r["proj"]) for r in all_disp]), 8)
    w_task = min(max([str_width("작업"), 14] + [str_width(r["task"]) for r in all_disp]), task_cap)
    w_wm = min(max([str_width("워커/모델"), 10] + [str_width(r["wm"]) for r in all_disp]), 16)
    w_elap = min(max([str_width("경과"), 4] + [str_width(r["elapsed"]) for r in all_disp]), 10)
    w_upd = min(max([str_width("갱신"), 6] + [str_width(r["updated"]) for r in all_disp]), 8)
    w_eta = min(max([str_width("예정")] + [str_width(r["eta"]) for r in run_rows]), 12)

    if show_worker_col:
        prefix_w = 2 + w_proj + 2 + w_task + 2 + w_wm + 2 + w_elap + 2 + w_upd + 2 + w_eta + 2
    else:
        prefix_w = 2 + w_proj + 2 + w_task + 2 + w_elap + 2 + w_upd + 2 + w_eta + 2
    w_stage = max(14, term_cols - prefix_w)

    def format_section_header(section_kind):
        if show_worker_col:
            header = (
                "  "
                + fit_width("프로젝트", w_proj) + "  "
                + fit_width("작업", w_task) + "  "
                + fit_width("워커/모델", w_wm) + "  "
                + fit_width("경과", w_elap) + "  "
                + fit_width("갱신", w_upd) + "  "
            )
        else:
            header = (
                "  "
                + fit_width("프로젝트", w_proj) + "  "
                + fit_width("작업", w_task) + "  "
                + fit_width("경과", w_elap) + "  "
                + fit_width("갱신", w_upd) + "  "
            )
        if section_kind == "running":
            header += fit_width("예정", w_eta) + "  "
        return header + "단계"

    def format_section_row(drow, section_kind):
        st = fit_width(drow["stage"], w_stage).rstrip()
        if show_worker_col:
            row = (
                "  "
                + fit_width(drow["proj"], w_proj) + "  "
                + fit_width(drow["task"], w_task) + "  "
                + fit_width(drow["wm"], w_wm) + "  "
                + fit_width(drow["elapsed"], w_elap) + "  "
                + fit_width(drow["updated"], w_upd) + "  "
            )
        else:
            row = (
                "  "
                + fit_width(drow["proj"], w_proj) + "  "
                + fit_width(drow["task"], w_task) + "  "
                + fit_width(drow["elapsed"], w_elap) + "  "
                + fit_width(drow["updated"], w_upd) + "  "
            )
        if section_kind == "running":
            row += fit_width(drow["eta"], w_eta) + "  "
        return row + st

    print(
        f"작업 현황 {now_hm}  "
        f"실행 {len(run_rows)} · 주의 {attention_total} (표시 {len(att_rows)}) · 끝남 {done_total} (표시 {len(done_rows)}) · "
        f"오래된 미정리 {old_uncleaned_count} (숨김)"
    )
    if unread_inbox_line():
        print(unread_inbox_line())
    print_unverified_alerts()

    sections = [
        ("⚠ 주의 필요", "33", att_rows, False, "attention"),
        ("✓ 최근 끝남 (24h)", "90", done_rows, False, "done"),
    ]
    printed_header = False
    if run_rows or groups or observed_groups:
        print()
        print(colorize("▶ 실행 중", "32"))
        if run_rows or any(records for _, records in groups.items()):
            print(format_section_header("running"))
            printed_header = True
        for index, (key, records) in enumerate(sorted(groups.items())):
            if index:
                print()
            owner, session, pid, start, model = key
            print(fit_width(f"  {owner} · {short_model(model)} (PID {pid})", term_cols).rstrip())
            for rec in records:
                print(format_section_row(build_display_row(rec, "running"), "running"))
                for item in observed_under.get(rec["path"], []):
                    print_observed(item)
            for item in observed_groups.pop(key, []):
                print_observed(item)
        for key, items in observed_groups.items():
            print(f"  {key[0]}")
            for item in items:
                print_observed(item)
        if unknown:
            if groups:
                print()
            for label, records in sorted(unknown.items()):
                print(fit_width(f"  {label}", term_cols).rstrip())
                for rec in records:
                    print(format_section_row(build_display_row(rec, "running"), "running"))
                    for item in observed_under.get(rec["path"], []):
                        print_observed(item)
    for title_txt, color_code, srows, show_cols, section_kind in sections:
        if not srows:
            continue
        print()
        print(colorize(title_txt, color_code))
        if show_cols or not printed_header:
            print(format_section_header(section_kind))
            printed_header = True
        for drow in srows:
            print(format_section_row(drow, section_kind))

    print()
    print(get_footer_line(watch_dir))
    print_notice()
    sys.exit(0)

raw_w = [max(str_width(headers[i]), max((str_width(r[i]) for r in rows), default=0)) for i in range(7)]

if unread_inbox_line():
    print(unread_inbox_line())
print_unverified_alerts()

w0 = min(max(raw_w[0], str_width(headers[0])), 24)
w2 = min(max(raw_w[2], str_width(headers[2])), 10)
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
displayed_pids = set()
for task_path in raw_tasks:
    for pid_file in ("worker.pid", "job.pid"):
        try:
            displayed_pids.add(int((Path(task_path) / pid_file).read_text().strip()))
        except (OSError, ValueError):
            pass
extra_groups = {}
under_worker = {}
row_paths = {r[9] for r in rows}
for item in display_observed(set(raw_tasks), displayed_pids):
    if item["pid"] in displayed_pids:
        continue
    if item["origin"] != "watchd" and item.get("worker_task") in row_paths:
        under_worker.setdefault(item["worker_task"], []).append(item)
        continue
    label = (item.get("workspace") or "코디네이터 불명") if item["origin"] in ("worker", "coordinator") else ("감시 데몬" if item["origin"] == "watchd" else "사용자 직접" if item["origin"] == "user" else "출처 불명")
    if item["kind"] == "작업" and str(item.get("claude_pid") or "").isdigit() and not Path("/proc", str(item["claude_pid"])).exists():
        label += " · 코디네이터 종료됨"
    key = (label, str(item.get("claude_pid") or "?"), item.get("session_id") or "") if item["origin"] != "watchd" else (label, "?", "")
    extra_groups.setdefault(key, []).append(item)

def print_all_observed(item):
    task_path = Path(item["task"]) if item.get("task") else None
    title = ""
    if task_path:
        try:
            title = next((line.lstrip("# ").strip() for line in (task_path / "task.md").read_text().splitlines() if line.startswith("# ")), "")
        except OSError:
            pass
        if not title:
            try:
                title = (task_path / "STATUS").read_text().splitlines()[-1]
            except (OSError, IndexError):
                pass
    try:
        ticks = int(item["start"].rsplit(":", 1)[1])
        elapsed = max(0, int(float(Path("/proc/uptime").read_text().split()[0]) - ticks / os.sysconf("SC_CLK_TCK")))
        duration = f"{elapsed // 60}분"
    except (OSError, ValueError, IndexError):
        duration = "시간 미상"
    if item["kind"] == "작업":
        count = f" ({item['count']}개)" if item.get("count", 1) > 1 else ""
        print(f"    ↳ 작업{count} · {item['cmd'][:60]} · {duration} · {item.get('child_summary', '-')[:60]}")
        return
    print(f"    ↳ {item['kind']} ({item.get('count', 1)}개) · {title or item['cmd']} · {duration} · {task_path.name if task_path else '-'} (PID {item['pid']})")

for r in rows:
    print(render_row(r[:7]))
    for item in under_worker.get(r[9], []):
        print_all_observed(item)
for r in rows:
    if r[8]:
        print(r[8])
for (label, pid, session), items in extra_groups.items():
    print(f"  {label}" if label == "감시 데몬" else f"  {label} · 세션 {session or '미상'} (PID {pid})")
    for item in items:
        print_all_observed(item)
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
