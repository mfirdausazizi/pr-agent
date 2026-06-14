import fnmatch
import re
import time
from pathlib import Path

from pr_agent.algo.repo_context.models import RepoContextSnippet, WorkspaceRepo
from pr_agent.algo.repo_context.safe_paths import read_safe_text
from pr_agent.algo.repo_context.snippet_extractor import extract_snippet


def _matches_any(path: str, globs: list[str] | tuple[str, ...] | None) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in globs or [])


def _is_included(path: str, include_globs: list[str] | tuple[str, ...] | None) -> bool:
    if not include_globs:
        return True
    return _matches_any(path, include_globs)


def search_text(
    repos: list[WorkspaceRepo],
    query: str,
    max_results: int,
    include_globs: list[str] | tuple[str, ...] | None = None,
    exclude_globs: list[str] | tuple[str, ...] | None = None,
    timeout_sec: int = 10,
    max_files_scanned: int = 12000,
    max_file_bytes: int = 200000,
) -> list[RepoContextSnippet]:
    if not query or max_results <= 0 or max_files_scanned <= 0:
        return []

    start_time = time.monotonic()
    results: list[RepoContextSnippet] = []
    files_scanned = 0

    for repo in repos:
        for rel_path in sorted(repo.tracked_files):
            if files_scanned >= max_files_scanned:
                return results
            if time.monotonic() - start_time > timeout_sec:
                return results
            if not _is_included(rel_path, include_globs) or _matches_any(rel_path, exclude_globs):
                continue

            files_scanned += 1
            try:
                text = read_safe_text(repo.root, rel_path, repo.tracked_files, exclude_globs, max_file_bytes)
            except ValueError:
                continue

            for line_index, line in enumerate(text.splitlines(), start=1):
                if query not in line:
                    continue
                snippet = extract_snippet(
                    repo.name,
                    repo.root,
                    rel_path,
                    line_index,
                    2,
                    repo.tracked_files,
                    exclude_globs,
                    max_file_bytes,
                )
                snippet.reason = f"matched query on line {line_index}"
                snippet.score = line.count(query)
                results.append(snippet)
                if len(results) >= max_results:
                    return results
    return results


