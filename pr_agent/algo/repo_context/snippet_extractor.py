from pathlib import Path

from pr_agent.algo.repo_context.models import RepoContextSnippet
from pr_agent.algo.repo_context.safe_paths import read_safe_text


def extract_snippet(
    repo_name: str,
    root: str | Path,
    rel_path: str,
    line_number: int,
    context_lines: int,
    allowed_files: set[str],
    exclude_globs: list[str] | tuple[str, ...] | None,
    max_file_bytes: int,
) -> RepoContextSnippet:
    text = read_safe_text(root, rel_path, allowed_files, exclude_globs, max_file_bytes)
    lines = text.splitlines()
    if not lines:
        start_line = 1
        end_line = 1
        content = ""
    else:
        bounded_line_number = max(1, min(line_number, len(lines)))
        start_line = max(1, bounded_line_number - context_lines)
        end_line = min(len(lines), bounded_line_number + context_lines)
        content = "\n".join(lines[start_line - 1:end_line])

    return RepoContextSnippet(
        repo_name=repo_name,
        path=rel_path,
        start_line=start_line,
        end_line=end_line,
        content=content,
        reason=f"line {line_number}",
    )
