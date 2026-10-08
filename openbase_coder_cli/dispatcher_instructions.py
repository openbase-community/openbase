"""Supply the built-in dispatcher rules and the canonical dispatch procedure."""

from pathlib import Path

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


def with_dispatcher_rules(instructions: str) -> str:
    """Dispatcher developer instructions: the base, built-in rules, procedure."""
    if CURRENT_STATE_HEADING not in instructions:
        instructions = instructions + "\n\n" + CURRENT_STATE_RULES
    return with_dispatcher_skill(instructions)
