from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
from typing import Iterable

READABLE_ARTIFACTS = {"input.md", "run-contract.md", "plan.md", "output.md", "review.md", "events.jsonl"}
WRITABLE_ARTIFACTS = {"input.md", "run-contract.md", "plan.md", "output.md", "review.md"}
SAFE_ARTIFACTS = READABLE_ARTIFACTS
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_CONTRACT_PATHS = [
    PROJECT_ROOT / "system" / "contracts" / "session-health.md",
]

POLICY_FILES = {
    "system/identity.md": "# Helm Identity\n\nHelm is a local-first project workspace. Its assistant runtime is configurable.\n",
    "system/policies/external-actions.md": "# External Actions Policy\n\n- Require explicit user confirmation before external messages, publishing, account changes, or irreversible actions.\n- Local reads, tests, and scoped edits may proceed without external side effects.\n",
    "system/policies/communication.md": "# Communication Policy\n\n- Be concise, direct, and respectful.\n- Match the user's language and distinguish verified facts from assumptions.\n",
    "system/policies/adaptive-router.md": "# Task Routing\n\n- Handle small, well-scoped tasks directly.\n- For multi-step or high-impact work, plan, verify prerequisites, and use an independent review when useful.\n- Keep execution effort proportional to complexity and risk.\n",
    "system/policies/dox-lite.md": "# Documentation Policy\n\n- Read the nearest owning documentation before changing a subsystem.\n- Update the smallest relevant docs and tests alongside code changes.\n- Prefer compact, verifiable project maps over duplicated notes or chat transcripts.\n",
    "system/policies/ui-architecture.md": "# Helm UI Architecture Policy\n\n- The center workspace holds the user-facing chat and project views. Do not duplicate chat inside the Agent Console.\n- The Agent Console shows run state, runtime/tool activity, context, artifacts, verification, and debugging details.\n- Keep the Basic console compact; Expert mode may expose deeper diagnostics.\n",
    "system/policies/environment-routing.md": "# Environment Routing Policy\n\n- Keep development and stable environments separate.\n- Make code changes in a development checkout; promote verified commits through a guarded release process.\n- Runtime dispatch sends the rendered `input.md` bundle to the configured agent process.\n- Set `HELM_RUNTIME_COMMAND` to choose a local Hermes-compatible runtime.\n- External sends, publishing, real accounts, or user-data deletion require explicit user approval.\n",
    "system/policies/run-contract.md": "# Run Contract Policy\n\nFor non-trivial tasks, create or update `run-contract.md` before execution. Include the goal, non-goals, scope, verification, stop conditions, and follow-up. Treat the contract as an editable artifact, not a replacement for the user's request.\n",
    "system/policies/milestone-context-reset.md": "# Milestone Context Reset Policy\n\nAfter completing a substantial project milestone, refresh the architecture, file/API maps, roadmap, and verification status before beginning the next one. Keep the records compact and accurate.\n",
    "system/policies/autonomy-lanes.md": "# Autonomy Lanes Policy\n\nGreen lane: local reads, tests, snapshots, localhost smoke, and approved local edits. Yellow lane: larger edits, development database changes, and runtime/policy changes; report evidence. Red lane: external sends, publishing, real accounts, user-data deletion, and identity changes require explicit user approval.\n",
    "system/policies/knowledge-routing.md": "# Knowledge Routing Policy\n\nStore only stable user or environment facts in memory. Skills hold reusable workflows; policy docs hold durable rules; handoffs hold temporary task state; project docs hold project-specific context.\n",
    "system/failure-patterns.md": "# Failure Patterns Log\n\nUse this file for recurring or severe assistant failures, not ordinary task logs.\n\n## Template\n\n### Pattern\n- What failed:\n- Trigger:\n- Impact:\n- Fix:\n- Enforced in: skill / policy / test / UI gate\n\n## Active Patterns\n\n- Unsupported state claim: verify file and test state with tools before reporting it.\n",
    "system/stages/daily-plan.md": "# Stage Contract: Daily Plan\n\n## Rules\n- Focus on user-stated priorities and commitments.\n- Keep check-ins concise and avoid collecting irrelevant sensitive details.\n- Connect daily tasks with larger goals.\n\n## Outputs\n- plan.md\n- next-actions.md\n",
    "system/stages/project-build.md": "# Stage Contract: Project Build\n\n## Rules\n- Inspect the current project state before editing.\n- Add regression tests for behavior changes.\n- Require approval for external actions.\n\n## Outputs\n- plan.md\n- output.md\n- review.md\n",
    "system/stages/code-review.md": "# Stage Contract: Code Review\n\n## Rules\n- Check security, regressions, tests, and maintainability.\n- Report concrete findings with file paths and evidence.\n\n## Outputs\n- review.md\n",
    "system/stages/weekly-review.md": "# Stage Contract: Weekly Review\n\n## Rules\n- Compare completed work with the user's stated goals.\n- Recommend a small set of clear next priorities.\n\n## Outputs\n- weekly-review.md\n- next-week-plan.md\n",
}

DOMAIN_CONTEXTS = {
    "school": "# Learning Context\n\nCoursework, study notes, and project-based learning.\n",
    "ai-it": "# Engineering Context\n\nSoftware projects, experiments, and technical learning.\n",
    "sport": "# Training Context\n\nTraining sessions, progress notes, and recovery planning.\n",
    "life": "# Personal Planning Context\n\nGoals, daily planning, and personal administration.\n",
}


