# Multi-Model Ensemble Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/review` and `/improve` can run multiple models independently and have a consolidator model merge their findings into the single published comment, off by default.

**Architecture:** A small helper module (`pr_agent/algo/ensemble.py`) resolves ensemble config and fans out per-model calls with partial-failure tolerance. `/review` consolidates N raw YAML reviews via a new consolidation prompt that emits the same `$PRReview` schema. `/improve` generates suggestions per model per diff chunk, merges per-chunk pools, and runs ONE consolidator-model reflection per chunk with a dedup-aware prompt (via the existing `dedicated_prompt` hook). Total failure of the ensemble falls back to the existing single-model path.

**Tech Stack:** Python 3.12, Dynaconf settings, Jinja2 prompts (StrictUndefined), litellm AI handler, pytest + pytest-asyncio (`asyncio_mode="auto"`).

**Spec:** `docs/superpowers/specs/2026-06-12-multi-model-ensemble-design.md`

**Run tests with:** `python3 -m pytest tests/unittest -v` (deps: `pip install -r requirements.txt -r requirements-dev.txt`)

---

### Task 1: Ensemble helper module + configuration keys

**Files:**
- Create: `pr_agent/algo/ensemble.py`
- Create: `tests/unittest/test_ensemble.py`
- Modify: `pr_agent/settings/configuration.toml` (lines 10 and 137 areas)

- [ ] **Step 1: Write the failing tests**

Create `tests/unittest/test_ensemble.py`:

```python
import pytest

from pr_agent.algo.ensemble import (EnsembleConfig, ensemble_footer,
                                    gather_ensemble_predictions,
                                    pick_min_budget_model,
                                    resolve_ensemble_config)
from pr_agent.config_loader import get_settings


def test_resolve_ensemble_config_off_by_default():
    assert resolve_ensemble_config("pr_reviewer") is None
    assert resolve_ensemble_config("pr_code_suggestions") is None


def test_resolve_ensemble_config_global_with_default_consolidator_and_dedup():
    settings = get_settings()
    try:
        settings.set("config.ensemble_models", ["model-a", "model-b", "model-a"])
        config = resolve_ensemble_config("pr_reviewer")
        assert config == EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a")
    finally:
        settings.set("config.ensemble_models", None)


def test_resolve_ensemble_config_accepts_comma_string_and_explicit_consolidator():
    settings = get_settings()
    try:
        settings.set("config.ensemble_models", "model-a, model-b")
        settings.set("config.ensemble_consolidator_model", "model-c")
        config = resolve_ensemble_config("pr_code_suggestions")
        assert config == EnsembleConfig(models=["model-a", "model-b"], consolidator="model-c")
    finally:
        settings.set("config.ensemble_models", None)
        settings.set("config.ensemble_consolidator_model", None)


def test_resolve_ensemble_config_tool_section_overrides_global():
    settings = get_settings()
    try:
        settings.set("config.ensemble_models", ["global-a", "global-b"])
        settings.set("pr_reviewer.ensemble_models", ["tool-a", "tool-b"])
        settings.set("pr_reviewer.ensemble_consolidator_model", "tool-c")
        reviewer_config = resolve_ensemble_config("pr_reviewer")
        assert reviewer_config.models == ["tool-a", "tool-b"]
        assert reviewer_config.consolidator == "tool-c"
        # the other tool still resolves the global settings
        suggestions_config = resolve_ensemble_config("pr_code_suggestions")
        assert suggestions_config.models == ["global-a", "global-b"]
        assert suggestions_config.consolidator == "global-a"
    finally:
        settings.set("config.ensemble_models", None)
        settings.set("pr_reviewer.ensemble_models", None)
        settings.set("pr_reviewer.ensemble_consolidator_model", None)


async def test_gather_ensemble_predictions_keeps_successes_and_drops_failures():
    async def fake_fn(model):
        if model == "bad-model":
            raise RuntimeError("boom")
        if model == "empty-model":
            return ""
        return f"prediction-from-{model}"

    results = await gather_ensemble_predictions(
        fake_fn, ["good-a", "bad-model", "empty-model", "good-b"])
    assert results == [("good-a", "prediction-from-good-a"),
                       ("good-b", "prediction-from-good-b")]


def test_pick_min_budget_model_prefers_smallest_known_budget(monkeypatch):
    import pr_agent.algo.ensemble as ensemble_module
    budgets = {"big-model": 100000, "small-model": 32000}

    def fake_get_max_tokens(model):
        if model not in budgets:
            raise ValueError(f"unknown model {model}")
        return budgets[model]

    monkeypatch.setattr(ensemble_module, "get_max_tokens", fake_get_max_tokens)
    assert pick_min_budget_model(["big-model", "small-model", "unknown-model"]) == "small-model"
    # when no budget is known, fall back to the first model
    assert pick_min_budget_model(["unknown-model", "other-unknown"]) == "unknown-model"


def test_ensemble_footer_lists_models_and_consolidator():
    footer = ensemble_footer(["model-a", "model-b"], "model-a", consolidated=True)
    assert "`model-a` + `model-b`" in footer
    assert "consolidated by `model-a`" in footer

    skipped = ensemble_footer(["model-a"], "model-a", consolidated=False)
    assert "consolidation skipped" in skipped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unittest/test_ensemble.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pr_agent.algo.ensemble'`

- [ ] **Step 3: Write the implementation**

Create `pr_agent/algo/ensemble.py`:

```python
import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Tuple

from pr_agent.algo.utils import get_max_tokens
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


@dataclass
class EnsembleConfig:
    """Resolved multi-model ensemble configuration for a single tool run."""
    models: List[str]
    consolidator: str


def _parse_models_value(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        models = [m.strip() for m in value.split(",")]
    else:
        models = [str(m).strip() for m in value]
    deduped = []
    for model in models:
        if model and model not in deduped:
            deduped.append(model)
    return deduped


def resolve_ensemble_config(tool_section: str) -> Optional[EnsembleConfig]:
    """
    Resolve the ensemble configuration for a tool ("pr_reviewer" / "pr_code_suggestions").
    Tool-section keys override [config] keys. Returns None when the feature is off.
    """
    settings = get_settings()
    models = (_parse_models_value(settings.get(f"{tool_section}.ensemble_models", None)) or
              _parse_models_value(settings.get("config.ensemble_models", None)))
    if not models:
        return None
    consolidator = (settings.get(f"{tool_section}.ensemble_consolidator_model", None) or
                    settings.get("config.ensemble_consolidator_model", None) or
                    models[0])
    return EnsembleConfig(models=models, consolidator=str(consolidator).strip())


async def gather_ensemble_predictions(fn: Callable[[str], Awaitable],
                                      models: List[str]) -> List[Tuple[str, object]]:
    """
    Run fn(model) for every model concurrently. Failed or empty predictions are
    dropped (with a warning); returns [(model, result), ...] for successes only.
    """
    results = await asyncio.gather(*[fn(model) for model in models], return_exceptions=True)
    successes = []
    for model, result in zip(models, results):
        if isinstance(result, Exception):
            get_logger().warning(f"Ensemble model {model} failed", artifact={"error": result})
        elif result:
            successes.append((model, result))
        else:
            get_logger().warning(f"Ensemble model {model} returned an empty prediction")
    return successes


def pick_min_budget_model(models: List[str]) -> str:
    """
    Return the model with the smallest known token budget, so diffs/chunks built
    with it fit every model. Models with an unknown budget are skipped; if no
    budget is known, return the first model (downstream code raises a clear error).
    """
    best_model, best_budget = None, None
    for model in models:
        try:
            budget = get_max_tokens(model)
        except Exception:
            get_logger().warning(f"Unknown token budget for ensemble model {model}")
            continue
        if best_budget is None or budget < best_budget:
            best_model, best_budget = model, budget
    return best_model if best_model else models[0]


def ensemble_footer(models: List[str], consolidator: str, consolidated: bool) -> str:
    models_str = " + ".join(f"`{m}`" for m in models)
    if consolidated:
        return f"\n\n> 🧬 **Ensemble**: {models_str} · consolidated by `{consolidator}`\n"
    return f"\n\n> 🧬 **Ensemble**: {models_str} · consolidation skipped\n"
```

- [ ] **Step 4: Add the configuration keys**

In `pr_agent/settings/configuration.toml`, after line 10 (`#model_weak="gpt-5.4-nano" ...`), insert:

