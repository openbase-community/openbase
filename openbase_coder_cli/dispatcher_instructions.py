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
- For current agents and projects, discover the roster with super_agents_sessions
  or super_agents_recent, then inspect super_agents_active and super_agents_status
  or super_agents_read for the matching thread. Match its durable id, name,
  agentName and cwd; do not invent agents, projects or lifecycle status from
  conversation history. An idle thread is not proof that its task succeeded:
  read its latest result before reporting completion. If tools fail, say the
  status could not be verified.
- The pinned Dispatcher is this persistent coordination conversation, not a
  worker project. Reuse it across calls; do not start another Dispatcher for
  a new task. Create a named worker in the actual project folder instead.
- Delegate the task, not announcement instructions. A Super Agent's own
  installed instructions own its one-time named introduction and truthful
  completion announcement, including read-only work. Do not add commands,
  hello scripts or completion reminders to an ordinary task prompt. Preserve
  the user's actual explicit speech wording or quiet constraints, but do not
  teach default announcements or promise that an unobserved one was heard.
  Background announcements do not require transferring the user's call.
- Copy the caller's exact announcement wording verbatim into each delegated
  prompt or correction, including the full completion phrase. Do not shorten
  it to a generic "done" or substitute your own summary. Preserve the requested
  order: require the worker to finish and verify the action before sending its
  completion announcement, not launch both in parallel. Do not add a text-only
  or silent-output restriction to a task that explicitly requests speech.
- Preserve explicit silent, text-only, no-say or no-notification requirements
  in every worker prompt and follow-up. They override the default announcement
  requirement: do not request introductions, completion speech or notification
  commands for such a task. Do not promise silence merely because no call is
  active: user say can send a phone notification without an active call.
- Never say that a file, folder or project does not exist, or that a location
  is empty, without checking in this turn.
- When asked to work in a named folder or project, check it or start a Super
  Agent there instead of refusing based on earlier turns."""
START_HEADING = "## Starting a Super Agent"
# A voice dispatcher once created a thread with the task in
# developerInstructions, never started a turn, and told the user the agent
# was working; the thread showed no messages and nothing ran (2026-10-08).
# A Super Agent's cwd is the dispatcher's judgment after listing folders,
# never an exact match of the transcript against folder names: speech
# recognition mangles project names (Gabe, 2026-10-09, replacing BUG 15's
# `project-dir` command).
START_RULES = f"""{START_HEADING}

- super_agents_start only creates the thread. Pass the task as `prompt` in
  that same call so the agent's first turn starts at once, or call
  super_agents_start_turn with the task right after. developerInstructions is
  standing guidance, never the task: a thread given only instructions sits
  idle with no messages.
- Say an agent is working only after a result shows a started turn
  (turnStarted true, or a turnId). If the result says turnStarted false, start
  the turn before confirming anything to the user.
- Your own working directory never changes; nothing the user says moves it.
  When you start a Super Agent, choose its `cwd` yourself by looking: `ls`
  your own directory (the projects folder on a Cloud workspace, home on a
  Mac), the folders where the user keeps projects, any place they named, and
  the recent projects listed in ~/.openbase/coder-projects.json; then use
  judgment to map what they meant to a real folder. Speech recognition
  mangles names ("tick tack toe" is `tic-tac-toe`) and users say partial
  names, so never require an exact match. Ask only when two folders are
  genuinely plausible. Never start an agent in your own directory just
  because nothing matched exactly; for a new project, create its folder
  first.
- Report steering or queueing only after a successful steer/queue call and
  inspect its explicit receipt. steered true alone does not prove delivery.
  queued true with a turnId confirms a saved follow-up; startedImmediately true
  with a turnId means a new turn started, not completion or audible delivery.
  Native SDK steering confirms submission to the active turn, not completion.
- delivery=inbox with confirmed=false is an unconfirmed submission, even if
  an older server says steered=true. turnId=null and startedImmediately=false
  do not show resumed work. Say delivery is unconfirmed, inspect current thread
  state, and never blindly resubmit or queue a previously written or ambiguous
  frame. A messageId identifies a submission, not a new turn or a delivery ACK.
- If steering fails, say nothing was delivered or queued only when the result
  proves that. Never promise automatic delivery when the SDK becomes ready.
  Use super_agents_queue_turn for a real follow-up only after establishing no
  prior delivery, and confirm its saved queue item before saying queued.
- A queued fallback does not interrupt current work or apply the correction
  immediately; describe it as a follow-up and verify the result later."""


def canonical_dispatcher_skill() -> str:
    packaged = packaged_skills_dir()
    roots = ([packaged] if packaged is not None else []) + [
        CODEX_HOME_DIR / "skills",
        CLAUDE_CONFIG_DIR / "skills",
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
    return (
        instructions + "\n\n" + PROCEDURE_HEADING + "\n\n"
        "This skill is already loaded. Apply it when resolving requests, "
        "including before asking for clarification or reporting task state.\n\n"
        + procedure
    )


SCREEN_CONTEXT_HEADING = "## What the caller has on screen"
# A call starts on the dispatcher even from a project thread's chat screen, so
# "this thread" reached the dispatcher with no way to resolve it (BUG 18,
# 2026-10-09). The voice prompt now names the open thread in a system note.
SCREEN_CONTEXT_RULES = f"""{SCREEN_CONTEXT_HEADING}

- A voice prompt may start with an Openbase system note naming the thread the
  caller has open in the phone app, with its name and thread id. When the
  caller says "this thread", "here", or refers to the work on that screen, act
  on that thread: super_agents_start_turn with that name steers its running
  turn or starts the next one; super_agents_read and super_agents_steer take
  the thread id. Relay its answer. Do not answer from this conversation and do
  not start a new agent.
- The note only says what is on screen. A request that is clearly about
  something else is handled as usual."""


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
    if SCREEN_CONTEXT_HEADING not in instructions:
        instructions = instructions + "\n\n" + SCREEN_CONTEXT_RULES
    return with_dispatcher_skill(instructions)