def bootstrap_workspace(root: str | Path) -> Path:
    root = Path(root).expanduser()
    for rel, content in POLICY_FILES.items():
        ensure_file(root / rel, content)
    for domain, content in DOMAIN_CONTEXTS.items():
        base = root / "domains" / domain
        ensure_file(base / "CONTEXT.md", content)
        (base / "active").mkdir(parents=True, exist_ok=True)
        (base / "outputs").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    return root


def ensure_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def workspace_tree(root: str | Path) -> list[dict[str, str]]:
    root = bootstrap_workspace(root)
    items: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        items.append({"path": rel, "type": "dir" if path.is_dir() else "file"})
    return items


def create_project_workspace(root: str | Path, slug: str, name: str, mode: str) -> Path:
    root = bootstrap_workspace(root)
    domain = {
        "learning": "school",
        "work": "ai-it",
        "sideproject": "ai-it",
    }.get(mode, "ai-it")
    project_dir = root / "domains" / domain / "active" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    ensure_file(
        project_dir / "CONTEXT.md",
        f"# {name}\n\nMode: {mode}\n\nTento workspace bol vytvorený z Helmu.\n",
    )
    ensure_file(project_dir / "README.md", f"# {name}\n\nAktívny pracovný priečinok pre Helm.\n")
    return project_dir


def render_recent_run_summaries(summaries: list[dict[str, object]], limit: int = 3) -> str:
    """Render recent auto-generated run summaries (M15a) as fresh project context.

    This replaces hand-maintained "current milestone" prose that always goes
    stale: summaries are produced automatically at the end of every run.
    """
    if not summaries:
        return ""
    lines = ["\n## Recent run summaries\n"]
    for item in summaries[:limit]:
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        run_id = item.get("run_id")
        head = summary.splitlines()[0][:200]
        lines.append(f"- **run {run_id}**: {head}")
        tests = str(item.get("tests") or "").strip()
        if tests:
            lines.append(f"  - tests: {tests[:120]}")
        next_actions = str(item.get("next_actions") or "").strip()
        if next_actions:
            lines.append(f"  - next: {next_actions[:160]}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def _trim_to_budget(text: str, budget_tokens: int) -> tuple[str, bool]:
    """Hard cap on rendered context. Returns (text, was_trimmed)."""
    limit_chars = max(0, budget_tokens) * 4
    if len(text) <= limit_chars:
        return text, False
    cut = text[:limit_chars]
    boundary = cut.rfind("\n## ")
    if boundary > limit_chars // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "\n\n_[context trimmed to fit tier budget]_\n", True


def create_run_artifacts(
    root: str | Path,
    run_id: int,
    prompt: str,
    mode: str,
    project_context: str = "",
    project_map_docs: list[dict[str, object]] | None = None,
    retrieved_context_chunks: list[dict[str, object]] | None = None,
    symbol_index: list[dict[str, object]] | None = None,
    workflow_policies: list[dict[str, str]] | None = None,
    runtime_contracts: list[dict[str, str]] | None = None,
    context_tier: str | None = None,
    recent_run_summaries: list[dict[str, object]] | None = None,
) -> Path:
    root = bootstrap_workspace(root)
    slug = slugify(prompt) or "run"
    date = datetime.now().strftime("%Y-%m-%d")
    run_dir = root / "runs" / f"{date}-{run_id:04d}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=True)

    decision = classify_context_tier(prompt, override=context_tier)
    tier = str(decision["tier"])
    budget = int(decision["budget_tokens"])  # type: ignore[arg-type]

    # Which sections each tier is allowed to include. Retrieved chunks are
    # prompt-targeted (highest value per token) so they join at M, while bulky
    # static navigation/workflow docs are the first thing trimming discards.
    include_project_ctx = tier != TIER_XS
    include_summaries = tier != TIER_XS
    include_contracts = tier in {TIER_S, TIER_M, TIER_L}
    include_retrieved = tier in {TIER_M, TIER_L}
    include_navigation = tier in {TIER_M, TIER_L}
    include_workflow = tier == TIER_L

    context_section = (
        f"\n## Project context\n\n{project_context}\n" if project_context and include_project_ctx else ""
    )
    summaries_section = (
        render_recent_run_summaries(recent_run_summaries or []) if include_summaries else ""
    )
    navigation_section = (
        render_context_navigation(
            project_map_docs if project_map_docs is not None else load_project_map_docs(root)
        )
        if include_navigation
        else ""
    )
    retrieved_section = (
        render_retrieved_context(
            retrieved_context_chunks
            if retrieved_context_chunks is not None
            else load_retrieved_context_chunks(root, prompt)
        )
        if include_retrieved
        else ""
    )
    workflow_section = (
        render_workflow_policies(
            workflow_policies if workflow_policies is not None else load_workflow_policies(root)
        )
        if include_workflow
        else ""
    )
    contracts_section = (
        render_runtime_contracts(
            runtime_contracts if runtime_contracts is not None else load_runtime_contracts()
        )
        if include_contracts
        else ""
    )

    # M22: prompt-targeted symbol locations. Tiny (a few hundred chars) and the
    # highest value per token in the whole body, because each line replaces a
    # grep round-trip plus its match noise.
    symbol_section = (
        render_symbol_index(
            symbol_index if symbol_index is not None else load_symbol_index(root, prompt)
        )
        if include_retrieved
        else ""
    )

    # Ordered by value density: prompt-specific context first so that budget
    # trimming (which cuts from the end) sacrifices generic docs, not the
    # material actually relevant to this prompt.
    body = (
        f"{context_section}{summaries_section}{symbol_section}{retrieved_section}"
        f"{contracts_section}{navigation_section}{workflow_section}"
    )
    body, trimmed = _trim_to_budget(body, budget)

    # Stable prefix (identity/mode/tier) keeps prompt caching effective; only
    # the body and prompt vary between runs.
    header = f"# Input\n\nMode: {mode}\nContext tier: {tier} (~{budget} tok budget)\n"
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "input.md").write_text(
        f"{header}{body}\nPrompt:\n\n{prompt}\n", encoding="utf-8"
    )
    (run_dir / "run-contract.md").write_text(render_run_contract(prompt, mode, project_context), encoding="utf-8")
    (run_dir / "plan.md").write_text("# Plan\n\n_TODO: Bob navrhne ďalší krok._\n", encoding="utf-8")
    (run_dir / "output.md").write_text("# Output\n\n_TODO: výstup stage._\n", encoding="utf-8")
    (run_dir / "review.md").write_text("# Review\n\n_TODO: review gate._\n", encoding="utf-8")
    append_event(
        run_dir,
        "artifacts_initialized",
        {
            "mode": mode,
            "context_tier": tier,
            "tier_reason": decision["reason"],
            "tier_override": decision["override"],
            "budget_tokens": budget,
            "rendered_chars": len(body),
            "trimmed": trimmed,
            "signals": decision["signals"],
        },
    )
    return run_dir


