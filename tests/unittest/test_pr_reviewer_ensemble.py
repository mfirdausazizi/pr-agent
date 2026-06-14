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
        "repo_context": "", "repo_context_status": "unavailable",
        "consolidation_verification_context": "",
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
    reviewer.repo_context_bundle = None
    return reviewer


async def test_ensemble_consolidates_two_predictions(monkeypatch):
    reviewer = build_reviewer()
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", lambda *args, **kwargs: "diff")
    reviewer._get_prediction_for_diff = AsyncMock(
        side_effect=lambda model, diff: {"model-a": "review-a", "model-b": "review-b"}[model])
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

    async def member(model, patches_diff):
        if model == "model-a":
            raise RuntimeError("boom")
        return "review-b"

    reviewer._get_prediction_for_diff = member
    reviewer._consolidate_predictions = AsyncMock()

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    assert reviewer.prediction == "review-b"
    assert reviewer.ensemble_models_used == ["model-b"]
    assert reviewer.ensemble_consolidated is False
    assert reviewer.ensemble_consolidator == ""
    reviewer._consolidate_predictions.assert_not_awaited()


async def test_ensemble_falls_back_to_standard_flow_when_all_models_fail(monkeypatch):
    reviewer = build_reviewer()
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", lambda *args, **kwargs: "diff")

    async def fail(model, patches_diff):
        raise RuntimeError("boom")

    reviewer._get_prediction_for_diff = fail
    fallback = AsyncMock()
    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fallback)

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    fallback.assert_awaited_once()
    assert reviewer.ensemble_models_used == []


async def test_ensemble_uses_first_review_when_consolidation_fails(monkeypatch):
    reviewer = build_reviewer()
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", lambda *args, **kwargs: "diff")

    async def member(model, patches_diff):
        return {"model-a": "review-a", "model-b": "review-b"}[model]

    reviewer._get_prediction_for_diff = member
    reviewer._consolidate_predictions = AsyncMock(side_effect=RuntimeError("boom"))

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    assert reviewer.prediction == "review-a"
    assert reviewer.ensemble_consolidated is False


async def test_ensemble_skips_models_with_empty_diff(monkeypatch):
    reviewer = build_reviewer()
    diffs = {"model-a": "", "model-b": "diff-b"}
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff",
                        lambda provider, handler, model, **kwargs: diffs[model])

    async def member(model, patches_diff):
        assert patches_diff == "diff-b"
        return "review-b"

    reviewer._get_prediction_for_diff = member
    reviewer._consolidate_predictions = AsyncMock()

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-a"))

    assert reviewer.prediction == "review-b"
    assert reviewer.ensemble_models_used == ["model-b"]


async def test_ensemble_get_pr_diff_uses_per_model_repo_context_token_handler(monkeypatch):
    reviewer = build_reviewer()
    reviewer.repo_context_bundle = MagicMock()
    reviewer._format_repo_context_vars = MagicMock(side_effect=lambda model: {
        **reviewer.vars,
        "repo_context": f"context for {model}",
        "repo_context_status": "ok",
        "consolidation_verification_context": "",
    })
    token_handler_vars = []

    def fake_token_handler(pr, variables, system, user):
        token_handler_vars.append(variables)
        return MagicMock()

    def fake_get_pr_diff(provider, handler, model, **kwargs):
        assert token_handler_vars[-1]["repo_context"] == f"context for {model}"
        return f"diff-{model}"

    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", fake_token_handler)
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", fake_get_pr_diff)
    reviewer._get_prediction_for_diff = AsyncMock(side_effect=lambda model, diff, variables=None: f"review-{model}")
    reviewer._consolidate_predictions = AsyncMock(return_value="consolidated")

    await reviewer._prepare_prediction_ensemble(
        EnsembleConfig(models=["model-a", "model-b"], consolidator="model-c"))

    assert [vars_["repo_context"] for vars_ in token_handler_vars[:2]] == [
        "context for model-a", "context for model-b",
    ]
    assert reviewer._get_prediction_for_diff.await_args_list[0].args[2]["repo_context"] == "context for model-a"


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


async def test_consolidate_predictions_accounts_for_reviews_and_repo_context(monkeypatch):
    reviewer = build_reviewer()
    reviewer.ai_handler.chat_completion = AsyncMock(return_value=("consolidated-yaml", "stop"))
    reviewer.repo_context_bundle = MagicMock()
    reviewer._format_repo_context_vars = MagicMock(return_value={
        **reviewer.vars,
        "repo_context": "repo evidence",
        "repo_context_status": "ok",
        "consolidation_verification_context": "verification evidence",
    })
    token_handler_vars = {}

    def fake_token_handler(pr, variables, system, user):
        token_handler_vars.update(variables)
        return MagicMock()

    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", fake_token_handler)
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", lambda *args, **kwargs: "the-diff")

    await reviewer._consolidate_predictions([("model-a", "review-a"), ("model-b", "review-b")], "model-c")

    assert "review-a" in token_handler_vars["model_reviews"]
    assert token_handler_vars["repo_context"] == "repo evidence"
    assert token_handler_vars["consolidation_verification_context"] == "verification evidence"
    kwargs = reviewer.ai_handler.chat_completion.call_args.kwargs
    assert "repo evidence" in kwargs["user"]
    assert "verification evidence" in kwargs["user"]


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
