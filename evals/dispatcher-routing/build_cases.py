"""Build the dispatcher-routing eval case set.

Writes cases.json, fleets.json and cases-review.html next to this file.

One case = one voice-dispatcher turn:
  fleet      - which fixture fleet (registered Super Agent threads + Desktop
               folders) the dispatcher sees, see FLEETS below
  history    - prior exchanges in the dispatcher session (verbatim or
               condensed), newest last
  utterance  - the voice-transcribed user turn under test
  expected   - the routing gold: disposition, target thread, required/forbidden
               content in the delivered instruction, forbidden actions
  observed   - what the real dispatcher did when the turn is taken from a real
               session (reference only, NOT gold)

Dispositions (closed set, primary metric):
  new_thread       start one or more new Super Agent threads
  existing_thread  deliver to an existing thread (steer / queue / start_turn)
  cancel_replace   cancel an existing owner and start a replacement
  transfer         hand the voice session to an agent (shell transfer command)
  cancel           cancel an existing thread, start nothing
  answer_only      answer / inspect / report; spawn or steer nothing
  clarify          ask the user; change nothing
  mixed            several of the above, listed in expected.actions
"""

from __future__ import annotations

import html
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Fleet fixtures. `threads` becomes the registered-task JSON the dispatcher is
# given each turn AND the canned answers of the stubbed super-agents MCP
# tools. `folders` are created under the sandbox Desktop before the turn.
# --------------------------------------------------------------------------

DESK = "~/Desktop"
OB = "~/Projects/openbase/code/openbase-coder-workspace"


def th(
    name, agent, cwd, status, model="opus", last=None, minutes_ago=5, turn_running=None
):
    return {
        "name": name,
        "agent_name": agent,
        "cwd": cwd,
        "status": status,  # running | completed | failed | unknown
        "model": model,
        "last_useful_message": last,
        "last_activity_minutes_ago": minutes_ago,
        "active_turn_prompt": turn_running,
    }


FLEETS = {
    "empty": {"threads": [], "folders": {}},
    "desktop-fresh": {
        "threads": [],
        "folders": {
            "maple": ["briefing.md"],
            "cedar": ["briefing.md"],
            "elm": ["briefing.md"],
        },
    },
    "maple-cedar-running": {
        "threads": [
            th(
                "maple",
                "Carl",
                f"{DESK}/maple",
                "running",
                last="Scaffolding the React Tetris app.",
                minutes_ago=2,
                turn_running="Read briefing.md in ~/Desktop/maple and build the app it describes.",
            ),
            th(
                "cedar",
                "Dottie",
                f"{DESK}/cedar",
                "running",
                last="Implementing chess move validation.",
                minutes_ago=3,
                turn_running="Read briefing.md in ~/Desktop/cedar and build the app it describes.",
            ),
        ],
        "folders": {
            "maple": ["briefing.md"],
            "cedar": ["briefing.md"],
            "elm": ["briefing.md"],
        },
    },
    "maple-cedar-running-elm-running": {
        "threads": [
            th(
                "maple",
                "Carl",
                f"{DESK}/maple",
                "running",
                last="Build passed; heading is Silver Fox.",
                minutes_ago=4,
            ),
            th(
                "cedar",
                "Dottie",
                f"{DESK}/cedar",
                "running",
                last="Build passed; heading is Blue Harbor.",
                minutes_ago=4,
            ),
            th(
                "elm",
                "Nico",
                f"{DESK}/elm",
                "running",
                last="Running the controlled stalled command (sleep 900).",
                minutes_ago=1,
                turn_running="Perform the controlled stalled command test described in briefing.md.",
            ),
        ],
        "folders": {
            "maple": ["briefing.md"],
            "cedar": ["briefing.md"],
            "elm": ["briefing.md"],
        },
    },
    "maple-cedar-done": {
        "threads": [
            th(
                "maple",
                "Carl",
                f"{DESK}/maple",
                "completed",
                last="Copper Meadow heading set; production build exit 0; build.log and result.md written.",
                minutes_ago=12,
            ),
            th(
                "cedar",
                "Dottie",
                f"{DESK}/cedar",
                "completed",
                last="Blue Harbor heading and dark blue background set; production build exit 0.",
                minutes_ago=9,
            ),
        ],
        "folders": {
            "maple": ["briefing.md", "build.log", "result.md"],
            "cedar": ["briefing.md", "build.log", "result.md"],
            "elm": ["briefing.md"],
        },
    },
    "maple-stale-cedar-running": {
        "threads": [
            th(
                "maple",
                "Carl",
                f"{DESK}/maple",
                "running",
                last="Starting the production build.",
                minutes_ago=41,
                turn_running="Change the heading to Silver Fox and run the production build.",
            ),
            th(
                "cedar",
                "Dottie",
                f"{DESK}/cedar",
                "running",
                last="Implementing chess move validation.",
                minutes_ago=3,
            ),
        ],
        "folders": {
            "maple": ["briefing.md"],
            "cedar": ["briefing.md"],
            "elm": ["briefing.md"],
        },
    },
    # Reconstructions of the 2026-09-22 real session at different moments.
    "sep22-early": {
        "threads": [
            th(
                "fable-model-check",
                "Rachel",
                OB,
                "completed",
                model="fable",
                last="I'm Rachel, running on Claude Fable 5.",
                minutes_ago=1,
            ),
        ],
        "folders": {},
    },
    "sep22-casper": {
        "threads": [
            th(
                "fable-model-check",
                "Rachel",
                OB,
                "completed",
                model="fable",
                minutes_ago=16,
            ),
            th(
                "voice-transcript-diagnostics",
                "Casper",
                OB,
                "running",
                last="Mapping the iOS and desktop voice pipelines.",
                minutes_ago=4,
            ),
        ],
        "folders": {},
    },
    "sep22-casper-dorothy": {
        "threads": [
            th(
                "fable-model-check",
                "Rachel",
                OB,
                "completed",
                model="fable",
                minutes_ago=23,
            ),
            th(
                "voice-transcript-diagnostics",
                "Casper",
                OB,
                "running",
                last="Mapping the iOS and desktop voice pipelines.",
                minutes_ago=9,
            ),
            th(
                "cli-report-issue",
                "Dorothy",
                f"{OB}-worktrees/cli-report-issue/cli",
                "running",
                last="Exploring the CLI command structure.",
                minutes_ago=1,
            ),
        ],
        "folders": {},
    },
    "sep22-cindy-just-started": {
        "threads": [
            th(
                "voice-transcript-diagnostics",
                "Casper",
                OB,
                "completed",
                last="Standing by for the two exploration results.",
                minutes_ago=600,
            ),
            th(
                "cli-report-issue",
                "Dorothy",
                f"{OB}-worktrees/cli-report-issue/cli",
                "failed",
                last="RuntimeError: Executor shutdown has been called",
                minutes_ago=590,
            ),
            th(
                "vpn-disconnect-diagnostics",
                "Cindy",
                OB,
                "running",
                model="opus",
                last="Reading the iOS VPN manager.",
                minutes_ago=1,
                turn_running="Investigate a suspected VPN auto-disconnect issue with the Openbase iOS app.",
            ),
        ],
        "folders": {},
    },
    "sep22-midday": {
        "threads": [
            th(
                "voice-transcript-diagnostics",
                "Casper",
                OB,
                "completed",
                last="Standing by for the two exploration results.",
                minutes_ago=600,
            ),
            th(
                "cli-report-issue",
                "Dorothy",
                f"{OB}-worktrees/cli-report-issue/cli",
                "failed",
                last="RuntimeError: Executor shutdown has been called",
                minutes_ago=590,
            ),
            th(
                "vpn-disconnect-diagnostics-fable",
                "Skylar",
                OB,
                "running",
                model="fable",
                last="Inspecting NEPacketTunnelProvider lifecycle.",
                minutes_ago=10,
            ),
            th(
                "ios-dispatcher-view-glitches",
                "Joey",
                OB,
                "running",
                model="fable",
                last="Stripping voice tags in the dispatcher view.",
                minutes_ago=8,
            ),
            th(
                "andy-palmer-intro-email-reply",
                "Oliver",
                OB,
                "running",
                last="Searching the Openbase inbox for Andy Palmer.",
                minutes_ago=6,
            ),
            th(
                "joanna-marketing-video-plan",
                "Calypso",
                OB,
                "running",
                last="Found Joanna's announcement email; awaiting read approval.",
                minutes_ago=4,
            ),
            th(
                "approval-resume-agent-online",
                "Savannah",
                OB,
                "running",
                model="fable",
                last="Mapping approval/resume code paths.",
                minutes_ago=2,
            ),
        ],
        "folders": {},
    },
    "sep22-afternoon": {
        "threads": [
            th(
                "voice-transcript-diagnostics",
                "Casper",
                OB,
                "completed",
                last="Standing by for the two exploration results.",
                minutes_ago=1100,
            ),
            th(
                "cli-report-issue",
                "Dorothy",
                f"{OB}-worktrees/cli-report-issue/cli",
                "completed",
                last="Merged openbase report issue into develop.",
                minutes_ago=200,
            ),
            th(
                "vpn-disconnect-diagnostics-fable",
                "Skylar",
                OB,
                "completed",
                model="fable",
                last="Report written: .reports/2026-09-22-ios-vpn-disconnect-diagnostics.md",
                minutes_ago=180,
            ),
            th(
                "ios-dispatcher-view-glitches",
                "Joey",
                OB,
                "completed",
                model="fable",
                last="Committed voice-tag stripping and prompt display fix to develop.",
                minutes_ago=170,
            ),
            th(
                "andy-palmer-intro-email-reply",
                "Oliver",
                OB,
                "completed",
                last="Draft reply saved; awaiting approval.",
                minutes_ago=150,
            ),
            th(
                "joanna-marketing-video-plan",
                "Calypso",
                OB,
                "completed",
                last="Plan written to ~/Desktop/joanna-marketing-video-plan.md",
                minutes_ago=150,
            ),
            th(
                "super-agents-state-accuracy",
                "Morgan",
                OB,
                "failed",
                model="fable",
                last="RuntimeError: Executor shutdown has been called",
                minutes_ago=120,
            ),
            th(
                "speech-recognition-trouble-diagnosis",
                "Jolene",
                OB,
                "failed",
                last="RuntimeError: Executor shutdown has been called",
                minutes_ago=110,
            ),
            th(
                "imessage-maybe-labels",
                "Pieter",
                OB,
                "completed",
                last="Proposal written for maybe-label reconstruction.",
                minutes_ago=100,
            ),
        ],
        "folders": {},
    },
    "sep22-evening": {
        "threads": [
            th(
                "voice-transcript-diagnostics",
                "Casper",
                OB,
                "completed",
                last="Standing by for the two exploration results.",
                minutes_ago=1130,
            ),
            th(
                "vpn-disconnect-diagnostics-fable",
                "Skylar",
                OB,
                "completed",
                model="fable",
                last="Report written: .reports/2026-09-22-ios-vpn-disconnect-diagnostics.md",
                minutes_ago=210,
            ),
            th(
                "ios-dispatcher-view-glitches",
                "Joey",
                OB,
                "completed",
                model="fable",
                last="Committed voice-tag stripping and prompt display fix to develop.",
                minutes_ago=200,
            ),
            th(
                "super-agents-state-accuracy",
                "Morgan",
                OB,
                "running",
                model="fable",
                last="Resumed after crash; tracing status view layer.",
                minutes_ago=3,
            ),
            th(
                "speech-recognition-trouble-diagnosis",
                "Jolene",
                OB,
                "completed",
                last="Committed: Stop false spoken 'speech recognition is having trouble' alarms.",
                minutes_ago=5,
            ),
            th(
                "executor-shutdown-crash-investigation",
                "Thandi",
                OB,
                "running",
                last="Reading livekit-agent restart logic.",
                minutes_ago=2,
            ),
        ],
        "folders": {},
    },
    "morgan-plan-question": {
        "threads": [
            th(
                "super-agents-state-accuracy",
                "Morgan",
                OB,
                "running",
                model="fable",
                last="Plan-mode question: Should the completion fix also cover Codex app-server threads, or Claude Code threads only? (a) both backends (b) Claude Code only",
                minutes_ago=1,
                turn_running="Diagnose and fix the state-accuracy bug (plan mode).",
            ),
        ],
        "folders": {},
    },
}

# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

CASES: list[dict] = []


def case(id, tags, fleet, utterance, expected, history=None, observed=None, note=None):
    assert fleet in FLEETS, fleet
    assert expected["disposition"] in {
        "new_thread",
        "existing_thread",
        "cancel_replace",
        "transfer",
        "cancel",
        "answer_only",
        "clarify",
        "mixed",
    }, expected["disposition"]
    CASES.append(
        {
            "id": id,
            "tags": tags,
            "fleet": fleet,
            "history": history or [],
            "utterance": utterance,
            "expected": expected,
            "observed": observed,
            "note": note,
        }
    )


def ex(
    disposition,
    target=None,
    *,
    new_threads=None,
    model=None,
    cwd=None,
    must_include=None,
    must_not_include=None,
    must_not=None,
    mechanism=None,
    actions=None,
    spoken_must_include=None,
    spoken_must_end_with=None,
    accept_also=None,
):
    d = {"disposition": disposition}
    if target is not None:
        d["target"] = target
    if new_threads is not None:
        d["new_threads"] = new_threads
    if model is not None:
        d["model"] = model
    if cwd is not None:
        d["cwd"] = cwd
    if mechanism is not None:
        d["mechanism"] = mechanism
    if actions is not None:
        d["actions"] = actions
    if must_include:
        d["must_include"] = must_include
    if must_not_include:
        d["must_not_include"] = must_not_include
    if must_not:
        d["must_not"] = must_not
    if spoken_must_include:
        d["spoken_must_include"] = spoken_must_include
    if spoken_must_end_with:
        d["spoken_must_end_with"] = spoken_must_end_with
    if accept_also:
        d["accept_also"] = accept_also
    return d


# ---- Real turns from the 2026-09-22 dispatcher session (source: real) ------

case(
    "real-hello",
    ["answer-only", "real", "easy"],
    "empty",
    "Hey, are you there?",
    ex("answer_only", must_not=["new_thread", "existing_thread"]),
    observed="Answered: I'm here! What can I do for you?",
)

