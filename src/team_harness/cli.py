import asyncio
import json
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from team_harness.agents.manager import AgentManager
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.agents.tmux_view import tmux_available
from team_harness.config import _default_config_text
from team_harness.config import _local_config_text
from team_harness.config import CONFIG_PATH
from team_harness.config import load_config
from team_harness.config import LOCAL_CONFIG_DIR_NAME
from team_harness.config import RUNS_DIR
from team_harness.coordinator.loop import _perform_manual_compaction
from team_harness.coordinator.loop import run_one_turn
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.coordinator.system_prompt import COORDINATOR_PROMPT
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER
from team_harness.harness import _build_registry
from team_harness.harness import _finalize_run
from team_harness.harness import _graceful_shutdown
from team_harness.harness import _make_client
from team_harness.harness import _make_run_id
from team_harness.harness import _prepare_session_output_dir
from team_harness.harness import _show_no_config_hint
from team_harness.harness import _warn_provider_startup
from team_harness.harness import TeamHarness
from team_harness.skills.loader import load_skill_metadata
from team_harness.tools import agent_tools
from team_harness.tools import fs_tools
from team_harness.tools import todo_tools
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.context import resolve_model_limit
from team_harness.tracking.reaper import DEFAULT_DRAIN_TIMEOUT_S
from team_harness.tracking.reaper import DEFAULT_GRACE_S
from team_harness.tracking.reaper import reap_run
from team_harness.tracking.reaper import ReapRefusedError
from team_harness.tracking.reaper import resolve_run_json
from team_harness.tracking.run_log import RunLogWriter
from team_harness.ui.console import ConsoleBase
from team_harness.ui.console import make_console
from team_harness.ui.prompt import make_prompt_session
from team_harness.ui.prompt import read_user_input


@click.group()
def main() -> None:
    """th \u2014 multi-agent AI orchestration harness."""


