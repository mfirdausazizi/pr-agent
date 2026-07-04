# Multi-Model Ensemble for /review and /improve — Design

**Date:** 2026-06-12
**Status:** Approved
**Repo:** mfirdausazizi/pr-agent (fork of The-PR-Agent/pr-agent)

## Goal

Allow `/review` and `/improve` to run **multiple models independently** (e.g.
`claude-opus-4-8` + `gpt-5.5`) and have a **consolidator model** merge their
findings into the single published comment. Single-model behavior must remain
byte-identical when the feature is off (default).

## Constraints & context

- All models are accessed through one OpenAI-compatible endpoint (CLIProxyAPI):
  model names look like `openai/<name>`, keyed by `[openai] key` + `api_base`.
  No per-model endpoint plumbing is needed.
- `BaseAiHandler.chat_completion()` takes `model` per call, so one handler
  instance serves all ensemble models.
- `/improve` line numbers and scores come **only** from the mandatory
  self-reflection call; consolidation must preserve that contract.
- `/improve` already fans out parallel calls per diff chunk via
  `asyncio.gather` and merges with a score-threshold filter.

## Configuration

New keys (defaults shown; empty list = feature off):

```toml
[config]
ensemble_models = []                  # e.g. ["openai/claude-opus-4-8", "openai/gpt-5.5"]
ensemble_consolidator_model = ""      # empty -> defaults to ensemble_models[0]

[pr_reviewer]
# ensemble_models / ensemble_consolidator_model — optional per-tool overrides

[pr_code_suggestions]
# ensemble_models / ensemble_consolidator_model — optional per-tool overrides
```

- Values accepted as TOML list **or** comma-separated string (same parsing as
  `config.fallback_models`).
- Resolution order: tool section → `[config]` → off. Implemented by a new
  helper `resolve_ensemble_config(tool_section: str) -> EnsembleConfig | None`
  in `pr_agent/algo/ensemble.py` (returns `None` when inactive).
- Duplicate model names are de-duplicated preserving order. A 1-element list
  runs that single model and skips consolidation (logged).
- Per-command overrides work with zero new plumbing:
  `/review --config.ensemble_models=...` or `--pr_reviewer.ensemble_models=...`
  (`update_settings_from_args` already applies these request-scoped).
- Models unknown to `MAX_TOKENS` need `config.custom_model_max_tokens`
  (existing mechanism, documented in the new docs page).

## Shared helper module: `pr_agent/algo/ensemble.py`

- `EnsembleConfig` dataclass: `models: list[str]`, `consolidator: str`.
- `resolve_ensemble_config(tool_section)` as above.
- `gather_ensemble_predictions(fn, models)` — `asyncio.gather` over
  `fn(model)` with `return_exceptions=True`; logs per-model failures; returns
  list of `(model, result)` for successes only.
- Footer builder: `ensemble_footer(models_succeeded, consolidator,
  consolidated: bool)` →
  `> 🧬 Ensemble: \`model-a\` + \`model-b\` · consolidated by \`model-c\``
  (or a note when consolidation was skipped/failed).

## /review flow (PRReviewer)

When ensemble is active, `run()` replaces the single
`retry_with_fallback_models(self._prepare_prediction)` call with:

1. **Fan-out:** for each ensemble model, compute its own token-budgeted diff
   (`get_pr_diff(model)`, exactly as the fallback loop does today) and run the
   existing `pr_review_prompt` → one raw YAML review per model. Parallel via
   `gather_ensemble_predictions`.
2. **Degradation ladder:**
   - ≥2 successes → consolidate (step 3).
   - exactly 1 success → use it directly; footer notes consolidation skipped.
   - 0 successes → fall back to the standard single-model path
     (`retry_with_fallback_models`), logged loudly.
3. **Consolidation:** one `chat_completion` to the consolidator model with a
   new prompt (`pr_agent/settings/pr_reviewer_consolidate_prompts.toml`,
   section `[pr_review_consolidate_prompt]`). Inputs: same PR context vars as
   the review prompt, a diff re-budgeted for the consolidator (its
   TokenHandler includes the model reviews in the var budget), and each
   model's review YAML labeled by model name. Output: **the same `$PRReview`
   YAML schema** (conditional sections gated by the same `require_*` vars,
   `num_max_findings` enforced) so `_prepare_pr_review`, markdown rendering,
   labels, and persistent-comment publishing are untouched.
   Prompt instructions: merge overlapping findings (prefer the clearer
   phrasing; union of distinct findings up to `num_max_findings`, ranked by
   severity), adjudicate scalar conflicts (effort, security concerns) with
   judgment grounded in the diff, never invent findings absent from all
   inputs.
