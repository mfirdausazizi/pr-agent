import asyncio
from types import SimpleNamespace

import pytest

import pr_agent.servers.github_app as github_app
from pr_agent.identity_providers.identity_provider import Eligibility

API_URL = "https://api.github.com/repos/acme/repo/pulls/1"


@pytest.fixture
def push_env(monkeypatch):
    """Stub everything around the push-trigger dedupe logic (port of upstream 4a1f880d test)."""
    settings = SimpleNamespace(github_app=SimpleNamespace(
        handle_push_trigger=True,
        push_trigger_ignore_merge_commits=True,
        push_trigger_pending_tasks_backlog=True,
    ))
    monkeypatch.setattr(github_app, "get_settings", lambda: settings)
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_app, "_check_pull_request_event",
                        lambda _action, body, _ctx: (body["pull_request"], API_URL))
    monkeypatch.setattr(github_app, "get_identity_provider", lambda: SimpleNamespace(
        verify_eligibility=lambda *_args: Eligibility.ELIGIBLE))
    github_app._duplicate_push_triggers.pop(API_URL, None)
    github_app._pending_task_duplicate_push_conditions.pop(API_URL, None)
    state = {"count": 0}
    yield state
    github_app._duplicate_push_triggers.pop(API_URL, None)
    github_app._pending_task_duplicate_push_conditions.pop(API_URL, None)


def _push():
    body = {"before": "a", "after": "b", "pull_request": {"url": API_URL, "merge_commit_sha": "m"}}
    return github_app.handle_push_trigger_for_new_commits(body, "pull_request", "alice", "1", "synchronize", {},
                                                          agent=None)


def test_cancelled_backlog_waiter_releases_its_slot(push_env, monkeypatch):
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_perform(*_args, **_kwargs):
        push_env["count"] += 1
        if push_env["count"] == 1:
            first_started.set()
            await release_first.wait()

    monkeypatch.setattr(github_app, "_perform_auto_commands_github", fake_perform)

    async def scenario():
        first = asyncio.create_task(_push())
        await asyncio.wait_for(first_started.wait(), timeout=1)

        second = asyncio.create_task(_push())
        for _ in range(10):
            if github_app._duplicate_push_triggers[API_URL] == 2:
                break
            await asyncio.sleep(0)
        assert github_app._duplicate_push_triggers[API_URL] == 2

        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        try:
            assert github_app._duplicate_push_triggers[API_URL] == 1
        finally:
            release_first.set()
            await first

        assert github_app._duplicate_push_triggers[API_URL] == 0
        await asyncio.wait_for(_push(), timeout=1)
        assert push_env["count"] == 2

    asyncio.run(scenario())


def test_backlog_waiter_runs_after_first_task(push_env, monkeypatch):
    release_first = asyncio.Event()

    async def fake_perform(*_args, **_kwargs):
        push_env["count"] += 1
        if push_env["count"] == 1:
            await release_first.wait()

    monkeypatch.setattr(github_app, "_perform_auto_commands_github", fake_perform)

    async def scenario():
        first = asyncio.create_task(_push())
        await asyncio.sleep(0)
        second = asyncio.create_task(_push())
        await asyncio.sleep(0)
        assert await _push() == {}  # third concurrent push is discarded
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        assert push_env["count"] == 2
        assert github_app._duplicate_push_triggers[API_URL] == 0

    asyncio.run(scenario())
