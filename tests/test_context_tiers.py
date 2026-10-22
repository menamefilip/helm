"""M17: adaptive context tiers — deterministic classifier, no LLM calls."""

from pathlib import Path

import pytest

from app.workspace import classify_context_tier, TIER_XS, TIER_S, TIER_M, TIER_L


class TestTierClassification:
    @pytest.mark.parametrize("prompt", ["/clear", "/help", "/quest", "  /clear  ", "/theme dark"])
    def test_slash_commands_are_xs(self, prompt):
        assert classify_context_tier(prompt)["tier"] == TIER_XS

    @pytest.mark.parametrize("prompt", [
        "co je helm?",
        "kde je run summary?",
        "what does _finalize_runtime_bridge do?",
        "preco padol run 17?",
        "ako funguje harvest?",
    ])
    def test_questions_are_small(self, prompt):
        assert classify_context_tier(prompt)["tier"] == TIER_S

    @pytest.mark.parametrize("prompt", [
        "oprav okraje v quest tabe",
        "pridaj endpoint pre run summary",
        "uprav styl tlacidla v shared-ui/styles.css",
    ])
    def test_normal_tasks_are_medium(self, prompt):
        assert classify_context_tier(prompt)["tier"] == TIER_M

    @pytest.mark.parametrize("prompt", [
        "refaktoruj cely runtime bridge a rozdel main.py na moduly",
        "implementuj adaptivny kontext s tiermi, testami a UI badge",
        "prepis app/main.py, app/db.py a app/workspace.py na novu strukturu",
    ])
    def test_complex_tasks_are_large(self, prompt):
        assert classify_context_tier(prompt)["tier"] == TIER_L

    def test_explicit_override_wins(self):
        """User-selected tier from UI must beat the heuristic."""
        result = classify_context_tier("/clear", override=TIER_L)
        assert result["tier"] == TIER_L
        assert result["override"] is True

    def test_invalid_override_falls_back_to_heuristic(self):
        result = classify_context_tier("/clear", override="ENORMOUS")
        assert result["tier"] == TIER_XS
        assert result["override"] is False

    def test_result_exposes_reason_for_logging(self):
        """Dry-run logging (option 4) needs a machine-readable reason."""
        result = classify_context_tier("refaktoruj cely runtime bridge")
        assert result["reason"]
        assert "signals" in result

    def test_empty_prompt_does_not_crash(self):
        assert classify_context_tier("")["tier"] in {TIER_XS, TIER_S}

    def test_file_path_mention_lifts_tier(self):
        """Mentioning concrete files means the agent needs edit context."""
        bare = classify_context_tier("uprav to")["tier"]
        with_path = classify_context_tier("uprav app/main.py")["tier"]
        assert TIER_ORDER.index(with_path) >= TIER_ORDER.index(bare)


TIER_ORDER = [TIER_XS, TIER_S, TIER_M, TIER_L]


class TestTierBudgets:
    def test_tiers_are_strictly_increasing_in_budget(self):
        from app.workspace import TIER_BUDGETS
        budgets = [TIER_BUDGETS[t] for t in TIER_ORDER]
        assert budgets == sorted(budgets)
        assert len(set(budgets)) == len(budgets)

    def test_xs_budget_is_tiny_and_l_is_full(self):
        from app.workspace import TIER_BUDGETS
        assert TIER_BUDGETS[TIER_XS] <= 800
        assert TIER_BUDGETS[TIER_L] >= 6000


