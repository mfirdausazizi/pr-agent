import pytest
from unittest.mock import AsyncMock, MagicMock

import pr_agent.tools.pr_code_suggestions as pcs_module
from pr_agent.algo.ensemble import EnsembleConfig
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.config_loader import get_settings


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


async def test_reflect_and_score_treats_count_mismatch_as_failure():
    # the consolidate prompt asks the model to keep list length; if it drops
    # entries anyway, the reflection must be treated as failed (default scores)
    # instead of silently publishing unscored suggestions
    tool = build_tool()
    data = {"code_suggestions": [
        {"one_sentence_summary": "a", "label": "bug"},
        {"one_sentence_summary": "b", "label": "bug"},
    ]}
    # feedback has only one entry for two suggestions
    short_feedback = (
        "code_suggestions:\n"
        "- suggestion_summary: a\n"
        "  relevant_file: f\n"
        "  relevant_lines_start: 1\n"
        "  relevant_lines_end: 2\n"
        "  suggestion_score: 9\n"
        "  why: good\n"
    )
    tool.self_reflect_on_suggestions = AsyncMock(return_value=short_feedback)

    ok = await tool._reflect_and_score(data, "diff", model="model-c",
                                       dedicated_prompt="pr_code_suggestions_reflect_consolidate_prompt")

    assert ok is False
    for suggestion in data["code_suggestions"]:
        assert suggestion["score"] == 7


async def test_get_prediction_composes_generation_and_reflection():
    tool = build_tool()
    gen_data = {"code_suggestions": [{"one_sentence_summary": "s", "label": "bug", "relevant_file": "f"}]}
    tool._get_suggestions_prediction = AsyncMock(return_value=gen_data)
    tool._reflect_and_score = AsyncMock(return_value=True)

    data = await tool._get_prediction("some-model", "diff", "diff-no-lines")

    assert data is gen_data
    tool._reflect_and_score.assert_awaited_once()
    # _get_prediction must reflect with the reasoning/config model, not the generation model
    assert tool._reflect_and_score.call_args.args[2] != "some-model"


@pytest.mark.asyncio
async def test_run_appends_footer_to_artifact_when_ensemble(monkeypatch):
    tool = build_tool()
    tool.git_provider.get_files.return_value = ["f"]
    tool.progress_response = None
    data = {"code_suggestions": [{"one_sentence_summary": "s", "label": "bug",
                                  "relevant_file": "f", "score": 8}]}

    async def fake_ensemble(ensemble_config):
        tool.ensemble_models_used = ["model-a", "model-b"]
        tool.ensemble_consolidator = "model-a"
        tool.ensemble_consolidated = True
        return data

    tool.prepare_prediction_ensemble = fake_ensemble
    tool.generate_summarized_suggestions = MagicMock(return_value="TABLE")
    monkeypatch.setattr(pcs_module, "resolve_ensemble_config",
                        lambda section: EnsembleConfig(models=["model-a", "model-b"],
                                                       consolidator="model-a"))

    settings = get_settings()
    original_publish = settings.config.publish_output
    settings.config.publish_output = False
    try:
        await tool.run()
        artifact = settings.data["artifact"]
        assert artifact.startswith("TABLE")
        assert "consolidated by `model-a`" in artifact
    finally:
        settings.config.publish_output = original_publish
