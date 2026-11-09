"""Status grouping and bounded event lists with isolated synthetic tasks."""

import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from typing import cast


def test_bound_transcript_model_and_cache(tmp_path: Any, monkeypatch: Any):
    from team_harness.coordinator_models import resolve_model
    from team_harness.coordinator_models import session_for

    sid = "12345678-1234-1234-1234-123456789abc"
    other = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    root = tmp_path / ".codex/sessions/2026/01/01"
    root.mkdir(parents=True)
    path = root / f"rollout-2026-01-01-{sid}.jsonl"

    def event(model: Any):
        return json.dumps({"type": "turn_context", "payload": {"model": model}}) + "\n"

    path.write_text(event("actual-one"))
    cache = tmp_path / "cache"

    def resolve(session: Any = sid):
        return resolve_model(
            "codex", session, 123, "456", home=tmp_path, cache_dir=cache
        )

    assert session_for("codex", [], {b"CODEX_THREAD_ID": sid.encode()}) == sid
    assert session_for("claude", ["claude", "--resume", sid], {}) == sid
    assert resolve() == "actual-one"
    assert resolve(other) == ""
    original = Path.open

    def no_transcript_read(self: Any, *args: Any, **kwargs: Any):
        if self == path:
            raise AssertionError("unchanged transcript reread")
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", no_transcript_read)
        assert resolve() == "actual-one"
    with path.open("a") as stream:
        stream.write(event("actual-two"))
    assert resolve() == "actual-two"
    claude = tmp_path / ("." + "claude") / "projects/project"
    claude.mkdir(parents=True)
    (claude / f"{sid}.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "sessionId": sid, "message": {"model": "claude-real"}}
        )
        + "\n"
    )
    assert (
        resolve_model("claude", sid, 222, "3", home=tmp_path, cache_dir=cache)
        == "claude-real"
    )


def test_claude_process_session_file(tmp_path: Any, monkeypatch: Any):
    from team_harness.coordinator_models import resolve_model
    from team_harness.coordinator_models import session_for

    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "12345678-1234-1234-1234-123456789abc"
    old = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    session_file = sessions / "123.json"
    transcript = tmp_path / ".claude" / "projects" / "project"
    transcript.mkdir(parents=True)
    (transcript / f"{sid}.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "sessionId": sid, "message": {"model": "opus"}}
        )
        + "\n"
    )
    session_file.write_text(json.dumps({"sessionId": sid, "procStart": 456}))
    actual = session_for("claude", ["claude"], {}, 123, "456")
    assert actual == sid
    assert (
        resolve_model("claude", actual, 123, "456", home=tmp_path, persist_cache=False)
        == "opus"
    )
    assert session_for("claude", ["claude", "--resume", old], {}, 123, "456") == sid
    assert session_for("claude", ["claude", "-r", old], {}, 123, "457") == old
    session_file.write_text("{")
    assert (
        session_for(
            "claude", ["claude"], {b"CLAUDE_CODE_SESSION_ID": old.encode()}, 123, "456"
        )
        == old
    )
    session_file.unlink()
    assert session_for("claude", ["claude", "-r", old], {}, 123, "456") == old


