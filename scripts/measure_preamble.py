#!/usr/bin/env python3
"""Measure coordinator preamble size: system prompt + tool schemas.

Prints character and estimated token counts (via tiktoken if available,
or chars/4 approximation).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile

from team_harness.agents.manager import AgentManager
from team_harness.config import Config
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.harness import _build_registry
from team_harness.tracking.run_log import RunLogWriter
from team_harness.ui.console import PlainConsole

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))

    TOKEN_METHOD = "tiktoken (cl100k_base)"
except ImportError:

    def count_tokens(text: str) -> int:
        return math.ceil(len(text) / 4)

    TOKEN_METHOD = "chars / 4 approximation"


def measure_preamble() -> dict[str, object]:
    config = Config()
    allowed_types = [
        "codex",
        "gemini",
        "claude",
        "grok",
        "antigravity",
        "openhands",
        "opencode",
        "pi",
        "harness",
    ]
    skills: list = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        session_output_dir = str(tmp_path / "sessions" / "test_session")

        # 1. System Prompt
        system_prompt = build_system_prompt(
            config=config,
            allowed_types=allowed_types,
            skills=skills,
            session_output_dir=session_output_dir,
        )
        prompt_chars = len(system_prompt)
        prompt_tokens = count_tokens(system_prompt)

        # 2. Tool Schemas
        manager = AgentManager()
        run_log = RunLogWriter(
            run_id="measure_run",
            run_dir=tmp_path,
            provider="mock",
            model="mock",
            api_base="http://localhost:8080",
            session_output_dir=session_output_dir,
        )
        ui = PlainConsole(ctx=None, manager=manager, run_dir=tmp_path)  # type: ignore[arg-type]
        registry = _build_registry(
            allowed_types=allowed_types,
            manager=manager,
            run_log=run_log,
            config=config,
            ui=ui,
            run_dir=tmp_path,
            session_output_dir=session_output_dir,
        )

        schemas = registry.get_all_schemas()
        schemas_json = json.dumps(schemas, indent=2, ensure_ascii=False)
        schemas_chars = len(schemas_json)
        schemas_tokens = count_tokens(schemas_json)

        # Breakdown per tool
        tool_breakdown = []
        for schema in schemas:
            tool_name = schema.get("function", {}).get("name", "unknown")
            t_json = json.dumps(schema, indent=2, ensure_ascii=False)
            t_chars = len(t_json)
            t_tokens = count_tokens(t_json)
            tool_breakdown.append(
                {"name": tool_name, "chars": t_chars, "tokens": t_tokens}
            )

        total_chars = prompt_chars + schemas_chars
        total_tokens = prompt_tokens + schemas_tokens

        return {
            "token_method": TOKEN_METHOD,
            "system_prompt": {"chars": prompt_chars, "tokens": prompt_tokens},
            "tool_schemas": {
                "chars": schemas_chars,
                "tokens": schemas_tokens,
                "count": len(schemas),
                "breakdown": tool_breakdown,
            },
            "total_preamble": {"chars": total_chars, "tokens": total_tokens},
        }


def main() -> None:
    res = measure_preamble()
    print("=" * 60)
    print(f"Preamble Measurement Report (Method: {res['token_method']})")
    print("=" * 60)
    sp = res["system_prompt"]
    ts = res["tool_schemas"]
    tp = res["total_preamble"]

    print("\n[System Prompt]")
    print(f"  Characters: {sp['chars']:,}")
    print(f"  Tokens:     {sp['tokens']:,}")

    print(f"\n[Tool Schemas] ({ts['count']} tools)")
    print(f"  Characters: {ts['chars']:,}")
    print(f"  Tokens:     {ts['tokens']:,}")
    print("  Per-tool breakdown:")
    for item in sorted(ts["breakdown"], key=lambda x: x["tokens"], reverse=True):
        print(
            f"    - {item['name']:<25} : {item['chars']:>6,} chars, {item['tokens']:>5,} tokens"
        )

    print("\n[Total Preamble]")
    print(f"  Characters: {tp['chars']:,}")
    print(f"  Tokens:     {tp['tokens']:,}")
    print("=" * 60)


if __name__ == "__main__":
    main()
