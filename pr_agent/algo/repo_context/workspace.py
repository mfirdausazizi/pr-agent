import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

from pr_agent.algo.repo_context.safe_paths import read_safe_text
from pr_agent.log import get_logger


@dataclass
class RepoWorkspace:
    name: str
    root: str | None
    files: list[str] = field(default_factory=list)
    status: str = "ready"
    message: str = ""
    repo_url: str | None = None
    ref: str | None = None


@dataclass
class RepoWorkspaceSession:
    primary: RepoWorkspace
    external_repos: list[RepoWorkspace] = field(default_factory=list)
    diff_only: bool = False
    exclude_globs: list[str] | tuple[str, ...] = field(default_factory=list)
    max_file_bytes: int = 200000

    @property
    def repo_label(self) -> str:
        return self.primary.name

    @property
    def repos(self) -> list[RepoWorkspace]:
        return [self.primary, *self.external_repos]

    def open_file(self, path, start=None, end=None, repo_name=None):
        repo = self._repo_by_name(repo_name)
        if not repo or not repo.root or repo.status != "ready":
            raise ValueError(f"Repository is unavailable: {repo_name or self.primary.name}")
        text = read_safe_text(repo.root, path, set(repo.files), self.exclude_globs, self.max_file_bytes)
        lines = text.splitlines()
        start_line = max(1, int(start or 1))
        end_line = min(len(lines), int(end or len(lines)))
        if end_line < start_line:
            return ""
        return "\n".join(lines[start_line - 1:end_line])

    def _repo_by_name(self, repo_name=None):
        if repo_name is None:
            return self.primary
        for repo in self.repos:
            if repo.name == repo_name:
                return repo
        return None


