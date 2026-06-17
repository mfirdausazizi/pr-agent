from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo.utils import append_review_metadata
from pr_agent.git_providers.github_provider import GithubProvider


def _commit(sha, authored_at, files):
    return SimpleNamespace(
        sha=sha,
        files=files,
        commit=SimpleNamespace(
            author=SimpleNamespace(date=authored_at),
            message=f"commit {sha}",
        ),
    )


def _file(filename, status="modified", previous_filename=None):
    file_obj = SimpleNamespace(filename=filename, status=status)
    if previous_filename is not None:
        file_obj.previous_filename = previous_filename
    return file_obj


def _provider(pr_files, commits, previous_body="", previous_created_at=None):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr_commits = commits
    provider.pr = MagicMock()
    provider.pr.get_files.return_value = pr_files
    previous_review = SimpleNamespace(
        body=previous_body,
        created_at=previous_created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    provider.comments = [previous_review]
    provider.previous_review = previous_review
    provider.unreviewed_files_set = {}
    provider.incremental = SimpleNamespace(
        is_incremental=True,
        commits_range=None,
        first_new_commit=None,
        last_seen_commit=None,
    )
    provider._get_repo = MagicMock(return_value=SimpleNamespace(default_branch="production"))
    return provider


def test_incremental_files_are_scoped_to_current_pr_files():
    pr_file = _file("utils/signup-app-balancer.js")
    unrelated_file = _file("routes/v4/whatsapp/chatbot.js")
    commits = [
        _commit("old", datetime(2026, 1, 1, tzinfo=timezone.utc), [pr_file]),
        _commit("new", datetime(2026, 1, 3, tzinfo=timezone.utc), [pr_file, unrelated_file]),
    ]
    provider = _provider(
        pr_files=[pr_file],
        commits=commits,
        previous_body="## PR Reviewer Guide 🔍",
        previous_created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    provider._get_incremental_commits()

    assert provider.unreviewed_files_set == {"utils/signup-app-balancer.js": pr_file}


def test_incremental_file_scoping_handles_renames_by_current_pr_filename():
    pr_file = _file("new-name.py", status="renamed", previous_filename="old-name.py")
    commit_file = _file("old-name.py", status="removed")
    commits = [
        _commit("old", datetime(2026, 1, 1, tzinfo=timezone.utc), [commit_file]),
        _commit("new", datetime(2026, 1, 3, tzinfo=timezone.utc), [commit_file]),
    ]
    provider = _provider(
        pr_files=[pr_file],
        commits=commits,
        previous_body="## PR Reviewer Guide 🔍",
        previous_created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    provider._get_incremental_commits()

    assert provider.unreviewed_files_set == {"new-name.py": pr_file}


def test_incremental_commit_range_uses_reviewed_head_sha_metadata_before_timestamp():
    commits = [
        _commit("reviewed", datetime(2026, 1, 1, tzinfo=timezone.utc), [_file("old.py")]),
        _commit("new", datetime(2026, 1, 1, tzinfo=timezone.utc), [_file("new.py")]),
    ]
    previous_body = append_review_metadata("## PR Reviewer Guide 🔍", reviewed_head_sha="reviewed")
    provider = _provider(
        pr_files=[_file("new.py")],
        commits=commits,
        previous_body=previous_body,
        previous_created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    commit_range = provider.get_commit_range()

    assert [commit.sha for commit in commit_range] == ["new"]
    assert provider.incremental.last_seen_commit.sha == "reviewed"
    assert provider.incremental.first_new_commit.sha == "new"


def test_incremental_commit_range_falls_back_to_timestamp_when_metadata_is_missing():
    commits = [
        _commit("old", datetime(2026, 1, 1, tzinfo=timezone.utc), [_file("old.py")]),
        _commit("new", datetime(2026, 1, 3, tzinfo=timezone.utc), [_file("new.py")]),
    ]
    provider = _provider(
        pr_files=[_file("new.py")],
        commits=commits,
        previous_body="## PR Reviewer Guide 🔍",
        previous_created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    commit_range = provider.get_commit_range()

    assert [commit.sha for commit in commit_range] == ["new"]
    assert provider.incremental.last_seen_commit.sha == "old"
    assert provider.incremental.first_new_commit.sha == "new"


def test_incremental_commit_range_falls_back_to_timestamp_when_metadata_sha_is_missing():
    commits = [
        _commit("old", datetime(2026, 1, 1, tzinfo=timezone.utc), [_file("old.py")]),
        _commit("new", datetime(2026, 1, 3, tzinfo=timezone.utc), [_file("new.py")]),
    ]
    previous_body = append_review_metadata("## PR Reviewer Guide 🔍", reviewed_head_sha="rewritten")
    provider = _provider(
        pr_files=[_file("new.py")],
        commits=commits,
        previous_body=previous_body,
        previous_created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    commit_range = provider.get_commit_range()

    assert [commit.sha for commit in commit_range] == ["new"]
    assert provider.incremental.last_seen_commit.sha == "old"
    assert provider.incremental.first_new_commit.sha == "new"