case(
    "real-fable-model-check",
    ["new-thread", "real", "model-override"],
    "empty",
    "Launching a new Fable Super Agent and ask it what model are you and have it announced from the background not only its name, but what model it is?",
    ex(
        "new_thread",
        new_threads=1,
        model="fable",
        must_include=["model"],
        must_not=["answer_the_question_itself"],
    ),
    observed="start fable-model-check (Rachel, model=fable) + start_turn asking it to announce name and model via user say.",
)

case(
    "real-rachel-transcript",
    ["answer-only", "real", "verify"],
    "sep22-early",
    "What model did Rachel say she was running on? Check for transcripts rather than the super agent metadata?",
    ex("answer_only", target="fable-model-check", must_not=["new_thread"]),
    history=[
        [
            "Launching a new Fable Super Agent and ask it what model are you ...",
            "Done - I launched a new Fable Super Agent named Rachel.",
        ]
    ],
    observed="Grepped Rachel's session log; answered Claude Fable 5.",
)

case(
    "real-spike-wifi-diagnostics",
    ["new-thread", "real", "long-utterance"],
    "sep22-early",
    "So the question is how can we get it so that I can see a visualization of everything that went on including network timing and what was said when on the iOS side versus on the MacBook Pro side and and how can we get enough logs where we actually can diagnose why my words are getting chopped off because you can see that one of my messages about the particular subdirectory of the PSG project was it was completely lost. Well actually first start a new super agent to diagnose why as I walked out of my Wi-Fi range you didn't hear the folder name about Spike the first time because I definitely said it the first time. And in particular I imagine you won't have enough diagnostics even though I'll",
    ex("new_thread", new_threads=1, must_include=["Wi-Fi", "Spike"]),
    history=[
        [
            "In there there's a directory called spike Or something with spike in the name.",
            "Found it: psg-ai-spike under ~/Projects/psg/code.",
        ]
    ],
    observed="start voice-transcript-diagnostics (Casper) with a diagnostics brief.",
)

case(
    "real-report-issue-start",
    ["new-thread", "real", "dedupe-check"],
    "sep22-casper",
    "So I don't know if you already did this but start a super agent to implement in a new multi workspace work tree for open base the future of basically the CLI should report issue and basically when that CLI command is called to report an issue it should capture the most recent interaction by user and the thread or out of it particularly the message and the metadata and add it to some problems database within open base for for diagnostics. And after Yeah yeah that's that's good enough for now. It's kind of just. Like for internal use mostly, but it can it can go to production. Too.",
    ex("new_thread", new_threads=1, must_include=["report issue", "worktree"]),
    observed="start cli-report-issue (Dorothy) in a new multi-workspace worktree.",
)

case(
    "synth-report-issue-already-running",
    ["existing-thread", "synth", "dedupe-check"],
    "sep22-casper-dorothy",
    "So I don't know if you already did this but start a super agent to implement in a new multi workspace work tree for open base the CLI should report issue command that captures the most recent interaction and adds it to a problems database for diagnostics.",
    ex(
        "answer_only",
        target="cli-report-issue",
        must_not=["new_thread"],
        accept_also=["existing_thread"],
    ),
    note="Same request as real-report-issue-start but Dorothy already owns it one minute ago. Gold: say Dorothy is on it (optionally steer her), never start a second owner.",
)

case(
    "real-vpn-diagnostics",
    ["new-thread", "real", "garbled"],
    "sep22-casper-dorothy",
    "No, that's okay. What I want to do is to. Do some open based stuff. So right now I'm pretty sure that the VPN keeps auto disconnect. Ing. I'm not sure if that's because I have multiple VPNs installed on my iPhone between the staging app VPN. And the normal app reform. But basically, yeah, every time I open the app or many times when I open the app, it prompts me to hit the connect to VPN button. And maybe it's a red herring because I'm getting new builds installed onto my iPhone. And it wipes the VPN from before. So that could be it. But I'm also paranoid that the VPN is just auto disconnect. Ing. But I know it was active last night, so I'm not 100% sure what's going on. But what would be",
    ex("new_thread", new_threads=1, must_include=["VPN"]),
    observed="start vpn-disconnect-diagnostics (Cindy, opus); then asked whether 'table agent' meant plan mode.",
)

case(
    "real-not-fable-correction",
    ["cancel-replace", "real", "correction"],
    "sep22-cindy-just-started",
    "No, no, not Fable. F-A-B-L-E fable agent.",
    ex(
        "cancel_replace",
        target="vpn-disconnect-diagnostics",
        new_threads=1,
        model="fable",
        must_include=["VPN"],
        must_not=["two_live_owners_for_same_task"],
    ),
    history=[
        [
            "... I'm pretty sure that the VPN keeps auto disconnecting ... (long)",
            'Quick check - that came through garbled ("table agent"/"papal agent"), so I want to confirm before changing anything. My best guess is you want this VPN investigation run as a plan-mode agent?',
        ]
    ],
    observed="cancel vpn-disconnect-diagnostics; start vpn-disconnect-diagnostics-fable (Skylar, model=fable) with the same brief.",
)

case(
    "real-ios-dispatcher-view",
    ["new-thread", "real", "model-override"],
    "sep22-cindy-just-started",
    "Great. So why don't you know look into how to start a new also payable super agent or basically making it so that iOS is less glitchy because right now I'm looking in the dispatcher view. On iOS trying to get a visual indication of what I told you to do. And I just see my long prompt cut off. So like, and I also see voice tags being rendered in iOS, which I thought we already fixed, but I see XML because it's injecting the voice tags and those are currently rendered when they're not supposed to be. So startup Fable agent for fixing that. I can fix it right on the develop branch.",
    ex(
        "new_thread",
        new_threads=1,
        model="fable",
        must_include=["develop", "voice tag"],
    ),
    observed="start ios-dispatcher-view-glitches (Joey, fable) on develop.",
)

case(
    "real-andy-palmer-email",
    ["new-thread", "real", "constraints"],
    "sep22-cindy-just-started",
    "All right, now start a super agent. Maybe this one doesn't have to be fable, but it should basically check for an email from it. Andy Palmer in my open base email, I think there would be. Introducing me to someone Gilpin. I think Kevin built him. And using the email writing using the email writing skill to drafter response. It'll need my approval along the way. But that's fine. And yes, yes. Prestige. Don't actually send it. Though.",
    ex(
        "new_thread",
        new_threads=1,
        must_include=["Andy Palmer", "send"],
        must_not=["model_override"],
    ),
    observed="start andy-palmer-intro-email-reply (Oliver, default model); prompt forbids sending.",
)

case(
    "real-joanna-video-plan",
    ["new-thread", "real", "easy"],
    "sep22-cindy-just-started",
    "Start another one to look at basically, I got an email from Joanna's new past show and on it, but it also has her new hire who's their head of marketing or go to market. I think her name is Carmen. Get the approval to read it and then basically. Go on and draft the plan for my marketing video that'll help Joanna with. That would be great. Because I need Joanna to help me with a mindful microscopic coming. Up.",
    ex("new_thread", new_threads=1, must_include=["Joanna"]),
    observed="start joanna-marketing-video-plan (Calypso).",
)

