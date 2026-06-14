import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pr_agent.algo.repo_context.agent_planner import parse_planner_actions


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
        planner: Callable[[RepoContextBundle, int], Any] | None = None,
        fail_open: bool = True,
    ):
        self.workspace_session = workspace_session
        self.searcher = searcher
        self.planner = planner
        self.fail_open = fail_open

    def build(
        self,
        diff_files: list[Any],
        max_wall_time_sec: float = 10,
        max_agent_rounds: int = 1,
        max_queries_per_round: int = 5,
    ) -> RepoContextBundle:
        started_at = time.monotonic()
        try:
            bundle = RepoContextBundle(status="ok")
            self._add_seed_context(bundle, diff_files, max_queries_per_round)
            for round_index in range(max_agent_rounds):
                if self._deadline_passed(started_at, max_wall_time_sec):
                    break
                self._run_agent_round(bundle, round_index, max_queries_per_round)
            bundle.snippets = self._dedupe_and_rank(bundle.snippets)
            return bundle
        except Exception as exc:
            if self.fail_open:
                raise
            return RepoContextBundle(status="unavailable", reason=str(exc))

    def _add_seed_context(self, bundle: RepoContextBundle, diff_files: list[Any], limit: int) -> None:
        for diff_file in diff_files:
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

    def _run_agent_round(self, bundle: RepoContextBundle, round_index: int, limit: int) -> None:
        if not self.planner:
            return
        planned = self.planner(bundle, round_index)
        if isinstance(planned, str):
            actions = parse_planner_actions(planned, limit)
        else:
            actions = parse_planner_actions(json.dumps({"actions": list(planned or [])}), limit)
        for action in actions[:limit]:
            self._run_action(bundle, action, limit)

    def _run_action(self, bundle: RepoContextBundle, action: dict[str, Any], limit: int) -> None:
        action_type = action.get("type")
        if action_type == "find_references":
            self._extend_snippets(bundle, self.searcher.find_references(action.get("symbol") or action, limit=limit))
        elif action_type == "search_text":
            self._extend_snippets(bundle, self.searcher.search_text(action.get("query", ""), limit=limit))
        elif action_type == "find_importers":
            self._extend_snippets(bundle, self.searcher.find_importers(action.get("path", ""), limit=limit))
        elif action_type == "find_tests":
            self._extend_snippets(
                bundle,
                self.searcher.find_tests(action.get("path", ""), limit=limit),
                context_type="verification",
            )
        elif action_type == "open_file":
            self._open_file(bundle, action)

    def _open_file(self, bundle: RepoContextBundle, action: dict[str, Any]) -> None:
        path = action.get("path")
        if not path:
            return
        start = action.get("start")
        end = action.get("end")
        content = self.workspace_session.open_file(path, start=start, end=end)
        bundle.snippets.append(
            RepoContextSnippet(
                repo=getattr(self.workspace_session, "repo_label", "primary"),
                repo_label=getattr(self.workspace_session, "repo_label", "primary"),
                path=path,
                start=start or 1,
                end=end or start or 1,
                content=content,
                score=60,
            )
        )

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
