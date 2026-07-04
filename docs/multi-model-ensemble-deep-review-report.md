# Multi-Model Ensemble Branch — Deep Review and Canary Log Report

Date: 2026-07-04
Branch reviewed: `feat/multi-model-ensemble` (11 commits ahead of `main`, merge base `31d7dd02`)
Deployment checked: Coolify app `pr-agent-fork-canary` (`ahib8a9mgq077qljkszetods`) on server `wabot-dev-oc`

## Scope and Method

- Reviewed the full branch diff vs `main` (21 files, +3791/-33): `pr_agent/algo/ensemble.py`,
  `pr_agent/tools/pr_reviewer.py`, `pr_agent/tools/pr_code_suggestions.py`, the two new consolidation
  prompt TOMLs, configuration keys, and the four new test files.
- Three parallel independent reviewers covered (1) the core ensemble module + config, (2) the `/review`
  flow, (3) the `/improve` flow. Every candidate finding was verified against actual code paths.
- Validators run locally: all 23 ensemble unit tests pass
  (`PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_ensemble*.py tests/unittest/test_pr_*_ensemble.py -q`);
  `flake8` run on the touched files.
- Coolify canary inspected over SSH: app state via the Coolify API, running container, and 14 days of
  container logs (~3.7k structured records).

## Branch State

| Check | Result |
| --- | --- |
| Merge conflicts vs `main` | None (`git merge-tree` clean); branch is 11 ahead / 0 behind |
| Pushed | `origin/feat/multi-model-ensemble` is 1 commit behind local (`1ce15b5e` docs commit unpushed) |
| Deployed | Canary runs `feature/agentic-repo-access` tip `cdd265cf`, which **contains all 11 ensemble commits** (`9a7d5fab` is an ancestor) |
| AGENTS.md deployment notes | Stale — says deployed commit is `2063a917`; actual running image is `cdd265cf` |
| Unit tests | 23/23 ensemble tests pass |

## Code Review Findings

### P1 — Push-trigger ensemble override is silently reverted (verified live)

The canary sets `GITHUB_APP__PUSH_COMMANDS` with `--config.ensemble_models=openai/claude-opus-4-8`
intending single-model ensembles on push events, plus the env var
`CONFIG__ENSEMBLE_MODELS=openai/claude-opus-4-8,openai/gpt-5.5` for PR-open events.

The override never takes effect. Flow:

1. `_perform_auto_commands_github` (`pr_agent/servers/github_app.py:395`) consumes `--k=v` args via
   `update_settings_from_args` and does **not** forward them in `new_command`.
2. `PRAgent._handle_request` (`pr_agent/agent/pr_agent.py:56`) then calls `apply_repo_settings` again,
   which ends with `_reapply_env_overrides()` (`pr_agent/git_providers/utils.py:315`) — replaying
   `CONFIG__ENSEMBLE_MODELS` from the environment and clobbering the CLI value.

Live evidence (canary logs, 2026-07-04):

```
06:11:27 INFO: Updated setting CONFIG.ENSEMBLE_MODELS to: "openai/claude-opus-4-8"
06:12:00 INFO: Running ensemble review with models ['openai/claude-opus-4-8', 'openai/gpt-5.5'], consolidator openai/claude-opus-4-8
```

Every push-triggered `/review -i` and `/improve` runs the full 2-model ensemble + consolidation —
roughly 3x the intended LLM cost/latency per push. The clobbering is a base-branch interaction
(env replay from the `--extra_config_url` feature), not a bug in the ensemble commits themselves, but
the deployment intent is broken today.

**Recommended fix (no code change needed):** use the tool-section keys in push commands, which are not
set by any env var or repo `.pr_agent.toml` and therefore survive the replay:

```
GITHUB_APP__PUSH_COMMANDS=["/review -i --pr_reviewer.ensemble_models=openai/claude-opus-4-8","/improve --pr_code_suggestions.ensemble_models=openai/claude-opus-4-8"]
```

`resolve_ensemble_config` checks the tool section before `config.ensemble_models`, so this achieves the
intended single-model push behavior.

### P2 — Per-tool `ensemble_models=[]` cannot disable the ensemble

