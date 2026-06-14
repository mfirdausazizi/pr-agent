import os
import subprocess
from pathlib import Path

import pytest

from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.git_provider import GitProvider


class DummyGitProvider(GitProvider):
    def is_supported(self, capability: str) -> bool:
        return False

    def get_files(self):
        return []

    def get_diff_files(self):
        return []

    def publish_description(self, pr_title: str, pr_body: str):
        pass

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return False

    def get_languages(self):
        return {}

    def get_pr_branch(self):
        return None

    def get_user_id(self):
        return None

    def get_pr_description_full(self) -> str:
        return ""

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        pass

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str,
                               original_suggestion=None):
        pass

    def publish_inline_comments(self, comments: list[dict]):
        pass

    def publish_labels(self, labels):
        pass

    def get_issue_comments(self):
        return []

    def get_pr_labels(self, update=False):
        return []

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def add_eyes_reaction(self, comment):
        pass

    def remove_reaction(self, comment):
        pass

    def get_commit_messages(self):
        return ""

    def get_repo_settings(self):
        return ""


class FakeProvider:
    def __init__(self, *, local_root=None, spec=None, auth_header=None):
        self.local_root = local_root
        self.spec = spec
        self.auth_header = auth_header

    def get_repo_context_local_root(self):
        return self.local_root

    def get_repo_context_primary_checkout_spec(self):
        return self.spec

    def get_repo_context_auth_header(self):
        return self.auth_header

    def sanitize_repo_context_url(self, value):
        return GitProvider.sanitize_repo_context_url(self, value)


def test_git_provider_repo_context_defaults_are_unavailable():
    provider = DummyGitProvider()

    assert provider.get_repo_context_identity() == {}
    assert provider.get_repo_context_primary_checkout_spec() is None
    assert provider.get_repo_context_local_root() is None
    assert provider.get_repo_context_auth_header() is None


def test_sanitize_repo_context_url_redacts_tokens_and_auth_headers():
    provider = DummyGitProvider()

    assert provider.sanitize_repo_context_url("https://secret-token@github.com/org/repo.git") == (
        "https://***@github.com/org/repo.git"
    )
    assert provider.sanitize_repo_context_url("Authorization: Bearer ghs_secret") == "Authorization: Bearer ***"


def test_github_provider_repo_context_identity_checkout_spec_and_auth_header():
    provider = object.__new__(GithubProvider)
    provider.repo = "org/repo"
    provider.pr_num = 7
    provider.base_url_html = "https://github.com"
    provider.auth = type("Auth", (), {"token": "ghs_secret", "token_type": "token"})()
    provider.pr = type(
        "PullRequest",
        (),
        {
            "head": type("Head", (), {"sha": "head-sha", "ref": "feature"})(),
            "base": type("Base", (), {"sha": "base-sha", "ref": "main"})(),
        },
    )()

    assert provider.get_repo_context_identity() == {
        "provider": "github",
        "repo": "org/repo",
        "pr_num": 7,
        "head_sha": "head-sha",
        "head_ref": "feature",
        "base_sha": "base-sha",
        "base_ref": "main",
    }
    assert provider.get_repo_context_primary_checkout_spec() == {
        "repo_url": "https://github.com/org/repo.git",
        "pr_num": 7,
        "head_sha": "head-sha",
    }
    assert provider.get_repo_context_auth_header() == "Authorization: token ghs_secret"


def test_github_provider_repo_context_auth_header_prefers_bound_requester_auth():
    provider = object.__new__(GithubProvider)
    provider.auth = type("Auth", (), {"token": "stale", "token_type": "token"})()
    requester = type(
        "Requester",
        (),
        {"auth": type("BoundAuth", (), {"token": "fresh", "token_type": "token"})()},
    )()
    provider.github_client = type("Client", (), {"_Github__requester": requester})()

    assert provider.get_repo_context_auth_header() == "Authorization: token fresh"