case(
    "real-approval-resume-plus-status",
    ["mixed", "real", "compound"],
    "sep22-midday",
    "Fable Super event for basically making sure that if an open vase approval is required, that when I hit through, the agents that requested it, whether it's code or codex back, we'll then come back online. Like I'm not sure I'm in behavior and it has provided permission to run like live spikes on my MacBook Pro. Regarding this behavior. Although my codex account is currently out of credit, so it'll be more difficult. So go ahead and give it permission. Just proceed. To run the spike and then check all the super agents that you want. This morning just now with me because some of them finished and some of them I have questions and some of them have issues.",
    ex(
        "mixed",
        actions=[
            {"new_thread": 1, "model": "fable", "must_include": ["approval"]},
            {"answer_only": "status of every agent from this session"},
        ],
        must_not=["more_than_one_new_thread"],
    ),
    observed="start approval-resume-agent-online (Savannah, fable); super_agents_status; spoke a per-agent status rundown.",
)

case(
    "real-dorothy-failure-and-carmen-caveat",
    ["existing-thread", "real", "compound"],
    "sep22-midday",
    "So what was the failure of the Dorthy? Yeah, I didn't remember dispatching Dorothy at all. I was also, yeah, don't tell that you can confirm that Carmen is the correct. Person. I'm a child of Carmen and Joanna then can say that we just have to follow whatever Carmen wants and basically we had a hackathon with Joanna and I've been using vibes indirectly through its API. And I found it really accurate and helpful in determining my mental readiness on a given day and actually integrated in with my vibe coding workflow.",
    ex(
        "existing_thread",
        target="joanna-marketing-video-plan",
        must_include=["Carmen"],
        must_not=["new_thread"],
        spoken_must_include=["Dorothy"],
    ),
    observed="read + status on cli-report-issue; start_turn on joanna-marketing-video-plan with the Carmen caveat and hackathon context; explained Dorothy's crash.",
)

case(
    "real-dorothy-continue",
    ["existing-thread", "real", "resume-idle"],
    "sep22-midday",
    "Okay, yeah, yeah. So I have Dorothy continue her work. After continue. And then what was the next thing you're going to say after Dorothy?",
    ex(
        "existing_thread",
        target="cli-report-issue",
        mechanism="start_turn_on_idle",
        must_not=["new_thread"],
    ),
    history=[
        [
            "So what was the failure of the Dorothy? ...",
            "On Dorothy's failure: she got well into building the report-issue command and then the underlying process exited (executor shutdown), not a task problem. Want me to have her continue?",
        ]
    ],
    observed="start_turn on cli-report-issue telling Dorothy the interruption was a process exit and to resume.",
)

case(
    "real-state-accuracy-fable",
    ["new-thread", "real", "model-override"],
    "sep22-midday",
    "Okay, yeah, regarding like Skyler and the ones that Cho completed even though they have subagents. Like start a new fable tier super agent for diagnosing this issue and making sure that super agents MCP accur. Ately reflects the agent's state. So if they still have sub agents. Working, they can't be marked as completed properly. Or maybe if they have a watch going, they're like three. On-one products and then use this surface of this method.",
    ex("new_thread", new_threads=1, model="fable", must_include=["completed"]),
    observed="start super-agents-state-accuracy (Morgan, fable).",
)

case(
    "real-triple-steer",
    ["existing-thread", "real", "multi-target"],
    "sep22-midday",
    "So have the report issue command merged into the developed branch by that super agent that I was working on it. Yes. Proceed. And then I think the plan for the marketing video should be dropped on my desktop so that I don't forget about it. Also, what's the status of the ND Palmer intro? You should tell that agent that it definitely was there. And perhaps it's, I don't know, maybe it's my MIT email address. Maybe it's my open based email address and maybe it's my momsecu. Gabe gmail.com address. But it definitely was sent to me. So if you can't find that email, it needs to try harder to find it. And it's in the conversation with Andy with the ending.",
    ex(
        "mixed",
        actions=[
            {"existing_thread": "cli-report-issue", "must_include": ["develop"]},
            {
                "existing_thread": "joanna-marketing-video-plan",
                "must_include": ["Desktop"],
            },
            {
                "existing_thread": "andy-palmer-intro-email-reply",
                "must_include": ["MIT"],
            },
        ],
        must_not=["new_thread", "duplicate_delivery"],
    ),
    observed="start_turn on all three threads, each once; did not start a new thread.",
)

case(
    "real-imessage-maybe",
    ["new-thread", "real", "easy"],
    "sep22-midday",
    "Super agent investigated my iMessage CLI skill and especially the question around basically iOS has this maybe feature like maybe it's this person. And do we have access to those maybe labels when we're searching for conversations or doing that? If not, how might we reconstruct them without sending data about the conversation to a third party cloud provider? So yeah, it should propose something for that.",
    ex("new_thread", new_threads=1, must_include=["iMessage"]),
    observed="start imessage-maybe-labels (Pieter).",
)

case(
    "real-summarize-agents",
    ["answer-only", "real", "status"],
    "sep22-afternoon",
    "So I'm coming up to the end of my walk summarize. All the super agents and how far they got. So I can decide. So I can try to remember to not forget them or construct them to give visible evidence on my desktop.",
    ex("answer_only", must_not=["new_thread", "existing_thread", "write_file"]),
    observed="Wrote a summary file to the Desktop and spoke a short pointer. The user later said they did not want the file.",
    note="Gold is a spoken rundown, no file. 'visible evidence on my desktop' is about the agents' deliverables, not a new file.",
)

case(
    "real-speech-recognition-trouble",
    ["new-thread", "real", "authorization"],
    "sep22-afternoon",
    "Start a new super agent to diagnose why I'm getting this speech recognition is having trouble message played over the system. You can check open this or it can check open race logs and push effects to develop if it thinks a fix is needed. Yes, proceed.",
    ex("new_thread", new_threads=1, must_include=["speech recognition", "develop"]),
    observed="start speech-recognition-trouble-diagnosis (Jolene).",
)

case(
    "real-one-by-one",
    ["answer-only", "real", "status"],
    "sep22-afternoon",
    "I didn't actually want to write that file in the desktop, but you can keep it there. I want you to literally tell me what each super agent is doing one by one. Like, so what's the first one in that file?",
    ex("answer_only", must_not=["new_thread", "existing_thread", "write_file"]),
    observed="Spoke the first agent's status.",
)

case(
    "real-did-morgan-commit",
    ["answer-only", "real", "verify"],
    "sep22-afternoon",
    "Did Morgan yeah, did Morgan commit the fix to develop?",
    ex(
        "answer_only",
        target="super-agents-state-accuracy",
        must_not=["new_thread", "existing_thread"],
    ),
    observed="Grepped Morgan's log for commit evidence; answered not yet.",
)

case(
    "real-did-she-succeed",
    ["answer-only", "real", "verify"],
    "sep22-afternoon",
    "But did she succeed or not?",
    ex(
        "answer_only",
        target="approval-resume-agent-online",
        must_not=["new_thread", "existing_thread"],
    ),
    history=[
        [
            "Okay, what's the next one after Morgan?",
            "Next is Savannah (Fable) - the approval-resume investigation. Status says completed.",
        ]
    ],
    observed="Read Savannah's log; answered honestly that she stopped mid-investigation.",
)

