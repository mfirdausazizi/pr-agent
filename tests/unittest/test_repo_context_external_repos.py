import subprocess
from pathlib import Path

from pr_agent.git_providers.git_provider import GitProvider


class FakeProvider:
    def get_repo_context_auth_header(self):
        return "Authorization: Bearer ghs_secret"

    def sanitize_repo_context_url(self, value):
        return GitProvider.sanitize_repo_context_url(self, value)

    def get_repo_context_local_root(self):
        return None

    def get_repo_context_primary_checkout_spec(self):
        return None


def test_external_repos_respect_include_exclude_and_max_count(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = RepoWorkspaceManager(
        external_repos=[
            {"url": "https://github.com/org/one.git", "ref": "main", "name": "one"},
            {"url": "https://github.com/org/two.git", "ref": "develop", "name": "two"},
            {"url": "https://github.com/org/three.git", "ref": "main", "name": "three"},
        ],
        allowed_external_repo_urls=[
            "https://github.com/org/one.git",
            "https://github.com/org/two.git",
            "https://github.com/org/three.git",
        ],
        include_external_repos=["https://github.com/org/one.git", "https://github.com/org/two.git"],
        exclude_external_repos=["https://github.com/org/two.git"],
        max_external_repos=1,
    )

    with manager.create_session(FakeProvider()) as session:
        assert [repo.name for repo in session.external_repos] == ["one"]
        assert session.external_repos[0].status == "ready"

    fetch_commands = [command for command in commands if "fetch" in command]
    assert len(fetch_commands) == 1
    assert "https://github.com/org/one.git" in fetch_commands[0]
    assert "main" in fetch_commands[0]


def test_external_repo_unsupported_url_is_rejected_without_fetch(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = RepoWorkspaceManager(
        external_repos=[
            {"url": "ssh://github.com/org/private.git", "ref": "main", "name": "private"},
            {"url": "https://evil.example.com/org/repo.git", "ref": "main", "name": "evil"},
            {"url": "https://github.com/org/not-allowed.git", "ref": "main", "name": "not-allowed"},
        ],
        allowed_external_repo_urls=["https://github.com/org/allowed.git"],
    )

    with manager.create_session(FakeProvider()) as session:
        assert [repo.status for repo in session.external_repos] == [
            "unsupported_url",
            "unsupported_url",
            "not_allowlisted",
        ]

    assert not any("fetch" in command for command in commands)


def test_external_repo_requires_explicit_allowlist(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = RepoWorkspaceManager(
        external_repos=[{"url": "https://github.com/org/one.git", "ref": "main", "name": "one"}],
    )

    with manager.create_session(FakeProvider()) as session:
        assert session.external_repos[0].status == "not_allowlisted"

    assert not any("fetch" in command for command in commands)


def test_external_repo_fetch_failure_is_skipped_with_status(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    def fake_run(command, **kwargs):
        if "fetch" in command:
            raise subprocess.CalledProcessError(1, command, stderr="auth failed")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = RepoWorkspaceManager(
        external_repos=[{"url": "https://github.com/org/one.git", "ref": "main", "name": "one"}],
        allowed_external_repo_urls=["https://github.com/org/one.git"],
    )

    with manager.create_session(FakeProvider()) as session:
        temp_root = session.external_repos[0].root
        assert session.external_repos[0].status == "fetch_failed"
        assert "ghs_secret" not in session.external_repos[0].message
        assert Path(temp_root).exists()

    assert not Path(temp_root).exists()


def test_external_repo_fetch_uses_header_not_tokenized_remote(monkeypatch):
    from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = RepoWorkspaceManager(
        external_repos=[{"url": "https://github.com/org/one.git", "ref": "main", "name": "one"}],
        allowed_external_repo_urls=["https://github.com/org/one.git"],
    )

    with manager.create_session(FakeProvider()):
        pass

    fetch_commands = [command for command in commands if "fetch" in command]
    assert fetch_commands
    assert "http.extraHeader=Authorization: Bearer ghs_secret" in fetch_commands[0]
    assert "https://github.com/org/one.git" in fetch_commands[0]
    assert not any("ghs_secret@github.com" in " ".join(command) for command in commands)
    assert not any(command[:3] == ["git", "remote", "add"] for command in commands)