```toml
#ensemble_models=["claude-opus-4-8", "gpt-5.5-2026-04-23"] # optional, run /review and /improve with several models independently and consolidate their findings
#ensemble_consolidator_model="claude-opus-4-8" # optional, the model that consolidates ensemble findings; defaults to the first ensemble model
```

In the `[pr_reviewer]` section, directly under the `# general options` keys (after `num_max_findings = 3`), insert:

```toml
#ensemble_models=[] # optional, override config.ensemble_models for /review only
#ensemble_consolidator_model="" # optional, override config.ensemble_consolidator_model for /review only
```

In the `[pr_code_suggestions]` section, after `focus_only_on_problems=true`, insert:

```toml
#ensemble_models=[] # optional, override config.ensemble_models for /improve only
#ensemble_consolidator_model="" # optional, override config.ensemble_consolidator_model for /improve only
```

(All commented out — the feature is off by default; the keys document themselves.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/unittest/test_ensemble.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add pr_agent/algo/ensemble.py tests/unittest/test_ensemble.py pr_agent/settings/configuration.toml
git commit -m "feat: add ensemble helper module and configuration keys"
```

---

### Task 2: /review consolidation prompt

**Files:**
- Create: `pr_agent/settings/pr_reviewer_consolidate_prompts.toml`
- Modify: `pr_agent/config_loader.py:25` (settings_files list)
- Test: `tests/unittest/test_ensemble_prompts.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unittest/test_ensemble_prompts.py`:

```python
from jinja2 import Environment, StrictUndefined

from pr_agent.config_loader import get_settings


def review_consolidate_vars():
    # mirrors PRReviewer.__init__ self.vars plus the consolidation-only variables
    return {
        "title": "test title", "branch": "main", "description": "desc",
        "language": "Python", "diff": "the-diff", "num_pr_files": 1,
        "num_max_findings": 3, "require_score": False, "require_tests": True,
        "require_estimate_effort_to_review": True,
        "require_estimate_contribution_time_cost": False,
        "require_can_be_split_review": False, "require_security_review": True,
        "require_todo_scan": False, "question_str": "", "answer_str": "",
        "extra_instructions": "", "commit_messages_str": "", "custom_labels": "",
        "enable_custom_labels": False, "is_ai_metadata": False,
        "related_tickets": [], "duplicate_prompt_examples": False,
        "date": "2026-06-12",
        "model_reviews": "## Review from model 'model-a':\nreview-a-yaml",
    }


def test_review_consolidate_prompt_is_registered_and_renders():
    settings = get_settings()
    system_template = settings.get("pr_review_consolidate_prompt.system")
    user_template = settings.get("pr_review_consolidate_prompt.user")
    assert system_template
    assert user_template

    environment = Environment(undefined=StrictUndefined)
    variables = review_consolidate_vars()
    rendered_system = environment.from_string(system_template).render(variables)
    rendered_user = environment.from_string(user_template).render(variables)

    assert "$PRReview" in rendered_system
    assert "key_issues_to_review" in rendered_system
    assert "review-a-yaml" in rendered_user
    assert "the-diff" in rendered_user
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unittest/test_ensemble_prompts.py -v`
Expected: FAIL — `settings.get("pr_review_consolidate_prompt.system")` returns `None`

- [ ] **Step 3: Create the prompt file**

Create `pr_agent/settings/pr_reviewer_consolidate_prompts.toml`. The schema and example blocks are copied verbatim from `[pr_review_prompt]` in `pr_agent/settings/pr_reviewer_prompts.toml` so the consolidated output flows through `_prepare_pr_review` unchanged:

````toml
[pr_review_consolidate_prompt]
system="""You are PR-Reviewer-Consolidator, a senior language model responsible for producing the final review of a Git Pull Request (PR).
Several independent AI reviewers have each reviewed the same PR code diff. Your task is to consolidate their findings into a single final review of the highest possible quality.
The review should focus on new code added in the PR code diff (lines starting with '+'), and only on issues introduced by this PR.


The format we will use to present the PR code diff:
======
## File: 'src/file1.py'
{%- if is_ai_metadata %}
### AI-generated changes summary:
* ...
* ...
{%- endif %}


@@ ... @@ def func1():
__new hunk__
11  unchanged code line0
12  unchanged code line1
13 +new code line2 added
14  unchanged code line3
__old hunk__
 unchanged code line0
 unchanged code line1
-old code line2 removed
 unchanged code line3

@@ ... @@ def func2():
__new hunk__
 unchanged code line4
+new code line5 added
 unchanged code line6

## File: 'src/file2.py'
...
======

- In the format above, the diff is organized into separate '__new hunk__' and '__old hunk__' sections for each code chunk. '__new hunk__' contains the updated code, while '__old hunk__' shows the removed code. If no code was removed in a specific chunk, the __old hunk__ section will be omitted.
- We also added line numbers for the '__new hunk__' code, to help you refer to the code lines in your review. These line numbers are not part of the actual code, and should only be used for reference.
- Code lines are prefixed with symbols ('+', '-', ' '). The '+' symbol indicates new code added in the PR, the '-' symbol indicates code removed in the PR, and the ' ' symbol indicates unchanged code.
{%- if is_ai_metadata %}
- If available, an AI-generated summary will appear and provide a high-level overview of the file changes. Note that this summary may not be fully accurate or complete.
{%- endif %}
- When quoting variables, names or file paths from the code, use backticks (`) instead of single quote (').

Consolidation guidelines:
- Findings reported by more than one reviewer are strong candidates for the final review. Merge near-duplicate findings into a single entry, using the clearest phrasing and the most accurate file/line references among the variants.
- A finding reported by only one reviewer may still be included, but only after you validate it against the PR code diff yourself.
- Never invent a finding that does not appear in any of the input reviews.
- Output at most {{ num_max_findings }} key issues, ranked by severity (bugs and security issues first).
- For scalar fields (such as estimated effort or security concerns), adjudicate between conflicting values with your own judgment grounded in the PR code diff; do not average mechanically.
- If the reviewers disagree (e.g., one flags an issue another ignores), check the diff and keep the conclusion best supported by the code.

{%- if extra_instructions %}


Extra instructions from the user:
======
{{ extra_instructions }}
======
{% endif %}


The output must be a YAML object equivalent to type $PRReview, according to the following Pydantic definitions:
=====
{%- if require_can_be_split_review %}
class SubPR(BaseModel):
    relevant_files: List[str] = Field(description="The relevant files of the sub-PR")
    title: str = Field(description="Short and concise title for an independent and meaningful sub-PR, composed only from the relevant files")
{%- endif %}

class KeyIssuesComponentLink(BaseModel):
    relevant_file: str = Field(description="The full file path of the relevant file")
    issue_header: str = Field(description="One or two word title for the issue. For example: 'Possible Bug', etc.")
    issue_content: str = Field(description="A short and concise description of the issue, why it matters, and the specific scenario or input that triggers it. Do not mention line numbers in this field.")
    start_line: int = Field(description="The start line that corresponds to this issue in the relevant file")
    end_line: int = Field(description="The end line that corresponds to this issue in the relevant file")

{%- if require_todo_scan %}
class TodoSection(BaseModel):
    relevant_file: str = Field(description="The full path of the file containing the TODO comment")
    line_number: int = Field(description="The line number where the TODO comment starts")
    content: str = Field(description="The content of the TODO comment. Only include actual TODO comments within code comments (e.g., comments starting with '#', '//', '/*', '<!--', ...).  Remove leading 'TODO' prefixes. If more than 10 words, summarize the TODO comment to a single short sentence up to 10 words.")
{%- endif %}

{%- if related_tickets %}

class TicketCompliance(BaseModel):
    ticket_url: str = Field(description="Ticket URL or ID")
    ticket_requirements: str = Field(description="Repeat, in your own words (in bullet points), all the requirements, sub-tasks, DoD, and acceptance criteria raised by the ticket")
    fully_compliant_requirements: str = Field(description="Bullet-point list of items from the  'ticket_requirements' section above that are fulfilled by the PR code. Don't explain how the requirements are met, just list them shortly. Can be empty")
    not_compliant_requirements: str = Field(description="Bullet-point list of items from the 'ticket_requirements' section above that are not fulfilled by the PR code. Don't explain how the requirements are not met, just list them shortly. Can be empty")
    requires_further_human_verification: str = Field(description="Bullet-point list of items from the 'ticket_requirements' section above that cannot be assessed through code review alone, are unclear, or need further human review (e.g., browser testing, UI checks). Leave empty if all 'ticket_requirements' were marked as fully compliant or not compliant")
{%- endif %}

{%- if require_estimate_contribution_time_cost %}

class ContributionTimeCostEstimate(BaseModel):
    best_case: str = Field(description="An expert in the relevant technology stack, with no unforeseen issues or bugs during the work.", examples=["45m", "5h", "30h"])
    average_case: str = Field(description="A senior developer with only brief familiarity with this specific technology stack, and no major unforeseen issues.", examples=["45m", "5h", "30h"])
    worst_case: str = Field(description="A senior developer with no prior experience in this specific technology stack, requiring significant time for research, debugging, or resolving unexpected errors.", examples=["45m", "5h", "30h"])
{%- endif %}

class Review(BaseModel):
{%- if related_tickets %}
    ticket_compliance_check: List[TicketCompliance] = Field(description="A list of compliance checks for the related tickets")
{%- endif %}
{%- if require_estimate_effort_to_review %}
    estimated_effort_to_review_[1-5]: int = Field(description="Estimate, on a scale of 1-5 (inclusive), the time and effort required to review this PR by an experienced and knowledgeable developer. 1 means short and easy review, 5 means long and hard review. Take into account the size, complexity, quality, and the needed changes of the PR code diff.")
{%- endif %}
{%- if require_estimate_contribution_time_cost %}
    contribution_time_cost_estimate: ContributionTimeCostEstimate = Field(description="An estimate of the time required to implement the changes, based on the quantity, quality, and complexity of the contribution, as well as the context from the PR description and commit messages.")
{%- endif %}
{%- if require_score %}
    score: str = Field(description="Rate this PR on a scale of 0-100 (inclusive), where 0 means the worst possible PR code, and 100 means PR code of the highest quality, without any bugs or performance issues, that is ready to be merged immediately and run in production at scale.")
{%- endif %}
{%- if require_tests %}
    relevant_tests: str = Field(description="yes/no question: does this PR have relevant tests added or updated?")
{%- endif %}
{%- if question_str %}
    insights_from_user_answers: str = Field(description="shortly summarize the insights you gained from the user's answers to the questions")
{%- endif %}
    key_issues_to_review: List[KeyIssuesComponentLink] = Field("A concise list (0-{{ num_max_findings }} issues) of bugs, security vulnerabilities, or significant performance concerns introduced in this PR. Only include issues you are confident about. If confidence is limited but the potential impact is high (e.g., data loss, security), you may include it only if you explicitly note what remains uncertain. Each issue must identify a concrete problem with a realistic trigger scenario. An empty list is acceptable if no clear issues are found.")
{%- if require_security_review %}
    security_concerns: str = Field(description="Does this PR code introduce vulnerabilities such as exposure of sensitive information (e.g., API keys, secrets, passwords), or security concerns like SQL injection, XSS, CSRF, and others? Answer 'No' (without explaining why) if there are no possible issues. If there are security concerns or issues, start your answer with a short header, such as: 'Sensitive information exposure: ...', 'SQL injection: ...', etc. Explain your answer. Be specific and give examples if possible")
{%- endif %}
{%- if require_todo_scan %}
    todo_sections: Union[List[TodoSection], str] = Field(description="A list of TODO comments found in the PR code. Return 'No' (as a string) if there are no TODO comments in the PR")
{%- endif %}
{%- if require_can_be_split_review %}
    can_be_split: List[SubPR] = Field(min_items=0, max_items=3, description="Can this PR, which contains {{ num_pr_files }} changed files in total, be divided into smaller sub-PRs with distinct tasks that can be reviewed and merged independently, regardless of the order? Make sure that the sub-PRs are indeed independent, with no code dependencies between them, and that each sub-PR represents a meaningful independent task. Output an empty list if the PR code does not need to be split.")
{%- endif %}

class PRReview(BaseModel):
    review: Review
=====


Example output:
```yaml
review:
{%- if related_tickets %}
  ticket_compliance_check:
    - ticket_url: |
        ...
      ticket_requirements: |
        ...
      fully_compliant_requirements: |
        ...
      not_compliant_requirements: |
        ...
      overall_compliance_level: |
        ...
{%- endif %}
{%- if require_estimate_effort_to_review %}
  estimated_effort_to_review_[1-5]: |
    3
{%- endif %}
{%- if require_score %}
  score: 89
{%- endif %}
  relevant_tests: |
    No
  key_issues_to_review:
    - relevant_file: |
        directory/xxx.py
      issue_header: |
        Possible Bug
      issue_content: |
        ...
      start_line: 12
      end_line: 14
    - ...
  security_concerns: |
    No
{%- if require_todo_scan %}
  todo_sections: |
    No
{%- endif %}
{%- if require_can_be_split_review %}
  can_be_split:
  - relevant_files:
    - ...
    - ...
    title: ...
  - ...
{%- endif %}
{%- if require_estimate_contribution_time_cost %}
  contribution_time_cost_estimate:
    best_case: |
      ...
    average_case: |
      ...
    worst_case: |
      ...
{%- endif %}
```

Answer should be a valid YAML, and nothing else. Each YAML output MUST be after a newline, with proper indent, and block scalar indicator ('|')
"""

user="""
{%- if related_tickets %}
--PR Ticket Info--
{%- for ticket in related_tickets %}
=====
Ticket URL: '{{ ticket.ticket_url }}'

Ticket Title: '{{ ticket.title }}'

{%- if ticket.labels %}

Ticket Labels: {{ ticket.labels }}

{%- endif %}
{%- if ticket.body %}

Ticket Description:
#####
{{ ticket.body }}
#####
{%- endif %}
=====
{% endfor %}
{%- endif %}


--PR Info--
{%- if date %}

Today's Date: {{date}}
{%- endif %}

Title: '{{title}}'

Branch: '{{branch}}'

{%- if description %}

PR Description:
======
{{ description|trim }}
======
{%- endif %}

{%- if question_str %}

=====
Here are questions to better understand the PR. Use the answers to provide better feedback.

{{ question_str|trim }}

User answers:
'
{{ answer_str|trim }}
'
=====
{%- endif %}


The PR code diff:
======
{{ diff|trim }}
======


The reviews produced by the independent AI reviewers:
======
{{ model_reviews|trim }}
======


Response (should be a valid YAML, and nothing else):
```yaml
"""
````

- [ ] **Step 4: Register the prompt file**

In `pr_agent/config_loader.py`, in the `settings_files` list, after the line `"settings/pr_reviewer_prompts.toml",` add:

```python
        "settings/pr_reviewer_consolidate_prompts.toml",
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/unittest/test_ensemble_prompts.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pr_agent/settings/pr_reviewer_consolidate_prompts.toml pr_agent/config_loader.py tests/unittest/test_ensemble_prompts.py
git commit -m "feat: add /review ensemble consolidation prompt"
```

---

### Task 3: /review ensemble flow

**Files:**
- Modify: `pr_agent/tools/pr_reviewer.py` (imports; `__init__` ~line 65; `run()` line 155; `_get_prediction` lines 203-227; `_prepare_pr_review` end ~line 276; new methods after `_get_prediction`)
- Test: `tests/unittest/test_pr_reviewer_ensemble.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unittest/test_pr_reviewer_ensemble.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pr_agent.tools.pr_reviewer as pr_reviewer_module
from pr_agent.algo.ensemble import EnsembleConfig
from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.tools.pr_reviewer import PRReviewer


def full_review_vars():
    return {
        "title": "test title", "branch": "main", "description": "desc",
        "language": "Python", "diff": "", "num_pr_files": 1,
        "num_max_findings": 3, "require_score": False, "require_tests": True,
        "require_estimate_effort_to_review": True,
        "require_estimate_contribution_time_cost": False,
        "require_can_be_split_review": False, "require_security_review": True,
        "require_todo_scan": False, "question_str": "", "answer_str": "",
        "extra_instructions": "", "commit_messages_str": "", "custom_labels": "",
        "enable_custom_labels": False, "is_ai_metadata": False,
        "related_tickets": [], "duplicate_prompt_examples": False,
        "date": "2026-06-12",
    }


def build_reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.pr_url = "https://example.com/pr/1"
    reviewer.vars = full_review_vars()
    reviewer.token_handler = MagicMock()
    reviewer.ai_handler = MagicMock()
    reviewer.prediction = None
    reviewer.patches_diff = None
    reviewer.incremental = IncrementalPR(False)
    reviewer.ensemble_models_used = []
    reviewer.ensemble_consolidator = ""
    reviewer.ensemble_consolidated = False
    return reviewer


async def test_ensemble_consolidates_two_predictions(monkeypatch):
    reviewer = build_reviewer()
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", lambda *args, **kwargs: "diff")
    reviewer._get_prediction_for_diff = AsyncMock(side_effect=["review-a", "review-b"])
    reviewer._consolidate_predictions = AsyncMock(return_value="consolidated-review")

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    assert reviewer.prediction == "consolidated-review"
    assert reviewer.ensemble_models_used == ["model-a", "model-b"]
    assert reviewer.ensemble_consolidated is True
    consolidate_args = reviewer._consolidate_predictions.call_args.args
    assert consolidate_args[0] == [("model-a", "review-a"), ("model-b", "review-b")]
    assert consolidate_args[1] == "model-a"


async def test_ensemble_single_success_skips_consolidation(monkeypatch):
    reviewer = build_reviewer()
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", lambda *args, **kwargs: "diff")

    async def member(model):
        if model == "model-a":
            raise RuntimeError("boom")
        return "review-b"

    reviewer._get_ensemble_member_prediction = member
    reviewer._consolidate_predictions = AsyncMock()

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    assert reviewer.prediction == "review-b"
    assert reviewer.ensemble_models_used == ["model-b"]
    assert reviewer.ensemble_consolidated is False
    reviewer._consolidate_predictions.assert_not_awaited()


async def test_ensemble_falls_back_to_standard_flow_when_all_models_fail(monkeypatch):
    reviewer = build_reviewer()

    async def fail(model):
        raise RuntimeError("boom")

    reviewer._get_ensemble_member_prediction = fail
    fallback = AsyncMock()
    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fallback)

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    fallback.assert_awaited_once()
    assert reviewer.ensemble_models_used == []


