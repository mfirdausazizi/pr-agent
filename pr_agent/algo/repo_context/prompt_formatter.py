import re
from pathlib import PurePosixPath

from pr_agent.algo.repo_context.context_builder import RepoContextBundle, RepoContextSnippet


def reserve_diff_tokens(total_tokens: int, requested_context_tokens: int, min_diff_tokens_reserved: int) -> int:
    return max(0, min(requested_context_tokens, total_tokens - min_diff_tokens_reserved))


def format_repo_context(
    bundle: RepoContextBundle,
    token_handler,
    max_tokens: int,
    include_external: bool = True,
) -> tuple[str, str]:
    if bundle.status != "ok":
        return f"Repository context unavailable: {bundle.reason}".strip(), "unavailable"

    snippets = [snippet for snippet in bundle.snippets if include_external or _is_primary(snippet)]
    selected, clipped = _select_snippets(snippets, token_handler, max_tokens)
    audit_summary = _format_audit_summary(snippets if clipped else selected, clipped)
    while selected and _count_tokens(_render(selected, audit_summary), token_handler) > max_tokens:
        clipped = True
        audit_summary = _format_audit_summary(snippets, clipped)
        selected.pop()
    text = _render(selected, audit_summary)
    return text, "partial" if clipped else "ok"


def _select_snippets(
    snippets: list[RepoContextSnippet],
    token_handler,
    max_tokens: int,
) -> tuple[list[RepoContextSnippet], bool]:
    ordered = sorted(
        snippets,
        key=lambda snippet: (
            not _is_primary(snippet),
            snippet.context_type != "audit",
            snippet.context_type != "verification",
            -snippet.score,
            snippet.path,
            snippet.start,
        ),
    )
    selected = []
    clipped = False
    for snippet in ordered:
        candidate = selected + [snippet]
        if _count_tokens(_render(candidate), token_handler) <= max_tokens:
            selected.append(snippet)
        else:
            clipped = True
            if not _is_primary(snippet):
                continue
            compact = _clip_content(snippet, token_handler, max_tokens, selected)
            if compact is not None:
                selected.append(compact)
    return selected, clipped


def _clip_content(
    snippet: RepoContextSnippet,
    token_handler,
    max_tokens: int,
    selected: list[RepoContextSnippet],
) -> RepoContextSnippet | None:
    words = snippet.content.split()
    while words:
        clipped = RepoContextSnippet(
            repo=snippet.repo,
            repo_label=snippet.repo_label,
            path=snippet.path,
            start=snippet.start,
            end=snippet.end,
            content=" ".join(words),
            score=snippet.score,
            context_type=snippet.context_type,
        )
        if _count_tokens(_render(selected + [clipped]), token_handler) <= max_tokens:
            return clipped
        words.pop()
    return None


def _render(snippets: list[RepoContextSnippet], audit_summary: str | None = None) -> str:
    lines = ["Repository context:"]
    if audit_summary:
        lines.append(audit_summary)
    for snippet in snippets:
        lines.append(f"\n[{snippet.repo_label}] {_safe_path(snippet.path)}:{snippet.start}-{snippet.end}")
        lines.append("```")
        lines.append(snippet.content)
        lines.append("```")
    return "\n".join(lines)


def _count_tokens(text: str, token_handler) -> int:
    return token_handler.count_tokens(text)


def _is_primary(snippet: RepoContextSnippet) -> bool:
    return snippet.repo == "primary" or snippet.repo_label == "primary"


def _format_audit_summary(snippets: list[RepoContextSnippet], clipped: bool) -> str:
    call_counts: dict[str, dict[str, int]] = {}
    completed_exact_audits = _completed_exact_audit_symbols(snippets)
    ignored_names = {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "function",
        "return",
        "require",
        "describe",
        "it",
        "test",
    }
    for snippet in snippets:
        if snippet.context_type in {"audit", "verification"}:
            continue
        safe_path = _safe_path(snippet.path)
        for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", snippet.content):
            if name in ignored_names:
                continue
            path_counts = call_counts.setdefault(name, {})
            path_counts[safe_path] = path_counts.get(safe_path, 0) + 1

    summaries = []
    for name, path_counts in sorted(
        call_counts.items(),
        key=lambda item: (-sum(item[1].values()), item[0]),
    ):
        if sum(path_counts.values()) < 2 or len(path_counts) < 2:
            continue
        path_summary = ", ".join(
            f"`{path}` ({count})" for path, count in sorted(path_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        if name in completed_exact_audits:
            if clipped:
                summaries.append(
                    f"- Completed repo-context audit found `{name}` references; context snippets include "
                    f"call-site evidence in {len(path_counts)} files "
                    f"({sum(path_counts.values())} context occurrences): {path_summary}."
                )
            else:
                summaries.append(
                    f"- Completed repo-context audit found `{name}` references; selected call-site evidence appears in "
                    f"{len(path_counts)} files ({sum(path_counts.values())} selected occurrences): {path_summary}."
                )
        elif clipped:
            summaries.append(
                f"- Repo context sample shows `{name}` references in {len(path_counts)} files "
                f"({sum(path_counts.values())} context occurrences, sampled/partial): {path_summary}."
            )
        else:
            summaries.append(
                f"- Available snippets show `{name}` references in {len(path_counts)} files "
                f"({sum(path_counts.values())} selected occurrences): {path_summary}. "
                "This is evidence from selected snippets, not an exhaustive whole-repo audit."
            )
        if len(summaries) >= 3:
            break
    if not summaries:
        return ""
    return "\nRepository context audit summary:\n" + "\n".join(summaries)


def _completed_exact_audit_symbols(snippets: list[RepoContextSnippet]) -> set[str]:
    completed_symbols = set()
    for snippet in snippets:
        if snippet.context_type != "audit":
            continue
        completed_symbols.update(
            re.findall(
                r"\bExact reference audit for ([A-Za-z_][A-Za-z0-9_]*): completed scan\b",
                snippet.content,
                flags=re.IGNORECASE,
            )
        )
    return completed_symbols


def _safe_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    temp_markers = ("/tmp/", "/private/tmp/", "/var/folders/")
    for marker in temp_markers:
        if marker in normalized:
            normalized = normalized.split(marker, 1)[1]
            parts = PurePosixPath(normalized).parts
            return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    pure_path = PurePosixPath(normalized)
    if pure_path.is_absolute():
        return "/".join(pure_path.parts[-2:])
    return normalized