case(
    "real-walkthrough-refresh",
    ["answer-only", "real", "status"],
    "sep22-afternoon",
    "Yeah. Where are we going through them one by one? Like, let's re-go through them one by one. Or. But make sure your desktop report is up to date first. And then once you make sure, because a lot of time has passed, I imagine all the super agents are done. Right? Or are any of them failed? Only the ones from today is what I'm asking them.",
    ex("answer_only", must_not=["new_thread", "existing_thread"]),
    observed="Refreshed the Desktop report; answered which two failed (Morgan, Jolene) and why.",
    note="Updating the existing Desktop report is allowed here because the user asked for it.",
)

case(
    "real-restart-crashed-plus-investigate",
    ["mixed", "real", "compound"],
    "sep22-afternoon",
    "I'm interrupting you to say basically that I want the agents that crashed to restart. Now, I want you to start a new super to investigate why the crash happened.",
    ex(
        "mixed",
        actions=[
            {
                "existing_thread": "super-agents-state-accuracy",
                "mechanism": "start_turn_on_idle",
            },
            {
                "existing_thread": "speech-recognition-trouble-diagnosis",
                "mechanism": "start_turn_on_idle",
            },
            {"new_thread": 1, "must_include": ["crash"]},
        ],
        must_not=["more_than_one_new_thread", "restart_non_failed_threads"],
    ),
    history=[
        [
            "... are any of them failed? Only the ones from today.",
            "Of today's agents only two failed - Morgan and Jolene - both from the same process-exit crash, not their own doing.",
        ]
    ],
    observed="start_turn on Morgan and Jolene with resume instructions; start executor-shutdown-crash-investigation (Thandi).",
)

case(
    "real-yes-please-verify",
    ["answer-only", "real", "verify"],
    "sep22-evening",
    "Yes, please verify.",
    ex(
        "answer_only",
        target="speech-recognition-trouble-diagnosis",
        must_not=["new_thread", "existing_thread"],
    ),
    history=[
        [
            "Do the next one.",
            "Jolene - the speech-recognition alarm fix. Status says completed and she claims a commit on develop. Want me to verify the commit is real?",
        ]
    ],
    observed="Grepped the log and the develop branch; confirmed the commit exists.",
)

case(
    "real-ios-steer-render-existing-owner",
    ["existing-thread", "real", "owner-resolution"],
    "sep22-evening",
    "There's an iOS glitch currently where when I'm on a voice call and I steer by just adding, like, whether it's unmuting and adding more, or maybe it's something else. Whenever I'm doing that steer, it doesn't render on the iOS app. I can't, I can only see my initial message in the subsequent steers. So if you want to have. I think, Joey was working on that. So if you want to have. You want to have, I think, Joey was working on that. But like an existing iOS agent could take that one as well.",
    ex(
        "existing_thread",
        target="ios-dispatcher-view-glitches",
        mechanism="start_turn_on_idle",
        must_include=["steer"],
        must_not=["new_thread"],
    ),
    observed="start_turn on ios-dispatcher-view-glitches (Joey) with the steer-rendering bug; a second start_turn added push authorization.",
)

case(
    "real-jitter-buffer",
    ["new-thread", "real", "compound"],
    "sep22-evening",
    "Start as new super agent investigate the question of whether we have Jitter buffer. For when the iOS app is reading packets. Because that last response I didn't fully understand all of it. I understood most of it. But one phrase of one sentence was, I think there's loss. Like. And so I'm not sure if life could support this or. But it seems like when the network had even had, like, people within it. That the iOS or that the, the user risked losing all of or losing or not hearing part of the message. So investigate that in a new super agent and then tell me the, the next one.",
    ex(
        "new_thread",
        new_threads=1,
        must_include=["jitter"],
        must_not=["more_than_one_new_thread"],
    ),
    observed="start ios-jitter-buffer-investigation (Conor); also start_turn on Skylar to implement a previously offered fix.",
)

case(
    "real-look-at-casper",
    ["answer-only", "real", "verify"],
    "sep22-evening",
    "And then look at Casper.",
    ex(
        "answer_only",
        target="voice-transcript-diagnostics",
        must_not=["new_thread", "existing_thread"],
    ),
    history=[
        [
            "... investigate that in a new super agent and then tell me the next one.",
            "Conor is on the jitter-buffer question. Next on the list is Casper, the voice-transcript diagnostics agent.",
        ]
    ],
    observed="Read Casper's log; reported a real false-completion with no report on disk.",
)

case(
    "real-did-you-launch-others",
    ["answer-only", "real", "question-not-instruction"],
    "sep22-evening",
    "Okay, but it wasn't just the shut up bug I told you to investigate. Did you also launch other super agents or the other things I said that were before, like I told you, to investigate the CASPer thing? Did you do that?",
    ex("answer_only", must_not=["new_thread", "existing_thread"]),
    observed="Answered honestly: only Henry was launched; Casper was not resumed because no yes had been given.",
    note="A question about what was launched is not authorization to launch anything.",
)

case(
    "real-plain-english",
    ["answer-only", "real", "easy"],
    "sep22-casper",
    "I mean that last one you said that was I didn't understand a word of what you said but it is in simple English what that skill does",
    ex("answer_only", must_not=["new_thread", "existing_thread"]),
    observed="Explained the brand-kit-uplevel skill in plain English.",
)

case(
    "real-garbled-fragment",
    ["clarify", "real", "garbled"],
    "sep22-afternoon",
    "Homies. You want to back? There? Like this.",
    ex(
        "clarify",
        must_not=["new_thread", "existing_thread"],
        accept_also=["answer_only"],
    ),
    observed="No tools; brief reply.",
)

# ---- Scripted voice-test scenarios (source: scenario) ----------------------

case(
    "scn-start-two-desktop",
    ["new-thread", "scenario", "multi-target"],
    "desktop-fresh",
    "Hey, can you start two Super Agents for me? One in the maple folder on my Desktop, and one in cedar. Have them follow the briefing files and build those apps.",
    ex(
        "new_thread",
        new_threads=2,
        must_include=["briefing.md"],
        must_not=["recursive_home_search"],
        cwd=["~/Desktop/maple", "~/Desktop/cedar"],
    ),
)

case(
    "scn-maple-heading-correction",
    ["existing-thread", "scenario", "correction"],
    "maple-cedar-running",
    "For the Tetris app in maple, change the heading to Silver Fox. Actually, sorry, make that Copper Meadow. Keep everything else the same and run the build.",
    ex(
        "existing_thread",
        target="maple",
        must_include=["Copper Meadow", "build"],
        must_not_include=["Silver Fox"],
        must_not=["new_thread"],
    ),
)

case(
    "scn-cedar-heading-background",
    ["existing-thread", "scenario", "easy"],
    "maple-cedar-running",
    "And for the chess app in cedar, make the heading Blue Harbor. Oh, and use a dark blue background. Please build it when you are done.",
    ex(
        "existing_thread",
        target="cedar",
        must_include=["Blue Harbor", "dark blue"],
        must_not=["new_thread"],
    ),
)

case(
    "scn-how-did-they-turn-out",
    ["answer-only", "scenario", "verify"],
    "maple-cedar-done",
    "How did those two apps turn out? Check the actual headings and whether the builds passed. I just want a quick update, no need to deploy anything.",
    ex("answer_only", must_not=["new_thread", "existing_thread", "deploy"]),
)

case(
    "scn-choose-heading-ask-first",
    ["clarify", "scenario", "clarification-state"],
    "maple-cedar-running",
    "For maple, I am choosing between the headings Amber Kite and Silver Fox. Ask me to confirm which one before you change anything.",
    ex(
        "clarify",
        must_not=["new_thread", "existing_thread"],
        spoken_must_include=["Amber Kite", "Silver Fox"],
    ),
)

