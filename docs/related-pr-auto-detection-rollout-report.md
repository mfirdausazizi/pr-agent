# PR-Agent Related PR Auto-Detection and Wabot Rollout Report

## Executive Summary

This report documents the full set of changes made during the related PR review rollout. The work started from live
debugging of PR-Agent canary behavior, continued through Wabot repository-specific repo-context configuration, and
ended with a new PR-Agent feature that automatically detects related pull requests from PR descriptions and reviews
their PR heads as external repository context.

| Field | Value |
| --- | --- |
| PR-Agent feature branch | `feature/agentic-repo-access` |
| Deployed commit | `2063a917ff5f2956d97f49e7d40c47705549110a` |
| Commit message | `feat: auto-detect related PR repo context` |
| Coolify canary app | `pr-agent-fork-canary` |
| Coolify app UUID | `ahib8a9mgq077qljkszetods` |
| Canary deployment UUID | `mi4u4qd448imrblj377ixlej` |
| Canary URL | `https://ahib8a9mgq077qljkszetods.149.118.150.110.sslip.io` |
| Running image | `ahib8a9mgq077qljkszetods:2063a917ff5f2956d97f49e7d40c47705549110a` |
| Wabot related PR cap | `max_related_prs = 2` |

## Timeline of Changes

### 1. Auto-review failure investigation

The initial investigation looked at why PR-Agent did not auto-review `fatomate/wabot-backend-v3#58` after a new commit.
GitHub metadata, PR comments, and Coolify logs showed that the GitHub App received the `pull_request.synchronize`
webhook and started `/review -i`, but the review fell back to a large full-PR review and the Gunicorn worker timed out.

Root cause:

- The PR had no previous PR-Agent review suitable for an incremental baseline.
- PR-Agent attempted a large full review.
- The canary worker was killed by Gunicorn timeout before it could post output.

Operational fix:

- Set `GUNICORN_CMD_ARGS=--timeout 600` on the Coolify canary.
- Redeployed/restarted the canary.
- Verified effective runtime timeout and `/openapi.json=200`.

### 2. Wabot repository-specific repo context

Three Wabot repositories were reviewed and configured with repo-specific `.pr_agent.toml` files:

| Repository | Default branch | Role |
| --- | --- | --- |
| `fatomate/wabot_rag` | `master` | RAG and custom Prompt Agent knowledge surfaces |
| `fatomate/wabot-backend-v3` | `production` | Node/Express backend and Prompt Agent APIs |
| `fatomate/wabot-v4` | `main` | Next.js frontend and Prompt Agent UI |

The configs enabled repo context, allowlisted the other two Wabot repositories, bounded context size, and added
reviewer instructions for cross-repo contracts, deployment-order risk, tenant isolation, authentication, data flow, and
performance risks for 1K+ active chatbot instances.

Initial configuration commits:

| Repository | Branch | Commit |
| --- | --- | --- |
| `fatomate/wabot_rag` | `master` | `b5284ad6645f902aa859fb2049778718a625424b` |
| `fatomate/wabot-backend-v3` | `production` | `63f72897d5e69407547247b7f9b7e80f8f8653fc` |
| `fatomate/wabot-v4` | `main` | `0d5b899d0d655dec084cb281f132167450a1b76f` |

### 3. Live repo-context verification before auto-detection

A live review was triggered on `fatomate/wabot_rag#34`. Logs showed:

- Repository settings were applied.
- `repo_context.enabled=true`.
- External backend and frontend repositories were present.
- Ensemble review ran with `openai/claude-opus-4-8` and `openai/gpt-5.5`.
- No structured `ERROR`, `CRITICAL`, exception, or timeout was found for that run.

This proved that static cross-repo context worked, but related PR refs still required manual `/review` overrides.

## New Feature: Automatic Related PR Detection

### Problem

When frontend, backend, and RAG PRs are created together, reviewers need PR-Agent to inspect the related PR branches, not
only each related repository's default branch. Before this change, PR-Agent could not infer related PRs from a PR
description. Users had to manually pass `repo_context.external_repositories` with `refs/pull/<number>/head`.

### Implementation

The new implementation adds automatic related PR detection for `/review` when repo context is enabled and a repository
opts in.

