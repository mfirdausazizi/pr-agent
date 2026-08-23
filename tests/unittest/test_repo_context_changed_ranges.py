from pr_agent.algo.repo_context.changed_ranges import parse_changed_ranges
from pr_agent.algo.repo_context.models import ChangedRange
from pr_agent.algo.types import FilePatchInfo


def _file_patch(patch: str, filename: str = "app.py") -> FilePatchInfo:
    return FilePatchInfo(base_file="", head_file="", patch=patch, filename=filename)


def test_parse_changed_ranges_returns_added_lines_from_new_file_coordinates():
    patch = """@@ -1,3 +1,4 @@
 context
-old
+new
+second
 tail
"""

    assert parse_changed_ranges(_file_patch(patch)) == [
        ChangedRange("app.py", 2, 3),
    ]


def test_parse_changed_ranges_handles_added_file_starting_at_line_one():
    patch = """@@ -0,0 +1,3 @@
+first
+second
+third
"""

    assert parse_changed_ranges(_file_patch(patch, "new.py")) == [
        ChangedRange("new.py", 1, 3),
    ]


def test_parse_changed_ranges_keeps_separate_hunks_separate():
    patch = """@@ -1,2 +1,3 @@
 unchanged
+added one
 keep
@@ -10,2 +11,3 @@
 unchanged
+added two
 keep
"""

    assert parse_changed_ranges(_file_patch(patch)) == [
        ChangedRange("app.py", 2, 2),
        ChangedRange("app.py", 12, 12),
    ]


def test_parse_changed_ranges_ignores_deleted_only_hunks():
    patch = """@@ -1,3 +1,2 @@
 keep
-removed
 tail
"""

    assert parse_changed_ranges(_file_patch(patch)) == []