# ── M17: adaptive context tiers ───────────────────────────────────────────
#
# Deliberately deterministic: no LLM call decides the tier, so classification
# is free, instant, and unit-testable. Misclassification is recoverable by
# design — the runtime agent has file/search tools and can pull more context
# on demand, so a too-small tier degrades to "agent reads a file" rather than
# "agent is blind".

TIER_XS = "XS"
TIER_S = "S"
TIER_M = "M"
TIER_L = "L"

TIER_SEQUENCE = (TIER_XS, TIER_S, TIER_M, TIER_L)

# Approximate token budget per tier (4 chars ≈ 1 token).
TIER_BUDGETS = {
    TIER_XS: 300,
    TIER_S: 2000,
    TIER_M: 5000,
    TIER_L: 8500,
}

SLASH_COMMAND_RE = re.compile(r"^/[a-z][\w-]*", re.IGNORECASE)

# Words that signal broad, multi-file or architectural work.
# Stored as STEMS and matched by prefix: Slovak inflects heavily
# ("všetky/všetkých/všetkym", "navrhni/navrhnúť/navrhnutie"), so exact-set
# matching silently under-classifies complex work as a small task.
_COMPLEX_STEMS = (
    "refaktor", "refactor", "prepis", "rewrite", "migr",
    "architekt", "architect", "rozdel", "split", "modulariz",
    "implement", "navrh", "design", "optimaliz", "optimiz",
    "e2e", "komplet",
    "prerob", "preprac", "redesign", "od zaciatku", "od základu",
)

# Words that signal a concrete but bounded edit.
_TASK_STEMS = (
    "oprav", "fix", "uprav", "zmen", "change", "pridaj", "add", "odstran",
    "remove", "dopln", "doplň", "nastav", "set", "premenuj", "rename",
    "aktualiz", "update", "otestuj", "test", "commit", "vymen", "vymeň",
    "replace", "presun", "move",
)

# Interrogative openers -> user wants an answer, not an edit.
_QUESTION_WORDS = {
    "co", "čo", "kde", "kedy", "ako", "preco", "prečo", "kto", "ktory", "ktorý",
    "what", "where", "when", "how", "why", "who", "which", "is", "does", "can",
    "vysvetli", "explain", "popis", "describe", "zhrn", "summarize",
}


def _stem_hits(words: list[str], stems: tuple[str, ...]) -> list[str]:
    """Prefix-match words against stems so inflected Slovak forms still count."""
    hits = []
    for word in words:
        for stem in stems:
            if word.startswith(stem):
                hits.append(word)
                break
    return sorted(set(hits))

_PATH_RE = re.compile(r"\b[\w./-]+\.(?:py|js|css|html|md|json|ya?ml|sh|toml|txt)\b")

# Phrases that genuinely mean "this spans many files". A bare "vsetko"/"celu" is
# usually descriptive in Slovak ("nevidim celu spravu"), so only the explicit
# scope phrasing counts as a complexity signal.
_BROAD_SCOPE_RE = re.compile(
    r"(?i)\b(?:vo\s+)?(?:vsetk|všetk)\w*\s+(?:suborov|súborov|suboroch|súboroch|modulov|miestach|endpointov)"
    r"|\ball\s+(?:files|modules|endpoints)\b"
    r"|\bcel(?:y|ý|u|ú|e|é|om|eho)\s+(?:projekt|codebase|backend|frontend|modul|system|systém|architektur\w*)"
)


def _normalise_words(text: str) -> list[str]:
    return re.findall(r"[a-zá-žA-ZÁ-Ž_]+", (text or "").lower())


