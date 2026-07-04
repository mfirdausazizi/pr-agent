# Multi-model ensemble

`/review` and `/improve` can run **several models independently** and have a
**consolidator model** merge their findings into the single published comment.
Different models notice different problems; the consolidator merges overlapping
findings, adjudicates conflicts against the diff, and drops near-duplicates.

The feature is off by default. Enable it with:

```toml
[config]
ensemble_models = ["claude-opus-4-8", "gpt-5.5-2026-04-23"]
ensemble_consolidator_model = "claude-opus-4-8"  # optional; defaults to the first ensemble model
```

`ensemble_models` also accepts a comma-separated string. Both keys can be set per tool under
`[pr_reviewer]` / `[pr_code_suggestions]` (tool keys win over `[config]`), and can be passed per command:

```
/review --config.ensemble_models='["claude-opus-4-8","gpt-5.5-2026-04-23"]'
```

To disable the ensemble for one tool while keeping it globally enabled, set an
explicitly empty tool-level value:

```toml
[config]
ensemble_models = ["claude-opus-4-8", "gpt-5.5-2026-04-23"]

[pr_code_suggestions]
ensemble_models = []  # /improve runs single-model; /review keeps the ensemble
```

!!! note "Server deployments: prefer tool-section CLI overrides"
    In app/webhook deployments, `--config.ensemble_models=...` args in
    `pr_commands`/`push_commands` can be overridden back by a
    `CONFIG__ENSEMBLE_MODELS` environment variable, because environment values
    are re-applied as the highest-precedence layer on each request. Use the
    tool-section form instead, which no env var resets:

    ```
    /review --pr_reviewer.ensemble_models=claude-opus-4-8
    /improve --pr_code_suggestions.ensemble_models=claude-opus-4-8
    ```

## How it works

For `/review`, every ensemble model reviews the PR in parallel (each gets a
diff budgeted for its own context window). The consolidator model then receives
the diff plus all the raw reviews and produces the final review in the standard
format, so labels, persistent comments, and all other `/review` features work
unchanged.

For `/improve`, every ensemble model generates suggestions for the same diff
chunks (chunk boundaries are computed once, with the most token-constrained
model). The merged suggestion pool then goes through a single consolidating
self-reflection per chunk, run by the consolidator model, which scores
suggestions, locates line numbers, and zeroes out near-duplicates across
models — the same reflection call count as a regular `/improve` run, plus the
extra generation calls.

A footer notes which models contributed, e.g.:

> 🧬 **Ensemble**: `claude-opus-4-8` + `gpt-5.5-2026-04-23` · consolidated by `claude-opus-4-8`

## Failure behavior

The ensemble never fails a run that would have succeeded single-model:

- A model that errors out — including at diff-budgeting time (e.g. a model name
  missing from `MAX_TOKENS`) — is dropped (logged); the rest continue.
- If only one model succeeds: for `/review`, its review is used directly and consolidation is skipped; for `/improve`, the consolidating reflection still runs (it doubles as the regular scoring/line-number pass).
- If every ensemble model fails, the run falls back to the standard
  `config.model` + `fallback_models` flow.
- If the `/review` consolidation call fails, the first successful model's
  review is published instead.
- If the `/improve` consolidating reflection returns a feedback list whose
  length doesn't match the suggestions, it is treated as a failed reflection:
  suggestions fall back to a default score and the footer reports
  the degradation instead of claiming a successful consolidation.

## Notes

- Each ensemble model must either be listed in pr-agent's `MAX_TOKENS` table or
  be covered by `config.custom_model_max_tokens`.
- Expect roughly N× the model cost of a single run for N ensemble models, plus
  one consolidation call.
