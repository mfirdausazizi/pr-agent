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
            max_agent_rounds=0,
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
        bundle = builder.build(diff_files=[diff_file], max_agent_rounds=0)

    assert bundle.status == "ok"
    assert any(
        snippet.path == "routes/users.js" and "db_delete('users', { id })" in snippet.content
        for snippet in bundle.snippets
    )
    assert not any(snippet.path.startswith(".coolify/") for snippet in bundle.snippets)
