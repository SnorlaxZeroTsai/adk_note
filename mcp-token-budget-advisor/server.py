"""
MCP Server: Token Budget Advisor
Uses LiteLLM's model_prices_and_context_window.json to provide
intelligent token budget recommendations for AI applications.
"""

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("token-budget-advisor")

LITELLM_DATA_PATH = os.environ.get(
    "LITELLM_MODEL_PRICES_PATH",
    str(Path(__file__).parent.parent / "litellm" / "model_prices_and_context_window.json"),
)

_model_data: dict[str, Any] | None = None


def load_model_data() -> dict[str, Any]:
    global _model_data
    if _model_data is None:
        with open(LITELLM_DATA_PATH) as f:
            _model_data = json.load(f)
    return _model_data


def get_chat_models() -> dict[str, dict]:
    data = load_model_data()
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, dict)
        and v.get("mode") == "chat"
        and isinstance(v.get("input_cost_per_token"), (int, float))
        and v.get("input_cost_per_token", 0) > 0
    }


def calculate_cost(model_info: dict, input_tokens: int, output_tokens: int) -> float:
    input_cost = model_info.get("input_cost_per_token", 0) * input_tokens
    output_cost = model_info.get("output_cost_per_token", 0) * output_tokens
    return input_cost + output_cost


@mcp.tool()
def get_model_info(model_name: str) -> str:
    """Get detailed pricing and context window info for a specific model."""
    data = load_model_data()
    if model_name not in data:
        suggestions = [k for k in data if model_name.lower() in k.lower()][:10]
        return json.dumps(
            {"error": f"Model '{model_name}' not found", "suggestions": suggestions},
            indent=2,
        )

    info = data[model_name]
    return json.dumps(
        {
            "model": model_name,
            "provider": info.get("litellm_provider", "unknown"),
            "max_input_tokens": info.get("max_input_tokens", info.get("max_tokens", "N/A")),
            "max_output_tokens": info.get("max_output_tokens", info.get("max_tokens", "N/A")),
            "input_cost_per_1M_tokens": round(info.get("input_cost_per_token", 0) * 1_000_000, 4),
            "output_cost_per_1M_tokens": round(info.get("output_cost_per_token", 0) * 1_000_000, 4),
            "supports_caching": info.get("supports_prompt_caching", False),
            "cache_read_cost_per_1M": round(info.get("cache_read_input_token_cost", 0) * 1_000_000, 4)
            if info.get("cache_read_input_token_cost")
            else None,
            "supports_vision": info.get("supports_vision", False),
            "supports_function_calling": info.get("supports_function_calling", False),
        },
        indent=2,
    )


@mcp.tool()
def estimate_cost(model_name: str, input_tokens: int, output_tokens: int) -> str:
    """Estimate the cost for a given number of input and output tokens on a model."""
    data = load_model_data()
    if model_name not in data:
        return json.dumps({"error": f"Model '{model_name}' not found"})

    info = data[model_name]
    cost = calculate_cost(info, input_tokens, output_tokens)

    return json.dumps(
        {
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_cost": round(info.get("input_cost_per_token", 0) * input_tokens, 6),
            "output_cost": round(info.get("output_cost_per_token", 0) * output_tokens, 6),
            "total_cost_usd": round(cost, 6),
            "cost_per_1000_calls": round(cost * 1000, 4),
        },
        indent=2,
    )


@mcp.tool()
def budget_advisor(
    monthly_budget_usd: float,
    avg_input_tokens_per_call: int,
    avg_output_tokens_per_call: int,
    expected_calls_per_day: int,
    use_case: str = "general",
) -> str:
    """
    Given a monthly budget, average token usage, and daily call volume,
    recommend models that fit the budget with cost breakdown.

    use_case options: general, coding, creative, analysis, vision, embedding
    """
    chat_models = get_chat_models()
    daily_budget = monthly_budget_usd / 30
    monthly_calls = expected_calls_per_day * 30

    recommendations = []

    for name, info in chat_models.items():
        max_input = info.get("max_input_tokens", info.get("max_tokens", 0))
        max_output = info.get("max_output_tokens", info.get("max_tokens", 0))

        if not isinstance(max_input, (int, float)) or not isinstance(max_output, (int, float)):
            continue
        if avg_input_tokens_per_call > max_input or avg_output_tokens_per_call > max_output:
            continue

        cost_per_call = calculate_cost(info, avg_input_tokens_per_call, avg_output_tokens_per_call)
        monthly_cost = cost_per_call * monthly_calls

        if monthly_cost <= monthly_budget_usd and monthly_cost > 0:
            utilization = (monthly_cost / monthly_budget_usd) * 100

            score = 0
            if use_case == "coding" and info.get("supports_function_calling"):
                score += 10
            if use_case == "vision" and info.get("supports_vision"):
                score += 10
            if use_case == "analysis" and max_input >= 100000:
                score += 10
            if info.get("supports_prompt_caching"):
                score += 5

            recommendations.append(
                {
                    "model": name,
                    "provider": info.get("litellm_provider", "unknown"),
                    "cost_per_call_usd": round(cost_per_call, 6),
                    "monthly_cost_usd": round(monthly_cost, 2),
                    "budget_utilization_pct": round(utilization, 1),
                    "max_input_tokens": max_input,
                    "max_output_tokens": max_output,
                    "supports_caching": info.get("supports_prompt_caching", False),
                    "score": score,
                }
            )

    recommendations.sort(key=lambda x: (-x["score"], x["monthly_cost_usd"]))
    top_picks = recommendations[:15]

    budget_analysis = {
        "budget": {
            "monthly_usd": monthly_budget_usd,
            "daily_usd": round(daily_budget, 2),
            "per_call_budget_usd": round(monthly_budget_usd / monthly_calls, 6),
        },
        "usage": {
            "avg_input_tokens": avg_input_tokens_per_call,
            "avg_output_tokens": avg_output_tokens_per_call,
            "daily_calls": expected_calls_per_day,
            "monthly_calls": monthly_calls,
        },
        "recommendations": top_picks,
        "total_models_evaluated": len(chat_models),
        "models_within_budget": len(recommendations),
    }

    return json.dumps(budget_analysis, indent=2)