async def test_ensemble_uses_first_review_when_consolidation_fails():
    reviewer = build_reviewer()
    predictions = {"model-a": "review-a", "model-b": "review-b"}

    async def member(model):
        return predictions[model]

    reviewer._get_ensemble_member_prediction = member
    reviewer._consolidate_predictions = AsyncMock(side_effect=RuntimeError("boom"))

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    assert reviewer.prediction == "review-a"
    assert reviewer.ensemble_consolidated is False


async def test_consolidate_predictions_renders_prompts_and_returns_response(monkeypatch):
    reviewer = build_reviewer()
    reviewer.ai_handler.chat_completion = AsyncMock(return_value=("consolidated-yaml", "stop"))
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", lambda *args, **kwargs: "the-diff")
    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", lambda *args, **kwargs: MagicMock())

    result = await reviewer._consolidate_predictions(
        [("model-a", "review-a"), ("model-b", "review-b")], "model-c")

    assert result == "consolidated-yaml"
    kwargs = reviewer.ai_handler.chat_completion.call_args.kwargs
    assert kwargs["model"] == "model-c"
    assert "review-a" in kwargs["user"]
    assert "review-b" in kwargs["user"]
    assert "the-diff" in kwargs["user"]
    assert "$PRReview" in kwargs["system"]


