#!/usr/bin/env python3
"""Prompt-injection / slop guard — first-line static filter for PR content.

Screens changed files for text that could hijack an AI reviewer or a downstream
agent (prompt injection), for hidden/deceptive unicode used to smuggle
instructions, and for low-effort AI "slop." Designed to run in CI on PR diffs
(see ``.github/workflows/pr-guard.yml``) *before* any LLM-based review — so a
malicious PR can't quietly steer the very model reviewing it.

Heuristic, not proof: it errs toward flagging. A maintainer waves through false
positives with the ``guard:ok`` label. Exit code 1 => findings => block + human.

Usage:
    scan_injection.py <file-or-dir> [<file-or-dir> ...]
    git diff --name-only origin/dev...HEAD | xargs scan_injection.py
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

# --- 1. classic prompt-injection phrasing (case-insensitive) -----------------
INJECTION_PATTERNS: list[str] = [
    r"ignore\s+(all\s+|the\s+)?(previous|prior|above|preceding)\s+instructions",
    r"disregard\s+(the\s+)?(previous|prior|above|system|preceding)",
    r"forget\s+(everything|all\s+previous|your\s+instructions|the\s+above)",
    r"you\s+are\s+now\s+(a|an|the)\b",
    r"\bsystem\s*prompt\b",
    r"\bdeveloper\s*(message|prompt)\b",
    r"new\s+(instructions|task|role|persona)\s*:",
    r"do\s+not\s+(tell|inform|alert)\s+(the\s+)?(user|reviewer|human|maintainer)",
    r"</?(system|assistant|user|tool|function)\s*>",  # fake chat-role tags
    r"\bBEGIN\s+SYSTEM\b|\bEND\s+SYSTEM\b",
    r"(print|reveal|leak|output)\s+your\s+(system\s+)?(prompt|instructions|rules)",
    r"approve\s+this\s+(pr|pull\s+request|change)\s+(without|regardless)",
    r"(exfiltrat|reverse\s+shell)",
    r"curl\s+[^\n|]*\|\s*(sh|bash|zsh)",  # pipe-to-shell payload smell
]

# --- 2. invisible / deceptive unicode ----------------------------------------
INVISIBLE_CHARS: frozenset[str] = frozenset(
    {
        "​",
        "‌",
        "‍",
        "⁠",
        "﻿",  # zero-width
        "‪",
        "‫",
        "‬",
        "‭",
        "‮",  # bidi overrides
        "⁦",
        "⁧",
        "⁨",
        "⁩",  # bidi isolates
    }
)


def _has_unicode_tags(s: str) -> bool:
    """Unicode Tags block (U+E0000..U+E007F) — used to smuggle hidden ASCII."""
    return any(0xE0000 <= ord(c) <= 0xE007F for c in s)


# --- 3. slop smells (low-effort AI output) -----------------------------------
SLOP_PATTERNS: list[str] = [
    r"as\s+an\s+ai\s+(language\s+)?model",
    r"\bTODO\b[^\n]*\b(implement\s+this|fill\s+in|your\s+code\s+here)\b",
    r"[#/]+\s*\.\.\.\s*(rest\s+of\s+)?(existing\s+|your\s+|the\s+)?code\b",
    r"placeholder\s+implementation",
    r"i\s+cannot\s+(assist|help)\s+with\s+that",
    r"here('|)s\s+(a|the)\s+(complete|full|updated)\s+(code|implementation)\s*:",
]

# scan text files only; skip these
SKIP_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2"}
)
# Directories that are never OUR content. CI passes an explicit changed-file
# list so it never walks these, but a contributor running `scan_injection.py .`
# would otherwise get a screenful of findings from their dependencies -- and a
# guard that cries wolf is a guard people stop reading.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "site-packages",
        "dist",
        "build",
        ".eggs",
    }
)
# our own detector strings would otherwise self-flag
SELF_EXEMPT: frozenset[str] = frozenset(
    {"scan_injection.py", "test_scan_injection.py", "CONTRIBUTING.md"}
)

_INJ = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
_SLOP = [re.compile(p, re.IGNORECASE) for p in SLOP_PATTERNS]


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    kind: str
    detail: str


def _skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def _iter_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(f for f in path.rglob("*") if f.is_file() and not _skipped(f))
        elif path.is_file():
            out.append(path)
    return [f for f in out if f.suffix.lower() not in SKIP_SUFFIXES]


def scan_file(path: Path) -> list[Finding]:
    if path.name in SELF_EXEMPT:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable — not our concern

    findings: list[Finding] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for rx in _INJ:
            if rx.search(line):
                findings.append(Finding(str(path), i, "injection", rx.pattern[:60]))
        for rx in _SLOP:
            if rx.search(line):
                findings.append(Finding(str(path), i, "slop", rx.pattern[:60]))
        bad = INVISIBLE_CHARS.intersection(line)
        if bad:
            findings.append(
                Finding(str(path), i, "invisible", f"hidden chars: {[hex(ord(c)) for c in bad]}")
            )
        if _has_unicode_tags(line):
            findings.append(Finding(str(path), i, "invisible", "unicode-tags block (hidden ASCII)"))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="files or dirs to scan")
    args = ap.parse_args(argv)

    findings: list[Finding] = []
    for f in _iter_files(args.paths):
        findings.extend(scan_file(f))

    if not findings:
        print("guard: clean — no injection/slop/hidden-unicode findings")
        return 0

    print(f"guard: {len(findings)} finding(s) — blocking pending human review\n")
    for fd in findings:
        print(f"  [{fd.kind}] {fd.file}:{fd.line}  {fd.detail}")
    print("\nIf these are false positives, a maintainer applies the 'guard:ok' label.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