@mcp.tool()
def compare_models(model_names: list[str], input_tokens: int = 1000, output_tokens: int = 500) -> str:
    """Compare multiple models side by side on pricing, context window, and features."""
    data = load_model_data()
    comparisons = []

    for name in model_names:
        if name not in data:
            comparisons.append({"model": name, "error": "not found"})
            continue

        info = data[name]
        cost = calculate_cost(info, input_tokens, output_tokens)

        comparisons.append(
            {
                "model": name,
                "provider": info.get("litellm_provider", "unknown"),
                "input_cost_per_1M": round(info.get("input_cost_per_token", 0) * 1_000_000, 4),
                "output_cost_per_1M": round(info.get("output_cost_per_token", 0) * 1_000_000, 4),
                "sample_cost_usd": round(cost, 6),
                "max_input_tokens": info.get("max_input_tokens", info.get("max_tokens", "N/A")),
                "max_output_tokens": info.get("max_output_tokens", info.get("max_tokens", "N/A")),
                "supports_caching": info.get("supports_prompt_caching", False),
                "supports_vision": info.get("supports_vision", False),
                "supports_function_calling": info.get("supports_function_calling", False),
                "supports_reasoning": info.get("supports_reasoning", False),
            }
        )

    return json.dumps(
        {
            "comparison": comparisons,
            "sample_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
        indent=2,
    )


@mcp.tool()
def token_budget_strategies(
    total_budget_usd: float,
    primary_model: str,
    secondary_model: str = "",
) -> str:
    """
    Provide token budget allocation strategies for a multi-tier model setup.
    Recommends how to split budget between expensive/capable and cheap/fast models.
    """
    data = load_model_data()

    if primary_model not in data:
        return json.dumps({"error": f"Primary model '{primary_model}' not found"})

    primary_info = data[primary_model]
    primary_input_cost = primary_info.get("input_cost_per_token", 0)
    primary_output_cost = primary_info.get("output_cost_per_token", 0)

    secondary_info = None
    if secondary_model and secondary_model in data:
        secondary_info = data[secondary_model]

    strategies = []

    # Strategy 1: All primary
    avg_cost_primary = primary_input_cost * 2000 + primary_output_cost * 1000
    calls_primary_only = int(total_budget_usd / avg_cost_primary) if avg_cost_primary > 0 else 0

    strategies.append(
        {
            "name": "All Primary",
            "description": f"Use {primary_model} for all calls",
            "estimated_monthly_calls": calls_primary_only,
            "cost_allocation": {"primary_pct": 100, "secondary_pct": 0},
            "best_for": "Maximum quality, lower volume",
        }
    )

    if secondary_info:
        sec_input_cost = secondary_info.get("input_cost_per_token", 0)
        sec_output_cost = secondary_info.get("output_cost_per_token", 0)
        avg_cost_secondary = sec_input_cost * 2000 + sec_output_cost * 1000

        # Strategy 2: 80/20 split
        budget_primary = total_budget_usd * 0.2
        budget_secondary = total_budget_usd * 0.8
        calls_p = int(budget_primary / avg_cost_primary) if avg_cost_primary > 0 else 0
        calls_s = int(budget_secondary / avg_cost_secondary) if avg_cost_secondary > 0 else 0

        strategies.append(
            {
                "name": "Router (80% cheap / 20% premium)",
                "description": f"Route simple tasks to {secondary_model}, complex to {primary_model}",
                "estimated_monthly_calls": calls_p + calls_s,
                "primary_calls": calls_p,
                "secondary_calls": calls_s,
                "cost_allocation": {"primary_pct": 20, "secondary_pct": 80},
                "best_for": "High volume with quality on demand",
            }
        )

        # Strategy 3: 50/50 balanced
        budget_p = total_budget_usd * 0.5
        budget_s = total_budget_usd * 0.5
        calls_p = int(budget_p / avg_cost_primary) if avg_cost_primary > 0 else 0
        calls_s = int(budget_s / avg_cost_secondary) if avg_cost_secondary > 0 else 0

        strategies.append(
            {
                "name": "Balanced (50/50)",
                "description": "Equal budget split between both models",
                "estimated_monthly_calls": calls_p + calls_s,
                "primary_calls": calls_p,
                "secondary_calls": calls_s,
                "cost_allocation": {"primary_pct": 50, "secondary_pct": 50},
                "best_for": "Balanced quality and volume",
            }
        )

        # Strategy 4: Caching optimization
        if primary_info.get("supports_prompt_caching"):
            cache_read_cost = primary_info.get("cache_read_input_token_cost", primary_input_cost * 0.1)
            cached_avg_cost = cache_read_cost * 2000 + primary_output_cost * 1000
            calls_cached = int(total_budget_usd / cached_avg_cost) if cached_avg_cost > 0 else 0

            strategies.append(
                {
                    "name": "Cache-Optimized Primary",
                    "description": f"Use {primary_model} with aggressive prompt caching (assumes 80% cache hit)",
                    "estimated_monthly_calls": calls_cached,
                    "savings_vs_uncached_pct": round(
                        (1 - cached_avg_cost / avg_cost_primary) * 100, 1
                    )
                    if avg_cost_primary > 0
                    else 0,
                    "best_for": "Repetitive prompts / system prompts / few-shot examples",
                }
            )

    return json.dumps(
        {
            "budget_usd": total_budget_usd,
            "primary_model": primary_model,
            "secondary_model": secondary_model or "none",
            "strategies": strategies,
            "tips": [
                "Use prompt caching for repeated system prompts to save 80-90% on input tokens",
                "Route simple classification/extraction to cheaper models",
                "Set max_tokens limits to prevent runaway output costs",
                "Batch requests where possible for volume discounts",
                "Monitor actual usage vs estimates and adjust weekly",
            ],
        },
        indent=2,
    )


@mcp.tool()
def find_cheapest_models(
    min_context_window: int = 8000,
    requires_vision: bool = False,
    requires_function_calling: bool = False,
    requires_reasoning: bool = False,
    top_n: int = 10,
) -> str:
    """Find the cheapest models that meet specific capability requirements."""
    chat_models = get_chat_models()
    candidates = []

    for name, info in chat_models.items():
        max_input = info.get("max_input_tokens", info.get("max_tokens", 0))
        if not isinstance(max_input, (int, float)):
            continue
        if max_input < min_context_window:
            continue
        if requires_vision and not info.get("supports_vision"):
            continue
        if requires_function_calling and not info.get("supports_function_calling"):
            continue
        if requires_reasoning and not info.get("supports_reasoning"):
            continue

        blended_cost = (
            info.get("input_cost_per_token", 0) * 0.7 + info.get("output_cost_per_token", 0) * 0.3
        )

        candidates.append(
            {
                "model": name,
                "provider": info.get("litellm_provider", "unknown"),
                "input_cost_per_1M": round(info.get("input_cost_per_token", 0) * 1_000_000, 4),
                "output_cost_per_1M": round(info.get("output_cost_per_token", 0) * 1_000_000, 4),
                "blended_cost_per_1M": round(blended_cost * 1_000_000, 4),
                "max_input_tokens": max_input,
                "max_output_tokens": info.get("max_output_tokens", info.get("max_tokens", "N/A")),
                "supports_caching": info.get("supports_prompt_caching", False),
            }
        )

    candidates.sort(key=lambda x: x["blended_cost_per_1M"])

    return json.dumps(
        {
            "filters": {
                "min_context_window": min_context_window,
                "requires_vision": requires_vision,
                "requires_function_calling": requires_function_calling,
                "requires_reasoning": requires_reasoning,
            },
            "results": candidates[:top_n],
            "total_matching": len(candidates),
        },
        indent=2,
    )


@mcp.tool()
def list_providers() -> str:
    """List all available providers and their model count."""
    chat_models = get_chat_models()
    providers: dict[str, int] = {}
    for info in chat_models.values():
        provider = info.get("litellm_provider", "unknown")
        providers[provider] = providers.get(provider, 0) + 1

    sorted_providers = sorted(providers.items(), key=lambda x: -x[1])
    return json.dumps(
        {"providers": [{"name": p, "model_count": c} for p, c in sorted_providers]},
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
