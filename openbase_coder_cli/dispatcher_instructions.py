"""Supply the canonical dispatch procedure before the first voice request."""

from pathlib import Path

from .paths import CODEX_HOME_DIR, CLAUDE_CONFIG_DIR
from .runtime import packaged_skills_dir

SKILL_NAME = "openbase-super-agent-dispatcher"
PROCEDURE_HEADING = "## Loaded canonical Super Agent dispatch procedure"


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