def classify_context_tier(
    prompt: str,
    override: str | None = None,
    has_active_run: bool = False,
) -> dict[str, object]:
    """Pick a context tier for a prompt.

    Returns a dict with the chosen tier, a human-readable reason and the raw
    signals, so the decision can be logged and audited (dry-run mode) without
    re-running the classifier.
    """
    text = (prompt or "").strip()
    words = _normalise_words(text)
    word_count = len(words)
    paths = _PATH_RE.findall(text)

    signals: dict[str, object] = {
        "word_count": word_count,
        "paths": paths[:10],
        "path_count": len(paths),
        "is_slash_command": bool(SLASH_COMMAND_RE.match(text)),
        "has_active_run": bool(has_active_run),
        "complex_hits": _stem_hits(words, _COMPLEX_STEMS),
        "broad_scope": bool(_BROAD_SCOPE_RE.search(text)),
        "task_hits": _stem_hits(words, _TASK_STEMS),
        "question_lead": bool(words) and words[0] in _QUESTION_WORDS,
        "ends_with_question_mark": text.endswith("?"),
    }

    if override is not None:
        if override in TIER_BUDGETS:
            return {
                "tier": override,
                "budget_tokens": TIER_BUDGETS[override],
                "override": True,
                "reason": "explicit override from caller/UI",
                "signals": signals,
            }
        signals["invalid_override"] = str(override)

    is_question = bool(signals["question_lead"] or signals["ends_with_question_mark"])
    signals["is_question"] = is_question

    if signals["is_slash_command"]:
        tier, reason = TIER_XS, "slash command needs almost no project context"
    elif not text:
        tier, reason = TIER_XS, "empty prompt"
    elif is_question and not paths and word_count <= 12:
        # Questions want an answer, not an edit: never escalate a short question
        # to a heavy tier just because it mentions a word like "celkovy".
        tier, reason = TIER_S, "informational question, no edit expected"
    elif signals["complex_hits"] or signals["broad_scope"] or len(paths) >= 3:
        tier, reason = TIER_L, "complex/multi-file work detected"
    elif word_count > 70 and (signals["task_hits"] or paths):
        # Length ALONE is a weak signal: real prompt history showed long
        # conversational messages with zero complexity markers getting L.
        # Require an actual edit signal alongside the length.
        tier, reason = TIER_L, "long prompt with edit intent"
    elif signals["task_hits"] or paths:
        tier, reason = TIER_M, "bounded edit task detected"
    elif is_question:
        tier, reason = TIER_S, "informational question, no edit expected"
    elif word_count <= 3:
        tier, reason = TIER_S, "very short prompt, treat as lightweight"
    else:
        tier, reason = TIER_M, "default: assume a normal task"

    return {
        "tier": tier,
        "budget_tokens": TIER_BUDGETS[tier],
        "override": False,
        "reason": reason,
        "signals": signals,
    }


WORKFLOW_POLICY_PATHS = [
    "system/policies/run-contract.md",
    "system/policies/milestone-context-reset.md",
    "system/policies/autonomy-lanes.md",
    "system/policies/knowledge-routing.md",
    "system/policies/dox-lite.md",
    "system/policies/ui-architecture.md",
    "system/policies/environment-routing.md",
    "system/failure-patterns.md",
]

PROJECT_MAP_DOC_PATHS = [
    "docs/HELM_SYSTEM_MAP.md",
    "docs/HELM_CONTEXT.md",
    "docs/FILE_INDEX.md",
    "docs/API_INDEX.md",
    "docs/UI_INDEX.md",
    "docs/TOKEN_USAGE_OPTIMIZATION.md",
]

HYBRID_RETRIEVAL_PATHS = [
    "app/main.py",
    "app/workspace.py",
    "app/db.py",
    "app/schemas.py",
    "shared-ui/app.js",
    "shared-ui/index.html",
    "shared-ui/styles.css",
    "tests/test_api.py",
    "docs/HELM_SYSTEM_MAP.md",
    "docs/HELM_CONTEXT.md",
    "docs/FILE_INDEX.md",
    "docs/API_INDEX.md",
    "docs/UI_INDEX.md",
    "docs/TOKEN_USAGE_OPTIMIZATION.md",
]


def project_docs_fallback_root() -> Path:
    return Path(os.getenv("HELM_PROJECT_DOCS_ROOT", Path(__file__).resolve().parents[1])).expanduser()


def load_workflow_policies(root: str | Path, max_chars_per_file: int = 4000) -> list[dict[str, str]]:
    root = Path(root).expanduser()
    policies: list[dict[str, str]] = []
    for rel in WORKFLOW_POLICY_PATHS:
        path = root / rel
        try:
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file].rstrip() + "\n\n[truncated]"
        policies.append({"path": rel, "name": Path(rel).name, "content": content})
    return policies


def load_project_map_docs(root: str | Path, max_chars_per_file: int = 2500) -> list[dict[str, object]]:
    primary_root = Path(root).expanduser()
    fallback_root = project_docs_fallback_root()
    maps: list[dict[str, object]] = []
    for rel in PROJECT_MAP_DOC_PATHS:
        source = "workspace"
        for base in (primary_root, fallback_root):
            path = base / rel
            if path.exists() and path.is_file():
                source = "fallback" if base == fallback_root and base != primary_root else "workspace"
                break
        else:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        original_size = len(content)
        truncated = False
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file].rstrip() + "\n\n[truncated — read targeted chunks from this file when needed]"
            truncated = True
        maps.append({
            "path": rel,
            "name": Path(rel).name,
            "content": content,
            "source_path": str(path),
            "source": source,
            "original_size": original_size,
            "truncated": truncated,
        })
    return maps


