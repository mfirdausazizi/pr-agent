import subprocess

from pr_agent.algo.repo_context.context_builder import RepoContextBuilder
from pr_agent.algo.repo_context.models import WorkspaceRepo
from pr_agent.algo.repo_context.searcher import RepoContextSearcher, search_text
from pr_agent.algo.repo_context.workspace import RepoWorkspaceManager
from pr_agent.algo.types import FilePatchInfo


def test_search_text_finds_matches_across_allowed_tracked_files(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / "a.py").write_text("alpha\nneedle here\nomega\n")
    (repo_b / "b.py").write_text("first\nsecond needle\nthird\n")
    repos = [
        WorkspaceRepo("repo-a", repo_a, {"a.py"}),
        WorkspaceRepo("repo-b", repo_b, {"b.py"}),
    ]

    snippets = search_text(repos, "needle", max_results=10)

    assert [(snippet.repo_name, snippet.path, snippet.start_line) for snippet in snippets] == [
        ("repo-a", "a.py", 1),
        ("repo-b", "b.py", 1),
    ]
    assert snippets[0].score > 0


def test_search_text_respects_include_exclude_and_result_cap(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n")
    (tmp_path / "src" / "skip.py").write_text("needle\n")
    (tmp_path / "README.md").write_text("needle\n")
    repo = WorkspaceRepo("repo", tmp_path, {"src/app.py", "src/skip.py", "README.md"})

    snippets = search_text(
        [repo],
        "needle",
        max_results=1,
        include_globs=["src/*.py"],
        exclude_globs=["src/skip.py"],
    )

    assert [(snippet.path, snippet.start_line) for snippet in snippets] == [("src/app.py", 1)]


def test_search_text_skips_missing_files_and_stops_after_file_cap(tmp_path):
    (tmp_path / "first.txt").write_text("needle\n")
    (tmp_path / "second.txt").write_text("needle\n")
    repo = WorkspaceRepo("repo", tmp_path, {"missing.txt", "first.txt", "second.txt"})

    snippets = search_text([repo], "needle", max_results=10, max_files_scanned=2)

    assert [snippet.path for snippet in snippets] == ["first.txt"]


def test_production_searcher_and_builder_find_db_delete_call_site(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app").mkdir()
    (repo / "tests").mkdir()
    (repo / "app" / "db.py").write_text("def db_delete(user_id): \n    return client.delete(user_id)\n")
    (repo / "app" / "service.py").write_text(
        "from app.db import db_delete\n\n"
        "def remove_user(user_id): \n"
        "    return db_delete(user_id)\n"
    )
    (repo / "tests" / "test_db.py").write_text(
        "from app.db import db_delete\n\n"
        "def test_db_delete(): \n"
        "    assert db_delete(1)\n"
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)

    provider = type(
        "Provider",
        (),
        {
            "get_repo_context_local_root": lambda self: str(repo),
            "get_repo_context_primary_checkout_spec": lambda self: None,
        },
    )()

    with RepoWorkspaceManager().create_session(provider) as session:
        searcher = RepoContextSearcher(session)
        builder = RepoContextBuilder(workspace_session=session, searcher=searcher)
        bundle = builder.build(
            diff_files=[{"path": "app/db.py", "changed_ranges": [{"start": 1, "end": 2}]}],
        )

    assert bundle.status == "ok"
    assert any(snippet.path == "app/service.py" and "db_delete(user_id)" in snippet.content for snippet in bundle.snippets)  # noqa: E501
    assert any(snippet.context_type == "verification" and snippet.path ==
               "tests/test_db.py" for snippet in bundle.snippets)
    assert not any(str(tmp_path) in snippet.path for snippet in bundle.snippets)


def test_production_builder_finds_js_callers_for_changed_function_body(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core").mkdir()
    (repo / "routes").mkdir()
    (repo / "core" / "common.js").write_text(
        "function db_delete(table, data) {\n"
        "  let query = `DELETE FROM ${table}`;\n"
        "  if (!Array.isArray(data) || data.length === 0) {\n"
        "    throw new Error('blocked');\n"
        "  }\n"
        "  return query;\n"
        "}\n"
        "module.exports = { db_delete };\n"
    )
    (repo / "routes" / "users.js").write_text(
        "const { db_delete } = require('../core/common');\n"
        "function removeUser(id) {\n"
        "  return db_delete('users', { id });\n"
        "}\n"
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)

    patch = "\n".join(
        [
            "@@ -1,6 +1,6 @@",
            " function db_delete(table, data) {",
            "   let query = `DELETE FROM ${table}`;",
            "+  if (!Array.isArray(data) || data.length === 0) {",
            "+    throw new Error('blocked');",
            "   }",
            "   return query;",
        ]
    )
    diff_file = FilePatchInfo(
        base_file="",
        head_file=(repo / "core" / "common.js").read_text(),
        patch=patch,
        filename="core/common.js",
    )
    provider = type(
        "Provider",
        (),
        {
            "get_repo_context_local_root": lambda self: str(repo),
            "get_repo_context_primary_checkout_spec": lambda self: None,
        },
    )()

    with RepoWorkspaceManager().create_session(provider) as session:
        searcher = RepoContextSearcher(session)
        builder = RepoContextBuilder(workspace_session=session, searcher=searcher)
        bundle = builder.build(diff_files=[diff_file])

    assert bundle.status == "ok"
    assert any(
        snippet.path == "routes/users.js" and "db_delete('users', { id })" in snippet.content
        for snippet in bundle.snippets
    )
    assert not any(snippet.path.startswith(".coolify/") for snippet in bundle.snippets)


def test_reference_search_prioritizes_call_sites_over_definition(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core").mkdir()
    (repo / "routes").mkdir()
    (repo / "core" / "common.js").write_text(
        "const Common = {\n"
        "  db_delete: async function(table, data) {\n"
        "    return data;\n"
        "  }\n"
        "};\n"
    )
    (repo / "routes" / "admin.js").write_text(
        "await Common.db_delete('teams', [{ id }]);\n"
        "await Common.db_delete('users', [{ id }]);\n"
    )
    (repo / "routes" / "rest-api.js").write_text(
        "await Common.db_delete('chats', [{ id }]);\n"
        "await Common.db_delete('messages', [{ id }]);\n"
    )

    snippets = RepoContextSearcher(
        SimpleSession([WorkspaceRepo("primary", repo, {
            "core/common.js",
            "routes/admin.js",
            "routes/rest-api.js",
        })])
    ).find_references({"name": "db_delete", "path": "core/common.js"}, limit=3)

    assert all(snippet.path != "core/common.js" for snippet in snippets)
    assert any(snippet.path == "routes/rest-api.js" for snippet in snippets)


def test_reference_search_diversifies_call_sites_across_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core").mkdir()
    (repo / "routes").mkdir()
    (repo / "tests").mkdir()
    (repo / "core" / "common.js").write_text("async function db_delete(table, data) { return data; }\n")
    (repo / "routes" / "admin.js").write_text(
        "\n".join(f"await Common.db_delete('admin_{index}', [id]);" for index in range(8)) + "\n"
    )
    (repo / "routes" / "rest-api.js").write_text("await Common.db_delete('rest', [id]);\n")
    (repo / "routes" / "tenant.js").write_text("await Common.db_delete('tenant', [id]);\n")
    (repo / "tests" / "test_common.js").write_text("await Common.db_delete('test', [id]);\n")

    snippets = RepoContextSearcher(
        SimpleSession([WorkspaceRepo("primary", repo, {
            "core/common.js",
            "routes/admin.js",
            "routes/rest-api.js",
            "routes/tenant.js",
            "tests/test_common.js",
        })])
    ).find_references({"name": "db_delete", "path": "core/common.js"}, limit=5)

    paths = [snippet.path for snippet in snippets]
    assert "routes/rest-api.js" in paths
    assert "routes/tenant.js" in paths
    assert "tests/test_common.js" not in paths
    assert len(set(paths)) >= 3


def test_reference_audit_counts_all_tracked_allowed_files_beyond_snippet_limits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core").mkdir()
    (repo / "routes").mkdir()
    (repo / "tests").mkdir()
    (repo / "core" / "common.js").write_text("async function db_delete(table, data) { return data; }\n")
    (repo / "routes" / "admin.js").write_text(
        "\n".join(f"await Common.db_delete('admin_{index}', [id]);" for index in range(9)) + "\n"
    )
    (repo / "routes" / "rest-api.js").write_text(
        "\n".join(f"await db_delete('rest_{index}', [id]);" for index in range(5)) + "\n"
    )
    (repo / "routes" / "tenant.js").write_text("await Common.db_delete('tenant', [id]);\n")
    (repo / "tests" / "test_common.js").write_text("await Common.db_delete('test', [id]);\n")
    (repo / ".secrets").write_text("db_delete should never be read\n")

    snippet = RepoContextSearcher(
        SimpleSession([WorkspaceRepo("primary", repo, {
            ".secrets",
            "core/common.js",
            "routes/admin.js",
            "routes/rest-api.js",
            "routes/tenant.js",
            "tests/test_common.js",
        })]),
        settings={"excluded_globs": ["tests/*"]},
    ).find_reference_audit({"name": "db_delete", "path": "core/common.js"}, limit=2)

    assert snippet is not None
    assert snippet.context_type == "audit"
    assert snippet.path == "repo-context-audit"
    assert "completed scan of tracked, non-excluded files" in snippet.content
    assert "Found 16 references across 4 files" in snippet.content
    assert "routes/admin.js (9)" in snippet.content
    assert "routes/rest-api.js (5)" in snippet.content
    assert "routes/tenant.js (1)" in snippet.content
    assert "core/common.js (1)" in snippet.content
    assert "test_common.js" not in snippet.content
    assert ".secrets" not in snippet.content
    assert str(tmp_path) not in snippet.content


def test_reference_audit_reports_all_detected_call_sites_pass_arrays(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core").mkdir()
    (repo / "routes").mkdir()
    (repo / "core" / "common.js").write_text(
        "async function db_delete(table, data) { return data; }\n"
        "const helpers = { db_delete: async function(table, data) { return data; } };\n"
    )
    (repo / "routes" / "admin.js").write_text(
        "await Common.db_delete('admin', [id, { hardDelete: true }]);\n"
        "await db_delete('tenant', [tenantId]);\n"
    )
    (repo / "routes" / "rest-api.js").write_text("return Common.db_delete('rest', [restId]);\n")

    snippet = RepoContextSearcher(
        SimpleSession([WorkspaceRepo("primary", repo, {
            "core/common.js",
            "routes/admin.js",
            "routes/rest-api.js",
        })])
    ).find_reference_audit({"name": "db_delete", "path": "core/common.js"})

    assert snippet is not None
    assert "Call argument shape audit: 3 array-form calls, 0 object-form calls, 0 missing/other calls, 0 unknown." in (
        snippet.content
    )
    assert "All detected call sites pass an array as the second argument." in snippet.content


def test_reference_audit_reports_bad_and_unknown_call_argument_shapes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "routes").mkdir()
    (repo / "routes" / "object.js").write_text("await Common.db_delete('object', { id });\n")
    (repo / "routes" / "missing.js").write_text("await db_delete('missing');\n")
    (repo / "routes" / "other.js").write_text("await Common.db_delete('other', id);\n")
    (repo / "routes" / "unknown.js").write_text("await Common.db_delete('unknown',\n    [id]);\n")

    snippet = RepoContextSearcher(
        SimpleSession([WorkspaceRepo("primary", repo, {
            "routes/missing.js",
            "routes/object.js",
            "routes/other.js",
            "routes/unknown.js",
        })])
    ).find_reference_audit("db_delete")

    assert snippet is not None
    assert "Call argument shape audit: 0 array-form calls, 1 object-form calls, 2 missing/other calls, 1 unknown." in (
        snippet.content
    )
    assert "All detected call sites pass an array as the second argument." not in snippet.content
    assert "object: routes/object.js:1" in snippet.content
    assert "missing: routes/missing.js:1" in snippet.content
    assert "other: routes/other.js:1" in snippet.content
    assert "unknown: routes/unknown.js:1" in snippet.content


def test_reference_audit_marks_partial_when_file_cap_stops_scan(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text("db_delete('a');\n")
    (repo / "b.js").write_text("db_delete('b');\n")
    (repo / "c.js").write_text("db_delete('c');\n")

    snippet = RepoContextSearcher(
        SimpleSession([WorkspaceRepo("primary", repo, {"a.js", "b.js", "c.js"})]),
        settings={"max_files_scanned": 2},
    ).find_reference_audit("db_delete")

    assert snippet is not None
    assert "partial scan" in snippet.content
    assert "max_files_scanned reached" in snippet.content
    assert "Found 2 references across 2 files" in snippet.content


class SimpleSession:
    def __init__(self, repos):
        self.repos = repos