def test_repository_adoption_display(tmp_path: Any):
    repo = Path(__file__).resolve().parents[2]
    main = tmp_path / "project"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(worktree)],
        check=True,
    )
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    watch = tmp_path / "watch"
    watch.mkdir()
    task = tmp_path / "task"
    task.mkdir()
    (task / "STATUS").write_text("working\n")
    (task / "worker.pid").write_text(str(os.getpid()))
    (task / ".th_owner").write_text(str(main))
    (task / ".th_coordinator.json").write_text(
        json.dumps({"pid": 99999999, "start": "1", "boot": "boot"})
    )
    (watch / "tasks.txt").write_text(str(task))
    shim = tmp_path / "sitecustomize.py"
    env = {
        **os.environ,
        "TH_WATCH_DIR": str(watch),
        "COLUMNS": "160",
        "NO_COLOR": "1",
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(repo / "src")]),
    }

    def output(paths: Any, models: Any = None):
        shim.write_text(
            "import team_harness.coordinator_identity as i\n"
            + "i.live_coordinators = lambda: "
            + repr(
                [
                    dict(
                        cwd=str(path),
                        session=f"s{n}",
                        pid=100 + n,
                        start="1",
                        boot="boot",
                        model=models[n] if models else "model",
                    )
                    for n, path in enumerate(paths)
                ]
            )
            + "\n"
        )
        return subprocess.run(
            [str(repo / "scripts/th-status.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    headers = output(
        [worktree, main, other, tmp_path],
        ["claude-opus-5-5", "claude-sonnet-5", "unrecognized", ""],
    )
    assert "project · worktree · Opus 5.5 (PID 100)" in headers
    assert "project · Sonnet 5 (PID 101)" in headers
    assert "other · unrecognized (PID 102)" in headers
    assert f"{tmp_path.name} · 모델 미확인 (PID 103)" in headers
    one = output([worktree])
    assert "project · worktree · model (PID 100)" in one and "인수됨" in one
    zero = output([other])
    assert "코디네이터 종료 — project (이전 PID 99999999)" in zero
    assert "인수됨" not in zero
    two = output([main, worktree])
    assert "코디네이터 불명 — project 후보 2개" in two
    assert "인수됨" not in two
    (task / ".th_coordinator.json").unlink()
    assert "코디네이터 불명 — project 후보 0개" in output([])
    (task / ".th_owner").unlink()
    nested = main / "outputs" / "job"
    nested.mkdir(parents=True)
    (nested / "STATUS").write_text("working\n")
    (nested / "job.pid").write_text(str(os.getpid()))
    (watch / "tasks.txt").write_text(str(nested))
    assert "인수됨" in output([worktree])
    ambiguous = output([main, worktree])
    assert "후보 2개" in ambiguous
    assert "인수됨" not in ambiguous


def test_empty_coordinator_groups(tmp_path: Any):
    repo = Path(__file__).resolve().parents[2]
    watch = tmp_path / "watch"
    watch.mkdir()
    shim = tmp_path / "sitecustomize.py"
    shim.write_text(
        "import team_harness.coordinator_identity as identity\n"
        "identity.live_coordinators = lambda: [\n"
        "    dict(cwd='/fixture/' + name, session='secret-' + name, pid=pid, "
        "start='1', boot='boot', model='actual-model')\n"
        "    for name, pid in [('alpha', 101), ('beta', 202), ('gamma', 303)]\n"
        "]\n"
    )
    env = {
        **os.environ,
        "TH_WATCH_DIR": str(watch),
        "COLUMNS": "110",
        "NO_COLOR": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(tmp_path), str(repo / "src"), os.environ.get("PYTHONPATH", "")]
        ),
    }
    command = [str(repo / "scripts/th-status.sh")]
    expected = "\n\n".join(
        f"  {name} · actual-model (PID {pid})"
        for name, pid in [("alpha", 101), ("beta", 202), ("gamma", 303)]
    )
    result = subprocess.run(
        command, env=env, text=True, capture_output=True, check=True
    )
    assert expected in result.stdout
    assert "프로젝트  작업" not in result.stdout
    assert "secret-" not in result.stdout
    watched = subprocess.Popen(
        command + ["-w", "1"], env=env, text=True, stdout=subprocess.PIPE
    )
    try:
        time.sleep(1.5)
        watched.terminate()
        out = watched.communicate(timeout=5)[0]
    finally:
        if watched.poll() is None:
            watched.kill()
            watched.wait()
    assert expected in out
    assert "프로젝트  작업" not in out
    assert "secret-" not in out