`pr_agent/algo/ensemble.py:39` combines levels with
`_parse_models_value(tool_value) or _parse_models_value(global_value)`. An explicit tool-level `[]`
parses to a falsy list and falls through to the global config, so the override documented in
`pr_agent/settings/configuration.toml` (lines 94 and 142: "override config.ensemble_models for
/review only") cannot be used to opt a single tool out while the global ensemble stays on.

**Fix:** distinguish "unset" (`None`) from "explicitly empty" (`[]`) before applying the fallback, e.g.
check `settings.get(...)` for `None` rather than relying on truthiness.

### P2 — A bad member model name aborts the whole `/review` instead of degrading

In `_prepare_prediction_ensemble` (`pr_agent/tools/pr_reviewer.py:250-256`), the per-member
`get_pr_diff(...)` calls run outside any try/except. `get_max_tokens()` raises for any model missing
from `MAX_TOKENS` when `config.custom_model_max_tokens` is unset. One misconfigured entry in
`ensemble_models` therefore kills the entire review (the blanket except in `run()` swallows it and
nothing is published), even though the same model failing at prediction time would be tolerated and
dropped by `gather_ensemble_predictions`.

Mitigated in the current deployment because the Wabot repos set `custom_model_max_tokens=128000`, but
it contradicts the flow's own degradation contract.

**Fix:** wrap each member `get_pr_diff` in try/except and treat failure like the empty-diff case.

### P2 — `/improve` consolidator count-mismatch degrades output while the footer claims success

The consolidation prompt (`pr_code_suggestions_reflect_consolidate_prompts.toml`) tells the model to
keep list length and score near-duplicates 0, but if the consolidator drops entries instead,
`analyze_self_reflection_response`'s length check silently no-ops, `_reflect_and_score`
(`pr_agent/tools/pr_code_suggestions.py:~848`) still returns success, and merged suggestions end up
without `score`/`relevant_lines_start`. The published comment degrades to nearly empty while the
ensemble footer still reads "consolidated by `<model>`".

**Fix:** treat a length mismatch as reflection failure (return `False`, fall back to default scores,
set `ensemble_consolidated=False`).

### P3 — flake8 E402 in `pr_agent/algo/ensemble.py`

`T = TypeVar("T")` sits between the stdlib and `pr_agent` imports, producing E402 on lines 7-9
(confirmed with the repo's flake8 7.3.0). `AGENTS.md` requires flake8-clean commits. Mechanical fix:
move the `TypeVar` below the imports.

### P3 — Misleading "All ensemble models failed" on empty diffs

For a PR whose only changed file is filtered out (observed live: `wabot-v4#298`, `package-lock.json`
only), each member logs "Empty diff" and the flow then logs
`All ensemble models failed, falling back to the standard review flow`, which re-runs `get_pr_diff` a
third time. No models actually failed. Cosmetic, but it pollutes error triage; consider a distinct
"empty diff, skipping ensemble" path/message.

### Non-bug observations (quality/robustness, no action required)

- The consolidator's diff is re-budgeted with all member reviews occupying prompt tokens, so on large
  PRs the consolidator validates findings against a *smaller* diff than the members saw.
- Member predictions ignore `finish_reason == "length"`; a truncated member YAML flows into
  consolidation as-is (only the consolidator call logs truncation).
- The ensemble path never sets `openai.deployment_id` per model, so Azure multi-deployment setups
  would reuse the current deployment for all members (not applicable to the current proxy setup).
- `fallback_models` are bypassed for ensemble members; they only re-enter via the all-members-failed
  fallback to the standard flow.
- Member outputs are embedded between `======` delimiters in the consolidation prompt; content
  containing `======` can escape its block (same injection surface class the base prompts accept for
  the diff itself).
- If the whole ensemble path raises mid-run, `run()` falls back to a full standard-flow regeneration —
  correct, but it doubles LLM cost for that PR.
- The `/improve` ensemble footer is appended before the self-review checkbox/help text (renders
  mid-comment) and is dropped from persistent-comment history entries. Cosmetic.
- Concurrency was explicitly audited: the ensemble module and both tool flows do **not** mutate the
  global Dynaconf settings (no `config.model` swapping), and per-member diffs are computed sequentially
  before fanning out AI calls. No cross-request races originate from the ensemble code.
- Partial-failure semantics are otherwise sound and tested: failed/empty members are dropped with
  warnings, cancellation propagates, a single survivor skips consolidation, consolidator failure falls
  back to the first member's review.

## Coolify Canary Log Analysis (last 14 days)

Container `ahib8a9mgq077qljkszetods-041752390764`, image `cdd265cf`, up 2 weeks. Log level breakdown:
1376 INFO / 70 WARNING / **3 ERROR** / 2178 DEBUG. Overall the service is healthy; ensemble reviews and
code suggestions are running successfully many times per day.

### Errors (3 total)

| When | Error | Assessment |
| --- | --- | --- |
| 07-03 13:02 | `Error getting PR diff fatomate/wabot-v4/298` | PR only changed `package-lock.json` (filtered out) → empty diff. Expected, but see the misleading "All ensemble models failed" cascade above. |
| 07-03 13:04 | `Error extracting tickets error='"message"'` | GitHub 404 on an issue lookup during ticket-compliance check (issue number belongs to another tracker). Pre-existing upstream behavior, cosmetic. |
| 07-03 13:22 | `Failed to parse AI prediction after fallbacks` | Model emitted trailing prose after the fenced YAML (`code_suggestions: []` followed by commentary). Content was an empty suggestion list anyway; single occurrence. |

Additionally, one unhandled ASGI exception on 07-04 00:11: GitHub returned a 504 while constructing the
git provider for the `wabot-v4#301` "opened" webhook, so that event's auto-commands were lost (no
retry). The PR was picked up on subsequent events. Upstream robustness gap, not branch-related.

### Warnings — one recurring actionable item

- **43× `External repo context checkout failed`** — all identical:
  `repo_url=https://github.com/fatomate/wabot_rag.git`, `fatal: couldn't find remote ref master`.
  The external-repo/related-PR config for cross-repo context references ref `master`, but `wabot_rag`'s
  default branch is `main`. Every affected review silently loses its cross-repo context.
  **Fix:** update the consuming repos' `.pr_agent.toml` external repo entries to `ref = "main"` (or
  omit the ref so the checkout uses `HEAD`).
