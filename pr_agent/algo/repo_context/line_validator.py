import re
from dataclasses import dataclass

from pr_agent.algo.types import EDIT_TYPE

RE_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class HunkAddedLines:
    filename: str
    hunk_start: int
    hunk_end: int
    added_lines: list[int]


def validate_key_issues_to_review(data: dict, diff_files: list) -> dict:
    """
    Keep reviewer findings anchored to changed primary PR lines.

    Findings on context lines are snapped to the nearest added line in the same hunk. Findings outside primary
    changed files, or outside changed hunks, are removed before markdown conversion can publish invalid anchors.
    """
    try:
        review = data.get("review", {})
        issues = review.get("key_issues_to_review")
        if not isinstance(issues, list):
            return data

        hunks_by_file, renamed_files = _build_added_hunks(diff_files)
        validated_issues = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            validated_issue = _validate_issue(issue, hunks_by_file, renamed_files)
            if validated_issue:
                validated_issues.append(validated_issue)
        review["key_issues_to_review"] = validated_issues
    except Exception as e:
        from pr_agent.log import get_logger

        get_logger().warning(f"Failed to validate review line anchors: {e}")
    return data


def _build_added_hunks(diff_files: list) -> tuple[dict[str, list[HunkAddedLines]], dict[str, str]]:
    hunks_by_file = {}
    renamed_files = {}
    for diff_file in diff_files or []:
        filename = getattr(diff_file, "filename", None) or getattr(diff_file, "head_file", None)
        if not filename:
            continue
        old_filename = getattr(diff_file, "old_filename", None)
        if old_filename and getattr(diff_file, "edit_type", None) == EDIT_TYPE.RENAMED:
            renamed_files[old_filename] = filename
        hunks = _parse_added_hunks(filename, getattr(diff_file, "patch", "") or "")
        if hunks:
            hunks_by_file[filename] = hunks
    return hunks_by_file, renamed_files


def _parse_added_hunks(filename: str, patch: str) -> list[HunkAddedLines]:
    hunks = []
    current_hunk_start = None
    current_hunk_end = None
    current_added_lines = []
    new_line = None

    def flush_hunk():
        if current_hunk_start is not None and current_added_lines:
            hunks.append(HunkAddedLines(filename, current_hunk_start, current_hunk_end, current_added_lines.copy()))

    for line in patch.splitlines():
        match = RE_HUNK_HEADER.match(line)
        if match:
            flush_hunk()
            new_start = int(match.group(3))
            new_size = int(match.group(4) or "1")
            current_hunk_start = new_start
            current_hunk_end = new_start + max(new_size, 1) - 1
            current_added_lines = []
            new_line = new_start
            continue
        if current_hunk_start is None:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current_added_lines.append(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1

    flush_hunk()
    return hunks


def _validate_issue(issue: dict, hunks_by_file: dict[str, list[HunkAddedLines]], renamed_files: dict[str, str]):
    relevant_file = str(issue.get("relevant_file", "")).strip()
    relevant_file = renamed_files.get(relevant_file, relevant_file)
    hunks = hunks_by_file.get(relevant_file)
    if not hunks:
        return None

    try:
        start_line = int(str(issue.get("start_line", 0)).strip())
        end_line = int(str(issue.get("end_line", start_line)).strip())
    except (TypeError, ValueError):
        return None
    if start_line <= 0 or end_line <= 0:
        return None
    if end_line < start_line:
        start_line, end_line = end_line, start_line

    for hunk in hunks:
        added_in_range = [line for line in hunk.added_lines if start_line <= line <= end_line]
        if added_in_range:
            issue["relevant_file"] = relevant_file
            issue["start_line"] = min(added_in_range)
            issue["end_line"] = max(added_in_range)
            return issue

    for hunk in hunks:
        if hunk.hunk_start <= start_line <= hunk.hunk_end or hunk.hunk_start <= end_line <= hunk.hunk_end:
            nearest_line = min(hunk.added_lines, key=lambda line: min(abs(line - start_line), abs(line - end_line)))
            issue["relevant_file"] = relevant_file
            issue["start_line"] = nearest_line
            issue["end_line"] = nearest_line
            return issue

    return None
