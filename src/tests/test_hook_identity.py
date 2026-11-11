"""Installer identity checks preserve user commands mentioning managed names."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from team_harness.hook_identity import managed_hook


@pytest.mark.parametrize(
    "command",
    [
        "/managed/journal-hook.sh prompt",
        '"/managed/journal-hook.sh"',
        "bash /managed/journal-hook.sh prompt",
        "env python3 /managed/journal-hook.sh",
        "TH_HARNESS_SRC=/managed/src python3 /managed/journal-hook.sh",
    ],
)
def test_recognizes_actual_known_script_invocations(command: str):
    assert managed_hook(
        {"type": "command", "command": command},
        ["/managed/journal-hook.sh"],
        allowed_args=((), ("prompt",)),
        allowed_env={"TH_HARNESS_SRC": "/managed/src"},
    )


@pytest.mark.parametrize(
    "command",
    [
        '/managed/journal-hook.sh "$(printf side-effect)"',
        '/managed/journal-hook.sh "`printf side-effect`"',
        '/managed/journal-hook.sh "$((1+2))"',
        '/managed/journal-hook.sh "${VALUE:=side-effect}"',
        "SIDE_EFFECT=1 /managed/journal-hook.sh",
        "TH_HARNESS_SRC=/custom/src /managed/journal-hook.sh",
        "env -i /managed/journal-hook.sh",
        "python3 -u /managed/journal-hook.sh",
        r"/managed/journal-hook.sh \"prompt\"",
        r"/managed/journal-hook.sh pro\mpt",
        "/managed/journal-hook.sh --custom",
        "/custom/python3 /managed/journal-hook.sh",
        "/custom/bash /managed/journal-hook.sh",
        "/managed/journal-hook.sh prompt extra",
        "echo journal-hook.sh",
        "echo /managed/journal-hook.sh",
        "notify --script /managed/journal-hook.sh",
        "env X=value notify /managed/journal-hook.sh",
        "python3 /custom/notify.py /managed/journal-hook.sh",
        "python3 -c 'print(1)' /managed/journal-hook.sh",
        "python3 -m notify /managed/journal-hook.sh",
        "bash -c /managed/journal-hook.sh",
        "bash -lc /managed/journal-hook.sh",
        "bash -n /managed/journal-hook.sh",
        "bash -o noexec /managed/journal-hook.sh",
        "python3 -h /managed/journal-hook.sh",
        "python3 -V /managed/journal-hook.sh",
        "echo before; /managed/journal-hook.sh",
        "/managed/journal-hook.sh && echo after",
        "/managed/journal-hook.sh > /tmp/output",
        "/managed/journal-hook.sh\necho after",
        "/custom/journal-hook.sh",
        '"/managed/journal-hook.sh',
    ],
)
def test_preserves_arguments_custom_scripts_and_ambiguous_shell_programs(command: str):
    assert not managed_hook(
        {"type": "command", "command": command},
        ["/managed/journal-hook.sh"],
        allowed_args=((), ("prompt",)),
        allowed_env={"TH_HARNESS_SRC": "/managed/src"},
    )


def test_all_installers_preserve_user_hook_arguments_and_deduplicate_managed_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    journal = scripts / "journal-hook.sh"
    image = home / ".claude/hooks/image_guard.py"
    zone = home / ".claude/hooks/coordinator_zone_guard.py"
    user_commands = [
        "echo journal-hook.sh",
        "echo image_guard.py",
        "echo coordinator_zone_guard.py",
        f'notify --script "{journal}"',
        f'python3 /custom/notifier.py "{image}"',
        f'python3 -c \'print("ok")\' "{zone}"',
        f"bash -c 'python3 \"{image}\"'",
        f'echo before; "{journal}"',
        f'python3 "{zone}" && echo after',
        "python3 /custom/image_guard.py",
        "bash /custom/journal-hook.sh",
        "python3 /custom/coordinator_zone_guard.py",
        r"python3 \$HOME/.claude/hooks/image_guard.py",
        r"python3 \~/.claude/hooks/image_guard.py",
        r"python3 \$HOME/.claude/hooks/coordinator_zone_guard.py",
        r"python3 \~/.claude/hooks/coordinator_zone_guard.py",
        f'\\"{journal}\\"',
        f'"{journal}" pro\\mpt',
        *[
            command
            for script in (journal, image, zone)
            for command in (
                f'"{script}" "$(printf side-effect)"',
                f'"{script}" "`printf side-effect`"',
                f'"{script}" "$((1+2))"',
                f'"{script}" "${{VALUE:=side-effect}}"',
                f'CUSTOM=changed "{script}"',
                f'"{script}" --custom',
            )
        ],
    ]
    user_hooks = [
        {"type": "command", "command": command, "timeout": 17}
        for command in user_commands
    ]
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup|resume|compact",
                            "hooks": user_hooks
                            + [
                                {"type": "command", "command": f'"{journal}"'},
                                {"type": "command", "command": f'env bash "{journal}"'},
                            ],
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": user_hooks
                            + [
                                {"type": "command", "command": f'"{journal}" prompt'},
                                {
                                    "type": "command",
                                    "command": f'bash "{journal}" prompt',
                                },
                                {
                                    "type": "command",
                                    "command": "python3 ~/.claude/hooks/image_guard.py",
                                },
                            ]
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "Read|Bash",
                            "hooks": user_hooks
                            + [
                                {"type": "command", "command": f'python3 "{image}"'},
                                {
                                    "type": "command",
                                    "command": "python3 ~/.claude/hooks/block_image_read.py",
                                },
                                {"type": "command", "command": f'env python3 "{zone}"'},
                                {
                                    "type": "command",
                                    "command": f'TH_HARNESS_SRC="{scripts.parent / "src"}" python3 "{zone}"',
                                },
                            ],
                        },
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 ~/.claude/hooks/image_guard.py",
                                }
                            ],
                        },
                    ],
                }
            }
        )
    )
    codex = home / ".codex/hooks.json"
    codex.parent.mkdir(parents=True)
    codex.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": user_hooks}]}}))
    env = {
        **os.environ,
        "HOME": str(home),
        "TH_CLAUDE_HOOKS_DIR": str(home / ".claude/hooks"),
        "TH_CLAUDE_SETTINGS": str(settings),
        "TH_CODEX_HOOKS": str(codex),
        "TH_CODEX_HOOKS_BACKUP": str(home / ".codex/hooks.before-test.json"),
        "TH_INSTALL_TARGET_HOME": str(home),
    }
    previous = None
    for _ in range(2):
        for name in (
            "install-journal-rule.sh",
            "install-image-guard.sh",
            "install-coordinator-zone-guard.sh",
        ):
            subprocess.run(
                ["bash", str(scripts / name)],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
        data = json.loads(settings.read_text())["hooks"]
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
            actual = [hook for entry in data[event] for hook in entry["hooks"]]
            for hook in user_hooks:
                assert hook in actual, (event, hook)
        actual = [hook for entry in data["SessionStart"] for hook in entry["hooks"]]
        assert (
            sum(
                managed_hook(hook, [journal], allowed_args=((), ("prompt",)))
                for hook in actual
            )
            == 1
        )
        actual = [hook for entry in data["UserPromptSubmit"] for hook in entry["hooks"]]
        assert (
            sum(
                managed_hook(hook, [journal], allowed_args=((), ("prompt",)))
                for hook in actual
            )
            == 1
        )
        assert sum(managed_hook(hook, [image]) for hook in actual) == 1
        actual = [hook for entry in data["PreToolUse"] for hook in entry["hooks"]]
        assert sum(managed_hook(hook, [image]) for hook in actual) == 2
        assert (
            sum(
                managed_hook(
                    hook,
                    [zone],
                    allowed_env={"TH_HARNESS_SRC": str(scripts.parent / "src")},
                )
                for hook in actual
            )
            == 1
        )
        codex_hooks = [
            hook
            for entry in json.loads(codex.read_text())["hooks"]["PreToolUse"]
            for hook in entry["hooks"]
        ]
        assert all(hook in codex_hooks for hook in user_hooks)
        snapshot = settings.read_bytes(), codex.read_bytes()
        if previous is not None:
            assert snapshot == previous
        previous = snapshot


@pytest.mark.parametrize("prefix", ["~", "$HOME", "${HOME}"])
def test_standard_home_guard_paths_are_recognized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
):
    monkeypatch.setenv("HOME", str(tmp_path))
    guard = tmp_path / ".claude/hooks/image_guard.py"
    assert managed_hook(
        {
            "type": "command",
            "command": f"python3 {prefix}/.claude/hooks/image_guard.py",
        },
        [guard],
    )
    assert not managed_hook(
        {
            "type": "command",
            "command": f"python3 '{prefix}/.claude/hooks/image_guard.py'",
        },
        [guard],
    )


@pytest.mark.parametrize(
    "command",
    [
        r"python3 \$HOME/.claude/hooks/image_guard.py",
        r"python3 \${HOME}/.claude/hooks/image_guard.py",
        r"python3 \~/.claude/hooks/image_guard.py",
        r"python3 \"$HOME/.claude/hooks/image_guard.py\"",
        r'python3 "$HOME/.claude/hooks/image_guard.py" \"\"',
    ],
)
def test_escaped_home_paths_are_preserved_without_shell_escape_interpretation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not managed_hook(
        {"type": "command", "command": command},
        [tmp_path / ".claude/hooks/image_guard.py"],
    )
