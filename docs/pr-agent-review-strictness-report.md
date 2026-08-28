# PR-Agent review strictness on `wabot-dev-oc`

**Date:** 2026-08-28

**Scope:** Live Coolify canary `pr-agent-fork-canary`, Wabot repository configuration, recent automated review output, upstream PR-Agent controls, and first-party Cursor plugin review guidance.

## Executive summary

The review process is long because the deployment is optimized for **recall and repeated feedback**, not for a small merge-blocking signal:

1. `/review` **and** `/improve` run when a PR opens, then `/review -i` **and** `/improve` run again on every push.
2. Wabot repository settings raise `/review` from the upstream default of 3 findings to **6–7**, enable several auxiliary sections, and supply broad instructions that enumerate many performance risks.
3. `/improve` already uses `focus_only_on_problems=true`, but `suggestions_score_threshold=0` publishes every nonzero suggestion. PR-Agent's own scoring rubric classifies scores **3–7** as minor/style/readability/maintainability and reserves **8–10** for critical issues such as major bugs or security concerns.
4. Recent PRs confirm the result: 31 of 32 sampled `/improve` suggestions scored below 8, while push automation produced 19 incremental review comments across four PRs.

**Recommendation:** treat strictness as a **publication threshold**, not deeper analysis. Keep the strong models, ensemble, and repository context, but publish only high-confidence merge blockers. On PR open, retain `/improve` only with a score-8 filter; remove it from push automation. Cap `/review` prompts at 2 findings, simplify its output sections and instructions, and damp repeated incremental reviews.

## Evidence from the live deployment

### Runtime identity and effective base configuration

Read-only inspection of `wabot-dev-oc` found:

- Container: `ahib8a9mgq077qljkszetods-081516380061`
- Image: `ahib8a9mgq077qljkszetods:73f3caee6ac062ff0e1b4b5444b4ad5e3e1bf48d`
- Models: `openai/claude-opus-5` and `openai/gpt-5.6-sol`
- Consolidator: `openai/claude-opus-5`
- Reasoning effort: `high`
- PR-open commands: `/describe`, `/review`, `/improve`
- Push commands: `/review -i`, `/improve`

These live values differ from the repository's 2026-08-23 deployment record (`claude-opus-4-8`, `gpt-5.5`, `xhigh`). The container identity is unchanged, so Coolify configuration drifted after that record. This report uses direct 2026-08-28 runtime inspection as authoritative; the deployment record should be updated separately.

Effective base settings include:

| Setting | Effective value | Effect |
| --- | ---: | --- |
| `pr_reviewer.num_max_findings` | 3 | Upstream-sized cap, but overridden per repository |
| `pr_reviewer.require_tests_review` | `true` | Adds tests output |
| `pr_reviewer.require_estimate_effort_to_review` | `true` | Adds effort output |
| `pr_reviewer.require_security_review` | `true` | Adds security output |
| `pr_reviewer.require_ticket_analysis_review` | `true` | Adds ticket compliance output when ticket context exists |
| `pr_code_suggestions.focus_only_on_problems` | `true` | Correctly avoids general improvement mode |
| `pr_code_suggestions.suggestions_score_threshold` | 0 | Admits all nonzero-scored suggestions |
| `pr_code_suggestions.persistent_comment` | `true` | Accumulates suggestion history in one large comment |
| `pr_code_suggestions.max_history_len` | 4 | Retains several prior push results |

### Repository overrides amplify breadth

The default-branch `.pr_agent.toml` files currently override `/review` as follows:

| Repository | `num_max_findings` | Other breadth-increasing controls |
| --- | ---: | --- |
| `fatomate/wabot_rag` | 6 | tests, security, ticket analysis, split-PR analysis, detailed performance checklist, cross-repo context |
| `fatomate/wabot-backend-v3` | 7 | same pattern; broad backend performance checklist |
| `fatomate/wabot-v4` | 6 | same pattern; broad frontend performance checklist |

