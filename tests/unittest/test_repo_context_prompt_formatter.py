from pr_agent.algo.repo_context.context_builder import RepoContextBundle, RepoContextSnippet
from pr_agent.algo.repo_context.prompt_formatter import format_repo_context, reserve_diff_tokens


class WordTokenHandler:
    def count_tokens(self, text):
        return len(text.split())


def test_formatter_labels_repos_cites_lines_and_omits_temp_paths():
    bundle = RepoContextBundle(
        status="ok",
        snippets=[
            RepoContextSnippet(
                repo="primary",
                repo_label="primary",
                path="app/service.py",
                start=3,
                end=4,
                content="return db_delete(user_id)",
                score=100,
            ),
            RepoContextSnippet(
                repo="external",
                repo_label="shared-lib",
                path="/var/folders/tmp/shared/db.py",
                start=8,
                end=9,
                content="def db_delete(user_id): pass",
                score=10,
            ),
        ],
    )

    text, status = format_repo_context(bundle, WordTokenHandler(), max_tokens=80)

    assert status == "ok"
    assert "[primary] app/service.py:3-4" in text
    assert "[shared-lib] shared/db.py:8-9" in text
    assert "/var/folders" not in text


def test_formatter_clips_external_before_primary_under_budget_pressure():
    bundle = RepoContextBundle(
        status="ok",
        snippets=[
            RepoContextSnippet(
                repo="primary",
                repo_label="primary",
                path="app/core.py",
                start=1,
                end=2,
                content="primary important caller context",
                score=100,
            ),
            RepoContextSnippet(
                repo="external",
                repo_label="external-lib",
                path="lib/core.py",
                start=1,
                end=2,
                content="external context should disappear first",
                score=50,
            ),
            RepoContextSnippet(
                repo="primary",
                repo_label="primary",
                path="tests/test_core.py",
                start=1,
                end=2,
                content="verification test context",
                score=1,
                context_type="verification",
            ),
        ],
    )

    text, status = format_repo_context(bundle, WordTokenHandler(), max_tokens=18)

    assert status == "partial"
    assert "primary important caller context" in text
    assert "verification test context" in text
    assert "external context should disappear first" not in text


def test_formatter_can_exclude_external_context():
    bundle = RepoContextBundle(
        status="ok",
        snippets=[
            RepoContextSnippet("external", "external-lib", "lib/core.py", 1, 2, "external context", score=50),
            RepoContextSnippet("primary", "primary", "app/core.py", 1, 2, "primary context", score=100),
        ],
    )

    text, _ = format_repo_context(bundle, WordTokenHandler(), max_tokens=80, include_external=False)

    assert "primary context" in text
    assert "external context" not in text


def test_reserve_diff_tokens_preserves_minimum_reserved_budget():
    assert reserve_diff_tokens(total_tokens=100, requested_context_tokens=90, min_diff_tokens_reserved=25) == 75