def render_context_navigation(map_docs: list[dict[str, object]]) -> str:
    if not map_docs:
        return ""
    sections = [
        "\n## Context navigation maps",
        "",
        "Use these project maps before broad source inspection. They are the token-saving navigation layer for Helm: identify the owning file/API/UI area first, then read only targeted chunks instead of full large files.",
        "",
        "Required navigation order:",
        "1. Start with `docs/HELM_SYSTEM_MAP.md` for source-of-truth routing and safe-read rules.",
        "2. Read `docs/HELM_CONTEXT.md` for current architecture/status.",
        "3. Use `docs/FILE_INDEX.md` to choose exact files and bounded line ranges.",
        "4. Use `docs/API_INDEX.md` for backend endpoints/contracts.",
        "5. Use `docs/UI_INDEX.md` for shared-ui symbols, DOM contracts, and browser-smoke targets.",
        "6. Use `docs/TOKEN_USAGE_OPTIMIZATION.md` to keep reads/searches chunked and evidence-based.",
    ]
    for doc in map_docs:
        sections.extend(["", f"### {doc['path']}", "", str(doc["content"]).strip()])
    sections.append("")
    return "\n".join(sections)


def _retrieval_roots(root: str | Path) -> list[Path]:
    primary = Path(root).expanduser()
    fallback = project_docs_fallback_root()
    live_roots = {Path.home() / "ObsidianVault" / "helm" / "workspace-live", Path.home() / "ObsidianVault" / "helm" / "live"}
    if configured_root := os.getenv("HELM_WORKSPACE_ROOT"):
        live_roots.add(Path(configured_root).expanduser())
    if primary != fallback and primary not in live_roots:
        return [primary]
    roots: list[Path] = []
    for candidate in (primary, fallback):
        if candidate not in roots:
            roots.append(candidate)
    return roots


def _query_terms(text: str) -> set[str]:
    stop_words = {
        "the", "and", "for", "with", "from", "that", "this", "into", "runtime", "helm", "bob",
        "sprav", "urob", "oprav", "prosím", "prosim", "agent", "system", "systém", "toto", "ten", "pre",
    }
    terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_{}./-]{3,}", text[:4000])}
    expanded = set(terms)
    for term in terms:
        expanded.update(part for part in re.split(r"[_./{}-]+", term) if len(part) >= 3)
    filtered = {term for term in expanded if term not in stop_words}
    return set(sorted(filtered, key=lambda item: (-len(item), item))[:32])


def _chunk_anchor(line: str) -> bool:
    stripped = line.strip()
    return bool(
        re.match(r"^(def|class)\s+", stripped)
        or re.match(r"^(async\s+)?function\s+", stripped)
        or re.match(r"^(const|let|var)\s+[A-Za-z0-9_]+\s*=", stripped)
        or stripped.startswith("@app.")
        or stripped.startswith("#")
        or stripped.startswith("##")
    )


def _file_chunks(path: Path, rel: str, max_lines: int = 80) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return []
    if not lines:
        return []
    anchors = [idx for idx, line in enumerate(lines) if _chunk_anchor(line)] or [0]
    chunks: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    for anchor_idx, start_idx in enumerate(anchors):
        end_idx = anchors[anchor_idx + 1] if anchor_idx + 1 < len(anchors) else min(len(lines), start_idx + max_lines)
        if end_idx - start_idx > max_lines:
            end_idx = start_idx + max_lines
        key = (start_idx, end_idx)
        if key in seen or start_idx >= end_idx:
            continue
        seen.add(key)
        title = lines[start_idx].strip()[:120] or Path(rel).name
        content = "\n".join(lines[start_idx:end_idx]).strip()
        chunks.append({
            "path": rel,
            "source_path": str(path),
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "title": title,
            "content": content,
        })
    return chunks


def _score_chunk(chunk: dict[str, object], terms: set[str]) -> int:
    haystack = " ".join([
        str(chunk.get("path", "")),
        str(chunk.get("title", "")),
        str(chunk.get("content", "")),
    ]).lower()
    score = 0
    for term in terms:
        count = haystack.count(term)
        if count:
            score += min(count, 5)
            if term in str(chunk.get("path", "")).lower():
                score += 3
            if term in str(chunk.get("title", "")).lower():
                score += 4
    return score


SYMBOL_INDEX_PATHS = [
    "app/main.py",
    "app/workspace.py",
    "app/db.py",
    "app/schemas.py",
    "shared-ui/app.js",
]

# Discovery rules, kept identical to scripts/generate_code_index.py so the
# runtime and the generated doc can never disagree about what is worth indexing.
SYMBOL_INDEX_GLOBS = ["app/*.py", "scripts/*.py", "shared-ui/*.js"]
SYMBOL_INDEX_MIN_LINES = 400


