import time
from dataclasses import dataclass, field
from typing import Any

from pr_agent.algo.repo_context.symbol_extractor import extract_changed_symbols


@dataclass(frozen=True)
class RepoContextSnippet:
    repo: str
    repo_label: str
    path: str
    start: int
    end: int
    content: str
    score: int = 0
    context_type: str = "context"


@dataclass
class RepoContextBundle:
    status: str
    snippets: list[RepoContextSnippet] = field(default_factory=list)
    reason: str = ""


class RepoContextBuilder:
    def __init__(
        self,
        workspace_session: Any,
        searcher: Any,
        raise_on_error: bool = False,
    ):
        self.workspace_session = workspace_session
        self.searcher = searcher
        self.raise_on_error = raise_on_error

    def build(
        self,
        diff_files: list[Any],
        max_wall_time_sec: float = 10,
        max_queries_per_round: int = 5,
    ) -> RepoContextBundle:
        started_at = time.monotonic()
        try:
            bundle = RepoContextBundle(status="ok")
            self._add_seed_context(bundle, diff_files, max_queries_per_round, started_at, max_wall_time_sec)
            bundle.snippets = self._dedupe_and_rank(bundle.snippets)
            return bundle
        except Exception as exc:
            if self.raise_on_error:
                raise
            return RepoContextBundle(status="unavailable", reason=str(exc))

    def _add_seed_context(self, bundle: RepoContextBundle, diff_files: list[Any], limit: int,
                          started_at: float, max_wall_time_sec: float) -> None:
        changed_symbols = self._extract_changed_symbols(diff_files, limit)
        for symbol in changed_symbols:
            if self._deadline_passed(started_at, max_wall_time_sec):
                return
            reference_limit = max(limit * 2, 10)
            if hasattr(self.searcher, "find_reference_audit"):
                self._extend_snippets(
                    bundle,
                    [self.searcher.find_reference_audit(symbol, limit=reference_limit)],
                    score=1000,
                    context_type="audit",
                )
            self._extend_snippets(bundle, self.searcher.find_references(symbol, limit=reference_limit), score=95)
            symbol_path = _get(symbol, "path")
            if symbol_path:
                self._extend_snippets(bundle, self.searcher.find_importers(symbol_path, limit=limit), score=70)
                self._extend_snippets(
                    bundle,
                    self.searcher.find_tests(symbol_path, limit=limit),
                    score=80,
                    context_type="verification",
                )
        if changed_symbols:
            return

        for diff_file in diff_files:
            if self._deadline_passed(started_at, max_wall_time_sec):
                return
            path = _get(diff_file, "path") or _get(diff_file, "filename") or _get(diff_file, "head_file")
            if not path:
                continue
            ranges = _get(diff_file, "changed_ranges") or [{"start": None, "end": None}]
            for changed_range in ranges:
                start = _get(changed_range, "start") or _get(changed_range, "start_line")
                end = _get(changed_range, "end") or _get(changed_range, "end_line")
                symbols = self.searcher.find_symbols(path, start_line=start, end_line=end)
                for symbol in symbols or []:
                    self._extend_snippets(bundle, self.searcher.find_references(symbol, limit=limit), score=90)
            self._extend_snippets(bundle, self.searcher.find_importers(path, limit=limit), score=70)
            self._extend_snippets(
                bundle,
                self.searcher.find_tests(path, limit=limit),
                score=80,
                context_type="verification",
            )

    @staticmethod
    def _extract_changed_symbols(diff_files: list[Any], limit: int) -> list[Any]:
        try:
            return extract_changed_symbols(diff_files, max_symbols=limit)
        except Exception:
            return []

    def _extend_snippets(
        self,
        bundle: RepoContextBundle,
        raw_snippets: list[Any],
        score: int = 50,
        context_type: str = "context",
    ) -> None:
        for raw in raw_snippets or []:
            repo = _get(raw, "repo") or _get(raw, "repo_name") or "primary"
            repo_label = _get(raw, "repo_label") or _get(raw, "repo_name") or repo
            path = _get(raw, "path")
            content = _get(raw, "content") or ""
            if not path or not content:
                continue
            snippet_type = _get(raw, "context_type") or context_type
            bundle.snippets.append(
                RepoContextSnippet(
                    repo=repo,
                    repo_label=repo_label,
                    path=path,
                    start=_get(raw, "start") or _get(raw, "start_line") or 1,
                    end=_get(raw, "end") or _get(raw, "end_line") or _get(raw, "start") or _get(raw, "start_line") or 1,
                    content=content,
                    score=_get(raw, "score") or score,
                    context_type=snippet_type,
                )
            )

    def _dedupe_and_rank(self, snippets: list[RepoContextSnippet]) -> list[RepoContextSnippet]:
        deduped = {}
        for snippet in snippets:
            key = (snippet.repo, snippet.path, snippet.start, snippet.end, snippet.content)
            existing = deduped.get(key)
            if existing is None or snippet.score > existing.score:
                deduped[key] = snippet
        return sorted(
            deduped.values(),
            key=lambda item: (
                item.repo != "primary" and item.repo_label != "primary",
                item.context_type != "audit",
                item.context_type != "verification",
                -item.score,
                item.path,
                item.start,
            ),
        )

    @staticmethod
    def _deadline_passed(started_at: float, max_wall_time_sec: float) -> bool:
        return time.monotonic() - started_at >= max_wall_time_sec


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