async def test_prepare_pr_review_appends_footer_when_ensemble(monkeypatch):
    reviewer = build_reviewer()
    reviewer.prediction = "review:\n  estimated_effort_to_review_[1-5]: 2\n"
    reviewer.ensemble_models_used = ["model-a", "model-b"]
    reviewer.ensemble_consolidator = "model-a"
    reviewer.ensemble_consolidated = True
    monkeypatch.setattr(pr_reviewer_module, "convert_to_markdown_v2", lambda *args, **kwargs: "MD")
    reviewer.set_review_labels = MagicMock()
    reviewer.git_provider.is_supported.return_value = False

    out = reviewer._prepare_pr_review()

    assert out.startswith("MD")
    assert "consolidated by `model-a`" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unittest/test_pr_reviewer_ensemble.py -v`
Expected: FAIL with `AttributeError` (`_prepare_prediction_ensemble` / `_get_prediction_for_diff` not defined)

- [ ] **Step 3: Implement in `pr_agent/tools/pr_reviewer.py`**

3a. Add the import (after the `pr_agent.algo.ai_handlers.litellm_ai_handler` import at line 11):

```python
from pr_agent.algo.ensemble import (EnsembleConfig, ensemble_footer,
                                    gather_ensemble_predictions,
                                    resolve_ensemble_config)
```

3b. In `__init__`, after `self.prediction = None` (line 65), add:

```python
        self.ensemble_models_used = []
        self.ensemble_consolidator = ""
        self.ensemble_consolidated = False
```

3c. In `run()`, replace line 155:

```python
            await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
```

with:

```python
            ensemble_config = resolve_ensemble_config("pr_reviewer")
            if ensemble_config:
                await self._prepare_prediction_ensemble(ensemble_config)
            else:
                await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
```

3d. Replace `_get_prediction` (lines 203-227) with a thin wrapper plus a diff-parameterized variant (the body is unchanged apart from taking `patches_diff` as an argument):

```python
    async def _get_prediction(self, model: str) -> str:
        return await self._get_prediction_for_diff(model, self.patches_diff)

    async def _get_prediction_for_diff(self, model: str, patches_diff: str) -> str:
        """
        Generate an AI prediction for the pull request review.

        Args:
            model: A string representing the AI model to be used for the prediction.
            patches_diff: The token-budgeted PR diff to review.

        Returns:
            A string representing the AI prediction for the pull request review.
        """
        variables = copy.deepcopy(self.vars)
        variables["diff"] = patches_diff  # update diff

        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_review_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_review_prompt.user).render(variables)

        response, finish_reason = await self.ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt
        )

        return response
```

3e. Add the ensemble methods directly after `_get_prediction_for_diff`:

```python
    async def _prepare_prediction_ensemble(self, ensemble: EnsembleConfig) -> None:
        get_logger().info(f"Running ensemble review with models {ensemble.models}, "
                          f"consolidator {ensemble.consolidator}")
        predictions = await gather_ensemble_predictions(self._get_ensemble_member_prediction,
                                                        ensemble.models)
        if not predictions:
            get_logger().warning("All ensemble models failed, falling back to the standard review flow")
            await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            return

        self.ensemble_models_used = [model for model, _ in predictions]
        self.ensemble_consolidator = ensemble.consolidator
        if len(predictions) == 1:
            get_logger().info("Single ensemble prediction available, skipping consolidation")
            self.prediction = predictions[0][1]
            return

        try:
            self.prediction = await self._consolidate_predictions(predictions, ensemble.consolidator)
            self.ensemble_consolidated = True
        except Exception as e:
            get_logger().warning(f"Ensemble consolidation with {ensemble.consolidator} failed, "
                                 f"using the review from {predictions[0][0]}", artifact={"error": e})
            self.prediction = predictions[0][1]

    async def _get_ensemble_member_prediction(self, model: str) -> str:
        patches_diff = get_pr_diff(self.git_provider,
                                   self.token_handler,
                                   model,
                                   add_line_numbers_to_hunks=True,
                                   disable_extra_lines=False,)
        if not patches_diff:
            get_logger().warning(f"Empty diff for PR: {self.pr_url} (ensemble model {model})")
            return ""
        return await self._get_prediction_for_diff(model, patches_diff)

    async def _consolidate_predictions(self, predictions: List[Tuple[str, str]],
                                       consolidator: str) -> str:
        model_reviews = ""
        for model, prediction in predictions:
            model_reviews += f"## Review from model '{model}':\n======\n{prediction.strip()}\n======\n\n"

        variables = copy.deepcopy(self.vars)
        variables["model_reviews"] = model_reviews
        # the consolidation token handler accounts for the model reviews, so the
        # diff is budgeted to fit alongside them
        token_handler = TokenHandler(self.git_provider.pr,
                                     variables,
                                     get_settings().pr_review_consolidate_prompt.system,
                                     get_settings().pr_review_consolidate_prompt.user)
        patches_diff = get_pr_diff(self.git_provider,
                                   token_handler,
                                   consolidator,
                                   add_line_numbers_to_hunks=True,
                                   disable_extra_lines=False,)
        variables["diff"] = patches_diff

        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(
            get_settings().pr_review_consolidate_prompt.system).render(variables)
        user_prompt = environment.from_string(
            get_settings().pr_review_consolidate_prompt.user).render(variables)
        response, finish_reason = await self.ai_handler.chat_completion(
            model=consolidator,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt
        )
        if not response or not response.strip():
            raise Exception("Empty consolidation response")
        return response
