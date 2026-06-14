from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChangedRange:
    path: str
    start_line: int
    end_line: int


@dataclass
class ChangedSymbol:
    name: str
    path: str
    line_start: int
    line_end: int
    kind: str
    language: str


@dataclass
class RepoContextSnippet:
    repo_name: str
    path: str
    start_line: int
    end_line: int
    content: str
    symbol: str = ""
    reason: str = ""
    source: str = "deterministic"
    score: float = 0

    @property
    def repo(self) -> str:
        return self.repo_name

    @property
    def repo_label(self) -> str:
        return self.repo_name

    @property
    def start(self) -> int:
        return self.start_line

    @property
    def end(self) -> int:
        return self.end_line

    @property
    def context_type(self) -> str:
        return "verification" if self.reason.startswith("test") else "context"


@dataclass
class RepoContextBundle:
    snippets: list[RepoContextSnippet]
    status: str = "available"
    unavailable_reason: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class RepoContextActionResult:
    snippets: list[RepoContextSnippet] = field(default_factory=list)
    status: str = "available"
    message: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class WorkspaceRepo:
    name: str
    root: Path
    tracked_files: set[str]
    repo_type: str = "primary"
