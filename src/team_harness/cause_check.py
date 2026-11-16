"""Conservative detection of an actionable cause hypothesis in a brief."""

from __future__ import annotations

import re


def _visible_lines(text: str) -> list[str]:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
        if end:
            front = "\n".join(lines[1:end])
            if re.search(r"(?im)^cause_check\s*:\s*['\"]?off['\"]?\s*(?:#.*)?$", front):
                return []
            lines = lines[end + 1 :]
    visible = []
    fence = ""
    background = False
    for line in lines:
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            continue
        if fence or re.match(r"^\s*>", line):
            continue
        heading = re.match(r"^\s*#{1,2}\s+(.+?)\s*#*\s*$", line)
        if heading:
            background = bool(re.search(r"배경|이력|경위|과거", heading.group(1)))
        if not background:
            visible.append(line)
    return visible


def has_cause_hypothesis(text: str) -> bool:
    for line in _visible_lines(text):
        if re.match(r"^\s*##\s+원인\s*가설\s*#*\s*$", line):
            return True
        if re.search(r"(?:과거|이전|당시|예전|기존에는).*(?:원인은|원인\s*:)", line):
            continue
        if re.search(
            r"(?:^|\s|[-*])원인\s*:\s*\S|원인은\s+.{1,100}이다(?:[.。\s]|$)", line
        ):
            return True
    return False


def has_symptom_section(text: str) -> bool:
    return any(
        re.match(r"^\s*##\s+증상\s*재현\s*#*\s*$", line)
        for line in _visible_lines(text)
    )