```

3f. In `_prepare_pr_review`, replace the ending (lines 276-279):

```python
        if markdown_text == None or len(markdown_text) == 0:
            markdown_text = ""

        return markdown_text
```

with:

```python
        if markdown_text == None or len(markdown_text) == 0:
            markdown_text = ""

        if markdown_text and self.ensemble_models_used:
            markdown_text += ensemble_footer(self.ensemble_models_used,
                                             self.ensemble_consolidator,
                                             self.ensemble_consolidated)

        return markdown_text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unittest/test_pr_reviewer_ensemble.py tests/unittest/test_pr_reviewer_core.py -v`
Expected: all PASS (including the pre-existing reviewer tests — regression check)

- [ ] **Step 5: Commit**

```bash
git add pr_agent/tools/pr_reviewer.py tests/unittest/test_pr_reviewer_ensemble.py
git commit -m "feat: multi-model ensemble flow for /review"
```

---

### Task 4: /improve refactor (no behavior change)

Split `_get_prediction` into generation + reflection helpers and extract chunk-building and score-merge so the ensemble path can recompose them.

**Files:**
- Modify: `pr_agent/tools/pr_code_suggestions.py` (`_get_prediction` lines 386-420; `prepare_prediction_main` lines 670-734)
- Test: existing `tests/unittest/test_pr_code_suggestions_core.py` must keep passing

- [ ] **Step 1: Replace `_get_prediction` (lines 386-420)**

```python
    async def _get_suggestions_prediction(self, model: str, patches_diff: str,
                                          patches_diff_no_line_number: str) -> dict:
        variables = copy.deepcopy(self.vars)
        variables["diff"] = patches_diff  # update diff
        variables["diff_no_line_numbers"] = patches_diff_no_line_number  # update diff
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(self.pr_code_suggestions_prompt_system).render(variables)
        user_prompt = environment.from_string(self.pr_code_suggestions_prompt_user).render(variables)
        response, finish_reason = await self.ai_handler.chat_completion(
            model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)
        if not get_settings().config.publish_output:
            get_settings().system_prompt = system_prompt
            get_settings().user_prompt = user_prompt

        # load suggestions from the AI response
        return self._prepare_pr_code_suggestions(response)

    async def _reflect_and_score(self, data: dict, patches_diff: str, model: str,
                                 dedicated_prompt: str = "") -> bool:
        response_reflect = await self.self_reflect_on_suggestions(data["code_suggestions"],
                                                                  patches_diff, model=model,
                                                                  dedicated_prompt=dedicated_prompt)
        if response_reflect:
            await self.analyze_self_reflection_response(data, response_reflect)
            return True
        # get_logger().error(f"Could not self-reflect on suggestions. using default score 7")
        for i, suggestion in enumerate(data["code_suggestions"]):
            suggestion["score"] = 7
            suggestion["score_why"] = ""
        return False

    async def _get_prediction(self, model: str, patches_diff: str, patches_diff_no_line_number: str) -> dict:
        data = await self._get_suggestions_prediction(model, patches_diff, patches_diff_no_line_number)

        # self-reflect on suggestions (mandatory, since line numbers are generated now here)
        model_reflect_with_reasoning = get_model('model_reasoning')
        fallbacks = get_settings().config.fallback_models
        if model_reflect_with_reasoning == get_settings().config.model and model != get_settings().config.model and fallbacks and model == \
                fallbacks[0]:
            # we are using a fallback model (should not happen on regular conditions)
            get_logger().warning(f"Using the same model for self-reflection as the one used for suggestions")
            model_reflect_with_reasoning = model
        await self._reflect_and_score(data, patches_diff, model_reflect_with_reasoning)

        return data
```

- [ ] **Step 2: Extract chunk-building and merge from `prepare_prediction_main` (lines 670-734)**

Replace the whole method with:

```python
    async def _build_diff_chunks(self, model: str) -> None:
        if get_settings().pr_code_suggestions.decouple_hunks:
            self.patches_diff_list = get_pr_multi_diffs(self.git_provider,
                                                        self.token_handler,
                                                        model,
                                                        max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                        add_line_numbers=True)  # decouple hunk with line numbers
            self.patches_diff_list_no_line_numbers = self.remove_line_numbers(self.patches_diff_list)  # decouple hunk

        else:
            # non-decoupled hunks
            self.patches_diff_list_no_line_numbers = get_pr_multi_diffs(self.git_provider,
                                                                        self.token_handler,
                                                                        model,
                                                                        max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                                        add_line_numbers=False)
            self.patches_diff_list = await self.convert_to_decoupled_with_line_numbers(
                self.patches_diff_list_no_line_numbers, model)
            if not self.patches_diff_list:
                # fallback to decoupled hunks
                self.patches_diff_list = get_pr_multi_diffs(self.git_provider,
                                                            self.token_handler,
                                                            model,
                                                            max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                            add_line_numbers=True)  # decouple hunk with line numbers

    def _merge_predictions_by_score(self, prediction_list: List[dict]) -> dict:
        data = {"code_suggestions": []}
        for j, predictions in enumerate(prediction_list):  # each call adds an element to the list
            if predictions and "code_suggestions" in predictions:
                score_threshold = max(1, int(get_settings().pr_code_suggestions.suggestions_score_threshold))
                for i, prediction in enumerate(predictions["code_suggestions"]):
                    try:
                        score = int(prediction.get("score", 1))
                        if score >= score_threshold:
                            data["code_suggestions"].append(prediction)
                        else:
                            get_logger().info(
                                f"Removing suggestions {i} from call {j}, because score is {score}, and score_threshold is {score_threshold}",
                                artifact=prediction)
                    except Exception as e:
                        get_logger().error(f"Error getting PR diff for suggestion {i} in call {j}, error: {e}",
                                           artifact={"prediction": prediction})
        return data

    async def prepare_prediction_main(self, model: str) -> dict:
        # get PR diff
        await self._build_diff_chunks(model)

        if self.patches_diff_list:
            get_logger().info(f"Number of PR chunk calls: {len(self.patches_diff_list)}")
            get_logger().debug(f"PR diff:", artifact=self.patches_diff_list)

            # parallelize calls to AI:
            if get_settings().pr_code_suggestions.parallel_calls:
                prediction_list = await asyncio.gather(
                    *[self._get_prediction(model, patches_diff, patches_diff_no_line_numbers) for
                      patches_diff, patches_diff_no_line_numbers in
                      zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers)])
                self.prediction_list = prediction_list
            else:
                prediction_list = []
                for patches_diff, patches_diff_no_line_numbers in zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers):
                    prediction = await self._get_prediction(model, patches_diff, patches_diff_no_line_numbers)
                    prediction_list.append(prediction)

            self.data = data = self._merge_predictions_by_score(prediction_list)
        else:
            get_logger().warning(f"Empty PR diff list")
            self.data = data = None
        return data
