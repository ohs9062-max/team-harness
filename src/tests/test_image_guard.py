"""Unit tests for the installed, stdlib-only Claude image guard hook."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "hooks" / "image_guard.py"


def _run_guard(
    tmp_path: Path, payload: dict[str, object], **env: str
) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path), **env},
    )
    return json.loads(result.stdout) if result.stdout else {}


def _prompt(session: str, text: str) -> dict[str, object]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "prompt": text,
    }


def _read(session: str, path: Path) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": "Read",
        "tool_input": {"file_path": str(path)},
    }


def _is_denied(result: dict[str, object]) -> bool:
    output = result.get("hookSpecificOutput", {})
    return isinstance(output, dict) and output.get("permissionDecision") == "deny"


def test_image_read_is_denied_without_a_user_permit(tmp_path: Path) -> None:
    assert _is_denied(_run_guard(tmp_path, _read("session-1", tmp_path / "chart.png")))


def test_explicit_path_permit_allows_only_that_path(tmp_path: Path) -> None:
    requested = tmp_path / "requested.png"
    other = tmp_path / "other.png"
    _run_guard(tmp_path, _prompt("session-1", f"{requested} 이미지를 읽어줘"))

    assert not _is_denied(_run_guard(tmp_path, _read("session-1", requested)))
    assert _is_denied(_run_guard(tmp_path, _read("session-1", other)))


def test_stop_removes_the_current_turn_permit(tmp_path: Path) -> None:
    path = tmp_path / "chart.png"
    _run_guard(tmp_path, _prompt("session-1", f"{path} 이미지를 확인해"))
    _run_guard(tmp_path, {"hook_event_name": "Stop", "session_id": "session-1"})

    assert _is_denied(_run_guard(tmp_path, _read("session-1", path)))


def test_image_cannot_be_reread_without_a_new_explicit_path_request(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chart.png"
    _run_guard(tmp_path, _prompt("session-1", f"{path} 이미지를 읽어줘"))
    assert not _is_denied(_run_guard(tmp_path, _read("session-1", path)))

    assert _is_denied(_run_guard(tmp_path, _read("session-1", path)))
    _run_guard(tmp_path, {"hook_event_name": "Stop", "session_id": "session-1"})
    _run_guard(tmp_path, _prompt("session-1", "이미지를 읽어줘"))
    assert _is_denied(_run_guard(tmp_path, _read("session-1", path)))


def test_unscoped_explicit_request_allows_at_most_five_new_images(
    tmp_path: Path,
) -> None:
    _run_guard(tmp_path, _prompt("session-1", "이미지를 읽어줘"))
    paths = [tmp_path / f"image-{index}.png" for index in range(6)]

    for path in paths[:5]:
        assert not _is_denied(_run_guard(tmp_path, _read("session-1", path)))
    assert _is_denied(_run_guard(tmp_path, _read("session-1", paths[5])))


def test_worker_image_override_allows_a_read(tmp_path: Path) -> None:
    assert not _is_denied(
        _run_guard(
            tmp_path, _read("session-1", tmp_path / "chart.png"), TH_ALLOW_IMAGES="1"
        )
    )


def test_bash_byte_reader_is_denied_without_a_permit(tmp_path: Path) -> None:
    path = tmp_path / "chart.png"
    result = _run_guard(
        tmp_path,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "tool_name": "Bash",
            "tool_input": {"command": f"base64 {path}"},
        },
    )
    assert _is_denied(result)


def test_non_image_files_always_pass(tmp_path: Path) -> None:
    assert not _is_denied(
        _run_guard(tmp_path, _read("session-1", tmp_path / "table.csv"))
    )


E = "." + "png"
J = "." + "jpg"
BLOCK = {
    "cat": f"cat /d/a{E}",
    "base64": f"base64 a{J}",
    "base64_in": f"base64 < a{E}",
    "xxd": f"xxd a{E} | head",
    "head_c": f"head -c 100 a{E}",
    "strings": f"strings 'a b{E}'",
    "chain": f"cd /d && cat a{E}",
    "pipe": f"echo x | cat a{E}",
    "subst": f"x=$(base64 a{E})",
    "backtick": f"x=`cat a{E}`",
    "sudo": f"sudo cat a{E}",
    "timeout": f"timeout 5 cat a{E}",
    "abs_bin": f"/bin/cat a{E}",
    "env": f"FOO=1 cat a{E}",
    "glob": f"cat *{E}",
    "newline": f"ls\ncat a{E}",
    "upper": f"cat A{E.upper()}",
    "py_c_open": f"python3 -c \"print(open('a{E}','rb').read())\"",
    "py_c_b64": f"python3 -c \"import base64;print(base64.b64encode(__import__('pathlib').Path('a{E}').read_bytes()))\"",
    "py_heredoc_open": f"python3 - <<'E'\nprint(open('a{E}','rb').read())\nE",
    "pipe_heredoc_py": f"cat <<'E' | python3\nprint(open('a{E}','rb').read())\nE",
    "node_e": f"node -e \"console.log(require('fs').readFileSync('a{E}'))\"",
    "unbalanced_quote": f"cat 'a{E}",
    "pipe_nospace": f"cat a{E}|head",
    "amp_nospace": f"cat a{E}&&ls",
    "semi_nospace": f"cat a{E};ls",
    "redirect_nospace": f"cat a{E}>/dev/null",
    "var_reader": f"IMG=a{E}; base64 $IMG",
    "var_quoted": f'IMG="a{J}"; cat "$IMG"',
    "for_loop": f"for f in a{E} b{E}; do base64 $f; done",
    "cmd_subst_arg": f"base64 $(ls *{E})",
    "py_path_read_bytes": f"python3 -c \"from pathlib import Path;print(Path('a{E}').read_bytes())\"",
    "after_heredoc": f"cat > x.py <<'E'\nprint(1)\nE\ncat a{E}",
}
ALLOW = {
    "heredoc_script": f"cat > x.py <<'E'\nimport cv2\ncv2.imwrite('out{E}', img)\nE",
    "heredoc_read_code": f"cat > x.py <<'E'\nimg = cv2.imread('in{J}')\nprint(open('a{E}','rb'))\nE",
    "four_scripts": "\n".join(
        f"cat > s{i}.py <<'E'\nsave('o{i}{E}')\nE" for i in range(4)
    ),
    "heredoc_tab": f"cat > x.py <<-E\n\tsave('o{E}')\n\tE",
    "heredoc_append": f"cat >> x.py <<\"E\"\nsave('o{E}')\nE",
    "tee_heredoc": f"tee x.py <<'E'\nsave('o{E}')\nE",
    "py_out_arg": f"python3 train.py --out result{E}",
    "py_in_arg": f"python3 infer.py --input a{J} --save b{J}",
    "py_chain": f"cd /d && python3 draw.py a{E} 2>&1 | tail -5",
    "uv_run": f"uv run python draw.py --out o{E}",
    "redirect_out": f"curl -s http://x > o{E}",
    "cat_txt_to_img": f"cat parts.bin > o{E}",
    "ls": f"ls /d/a{E}",
    "wget": f"wget -O a{E} http://x",
    "cp": f"cp a{E} b{E}",
    "mv": f"mv a{E} b{E}",
    "grep": f"grep -c '{E}' list.txt",
    "echo": f"echo a{E}",
    "git_add": f"git add a{E}",
    "py_c_save": f"python3 -c \"import cv2;cv2.imwrite('o{E}', x)\"",
    "py_heredoc_save": f"python3 - <<'E'\nplt.savefig('o{E}')\nE",
    "py_patch_script": f"python3 - <<'E'\np='draw.py'; s=open(p).read()\ns=s.replace('a{E}','b{E}'); open(p,'w').write(s)\nE",
    "py_c_imread_shape": f"python3 -c \"import cv2;print(cv2.imread('a{E}').shape)\"",
    "py_c_pil_size": f"python3 -c \"from PIL import Image;print(Image.open('a{J}').size)\"",
    "cat_var_md": f"ls a{E}; cat $D/report.md",
    "no_image": "cat x.py",
    "tail_log": "tail -5 run.log",
}


@pytest.mark.parametrize("command", list(BLOCK.values()), ids=list(BLOCK))
def test_bash_image_read_cases_are_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.delenv("TH_ALLOW_IMAGES", raising=False)
    result = _run_guard(
        tmp_path,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
    )
    assert _is_denied(result)


@pytest.mark.parametrize("command", list(ALLOW.values()), ids=list(ALLOW))
def test_bash_non_read_cases_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.delenv("TH_ALLOW_IMAGES", raising=False)
    result = _run_guard(
        tmp_path,
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
    )
    assert not _is_denied(result)
