"""Picker offerings, separate from the execution resolver's legacy aliases.

Verified against Codex 0.161.0 model/list and Claude Code 2.1.282 SDK
initialize models on 2026-10-09. Clients render the API's options verbatim.
"""

from super_agents.claude_options import OPENBASE_CLOUD_CLAUDE_MODEL_MAP

CLAUDE_MODEL_OPTIONS = tuple(
    {
        "id": OPENBASE_CLOUD_CLAUDE_MODEL_MAP[family],
        "label": label,
        "description": description,
        "is_default": family == "haiku",
    }
    for family, label, description in (
        ("haiku", "Claude Haiku 4.5", "Fastest for quick answers."),
        ("sonnet", "Claude Sonnet 5", "Efficient for routine tasks."),
        ("opus", "Claude Opus 5.5", "For everyday and complex tasks."),
        ("fable", "Claude Fable 5.1", "For the hardest and longest-running tasks."),
    )
)

CODEX_MODEL_OPTIONS = (
    {
        "id": "gpt-5.6-terra",
        "label": "GPT-5.6-Terra",
        "description": "Older balanced model for straightforward work.",
    },
    {
        "id": "gpt-6-luna",
        "label": "GPT-6-Luna",
        "description": "Fast and affordable model for easier tasks.",
    },
    {
        "id": "gpt-6.1-sol",
        "label": "GPT-6.1-Sol",
        "description": "Latest workhorse model for coding and everyday work.",
    },
    {
        "id": "gpt-6-astra",
        "label": "GPT-6-Astra",
        "description": "Frontier intelligence for the most demanding work.",
    },
)