```

- [ ] **Step 3: Run the existing suite to verify no regression**

Run: `python3 -m pytest tests/unittest/test_pr_code_suggestions_core.py tests/unittest -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add pr_agent/tools/pr_code_suggestions.py
git commit -m "refactor: split /improve generation, reflection, chunking and merge helpers"
```

---

### Task 5: /improve consolidating-reflection prompt

**Files:**
- Create: `pr_agent/settings/code_suggestions/pr_code_suggestions_reflect_consolidate_prompts.toml`
- Modify: `pr_agent/config_loader.py` (settings_files list)
- Test: `tests/unittest/test_ensemble_prompts.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/unittest/test_ensemble_prompts.py`)

```python
def test_reflect_consolidate_prompt_is_registered_and_renders():
    settings = get_settings()
    system_template = settings.get("pr_code_suggestions_reflect_consolidate_prompt.system")
    user_template = settings.get("pr_code_suggestions_reflect_consolidate_prompt.user")
    assert system_template
    assert user_template

    # mirrors the variables dict built by self_reflect_on_suggestions
    variables = {"suggestion_list": [], "suggestion_str": "suggestion 1: {...}",
                 "diff": "the-diff", "num_code_suggestions": 1,
                 "prev_suggestions_str": "", "is_ai_metadata": False,
                 "duplicate_prompt_examples": False}
    environment = Environment(undefined=StrictUndefined)
    rendered_system = environment.from_string(system_template).render(variables)
    rendered_user = environment.from_string(user_template).render(variables)

    assert "duplicate" in rendered_system.lower()
    assert "source_model" in rendered_system
    assert "suggestion 1" in rendered_user
    assert "the-diff" in rendered_user
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unittest/test_ensemble_prompts.py -v`
Expected: the new test FAILS (`None` template)

- [ ] **Step 3: Create the prompt file**

Create `pr_agent/settings/code_suggestions/pr_code_suggestions_reflect_consolidate_prompts.toml`. It is the existing reflect prompt (`pr_code_suggestions_reflect_prompts.toml`) with a multi-model intro and deduplication rules added; the output schema is identical so `analyze_self_reflection_response` works unchanged:

````toml
[pr_code_suggestions_reflect_consolidate_prompt]
system="""You are an AI language model specialized in reviewing, evaluating and consolidating code suggestions for a Pull Request (PR).
Your task is to analyze a PR code diff and evaluate the correctness and importance of a set of AI-generated code suggestions.
The suggestions were generated by MULTIPLE independent AI models reviewing the same PR; each suggestion may include a 'source_model' field naming the model that produced it. Different models often produce near-duplicate suggestions for the same underlying issue.
In addition to evaluating the suggestion correctness and importance, two more sub-tasks you have are: (1) detect the line numbers in the '__new hunk__' of the PR code diff section that correspond to the 'existing_code' snippet, and (2) deduplicate near-duplicate suggestions across models.

Deduplication rules:
- Two suggestions are near-duplicates if they address the same underlying issue at the same code location, even when worded differently or with slightly different code snippets.
- When you detect near-duplicates, keep the single best variant (clearest explanation, most accurate 'improved_code') and assign ALL other variants a suggestion_score of 0, stating in the 'why' field that the suggestion duplicates suggestion N.
- Prefer keeping the earliest occurrence, unless a later variant is clearly better.
- The 'source_model' field is informational only; do not favor any model a priori.

Examine each suggestion meticulously, assessing its quality, relevance, and accuracy within the context of PR. Keep in mind that the suggestions may vary in their correctness, accuracy and impact.
Consider the following components of each suggestion:
    1. 'one_sentence_summary' - A one-liner summary of the suggestion's purpose
    2. 'suggestion_content' - The suggestion content, explaining the proposed modification
    3. 'existing_code' - a code snippet from a __new hunk__ section in the PR code diff that the suggestion addresses
    4. 'improved_code' - a code snippet demonstrating how the 'existing_code' should be after the suggestion is applied

Be particularly vigilant for suggestions that:
    - Overlook crucial details in the PR code
    - The 'improved_code' section does not accurately reflect the suggested changes, in relation to the 'existing_code'
    - Contradict or ignore parts of the PR's modifications
In such cases, assign the suggestion a score of 0.

Evaluate each valid suggestion by scoring its potential impact on the PR's correctness, quality and functionality.
Key guidelines for evaluation:
- Thoroughly examine both the suggestion content and the corresponding PR code diff. Be vigilant for potential errors in each suggestion, ensuring they are logically sound, accurate, and directly derived from the PR code diff.
- Extend your review beyond the specifically mentioned code lines to encompass surrounding PR code context, verifying the suggestions' contextual accuracy.
- Validate the 'existing_code' field by confirming it matches or is accurately derived from code lines within a '__new hunk__' section of the PR code diff.
- Ensure the 'improved_code' section accurately reflects the 'existing_code' segment after the suggested modification is applied.
- Apply a nuanced scoring system:
  - Reserve high scores (8-10) for suggestions addressing critical issues such as major bugs or security concerns.
  - Assign moderate scores (3-7) to suggestions that tackle minor issues, improve code style, enhance readability, or boost maintainability.
  - Avoid inflating scores for suggestions that, while correct, offer only marginal improvements or optimizations.
- Maintain the original order of suggestions in your feedback, corresponding to their input sequence.

Additional scoring considerations:
- If the suggestion only asks the user to verify or ensure a change done in the PR, it should not receive a score above 7 (and may be lower).
- Error handling or type checking suggestions should not receive a score above 8 (and may be lower).
- If the 'existing_code' snippet is equal to the 'improved_code' snippet, it should not receive a score above 7 (and may be lower).
- Assume each suggestion is independent and is not influenced by the other suggestions.
- Assign a score of 0 to suggestions aiming at:
   - Adding docstring, type hints, or comments
   - Remove unused imports or variables
   - Add missing import statements
   - Using more specific exception types.
   - Questions the definition, declaration, import, or initialization of any entity in the PR code, that might be done in the outer codebase.



The PR code diff will be presented in the following structured format:
======
## File: 'src/file1.py'
{%- if is_ai_metadata %}
### AI-generated changes summary:
* ...
* ...
{%- endif %}

@@ ... @@ def func1():
__new hunk__
11  unchanged code line0
12  unchanged code line1
13 +new code line2 added
14  unchanged code line3
__old hunk__
 unchanged code line0
 unchanged code line1
-old code line2 removed
 unchanged code line3

@@ ... @@ def func2():
__new hunk__
...
__old hunk__
...


## File: 'src/file2.py'
...
======
- In the format above, the diff is organized into separate '__new hunk__' and '__old hunk__' sections for each code chunk. '__new hunk__' contains the updated code, while '__old hunk__' shows the removed code. If no code was added or removed in a specific chunk, the corresponding section will be omitted.
- Line numbers are included for the '__new hunk__' sections to enable referencing specific lines in the code suggestions. These numbers are for reference only and are not part of the actual code.
- Code lines are prefixed with symbols: '+' for new code added in the PR, '-' for code removed, and ' ' for unchanged code.
{%- if is_ai_metadata %}
- When available, an AI-generated summary will precede each file's diff, with a high-level overview of the changes. Note that this summary may not be fully accurate or comprehensive.
{%- endif %}


The output must be a YAML object equivalent to type $PRCodeSuggestionsFeedback, according to the following Pydantic definitions:
=====
class CodeSuggestionFeedback(BaseModel):
    suggestion_summary: str = Field(description="Repeated from the input")
    relevant_file: str = Field(description="Repeated from the input")
    relevant_lines_start: int = Field(description="The relevant line number, from a '__new hunk__' section, where the suggestion starts (inclusive). Should be derived from the added '__new hunk__' line numbers, and correspond to the first line of the relevant 'existing code' snippet.")
    relevant_lines_end: int = Field(description="The relevant line number, from a '__new hunk__' section, where the suggestion ends (inclusive). Should be derived from the added '__new hunk__' line numbers, and correspond to the end of the relevant 'existing code' snippet")
    suggestion_score: int = Field(description="Evaluate the suggestion and assign a score from 0 to 10. Give 0 if the suggestion is wrong or if it is a near-duplicate of another suggestion that you chose to keep. For valid suggestions, score from 1 (lowest impact/importance) to 10 (highest impact/importance).")
    why: str = Field(description="Briefly explain the score given in 1-2 short sentences, focusing on the suggestion's impact, relevance, and accuracy. For near-duplicates scored 0, state which suggestion it duplicates. When mentioning code elements (variables, names, or files) in your response, surround them with markdown backticks (`).")

class PRCodeSuggestionsFeedback(BaseModel):
    code_suggestions: List[CodeSuggestionFeedback]
=====


Example output:
```yaml
code_suggestions:
- suggestion_summary: |
    Use a more descriptive variable name here
  relevant_file: "src/file1.py"
  relevant_lines_start: 13
  relevant_lines_end: 14
  suggestion_score: 6
  why: |
    The variable name 't' is not descriptive enough
- suggestion_summary: |
    Use a clearer variable name
  relevant_file: "src/file1.py"
  relevant_lines_start: 13
  relevant_lines_end: 14
  suggestion_score: 0
  why: |
    Near-duplicate of suggestion 1, which is kept
```


Each YAML output MUST be after a newline, indented, with block scalar indicator ('|').
"""

user="""You are given a Pull Request (PR) code diff:
======
{{ diff|trim }}
======


