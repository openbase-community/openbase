"""Supply the built-in dispatcher rules and the canonical dispatch procedure."""

from pathlib import Path

from .host_kind import HOST_KIND_HEADING, with_host_section
from .paths import CLAUDE_CONFIG_DIR, CODEX_HOME_DIR
from .runtime import packaged_skills_dir

SKILL_NAME = "openbase-super-agent-dispatcher"
PROCEDURE_HEADING = "## Loaded canonical Super Agent dispatch procedure"
CURRENT_STATE_HEADING = "## Current state of the user's computer"
# A persistent dispatcher once answered "your desktop is empty" from a listing
# made 100 minutes earlier in the same thread, and refused to open a desktop
# folder created since; the state had changed, the conversation had not.
CURRENT_STATE_RULES = f"""{CURRENT_STATE_HEADING}

- Answer questions about the current state of the user's computer (files,
  folders, apps, processes, repo status) by checking it in this turn, with a
  quick read-only command or through a Super Agent, never from earlier turns
  of this conversation: it may have changed since.
- Never say that a file, folder or project does not exist, or that a location
  is empty, without checking in this turn.
- When asked to work in a named folder or project, check it or start a Super
  Agent there instead of refusing based on earlier turns."""
START_HEADING = "## Starting a Super Agent"
# A voice dispatcher once created a thread with the task in
# developerInstructions, never started a turn, and told the user the agent
# was working; the thread showed no messages and nothing ran (2026-10-08).
START_RULES = f"""{START_HEADING}

- super_agents_start only creates the thread. Pass the task as `prompt` in
  that same call so the agent's first turn starts at once, or call
  super_agents_start_turn with the task right after. developerInstructions is
  standing guidance, never the task: a thread given only instructions sits
  idle with no messages.
- Say an agent is working only after a result shows a started turn
  (turnStarted true, or a turnId). If the result says turnStarted false, start
  the turn before confirming anything to the user."""


def canonical_dispatcher_skill() -> str:
    packaged = packaged_skills_dir()
    roots = ([packaged] if packaged is not None else []) + [
        CODEX_HOME_DIR / "skills", CLAUDE_CONFIG_DIR / "skills",
    ]
    for root in roots:
        path = Path(root) / SKILL_NAME / "SKILL.md"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    # Existing instructions still require loading the skill through tools.
    return ""


def with_dispatcher_skill(instructions: str) -> str:
    if PROCEDURE_HEADING in instructions:
        return instructions
    procedure = canonical_dispatcher_skill()
    if not procedure:
        return instructions
    return (instructions + "\n\n" + PROCEDURE_HEADING + "\n\n"
            "This skill is already loaded. Apply it when resolving requests, "
            "including before asking for clarification or reporting task state.\n\n" + procedure)


def with_dispatcher_rules(instructions: str, *, host: str | None = None) -> str:
    """Dispatcher developer instructions: the base, host, built-in rules, procedure.

    Built-in rules are the current-state rules and the start rules; each is
    appended once, so the result is idempotent.

    ``host`` is a ``host_kind`` value; None detects this install's.
    """
    if HOST_KIND_HEADING not in instructions:
        base, heading, current_state = instructions.partition(CURRENT_STATE_HEADING)
        instructions = with_host_section(base.rstrip(), host)
        if heading:
            instructions += "\n\n" + heading + current_state
    if CURRENT_STATE_HEADING not in instructions:
        instructions = instructions + "\n\n" + CURRENT_STATE_RULES
    if START_HEADING not in instructions:
        instructions = instructions + "\n\n" + START_RULES
    return with_dispatcher_skill(instructions)
