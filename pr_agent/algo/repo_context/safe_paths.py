import fnmatch
import os
import subprocess
from pathlib import Path, PurePosixPath


_SECRET_FILENAMES = {
    ".env",
    ".secrets",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
}
_SECRET_GLOBS = ("*.pem", "*.key", "*.p12", ".env.*", ".aws/credentials", "service-account*.json")


def _normalize_relative_path(path: str | Path) -> str:
    return str(PurePosixPath(str(path).replace("\\", "/")))


def is_allowed_relative_path(path: str | Path) -> bool:
    path_str = str(path)
    if "\0" in path_str:
        return False
    pure_path = PurePosixPath(path_str.replace("\\", "/"))
    if pure_path.is_absolute():
        return False
    if any(part in ("", ".", "..") for part in pure_path.parts):
        return False
    return bool(pure_path.parts)


def build_tracked_file_set(root: str | Path) -> set[str]:
    root_path = Path(root)
    try:
        result = subprocess.run(
            ["git", "-C", str(root_path), "ls-files"],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )
        return {
            rel_path
            for rel_path in result.stdout.splitlines()
            if is_allowed_relative_path(rel_path) and not _is_secret_path(rel_path)
        }
    except (OSError, subprocess.CalledProcessError):
        pass

    tracked_files: set[str] = set()
    for current_root, dirnames, filenames in os.walk(root_path):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for filename in filenames:
            file_path = Path(current_root) / filename
            rel_path = file_path.relative_to(root_path).as_posix()
            if is_allowed_relative_path(rel_path) and not _is_secret_path(rel_path):
                tracked_files.add(rel_path)
    return tracked_files


def _matches_any_glob(path: str, globs: list[str] | tuple[str, ...] | None) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in globs or [])


def _is_secret_path(path: str) -> bool:
    normalized_path = _normalize_relative_path(path)
    pure_path = PurePosixPath(normalized_path)
    return pure_path.name in _SECRET_FILENAMES or _matches_any_glob(normalized_path, _SECRET_GLOBS)


def _reject_binary(path: Path) -> None:
    try:
        sample = path.read_bytes()[:8192]
    except OSError as exc:
        raise ValueError(f"Cannot read file: {path}") from exc
    if b"\0" in sample:
        raise ValueError(f"Binary file is not allowed: {path}")
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Binary file is not allowed: {path}") from exc


def resolve_safe_path(
    root: str | Path,
    rel_path: str | Path,
    allowed_files: set[str],
    exclude_globs: list[str] | tuple[str, ...] | None,
    max_file_bytes: int,
) -> Path:
    normalized_path = _normalize_relative_path(rel_path)
    if not is_allowed_relative_path(normalized_path):
        raise ValueError(f"Unsafe relative path: {rel_path}")
    if _is_secret_path(normalized_path):
        raise ValueError(f"Secret file is not allowed: {normalized_path}")
    if normalized_path not in allowed_files:
        raise ValueError(f"File is not tracked: {normalized_path}")
    if _matches_any_glob(normalized_path, exclude_globs):
        raise ValueError(f"File is excluded: {normalized_path}")

    root_path = Path(root).resolve()
    candidate_path = root_path / normalized_path
    try:
        real_path = candidate_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"File does not exist: {normalized_path}") from exc

    if os.path.commonpath([root_path, real_path]) != str(root_path):
        raise ValueError(f"Path escapes repository root: {normalized_path}")
    if not real_path.is_file():
        raise ValueError(f"Path is not a file: {normalized_path}")
    if real_path.stat().st_size > max_file_bytes:
        raise ValueError(f"File is too large: {normalized_path}")
    _reject_binary(real_path)
    return real_path


def read_safe_text(
    root: str | Path,
    rel_path: str | Path,
    allowed_files: set[str],
    exclude_globs: list[str] | tuple[str, ...] | None,
    max_file_bytes: int,
) -> str:
    safe_path = resolve_safe_path(root, rel_path, allowed_files, exclude_globs, max_file_bytes)
    return safe_path.read_text(encoding="utf-8")