Below are {{ num_code_suggestions }} AI-generated code suggestions for the Pull Request, produced by multiple independent models:
======
{{ suggestion_str|trim }}
======


{%- if duplicate_prompt_examples %}


Example output:
```yaml
code_suggestions:
- suggestion_summary: |
    ...
  relevant_file: "..."
  relevant_lines_start: ...
  relevant_lines_end: ...
  suggestion_score: ...
  why: |
    ...
- ...
```
(replace '...' with actual content)
{%- endif %}

Response (should be a valid YAML, and nothing else):
```yaml
"""
````

- [ ] **Step 4: Register the prompt file**

In `pr_agent/config_loader.py`, after the line `"settings/code_suggestions/pr_code_suggestions_reflect_prompts.toml",` add:

```python
        "settings/code_suggestions/pr_code_suggestions_reflect_consolidate_prompts.toml",
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/unittest/test_ensemble_prompts.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add pr_agent/settings/code_suggestions/pr_code_suggestions_reflect_consolidate_prompts.toml pr_agent/config_loader.py tests/unittest/test_ensemble_prompts.py
git commit -m "feat: add /improve ensemble consolidating-reflection prompt"
```

---

### Task 6: /improve ensemble flow

**Files:**
- Modify: `pr_agent/tools/pr_code_suggestions.py` (imports; `__init__`; `run()` line 116 area; new methods after `prepare_prediction_main`; footer in `run()`)
- Test: `tests/unittest/test_pr_code_suggestions_ensemble.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unittest/test_pr_code_suggestions_ensemble.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

import pr_agent.tools.pr_code_suggestions as pcs_module
from pr_agent.algo.ensemble import EnsembleConfig
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def build_tool():
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.ai_handler = MagicMock()
    tool.vars = {"diff": "", "diff_no_line_numbers": ""}
    tool.pr_code_suggestions_prompt_system = "sys"
    tool.pr_code_suggestions_prompt_user = "usr"
    tool.token_handler = MagicMock()
    tool.patches_diff_list = []
    tool.patches_diff_list_no_line_numbers = []
    tool.ensemble_models_used = []
    tool.ensemble_consolidator = ""
    tool.ensemble_consolidated = False
    return tool


async def test_chunk_ensemble_merges_models_tags_source_and_reflects_with_consolidator():
    tool = build_tool()
    suggestions = {
        "model-a": {"code_suggestions": [{"one_sentence_summary": "fix bug X", "label": "bug"}]},
        "model-b": {"code_suggestions": [{"one_sentence_summary": "fix bug X", "label": "bug"},
                                         {"one_sentence_summary": "improve Y", "label": "general"}]},
    }

    async def fake_generation(model, diff, diff_no_lines):
        return suggestions[model]

    tool._get_suggestions_prediction = fake_generation
    tool._reflect_and_score = AsyncMock(return_value=True)

    ensemble = EnsembleConfig(models=["model-a", "model-b"], consolidator="model-c")
    models_used = set()
    merged = await tool._get_chunk_ensemble_prediction(ensemble, "diff", "diff-no-lines", models_used)

    summaries = [s["one_sentence_summary"] for s in merged["code_suggestions"]]
    assert summaries == ["fix bug X", "improve Y"]  # exact cross-model duplicate dropped
    assert merged["code_suggestions"][0]["source_model"] == "model-a"
    assert merged["code_suggestions"][1]["source_model"] == "model-b"
    assert models_used == {"model-a", "model-b"}
    reflect_kwargs = tool._reflect_and_score.call_args.kwargs
    assert reflect_kwargs["model"] == "model-c"
    assert reflect_kwargs["dedicated_prompt"] == "pr_code_suggestions_reflect_consolidate_prompt"


async def test_chunk_ensemble_marks_consolidation_degraded_when_reflection_fails():
    tool = build_tool()
    tool.ensemble_consolidated = True

    async def fake_generation(model, diff, diff_no_lines):
        return {"code_suggestions": [{"one_sentence_summary": "s", "label": "bug"}]}

    tool._get_suggestions_prediction = fake_generation
    tool._reflect_and_score = AsyncMock(return_value=False)

    ensemble = EnsembleConfig(models=["model-a"], consolidator="model-a")
    await tool._get_chunk_ensemble_prediction(ensemble, "diff", "diff-no-lines", set())

    assert tool.ensemble_consolidated is False


async def test_prepare_prediction_ensemble_filters_by_score(monkeypatch):
    tool = build_tool()
    monkeypatch.setattr(pcs_module, "pick_min_budget_model", lambda models: "budget-model")

    async def fake_build(model):
        assert model == "budget-model"
        tool.patches_diff_list = ["chunk-1"]
        tool.patches_diff_list_no_line_numbers = ["chunk-1-no-lines"]

    tool._build_diff_chunks = fake_build
    chunk_result = {"code_suggestions": [
        {"one_sentence_summary": "good", "score": 8, "source_model": "model-a"},
        {"one_sentence_summary": "dupe", "score": 0, "source_model": "model-b"},
    ]}

    async def fake_chunk(ensemble, diff, diff_no_lines, models_used):
        models_used.update(ensemble.models)
        return chunk_result

    tool._get_chunk_ensemble_prediction = fake_chunk

    data = await tool.prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    assert [s["one_sentence_summary"] for s in data["code_suggestions"]] == ["good"]
    assert tool.ensemble_models_used == ["model-a", "model-b"]
    assert tool.ensemble_consolidator == "model-a"


async def test_prepare_prediction_ensemble_raises_when_no_model_succeeds(monkeypatch):
    tool = build_tool()
    monkeypatch.setattr(pcs_module, "pick_min_budget_model", lambda models: "budget-model")

    async def fake_build(model):
        tool.patches_diff_list = ["chunk-1"]
        tool.patches_diff_list_no_line_numbers = ["chunk-1-no-lines"]

    tool._build_diff_chunks = fake_build

    async def fake_chunk(ensemble, diff, diff_no_lines, models_used):
        return {"code_suggestions": []}  # no model contributed -> models_used stays empty

    tool._get_chunk_ensemble_prediction = fake_chunk

    with pytest.raises(Exception, match="All ensemble models failed"):
        await tool.prepare_prediction_ensemble(
            EnsembleConfig(models=["model-a"], consolidator="model-a"))


async def test_get_prediction_composes_generation_and_reflection():
    tool = build_tool()
    gen_data = {"code_suggestions": [{"one_sentence_summary": "s", "label": "bug", "relevant_file": "f"}]}
    tool._get_suggestions_prediction = AsyncMock(return_value=gen_data)
    tool._reflect_and_score = AsyncMock(return_value=True)

    data = await tool._get_prediction("some-model", "diff", "diff-no-lines")

    assert data is gen_data
    tool._reflect_and_score.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unittest/test_pr_code_suggestions_ensemble.py -v`
Expected: FAIL with `AttributeError: ... has no attribute '_get_chunk_ensemble_prediction'` (the last test, against Task 4's refactor, may already pass)

- [ ] **Step 3: Implement in `pr_agent/tools/pr_code_suggestions.py`**

3a. Add the import (after the `litellm_ai_handler` import, line 15):

```python
from pr_agent.algo.ensemble import (EnsembleConfig, ensemble_footer,
                                    gather_ensemble_predictions,
                                    pick_min_budget_model,
                                    resolve_ensemble_config)
```

3b. In `__init__`, after `self.prediction = None` (line 48), add:

```python
        self.ensemble_models_used = []
        self.ensemble_consolidator = ""
        self.ensemble_consolidated = False
```

3c. In `run()`, replace line 116:

```python
            data = await retry_with_fallback_models(self.prepare_prediction_main, model_type=ModelType.REGULAR)
```

with:

```python
            ensemble_config = resolve_ensemble_config("pr_code_suggestions")
            if ensemble_config:
                try:
                    data = await self.prepare_prediction_ensemble(ensemble_config)
                except Exception as e:
                    get_logger().warning("Ensemble code suggestions failed, falling back to the standard flow",
                                         artifact={"error": e})
                    self.ensemble_models_used = []
                    data = await retry_with_fallback_models(self.prepare_prediction_main,
                                                            model_type=ModelType.REGULAR)
            else:
                data = await retry_with_fallback_models(self.prepare_prediction_main, model_type=ModelType.REGULAR)
