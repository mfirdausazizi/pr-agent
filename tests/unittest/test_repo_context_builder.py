from pr_agent.algo.repo_context.context_builder import RepoContextBuilder
from pr_agent.algo.types import FilePatchInfo


class FakeSession:
    repo_label = "primary"
    repo_root = "/var/folders/tmp/checkout"

    def open_file(self, path, start=None, end=None):
        lines = {
            "app/db.py": "def db_delete(user_id): \n    return client.delete(user_id)\n",
            "app/service.py": "from app.db import db_delete\n\ndef remove_user(user_id): \n    return db_delete(user_id)\n",  # noqa: E501
        }[path].splitlines()
        selected = lines[(start or 1) - 1: end]
        return "\n".join(selected)


class FakeSearcher:
    def find_symbols(self, path, start_line=None, end_line=None):
        return [{"name": "db_delete", "path": "app/db.py", "start": 1, "end": 2}]

    def find_references(self, symbol, limit=5):
        return [
            {
                "repo": "primary",
                "repo_label": "primary",
                "path": "app/service.py",
                "start": 3,
                "end": 4,
                "content": "def remove_user(user_id): \n    return db_delete(user_id)",
            }
        ]

    def find_reference_audit(self, symbol, limit=5):
        return {
            "repo": "primary",
            "repo_label": "primary",
            "path": "repo-context-audit",
            "start": 1,
            "end": 1,
            "content": (
                "Exact reference audit for db_delete: completed scan of tracked, non-excluded files. "
                "Found 2 references across 2 files: app/db.py (1), app/service.py (1)."
            ),
            "context_type": "audit",
            "score": 1000,
        }

    def find_importers(self, path, limit=5):
        return []

    def find_tests(self, path, limit=5):
        return [
            {
                "repo": "primary",
                "repo_label": "primary",
                "path": "tests/test_db.py",
                "start": 1,
                "end": 2,
                "content": "def test_db_delete(): \n    assert db_delete(1)",
            }
        ]

    def search_text(self, query, limit=5):
        return []


def test_builder_deterministic_seed_includes_db_delete_caller_snippet():
    builder = RepoContextBuilder(workspace_session=FakeSession(), searcher=FakeSearcher())

    bundle = builder.build(
        diff_files=[
            FilePatchInfo(
                base_file="",
                head_file="def db_delete(user_id):\n    return client.delete(user_id)\n",
                patch="@@ -1,2 +1,2 @@\n def db_delete(user_id):\n+    return client.delete(user_id)\n",
                filename="app/db.py",
            )
        ],
        max_agent_rounds=0,
    )

    assert bundle.status == "ok"
    assert any(snippet.path == "app/service.py" and "db_delete(user_id)" in snippet.content for snippet in bundle.snippets)  # noqa: E501
    assert any(snippet.context_type == "verification" and snippet.path ==
               "tests/test_db.py" for snippet in bundle.snippets)


def test_builder_includes_reference_audit_before_changed_symbol_snippets():
    builder = RepoContextBuilder(workspace_session=FakeSession(), searcher=FakeSearcher())

    bundle = builder.build(
        diff_files=[
            FilePatchInfo(
                base_file="",
                head_file="def db_delete(user_id):\n    return client.delete(user_id)\n",
                patch="@@ -1,2 +1,2 @@\n def db_delete(user_id):\n+    return client.delete(user_id)\n",
                filename="app/db.py",
            )
        ],
        max_agent_rounds=0,
    )

    audit_index = next(index for index, snippet in enumerate(bundle.snippets) if snippet.context_type == "audit")
    reference_index = next(index for index, snippet in enumerate(bundle.snippets) if snippet.path == "app/service.py")
    assert audit_index < reference_index
    assert bundle.snippets[audit_index].path == "repo-context-audit"
    assert "Exact reference audit for db_delete" in bundle.snippets[audit_index].content


def test_builder_expands_reference_limit_for_changed_symbols():
    class CapturingSearcher(FakeSearcher):
        def __init__(self):
            self.reference_limits = []

        def find_references(self, symbol, limit=5):
            self.reference_limits.append(limit)
            return super().find_references(symbol, limit=limit)

    searcher = CapturingSearcher()
    builder = RepoContextBuilder(workspace_session=FakeSession(), searcher=searcher)

    builder.build(
        diff_files=[
            FilePatchInfo(
                base_file="",
                head_file="def db_delete(user_id):\n    return client.delete(user_id)\n",
                patch="@@ -1,2 +1,2 @@\n def db_delete(user_id):\n+    return client.delete(user_id)\n",
                filename="app/db.py",
            )
        ],
        max_agent_rounds=0,
        max_queries_per_round=5,
    )

    assert searcher.reference_limits == [10]


class FailingSearcher(FakeSearcher):
    def find_symbols(self, path, start_line=None, end_line=None):
        raise RuntimeError("index unavailable")


def test_builder_falls_back_to_unavailable_bundle_when_search_fails():
    builder = RepoContextBuilder(
        workspace_session=FakeSession(),
        searcher=FailingSearcher(),
        fail_open=False,
    )

    bundle = builder.build(diff_files=[{"path": "app/db.py", "changed_ranges": [{"start": 1, "end": 2}]}])

    assert bundle.status == "unavailable"
    assert bundle.snippets == []
    assert "index unavailable" in bundle.reason


class Planner:
    def __call__(self, bundle, round_index):
        raise RuntimeError("planner down")


def test_builder_falls_back_when_planner_fails():
    builder = RepoContextBuilder(
        workspace_session=FakeSession(),
        searcher=FakeSearcher(),
        planner=Planner(),
        fail_open=False,
    )

    bundle = builder.build(
        diff_files=[{"path": "app/db.py", "changed_ranges": [{"start": 1, "end": 2}]}],
        max_agent_rounds=1,
    )

    assert bundle.status == "unavailable"
    assert "planner down" in bundle.reason


class UnsafePlanner:
    def __call__(self, bundle, round_index):
        return [{"type": "open_file", "path": "../secret.py"}]


def test_builder_rejects_unsafe_planner_actions_before_opening_files():
    builder = RepoContextBuilder(
        workspace_session=FakeSession(),
        searcher=FakeSearcher(),
        planner=UnsafePlanner(),
        fail_open=False,
    )

    bundle = builder.build(
        diff_files=[{"path": "app/db.py", "changed_ranges": [{"start": 1, "end": 2}]}],
        max_agent_rounds=1,
    )

    assert bundle.status == "unavailable"
    assert "Unsafe repo path" in bundle.reason
