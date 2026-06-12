import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Tuple, TypeVar

T = TypeVar("T")

from pr_agent.algo.utils import get_max_tokens
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


@dataclass
class EnsembleConfig:
    """Resolved multi-model ensemble configuration for a single tool run."""
    models: List[str]
    consolidator: str


def _parse_models_value(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        models = [m.strip() for m in value.split(",")]
    else:
        models = [str(m).strip() for m in value]
    deduped = []
    for model in models:
        if model and model not in deduped:
            deduped.append(model)
    return deduped


def resolve_ensemble_config(tool_section: str) -> Optional[EnsembleConfig]:
    """
    Resolve the ensemble configuration for a tool ("pr_reviewer" / "pr_code_suggestions").
    Tool-section keys override [config] keys. Returns None when the feature is off.
    """
    settings = get_settings()
    models = (_parse_models_value(settings.get(f"{tool_section}.ensemble_models", None)) or
              _parse_models_value(settings.get("config.ensemble_models", None)))
    if not models:
        return None
    consolidator = (settings.get(f"{tool_section}.ensemble_consolidator_model", None) or
                    settings.get("config.ensemble_consolidator_model", None) or
                    models[0])
    return EnsembleConfig(models=models, consolidator=str(consolidator).strip())


async def gather_ensemble_predictions(fn: Callable[[str], Awaitable[T]],
                                      models: List[str]) -> List[Tuple[str, T]]:
    """
    Run fn(model) for every model concurrently. Failed or empty predictions are
    dropped (with a warning); returns [(model, result), ...] for successes only.
    """
    results = await asyncio.gather(*[fn(model) for model in models], return_exceptions=True)
    successes = []
    for model, result in zip(models, results):
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            # cancellation / interpreter exit must propagate, never be swallowed
            raise result
        if isinstance(result, Exception):
            get_logger().warning(f"Ensemble model {model} failed", artifact={"error": result})
        elif result:
            successes.append((model, result))
        else:
            get_logger().warning(f"Ensemble model {model} returned an empty prediction")
    return successes


def pick_min_budget_model(models: List[str]) -> str:
    """
    Return the model with the smallest known token budget, so diffs/chunks built
    with it fit every model. Models with an unknown budget are skipped; if no
    budget is known, return the first model (downstream code raises a clear error).
    """
    best_model, best_budget = None, None
    for model in models:
        try:
            budget = get_max_tokens(model)
        except Exception:
            get_logger().warning(f"Unknown token budget for ensemble model {model}")
            continue
        if best_budget is None or budget < best_budget:
            best_model, best_budget = model, budget
    return best_model if best_model else models[0]


def ensemble_footer(models: List[str], consolidator: str, consolidated: bool) -> str:
    models_str = " + ".join(f"`{m}`" for m in models)
    if consolidated:
        return f"\n\n> 🧬 **Ensemble**: {models_str} · consolidated by `{consolidator}`\n"
    return f"\n\n> 🧬 **Ensemble**: {models_str} · consolidation skipped\n"