4. **Consolidator failure:** publish the first ensemble model's parsed review;
   footer notes consolidation failed.
5. **Footer:** appended to the rendered markdown in `_prepare_pr_review` when
   ensemble ran (all outcomes).

Incremental review (`-i`) and answer mode compose with ensemble unchanged —
they only alter the diff/vars, not the call structure.

## /improve flow (PRCodeSuggestions)

When ensemble is active, `prepare_prediction_main` changes:

1. **Chunking once:** compute `get_pr_multi_diffs` using the *smallest* token
   budget among `ensemble_models ∪ {consolidator}` (helper picks the
   min-`get_max_tokens` model), so every model sees identical chunk
   boundaries.
2. **Refactor:** split `_get_prediction` into generation
   (`_get_suggestions_prediction`) and reflection so they can be recomposed.
   The single-model path keeps current behavior exactly.
3. **Fan-out per chunk:** all ensemble models generate suggestions for the
   chunk in parallel (one big gather over model×chunk). Each parsed
   suggestion is tagged with an internal `source_model` key (stripped from
   published output; ignored by existing renderers).
4. **Consolidating reflection (1 call per chunk, same count as today):** the
   merged per-chunk pool goes to `self_reflect_on_suggestions` with
   `model=consolidator` and `dedicated_prompt="pr_code_suggestions_reflect_consolidate_prompt"`
   (new prompt file `pr_code_suggestions_reflect_consolidate_prompts.toml`;
   the `dedicated_prompt` hook already exists). The prompt does everything
   the current reflect prompt does (score 0–10, line numbers, why) **plus**:
   suggestions that are near-duplicates of an earlier suggestion in the list
   get `suggestion_score: 0` and `why: "duplicate of suggestion N"`. The
   positional one-feedback-per-suggestion contract is preserved, so
   `analyze_self_reflection_response` works with minimal change, and the
   existing merge-time score filter drops the duplicates.
5. **Failure handling:** a failed model×chunk generation is dropped (logged);
   all-models-failed → fall back to the standard path. Reflection failure
   degrades exactly as today (default score 7) — duplicates may then appear;
   logged and noted in footer.
6. **Footer:** appended to the suggestions table (and the
   `publish_output=false` artifact), same builder as /review.

Self-reflection's existing `model_reasoning` preference applies only to the
single-model path; in ensemble mode the consolidator model owns reflection.

## Non-goals (YAGNI)

- Per-model endpoints/keys (everything goes through one OpenAI-compatible
  base).
- Ensemble for other tools (`/describe`, `/ask`, …) — the helper module makes
  later adoption easy, but only /review and /improve change now.
- Per-model raw-output publishing (folded `<details>` sections) — footer
  attribution only.
- Weighted voting/scoring schemes between models — the consolidator model's
  judgment is the merge mechanism.

## Error-handling principles

Ensemble must never make a run fail that would have succeeded single-model:
partial failure degrades, total failure falls back to the standard path.
Existing tenacity retries inside `LiteLLMAIHandler` apply per call. All
degradations are logged via `get_logger()` with model names and visible in the
footer where user-relevant.

## Testing

Unit tests (pytest, `tests/unittest/`, repo's existing patterns:
`ToolClass.__new__` + MagicMock git provider, AsyncMock `chat_completion`,
settings mutation with try/finally restore):

- `resolve_ensemble_config`: off by default; global; per-tool override wins;
  comma-string parsing; dedup; consolidator default = first model.
- `gather_ensemble_predictions`: all succeed / partial fail / all fail.
- /review: 2-model consolidation path produces consolidated YAML through
  `_prepare_pr_review`; 1-success skips consolidation; 0-success falls back;
  consolidator-failure fallback; footer present; **regression** — ensemble
  off ⇒ existing flow unchanged (current tests keep passing).
- /improve: chunk budget = min over models; `source_model` tagging; merged
  pool reflection with consolidator + dedicated prompt; score-0 duplicates
  filtered at merge; reflection-failure default score 7; regression as above.
- Prompt files load through the config loader (section names resolvable via
  `get_settings()`).

## Documentation

- New page `docs/docs/core-abilities/ensemble_review.md` + `mkdocs.yml` nav
  entry (alongside `self_reflection.md`).
- `docs/docs/usage-guide/changing_a_model.md`: ensemble config section with
  an OpenAI-compatible-proxy (CLIProxy-style) recipe.
- Sections in `docs/docs/tools/review.md` and `docs/docs/tools/improve.md`.
- Config keys documented inline in `configuration.toml` comments.