case(
    "scn-verify-cedar-while-maple-pending",
    ["answer-only", "scenario", "clarification-state"],
    "maple-cedar-running",
    "Verify only the cedar app. Read its actual current heading and saved build result. Tell me the actual result aloud, ending with copper meadow complete.",
    ex(
        "answer_only",
        target="cedar",
        must_not=[
            "new_thread",
            "existing_thread",
            "steer_maple_with_unconfirmed_heading",
        ],
        spoken_must_end_with="copper meadow complete",
    ),
    history=[
        [
            "For maple, I am choosing between the headings Amber Kite and Silver Fox. Ask me to confirm which one before you change anything.",
            "Sure - which heading do you want for maple, Amber Kite or Silver Fox? I won't change anything until you confirm.",
        ]
    ],
    note="The maple clarification is still open. An unrelated instruction must not resolve it.",
)

case(
    "scn-confirm-silver-fox",
    ["existing-thread", "scenario", "clarification-state"],
    "maple-cedar-running",
    "Confirm the maple heading should be Silver Fox. Steer its existing agent to make that change, run the production build, save the actual output and exit code, and announce the result ending with copper meadow complete.",
    ex(
        "existing_thread",
        target="maple",
        must_include=["Silver Fox", "copper meadow complete", "exit code"],
        must_not=["new_thread"],
    ),
    history=[
        [
            "For maple, I am choosing between Amber Kite and Silver Fox. Ask me to confirm first.",
            "Which one do you want, Amber Kite or Silver Fox?",
        ],
        [
            "Verify only the cedar app ... ending with copper meadow complete.",
            "Cedar's heading is Blue Harbor and its last production build exited 0. Copper meadow complete.",
        ],
    ],
)

case(
    "scn-steer-maple-silver-fox",
    ["existing-thread", "scenario", "easy"],
    "maple-cedar-running",
    "Steer the Tetris agent in maple. Change its heading to Silver Fox. Run the production build after that change, save its actual output and exit code, and announce the result ending with copper meadow complete.",
    ex(
        "existing_thread",
        target="maple",
        must_include=["Silver Fox", "copper meadow complete"],
        must_not=["new_thread"],
    ),
)

case(
    "scn-verify-cedar-spelled",
    ["answer-only", "scenario", "verify"],
    "maple-cedar-done",
    "Verify the existing task in the cedar folder on the Desktop. Cedar is spelled C, E, D, A, R. Tell me its current visible heading and latest production build result. End with copper meadow complete.",
    ex(
        "answer_only",
        target="cedar",
        must_not=["new_thread", "existing_thread"],
        spoken_must_end_with="copper meadow complete",
    ),
)

case(
    "scn-transfer-tetris",
    ["transfer", "scenario", "easy"],
    "maple-cedar-running",
    "Transfer me to the Tetris agent in the maple folder.",
    ex("transfer", target="maple", must_not=["new_thread", "ask_for_confirmation"]),
)

case(
    "scn-put-me-through",
    ["transfer", "scenario", "phrasing"],
    "maple-cedar-running",
    "Can you put me through to the agent working on the Tetris app in Maple?",
    ex("transfer", target="maple", must_not=["new_thread", "ask_for_confirmation"]),
)

case(
    "scn-both-agents-update",
    ["answer-only", "scenario", "verify"],
    "maple-cedar-done",
    "Can you check with both agents and give me one short update on what changed and whether the builds passed?",
    ex("answer_only", must_not=["new_thread"]),
)

case(
    "scn-start-elm",
    ["new-thread", "scenario", "easy"],
    "maple-cedar-running",
    "Start a Super Agent named elm in the elm folder on the Desktop. Have it read the briefing file and perform the controlled stalled command test described there.",
    ex(
        "new_thread", new_threads=1, cwd=["~/Desktop/elm"], must_include=["briefing.md"]
    ),
)

case(
    "scn-steer-elm-interrupt",
    ["existing-thread", "scenario", "interrupt"],
    "maple-cedar-running-elm-running",
    "Steer the elm agent. Stop its waiting command now. Inside elm, write the correction result file specified in its briefing containing meadow sunrise. Announce that the file is written, ending with copper meadow complete.",
    ex(
        "existing_thread",
        target="elm",
        mechanism="steer_interrupt",
        must_include=["meadow sunrise", "copper meadow complete"],
        must_not=["new_thread"],
    ),
)

case(
    "scn-smoke-how-can-you-help",
    ["answer-only", "scenario", "easy"],
    "empty",
    "Hey, before we start coding, can you tell me briefly how you can help me work with two coding agents at once?",
    ex("answer_only", must_not=["new_thread"]),
)

case(
    "scn-walk-me-through",
    ["answer-only", "scenario", "easy"],
    "maple-cedar-running",
    "Could you walk me through how you coordinate multiple coding agents, in about two hundred words? Include an example of changing a requirement while both agents are still working.",
    ex("answer_only", must_not=["new_thread", "existing_thread"]),
)

case(
    "scn-which-two-apps",
    ["answer-only", "scenario", "status"],
    "maple-cedar-running",
    "Thanks. Which two apps are we working on right now?",
    ex(
        "answer_only",
        must_not=["new_thread", "existing_thread"],
        spoken_must_include=["Tetris", "chess"],
    ),
)

case(
    "scn-turtle-balloon-story",
    ["answer-only", "scenario", "correction"],
    "empty",
    "Tell me a short story about a turtle. Actually, make it about a balloon instead. End the answer with copper meadow complete, and read the whole answer aloud.",
    ex(
        "answer_only",
        must_not=["new_thread"],
        spoken_must_include=["balloon"],
        spoken_must_end_with="copper meadow complete",
    ),
    note="Trivial non-coding request: the dispatcher answers directly instead of delegating.",
)

case(
    "scn-amber-kite-spelled",
    ["existing-thread", "scenario", "spelling"],
    "maple-cedar-running",
    "Amber Kite, spelled K-I-T-E.",
    ex(
        "existing_thread",
        target="maple",
        must_include=["Amber Kite"],
        must_not_include=["KITE"],
        must_not=["new_thread"],
    ),
    history=[
        [
            "For maple, change the heading to Amber Kite and run the build.",
            "Just to be sure I heard the heading right - Amber Kite, K-I-T-E, or Amber Night?",
        ]
    ],
    note="Spelling resolves one word; the other unambiguous word is retained.",
)

# ---- Memo-derived and synthesized hard cases (source: memo / synth) --------

case(
    "memo-steer-new-evidence",
    ["existing-thread", "memo", "evidence-routing"],
    "sep22-casper",
    "By the way, I just noticed the drop only happens when I'm on cellular, and the iOS log shows a reconnect at 2:08. Not sure if that helps.",
    ex(
        "existing_thread",
        target="voice-transcript-diagnostics",
        must_include=["cellular"],
        must_not=["new_thread", "summarize_without_steering"],
    ),
    note="2026-09-15 incident: the user had to ask three times before evidence reached the active agent. Steer immediately and confirm it was routed.",
)