def test_groups_and_recent_five(tmp_path: Any):
    repo = Path(__file__).resolve().parents[2]
    watch = tmp_path / "watch"
    watch.mkdir()
    pid = os.getpid()
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    start = stat[19]
    tasks = []
    for i in range(17):
        kind = "실행" if i < 5 else "주의" if i < 11 else "끝남"
        task = tmp_path / f"{kind}{i:02}"
        task.mkdir()
        (task / "task.md").write_text(f"# {kind}{i:02}\n")
        (task / "STATUS").write_text("progress\n")
        if i < 5:
            (task / "worker.pid").write_text(str(pid))
            (task / ".th_owner").write_text("프로젝트")
            meta = {
                "pid": pid,
                "start": start,
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "session": f"session{i % 2}",
                "model": "astra" if i % 2 else "",
            }
            if i == 3:
                meta["start"] = "0"  # PID reuse
            if i == 4:
                meta["pid"] = 99999999  # dead
            (task / ".th_coordinator.json").write_text(json.dumps(meta))
        else:
            marker = task / ("FAILED" if i < 11 else "DONE")
            marker.write_text("test")
            event_time = int(time.time()) - (11 if i in (9, 10) else 20 - i)
            os.utime(marker, (event_time, event_time))
            os.utime(task / "STATUS", (time.time() - 100, time.time() - 100))
            os.utime(task / "task.md", (time.time() - 100, time.time() - 100))
            if i in (5, 11):
                (task / "unrelated.txt").write_text("later output")
        tasks.append(task)
    (watch / "tasks.txt").write_text("\n".join(map(str, tasks)))
    env = {**os.environ, "TH_WATCH_DIR": str(watch), "COLUMNS": "110", "NO_COLOR": "1"}
    result = subprocess.run(
        [str(repo / "scripts/th-status.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    out = result.stdout

    def assert_running_header(output: Any):
        running = (
            output.split("▶ 실행 중", 1)[1]
            .split("⚠ 주의 필요", 1)[0]
            .split("✓ 최근 끝남", 1)[0]
        )
        lines = running.splitlines()
        headers = [
            line
            for line in lines
            if "프로젝트" in line and "작업" in line and "워커/모델" in line
        ]
        assert len(headers) == 1
        assert lines[1] == headers[0]
        for index, line in enumerate(lines):
            if " · " in line and "(PID " in line or "코디네이터 종료" in line:
                has_workers = (
                    f"프로젝트 · astra (PID {pid})" in line
                    or f"프로젝트 · 모델 미확인 (PID {pid})" in line
                    or "코디네이터 종료" in line
                )
                if has_workers:
                    assert index + 1 < len(lines)
                    assert "실행" in lines[index + 1]
                else:
                    assert index + 1 == len(lines) or not lines[index + 1].strip()

    assert_running_header(out)
    assert "주의 6 (표시 5)" in out and "끝남 6 (표시 5)" in out
    assert f"프로젝트 · astra (PID {pid})" in out
    assert f"프로젝트 · 모델 미확인 (PID {pid})" in out
    assert "session0" not in out and "session1" not in out
    assert "\n\n  프로젝트 ·" in out
    assert "코디네이터 종료" in out
    assert "주의05" not in out and "끝남11" not in out
    assert out.index("주의09") < out.index("주의10")
    watched = subprocess.Popen(
        [str(repo / "scripts/th-status.sh"), "-w", "1"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        time.sleep(1.5)
        watched.terminate()
        watch_out = watched.communicate(timeout=5)[0]
    finally:
        if watched.poll() is None:
            watched.kill()
            watched.wait()
    assert "주의 6 (표시 5)" in watch_out
    assert_running_header(watch_out)
    assert "프로젝트 · astra" in watch_out
    assert "\n\n  프로젝트 ·" in watch_out
    assert "session0" not in watch_out and "session1" not in watch_out
    sample = os.environ.get("TH_SAMPLE_OUTPUT")
    if sample:
        Path(sample).write_text(out)


def test_sdk_assignment_records_coordinator_identity(tmp_path: Any):
    from types import SimpleNamespace

    from team_harness.config import Config
    from team_harness.coordinator_identity import alive
    from team_harness.tools.agent_tools import _prepare_agent_assignment

    config = Config(model="coordinator-model")
    _, assignment, *_ = _prepare_agent_assignment(
        agent_id="agent_test",
        prompt="test",
        run_log=cast(Any, SimpleNamespace(run_id="session-test")),
        config=config,
        run_dir=tmp_path,
        session_output_dir=str(tmp_path),
        caller_context=None,
        kwargs={},
    )
    identity = json.loads((assignment.parent / ".th_coordinator.json").read_text())
    assert alive(identity)
    assert identity["pid"] == os.getpid()
    assert identity["session"] == "session-test"
    assert identity["model"] == "coordinator-model"
    identity["start"] = "0"
    assert not alive(identity)


def test_observation_registry_is_separate_and_deduplicated(
    tmp_path: Any, monkeypatch: Any
):
    from team_harness.coordinator_identity import capture_current
    from team_harness.status_registry import append
    from team_harness.status_registry import register

    watch = tmp_path / "watch"
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    task = tmp_path / "관찰작업"
    meta = capture_current("session-observe", "coordinator-model")
    entry = register(
        task, os.getpid(), "codex", "worker-model", task / "worker.log", tmp_path, meta
    )
    assert not (watch / "tasks.txt").exists()
    result = subprocess.run(
        [str(Path(__file__).resolve().parents[2] / "scripts/th-status.sh")],
        env={**os.environ, "COLUMNS": "110", "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "coordinator-model" in result.stdout
    assert "관찰작업" in result.stdout
    append({**entry, "exit_code": 0, "finished": time.time()})
    # A reused PID must not be considered alive, including after an exit record.
    newer = {
        **entry,
        "time": entry["time"] + 1,
        "start": "0",
        "exit_code": 0,
        "finished": time.time(),
    }
    append(newer)
    append({**entry, "exit_code": 1, "finished": time.time()})
    out = subprocess.run(
        [str(Path(__file__).resolve().parents[2] / "scripts/th-status.sh")],
        env={**os.environ, "COLUMNS": "110", "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "끝남 1 (표시 1)" in out
    assert "실행 0" in out


async def test_common_spawn_records_and_updates_continuation(
    tmp_path: Any, monkeypatch: Any
):
    import asyncio

    from team_harness.agents.spawner import spawn
    from team_harness.config import Config
    from tests.helpers import fake_agent_template

    watch = tmp_path / "watch"
    monkeypatch.setenv("TH_WATCH_DIR", str(watch))
    task = tmp_path / "assignment"
    config = Config(
        model="coordinator-model", agent_templates={"codex": fake_agent_template()}
    )
    for _ in range(2):
        result = await spawn(
            agent_id="fake",
            agent_type="codex",
            prompt="test",
            cwd=tmp_path,
            config=config,
            log_dir=tmp_path / "logs",
            status_task_dir=task,
            status_session="session-a",
        )
        await result.proc.wait()
        await asyncio.sleep(0)
    entries = [
        json.loads(line)
        for line in (watch / "status_tasks.jsonl").read_text().splitlines()
    ]
    starts = [entry for entry in entries if "exit_code" not in entry]
    exits = [entry for entry in entries if "exit_code" in entry]
    assert len(starts) == len(exits) == 2
    assert starts[0]["pid"] != starts[1]["pid"]
    assert all(
        e["path"] == str(task) and e["coordinator"]["model"] == "coordinator-model"
        for e in entries
    )
    assert not (watch / "tasks.txt").exists()


def test_quota_event_timestamp_and_korean_width(tmp_path: Any):
    from datetime import datetime
    from datetime import timedelta

    watch = tmp_path / "watch"
    watch.mkdir()
    paths = []
    now = datetime.now().replace(microsecond=0)
    for index in range(6):
        task = tmp_path / f"한글작업{index}"
        task.mkdir()
        (task / "STATUS").write_text("progress\n")
        stamp = (now - timedelta(minutes=6 - index)).isoformat(sep=" ")
        (task / "codex.log").write_text(f"{stamp} ERROR: rate limit exceeded\n")
        paths.append(task)
    # Deliberately make the oldest event's file the most recently changed.
    os.utime(paths[0] / "codex.log", (time.time() + 10, time.time() + 10))
    (watch / "tasks.txt").write_text("\n".join(map(str, paths)))
    out = subprocess.run(
        [str(Path(__file__).resolve().parents[2] / "scripts/th-status.sh")],
        env={
            **os.environ,
            "TH_WATCH_DIR": str(watch),
            "COLUMNS": "110",
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "주의 6 (표시 5)" in out
    assert "한글작업0" not in out
    assert out.index("한글작업5") < out.index("한글작업1")
    import unicodedata

    for line in out.splitlines():
        assert (
            sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in line)
            <= 110
        )


def test_discovery_and_legacy_session_matching(tmp_path: Any, monkeypatch: Any):
    import team_harness.coordinator_identity as identity
    from team_harness.coordinator_identity import live_coordinators
    from team_harness.coordinator_identity import session_owner

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(identity, "resolve_model", lambda *args: "")
    original_process = identity.process
    child_ids = set()

    def isolated_process(pid: Any):
        info = original_process(pid)
        return (1, *info[1:]) if info and pid in child_ids else info

    monkeypatch.setattr(identity, "process", isolated_process)
    children = []
    try:
        for pane, role in (
            ("fixture-one", "coordinator"),
            ("fixture-two", "coordinator"),
            ("fixture-empty", "coordinator"),
            ("fixture-worker", "worker"),
        ):
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "ORCA_PANE_KEY": pane,
                "TH_ROLE": role,
            }
            env.pop("CLAUDE_CODE_SESSION_ID", None)
            children.append(
                subprocess.Popen(
                    ["bash", "-c", "exec -a claude sleep 60"], env=env, cwd=tmp_path
                )
            )
            child_ids.add(children[-1].pid)
        # Wait for exec without polling: the subprocess itself signals readiness via /proc on first scan.
        time.sleep(0.15)
        live = [c for c in live_coordinators() if c["session"].startswith("fixture-")]
        assert {c["session"] for c in live} == {
            "fixture-one",
            "fixture-two",
            "fixture-empty",
        }
        assert all(not c["model"] for c in live)
        task = tmp_path / "task"
        task.mkdir()
        session = task / ".th_session"
        session.write_text("fixture-one")
        assert session_owner(task, live)["session"] == "fixture-one"
        session.write_text("different-session")
        assert session_owner(task, live) is None
        session.write_text("fixture-two")
        os.utime(session, (1, 1))
        assert session_owner(task, live) is None
        duplicate = next(c for c in live if c["session"] == "fixture-one")
        session.write_text("fixture-one")
        assert session_owner(task, live + [duplicate]) is None
        invalid = {**duplicate, "start": "0"}
        from team_harness.coordinator_identity import alive

        assert not alive(invalid)
        children[0].terminate()
        children[0].wait()
        assert "fixture-one" not in {c["session"] for c in live_coordinators()}
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait()