The extra instructions are security-aware, but their long lists of possible performance smells prime the reviewer to search exhaustively. They say to flag risks such as loops, scans, indexes, joins, payload fan-out, polling, caching, batching, and transaction boundaries. That is useful for an audit, but it is broader than a merge-blocker gate.

Repository context and related-PR context also give the models more evidence. That can increase finding recall, but it also prevents diff-only false positives. It should **not** be disabled in the first rollout; output filtering is the safer first control.

### Sampled output demonstrates the noise source

A bounded sample of four recent Wabot PRs produced:

| PR | Reviewer comments | Incremental reviewer comments | `/improve` suggestions | Suggestions below 8 | Current `/improve` comment size |
| --- | ---: | ---: | ---: | ---: | ---: |
| [`wabot-backend-v3#431`](https://github.com/fatomate/wabot-backend-v3/pull/431) | 7 | 6 | 9 | 9 | 15,653 chars |
| [`wabot-v4#488`](https://github.com/fatomate/wabot-v4/pull/488) | 13 | 12 | 12 | 11 | 19,485 chars |
| [`wabot-backend-v3#432`](https://github.com/fatomate/wabot-backend-v3/pull/432) | 2 | 1 | 7 | 7 | 11,728 chars |
| [`wabot-v4#490`](https://github.com/fatomate/wabot-v4/pull/490) | 1 | 0 | 4 | 4 | 6,884 chars |
| **Total** | **23** | **19** | **32** | **31 (97%)** | **53,750 chars** |

Examples of published low-score suggestions included brittle test regexes, assertion ordering, cross-platform environment assignment, and deduplication. Some may be useful, but PR-Agent scored them 2–7; they are not a reliable merge-blocking signal.

At the current threshold, the code floors `0` to `1`, so all nonzero suggestions survive. In the fork's ensemble path, member suggestions are merged, the consolidator scores and deduplicates them, and `_merge_predictions_by_score` applies the threshold afterward ([source](../pr_agent/tools/pr_code_suggestions.py)). The score filter therefore governs the final consolidated suggestions. By contrast, `num_max_findings` is enforced through the member and consolidator prompts, not a deterministic post-parse slice; “2 findings” should be treated as the intended model cap unless a hard filter is later added.

## Relevant upstream and Cursor guidance

### PR-Agent already exposes the required controls

Upstream defaults cap `/review` at 3 and expose controls for optional sections, no-finding comments, and incremental review thresholds ([configuration](https://github.com/the-pr-agent/pr-agent/blob/9fbe1ed3bf51eaf055c8a845bdd93cb5275762e3/pr_agent/settings/configuration.toml#L100-L125)).

For `/improve`, upstream provides `focus_only_on_problems` and `suggestions_score_threshold`. The source cautions against setting the threshold above 8 because that can clip highly relevant suggestions ([configuration](https://github.com/the-pr-agent/pr-agent/blob/9fbe1ed3bf51eaf055c8a845bdd93cb5275762e3/pr_agent/settings/configuration.toml#L170-L190)). Score 8 is therefore the strongest supported default without deliberately crossing that warning.

The reflection rubric is explicit:

- scores 8–10: critical issues such as major bugs or security concerns;
- scores 3–7: minor issues, style, readability, or maintainability;
- error-handling or type-checking suggestions should not score above 8 by default.

Source: [reflection prompt](https://github.com/the-pr-agent/pr-agent/blob/9fbe1ed3bf51eaf055c8a845bdd93cb5275762e3/pr_agent/settings/code_suggestions/pr_code_suggestions_reflect_prompts.toml#L19-L34).

PR-Agent also explicitly separates commands run on PR open from commands run on each push ([automation documentation](https://github.com/the-pr-agent/pr-agent/blob/9fbe1ed3bf51eaf055c8a845bdd93cb5275762e3/docs/docs/usage-guide/automations_and_usage.md#L95-L170)). Running `/improve` twice—on open and every push—is a deployment choice, not a requirement.

### Cursor plugin principles

Cursor's first-party plugin repository provides a useful review policy:

- `review-and-ship`: prioritize **correctness, security, and regressions over style-only comments** ([source](https://github.com/cursor/plugins/blob/397c8660da6d3d873a91e18c2ca2f22cac1f0ac1/cursor-team-kit/skills/review-and-ship/SKILL.md#L25-L39)).
- `thermo-nuclear-code-quality-review`: **do not flood** a review with low-value nits; prefer **a smaller number of high-conviction comments** ([source](https://github.com/cursor/plugins/blob/397c8660da6d3d873a91e18c2ca2f22cac1f0ac1/cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md#L155-L167)).
- `pstack/interrogate`: cross-model agreement is high-confidence signal, while lone-model findings are lower confidence; it separates findings into **Act on**, **Consider**, **Noted**, and **Dismissed**, with only “Act on” findings described as real PR blockers ([source](https://github.com/cursor/plugins/blob/397c8660da6d3d873a91e18c2ca2f22cac1f0ac1/pstack/skills/interrogate/SKILL.md#L9-L9), [judgment buckets](https://github.com/cursor/plugins/blob/397c8660da6d3d873a91e18c2ca2f22cac1f0ac1/pstack/skills/interrogate/SKILL.md#L76-L88)).

The transferable idea is not Cursor's intentionally harsh audit rubric. It is the separation between **analysis depth** and **what gets published as blocking feedback**.

## Recommended policy: merge-blocking mode

Automated feedback should publish only:

- **P0:** cross-tenant or auth bypass, exploitable security issue, data loss/corruption, broad outage, or irreversible migration failure.
- **P1:** deterministic correctness, compatibility, or reliability regression on a realistic supported path.

Every published finding must identify:

1. the changed code that introduced it;
2. a concrete trigger or supported execution path;
3. material impact;
4. a concise corrective direction.

Automation should omit style, naming, readability, generic maintainability, speculative optimization, optional hardening, test-shape preferences, and refactors that are merely cleaner. These can remain available through manual `/improve` or human review.

## Recommended configuration

### 1. Change the global automatic commands

Preferred first rollout:

```text
GITHUB_APP__PR_COMMANDS=[
  "/describe --pr_description.final_update_message=false",
  "/review",
  "/improve"
]
GITHUB_APP__PUSH_COMMANDS=[
  "/review -i"
]
```

This keeps one line-anchored `/improve` pass when a PR opens, where the repository-level score-8 filter should make it nearly silent, but removes the repeated two-model `/improve` pass from every push. The sample indicates that only 1 of 32 suggestions would have survived. If the surviving suggestions are still low-value—or the ensemble cost is unjustified—remove `/improve` from `PR_COMMANDS` too.

For `/review -i`, use the repository thresholds below as a middle rung before fully manual review. With `require_all_thresholds_for_incremental_review=false`, a review can run after either 3 new commits or a 30-minute interval, while rapid smaller pushes are skipped. Thresholds suppress events rather than scheduling a later run. A final push that needs immediate review must use full `/review`, or `/review -i --pr_reviewer.minimal_commits_for_incremental_review=0`; plain manual `/review -i` is subject to the same thresholds. Rapid concurrent pushes may also be coalesced by the existing push backlog, but ordinary sequential pushes remain eligible.

### 2. Replace broad repository review settings

Apply consistently to the three Wabot repositories:

```toml
[pr_reviewer]
num_max_findings = 2
require_score_review = false
require_tests_review = false
require_estimate_effort_to_review = false
require_can_be_split_review = false
require_security_review = true
require_ticket_analysis_review = false
publish_output_no_suggestions = true
require_all_thresholds_for_incremental_review = false
minimal_commits_for_incremental_review = 3
minimal_minutes_for_incremental_review = 30
extra_instructions = """
Report only high-confidence merge blockers introduced by this PR:
- P0: security/tenant-isolation bypass, data loss or corruption, outage, or irreversible migration failure.
- P1: deterministic correctness, compatibility, or reliability regression on a realistic supported path.
For every finding, identify the changed code, concrete trigger, material impact, and corrective direction.
Do not report style, naming, readability, generic maintainability, speculative performance, optional hardening,
test-shape preferences, or refactors that are merely cleaner. If no P0/P1 issue is evidenced, return no findings.
For performance, report only a changed operation with a concrete production-scale trigger and material impact.
"""

[pr_code_suggestions]
focus_only_on_problems = true
suggestions_score_threshold = 8
publish_output_no_suggestions = false
max_history_len = 1
```

`num_max_findings=2` is deliberately below upstream's default of 3. It is prompt guidance rather than a hard post-parse limit; if models exceed it in practice, add a deterministic cap in code. `require_security_review=true` is retained because security cannot be simplified away. Lower-value auxiliary sections are removed, which also removes the effort label generated from `require_estimate_effort_to_review`.

Keep `pr_reviewer.publish_output_no_suggestions=true` so a short clean result distinguishes “reviewed with no blockers” from a failed or timed-out review. For `/improve`, silence on no suggestions is acceptable, so its value remains `false`.

Threshold 8 is applied after ensemble consolidation and would have removed 31 of 32 sampled suggestions. It is a high-impact filter, not a perfect “critical-only” guarantee: score 8 can include the top of PR-Agent's error-handling/type-checking band.

These per-repository settings currently override defaults safely. A future `PR_REVIEWER__*` or `PR_CODE_SUGGESTIONS__*` Coolify environment variable would take precedence and should be checked during rollout.

### 3. Keep the ensemble and repository context initially

Do not first solve noise by downgrading models, reducing reasoning, or removing cross-repository context. Those controls affect detection quality and false-positive validation. Output cap, severity rubric, score threshold, and automation cadence are more direct.

The fork's consolidator already treats cross-model agreement as strong evidence and requires lone-model findings to be validated against the diff. A later code-level enhancement could add an explicit P0/P1 severity field and deterministic post-parse filter. Consensus should not be mandatory for security or data-loss findings because models can share blind spots.

## Rollout and measurement

1. **Baseline:** preserve the four-PR sample and classify whether authors accepted, acted on, dismissed, or ignored each existing suggestion; the current report measures model score, not adoption value.
2. **Canary:** apply the config-only policy service-wide to the live canary for 10–20 PRs. This intentionally covers every repository served by this GitHub App; repository settings cannot opt out of the overridden keys.
3. **Shadow audit:** retain `/improve` on PR open at threshold 8 and sample filtered suggestions from logs/artifacts weekly, without publishing them.
4. **Decision:** retain or roll back the service-wide policy based on published signal; evaluate escaped P0/P1 defects over a longer horizon because 10–20 PRs is too small for that low-base-rate metric.

Suggested targets:

- no `/improve` run on push and at most one on PR open;
- no published `/improve` suggestion below score 8;
- a model-targeted maximum of 2 automated `/review` findings per run;
- materially fewer incremental reviewer comments and lower median automated-comment length;
- lower author dismissal rate, with escaped P0/P1 defects monitored over a longer horizon.

## Risks and follow-up

- A score threshold is model judgment, not proof. Measure disposition of the existing sample and audit filtered suggestions before making score 8 permanent.
- At threshold 8, a failed or count-mismatched self-reflection assigns suggestions the fallback score 7 and silently filters all of them. Monitor logs for `using default score 7` and non-consolidated ensemble warnings so reflection failures are not mistaken for a clean `/improve` run.
- Prompt-only severity and finding-count rules can drift. If noisy P2/P3 findings or more than 2 issues persist, add an explicit severity field and deterministic post-parse P0/P1/count filter with tests.
- Disabling auxiliary test/ticket sections removes recurring summaries, not the ability to report a concrete test-related or ticket-breaking P1 regression through the main findings list.
- Incremental thresholds can skip a final push without scheduling a later run. The working manual escape hatch is full `/review`, or `/review -i --pr_reviewer.minimal_commits_for_incremental_review=0`; plain `/review -i` remains gated.
- A successful no-blocker `/review` should remain visible, while an empty `/improve` should remain silent.

## Fable oracle review and disposition

A separate Herdr-managed Pi session ran `cliproxyapi/claude-fable-5` with high reasoning as an independent oracle. Its strongest challenge was that removing all automatic `/improve` discarded the one score-8 signal while making shadow auditing impossible. This report adopted the better threshold-first approach: keep `/improve` once on PR open, remove it from push, and audit filtered output.

The oracle also identified runtime-record drift, fork-specific ensemble semantics, the non-deterministic nature of `num_max_findings`, missing author-disposition evidence, silent clean-review ambiguity, cadence, environment precedence, and effort-label side effects. Those points are now reflected above. Its claim that the live runtime identity was untrustworthy was not accepted: direct read-only inspection on 2026-08-28 is newer than the 2026-08-23 record, but the discrepancy is now explicit.

## Recommendation

Adopt the config-only merge-blocking mode first:

1. keep `/improve` once on PR open at `suggestions_score_threshold=8`, but remove it from `PUSH_COMMANDS`;
2. lower repository `num_max_findings` to 2 and use 3-commit/30-minute incremental-review dampers;
3. replace exhaustive repository instructions with the P0/P1 evidence rubric;
4. disable nonessential `/review` sections, retaining security and a short clean-review result;
5. record author disposition for the current sample and shadow-audit filtered suggestions;
6. measure 10–20 PRs for noise/adoption, then monitor escaped P0/P1 defects over a longer horizon before considering model, context, or code changes.

This is the smallest change that directly addresses the observed problem while preserving the deployment's strong analysis capability and the single highest-scored line-level suggestion channel.

## Deployed canary record

Implemented service-wide on 2026-08-28 because the requested target was the running Coolify canary, not one repository. The environment layer intentionally overrides the listed review and suggestion keys for every repository served by this GitHub App.

- Application: `pr-agent-fork-canary` (`ahib8a9mgq077qljkszetods`)
- Deployment: `qunp4k6dvtbcblo9zsl2xrwn`, finished 2026-08-28 14:04:32 UTC
- Container: `ahib8a9mgq077qljkszetods-135836484725`
- Image/commit: `73f3caee6ac062ff0e1b4b5444b4ad5e3e1bf48d`
- PR-open commands: `/describe`, `/review`, `/improve`
- Push commands: `/review -i` only
- Strictness overrides: the values in the configuration block above, stored as typed `PR_REVIEWER__*` and `PR_CODE_SUGGESTIONS__*` environment variables

Verification confirmed typed effective settings, environment replay after simulated repository overrides, healthy `/` and `/openapi.json`, no startup error markers, and 12–15 file descriptors per Gunicorn process with no leaked temporary TOML files. End-to-end verification then posted `/review` to [`wabot-v4#491`](https://github.com/fatomate/wabot-v4/pull/491): the webhook loaded the repository settings, completed successfully, and published a 446-character clean review with no tests, effort, or ticket sections and a `No security concerns identified` row, versus the earlier 4,448-character review. The request window had zero error, traceback, fallback-score, or ensemble-failure markers.

### Rollback

1. Restore `GITHUB_APP__PUSH_COMMANDS` to `["/review -i --config.ensemble_models=openai/claude-opus-5","/improve --config.ensemble_models=openai/claude-opus-5"]`.
2. Delete the production and preview rows for the newly added `PR_REVIEWER__*` keys plus `PR_CODE_SUGGESTIONS__FOCUS_ONLY_ON_PROBLEMS`, `PR_CODE_SUGGESTIONS__SUGGESTIONS_SCORE_THRESHOLD`, `PR_CODE_SUGGESTIONS__PUBLISH_OUTPUT_NO_SUGGESTIONS`, and `PR_CODE_SUGGESTIONS__MAX_HISTORY_LEN`.
3. Queue a normal Coolify deployment explicitly pinned to `73f3caee6ac062ff0e1b4b5444b4ad5e3e1bf48d`; Dockerfile apps cannot use Coolify restart-only for this change.
4. Verify the restored command list and effective Dynaconf settings in the replacement container.