def discover_symbol_index_paths(root: str | Path) -> list[str]:
    """Return files large enough that locating code in them costs real tokens.

    Zero-maintenance by design: the earlier hand-written `docs/FILE_INDEX.md`
    rotted for 52 days and understated `app/main.py` by 51 %, which is what made
    the agent fall back to grep in the first place. A hardcoded path list would
    rot the same way -- a new 2000-line module would simply be invisible, with no
    error to notice. So the list is derived from the same size rule the generator
    uses, and `SYMBOL_INDEX_PATHS` is only a fallback for when the tree cannot be
    scanned.
    """
    found: list[str] = []
    for base in _retrieval_roots(root):
        for pattern in SYMBOL_INDEX_GLOBS:
            for path in sorted(base.glob(pattern)):
                if not path.is_file():
                    continue
                try:
                    lines = len(
                        path.read_text(encoding="utf-8", errors="replace").splitlines()
                    )
                except OSError:
                    continue
                if lines < SYMBOL_INDEX_MIN_LINES:
                    continue
                rel = path.relative_to(base).as_posix()
                if rel not in found:
                    found.append(rel)
        if found:
            break
    return found or list(SYMBOL_INDEX_PATHS)

# JS: module-level declarations only. Indentation is the discriminator, and the
# limit is derived from the real file rather than guessed -- in shared-ui/app.js
# (an IIFE, so "top level" is 2 spaces) *all* 132 function declarations sit at
# indent 2, while `const`/`let`/`var` appear at 2 (41x) and at 4/6/8/10 (374x,
# i.e. function-locals).
#
# Matching any indentation indexed locals such as
# `    const command = (raw || '').toLowerCase();` as navigable symbols, so a
# prompt containing the ordinary word "command" pulled an offset into context --
# exactly the noise this feature removes. Requiring indent 0 was the opposite
# error: it dropped every real symbol in the file.
_JS_MAX_SYMBOL_INDENT = 2
_PY_SYMBOL_RE = re.compile(r"^\s*(?:async\s+)?(def|class)\s+(\w+)")
_JS_SYMBOL_RE = re.compile(
    r"^(?P<indent> *)(?:export\s+)?(?:async\s+)?"
    r"(?:function\s+(?P<fn>\w+)"
    r"|(?:const|let|var)\s+(?P<var>\w+)\s*=\s*(?:async\s*)?(?:\(|function\b))"
)


def _js_symbol_name(line: str) -> str | None:
    """Return a module-level JS symbol name, or None for locals/non-declarations."""
    match = _JS_SYMBOL_RE.match(line)
    if not match:
        return None
    if len(match.group("indent")) > _JS_MAX_SYMBOL_INDENT:
        return None
    return match.group("fn") or match.group("var")

# Score awarded when a prompt term equals a symbol name exactly.
SYMBOL_EXACT_SCORE = 10

# Minimum score required to emit a symbol. Set to the exact-match score on
# purpose: substring matching was measured and rejected.
#
# Replay of 149 real lookup calls (scripts/measure_symbol_index.py) against the
# session DB, swept in scripts/sweep_symbol_index.py:
#
#   substring allowed  -> charged 46/62 prompts, net  +1,019 B  (+0.2 %)
#   exact only         -> charged 14/62 prompts, net +37,558 B  (+7.4 %)
#
# Identical recall (22 lookups answered either way) at ~1/10th the cost, because
# weak substring hits fire on almost every technical prompt and pay for nothing.
# Recall itself is capped near 26 % (scripts/diagnose_symbol_index.py): prompts
# usually do not name the symbol the agent ends up hunting. So precision is the
# only lever that moves the number. Do not lower this without re-running the
# sweep -- tests/test_symbol_index.py enforces it.
SYMBOL_MATCH_MIN_SCORE = SYMBOL_EXACT_SCORE


def load_symbol_index(
    root: str | Path,
    prompt: str,
    max_symbols: int = 4,
) -> list[dict[str, object]]:
    """Return symbols relevant to `prompt`, each with its exact line number.

    Purpose (M22): remove grep round-trips. Measured on the 2026-08-18 sessions,
    58 of 106 `terminal` calls were `grep`/`sed -n` used only to locate a symbol,
    and code lookup consumed ~59 % of all tool output entering context.

    Deliberately prompt-targeted rather than a static document: the full symbol
    index for this repo is ~22k chars / 430 symbols, and project map docs are
    capped at 2500 chars, so a static doc loses ~89 % of its content (and drops
    whole files). Selecting per prompt keeps it a handful of lines.

    Cheap by construction: line-regex scan, no AST, no LLM, no embeddings --
    consistent with the deterministic low-overhead retrieval already used here.
    """
    terms = _query_terms(prompt)
    if not terms:
        return []

    hits: list[dict[str, object]] = []
    for rel in discover_symbol_index_paths(root):
        for base in _retrieval_roots(root):
            path = base / rel
            if not path.exists() or not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                break

            is_py = path.suffix == ".py"
            found: list[tuple[int, str, str]] = []
            for lineno, line in enumerate(lines, start=1):
                if is_py:
                    m = _PY_SYMBOL_RE.match(line)
                    if m:
                        found.append((lineno, m.group(1), m.group(2)))
                else:
                    js_name = _js_symbol_name(line)
                    if js_name:
                        found.append((lineno, "function", js_name))

            for idx, (lineno, kind, name) in enumerate(found):
                lowered = name.lower()
                # Exact name match only. Substring matching was measured as a
                # net no-op (see SYMBOL_MATCH_MIN_SCORE) and re-introduces the
                # noise this is meant to replace.
                score = SYMBOL_EXACT_SCORE * sum(
                    1 for term in terms if term == lowered
                )
                if score < SYMBOL_MATCH_MIN_SCORE:
                    continue
                end = found[idx + 1][0] - 1 if idx + 1 < len(found) else len(lines)
                hits.append({
                    "path": rel,
                    "name": name,
                    "kind": kind,
                    "line": lineno,
                    "span": max(1, end - lineno + 1),
                    "score": score,
                })
            break

    hits.sort(key=lambda h: (-int(str(h["score"])), str(h["path"]), int(str(h["line"]))))
    return hits[:max_symbols]