def test_local_session_uses_tracked_files_only(tmp_path, monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.py").write_text("tracked")
    (root / "untracked.py").write_text("untracked")

    def fake_run(command, **kwargs):
        assert command == ["git", "-C", str(root), "ls-files"]
        return subprocess.CompletedProcess(command, 0, stdout="tracked.py\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with RepoWorkspaceManager().create_session(FakeProvider(local_root=str(root))) as session:
        assert session.primary.root == str(root)
        assert session.primary.files == ["tracked.py"]
        assert session.diff_only is False


def test_open_file_rejects_configured_excluded_file(tmp_path, monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.py").write_text("tracked\n")

    def fake_run(command, **kwargs):
        assert command == ["git", "-C", str(root), "ls-files"]
        return subprocess.CompletedProcess(command, 0, stdout="tracked.py\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    manager = RepoWorkspaceManager(exclude_globs=["tracked.py"])
    with manager.create_session(FakeProvider(local_root=str(root))) as session:
        with pytest.raises(ValueError, match="File is excluded"):
            session.open_file("tracked.py")


def test_open_file_rejects_configured_oversize_file(tmp_path, monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.py").write_text("tracked\n")

    def fake_run(command, **kwargs):
        assert command == ["git", "-C", str(root), "ls-files"]
        return subprocess.CompletedProcess(command, 0, stdout="tracked.py\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with RepoWorkspaceManager(max_file_bytes=3).create_session(FakeProvider(local_root=str(root))) as session:
        with pytest.raises(ValueError, match="File is too large"):
            session.open_file("tracked.py")


def test_open_file_rejects_tracked_env_local(tmp_path, monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env.local").write_text("TOKEN=secret\n")

    def fake_run(command, **kwargs):
        assert command == ["git", "-C", str(root), "ls-files"]
        return subprocess.CompletedProcess(command, 0, stdout=".env.local\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with RepoWorkspaceManager().create_session(FakeProvider(local_root=str(root))) as session:
        with pytest.raises(ValueError, match="Secret file is not allowed"):
            session.open_file(".env.local")


def test_primary_checkout_uses_pull_ref_and_does_not_persist_token(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = FakeProvider(
        spec={"repo_url": "https://github.com/org/repo.git", "pr_num": 7, "head_sha": "abc123"},
        auth_header="Authorization: Bearer ghs_secret",
    )

    with RepoWorkspaceManager().create_session(provider) as session:
        assert Path(session.primary.root).exists()
        assert session.primary.status == "ready"

    assert ["git", "init"] in commands
    fetch_commands = [command for command in commands if "fetch" in command]
    assert fetch_commands
    assert "refs/pull/7/head" in fetch_commands[0]
    assert "-c" in fetch_commands[0]
    assert "http.extraHeader=Authorization: Bearer ghs_secret" in fetch_commands[0]
    assert not any("ghs_secret@github.com" in " ".join(command) for command in commands)
    assert not any(command[:3] == ["git", "remote", "add"] for command in commands)


def test_primary_checkout_falls_back_to_head_sha(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if "refs/pull/7/head" in command:
            raise subprocess.CalledProcessError(1, command, stderr="missing ref")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = FakeProvider(spec={"repo_url": "https://github.com/org/repo.git", "pr_num": 7, "head_sha": "abc123"})

    with RepoWorkspaceManager().create_session(provider) as session:
        assert session.primary.status == "ready"

    fetch_commands = [command for command in commands if "fetch" in command]
    assert "refs/pull/7/head" in fetch_commands[0]
    assert "abc123" in fetch_commands[1]


def test_primary_checkout_falls_back_to_diff_only_and_cleans_tempdir(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    seen_roots = []

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "init"]:
            seen_roots.append(os.getcwd())
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise subprocess.CalledProcessError(1, command, stderr="network denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = FakeProvider(spec={"repo_url": "https://github.com/org/repo.git", "pr_num": 7, "head_sha": "abc123"})

    with RepoWorkspaceManager(fallback_to_diff_only=True).create_session(provider) as session:
        temp_root = session.primary.root
        assert session.diff_only is True
        assert session.primary.status == "checkout_failed"
        assert Path(temp_root).exists()

    assert not Path(temp_root).exists()


def test_primary_checkout_failure_sanitizes_auth_header_when_not_fallback(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "init"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise subprocess.CalledProcessError(1, command, stderr="Authorization: Bearer ghs_secret")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = FakeProvider(
        spec={"repo_url": "https://github.com/org/repo.git", "pr_num": 7, "head_sha": "abc123"},
        auth_header="Authorization: Bearer ghs_secret",
    )

    with pytest.raises(RuntimeError) as exc_info:
        with RepoWorkspaceManager(fallback_to_diff_only=False).create_session(provider):
            pass

    message = str(exc_info.value)
    assert "ghs_secret" not in message
    assert "http.extraHeader=Authorization" not in message
    assert "Authorization: Bearer ***" in message


def test_primary_checkout_cleans_tempdir_when_context_raises(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = FakeProvider(spec={"repo_url": "https://github.com/org/repo.git", "pr_num": 7, "head_sha": "abc123"})

    with pytest.raises(RuntimeError):
        with RepoWorkspaceManager().create_session(provider) as session:
            temp_root = session.primary.root
            assert Path(temp_root).exists()
            raise RuntimeError("boom")

    assert not Path(temp_root).exists()
