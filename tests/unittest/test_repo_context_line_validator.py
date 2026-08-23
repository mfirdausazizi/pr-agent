from pr_agent.algo.repo_context.line_validator import validate_key_issues_to_review
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo


def _file_patch(patch: str, filename: str = "app.py", edit_type: EDIT_TYPE = EDIT_TYPE.MODIFIED, old_filename=None):
    return FilePatchInfo(base_file="", head_file="", patch=patch, filename=filename, edit_type=edit_type,
                         old_filename=old_filename)


def test_line_validator_keeps_findings_on_added_lines():
    data = {"review": {"key_issues_to_review": [
        {"relevant_file": "app.py", "issue_header": "Bug", "issue_content": "content", "start_line": 2, "end_line": 2},
    ]}}
    diff_files = [_file_patch("""@@ -1,2 +1,3 @@
 keep
+added
 tail
""")]

    validate_key_issues_to_review(data, diff_files)

    assert data["review"]["key_issues_to_review"][0]["start_line"] == 2


def test_line_validator_snaps_context_line_to_nearest_added_line_in_same_hunk():
    data = {"review": {"key_issues_to_review": [
        {"relevant_file": "app.py", "issue_header": "Bug", "issue_content": "content", "start_line": 3, "end_line": 3},
    ]}}
    diff_files = [_file_patch("""@@ -1,4 +1,5 @@
 one
 unchanged
+added
 tail
""")]

    validate_key_issues_to_review(data, diff_files)

    issue = data["review"]["key_issues_to_review"][0]
    assert issue["start_line"] == 3
    assert issue["end_line"] == 3


def test_line_validator_drops_external_and_unchanged_file_findings():
    data = {"review": {"key_issues_to_review": [
        {"relevant_file": "external.py", "issue_header": "Bug", "issue_content": "content",
         "start_line": 1, "end_line": 1},
        {"relevant_file": "app.py", "issue_header": "Bug", "issue_content": "content",
         "start_line": 20, "end_line": 20},
    ]}}
    diff_files = [_file_patch("""@@ -1,2 +1,3 @@
 keep
+added
 tail
""")]

    validate_key_issues_to_review(data, diff_files)

    assert data["review"]["key_issues_to_review"] == []


def test_line_validator_handles_renamed_and_new_files():
    data = {"review": {"key_issues_to_review": [
        {"relevant_file": "old.py", "issue_header": "Bug", "issue_content": "content", "start_line": 1, "end_line": 1},
        {"relevant_file": "new_file.py", "issue_header": "Bug", "issue_content": "content",
         "start_line": 2, "end_line": 2},
    ]}}
    diff_files = [
        _file_patch("""@@ -1,1 +1,2 @@
+renamed added
 keep
""", filename="renamed.py", edit_type=EDIT_TYPE.RENAMED, old_filename="old.py"),
        _file_patch("""@@ -0,0 +1,2 @@
+first
+second
""", filename="new_file.py", edit_type=EDIT_TYPE.ADDED),
    ]

    validate_key_issues_to_review(data, diff_files)

    issues = data["review"]["key_issues_to_review"]
    assert issues[0]["relevant_file"] == "renamed.py"
    assert issues[0]["start_line"] == 1
    assert issues[1]["relevant_file"] == "new_file.py"


def test_line_validator_ignores_no_newline_marker_before_added_line():
    data = {"review": {"key_issues_to_review": [
        {"relevant_file": "app.py", "issue_header": "Bug", "issue_content": "content", "start_line": 1, "end_line": 1},
    ]}}
    diff_files = [_file_patch("""@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
""")]

    validate_key_issues_to_review(data, diff_files)

    assert data["review"]["key_issues_to_review"][0]["start_line"] == 1
    assert data["review"]["key_issues_to_review"][0]["end_line"] == 1


def test_line_validator_ignores_no_newline_marker_after_added_line():
    data = {"review": {"key_issues_to_review": [
        {"relevant_file": "app.py", "issue_header": "Bug", "issue_content": "content", "start_line": 2, "end_line": 2},
    ]}}
    diff_files = [_file_patch("""@@ -1,1 +1,2 @@
 keep
+added
\\ No newline at end of file
""")]

    validate_key_issues_to_review(data, diff_files)

    issue = data["review"]["key_issues_to_review"][0]
    assert issue["start_line"] == 2
    assert issue["end_line"] == 2