| File | Change |
| --- | --- |
| `pr_agent/algo/repo_context/related_prs.py` | New helper module for parsing related PR links and building external repo configs. |
| `pr_agent/tools/pr_reviewer.py` | Merges detected related PR external repos into the repo-context workspace setup. |
| `pr_agent/settings/configuration.toml` | Adds opt-in defaults for related PR detection. |
| `tests/unittest/test_repo_context_related_prs.py` | Adds parser and allowlist tests. |
| `tests/unittest/test_pr_reviewer_core.py` | Adds reviewer integration tests for related PR merging. |

Behavior:

- Parses full GitHub PR URLs, for example `https://github.com/fatomate/wabot-v4/pull/150`.
- Parses shorthand links, for example `fatomate/wabot-v4#150`.
- Deduplicates matches while preserving source order.
- Skips the current PR if it appears in the description.
- Requires an explicit allowlist entry in `repo_context.allowed_external_repo_urls`.
- Rejects non-`github.com` hosts during canonicalization.
- Caps detected related PRs through `repo_context.max_related_prs`.
- Emits external repo refs as `refs/pull/<number>/head`.
- Replaces a static external repo default-branch entry when a related PR points to the same repo URL.
- Logs `Detected related PR repo context` with the resolved external repositories for traceability.

Default config:

```toml
[repo_context]
include_related_prs=false
max_related_prs=3
```

The feature is disabled by default and must be enabled per repository or deployment configuration.

## Wabot Opt-In Configuration

After the PR-Agent feature was pushed and deployed, all three Wabot repository configs were updated to opt in:

```toml
[repo_context]
include_related_prs = true
max_related_prs = 2
```

| Repository | Branch | Opt-in commit |
| --- | --- | --- |
| `fatomate/wabot_rag` | `master` | `519ebcaa9e916cfe1354dbef32273b0d5f5e2f72` |
| `fatomate/wabot-backend-v3` | `production` | `d422351ee2034c8294ff62bc15a03c04f3483d07` |
| `fatomate/wabot-v4` | `main` | `0e0cd48202275175c93094cac0f5ae26b35897d0` |

Final config verification parsed all three TOML files and confirmed:

```text
fatomate/wabot_rag@master: related=True, max=2, allowed=2
fatomate/wabot-backend-v3@production: related=True, max=2, allowed=2
fatomate/wabot-v4@main: related=True, max=2, allowed=2
```

## Validation Evidence

### Automated tests

Validation was run from the `feature/agentic-repo-access` worktree.

| Check | Result |
| --- | --- |
| Focused related PR tests | `6 passed` |
| Changed-area tests | `105 passed` |
| Lint and whitespace checks | passed |
| `pr_agent/settings/configuration.toml` parse | passed |
| Full unit suite | `661 passed, 1 skipped, 2 warnings` |
| Oracle verification | approved |

The warnings were pre-existing deprecation warnings from unrelated tests.

### Deployment validation

The canary deployment was queued through Coolify and finished successfully.

| Field | Value |
| --- | --- |
| Deployment UUID | `mi4u4qd448imrblj377ixlej` |
| Deployment status | `finished` |
| Commit | `2063a917ff5f2956d97f49e7d40c47705549110a` |
| Runtime image | `ahib8a9mgq077qljkszetods:2063a917ff5f2956d97f49e7d40c47705549110a` |
| Health check | `/openapi.json` returned `200` |
| Gunicorn timeout | `GUNICORN_CMD_ARGS=--timeout 600` |

Runtime import check inside the canary container verified that the new helper resolves a related PR to a pull ref:

```text
[{'name': 'fatomate-wabot-backend-v3-pr-91',
  'url': 'https://github.com/fatomate/wabot-backend-v3.git',
  'ref': 'refs/pull/91/head'}]
include_related_default False
max_related_default 3
```

This confirmed that the deployed code is present and that global defaults remain opt-in.

### Live PR verification

Three interlinked Wabot PRs were updated with related PR links in their descriptions and reviewed through the canary:

| Source PR | Related PRs detected from logs |
| --- | --- |
| `fatomate/wabot-backend-v3#58` | `wabot-v4#150`, `wabot_rag#34` |
| `fatomate/wabot_rag#34` | `wabot-v4#150`, `wabot-backend-v3#58` |
| `fatomate/wabot-v4#150` | `wabot_rag#34`, `wabot-backend-v3#58` |

