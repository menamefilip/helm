#!/usr/bin/env python3
"""Generate a symbol index with LINE NUMBERS for large source files.

Why this exists (M22, measured, not guessed):

In the 2026-08-18 sessions, 58 of 106 `terminal` calls were `grep`/`sed -n` used
purely to locate code, and `app/main.py` alone was accessed 41 times. Combined
with `read_file`, locating and re-reading code accounted for ~59 % of all tool
output entering the context window (~75k of ~123k tokens).

The cause was not the monolith itself but that `docs/FILE_INDEX.md`:
  1. was hand-written and 52 days stale (claimed 2279 lines for a 3438-line file
     -- understated by 51 %), and
  2. listed symbol NAMES but no LINE NUMBERS, so knowing a function existed did
     not tell you where to read. Hence: grep to find the line, then read.

This script emits `docs/CODE_INDEX.md`: every symbol with its line number and
span, so an agent can go straight to `read_file(path, offset=N, limit=M)` and
skip the grep entirely. It is generated, so it cannot silently rot.

Usage:
    python3 scripts/generate_code_index.py            # write docs/CODE_INDEX.md
    python3 scripts/generate_code_index.py --check    # CI: fail if stale
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "CODE_INDEX.md"

# Only index files big enough that locating code is a real cost.
MIN_LINES = 400

PY_GLOBS = ["app/*.py", "scripts/*.py"]
JS_GLOBS = ["shared-ui/*.js"]

# JS: module-level declarations only. Indentation is the discriminator, and the
# limit is derived from the real file, not guessed: in shared-ui/app.js (an IIFE,
# so "top level" is 2 spaces) all 132 function declarations sit at indent 2,
# while const/let/var appear at 2 (41x) and at 4/6/8/10 (374x -- locals).
# Matching any indentation indexed locals like
# `    const command = (raw || '').toLowerCase();` as navigable symbols, which
# made ordinary English words in a prompt resolve to an offset. Requiring indent
# 0 was the opposite error and dropped every real symbol.
# Must stay in step with app/workspace.py -- pinned by tests/test_symbol_index.py.
JS_MAX_INDENT = 2
JS_FN = re.compile(
    r"^(?P<indent> *)(?:export\s+)?(?:async\s+)?"
    r"(?:function\s+(?P<fn>\w+)"
    r"|(?:const|let|var)\s+(?P<var>\w+)\s*=\s*(?:async\s*)?(?:\(|function\b))",
)


def js_symbol_name(line: str) -> str | None:
    """Return a module-level JS symbol name, or None for locals."""
    match = JS_FN.match(line)
    if not match:
        return None
    if len(match.group("indent")) > JS_MAX_INDENT:
        return None
    return match.group("fn") or match.group("var")


def python_symbols(path: Path) -> list[tuple[int, int, str, str]]:
    """Return (lineno, end_lineno, kind, name) for defs/classes."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out: list[tuple[int, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            out.append((node.lineno, end, kind, node.name))
    return sorted(out)


def js_symbols(path: Path) -> list[tuple[int, int, str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        name = js_symbol_name(line)
        if name:
            hits.append((i, name))
    out: list[tuple[int, int, str, str]] = []
    for idx, (lineno, name) in enumerate(hits):
        end = hits[idx + 1][0] - 1 if idx + 1 < len(hits) else len(lines)
        out.append((lineno, end, "function", name))
    return out


def collect() -> list[dict]:
    files: list[Path] = []
    for pattern in PY_GLOBS + JS_GLOBS:
        files.extend(sorted(REPO.glob(pattern)))

    report: list[dict] = []
    for path in files:
        if not path.is_file():
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count < MIN_LINES:
            continue
        syms = python_symbols(path) if path.suffix == ".py" else js_symbols(path)
        report.append({"path": path.relative_to(REPO).as_posix(), "lines": line_count, "symbols": syms})
    return report


def render(report: list[dict]) -> str:
    out: list[str] = [
        "# Code Index (generated)",
        "",
        "**Do not hand-edit.** Regenerate with `python3 scripts/generate_code_index.py`.",
        "",
        "Every symbol below carries its line span, so you can read it directly:",
        "",
        "```",
        "read_file(path, offset=<line>, limit=<span>)",
        "```",
        "",
        "Use this instead of `grep`/`sed -n` to locate code. Only files with",
        f"{MIN_LINES}+ lines are indexed -- smaller files are cheap to read whole.",
        "",
    ]
    for entry in report:
        out.append(f"## `{entry['path']}` — {entry['lines']:,} lines")
        out.append("")
        if not entry["symbols"]:
            out.append("_No top-level symbols detected._")
            out.append("")
            continue
        for lineno, end, kind, name in entry["symbols"]:
            span = max(1, end - lineno + 1)
            out.append(f"- `{name}` — {kind}, line {lineno}, span {span}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed index is stale")
    args = parser.parse_args()

    content = render(collect())

    if args.check:
        if not OUT.exists():
            print(f"MISSING: {OUT.relative_to(REPO)} — run scripts/generate_code_index.py")
            return 1
        if OUT.read_text(encoding="utf-8") != content:
            print(f"STALE: {OUT.relative_to(REPO)} — run scripts/generate_code_index.py")
            return 1
        print(f"OK: {OUT.relative_to(REPO)} is current")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    total = sum(len(e["symbols"]) for e in collect())
    print(f"Wrote {OUT.relative_to(REPO)}: {len(collect())} files, {total} symbols")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