def render_symbol_index(hits: list[dict[str, object]]) -> str:
    """Render symbol hits as an actionable read instruction.

    Returns '' for no hits so an unrelated prompt costs zero tokens.
    """
    if not hits:
        return ""
    lines = [
        "## Symbol index (prompt-targeted)",
        "",
        "Exact locations for symbols mentioned in this task. Read them directly with",
        "`read_file(path, offset=<line>, limit=<span>)` — do NOT grep to find them.",
        "",
    ]
    for hit in hits:
        lines.append(
            f"- `{hit['name']}` ({hit['kind']}) — `{hit['path']}` "
            f"offset={hit['line']} limit={hit['span']}"
        )
    return "\n".join(lines) + "\n"


def load_retrieved_context_chunks(
    root: str | Path,
    prompt: str,
    max_chunks: int = 8,
    max_chars_per_chunk: int = 900,
) -> list[dict[str, object]]:
    terms = _query_terms(prompt)
    if not terms:
        return []
    candidates: list[dict[str, object]] = []
    resolved_paths: set[str] = set()
    for rel in HYBRID_RETRIEVAL_PATHS:
        for base in _retrieval_roots(root):
            path = base / rel
            if not path.exists() or not path.is_file():
                continue
            path_key = str(path.resolve())
            if path_key in resolved_paths:
                break
            resolved_paths.add(path_key)
            for chunk in _file_chunks(path, rel):
                score = _score_chunk(chunk, terms)
                if score <= 0:
                    continue
                content = str(chunk["content"])
                truncated = False
                if len(content) > max_chars_per_chunk:
                    content = content[:max_chars_per_chunk].rstrip() + "\n[chunk truncated — read bounded source lines if needed]"
                    truncated = True
                chunk = {**chunk, "content": content, "score": score, "truncated": truncated}
                candidates.append(chunk)
            break
    candidates.sort(key=lambda item: (-int(str(item["score"])), str(item["path"]), int(str(item["start_line"]))))
    return candidates[:max_chunks]


def _markdown_fence(content: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`{3,}", content)), default=0)
    return "`" * max(3, longest + 1)


def render_retrieved_context(chunks: list[dict[str, object]]) -> str:
    if not chunks:
        return ""
    sections = [
        "\n## Retrieved relevant chunks",
        "",
        "Hybrid local retriever output: keyword/symbol/heading chunks selected from Helm source/docs. Treat these as starting evidence, then read bounded source chunks if a change depends on exact surrounding context.",
    ]
    for chunk in chunks:
        content = str(chunk["content"]).strip()
        fence = _markdown_fence(content)
        sections.extend([
            "",
            f"### {chunk['path']}:{chunk['start_line']}-{chunk['end_line']} — score {chunk['score']}",
            "",
            f"Source: `{chunk['source_path']}`",
            "",
            fence,
            content,
            fence,
        ])
    sections.append("")
    return "\n".join(sections)


def render_workflow_policies(policies: list[dict[str, str]]) -> str:
    if not policies:
        return ""
    sections = ["\n## Workflow governance", "", "These Helm workflow policies guide this run. Update the nearest owning policy/skill/doc instead of relying on chat history."]
    for policy in policies:
        sections.extend(["", f"### {policy['path']}", "", policy["content"].strip()])
    sections.append("")
    return "\n".join(sections)


def render_run_contract(prompt: str, mode: str, project_context: str = "") -> str:
    context_hint = project_context.strip()[:1200] if project_context else "None loaded."
    return (
        "# Run Contract\n\n"
        "This artifact is the working contract for a non-trivial Helm/Bob run. Edit it before execution if the scope is wrong.\n\n"
        f"## Goal\n{prompt.strip() or '_TODO: define exact user-facing outcome._'}\n\n"
        "## Non-goals\n- Do not perform external actions without the user's explicit approval.\n- Do not store temporary task state in memory; use artifacts/handoff/project docs.\n- Do not proceed to the next milestone before context/index reset is current.\n\n"
        f"## Scope\n- Mode: {mode}\n- Local workspace/docs/policies may be changed if they support the goal.\n- Project context summary: {context_hint}\n\n"
        "## Autonomy Lane\n- Green: local reads, tests, snapshots, localhost smoke.\n- Yellow: larger local edits, dev DB/test-data changes, runtime/policy changes; report with evidence.\n- Red: external sends/publishing/real accounts/user-data deletion/identity changes; stop for approval.\n\n"
        "## Verification\n- Focused tests for changed behavior.\n- Full `pytest -q` for Helm backend changes.\n- Browser smoke when UI/runtime event visibility changes.\n- Reviewer/critic pass for multi-file runtime/workflow changes.\n\n"
        "## Stop Conditions\n- Requirement ambiguity changes which subsystem should be edited.\n- A Red-lane action is needed.\n- Tests fail and root cause is not understood.\n- The run detects a session-health warning that requires handoff/new session.\n\n"
        "## Persistence Routing\n- Memory: stable facts only.\n- Skill: reusable workflow.\n- Policy doc: durable behavior rule.\n- Handoff: current work state.\n- Project docs: project-specific context.\n\n"
        "## Next Milestone Link\n_TODO: after completion, update HELM_CONTEXT/FILE_INDEX/API_INDEX/roadmap and snapshot before starting the next milestone._\n"
    )


def runtime_contract_paths() -> list[Path]:
    configured = os.getenv("HELM_RUNTIME_CONTRACT_PATHS")
    if configured is not None:
        return [Path(item).expanduser() for item in configured.split(os.pathsep) if item.strip()]
    return DEFAULT_RUNTIME_CONTRACT_PATHS


def load_runtime_contracts(max_chars_per_file: int = 6000) -> list[dict[str, str]]:
    contracts: list[dict[str, str]] = []
    for path in runtime_contract_paths():
        try:
            resolved = path.expanduser().resolve()
            if not resolved.exists() or not resolved.is_file():
                continue
            content = resolved.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file].rstrip() + "\n\n[truncated]"
        contracts.append({"path": str(resolved), "name": resolved.name, "content": content})
    return contracts