- 13× "No code suggestions found for the PR." — normal.
- 1× `Ensemble model openai/claude-opus-4-8 failed` (single member failure in 14 days; the flow
  degraded correctly).
- Misc issue-ID-not-found warnings for cross-tracker references — cosmetic.

### Ensemble behavior in production

- Dozens of `Running ensemble review/code suggestions with models ['openai/claude-opus-4-8',
  'openai/gpt-5.5'], consolidator openai/claude-opus-4-8` entries; consolidation completing normally.
- Confirmed the P1 finding live: push events log the single-model override being applied and then run
  the two-model ensemble anyway.

## Recommendations (prioritized)

1. **Fix the push-command override now (config-only):** switch `GITHUB_APP__PUSH_COMMANDS` to
   `--pr_reviewer.ensemble_models=...` / `--pr_code_suggestions.ensemble_models=...` on the Coolify
   canary. Immediate ~3x cost/latency reduction on push-triggered runs.
2. **Fix `wabot_rag` external-repo ref** (`master` → `main`) in the consuming repos' `.pr_agent.toml`
   to restore cross-repo context (43 silent failures in 14 days).
3. **Harden `resolve_ensemble_config`** to honor explicit empty per-tool lists (P2) — small code change
   plus a unit test.
4. **Guard member `get_pr_diff` in `_prepare_prediction_ensemble`** so a bad model name degrades
   instead of aborting the review (P2).
5. **Treat `/improve` consolidator length-mismatch as failure** so the footer never claims a
   consolidation that silently degraded (P2).
6. **Mechanical cleanups:** move `T = TypeVar("T")` below the imports (flake8 E402); consider a
   clearer log message for the empty-diff ensemble path; push the local `1ce15b5e` docs commit; update
   the AGENTS.md deployment snapshot (deployed commit is `cdd265cf`, not `2063a917`).

## Resolution Addendum (2026-07-04)

- **Decision:** keep the 2-model ensemble on all triggers (PR-open and push); cost/tokens accepted.
  The P1 push-override issue is therefore moot operationally — the inert `--config.ensemble_models`
  args in `PUSH_COMMANDS` are documented in `AGENTS.md`, and the working tool-section override form is
  documented in `docs/docs/core-abilities/ensemble_review.md` for future use.
- **Fixed (P2):** `resolve_ensemble_config` now honors an explicitly empty tool-level
  `ensemble_models` (`[]`/`""`) as a per-tool opt-out.
- **Fixed (P2):** member `get_pr_diff` failures in `_prepare_prediction_ensemble` are caught and the
  member is skipped, matching the flow's degradation contract.
- **Fixed (P2):** `analyze_self_reflection_response` returns success status; a feedback count mismatch
  is now treated as a failed reflection (default scores, `ensemble_consolidated=False`).
- **Fixed (P3):** `TypeVar` moved below imports (flake8 E402 clean).
- All fixes were written test-first; ensemble suite: 27/27 passing.

## Verification Status

- 23/23 ensemble unit tests pass locally.
- No merge conflicts with `main`.
- Canary healthy: 3 ERROR records in 14 days, container up 2 weeks, all recent ensemble runs completing.
- No secrets were printed during this investigation; Coolify token was read from `.secrets` and piped,
  never echoed.
