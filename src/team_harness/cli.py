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


def _parse_set_role(values: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Turn repeated ``--set-role role.field=value`` flags into role tables.

    Seven protocol roles times three fields would be twenty-one dedicated
    flags, so one repeatable flag carries them all, e.g.
    ``--set-role mode_c_implement.model=gpt-5.6-sol
    --set-role mode_c_implement.effort=medium``. Role and field names are
    validated by the protocol layer, so a typo fails loudly instead of leaving
    the default silently in place.
    """
    from team_harness.protocol.config import PROTOCOL_ROLE_FIELDS
    from team_harness.protocol.config import PROTOCOL_ROLE_NAMES

    tables: dict[str, dict[str, str]] = {}
    for raw in values:
        target, _, value = raw.partition("=")
        role_name, _, field_name = target.partition(".")
        if not value.strip() or not role_name or not field_name:
            raise click.BadParameter(
                f"expected role.field=value, got {raw!r}", param_hint="--set-role"
            )
        if role_name not in PROTOCOL_ROLE_NAMES:
            raise click.BadParameter(
                f"unknown role {role_name!r}; known roles: "
                f"{', '.join(PROTOCOL_ROLE_NAMES)}",
                param_hint="--set-role",
            )
        if field_name not in PROTOCOL_ROLE_FIELDS:
            raise click.BadParameter(
                f"unknown field {field_name!r} for role {role_name!r}; known "
                f"fields: {', '.join(PROTOCOL_ROLE_FIELDS)}",
                param_hint="--set-role",
            )
        tables.setdefault(role_name, {})[field_name] = value.strip()
    return tables


def _protocol_role_options() -> Any:
    """Per-run role/timeout overrides shared by every `th protocol` command."""

    def decorate(fn: Any) -> Any:
        fn = click.option(
            "--set-role",
            "set_role",
            multiple=True,
            metavar="ROLE.FIELD=VALUE",
            help="Override one protocol role knob for this run, e.g. "
            "--set-role mode_c_implement.model=gpt-5.6-sol. FIELD is "
            "agent, model or effort. Repeatable. Takes precedence over "
            "HARNESS_MODE_* env vars and config.toml.",
        )(fn)
        fn = click.option(
            "--agent-timeout",
            "agent_timeout_sec",
            type=click.IntRange(min=1),
            default=600,
            show_default=True,
            help="Seconds a single worker stage may run before it is killed.",
        )(fn)
        fn = click.option(
            "--check-timeout",
            "check_timeout_sec",
            type=click.IntRange(min=1),
            default=300,
            show_default=True,
            help="Seconds the deterministic CHECK commands may run.",
        )(fn)
        return fn

    return decorate


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
# How many recent protocol runs `th protocol status` lists with no --run-dir.
_PROTOCOL_RUN_LIST_LIMIT = 20


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
    _print_protocol_usage(list(getattr(state, "handoffs", []) or []))
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
    default="c",
    show_default=True,
    help="a=PARALLEL COMPETITION, c=ROLE PIPELINE (the default when no mode is "
    "given, per design/harness_protocol/WORKFLOW.md). "
    "For MODE B (RELAY) use `th protocol relay` on an existing task-id.",
)
@click.option("--repo", default=".", show_default=True, help="Target git repository.")
@_protocol_visible_option()
@click.option(
    "--tmux-session",
    default=None,
    help="tmux session name (default: team-harness-<task-id>).",
)
@_protocol_role_options()
def protocol_run(
    task: str,
    mode: str,
    repo: str,
    visible: bool,
    tmux_session: str | None,
    set_role: tuple[str, ...],
    agent_timeout_sec: int,
    check_timeout_sec: int,
) -> None:
    """Start a fresh MODE A or MODE C task with the given TASK description."""

    asyncio.run(
        _protocol_run(
            task=task,
            mode=mode,
            repo=repo,
            visible=visible,
            tmux_session=tmux_session,
            set_role=set_role,
            agent_timeout_sec=agent_timeout_sec,
            check_timeout_sec=check_timeout_sec,
        )
    )


async def _protocol_run(
    *,
    task: str,
    mode: str,
    repo: str,
    visible: bool,
    tmux_session: str | None,
    set_role: tuple[str, ...] = (),
    agent_timeout_sec: int = 600,
    check_timeout_sec: int = 300,
) -> None:
    from team_harness.protocol.config import load_protocol_config
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
    # --set-role goes in as the runtime layer so a flag typed for this run
    # beats a stale HARNESS_MODE_* variable left in the shell; config.toml is
    # read relative to the target repo so a repo-local .team-harness/config.toml
    # applies to work done on that repo.
    proto_cfg = load_protocol_config(
        runtime_roles=_parse_set_role(set_role), config_start_dir=target_repo
    )
    mode_fn = run_mode_a if mode == "a" else run_mode_c
    state = await mode_fn(
        task_id=task_id,
        user_request=task,
        target_repo=target_repo,
        run_dir=run_dir,
        agent_runner=runner,
        protocol_config=proto_cfg,
        agent_timeout_sec=agent_timeout_sec,
        check_timeout_sec=check_timeout_sec,
    )
    _print_protocol_state(state, run_dir=run_dir)
    if mode == "a" and state.status == "WAITING_USER":
        click.echo(
            "[protocol] MODE A는 사용자 선택이 필요합니다:\n"
            f"  th protocol resume --task-id {task_id} --run-dir {run_dir} "
            f"--repo {target_repo} --selection SELECT_WORKER_1|SELECT_WORKER_2"
            "|SELECT_HYBRID|REWORK|CANCEL"
        )


@protocol.command("status")
@click.option(
    "--run-dir",
    default=None,
    type=click.Path(),
    help="run_dir from `protocol run`. Omit to list recent protocol runs.",
)
def protocol_status(run_dir: str | None) -> None:
    """Show a saved protocol run's stages, or list recent runs.

    Nothing here launches a worker or changes state — it only reads
    protocol_state.json, which is the only record of what a run actually did
    once its terminal output has scrolled away.
    """

    if run_dir is None:
        _print_protocol_run_list()
        return
    _print_protocol_status(Path(run_dir).resolve())


def _print_protocol_run_list() -> None:
    """List protocol run directories, newest first."""
    from team_harness.protocol.state import ProtocolStateManager

    candidates = sorted(
        (path for path in RUNS_DIR.glob("protocol-*") if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    if not candidates:
        click.echo(f"[protocol] {RUNS_DIR} 아래에 protocol 실행 기록이 없습니다.")
        return
    for path in candidates[:_PROTOCOL_RUN_LIST_LIMIT]:
        state = ProtocolStateManager(path).load_state()
        if state is None:
            click.echo(f"  {path.name}  (state 없음)")
            continue
        click.echo(
            f"  {path.name}  MODE {state.mode}  {state.status:<12} stage={state.stage}"
        )
    click.echo(
        f"[protocol] 자세히 보려면: th protocol status --run-dir {RUNS_DIR}/<이름>"
    )


def _print_protocol_status(run_dir: Path) -> None:
    """Print one run's stage table, then anything that blocked it."""
    from team_harness.protocol.state import ProtocolStateManager

    state = ProtocolStateManager(run_dir).load_state()
    if state is None:
        raise click.ClickException(f"No protocol state found in {run_dir}")

    click.echo(
        f"[protocol] task_id={state.task_id} MODE {state.mode} "
        f"status={state.status} stage={state.stage}"
    )
    if state.base_branch or state.base_commit:
        click.echo(f"[protocol] base: {state.base_branch} @ {state.base_commit[:12]}")
    for label, info in state.worktrees.items():
        click.echo(
            f"[protocol] worktree {label}: {info.get('branch')} @ {info.get('path')}"
        )

    if state.handoffs:
        click.echo("[protocol] ----- stage별 실행 기록 -----")
        for handoff in state.handoffs:
            click.echo("  " + _format_handoff(handoff))
    elif state.stage_statuses:
        click.echo("[protocol] ----- stage 상태 -----")
        for stage, status in state.stage_statuses.items():
            click.echo(f"  {stage:<16} {status}")

    _print_protocol_usage(state.handoffs)

    if state.checks:
        click.echo("[protocol] ----- CHECK -----")
        for check in state.checks:
            click.echo(f"  {check.get('status'):<8} {check.get('command')}")

    if state.blocker:
        click.echo(f"[protocol] blocker: {state.blocker}")
    if state.mode == "C" and state.status != "DONE":
        click.echo(
            "[protocol] 이어서 진행하려면: "
            f"th protocol resume --task-id {state.task_id} --run-dir {run_dir} "
            f"--repo {state.target_repo}"
        )
    click.echo(f"[protocol] run_dir={run_dir}")


def _print_protocol_usage(handoffs: list[dict[str, Any]]) -> None:
    """Print what the run spent, and say how much of that is actually known."""
    from team_harness.protocol.usage import summarize_usage

    totals = summarize_usage(handoffs)
    if totals.stages_run == 0 and totals.stages_refused == 0:
        return

    click.echo("[protocol] ----- 사용량 -----")
    line = (
        f"  실행된 stage {totals.stages_run}개 · 총 {totals.total_duration_sec:.1f}초"
    )
    if totals.stages_failed:
        line += f" · 실패 {totals.stages_failed}개"
    if totals.stages_refused:
        line += f" · 거부(실행 안 됨) {totals.stages_refused}개"
    click.echo(line)

    for label, mapping in (
        ("agent별 시간", totals.duration_by_agent),
        ("model별 시간", totals.duration_by_model),
    ):
        if not mapping:
            continue
        parts = ", ".join(
            f"{key} {value:.1f}s"
            for key, value in sorted(mapping.items(), key=lambda kv: -kv[1])
        )
        click.echo(f"  {label}: {parts}")

    if totals.stages_reporting_usage == 0:
        # Antigravity reports nothing, so this is normal, not an error.
        click.echo("  토큰: 워커가 보고한 사용량 없음")
        return

    numbers: list[str] = []
    if totals.total_tokens is not None:
        numbers.append(f"합계 {totals.total_tokens:,}")
    if totals.input_tokens is not None:
        numbers.append(f"입력 {totals.input_tokens:,}")
    if totals.output_tokens is not None:
        numbers.append(f"출력 {totals.output_tokens:,}")
    if totals.cached_input_tokens is not None:
        numbers.append(f"캐시 읽기 {totals.cached_input_tokens:,}")
    if numbers:
        click.echo("  토큰: " + " · ".join(numbers))
    if totals.cost_usd is not None:
        click.echo(f"  워커 보고 비용: ${totals.cost_usd:.4f} USD")

    # Never let a partial total read as a complete one.
    if not totals.usage_is_complete:
        click.echo(
            f"  ⚠ 위 토큰/비용은 실행된 {totals.stages_run}개 stage 중 "
            f"{totals.stages_reporting_usage}개만 반영합니다 "
            "(나머지 워커 CLI는 사용량을 보고하지 않습니다)."
        )


def _format_handoff(handoff: dict[str, Any]) -> str:
    """One line per stage: who ran it, on what model, and how it ended."""
    stage = str(handoff.get("stage", "?"))
    agent = str(handoff.get("agent_type") or handoff.get("agent") or "?")
    model = handoff.get("effective_model") or handoff.get("model")
    if handoff.get("spawned") is False:
        outcome = "실행되지 않음"
        model_label = "-"
    else:
        outcome = "성공" if handoff.get("success") else "실패"
        model_label = str(model or "default")
    line = f"{stage:<16} {outcome:<12} {agent} {model_label}"
    duration = handoff.get("duration_sec")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        line += f"  {float(duration):.1f}s"
    classification = handoff.get("failure_classification")
    if isinstance(classification, dict):
        detail = classification.get("category")
        resets_at = classification.get("resets_at")
        line += f"  [{detail}"
        line += f", resets {resets_at}]" if resets_at else "]"
    verdict = handoff.get("review_verdict")
    if verdict:
        line += f"  verdict={verdict}"
    return line


@protocol.command("resume")
@click.option("--task-id", required=True)
@click.option(
    "--run-dir", required=True, type=click.Path(), help="run_dir from `protocol run`."
)
@click.option("--repo", default=".", show_default=True)
@click.option(
    "--selection",
    default=None,
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
    help="MODE A only: which worker's result to take. Required for MODE A.",
)
@click.option("--note", default="", help="Optional instruction accompanying REWORK.")
@click.option(
    "--from-stage",
    default=None,
    type=click.Choice(["DESIGN", "IMPLEMENT", "REVIEW"]),
    help="MODE C only: stage to re-enter at. Defaults to the earliest stage "
    "that is not DONE, which after a block is the stage that blocked.",
)
@_protocol_visible_option()
@click.option("--tmux-session", default=None)
@_protocol_role_options()
def protocol_resume(
    task_id: str,
    run_dir: str,
    repo: str,
    selection: str | None,
    note: str,
    from_stage: str | None,
    visible: bool,
    tmux_session: str | None,
    set_role: tuple[str, ...],
    agent_timeout_sec: int,
    check_timeout_sec: int,
) -> None:
    """Continue a saved protocol run.

    \b
    MODE A — complete a WAITING_USER run with --selection.
    MODE C — re-enter the pipeline at --from-stage (default: the earliest
             stage that is not DONE) reusing the existing task worktree, so a
             run that blocked late does not re-pay for DESIGN and IMPLEMENT.

    The mode is read from the saved state, not guessed.
    """

    asyncio.run(
        _protocol_resume(
            task_id=task_id,
            run_dir=run_dir,
            repo=repo,
            selection=selection,
            note=note,
            from_stage=from_stage,
            visible=visible,
            tmux_session=tmux_session,
            set_role=set_role,
            agent_timeout_sec=agent_timeout_sec,
            check_timeout_sec=check_timeout_sec,
        )
    )


async def _protocol_resume(
    *,
    task_id: str,
    run_dir: str,
    repo: str,
    selection: str | None,
    note: str,
    from_stage: str | None = None,
    visible: bool,
    tmux_session: str | None,
    set_role: tuple[str, ...] = (),
    agent_timeout_sec: int = 600,
    check_timeout_sec: int = 300,
) -> None:
    from team_harness.protocol.config import load_protocol_config
    from team_harness.protocol.mode_a import resume_mode_a
    from team_harness.protocol.mode_c import resume_mode_c
    from team_harness.protocol.mode_c import TeamHarnessAgentRunner
    from team_harness.protocol.state import ProtocolStateManager

    resolved_run_dir = Path(run_dir).resolve()
    resolved_repo = str(Path(repo).resolve())
    saved = ProtocolStateManager(resolved_run_dir).load_state()
    if saved is None:
        raise click.ClickException(f"No protocol state found in {resolved_run_dir}")

    session_name = _resolve_visibility(
        visible=visible, tmux_session=tmux_session, task_id=task_id
    )
    runner = TeamHarnessAgentRunner(log_dir=resolved_run_dir, tmux_session=session_name)
    proto_cfg = load_protocol_config(
        runtime_roles=_parse_set_role(set_role), config_start_dir=resolved_repo
    )

    if saved.mode == "C":
        if selection is not None:
            raise click.ClickException(
                "--selection applies to MODE A runs; this run is MODE C. "
                "Use --from-stage instead."
            )
        state = await resume_mode_c(
            run_dir=resolved_run_dir,
            agent_runner=runner,
            from_stage=from_stage,
            protocol_config=proto_cfg,
            agent_timeout_sec=agent_timeout_sec,
            check_timeout_sec=check_timeout_sec,
        )
        _print_protocol_state(state, run_dir=resolved_run_dir)
        return

    if selection is None:
        raise click.ClickException(
            "--selection is required to resume a MODE A run "
            "(SELECT_WORKER_1|SELECT_WORKER_2|SELECT_HYBRID|REWORK|CANCEL)."
        )
    if from_stage is not None:
        raise click.ClickException(
            "--from-stage applies to MODE C runs; this run is MODE A."
        )
    state = await resume_mode_a(
        task_id=task_id,
        selection=selection,
        user_instruction=note,
        run_dir=resolved_run_dir,
        target_repo=resolved_repo,
        agent_runner=runner,
        protocol_config=proto_cfg,
        agent_timeout_sec=agent_timeout_sec,
        check_timeout_sec=check_timeout_sec,
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
@_protocol_role_options()
def protocol_relay(
    task_id: str,
    run_dir: str,
    repo: str,
    next_agent: str | None,
    visible: bool,
    tmux_session: str | None,
    set_role: tuple[str, ...],
    agent_timeout_sec: int,
    check_timeout_sec: int,
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
            set_role=set_role,
            agent_timeout_sec=agent_timeout_sec,
            check_timeout_sec=check_timeout_sec,
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
    set_role: tuple[str, ...] = (),
    agent_timeout_sec: int = 600,
    check_timeout_sec: int = 300,
) -> None:
    from team_harness.protocol.config import load_protocol_config
    from team_harness.protocol.mode_b import run_mode_b
    from team_harness.protocol.mode_c import TeamHarnessAgentRunner

    resolved_run_dir = Path(run_dir).resolve()
    resolved_repo = str(Path(repo).resolve())
    session_name = _resolve_visibility(
        visible=visible, tmux_session=tmux_session, task_id=task_id
    )
    runner = TeamHarnessAgentRunner(log_dir=resolved_run_dir, tmux_session=session_name)
    state = await run_mode_b(
        task_id=task_id,
        next_agent=next_agent,
        run_dir=resolved_run_dir,
        target_repo=resolved_repo,
        agent_runner=runner,
        protocol_config=load_protocol_config(
            runtime_roles=_parse_set_role(set_role), config_start_dir=resolved_repo
        ),
        agent_timeout_sec=agent_timeout_sec,
        check_timeout_sec=check_timeout_sec,
    )
    _print_protocol_state(state, run_dir=resolved_run_dir)
