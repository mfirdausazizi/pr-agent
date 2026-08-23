import subprocess

import pytest

from pr_agent.algo.repo_context.safe_paths import (
    build_tracked_file_set,
    is_allowed_relative_path,
    read_safe_text,
    resolve_safe_path,
)


def test_is_allowed_relative_path_rejects_unsafe_paths():
    assert is_allowed_relative_path("src/app.py")
    assert not is_allowed_relative_path("/src/app.py")
    assert not is_allowed_relative_path("../app.py")
    assert not is_allowed_relative_path("src/../app.py")
    assert not is_allowed_relative_path("src/app.py\0")


def test_build_tracked_file_set_lists_regular_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n")

    assert build_tracked_file_set(tmp_path) == {"src/app.py"}


def test_build_tracked_file_set_uses_git_ls_files_when_available(tmp_path):
    (tmp_path / "tracked.py").write_text("tracked")
    (tmp_path / "untracked.py").write_text("untracked")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True, capture_output=True, text=True)

    assert build_tracked_file_set(tmp_path) == {"tracked.py"}


def test_resolve_safe_path_rejects_absolute_traversal_null_untracked_and_excluded(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n")
    allowed = {"src/app.py"}

    for rel_path in ["/tmp/app.py", "../app.py", "src/app.py\0", "src/missing.py"]:
        with pytest.raises(ValueError):
            resolve_safe_path(tmp_path, rel_path, allowed, [], 200000)

    with pytest.raises(ValueError):
        resolve_safe_path(tmp_path, "src/app.py", allowed, ["src/*.py"], 200000)


def test_resolve_safe_path_rejects_secret_files(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n")
    (tmp_path / ".secrets").write_text("TOKEN=secret\n")

    secret_paths = [
        ".env",
        ".secrets",
        "id_rsa",
        "id_ed25519",
        "cert.pem",
        "private.key",
        "client.p12",
        "credentials.json",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".aws/credentials",
        "service-account-prod.json",
    ]
    for rel_path in secret_paths:
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("TOKEN=secret\n")

    for rel_path in secret_paths:
        with pytest.raises(ValueError):
            resolve_safe_path(tmp_path, rel_path, {rel_path}, [], 200000)


def test_resolve_safe_path_rejects_oversize_binary_and_symlink_escape(tmp_path):
    (tmp_path / "large.txt").write_text("abcdef")
    (tmp_path / "binary.bin").write_bytes(b"abc\0def")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    (tmp_path / "escape.txt").symlink_to(outside)

    with pytest.raises(ValueError):
        resolve_safe_path(tmp_path, "large.txt", {"large.txt"}, [], 3)
    with pytest.raises(ValueError):
        read_safe_text(tmp_path, "binary.bin", {"binary.bin"}, [], 200000)
    with pytest.raises(ValueError):
        resolve_safe_path(tmp_path, "escape.txt", {"escape.txt"}, [], 200000)


def test_read_safe_text_returns_text_for_allowed_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("hello\n")

    assert read_safe_text(tmp_path, "src/app.py", {"src/app.py"}, [], 200000) == "hello\n"