class TestAdaptiveInputRendering:
    """The tier must actually change input.md size, not just get logged."""

    def _build(self, tmp_path, prompt, **kw):
        from app.workspace import create_run_artifacts
        run_dir = create_run_artifacts(tmp_path / "ws", 1, prompt, "work", "Project ctx", **kw)
        return (run_dir / "input.md").read_text(encoding="utf-8")

    def test_xs_is_dramatically_smaller_than_l(self, tmp_path):
        xs = self._build(tmp_path / "a", "/clear")
        lg = self._build(tmp_path / "b", "refaktoruj cely runtime bridge a rozdel main.py")
        assert len(xs) < len(lg) / 4, f"XS={len(xs)} should be far below L={len(lg)}"

    def test_xs_stays_within_budget(self, tmp_path):
        from app.workspace import TIER_BUDGETS, TIER_XS
        xs = self._build(tmp_path, "/clear")
        assert len(xs) / 4 <= TIER_BUDGETS[TIER_XS] * 1.5

    def test_prompt_always_survives_every_tier(self, tmp_path):
        """Trimming context must never trim the user's actual request."""
        for i, p in enumerate(["/clear", "co je helm?", "oprav quest tab", "refaktoruj vsetky moduly"]):
            out = self._build(tmp_path / f"t{i}", p)
            assert p.lstrip("/") in out or p in out, f"prompt lost at tier for {p!r}"

    def test_tier_recorded_in_input(self, tmp_path):
        out = self._build(tmp_path, "/clear")
        assert "XS" in out, "tier should be visible in the rendered input for auditability"

    def test_identity_prefix_is_stable_across_tiers(self, tmp_path):
        """Stable prefix preserves prompt caching; only tier line + tail vary."""
        a = self._build(tmp_path / "x", "co je helm?")
        b = self._build(tmp_path / "y", "oprav quest tab")
        assert a.startswith("# Input\n\nMode: work\n")
        assert b.startswith("# Input\n\nMode: work\n")

    def test_explicit_override_changes_rendered_size(self, tmp_path):
        from app.workspace import TIER_L
        small = self._build(tmp_path / "s", "/clear")
        forced = self._build(tmp_path / "f", "/clear", context_tier=TIER_L)
        assert len(forced) > len(small) * 2


class TestTierApiAndLogging:
    def test_classify_endpoint_previews_tier(self, tmp_path):
        from tests.test_api import make_client
        client, _ = make_client(tmp_path)
        r = client.post("/api/context/classify", json={"prompt": "/clear"})
        assert r.status_code == 200
        assert r.json()["tier"] == "XS"

    def test_classify_endpoint_honours_override(self, tmp_path):
        from tests.test_api import make_client
        client, _ = make_client(tmp_path)
        r = client.post("/api/context/classify", json={"prompt": "/clear", "context_tier": "L"})
        assert r.json()["tier"] == "L"
        assert r.json()["override"] is True

    def test_classify_rejects_bad_tier(self, tmp_path):
        from tests.test_api import make_client
        client, _ = make_client(tmp_path)
        assert client.post("/api/context/classify", json={"prompt": "x", "context_tier": "HUGE"}).status_code == 422

    def test_tier_decision_is_logged_per_run(self, tmp_path):
        from tests.test_api import make_client, _any_project
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "/clear"}).json()
        events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
        assert "context_tier_selected" in [e["type"] for e in events]

    def test_tier_stats_aggregates_decisions(self, tmp_path):
        from tests.test_api import make_client, _any_project
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "/clear"})
        client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "refaktoruj cely runtime bridge"})
        stats = client.get("/api/context/tier-stats").json()
        assert stats["sampled_runs"] >= 2
        assert stats["counts"].get("XS", 0) >= 1
        assert stats["counts"].get("L", 0) >= 1

    def test_api_override_changes_input_size(self, tmp_path):
        from tests.test_api import make_client, _any_project
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        small = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "/clear"}).json()
        big = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "/clear", "context_tier": "L"}).json()
        a = client.get(f"/api/runs/{small['id']}/artifacts/input.md").json()["content"]
        b = client.get(f"/api/runs/{big['id']}/artifacts/input.md").json()["content"]
        assert len(b) > len(a) * 2


