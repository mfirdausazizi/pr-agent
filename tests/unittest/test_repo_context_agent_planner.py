import pytest

from pr_agent.algo.repo_context.agent_planner import build_planner_prompt, parse_planner_actions


def test_parse_planner_actions_accepts_json_object_with_allowed_actions():
    raw = """
    {
      "actions": [
        {"type": "search_text", "query": "db_delete"},
        {"type": "open_file", "path": "pr_agent/tools/pr_reviewer.py", "start": 10, "end": 20}
      ]
    }
    """

    actions = parse_planner_actions(raw, max_actions=5)

    assert actions == [
        {"type": "search_text", "query": "db_delete"},
        {"type": "open_file", "path": "pr_agent/tools/pr_reviewer.py", "start": 10, "end": 20},
    ]


def test_parse_planner_actions_accepts_top_level_list_and_limits_actions():
    raw = '[{"type": "find_tests", "path": "pr_agent/algo/utils.py"}, {"type": "find_importers", "path": "x.py"}]'

    assert parse_planner_actions(raw, max_actions=1) == [{"type": "find_tests", "path": "pr_agent/algo/utils.py"}]


def test_parse_planner_actions_accepts_code_like_search_query():
    raw = '{"actions": [{"type": "search_text", "query": "db_delete(user_id)"}]}'

    assert parse_planner_actions(raw, max_actions=1) == [{"type": "search_text", "query": "db_delete(user_id)"}]


@pytest.mark.parametrize(
    "raw",
    [
        '{"actions": [{"type": "delete_file", "path": "a.py"}]}',
        '{"actions": [{"type": "open_file", "path": "/etc/passwd"}]}',
        '{"actions": [{"type": "open_file", "path": "../secret.py"}]}',
        '{"actions": [{"type": "search_text", "query": "https://github.com/org/repo.git"}]}',
        '{"actions": [{"type": "search_text", "query": "pip install evil"}]}',
        '{"actions": [{"type": "search_text", "query": "subprocess.run(ls)"}]}',
    ],
)
def test_parse_planner_actions_rejects_unsafe_actions(raw):
    with pytest.raises(ValueError):
        parse_planner_actions(raw, max_actions=5)


def test_build_planner_prompt_describes_allowed_actions_compactly():
    prompt = build_planner_prompt("Find risky deletes", ["pr_agent/algo/utils.py"], max_actions=3)

    assert "find_references" in prompt
    assert "search_text" in prompt
    assert "open_file" in prompt
    assert "Return JSON only" in prompt
    assert "pr_agent/algo/utils.py" in prompt