class RepoContextSearcher:
    def __init__(self, workspace_session=None, workspace=None, settings=None):
        self.workspace_session = workspace_session or workspace
        self.settings = settings or {}
        self.include_globs = self.settings.get("included_globs") or self.settings.get("include_globs") or []
        self.exclude_globs = self.settings.get("excluded_globs") or self.settings.get("exclude_globs") or []
        self.max_file_bytes = self.settings.get("max_file_bytes", 200000)
        self.timeout_sec = self.settings.get("search_timeout_sec", 10)
        self.max_files_scanned = self.settings.get("max_files_scanned", 12000)

    def find_symbols(self, symbols=None, limit=20, **kwargs):
        path = symbols if isinstance(symbols, str) else kwargs.get("path")
        if not path:
            return []
        start_line = kwargs.get("start_line") or 1
        end_line = kwargs.get("end_line") or start_line
        repo = self._primary_repo()
        if not repo:
            return []
        try:
            text = read_safe_text(repo.root, path, repo.tracked_files, self.exclude_globs, self.max_file_bytes)
        except ValueError:
            return []
        lines = text.splitlines()
        selected = lines[max(1, int(start_line)) - 1:int(end_line or start_line)]
        names = []
        for offset, line in enumerate(selected, start=max(1, int(start_line))):
            found = self._symbols_from_line(line)
            for name in found:
                names.append({"name": name, "path": path, "start": offset, "end": offset})
                if len(names) >= limit:
                    return names
        return names

    def find_references(self, symbol, limit=5):
        name = self._symbol_name(symbol)
        if not name:
            return []
        snippets = self.search_text(name, limit=max(limit * 4, limit))
        symbol_path = _symbol_path(symbol)
        ranked = sorted(
            snippets,
            key=lambda snippet: (
                snippet.path == symbol_path,
                -self._reference_score(name, snippet.content),
                snippet.path,
                snippet.start_line,
            ),
        )
        non_test_ranked = [snippet for snippet in ranked if not self._looks_like_test_path(snippet.path)]
        return (non_test_ranked or ranked)[:limit]

    def find_importers(self, symbol, limit=5):
        path = symbol.get("path") if isinstance(symbol, dict) else str(symbol or "")
        if not path:
            return []
        module_name = Path(path).stem
        queries = [f"import {module_name}", f"from {path.removesuffix('.py').replace('/', '.')} import"]
        snippets = []
        seen = set()
        for query in queries:
            for snippet in self.search_text(query, limit=limit):
                key = (snippet.repo_name, snippet.path, snippet.start_line, snippet.end_line)
                if key in seen or snippet.path == path:
                    continue
                seen.add(key)
                snippet.reason = f"imports {path}"
                snippets.append(snippet)
                if len(snippets) >= limit:
                    return snippets
        return snippets

    def find_tests(self, symbol, limit=5):
        names = [self._symbol_name(symbol)]
        if isinstance(symbol, str) and "/" in symbol:
            names.extend(item["name"] for item in self.find_symbols(symbol, limit=limit))
        names.append(Path(str(symbol or "")).stem)
        names = [name for name in names if name]
        if not names:
            return []
        snippets = []
        seen = set()
        for name in names:
            for snippet in self.search_text(name, limit=limit * 2):
                key = (snippet.repo_name, snippet.path, snippet.start_line, snippet.end_line)
                if key in seen or not self._looks_like_test_path(snippet.path):
                    continue
                seen.add(key)
                snippet.reason = f"test reference for {name}"
                snippets.append(snippet)
                if len(snippets) >= limit:
                    return snippets
        return snippets

    def search_text(self, query, limit=5):
        return search_text(
            self._workspace_repos(),
            query,
            max_results=limit,
            include_globs=self.include_globs,
            exclude_globs=self.exclude_globs,
            timeout_sec=self.timeout_sec,
            max_files_scanned=self.max_files_scanned,
            max_file_bytes=self.max_file_bytes,
        )

    def open_file(self, path, repo_name=None):
        if hasattr(self.workspace_session, "open_file"):
            return self.workspace_session.open_file(path, repo_name=repo_name)
        repo = self._repo_by_name(repo_name)
        if not repo:
            raise ValueError(f"Repository is unavailable: {repo_name or 'primary'}")
        return read_safe_text(repo.root, path, repo.tracked_files, self.exclude_globs, self.max_file_bytes)

    def _workspace_repos(self):
        repos = []
        session_repos = getattr(self.workspace_session, "repos", None)
        if session_repos is None:
            primary = getattr(self.workspace_session, "primary", None)
            session_repos = [primary, *getattr(self.workspace_session, "external_repos", [])]
        for repo in session_repos or []:
            root = getattr(repo, "root", None)
            if not root or getattr(repo, "status", "ready") != "ready":
                continue
            tracked_files = set(getattr(repo, "tracked_files", None) or getattr(repo, "files", []) or [])
            if not tracked_files:
                continue
            repos.append(WorkspaceRepo(getattr(repo, "name", "primary"), Path(root), tracked_files))
        return repos

    def _primary_repo(self):
        repos = self._workspace_repos()
        return repos[0] if repos else None

    def _repo_by_name(self, repo_name):
        repos = self._workspace_repos()
        if repo_name is None:
            return repos[0] if repos else None
        for repo in repos:
            if repo.name == repo_name:
                return repo
        return None

    @staticmethod
    def _symbol_name(symbol) -> str:
        if isinstance(symbol, dict):
            return symbol.get("name") or symbol.get("symbol") or ""
        return getattr(symbol, "name", str(symbol or ""))

    @staticmethod
    def _symbols_from_line(line: str) -> list[str]:
        definitions = re.findall(r"\b(?:def|class|function)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        assignments = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        calls = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        keywords = {"if", "for", "while", "return", "assert"}
        return [name for name in [*definitions, *assignments, *calls] if name not in keywords]

    @staticmethod
    def _looks_like_test_path(path: str) -> bool:
        parts = Path(path).parts
        name = Path(path).name
        return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")

    @staticmethod
    def _reference_score(name: str, content: str) -> int:
        if re.search(rf"(?:\.|\b){re.escape(name)}\s*\(", content):
            return 3
        if re.search(rf"\b{re.escape(name)}\b", content):
            return 2
        return 1


def _symbol_path(symbol) -> str:
    if isinstance(symbol, dict):
        return symbol.get("path") or ""
    return getattr(symbol, "path", "")


RepositorySearcher = RepoContextSearcher
