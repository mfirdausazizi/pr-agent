from pr_agent.algo.repo_context.snippet_extractor import extract_snippet


def test_extract_snippet_returns_bounded_context_and_repo_label(tmp_path):
    path = tmp_path / "src" / "app.py"
    path.parent.mkdir()
    path.write_text("one\nmatch\nthree\nfour\n")

    snippet = extract_snippet(
        repo_name="primary",
        root=tmp_path,
        rel_path="src/app.py",
        line_number=2,
        context_lines=1,
        allowed_files={"src/app.py"},
        exclude_globs=[],
        max_file_bytes=200000,
    )

    assert snippet.repo_name == "primary"
    assert snippet.path == "src/app.py"
    assert snippet.start_line == 1
    assert snippet.end_line == 3
    assert snippet.content == "one\nmatch\nthree"
    assert snippet.reason == "line 2"


def test_extract_snippet_bounds_context_at_file_edges(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("one\ntwo\n")

    snippet = extract_snippet("repo", tmp_path, "app.py", 1, 5, {"app.py"}, [], 200000)

    assert snippet.start_line == 1
    assert snippet.end_line == 2
    assert snippet.content == "one\ntwo"