class RepoWorkspaceManager:
    def __init__(
            self,
            *,
            external_repos: list[dict] | None = None,
            allowed_external_repo_urls: list[str] | None = None,
            include_external_repos: list[str] | None = None,
            exclude_external_repos: list[str] | None = None,
            max_external_repos: int = 5,
            checkout_timeout_sec: int = 30,
            fallback_to_diff_only: bool = True,
            exclude_globs: list[str] | tuple[str, ...] | None = None,
            max_file_bytes: int | None = None,
            settings=None,
    ):
        if exclude_globs is None and settings:
            exclude_globs = settings.get("excluded_globs") or settings.get("exclude_globs") or []
        if max_file_bytes is None and settings:
            max_file_bytes = settings.get("max_file_bytes", 200000)
        self.external_repos = external_repos or []
        self.allowed_external_repo_urls = set(allowed_external_repo_urls or [])
        self.include_external_repos = set(include_external_repos or [])
        self.exclude_external_repos = set(exclude_external_repos or [])
        self.max_external_repos = max_external_repos
        self.checkout_timeout_sec = checkout_timeout_sec
        self.fallback_to_diff_only = fallback_to_diff_only
        self.exclude_globs = exclude_globs or []
        self.max_file_bytes = max_file_bytes or 200000

    @contextmanager
    def create_session(self, git_provider):
        cleanup_paths = []
        try:
            primary, primary_cleanup, diff_only = self._create_primary_workspace(git_provider)
            if primary_cleanup:
                cleanup_paths.append(primary_cleanup)
            external_workspaces, external_cleanup_paths = self._create_external_workspaces(git_provider)
            cleanup_paths.extend(external_cleanup_paths)
            yield RepoWorkspaceSession(
                primary=primary,
                external_repos=external_workspaces,
                diff_only=diff_only,
                exclude_globs=self.exclude_globs,
                max_file_bytes=self.max_file_bytes,
            )
        finally:
            for cleanup_path in cleanup_paths:
                shutil.rmtree(cleanup_path, ignore_errors=True)

    def _create_primary_workspace(self, git_provider) -> tuple[RepoWorkspace, str | None, bool]:
        local_root = git_provider.get_repo_context_local_root()
        if local_root:
            return RepoWorkspace(name="primary", root=local_root, files=self._tracked_files(local_root)), None, False

        checkout_spec = git_provider.get_repo_context_primary_checkout_spec()
        if not checkout_spec:
            return RepoWorkspace(name="primary", root=None, status="unavailable"), None, True

        temp_root = tempfile.mkdtemp(prefix="pr-agent-repo-context-")
        try:
            self._run_git(["git", "init"], cwd=temp_root)
            self._fetch_checkout(git_provider, temp_root, checkout_spec)
            return RepoWorkspace(name="primary", root=temp_root, files=self._tracked_files(temp_root), status="ready",
                                 repo_url=checkout_spec.get("repo_url")), temp_root, False
        except Exception as e:
            sanitized_error = self._sanitize(git_provider, str(e))
            get_logger().warning("Primary repo context checkout failed", artifact={"error": sanitized_error})
            if not self.fallback_to_diff_only:
                shutil.rmtree(temp_root, ignore_errors=True)
                raise RuntimeError(sanitized_error) from None
            return RepoWorkspace(name="primary", root=temp_root, status="checkout_failed",
                                 message=sanitized_error, repo_url=checkout_spec.get("repo_url")), temp_root, True

    def _create_external_workspaces(self, git_provider) -> tuple[list[RepoWorkspace], list[str]]:
        workspaces = []
        cleanup_paths = []
        selected_repos = self._selected_external_repos()
        for repo_config in selected_repos:
            url = repo_config.get("url")
            ref = repo_config.get("ref") or "HEAD"
            name = repo_config.get("name") or self._repo_name(url)
            if not self._is_supported_github_https_url(url):
                workspaces.append(RepoWorkspace(name=name, root=None, status="unsupported_url", repo_url=url, ref=ref))
                continue
            if url not in self.allowed_external_repo_urls:
                workspaces.append(RepoWorkspace(name=name, root=None, status="not_allowlisted", repo_url=url, ref=ref))
                continue

            temp_root = tempfile.mkdtemp(prefix="pr-agent-external-context-")
            cleanup_paths.append(temp_root)
            try:
                self._run_git(["git", "init"], cwd=temp_root)
                self._fetch_ref(git_provider, temp_root, url, ref)
                self._run_git(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=temp_root)
                workspaces.append(RepoWorkspace(name=name, root=temp_root, files=self._tracked_files(temp_root),
                                                status="ready", repo_url=url, ref=ref))
            except Exception as e:
                sanitized_error = self._sanitize(git_provider, str(e))
                get_logger().warning("External repo context checkout failed", artifact={
                    "repo_url": self._sanitize(git_provider, url),
                    "error": sanitized_error,
                })
                workspaces.append(RepoWorkspace(name=name, root=temp_root, status="fetch_failed",
                                                message=sanitized_error, repo_url=url, ref=ref))
        return workspaces, cleanup_paths

    def _selected_external_repos(self) -> list[dict]:
        selected = []
        for repo_config in self.external_repos:
            url = repo_config.get("url")
            if self.include_external_repos and url not in self.include_external_repos:
                continue
            if url in self.exclude_external_repos:
                continue
            selected.append(repo_config)
            if len(selected) >= self.max_external_repos:
                break
        return selected

    def _tracked_files(self, local_root: str) -> list[str]:
        result = subprocess.run(
            ["git", "-C", local_root, "ls-files"],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )
        return [line for line in result.stdout.splitlines() if line]

    def _fetch_checkout(self, git_provider, cwd: str, checkout_spec: dict):
        pr_num = checkout_spec.get("pr_num")
        head_sha = checkout_spec.get("head_sha")
        repo_url = checkout_spec.get("repo_url")
        try:
            self._fetch_ref(git_provider, cwd, repo_url, f"refs/pull/{pr_num}/head")
        except Exception:
            if not head_sha:
                raise
            self._fetch_ref(git_provider, cwd, repo_url, head_sha)
        self._run_git(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=cwd)

    def _fetch_ref(self, git_provider, cwd: str, repo_url: str, ref: str):
        command = ["git"]
        auth_header = git_provider.get_repo_context_auth_header()
        if auth_header:
            command.extend(["-c", f"http.extraHeader={auth_header}"])
        command.extend(["fetch", "--depth", "1", repo_url, ref])
        try:
            self._run_git(command, cwd=cwd)
        except subprocess.CalledProcessError as exc:
            message = exc.stderr or exc.stdout or "git fetch failed"
            raise RuntimeError(self._sanitize(git_provider, message)) from None

    def _run_git(self, command: list[str], *, cwd: str):
        previous_cwd = os.getcwd()
        try:
            os.chdir(cwd)
            return subprocess.run(command, check=True, capture_output=True, text=True, shell=False,
                                  timeout=self.checkout_timeout_sec)
        finally:
            os.chdir(previous_cwd)

    def _is_supported_github_https_url(self, url: str | None) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc.lower() == "github.com" and parsed.path.endswith(".git")

    def _repo_name(self, url: str | None) -> str:
        if not url:
            return "external"
        return urlparse(url).path.rstrip("/").split("/")[-1].removesuffix(".git") or "external"

    def _sanitize(self, git_provider, value: str) -> str:
        sanitized = git_provider.sanitize_repo_context_url(value)
        sanitized = re.sub(r"'?http\.extraHeader=Authorization:\s*Bearer\s+\*\*\*'?,?\s*", "", sanitized)
        return sanitized
