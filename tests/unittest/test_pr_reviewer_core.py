import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pr_agent.tools.pr_reviewer as pr_reviewer_module
from pr_agent.algo.repo_context.context_builder import RepoContextBundle, RepoContextSnippet
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


def _make_reviewer(git_provider=None):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = git_provider or MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.vars = {
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
        "date": "2026-06-12", "repo_context": "", "repo_context_status": "unavailable",
        "consolidation_verification_context": "",
    }
    reviewer.repo_context_bundle = None
    reviewer.repo_context_workspace = None
    reviewer.prediction = "review: {}"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.ensemble_models_used = []
    reviewer.ensemble_consolidator = ""
    reviewer.ensemble_consolidated = False
    return reviewer


def test_should_publish_review_no_suggestions_respects_config():
    reviewer = _make_reviewer()
    settings = get_settings()
    original_publish_no_suggestions = settings.pr_reviewer.publish_output_no_suggestions

    try:
        settings.pr_reviewer.publish_output_no_suggestions = False
        assert reviewer._should_publish_review_no_suggestions("No major issues detected") is False
        assert reviewer._should_publish_review_no_suggestions("A major issue was detected") is True

        settings.pr_reviewer.publish_output_no_suggestions = True
        assert reviewer._should_publish_review_no_suggestions("No major issues detected") is True
    finally:
        settings.pr_reviewer.publish_output_no_suggestions = original_publish_no_suggestions


def test_can_run_incremental_review_skips_auto_mode_without_new_commit():
    reviewer = _make_reviewer()
    reviewer.is_auto = True
    reviewer.incremental = SimpleNamespace(first_new_commit_sha=None)

    assert reviewer._can_run_incremental_review() is False


def test_set_review_labels_replaces_stale_review_labels_and_keeps_user_labels():
    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "require_estimate_effort_to_review": settings.pr_reviewer.require_estimate_effort_to_review,
        "require_security_review": settings.pr_reviewer.require_security_review,
        "enable_review_labels_effort": settings.pr_reviewer.enable_review_labels_effort,
        "enable_review_labels_security": settings.pr_reviewer.enable_review_labels_security,
    }
    settings.config.publish_output = True
    settings.pr_reviewer.require_estimate_effort_to_review = True
    settings.pr_reviewer.require_security_review = True
    settings.pr_reviewer.enable_review_labels_effort = True
    settings.pr_reviewer.enable_review_labels_security = True
    git_provider = MagicMock()
    git_provider.get_pr_labels.return_value = ["Review effort 1/5", "Possible security concern", "keep-me"]
    reviewer = _make_reviewer(git_provider)
    data = {
        "review": {
            "estimated_effort_to_review_[1-5]": "3, moderate",
            "security_concerns": "yes",
        }
    }

    try:
        reviewer.set_review_labels(data)

        git_provider.publish_labels.assert_called_once_with([
            "Review effort 3/5",
            "Possible security concern",
            "keep-me",
        ])
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.pr_reviewer.require_estimate_effort_to_review = original["require_estimate_effort_to_review"]
        settings.pr_reviewer.require_security_review = original["require_security_review"]
        settings.pr_reviewer.enable_review_labels_effort = original["enable_review_labels_effort"]
        settings.pr_reviewer.enable_review_labels_security = original["enable_review_labels_security"]


def test_get_user_answers_collects_question_and_answer_from_issue_comments():
    git_provider = MagicMock()
    git_provider.get_issue_comments.return_value = SimpleNamespace(reversed=[
        SimpleNamespace(body="Unrelated"),
        SimpleNamespace(body="Questions to better understand the PR:\n- Why?"),
        SimpleNamespace(body="/answer Because it fixes production."),
    ])
    reviewer = _make_reviewer(git_provider)
    reviewer.is_answer = True

    question, answer = reviewer._get_user_answers()

    assert question == "Questions to better understand the PR:\n- Why?"
    assert answer == "/answer Because it fixes production."


def test_repo_context_defaults_are_disabled():
    settings = get_settings()

    assert settings.repo_context.enabled is False
    assert settings.repo_context.fallback_to_diff_only is True
    assert settings.repo_context.cross_repos == []
    assert ".git/**" in settings.repo_context.excluded_globs


async def test_prepare_prediction_uses_repo_context_token_handler_before_diff(monkeypatch):
    reviewer = _make_reviewer()
    reviewer.git_provider.pr = MagicMock()
    reviewer.repo_context_bundle = MagicMock()
    reviewer.ai_handler = MagicMock()
    reviewer.ai_handler.chat_completion = AsyncMock(return_value=("review: {}", "stop"))
    reviewer._format_repo_context_vars = MagicMock(return_value={
        **reviewer.vars,
        "repo_context": "repo context for model-a",
        "repo_context_status": "ok",
    })
    token_handler_vars = {}

    def fake_token_handler(pr, variables, system, user):
        token_handler_vars.update(variables)
        return MagicMock()

    def fake_get_pr_diff(provider, handler, model, **kwargs):
        assert token_handler_vars["repo_context"] == "repo context for model-a"
        return "diff"

    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", fake_token_handler)
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", fake_get_pr_diff)

    await reviewer._prepare_prediction("model-a")

    assert reviewer.prediction == "review: {}"
    assert "repo context for model-a" in reviewer.ai_handler.chat_completion.call_args.kwargs["user"]