class TestSlovakInflectionAndFalsePositives:
    """Regression: Slovak inflects heavily, so exact-word matching silently
    under-classified complex work, and a greedy stem then over-classified
    questions. Both directions are pinned here."""

    @pytest.mark.parametrize("prompt", [
        "vymen kniznicu za novu vo vsetkych suboroch",
        "toto treba cele inak navrhnut",
        "potrebujem prerobit cely modul",
        "prepracuj celu architekturu",
        "migrujem vsetky endpointy",
    ])
    def test_inflected_complex_forms_reach_large(self, prompt):
        assert classify_context_tier(prompt)["tier"] == TIER_L

    @pytest.mark.parametrize("prompt", [
        "kde je celkovy prehlad?",
        "co je helm?",
        "ako funguje harvest?",
        "preco padol run 17?",
        "co robi test suite?",
    ])
    def test_questions_never_escalate_to_large(self, prompt):
        """'celkovy' must not trip the 'cely' complexity stem."""
        assert classify_context_tier(prompt)["tier"] in {TIER_XS, TIER_S}

    def test_question_with_explicit_paths_still_gets_edit_context(self):
        """A question naming concrete files may still need file context."""
        tier = classify_context_tier("co je zle v app/main.py?")["tier"]
        assert tier in {TIER_M, TIER_L}

    def test_diacritics_are_lowercased_not_stripped(self):
        from app.workspace import _normalise_words
        assert _normalise_words("ŤAŽKÝ Ľudo") == ["ťažký", "ľudo"]


class TestAutoSummarySecretHygiene:
    """Summaries are persisted AND re-injected into later prompts, so an
    unredacted secret would propagate across runs. Highest-value regression."""

    def test_secrets_never_reach_stored_summary(self, tmp_path):
        import sys
        from tests.test_api import make_client, _any_project
        from app.main import _execute_runtime_process, _finalize_runtime_bridge
        from app.db import connect

        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "deploy"}).json()

        script = (
            "print('export OPENAI_API_KEY=sk-test42')\n"
            "print(\"curl -H 'Authorization: Bearer sk-test42'\")\n"
            "print('Next steps: rotate token=supersecretvalue')\n"
            "print('3 passed in 1.2s')\n"
        )
        with connect(tmp_path / "workspace.db") as conn:
            run_row = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
            result = _execute_runtime_process(run_row, [sys.executable, "-c", script], timeout_seconds=30)
            _finalize_runtime_bridge(conn, run_row, result)

        data = client.get(f"/api/runs/{run['id']}/summary").json()
        blob = " ".join(str(v) for v in data.values())
        assert "sk-test42" not in blob
        assert "ghp_LEAKED" not in blob
        assert "supersecretvalue" not in blob
        # useful signal must survive redaction
        assert "3 passed" in data["tests"]


class TestDescriptiveScopeLanguage:
    """Descriptive words like 'whole' or 'everything' should not imply a broad edit."""

    @pytest.mark.parametrize("prompt", [
        "nevidim celu ukazku zobrazuje sa len prva cast",
        "vypis co vsetko je v zozname",
        "show me the full response again",
    ])
    def test_descriptive_cely_vsetko_is_not_complexity(self, prompt):
        assert classify_context_tier(prompt)["tier"] != TIER_L

    def test_long_chatty_prompt_without_edit_intent_is_not_large(self):
        prompt = " ".join(["sample"] * 60) + " and explain the result"
        assert classify_context_tier(prompt)["tier"] != TIER_L

    @pytest.mark.parametrize("prompt", [
        "replace the library in all files",
        "update all files in the package",
        "refactor the whole project",
        "refactor all files in the module",
    ])
    def test_explicit_broad_scope_still_reaches_large(self, prompt):
        assert classify_context_tier(prompt)["tier"] == TIER_L

    def test_broad_scope_signal_is_exposed(self):
        signals = classify_context_tier("uprav to vo vsetkych suboroch")["signals"]
        assert signals["broad_scope"] is True
        assert classify_context_tier("nevidim celu ukazku")["signals"]["broad_scope"] is False