def render_runtime_contracts(contracts: list[dict[str, str]]) -> str:
    if not contracts:
        return ""
    sections = ["\n## Runtime contracts", "", "These contracts are loaded into the Helm runtime prompt for this run."]
    for contract in contracts:
        sections.extend(["", f"### {contract['name']}", "", f"Path: `{contract['path']}`", "", contract["content"].strip()])
    sections.append("")
    return "\n".join(sections)


def list_artifacts(run_dir: str | Path) -> list[dict[str, object]]:
    run_dir = Path(run_dir)
    artifacts = []
    for path in sorted(run_dir.iterdir() if run_dir.exists() else []):
        if path.is_file():
            artifacts.append({"name": path.name, "size": path.stat().st_size})
    return artifacts


def save_artifact_version(run_dir: str | Path, name: str) -> dict[str, object]:
    artifact_path = resolve_artifact(run_dir, name)
    if not artifact_path.exists() or not artifact_path.is_file():
        raise ValueError("Artifact not found")

    versions_dir = _artifact_versions_dir(run_dir, name)
    versions_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    version_name = f"{timestamp}{artifact_path.suffix or '.md'}"
    version_path = (versions_dir / version_name).resolve()
    _ensure_under_versions_root(run_dir, version_path)
    version_path.write_text(artifact_path.read_text(encoding="utf-8"), encoding="utf-8")
    return _version_metadata(run_dir, name, version_path)


def list_artifact_versions(run_dir: str | Path, name: str) -> list[dict[str, object]]:
    versions_dir = _artifact_versions_dir(run_dir, name)
    if not versions_dir.exists():
        return []
    return [_version_metadata(run_dir, name, path) for path in sorted(versions_dir.iterdir()) if path.is_file()]


def resolve_artifact_version(run_dir: str | Path, name: str, version_name: str) -> Path:
    if not version_name or "/" in version_name or "\\" in version_name or version_name in {".", ".."}:
        raise ValueError("Invalid version name")
    if not re.fullmatch(r"[0-9T]+\.(?:md|jsonl|txt)", version_name):
        raise ValueError("Invalid version name")
    path = (_artifact_versions_dir(run_dir, name) / version_name).resolve()
    _ensure_under_versions_root(run_dir, path)
    return path


def resolve_artifact(run_dir: str | Path, name: str) -> Path:
    if name not in SAFE_ARTIFACTS:
        raise ValueError("Unsupported artifact")
    run_dir = Path(run_dir).resolve()
    path = (run_dir / name).resolve()
    if run_dir not in path.parents and path != run_dir:
        raise ValueError("Invalid artifact path")
    return path


def _artifact_versions_dir(run_dir: str | Path, name: str) -> Path:
    # Reuse artifact allow-list and traversal checks, then derive a directory
    # from the canonical artifact filename. Current artifacts are flat, but this
    # keeps version storage safe if the allow-list grows later.
    artifact_path = resolve_artifact(run_dir, name)
    run_dir = Path(run_dir).resolve()
    versions_root = (run_dir / ".versions").resolve()
    version_dir = (versions_root / artifact_path.relative_to(run_dir)).resolve()
    _ensure_under_versions_root(run_dir, version_dir)
    return version_dir


def _ensure_under_versions_root(run_dir: str | Path, path: Path) -> None:
    versions_root = (Path(run_dir).resolve() / ".versions").resolve()
    if path != versions_root and versions_root not in path.parents:
        raise ValueError("Invalid version path")


def _version_metadata(run_dir: str | Path, name: str, path: Path) -> dict[str, object]:
    path = path.resolve()
    _ensure_under_versions_root(run_dir, path)
    versions_root = (Path(run_dir).resolve() / ".versions").resolve()
    version_id = path.name
    timestamp = path.stem
    return {
        "artifact": name,
        "id": version_id,
        "name": version_id,
        "timestamp": timestamp,
        "size": path.stat().st_size,
        "path": f".versions/{path.relative_to(versions_root).as_posix()}",
    }


def append_event(run_dir: str | Path, event_type: str, payload: dict[str, object]) -> None:
    import json

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    event = {"type": event_type, "payload": payload, "created_at": datetime.now().isoformat(timespec="seconds")}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9áäčďéíĺľňóôŕšťúýž]+", "-", text)
    text = text.strip("-")
    return text[:48]