case(
    "memo-commentary-no-steer",
    ["answer-only", "memo", "commentary-exception"],
    "sep22-casper",
    "Just noting this for the conversation, don't do anything: the drop happened again at 2:15, on cellular again.",
    ex("answer_only", must_not=["new_thread", "existing_thread"]),
    note="Explicit commentary marker: no steer, no action.",
)

case(
    "synth-collision-same-repo",
    ["clarify", "synth", "collision"],
    "sep22-casper-dorothy",
    "Start another super agent in the openbase CLI repo to refactor the report command's database layer.",
    ex(
        "clarify",
        must_not=["new_thread"],
        spoken_must_include=["Dorothy"],
        accept_also=["existing_thread"],
    ),
    note="Dorothy is actively editing the same command. Gold: push back on the collision, suggest reusing her, sequencing, or a separate worktree.",
)

case(
    "synth-two-agents-separate-worktrees",
    ["new-thread", "synth", "collision"],
    "empty",
    "Start two super agents on the openbase CLI repo, one for the report command and one for the loops console page, each in its own worktree so they don't step on each other.",
    ex("new_thread", new_threads=2, must_include=["worktree"]),
    note="Collision risk already mitigated by the user; no pushback needed.",
)

case(
    "synth-stale-owner-replace",
    ["cancel-replace", "synth", "stale-owner"],
    "sep22-casper",
    "Casper seems dead, nothing has happened for ages. Start a fresh agent on the voice-transcript diagnostics and let Casper go.",
    ex(
        "cancel_replace",
        target="voice-transcript-diagnostics",
        new_threads=1,
        must_not=["two_live_owners_for_same_task"],
    ),
    note="Explicit replacement request. The dispatcher should still inspect Casper's state before cancelling, then start exactly one replacement.",
)

case(
    "synth-stale-owner-check-first",
    ["answer-only", "synth", "stale-owner"],
    "maple-stale-cedar-running",
    "Is Carl still working or did he get stuck? Nothing has come back for a while.",
    ex(
        "answer_only",
        target="maple",
        must_not=["new_thread", "cancel", "existing_thread"],
    ),
    note="Status question only. Missing or stale status is UNKNOWN, not proof of death; no restart without being asked.",
)

case(
    "synth-duplicate-delivery",
    ["existing-thread", "synth", "single-delivery"],
    "sep22-midday",
    "And tell him it was in the thread with Andy that ends with the Glasswing introduction.",
    ex(
        "existing_thread",
        target="andy-palmer-intro-email-reply",
        must_include=["Glasswing"],
        must_not_include=["MIT"],
        must_not=["new_thread"],
    ),
    history=[
        [
            "Tell Oliver to also check my MIT inbox for the Andy Palmer email.",
            "Done - I told Oliver to also search your MIT inbox.",
        ]
    ],
    note="Only the new clarification goes to Oliver; the MIT instruction was already delivered.",
)

case(
    "synth-seeder-is-cedar",
    ["existing-thread", "synth", "target-resolution"],
    "maple-cedar-running",
    "Steer the chess app in seeder to use a dark blue background.",
    ex(
        "existing_thread",
        target="cedar",
        must_include=["dark blue"],
        must_not=["new_thread", "clarify"],
    ),
    note="'seeder' is a transcription of cedar; exactly one chess app exists.",
)

case(
    "synth-chess-app-and-seeder",
    ["existing-thread", "synth", "target-resolution"],
    "maple-cedar-running",
    "Have the chess app and seeder add a move counter under the board.",
    ex(
        "existing_thread",
        target="cedar",
        must_include=["move counter"],
        must_not=["new_thread", "two_targets"],
    ),
    note="One target, not two: 'the chess app and seeder' is a transcription variation of the cedar chess app.",
)

case(
    "synth-busy-owner-follow-up",
    ["existing-thread", "synth", "steer-vs-new"],
    "maple-cedar-running",
    "Also for maple, after the build add a high-score table saved to local storage.",
    ex(
        "existing_thread",
        target="maple",
        mechanism="queue_or_steer",
        must_include=["high-score"],
        must_not=["new_thread"],
    ),
    note="Distinct new work for a busy owner: queue a follow-up turn (or steer); never a second maple agent.",
)

case(
    "synth-new-project-elm",
    ["new-thread", "synth", "steer-vs-new"],
    "maple-cedar-running",
    "Start a new agent in the elm folder on my Desktop to build a pomodoro timer web app.",
    ex("new_thread", new_threads=1, cwd=["~/Desktop/elm"], must_include=["pomodoro"]),
)

case(
    "synth-folder-missing",
    ["clarify", "synth", "folder-resolution"],
    "desktop-fresh",
    "Start a super agent in the birch folder on my Desktop to build a todo app from the briefing there.",
    ex("clarify", must_not=["new_thread", "recursive_home_search"]),
    note="No birch folder exists. A bounded Desktop listing comes back without it; ask, do not search the whole home or guess.",
)

case(
    "synth-plan-question-relay",
    ["answer-only", "synth", "plan-mode"],
    "morgan-plan-question",
    "What was Morgan asking me?",
    ex(
        "answer_only",
        target="super-agents-state-accuracy",
        must_not=["answer_plan_question_on_users_behalf", "new_thread"],
        spoken_must_include=["Codex"],
    ),
)

case(
    "synth-plan-answer-deliver",
    ["existing-thread", "synth", "plan-mode"],
    "morgan-plan-question",
    "Tell Morgan yes, both backends.",
    ex(
        "existing_thread",
        target="super-agents-state-accuracy",
        must_include=["both"],
        must_not=["new_thread"],
    ),
    history=[
        [
            "What was Morgan asking me?",
            "Morgan wants to know whether the completion fix should also cover Codex app-server threads or Claude Code only.",
        ]
    ],
)

case(
    "synth-always-remember",
    ["answer-only", "synth", "memory"],
    "empty",
    "Always remember: when I say ship it, that means push to develop, never to main.",
    ex(
        "answer_only",
        must_not=["new_thread", "existing_thread"],
        must_include=[
            "DISPATCHER_INSTRUCTIONS.md|VOICE_INSTRUCTIONS.md|CLAUDE.md|AGENTS.md"
        ],
    ),
    note="Persistent-memory request: edit one of the instruction files; nothing is dispatched.",
)

case(
    "synth-no-model-override",
    ["new-thread", "synth", "defaults"],
    "empty",
    "Start a super agent to add dark mode to the console.",
    ex(
        "new_thread",
        new_threads=1,
        must_include=["dark mode"],
        must_not=["model_override", "effort_override", "service_tier_override"],
    ),
)

case(
    "synth-fable-explicit",
    ["new-thread", "synth", "model-override"],
    "empty",
    "Start a Fable super agent to audit the netmesh helper for memory leaks.",
    ex("new_thread", new_threads=1, model="fable", must_include=["netmesh"]),
)

case(
    "synth-transfer-by-name",
    ["transfer", "synth", "easy"],
    "maple-cedar-running",
    "Put me through to Dottie.",
    ex("transfer", target="cedar", must_not=["new_thread", "ask_for_confirmation"]),
)

case(
    "synth-cancel-carl",
    ["cancel", "synth", "easy"],
    "maple-cedar-running",
    "Cancel Carl, we're not doing the Tetris app anymore.",
    ex("cancel", target="maple", must_not=["new_thread", "cancel_other_threads"]),
)

