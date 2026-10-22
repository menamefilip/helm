# Context tiers

Helm classifies prompts deterministically into XS, S, M, or L context tiers. The classifier is local and rule-based; it makes no model call.

- XS: empty prompts and lightweight UI commands.
- S: short informational questions.
- M: ordinary bounded tasks.
- L: multi-file, broad-scope, or complex work.

The user can override the suggested tier. Keep the user prompt intact at every tier, and update `tests/test_context_tiers.py` when changing signals or budgets.