async def test_run_builds_repo_context_when_enabled_and_cleans_workspace(monkeypatch):
    settings = get_settings()
    original_enabled = settings.repo_context.enabled
    original_publish = settings.config.publish_output
    settings.repo_context.enabled = True
    settings.config.publish_output = False
    reviewer = _make_reviewer()
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.git_provider.get_files.return_value = [MagicMock(filename="app.py", patch="+x")]
    reviewer.git_provider.get_diff_files.return_value = reviewer.git_provider.get_files.return_value
    reviewer.git_provider.get_pr_url.return_value = "https://example/pr/1"
    reviewer._prepare_prediction = AsyncMock()
    reviewer._prepare_pr_review = MagicMock(return_value="review")
    cleanup = MagicMock()
    reviewer._build_repo_context_bundle = MagicMock(return_value=SimpleNamespace(cleanup=cleanup))
    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(pr_reviewer_module, "resolve_ensemble_config", lambda *_: None)

    async def fake_retry(func, model_type=None):
        await func("model-a")

    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fake_retry)

    try:
        await reviewer.run()
    finally:
        settings.repo_context.enabled = original_enabled
        settings.config.publish_output = original_publish

    reviewer._build_repo_context_bundle.assert_called_once()
    cleanup.assert_called_once()


async def test_repo_context_builder_error_falls_back_to_diff_only(monkeypatch):
    settings = get_settings()
    original_fallback = settings.repo_context.fallback_to_diff_only
    settings.repo_context.fallback_to_diff_only = True
    reviewer = _make_reviewer()
    real_import = __import__

    def fail_import(name, *args, **kwargs):
        if name.startswith("pr_agent.algo.repo_context."):
            raise RuntimeError("builder unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fail_import)

    try:
        bundle = reviewer._build_repo_context_bundle()
    finally:
        settings.repo_context.fallback_to_diff_only = original_fallback

    assert bundle.status == "unavailable"
    assert reviewer.vars["repo_context"] == ""
    assert reviewer.vars["repo_context_status"] == "unavailable"


def test_build_repo_context_uses_production_searcher_and_renders_context(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app").mkdir()
    (repo / "app" / "db.py").write_text("def db_delete(user_id):\n    return client.delete(user_id)\n")
    (repo / "app" / "service.py").write_text("def remove_user(user_id):\n    return db_delete(user_id)\n")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)

    settings = get_settings()
    originals = {
        "enabled": settings.repo_context.enabled,
        "fallback_to_diff_only": settings.repo_context.fallback_to_diff_only,
        "max_context_tokens": settings.repo_context.get("max_context_tokens", None),
    }
    settings.repo_context.enabled = True
    settings.repo_context.fallback_to_diff_only = False
    settings.repo_context.max_context_tokens = 200
    git_provider = MagicMock()
    git_provider.pr = MagicMock()
    git_provider.get_repo_context_local_root.return_value = str(repo)
    git_provider.get_diff_files.return_value = [
        SimpleNamespace(filename="app/db.py", head_file="", patch="@@ -1 +1 @@\n+def db_delete(user_id):")
    ]
    reviewer = _make_reviewer(git_provider)

    try:
        bundle = reviewer._build_repo_context_bundle()
    finally:
        settings.repo_context.enabled = originals["enabled"]
        settings.repo_context.fallback_to_diff_only = originals["fallback_to_diff_only"]
        if originals["max_context_tokens"] is None:
            del settings.repo_context.max_context_tokens
        else:
            settings.repo_context.max_context_tokens = originals["max_context_tokens"]

    assert bundle.status == "ok"
    assert "db_delete(user_id)" in reviewer.vars["repo_context"]
    assert reviewer.vars["repo_context_status"] == "ok"
    assert str(tmp_path) not in reviewer.vars["repo_context"]


def test_build_repo_context_uses_full_diff_file_for_js_enclosing_symbol(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core").mkdir()
    (repo / "routes").mkdir()
    (repo / "core" / "common.js").write_text(
        "const Common = {\n"
        "  db_delete: async function(table, data) {\n"
        "    if (!Array.isArray(data) || data.length === 0) {\n"
        "      throw new Error('blocked');\n"
        "    }\n"
        "    return data;\n"
        "  }\n"
        "};\n"
    )
    (repo / "routes" / "users.js").write_text(
        "const Common = require('../core/common');\n"
        "async function removeUser(id) {\n"
        "  return Common.db_delete('users', { id });\n"
        "}\n"
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)

    settings = get_settings()
    original = dict(settings.repo_context)
    settings.repo_context.enabled = True
    settings.repo_context.fallback_to_diff_only = False
    settings.repo_context.max_context_tokens = 400
    settings.repo_context.max_context_snippets = 10
    git_provider = MagicMock()
    git_provider.pr = MagicMock()
    git_provider.get_repo_context_local_root.return_value = str(repo)
    git_provider.get_diff_files.return_value = [
        SimpleNamespace(
            filename="core/common.js",
            head_file=(repo / "core" / "common.js").read_text(),
            patch="\n".join(
                [
                    "@@ -1,7 +1,7 @@",
                    " const Common = {",
                    "   db_delete: async function(table, data) {",
                    "+    if (!Array.isArray(data) || data.length === 0) {",
                    "+      throw new Error('blocked');",
                    "     }",
                    "     return data;",
                ]
            ),
        )
    ]
    reviewer = _make_reviewer(git_provider)

    try:
        bundle = reviewer._build_repo_context_bundle()
    finally:
        settings.repo_context.clear()
        settings.repo_context.update(original)

    assert bundle.status == "ok"
    assert "routes/users.js" in reviewer.vars["repo_context"]
    assert "Common.db_delete('users', { id })" in reviewer.vars["repo_context"]


def test_build_repo_context_passes_external_repo_config_to_workspace_manager(monkeypatch):
    settings = get_settings()
    original = dict(settings.repo_context)
    settings.repo_context.include_external_repos = True
    settings.repo_context.external_repositories = [
        {"name": "shared", "url": "https://github.com/org/shared.git", "ref": "main"}
    ]
    settings.repo_context.allowed_external_repo_urls = ["https://github.com/org/shared.git"]
    settings.repo_context.max_external_repositories = 2
    settings.repo_context.external_include_repositories = ["https://github.com/org/shared.git"]
    settings.repo_context.external_exclude_repositories = []
    settings.repo_context.checkout_timeout_sec = 12
    settings.repo_context.fallback_to_diff_only = False
    captured = {}

    class CapturingWorkspaceManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def create_session(self, git_provider):
            class Context:
                def __enter__(self_inner):
                    return SimpleNamespace(primary=SimpleNamespace(root=None, files=[]), external_repos=[])

                def __exit__(self_inner, *args):
                    return None

            return Context()

    class EmptyBuilder:
        def __init__(self, **kwargs):
            pass

        def build(self, **kwargs):
            return RepoContextBundle(status="ok")

    monkeypatch.setattr("pr_agent.algo.repo_context.workspace.RepoWorkspaceManager", CapturingWorkspaceManager)
    monkeypatch.setattr("pr_agent.algo.repo_context.context_builder.RepoContextBuilder", EmptyBuilder)
    reviewer = _make_reviewer()
    reviewer.git_provider.get_diff_files.return_value = []

    try:
        reviewer._build_repo_context_bundle()
    finally:
        settings.repo_context.clear()
        settings.repo_context.update(original)

    assert captured["external_repos"] == [
        {"name": "shared", "url": "https://github.com/org/shared.git", "ref": "main"}
    ]
    assert captured["allowed_external_repo_urls"] == ["https://github.com/org/shared.git"]
    assert captured["max_external_repos"] == 2
    assert captured["include_external_repos"] == ["https://github.com/org/shared.git"]
    assert captured["checkout_timeout_sec"] == 12
    assert captured["fallback_to_diff_only"] is False


def test_format_repo_context_vars_clips_context_to_preserve_diff_budget():
    settings = get_settings()
    original = dict(settings.repo_context)
    settings.repo_context.max_context_tokens = 20
    settings.repo_context.max_context_token_ratio = 0.5
    settings.repo_context.min_diff_tokens_reserved = 20
    settings.repo_context.max_context_snippets = 1
    reviewer = _make_reviewer()
    reviewer.repo_context_bundle = RepoContextBundle(
        status="ok",
        snippets=[
            RepoContextSnippet(
                repo="primary",
                repo_label="primary",
                path="app.py",
                start=1,
                end=50,
                content=" ".join(["context"] * 100),
                score=100,
            ),
            RepoContextSnippet("primary", "primary", "other.py", 1, 1, "should not render", score=1),
        ],
    )

    try:
        variables = reviewer._format_repo_context_vars("model-a")
    finally:
        settings.repo_context.clear()
        settings.repo_context.update(original)

    assert variables["repo_context_status"] == "partial"
    assert variables["repo_context"].count("```") == 2
    assert "should not render" not in variables["repo_context"]
    assert len(variables["repo_context"].split()) <= 20


def test_prepare_pr_review_does_not_validate_lines_when_repo_context_disabled(monkeypatch):
    settings = get_settings()
    original_enabled = settings.repo_context.enabled
    settings.repo_context.enabled = False
    reviewer = _make_reviewer()
    reviewer.prediction = "review:\n  estimated_effort_to_review_[1-5]: 2\n  key_issues_to_review: []\n"
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.is_supported.return_value = False
    reviewer.set_review_labels = MagicMock()
    validator = MagicMock()
    monkeypatch.setattr(pr_reviewer_module, "validate_key_issues_to_review", validator)
    monkeypatch.setattr(pr_reviewer_module, "convert_to_markdown_v2", lambda *args, **kwargs: "MD")

    try:
        assert reviewer._prepare_pr_review() == "MD"
    finally:
        settings.repo_context.enabled = original_enabled

    validator.assert_not_called()
