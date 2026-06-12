import asyncio

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


@pytest.mark.asyncio
async def test_gather_ensemble_predictions_keeps_successes_and_drops_failures():
    async def fake_fn(model):
        if model == "bad-model":
            raise RuntimeError("boom")
        if model == "empty-model":
            return ""
        if model == "cancelled-model":
            raise asyncio.CancelledError("cancelled")
        return f"prediction-from-{model}"

    results = await gather_ensemble_predictions(
        fake_fn, ["good-a", "bad-model", "empty-model", "good-b", "cancelled-model"])
    assert results == [("good-a", "prediction-from-good-a"),
                       ("good-b", "prediction-from-good-b")]
    assert not any(m == "cancelled-model" for m, _ in results)


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
