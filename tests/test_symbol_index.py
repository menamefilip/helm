"""Tests for prompt-targeted symbol lookup (M22).

Origin: a scan of the 2026-08-18 sessions found that *locating* code, not
changing it, dominated token spend -- 58 of 106 `terminal` calls were
`grep`/`sed -n` used purely to find where a symbol lived, and `app/main.py`
alone was touched 41 times. Together with `read_file`, code lookup accounted for
~59 % of all tool output entering context (~75k of ~123k tokens).

Root cause was not the monolith: `docs/FILE_INDEX.md` was hand-written, 52 days
stale (claimed 2279 lines for a 3438-line file) and listed symbol *names*
without *line numbers*, so knowing a symbol existed did not say where to read it.

First attempt at a fix was wrong and this test suite exists partly to stop it
recurring: injecting the whole generated index as a 7th project map doc failed,
because map docs are capped at 2500 chars -- only 11 % of the 21,922-char index
survived and `shared-ui/app.js` fell off entirely. A static doc cannot carry 430
symbols within the budget.

Correct shape: select symbols relevant to *this prompt*, the same way
`load_retrieved_context_chunks` selects chunks. A handful of lines with exact
offsets replaces a grep round-trip.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.workspace import (
    SYMBOL_INDEX_GLOBS,
    SYMBOL_INDEX_MIN_LINES,
    SYMBOL_MATCH_MIN_SCORE,
    _JS_MAX_SYMBOL_INDENT,
    _js_symbol_name,
    discover_symbol_index_paths,
    load_symbol_index,
    render_symbol_index,
)


def _make_repo(tmp_path: Path) -> Path:
    """Minimal repo with a Python and a JS file, mirroring the real layout."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text(
        "\n".join(
            [
                "import os",
                "",
                "",
                "def _auto_run_summary(conn, run, result):",
                "    return None",
                "",
                "",
                "def _assess_run_completion(conn, run):",
                "    return 'review'",
                "",
                "",
                "class RunCreate:",
                "    pass",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ui = tmp_path / "shared-ui"
    ui.mkdir()
    (ui / "app.js").write_text(
        "\n".join(
            [
                "  async function renderFolderTree(selectPath) {",
                "    return 1;",
                "  }",
                "",
                "  function folderTreeRow(entry) {",
                "    return 2;",
                "  }",
            ]
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_symbol_lookup_finds_python_symbol_with_line_number(tmp_path):
    """The whole point: return an exact offset so grep is unnecessary."""
    root = _make_repo(tmp_path)
    hits = load_symbol_index(root, "oprav _auto_run_summary aby nefabrikoval changes")

    names = {hit["name"] for hit in hits}
    assert "_auto_run_summary" in names, f"target symbol not found, got {names}"

    hit = next(h for h in hits if h["name"] == "_auto_run_summary")
    assert hit["line"] == 4, f"expected line 4, got {hit['line']}"
    assert hit["path"] == "app/main.py"
    assert int(hit["span"]) >= 1


def test_symbol_lookup_finds_js_symbol(tmp_path):
    """shared-ui/app.js must be covered -- it fell off entirely in the first attempt."""
    root = _make_repo(tmp_path)
    hits = load_symbol_index(root, "uprav renderFolderTree v lavom paneli")

    names = {hit["name"] for hit in hits}
    assert "renderFolderTree" in names, f"JS symbol not found, got {names}"
    hit = next(h for h in hits if h["name"] == "renderFolderTree")
    assert hit["path"] == "shared-ui/app.js"
    assert hit["line"] == 1


def test_symbol_lookup_is_bounded(tmp_path):
    """Must stay small: this replaces grep, it must not become a second bloat source."""
    root = _make_repo(tmp_path)
    hits = load_symbol_index(root, "run summary completion folder tree render row create", max_symbols=3)
    assert len(hits) <= 3, f"lookup returned {len(hits)} symbols, cap not honoured"


def test_symbol_lookup_returns_nothing_for_unrelated_prompt(tmp_path):
    """An informational prompt must not drag code into context."""
    root = _make_repo(tmp_path)
    assert load_symbol_index(root, "ahoj ako sa mas") == []


def test_render_symbol_index_gives_actionable_read_instruction(tmp_path):
    """Rendered form must tell the agent exactly how to read, not just what exists."""
    root = _make_repo(tmp_path)
    hits = load_symbol_index(root, "_auto_run_summary")
    text = render_symbol_index(hits)

    assert "_auto_run_summary" in text
    assert "app/main.py" in text
    assert "offset=4" in text, f"no actionable offset in rendered output:\n{text}"
    assert "read_file" in text


def test_render_symbol_index_empty_is_empty_string(tmp_path):
    """No hits must cost zero tokens, not an empty header."""
    assert render_symbol_index([]) == ""


# --- Precision gate (measured, do not loosen without re-measuring) ----------
#
# Replaying 149 real lookup calls from the session DB
# (scripts/measure_symbol_index.py) showed the first cut of this feature was a
# no-op: +0.2 % net. Substring matching charged 46 of 62 prompts while
# answering the *same* 22 lookups that exact matching answered for 1/10th the
# cost.
#
#   min_score=4  (substring)  ->  46/62 prompts charged, net  +1,019 B
#   min_score=10 (exact only) ->  14/62 prompts charged, net +37,558 B
#
# Recall is capped at 26 % regardless (scripts/diagnose_symbol_index.py): most
# lookups hunt symbols the user prompt never names, so recall cannot be tuned
# up. Precision is the only real lever. These tests lock that in.


def test_partial_word_match_does_not_emit_symbols(tmp_path):
    """Substring hits are pure cost: measured to pay for 3x the prompts at equal recall.

    'run' appears inside `_auto_run_summary` and `_assess_run_completion`, but a
    prompt merely containing 'run' is not asking about either.
    """
    root = _make_repo(tmp_path)
    hits = load_symbol_index(root, "pozri sa na ten run a povedz co sa stalo")
    assert hits == [], (
        "substring matching re-enabled: this was measured as a net no-op "
        f"(+0.2%). Got {[h['name'] for h in hits]}"
    )


def test_exact_symbol_mention_still_matches(tmp_path):
    """The precision gate must not break the case the feature exists for."""
    root = _make_repo(tmp_path)
    hits = load_symbol_index(root, "oprav _assess_run_completion nech vracia needs_attention")
    assert "_assess_run_completion" in {h["name"] for h in hits}


def test_scores_are_exact_match_strength(tmp_path):
    """Every emitted hit must clear the exact-match bar, so cost tracks real signal."""
    root = _make_repo(tmp_path)
    hits = load_symbol_index(root, "_auto_run_summary a renderFolderTree")
    assert hits, "expected exact matches"
    for hit in hits:
        assert int(str(hit["score"])) >= SYMBOL_MATCH_MIN_SCORE, (
            f"{hit['name']} emitted with weak score {hit['score']}"
        )


def test_prompt_naming_no_real_symbol_costs_nothing(tmp_path):
    """Technical-sounding prompts that name no symbol must stay free."""
    root = _make_repo(tmp_path)
    assert load_symbol_index(root, "dorob backend a otestuj to poriadne") == []


def test_local_variables_are_not_indexed_as_symbols(tmp_path):
    """Indented `const x = ...` is a local variable, not a navigable symbol.

    Found while building a demo of the measured saving: the JS regex accepted any
    indentation, so `    const command = (rawCommand || '').toLowerCase();` inside
    a function was indexed as a top-level symbol named `command`. Common English
    words then matched it -- a Hermes notification containing the word "command"
    pulled an offset into the prompt. That is noise indistinguishable from the
    grep noise this feature exists to remove, and it inflated the measured saving.
    """
    root = tmp_path
    ui = root / "shared-ui"
    ui.mkdir()
    (ui / "app.js").write_text(
        "\n".join(
            [
                "function handleSlashCommand(rawCommand) {",
                "    const command = (rawCommand || '').toLowerCase();",
                "    let result = null;",
                "    return command;",
                "}",
                "",
                "const exportedHelper = (a) => a + 1;",
            ]
            + ["// pad"] * 450
        ),
        encoding="utf-8",
    )

    names = {h["name"] for h in load_symbol_index(root, "oprav command a result")}
    assert "command" not in names, "indexed a function-local variable as a symbol"
    assert "result" not in names, "indexed a function-local variable as a symbol"

    # Top-level declarations remain navigable.
    top = {h["name"] for h in load_symbol_index(root, "uprav exportedHelper")}
    assert "exportedHelper" in top, f"lost a real top-level symbol, got {top}"


def test_generator_also_excludes_local_variables():
    """Runtime and generated doc must agree, so the doc must exclude locals too."""
    content = (Path(__file__).resolve().parents[1] / "docs" / "CODE_INDEX.md").read_text(
        encoding="utf-8"
    )
    for noisy in ("`command` —", "`response` —", "`payload` —"):
        assert noisy not in content, (
            f"generated index still lists local variable {noisy!r}"
        )


# --- Zero-maintenance guarantees --------------------------------------------
#
# The failure this whole feature replaced was a *stale hand-written index*:
# docs/FILE_INDEX.md sat 52 days out of date and understated app/main.py by
# 51 %. A hardcoded path list in the runtime would rot exactly the same way --
# a new 2000-line module would be silently invisible, with nothing failing.
# These tests ensure discovery stays automatic and stays in step with the
# generator.


def test_discovery_picks_up_a_new_large_file_without_code_changes(tmp_path):
    """A newly added large module must be indexed with no edit to any path list."""
    root = _make_repo(tmp_path)
    big = root / "app" / "brand_new_module.py"
    big.write_text(
        "\n".join(["def freshly_added_symbol():", "    pass", ""] + ["# pad"] * 500),
        encoding="utf-8",
    )

    assert "app/brand_new_module.py" in discover_symbol_index_paths(root), (
        "discovery missed a new large file -- the path list has gone stale, "
        "which is the exact failure mode this feature replaced"
    )
    hits = load_symbol_index(root, "oprav freshly_added_symbol")
    assert "freshly_added_symbol" in {h["name"] for h in hits}


def test_discovery_ignores_small_files(tmp_path):
    """Small files are cheap to read whole; indexing them is wasted budget."""
    root = _make_repo(tmp_path)
    (root / "app" / "tiny.py").write_text("def tiny():\n    pass\n", encoding="utf-8")
    assert "app/tiny.py" not in discover_symbol_index_paths(root)


def test_runtime_and_generator_share_discovery_rules():
    """Runtime and generated doc must not disagree about what is worth indexing.

    If these drift, docs/CODE_INDEX.md documents one set of files while the agent
    is fed another -- stale-index rot with extra steps.
    """
    spec = importlib.util.spec_from_file_location(
        "_gen_code_index",
        Path(__file__).resolve().parents[1] / "scripts" / "generate_code_index.py",
    )
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    assert gen.MIN_LINES == SYMBOL_INDEX_MIN_LINES, (
        f"MIN_LINES drift: generator={gen.MIN_LINES} "
        f"runtime={SYMBOL_INDEX_MIN_LINES}"
    )
    assert sorted(gen.PY_GLOBS + gen.JS_GLOBS) == sorted(SYMBOL_INDEX_GLOBS), (
        f"glob drift: generator={sorted(gen.PY_GLOBS + gen.JS_GLOBS)} "
        f"runtime={sorted(SYMBOL_INDEX_GLOBS)}"
    )
    assert gen.JS_MAX_INDENT == _JS_MAX_SYMBOL_INDENT, (
        f"JS indent limit drift: generator={gen.JS_MAX_INDENT} "
        f"runtime={_JS_MAX_SYMBOL_INDENT} -- the two would index different symbols"
    )
    # Same input must classify identically in both implementations.
    for line, expected in [
        ("  function renderFolderTree(selectPath) {", "renderFolderTree"),
        ("    const command = (raw || '').toLowerCase();", None),
        ("      let result = null;", None),
        ("  const helper = (a) => a + 1;", "helper"),
    ]:
        assert gen.js_symbol_name(line) == expected, f"generator: {line!r}"
        assert _js_symbol_name(line) == expected, f"runtime: {line!r}"


def test_discovery_on_real_repo_covers_the_known_hot_files():
    """Regression: app/main.py and shared-ui/app.js are the actual grep magnets."""
    root = Path(__file__).resolve().parents[1]
    found = discover_symbol_index_paths(root)
    for rel in ("app/main.py", "shared-ui/app.js"):
        assert rel in found, f"{rel} not discovered, got {found}"
