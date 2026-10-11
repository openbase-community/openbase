"""Final developer-facing explanation of agent configuration after setup."""

from openbase_coder_cli.cli.setup.notices import info_notice


def agent_setup_summary(
    *, include_default_hooks: bool, shared_super_agents_mcp: bool
) -> str:
    sentences = [
        "Your Codex and Claude Code setup now includes Openbase session "
        "profiles for model and tool settings, plus links to the bundled "
        "Openbase skills."
    ]
    if include_default_hooks:
        sentences.append(
            "Session identity hooks are registered in your default agent "
            "configurations so ordinary sessions receive their Agent-Thread-Id "
            "for commits."
        )
    else:
        sentences.append(
            "Openbase's session identity hooks are scoped to its session profiles."
        )
    if shared_super_agents_mcp:
        sentences.append(
            "Super Agents MCP registration is enabled for ordinary terminal "
            "sessions so they can dispatch agents."
        )
    else:
        sentences.append(
            "Automatic Super Agents MCP registration in your default agent "
            "configurations is disabled."
        )
    sentences.append(
        "Start or resume a new Codex or Claude Code process to load these settings."
    )
    return info_notice(" ".join(sentences))
