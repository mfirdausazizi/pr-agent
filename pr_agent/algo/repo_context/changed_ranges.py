import re

from pr_agent.algo.repo_context.models import ChangedRange


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_changed_ranges(file_patch_info) -> list[ChangedRange]:
    patch = file_patch_info.patch or ""
    path = file_patch_info.filename
    ranges: list[ChangedRange] = []
    current_line: int | None = None
    range_start: int | None = None
    previous_added_line: int | None = None

    def flush_range() -> None:
        nonlocal range_start, previous_added_line
        if range_start is not None and previous_added_line is not None:
            ranges.append(ChangedRange(path, range_start, previous_added_line))
        range_start = None
        previous_added_line = None

    for line in patch.splitlines():
        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            flush_range()
            current_line = int(hunk_match.group(1))
            continue

        if current_line is None:
            continue

        if line.startswith("+") and not line.startswith("+++"):
            if range_start is None:
                range_start = current_line
            elif previous_added_line is not None and current_line != previous_added_line + 1:
                flush_range()
                range_start = current_line
            previous_added_line = current_line
            current_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            flush_range()
            current_line += 1

    flush_range()
    return ranges
