"""Normalize text for line-oriented event records."""

import sys
import unicodedata


def one_line(value: str, limit: int | None = None) -> str:
    lines = value.split("\n")
    result = " ⏎ ".join(
        "".join(
            " "
            if unicodedata.category(char).startswith("C") or char in "\u2028\u2029"
            else char
            for char in line
        )
        for line in lines
    )
    return result if limit is None else result[:limit]


if __name__ == "__main__":
    print(one_line(sys.argv[1]))