Coolify log summary:

```text
2026-06-16 03:58:42.839605+00:00 | fatomate/wabot-backend-v3 | fatomate/wabot-v4.git@refs/pull/150/head, fatomate/wabot_rag.git@refs/pull/34/head
2026-06-16 03:59:40.051516+00:00 | fatomate/wabot_rag | fatomate/wabot-v4.git@refs/pull/150/head, fatomate/wabot-backend-v3.git@refs/pull/58/head
2026-06-16 04:00:48.833004+00:00 | fatomate/wabot-v4 | fatomate/wabot_rag.git@refs/pull/34/head, fatomate/wabot-backend-v3.git@refs/pull/58/head
```

Aggregate result:

```text
detections=3
bot_outputs=3
```

The serious error scan found no `ERROR`, `CRITICAL`, `WORKER TIMEOUT`, `Traceback`, `Repo context unavailable`,
`Exception occurred`, or `Worker failed` markers during the review window.

Bot output comments were posted for all three triggered reviews:

| PR | Bot output time |
| --- | --- |
| `wabot-backend-v3#58` | `2026-06-16T04:01:46Z` |
| `wabot_rag#34` | `2026-06-16T04:01:57Z` |
| `wabot-v4#150` | `2026-06-16T04:04:24Z` |

## How to Test Again

### 1. Confirm repository config

```bash
gh api /repos/fatomate/wabot-v4/contents/.pr_agent.toml?ref=main \
  --jq '.content' | base64 -d | grep -E 'include_related_prs|max_related_prs|allowed_external_repo_urls'
```

Expected:

```text
include_related_prs = true
max_related_prs = 2
```

### 2. Add related PR links to an existing PR description

Either format works:

```md
Related Backend PR: fatomate/wabot-backend-v3#58
Related RAG PR: https://github.com/fatomate/wabot_rag/pull/34
```

The related repositories must be allowlisted in `.pr_agent.toml`.

### 3. Trigger review

```md
/review --pr_reviewer.extra_instructions="Test related PR detection. Do a deep cross-repo review using any related PR links in the PR description. Mention contract/API/data-flow compatibility and performance risks for 1K+ active chatbot instances."
```

### 4. Verify canary logs

```bash
ssh wabot-dev-oc '
  c=$(sudo docker ps --format "{{.Names}} {{.Image}}" |
    grep "ahib8a9mgq077qljkszetods:2063a917" | head -1 | cut -d" " -f1)
  sudo docker logs --since 30m "$c" 2>&1 |
    grep -F "Detected related PR repo context"
'
```

Expected:

```text
Detected related PR repo context
refs/pull/<related-pr-number>/head
```

### 5. Check for errors

```bash
ssh wabot-dev-oc '
  c=$(sudo docker ps --format "{{.Names}} {{.Image}}" |
    grep "ahib8a9mgq077qljkszetods:2063a917" | head -1 | cut -d" " -f1)
  sudo docker logs --since 30m "$c" 2>&1 |
    grep -E "\"name\": \"(ERROR|CRITICAL)\"|WORKER TIMEOUT|Traceback|Repo context unavailable|Exception occurred|Worker failed" || true
'
```

Expected: no matches.

## Safety Notes

- Do not shell-source `.secrets`; parse individual values when needed.
- Do not print or paste `COOLIFY_TOKEN`, GitHub tokens, `Authorization` headers, private keys, OpenAI keys, or
  Anthropic keys.
- Related PR detection is off by default in PR-Agent.
- A repository must explicitly opt in with `include_related_prs=true`.
- External repositories must be explicitly allowlisted.
- `max_related_prs` bounds external checkout fan-out.
- Roll back the entire canary by repointing the GitHub App webhook to
  `https://pr-agent.fatomate.com/api/v1/github_webhooks`.
- Disable only this feature by setting `include_related_prs=false` in the affected repository `.pr_agent.toml`.

## Current Status

The related PR auto-detection feature is implemented, tested, pushed to `origin/feature/agentic-repo-access`, deployed
to the Coolify canary, enabled for the three Wabot repositories, and verified on live reviews for backend, RAG, and
frontend PRs.
