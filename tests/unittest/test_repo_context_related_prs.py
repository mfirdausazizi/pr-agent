from pr_agent.algo.repo_context.related_prs import build_related_pr_external_repos, extract_github_pr_links


def test_extracts_github_pull_urls_and_shorthand_links():
    description = """
    ## Related PRs
    - https://github.com/fatomate/wabot-v4/pull/197
    - Backend: fatomate/wabot-backend-v3#91
    - duplicate with punctuation: https://github.com/fatomate/wabot-v4/pull/197).
    """

    links = extract_github_pr_links(description)

    assert [(link.owner, link.repo, link.number) for link in links] == [
        ("fatomate", "wabot-v4", 197),
        ("fatomate", "wabot-backend-v3", 91),
    ]


def test_related_pr_configs_are_allowlisted_and_use_pull_refs():
    description = """
    Related:
    - fatomate/wabot-backend-v3#91
    - https://github.com/fatomate/wabot-v4/pull/197?foo=bar#discussion
    - https://github.com/evil/private/pull/1
    """

    repos = build_related_pr_external_repos(
        description,
        allowed_external_repo_urls=[
            "https://github.com/fatomate/wabot-backend-v3.git",
            "https://github.com/fatomate/wabot-v4.git",
        ],
    )

    assert repos == [
        {
            "name": "fatomate-wabot-backend-v3-pr-91",
            "url": "https://github.com/fatomate/wabot-backend-v3.git",
            "ref": "refs/pull/91/head",
        },
        {
            "name": "fatomate-wabot-v4-pr-197",
            "url": "https://github.com/fatomate/wabot-v4.git",
            "ref": "refs/pull/197/head",
        },
    ]


def test_related_pr_configs_skip_self_link_and_respect_max_related_prs():
    description = """
    - fatomate/wabot_rag#34
    - fatomate/wabot-backend-v3#91
    - fatomate/wabot-v4#197
    """

    repos = build_related_pr_external_repos(
        description,
        allowed_external_repo_urls=[
            "https://github.com/fatomate/wabot_rag.git",
            "https://github.com/fatomate/wabot-backend-v3.git",
            "https://github.com/fatomate/wabot-v4.git",
        ],
        current_repo="fatomate/wabot_rag",
        current_pr_num=34,
        max_related_prs=1,
    )

    assert repos == [
        {
            "name": "fatomate-wabot-backend-v3-pr-91",
            "url": "https://github.com/fatomate/wabot-backend-v3.git",
            "ref": "refs/pull/91/head",
        }
    ]


def test_related_pr_configs_require_explicit_allowlist():
    description = "Related backend PR: fatomate/wabot-backend-v3#91"

    assert build_related_pr_external_repos(description, allowed_external_repo_urls=[]) == []
