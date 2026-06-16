import re
from dataclasses import dataclass
from urllib.parse import urlparse


_GITHUB_PULL_URL_RE = re.compile(
    r"(?:https?://)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)"
)
_GITHUB_SHORTHAND_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>\d+)"
)


@dataclass(frozen=True)
class RelatedPullRequest:
    owner: str
    repo: str
    number: int
    clone_url: str
    ref: str
    name: str


def extract_github_pr_links(description: str) -> list[RelatedPullRequest]:
    if not description:
        return []

    links = []
    seen = set()
    matches = list(_GITHUB_PULL_URL_RE.finditer(description))
    matches.extend(_GITHUB_SHORTHAND_RE.finditer(description))
    for match in sorted(matches, key=lambda item: item.start()):
        owner = match.group("owner")
        repo = match.group("repo")
        number = int(match.group("number"))
        key = (owner.lower(), repo.lower(), number)
        if key in seen:
            continue
        seen.add(key)
        links.append(_make_related_pr(owner, repo, number))
    return links


def build_related_pr_external_repos(
    description: str,
    allowed_external_repo_urls: list[str] | tuple[str, ...] | None,
    current_repo: str | None = None,
    current_pr_num: int | None = None,
    max_related_prs: int = 3,
) -> list[dict]:
    if not allowed_external_repo_urls or max_related_prs <= 0:
        return []

    allowed_by_canonical = {_canonical_github_clone_url(url): url for url in allowed_external_repo_urls}
    current_repo = (current_repo or "").lower()
    repos = []
    for link in extract_github_pr_links(description):
        if current_repo == f"{link.owner}/{link.repo}".lower() and current_pr_num == link.number:
            continue
        allowed_url = allowed_by_canonical.get(_canonical_github_clone_url(link.clone_url))
        if not allowed_url:
            continue
        repos.append({"name": link.name, "url": allowed_url, "ref": link.ref})
        if len(repos) >= max_related_prs:
            break
    return repos


def _make_related_pr(owner: str, repo: str, number: int) -> RelatedPullRequest:
    return RelatedPullRequest(
        owner=owner,
        repo=repo,
        number=number,
        clone_url=f"https://github.com/{owner}/{repo}.git",
        ref=f"refs/pull/{number}/head",
        name=f"{owner}-{repo}-pr-{number}",
    )


def _canonical_github_clone_url(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.netloc.lower() != "github.com":
        return ""
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return ""
    return f"https://github.com/{parts[0].lower()}/{parts[1].lower()}.git"