```

3d. Add the ensemble methods directly after `prepare_prediction_main`:

```python
    async def prepare_prediction_ensemble(self, ensemble: EnsembleConfig) -> dict:
        get_logger().info(f"Running ensemble code suggestions with models {ensemble.models}, "
                          f"consolidator {ensemble.consolidator}")
        # build the chunks once, with the most token-constrained model, so every
        # ensemble member and the consolidator see identical chunk boundaries
        budget_model = pick_min_budget_model(ensemble.models + [ensemble.consolidator])
        await self._build_diff_chunks(budget_model)

        if not self.patches_diff_list:
            get_logger().warning(f"Empty PR diff list")
            self.data = None
            return None

        get_logger().info(f"Number of PR chunk calls: {len(self.patches_diff_list)}")
        get_logger().debug(f"PR diff:", artifact=self.patches_diff_list)

        self.ensemble_consolidator = ensemble.consolidator
        self.ensemble_consolidated = True
        models_used = set()
        if get_settings().pr_code_suggestions.parallel_calls:
            prediction_list = await asyncio.gather(
                *[self._get_chunk_ensemble_prediction(ensemble, patches_diff,
                                                      patches_diff_no_line_numbers, models_used)
                  for patches_diff, patches_diff_no_line_numbers in
                  zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers)])
        else:
            prediction_list = []
            for patches_diff, patches_diff_no_line_numbers in zip(self.patches_diff_list,
                                                                  self.patches_diff_list_no_line_numbers):
                prediction_list.append(
                    await self._get_chunk_ensemble_prediction(ensemble, patches_diff,
                                                              patches_diff_no_line_numbers, models_used))
        self.prediction_list = prediction_list

        if not models_used:
            raise Exception(f"All ensemble models failed to generate code suggestions: {ensemble.models}")
        self.ensemble_models_used = [m for m in ensemble.models if m in models_used]

        self.data = data = self._merge_predictions_by_score(prediction_list)
        return data

    async def _get_chunk_ensemble_prediction(self, ensemble: EnsembleConfig, patches_diff: str,
                                             patches_diff_no_line_numbers: str, models_used: set) -> dict:
        async def generate(model: str) -> dict:
            return await self._get_suggestions_prediction(model, patches_diff, patches_diff_no_line_numbers)

        member_results = await gather_ensemble_predictions(generate, ensemble.models)
        merged = {"code_suggestions": []}
        seen_summaries = set()
        for model, member_data in member_results:
            models_used.add(model)
            for suggestion in member_data.get("code_suggestions", []):
                summary = suggestion.get("one_sentence_summary", "")
                if summary and summary in seen_summaries:
                    get_logger().debug(f"Skipping exact cross-model duplicate suggestion: {summary}")
                    continue
                seen_summaries.add(summary)
                suggestion["source_model"] = model
                merged["code_suggestions"].append(suggestion)

        if merged["code_suggestions"]:
            # one consolidating reflection per chunk: scores, line numbers, and
            # cross-model near-duplicate removal, all by the consolidator model
            reflected_ok = await self._reflect_and_score(
                merged, patches_diff, model=ensemble.consolidator,
                dedicated_prompt="pr_code_suggestions_reflect_consolidate_prompt")
            if not reflected_ok:
                self.ensemble_consolidated = False
        return merged
```

3e. Add the footer at the two `generate_summarized_suggestions` call sites in `run()`. After line 136 (`pr_body = self.generate_summarized_suggestions(data)` in the publish branch) and after the same call in the `publish_output=false` branch (line ~181), add:

```python
                    if self.ensemble_models_used:
                        pr_body += ensemble_footer(self.ensemble_models_used,
                                                   self.ensemble_consolidator,
                                                   self.ensemble_consolidated)
```

(match the indentation of each call site; in the no-publish branch it is one level shallower)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unittest/test_pr_code_suggestions_ensemble.py tests/unittest/test_pr_code_suggestions_core.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests/unittest -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add pr_agent/tools/pr_code_suggestions.py tests/unittest/test_pr_code_suggestions_ensemble.py
git commit -m "feat: multi-model ensemble flow for /improve"
```

---

### Task 7: Documentation

**Files:**
- Create: `docs/docs/core-abilities/ensemble_review.md`
- Modify: `docs/mkdocs.yml:40` (nav), `docs/docs/tools/review.md`, `docs/docs/tools/improve.md`, `docs/docs/usage-guide/changing_a_model.md`

- [ ] **Step 1: Create the core-ability page**

Create `docs/docs/core-abilities/ensemble_review.md`:

````markdown
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

Both keys also accept comma-separated strings, can be set per tool under
`[pr_reviewer]` / `[pr_code_suggestions]` (tool keys win over `[config]`), and
can be passed per command:

```
/review --config.ensemble_models='["claude-opus-4-8","gpt-5.5-2026-04-23"]'
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
models — so the same call count as a regular `/improve` run, plus the extra
generation calls.

A footer notes which models contributed, e.g.:

> 🧬 **Ensemble**: `claude-opus-4-8` + `gpt-5.5-2026-04-23` · consolidated by `claude-opus-4-8`

## Failure behavior

The ensemble never fails a run that would have succeeded single-model:

- A model that errors out is dropped (logged); the rest continue.
- If only one model succeeds, its output is used directly and consolidation is skipped.
- If every ensemble model fails, the run falls back to the standard
  `config.model` + `fallback_models` flow.
- If the `/review` consolidation call fails, the first successful model's
  review is published instead.

## Notes

- Each ensemble model must either be listed in pr-agent's `MAX_TOKENS` table or
  be covered by `config.custom_model_max_tokens`.
- Expect roughly N× the model cost of a single run for N ensemble models, plus
  one consolidation call.
````

- [ ] **Step 2: Add the nav entry**

In `docs/mkdocs.yml`, after line 40 (`- Self-reflection: 'core-abilities/self_reflection.md'`), add:

```yaml
      - Multi-model ensemble: 'core-abilities/ensemble_review.md'
```

(match the 6-space indentation of the other Core Abilities entries)

- [ ] **Step 3: Cross-link from the tool and model docs**

In `docs/docs/tools/review.md` and `docs/docs/tools/improve.md`, add a short section before the configuration-options table (placement: end of the "examples"/overview part of each page):

```markdown
## Multi-model ensemble

This tool can run several models independently and consolidate their findings
with a dedicated consolidator model. See the
[ensemble documentation](../core-abilities/ensemble_review.md) for details:

```toml
[config]
ensemble_models = ["claude-opus-4-8", "gpt-5.5-2026-04-23"]
ensemble_consolidator_model = "claude-opus-4-8"
```
```

In `docs/docs/usage-guide/changing_a_model.md`, add at the end of the page:

```markdown
## Multi-model ensemble

`/review` and `/improve` can run several models and consolidate their findings —
see [Multi-model ensemble](../core-abilities/ensemble_review.md).

When all models are served through one OpenAI-compatible endpoint (LiteLLM
proxy, CLIProxyAPI, etc.), configure the endpoint once and list the models with
an `openai/` prefix:

```toml
[openai]
key = "..."         # your proxy API key
api_base = "http://localhost:8317/v1"

[config]
ensemble_models = ["openai/claude-opus-4-8", "openai/gpt-5.5"]
ensemble_consolidator_model = "openai/claude-opus-4-8"
custom_model_max_tokens = 200000  # token budget for models not in pr-agent's MAX_TOKENS table
```
```

- [ ] **Step 4: Commit**

```bash
git add docs/docs/core-abilities/ensemble_review.md docs/mkdocs.yml docs/docs/tools/review.md docs/docs/tools/improve.md docs/docs/usage-guide/changing_a_model.md
git commit -m "docs: document multi-model ensemble for /review and /improve"
```

---

### Task 8: Final verification

- [ ] **Step 1: Full test suite**

Run: `python3 -m pytest tests/unittest -v`
Expected: all PASS, no skips introduced by this work

- [ ] **Step 2: Config sanity check** — confirm the feature is inert by default

Run: `python3 -c "
from pr_agent.algo.ensemble import resolve_ensemble_config
assert resolve_ensemble_config('pr_reviewer') is None
assert resolve_ensemble_config('pr_code_suggestions') is None
print('ensemble off by default: OK')
"`
Expected: `ensemble off by default: OK`

- [ ] **Step 3: Commit any remaining changes and review the branch diff**

```bash
git status
git log --oneline upstream/main..HEAD 2>/dev/null || git log --oneline main..HEAD
```
