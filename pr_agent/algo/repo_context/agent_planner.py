import asyncio
import inspect
import json
import re
from pathlib import PurePosixPath
from typing import Any


ALLOWED_ACTION_TYPES = {"find_references", "search_text", "open_file", "find_importers", "find_tests"}
_SHELL_PATTERN = re.compile(
    r"\b(?:bash|curl|eval|exec|git\s+clone|npm|pip|python|rm|sh|subprocess|sudo|wget)\b|[;&|`$]",
    re.IGNORECASE,
)
_CLONE_URL_PATTERN = re.compile(r"\b(?:https?|ssh|git)://\S+|git@\S+:\S+", re.IGNORECASE)


def parse_planner_actions(raw: str, max_actions: int) -> list[dict[str, Any]]:
    data = json.loads(raw)
    actions = data.get("actions") if isinstance(data, dict) else data
    if not isinstance(actions, list):
        raise ValueError("Planner response must be a JSON list or an object with actions")

    parsed = []
    for action in actions[:max_actions]:
        if not isinstance(action, dict):
            raise ValueError("Planner action must be an object")
        action_type = action.get("type")
        if action_type not in ALLOWED_ACTION_TYPES:
            raise ValueError(f"Unsupported planner action: {action_type}")

        normalized = {"type": action_type}
        for key, value in action.items():
            if key == "type":
                continue
            _validate_action_value(key, value)
            normalized[key] = value
        parsed.append(normalized)
    return parsed


def build_planner_prompt(goal: str, changed_files: list[str], max_actions: int) -> str:
    allowed = ", ".join(sorted(ALLOWED_ACTION_TYPES))
    files = ", ".join(changed_files[:20])
    return (
        "Plan repository context lookups for a code review.\n"
        f"Goal: {goal}\n"
        f"Changed files: {files}\n"
        f"Allowed actions: {allowed}.\n"
        "Use only relative repo paths. Do not request shell commands, installs, clones, or arbitrary URLs.\n"
        f"Return JSON only as {{\"actions\": [...]}} with at most {max_actions} actions."
    )


async def plan_repo_context_actions(ai_handler, model: str, prompt: str, timeout_sec: float) -> list[dict[str, Any]]:
    async def _call_handler():
        if hasattr(ai_handler, "chat_completion"):
            result = ai_handler.chat_completion(model=model, temperature=0, system="", user=prompt)
        elif callable(ai_handler):
            result = ai_handler(model=model, prompt=prompt)
        else:
            raise ValueError("Unsupported AI handler")
        if inspect.isawaitable(result):
            result = await result
        return result

    raw = await asyncio.wait_for(_call_handler(), timeout=timeout_sec)
    if isinstance(raw, tuple):
        raw = raw[0]
    if not isinstance(raw, str):
        raw = str(raw)
    return parse_planner_actions(raw, max_actions=20)


def _validate_action_value(key: str, value: Any) -> None:
    if isinstance(value, str):
        if _SHELL_PATTERN.search(value) or _CLONE_URL_PATTERN.search(value):
            raise ValueError(f"Unsafe planner value for {key}")
        if key in {"path", "file", "repo_path"}:
            _validate_relative_path(value)
    elif isinstance(value, list):
        for item in value:
            _validate_action_value(key, item)
    elif isinstance(value, dict):
        for child_key, child_value in value.items():
            _validate_action_value(child_key, child_value)
    elif value is not None and not isinstance(value, (int, float, bool)):
        raise ValueError(f"Unsupported planner value for {key}")


def _validate_relative_path(path: str) -> None:
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or any(part == ".." for part in pure_path.parts):
        raise ValueError(f"Unsafe repo path: {path}")