case(
    "synth-do-it-yourself",
    ["existing-thread", "synth", "router-not-worker"],
    "maple-cedar-running",
    "Can you just edit the README in maple yourself and add a line about how to run it?",
    ex(
        "existing_thread",
        target="maple",
        must_include=["README"],
        must_not=["dispatcher_edits_files", "new_thread"],
        accept_also=["clarify"],
    ),
    note="The dispatcher is a router: route the edit to Carl (or confirm), never edit project files itself.",
)

case(
    "synth-stop-build-interrupt",
    ["existing-thread", "synth", "interrupt"],
    "maple-stale-cedar-running",
    "Stop Carl's build right now, it's going to overwrite the wrong file. Tell him to check the old version back out first.",
    ex(
        "existing_thread",
        target="maple",
        mechanism="steer_interrupt",
        must_include=["old version"],
        must_not=["new_thread", "cancel"],
    ),
)

case(
    "synth-next-in-walkthrough",
    ["answer-only", "synth", "status"],
    "sep22-afternoon",
    "Next.",
    ex("answer_only", must_not=["new_thread", "existing_thread"]),
    history=[
        [
            "Tell me what each super agent is doing one by one. What's the first one?",
            "First: Casper - voice-transcript diagnostics. Status says completed, but his log ends mid-investigation with no report on disk.",
        ]
    ],
)

case(
    "synth-restart-only-failed",
    ["existing-thread", "synth", "owner-resolution"],
    "sep22-afternoon",
    "Restart whichever agents crashed today.",
    ex(
        "mixed",
        actions=[
            {
                "existing_thread": "super-agents-state-accuracy",
                "mechanism": "start_turn_on_idle",
            },
            {
                "existing_thread": "speech-recognition-trouble-diagnosis",
                "mechanism": "start_turn_on_idle",
            },
        ],
        must_not=["new_thread", "restart_non_failed_threads"],
    ),
    note="Only Morgan and Jolene are in failed state; completed threads must be left alone.",
)

case(
    "synth-report-request",
    ["answer-only", "synth", "reports"],
    "sep22-afternoon",
    "Write me a report of what every agent did today and where their deliverables are.",
    ex("answer_only", must_not=["new_thread"]),
    note="Reports are written with the reports CLI by the dispatcher itself; this is routing/status work, not delegated coding.",
)


# --------------------------------------------------------------------------
# Emit
# --------------------------------------------------------------------------


def main() -> None:
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case id"
    (HERE / "cases.json").write_text(
        json.dumps(CASES, indent=1, ensure_ascii=False) + "\n"
    )
    (HERE / "fleets.json").write_text(
        json.dumps(FLEETS, indent=1, ensure_ascii=False) + "\n"
    )
    (HERE / "cases-review.html").write_text(render_review())
    from collections import Counter

    print(len(CASES), "cases")
    print("by category:", dict(Counter(c["tags"][0] for c in CASES)))
    print("by source:  ", dict(Counter(c["tags"][1] for c in CASES)))
    print("by fleet:   ", dict(Counter(c["fleet"] for c in CASES)))


def render_review() -> str:
    from collections import Counter

    e = html.escape
    cats = Counter(c["tags"][0] for c in CASES)
    srcs = Counter(c["tags"][1] for c in CASES)
    rows = []
    for i, c in enumerate(CASES, 1):
        fleet = FLEETS[c["fleet"]]
        threads = (
            "".join(
                f"<li><code>{e(t['name'])}</code> ({e(t['agent_name'])}, {e(t['status'])}, {e(t['model'])}, {t['last_activity_minutes_ago']} min ago)"
                + (
                    f" - <i>{e(t['last_useful_message'])}</i>"
                    if t.get("last_useful_message")
                    else ""
                )
                + "</li>"
                for t in fleet["threads"]
            )
            or "<li><i>no registered Super Agents</i></li>"
        )
        folders = (
            ", ".join(
                f"~/Desktop/{k}/ [{', '.join(v)}]" for k, v in fleet["folders"].items()
            )
            or "none"
        )
        hist = (
            "".join(
                f"<div class=h><b>User:</b> {e(u)}<br><b>Dispatcher:</b> {e(a)}</div>"
                for u, a in c["history"]
            )
            or "<i>none (fresh turn)</i>"
        )
        exp = json.dumps(c["expected"], indent=1, ensure_ascii=False)
        obs = (
            f"<p class=obs><b>What the real dispatcher did (reference, not gold):</b> {e(c['observed'])}</p>"
            if c.get("observed")
            else ""
        )
        note = f"<p class=note><b>Note:</b> {e(c['note'])}</p>" if c.get("note") else ""
        rows.append(f"""
<section id="{e(c["id"])}">
<h2>{i}. <code>{e(c["id"])}</code> <span class=tags>{" ".join(f"<span class=tag>{e(t)}</span>" for t in c["tags"])}</span></h2>
<div class=grid>
<div><h3>Fleet: <code>{e(c["fleet"])}</code></h3><ul>{threads}</ul><p><b>Desktop folders:</b> {e(folders)}</p></div>
<div><h3>Conversation so far</h3>{hist}</div>
</div>
<h3>Utterance under test</h3><blockquote>{e(c["utterance"])}</blockquote>
<h3>Expected routing (gold, proposed)</h3><pre>{e(exp)}</pre>
{obs}{note}
</section>""")
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Dispatcher routing eval - case review</title>
<style>
body{{font:15px/1.45 -apple-system,system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}}
section{{border-top:2px solid #ddd;padding:18px 0}} h2{{font-size:17px;margin:0 0 8px}} h3{{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:#666;margin:12px 0 4px}}
blockquote{{background:#f6f7f9;border-left:4px solid #5b8def;margin:0;padding:10px 14px;font-size:16px}}
pre{{background:#f3f3f3;padding:10px;overflow:auto;font-size:12.5px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.tag{{display:inline-block;background:#eef;border-radius:10px;padding:1px 8px;font-size:12px;margin-left:4px}} .h{{background:#fafafa;border:1px solid #eee;padding:6px 8px;margin:4px 0;font-size:13.5px}}
.obs{{color:#555;font-size:13.5px}} .note{{color:#8a4b00;font-size:13.5px}} ul{{margin:4px 0;padding-left:18px}} li{{font-size:13.5px}}
.summary{{background:#f6f7f9;padding:12px 16px;border-radius:8px}}
</style></head><body>
<h1>Dispatcher routing eval - proposed inputs</h1>
<div class=summary>
<p><b>{len(CASES)} cases.</b> By category: {", ".join(f"{k} {v}" for k, v in cats.items())}.<br>By source: {", ".join(f"{k} {v}" for k, v in srcs.items())}.</p>
<p>Each case is one voice turn into the dispatcher. The <b>fleet</b> is the registered-Super-Agent snapshot the dispatcher sees (and what the stubbed MCP tools will report); <b>conversation so far</b> is injected as the session prefix; the <b>expected routing</b> is the proposed gold label you are asked to confirm or correct. Dispositions: new_thread, existing_thread, cancel_replace, transfer, cancel, answer_only, clarify, mixed.</p>
<p>Review question: are these representative of what the dispatcher actually sees? Any obvious missing cases, cases that do not matter, or gold labels you would score differently?</p>
</div>
{"".join(rows)}
</body></html>"""


if __name__ == "__main__":
    main()