def _write_config_file(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise click.ClickException(
            f"Config file already exists at {path}. Use --force to overwrite."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    click.echo(f"Created config at {path}")


def _write_sidecar_file(path: Path, text: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    click.echo(f"Created prompt file at {path}")


def _write_init_files(config_path: Path, config_text: str, force: bool) -> None:
    _write_config_file(path=config_path, text=config_text, force=force)
    _write_sidecar_file(
        config_path.parent / "coordinator_system_message.md", COORDINATOR_PROMPT
    )
    _write_sidecar_file(config_path.parent / "worker_suffix.md", "")
    _write_sidecar_file(config_path.parent / "worker_footer.md", DEFAULT_WORKER_FOOTER)


@main.command()
@click.option("--global", "use_global", is_flag=True, help="Create global config.")
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
def init(use_global: bool, force: bool) -> None:
    path = (
        CONFIG_PATH
        if use_global
        else Path.cwd() / LOCAL_CONFIG_DIR_NAME / "config.toml"
    )
    text = _default_config_text() if use_global else _local_config_text()
    _write_init_files(path, text, force)


@main.command(name="run")
@click.argument("task", required=False)
@click.option("--file", "-f", "task_file", type=click.Path())
@click.option("--provider", default=None)
@click.option("--model", default=None)
@click.option("--api-base", default=None)
@click.option("--api-key", default=None)
@click.option("--codex-auth-path", default=None)
@click.option("--agents", "allowed_agents", default=None)
@click.option("--max-retries", type=int, default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--system-prompt", default=None)
@click.option("--system-prompt-file", "cli_system_prompt_file", default=None)
@click.option("--cwd", default=".")
def run_cli(
    task: str | None,
    task_file: str | None,
    provider: str | None,
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    codex_auth_path: str | None,
    allowed_agents: str | None,
    max_retries: int | None,
    max_depth: int | None,
    system_prompt: str | None,
    cli_system_prompt_file: str | None,
    cwd: str,
) -> None:
    asyncio.run(
        _run(
            task=task,
            task_file=task_file,
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
            codex_auth_path=codex_auth_path,
            allowed_agents=allowed_agents,
            max_retries=max_retries,
            max_depth=max_depth,
            system_prompt=system_prompt,
            system_prompt_file=cli_system_prompt_file,
            cwd=cwd,
        )
    )


@main.command()
@click.option("--provider", default=None)
@click.option("--model", default=None)
@click.option("--api-base", default=None)
@click.option("--api-key", default=None)
@click.option("--codex-auth-path", default=None)
@click.option("--agents", "allowed_agents", default=None)
@click.option("--max-retries", type=int, default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--system-prompt", default=None)
@click.option("--system-prompt-file", "cli_system_prompt_file", default=None)
@click.option("--cwd", default=".")
def repl(
    provider: str | None,
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    codex_auth_path: str | None,
    allowed_agents: str | None,
    max_retries: int | None,
    max_depth: int | None,
    system_prompt: str | None,
    cli_system_prompt_file: str | None,
    cwd: str,
) -> None:
    asyncio.run(
        _repl(
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
            codex_auth_path=codex_auth_path,
            allowed_agents=allowed_agents,
            max_retries=max_retries,
            max_depth=max_depth,
            system_prompt=system_prompt,
            cli_system_prompt_file=cli_system_prompt_file,
            cwd=cwd,
        )
    )


@main.command()
@click.argument("run_id", required=False)
def logs(run_id: str | None) -> None:
    if run_id:
        run_json = RUNS_DIR / run_id / "run.json"
        if not run_json.exists():
            click.echo(f"ERROR: Run '{run_id}' not found at {run_json}", err=True)
            raise SystemExit(1)
        click.echo(json.dumps(json.loads(run_json.read_text()), indent=2))
        return
    if not RUNS_DIR.exists():
        click.echo("No runs yet.")
        return
    runs = sorted(RUNS_DIR.iterdir(), reverse=True)[:20]
    printed = False
    for run_dir in runs:
        run_json = run_dir / "run.json"
        if not run_json.exists():
            continue
        data = json.loads(run_json.read_text())
        click.echo(
            f"{run_dir.name}  start={str(data.get('start', '?'))[:19]}  "
            f"model={data.get('coordinator_model', '?')}  "
            f"turns={len(data.get('turns', []))}  agents={len(data.get('agents', []))}"
        )
        printed = True
    if not printed:
        click.echo("No runs yet.")


@main.command()
@click.argument("run_ref")
@click.option(
    "--policy",
    type=click.Choice(["drain", "reap", "ignore"]),
    default="drain",
    show_default=True,
    help="drain: wait for orphans to finish (timeout → kill); "
    "reap: kill now; ignore: only record what is still running.",
)
@click.option(
    "--drain-timeout-s",
    type=click.FloatRange(min=0),
    default=DEFAULT_DRAIN_TIMEOUT_S,
    show_default=True,
    help="Max seconds to wait — shared across ALL draining orphans — under "
    "--policy drain.",
)
@click.option(
    "--grace-s",
    type=click.FloatRange(min=0),
    default=DEFAULT_GRACE_S,
    show_default=True,
    help="Seconds between SIGTERM and SIGKILL when killing a group.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Act even if the run's original parent process still appears alive.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Probe and report only: no signals are sent, no files are written.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def reap(
    run_ref: str,
    policy: str,
    drain_timeout_s: float,
    grace_s: float,
    force: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Handle workers orphaned by a crashed run.

    RUN_REF is a run id (resolved under the runs dir), a run directory, or a
    direct path to its run.json. Every action verifies process identity
    (pgid + start time), so a recycled pid is never touched. Refuses to act on
    a run whose original parent is still alive unless --force is given.
    """
    candidate = Path(run_ref)
    if not candidate.exists():
        candidate = RUNS_DIR / run_ref
    run_json = resolve_run_json(candidate)
    if not run_json.exists():
        raise click.ClickException(f"run.json not found for '{run_ref}'")
    try:
        report = reap_run(
            run_json,
            policy=policy,  # type: ignore[arg-type]  # click.Choice guarantees the literal
            drain_timeout_s=drain_timeout_s,
            grace_s=grace_s,
            force=force,
            dry_run=dry_run,
        )
    except ReapRefusedError as exc:
        raise click.ClickException(str(exc)) from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise click.ClickException(f"cannot parse {run_json}: {exc}") from exc
    except OSError as exc:
        raise click.ClickException(f"cannot read {run_json}: {exc}") from exc
    if as_json:
        click.echo(json.dumps(report.model_dump(mode="json"), indent=2))
        return
    prefix = "[dry-run] " if dry_run else ""
    if not report.workers:
        click.echo(f"{prefix}{report.run_id}: no workers were left marked running.")
        return
    for worker in report.workers:
        click.echo(
            f"{prefix}{worker.agent_id} ({worker.agent_type}) "
            f"pgid={worker.pgid} policy={worker.policy}: {worker.outcome}"
        )


def _prepare_task(task: str | None, task_file: str | None) -> str:
    if task and task_file:
        raise click.UsageError("Provide either TASK or --file, not both.")
    if not task and not task_file:
        raise click.UsageError("Provide TASK or --file.")
    if task_file:
        return Path(task_file).read_text()
    assert task is not None
    return task


def _handle_kill_command(*, arg: str, manager: AgentManager, ui: ConsoleBase) -> None:
    """Let a human interrupt one running worker directly from the REPL.

    Unlike the coordinator-facing `kill_agent` tool, this bypasses the
    "don't kill too eagerly" heuristics entirely: a person watching the
    worker's live output (e.g. in a tmux window) asked for it explicitly, so
    there is nothing left to second-guess.
    """

    if not arg:
        running = [state for state in manager.list_all() if state.status == "running"]
        if not running:
            ui.print("실행 중인 worker가 없습니다.")
        else:
            ids = ", ".join(state.id for state in running)
            ui.print(f"사용법: /kill <agent_id>. 실행 중: {ids}")
        return
    matches = [
        state
        for state in manager.list_all()
        if state.id == arg or state.id.startswith(arg)
    ]
    if not matches:
        ui.print(f"'{arg}'에 해당하는 worker를 찾을 수 없습니다.")
        return
    if len(matches) > 1:
        ids = ", ".join(state.id for state in matches)
        ui.print(f"'{arg}'가 여러 worker와 일치합니다: {ids}")
        return
    target = matches[0]
    if target.status != "running":
        ui.print(f"{target.id}는 이미 '{target.status}' 상태입니다.")
        return
    manager.kill(target.id)
    ui.agent_event(event="killed", state=target)
    ui.print(f"{target.id} ({target.agent_type})를 종료했습니다.")


async def _run(task: str | None, task_file: str | None, **kwargs: Any) -> None:
    resolved_task = _prepare_task(task=task, task_file=task_file)
    allowed_agents = kwargs.pop("allowed_agents", None)
    harness = TeamHarness(
        provider=kwargs.get("provider"),
        model=kwargs.get("model"),
        api_base=kwargs.get("api_base"),
        api_key=kwargs.get("api_key"),
        codex_auth_path=kwargs.get("codex_auth_path"),
        agents=allowed_agents,
        max_retries=kwargs.get("max_retries"),
        max_depth=kwargs.get("max_depth"),
        system_prompt=kwargs.get("system_prompt"),
        system_prompt_file=kwargs.get("system_prompt_file"),
        cwd=kwargs.get("cwd"),
        console_mode="auto",
    )
    await harness.run(resolved_task)


async def _repl(**kwargs: Any) -> None:
    config = load_config(**kwargs)
    _show_no_config_hint(config, ui=None)
    click.echo(
        "No config file found. Run `team-harness init` to create one."
    ) if config.global_config_path is None and config.local_config_path is None else None
    run_id = _make_run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    session_output_dir = _prepare_session_output_dir(config=config, session_id=run_id)
    config.run_dir = run_dir
    manager = AgentManager()
    client = _make_client(config)
    run_log: RunLogWriter | None = None
    ui: ConsoleBase | None = None
    try:
        run_log = RunLogWriter(
            run_id=run_id,
            run_dir=run_dir,
            provider=config.provider,
            model=config.model,
            api_base=client.api_base,
            session_output_dir=str(session_output_dir),
        )
        model_limit = await resolve_model_limit(
            model_id=config.model, client=client, config=config
        )
        ctx = ContextTracker(model_id=config.model, model_limit=model_limit)
        ui = make_console(ctx=ctx, manager=manager, run_dir=run_dir, mode="auto")
        _warn_provider_startup(config, ui=ui)
        skills = load_skill_metadata(cwd=config.cwd)
        allowed_types = get_allowed_types(config)
        validate_templates(config=config, allowed_types=allowed_types)
        agent_tools.setup(
            manager=manager,
            run_log=run_log,
            config=config,
            ui=ui,
            session_output_dir=str(session_output_dir),
        )
        todo_tools.setup(run_dir=run_dir)
        fs_tools.setup_fs()
        registry = _build_registry(
            allowed_types=allowed_types,
            manager=manager,
            run_log=run_log,
            config=config,
            ui=ui,
            run_dir=run_dir,
            session_output_dir=str(session_output_dir),
        )
        system_prompt = build_system_prompt(
            config=config,
            allowed_types=allowed_types,
            skills=skills,
            session_output_dir=str(session_output_dir),
        )
        messages = [{"role": "system", "content": system_prompt}]
        turn_index = 0
        last_logged_index = 0
        session = make_prompt_session()
        ui.print_welcome(model=config.model, cwd=config.cwd, provider=config.provider)
        try:
            while True:
                ui.pause_for_input()
                try:
                    raw = await read_user_input(session)
                finally:
                    ui.resume_after_input()
                if raw is None:
                    break
                if not raw:
                    continue
                match raw:
                    case "/clear" | "/reset":
                        messages.clear()
                        messages.append({"role": "system", "content": system_prompt})
                        ctx.reset()
                        last_logged_index = 0
                        ui.reset_separator()
                        ui.print("Context reset. Agent state and run log preserved.")
                    case "/quit":
                        await _graceful_shutdown(
                            manager=manager,
                            run_log=run_log,
                            ui=ui,
                            timeout=config.shutdown_timeout_s,
                        )
                        break
                    case "/agents":
                        ui.print_agent_panel_inline()
                    case "/log":
                        ui.print(str(run_log.path))
                    case _ if raw == "/kill" or raw.startswith("/kill "):
                        _handle_kill_command(
                            arg=raw[len("/kill") :].strip(), manager=manager, ui=ui
                        )
                    case _ if raw == "/compact" or raw.startswith("/compact "):
                        focus_text = raw[len("/compact") :].strip() or None
                        compacted = await _perform_manual_compaction(
                            messages=messages,
                            client=client,
                            ctx=ctx,
                            ui=ui,
                            focus_text=focus_text,
                        )
                        if compacted:
                            last_logged_index = 0
                    case _:
                        ui.print_user_prompt(raw)
                        messages.append({"role": "user", "content": raw})
                        ctx.set_estimated_total(messages)
                        should_continue = True
                        while should_continue:
                            should_continue, last_logged_index = await run_one_turn(
                                messages=messages,
                                config=config,
                                run_log=run_log,
                                ui=ui,
                                tool_registry=registry,
                                client=client,
                                ctx=ctx,
                                turn_index=turn_index,
                                last_logged_index=last_logged_index,
                            )
                            turn_index += 1
        finally:
            await _finalize_run(
                manager=manager,
                run_log=run_log,
                session_output_dir=session_output_dir,
                shutdown_timeout_s=config.shutdown_timeout_s,
                ui=ui,
            )
            ui.stop()
    finally:
        await client.aclose()


@main.group()
def protocol() -> None:
    """Harness Protocol: MODE A/B/C multi-agent workflows.

    Canonical reference: design/harness_protocol/MODES.md,
    WORKFLOW.md, ENGINEERING_POLICY.md, HARNESS_AGENTS.md.

    \b
    MODE A — PARALLEL COMPETITION: two workers do the same task
             independently, then you pick a winner (`th protocol run --mode a`,
             then `th protocol resume`).
    MODE B — RELAY: hand an in-progress task to a different agent, keeping
             its git branch/worktree/state (`th protocol relay`).
    MODE C — ROLE PIPELINE: one agent designs, another implements, a third
             reviews (`th protocol run --mode c`).
    """


def _protocol_visible_option() -> Any:
    return click.option(
        "--visible/--no-visible",
        default=True,
        show_default=True,
        help="Open one tmux window per worker so you can watch it live "
        "(falls back to headless with a notice if tmux isn't installed).",
    )


def _resolve_visibility(
    *, visible: bool, tmux_session: str | None, task_id: str
) -> str | None:
    """Return the tmux session name to use, or None for headless."""

    if not visible:
        return None
    if not tmux_available():
        click.echo(
            "[protocol] tmux이 설치되어 있지 않아 화면 표시 없이(headless) 실행합니다."
        )
        return None
    return tmux_session or f"team-harness-{task_id}"


_COMPARE_REPORT_PRINT_LIMIT = 4000


def _print_protocol_state(state: Any, *, run_dir: Path) -> None:
    click.echo(f"[protocol] stage={state.stage} status={state.status}")
    if state.blocker:
        click.echo(f"[protocol] blocker: {state.blocker}")
    if state.status == "WAITING_USER" and getattr(state, "compare_path", None):
        _print_compare_report(state.compare_path)
    if state.user_selection:
        click.echo(f"[protocol] user_selection={state.user_selection}")
    if state.merge_status and state.merge_status != "PENDING":
        click.echo(f"[protocol] merge_status={state.merge_status}")
    if state.status == "DONE" and state.active_branch and state.checkpoint:
        # Protocol runs never modify the base working tree (TH-D16); merging
        # the result branch into the base branch is the user's call.
        click.echo(
            f"[protocol] 결과 브랜치: {state.active_branch} "
            f"(checkpoint {state.checkpoint[:12]})"
        )
        click.echo(
            "[protocol] base에 반영하려면: "
            f"git -C {state.target_repo} merge {state.active_branch}"
        )
    click.echo(f"[protocol] run_dir={run_dir}")


def _print_compare_report(compare_path: str) -> None:
    """Print the MODE A comparison report so a decision needs no other file.

    Capped, not because the content doesn't matter, but because this prints
    to a plain terminal — a truncation note always points back at the full
    file for anyone who wants the complete cross-review/response text.
    """

    try:
        text = Path(compare_path).read_text(encoding="utf-8")
    except OSError as exc:
        click.echo(f"[protocol] (비교 리포트를 읽을 수 없습니다: {exc})")
        return
    click.echo("[protocol] ----- 비교 리포트 -----")
    if len(text) > _COMPARE_REPORT_PRINT_LIMIT:
        click.echo(text[:_COMPARE_REPORT_PRINT_LIMIT])
        click.echo(f"[protocol] (리포트가 길어 잘렸습니다 — 전체 내용: {compare_path})")
    else:
        click.echo(text)
    click.echo("[protocol] ----- 비교 리포트 끝 -----")


@protocol.command("run")
@click.argument("task")
@click.option(
    "--mode",
    type=click.Choice(["a", "c"]),
    required=True,
    help="a=PARALLEL COMPETITION, c=ROLE PIPELINE. "
    "For MODE B (RELAY) use `th protocol relay` on an existing task-id.",
)
@click.option("--repo", default=".", show_default=True, help="Target git repository.")
@_protocol_visible_option()
@click.option(
    "--tmux-session",
    default=None,
    help="tmux session name (default: team-harness-<task-id>).",
)
def protocol_run(
    task: str, mode: str, repo: str, visible: bool, tmux_session: str | None
) -> None:
    """Start a fresh MODE A or MODE C task with the given TASK description."""

    asyncio.run(
        _protocol_run(
            task=task, mode=mode, repo=repo, visible=visible, tmux_session=tmux_session
        )
    )


async def _protocol_run(
    *, task: str, mode: str, repo: str, visible: bool, tmux_session: str | None
) -> None:
    from team_harness.protocol.mode_a import run_mode_a
    from team_harness.protocol.mode_c import run_mode_c
    from team_harness.protocol.mode_c import TeamHarnessAgentRunner

    task_id = _make_run_id()
    run_dir = RUNS_DIR / f"protocol-{task_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    target_repo = str(Path(repo).resolve())
    session_name = _resolve_visibility(
        visible=visible, tmux_session=tmux_session, task_id=task_id
    )

    click.echo(f"[protocol] task_id={task_id} mode={mode.upper()} run_dir={run_dir}")
    if session_name is not None:
        click.echo(f"[protocol] 실시간으로 보려면: tmux attach -t {session_name}")

    runner = TeamHarnessAgentRunner(log_dir=run_dir, tmux_session=session_name)
    mode_fn = run_mode_a if mode == "a" else run_mode_c
    state = await mode_fn(
        task_id=task_id,
        user_request=task,
        target_repo=target_repo,
        run_dir=run_dir,
        agent_runner=runner,
    )
    _print_protocol_state(state, run_dir=run_dir)
    if mode == "a" and state.status == "WAITING_USER":
        click.echo(
            "[protocol] MODE A는 사용자 선택이 필요합니다:\n"
            f"  th protocol resume --task-id {task_id} --run-dir {run_dir} "
            f"--repo {target_repo} --selection SELECT_WORKER_1|SELECT_WORKER_2"
            "|SELECT_HYBRID|REWORK|CANCEL"
        )


@protocol.command("resume")
@click.option("--task-id", required=True)
@click.option(
    "--run-dir", required=True, type=click.Path(), help="run_dir from `protocol run`."
)
@click.option("--repo", default=".", show_default=True)
@click.option(
    "--selection",
    required=True,
    type=click.Choice(
        [
            "SELECT_WORKER_1",
            "SELECT_WORKER_2",
            "SELECT_HYBRID",
            "REWORK",
            "CANCEL",
            "SELECT_CODEX",
            "SELECT_GEMINI",
        ]
    ),
)
@click.option("--note", default="", help="Optional instruction accompanying REWORK.")
@_protocol_visible_option()
@click.option("--tmux-session", default=None)
def protocol_resume(
    task_id: str,
    run_dir: str,
    repo: str,
    selection: str,
    note: str,
    visible: bool,
    tmux_session: str | None,
) -> None:
    """Complete a MODE A run that is WAITING_USER with your selection."""

    asyncio.run(
        _protocol_resume(
            task_id=task_id,
            run_dir=run_dir,
            repo=repo,
            selection=selection,
            note=note,
            visible=visible,
            tmux_session=tmux_session,
        )
    )


async def _protocol_resume(
    *,
    task_id: str,
    run_dir: str,
    repo: str,
    selection: str,
    note: str,
    visible: bool,
    tmux_session: str | None,
) -> None:
    from team_harness.protocol.mode_a import resume_mode_a
    from team_harness.protocol.mode_c import TeamHarnessAgentRunner

    resolved_run_dir = Path(run_dir).resolve()
    session_name = _resolve_visibility(
        visible=visible, tmux_session=tmux_session, task_id=task_id
    )
    runner = TeamHarnessAgentRunner(log_dir=resolved_run_dir, tmux_session=session_name)
    state = await resume_mode_a(
        task_id=task_id,
        selection=selection,
        user_instruction=note,
        run_dir=resolved_run_dir,
        target_repo=str(Path(repo).resolve()),
        agent_runner=runner,
    )
    _print_protocol_state(state, run_dir=resolved_run_dir)


@protocol.command("relay")
@click.option("--task-id", required=True)
@click.option(
    "--run-dir", required=True, type=click.Path(), help="run_dir from `protocol run`."
)
@click.option("--repo", default=".", show_default=True)
@click.option(
    "--next-agent",
    default=None,
    help="Agent type to relay to (default: the protocol config's mode_b_default).",
)
@_protocol_visible_option()
@click.option("--tmux-session", default=None)
def protocol_relay(
    task_id: str,
    run_dir: str,
    repo: str,
    next_agent: str | None,
    visible: bool,
    tmux_session: str | None,
) -> None:
    """MODE B — hand an existing task to a different agent, state intact."""

    asyncio.run(
        _protocol_relay(
            task_id=task_id,
            run_dir=run_dir,
            repo=repo,
            next_agent=next_agent,
            visible=visible,
            tmux_session=tmux_session,
        )
    )


async def _protocol_relay(
    *,
    task_id: str,
    run_dir: str,
    repo: str,
    next_agent: str | None,
    visible: bool,
    tmux_session: str | None,
) -> None:
    from team_harness.protocol.mode_b import run_mode_b
    from team_harness.protocol.mode_c import TeamHarnessAgentRunner

    resolved_run_dir = Path(run_dir).resolve()
    session_name = _resolve_visibility(
        visible=visible, tmux_session=tmux_session, task_id=task_id
    )
    runner = TeamHarnessAgentRunner(log_dir=resolved_run_dir, tmux_session=session_name)
    state = await run_mode_b(
        task_id=task_id,
        next_agent=next_agent,
        run_dir=resolved_run_dir,
        target_repo=str(Path(repo).resolve()),
        agent_runner=runner,
    )
    _print_protocol_state(state, run_dir=resolved_run_dir)
