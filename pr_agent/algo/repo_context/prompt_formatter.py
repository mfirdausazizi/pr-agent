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
    lines = ["Repository context:"]
    for snippet in selected:
        lines.append(f"\n[{snippet.repo_label}] {_safe_path(snippet.path)}:{snippet.start}-{snippet.end}")
        lines.append("```")
        lines.append(snippet.content)
        lines.append("```")
    text = "\n".join(lines)
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


def _render(snippets: list[RepoContextSnippet]) -> str:
    lines = ["Repository context:"]
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
