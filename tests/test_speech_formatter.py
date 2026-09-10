from __future__ import annotations

from openbase_coder_cli.livekit_agent.speech_formatter import (
    SpeechFormatOptions,
    format_for_speech,
    format_for_speech_segments,
)


def test_formatter_preserves_list_structure_before_cleanup():
    text = """Fixed:
1. Preserved newlines before parsing.
2. Added API and TTS replacements.
3. Skipped code blocks."""

    assert (
        format_for_speech(text)
        == "Fixed. One: Preserved newlines before parsing. Two: Added A P I and T T S replacements. Three: Skipped code blocks."
    )


def test_formatter_suppresses_code_blocks_and_humanizes_identifiers():
    text = """## Next steps

- Update README.md
- Run `uv run pytest`
- Keep `formatForSpeech()` private for now

```ts
console.log("do not speak this");
```"""

    assert format_for_speech_segments(text) == [
        "Section: Next steps.",
        "Update read me dot M D.",
        "Run U V run pytest.",
        "Keep format for speech private for now.",
        "Omitted.",
    ]
    assert "console.log" not in format_for_speech(text)


def test_formatter_speaks_unordered_lists_without_repeated_item_prefixes():
    text = """Summary:
- Fixed the TTS list handling.
- Updated the Super Agent voice instructions.
  - Avoid nested bullet narration too."""

    assert (
        format_for_speech(text)
        == "Summary. Fixed the T T S list handling. Updated the Super Agent voice instructions. Avoid nested bullet narration too."
    )


def test_formatter_preserves_numbered_list_markers_for_speech():
    text = """Next:
1) Keep numbered lists numbered.
2. Do not turn them into generic items."""

    assert (
        format_for_speech(text)
        == "Next. One: Keep numbered lists numbered. Two: Do not turn them into generic items."
    )


def test_formatter_reduces_omitted_code_narration_to_one_word():
    text = """I checked the output.

Code block omitted, shown on screen. It is Python, one line.

bash code omitted

bash one line

Done."""

    assert format_for_speech(text) == "I checked the output. Omitted. Done."


def test_formatter_splits_paths_filenames_and_identifier_styles():
    text = (
        "Changed src/utils/parseMarkdown.ts, OPENAI_API_KEY, "
        "snake_case, kebab-case, JSON, CLI, and STT."
    )

    assert (
        format_for_speech(text)
        == "Changed parse markdown dot T S in src slash utils, openai A P I key, snake case, kebab case, J S O N, C L I, and S T T."
    )


def test_formatter_omits_commit_hashes_from_spoken_text():
    text = (
        "Cloud develop is at `051a9c2`, while staging is at 89351a3. "
        "The word defaced remains ordinary prose, and `deadbee` is an "
        "all-letter commit reference."
    )

    assert format_for_speech(text) == (
        "Cloud develop is at commit reference omitted, while staging is at "
        "commit reference omitted. The word defaced remains ordinary prose, "
        "and commit reference omitted is an all letter commit reference."
    )


def test_formatter_pronounces_saas_and_sas_as_words():
    text = "SaaS billing, SAS analytics, SAAS metrics, and SaaSConfig.ts changed."

    assert (
        format_for_speech(text)
        == "sass billing, sass analytics, sass metrics, and sass config dot T S changed."
    )


def test_formatter_keeps_existing_acronyms_spelled_as_letters():
    text = "API, CLI, TTS, STT, and LLM remain explicit acronyms."

    assert (
        format_for_speech(text)
        == "A P I, C L I, T T S, S T T, and L L M remain explicit acronyms."
    )


def test_formatter_spells_aws_acronym_in_any_case():
    text = "AWS and aws credentials should both be spoken as letters."

    assert (
        format_for_speech(text)
        == "A W S and A W S credentials should both be spoken as letters."
    )


def test_formatter_reloads_user_tts_replacements_without_restart(monkeypatch, tmp_path):
    replacements_path = tmp_path / "tts-replacements.json"
    monkeypatch.setenv("OPENBASE_CODER_TTS_REPLACEMENTS_PATH", str(replacements_path))

    assert format_for_speech("Say foobarbaz clearly.") == "Say foobarbaz clearly."

    replacements_path.write_text(
        '{"replacements": {"foobarbaz": "foo bar baz"}}',
        encoding="utf-8",
    )
    assert format_for_speech("Say foobarbaz clearly.") == "Say foo bar baz clearly."

    replacements_path.write_text(
        '{"replacements": {"foobarbaz": "F O O bar baz"}}',
        encoding="utf-8",
    )
    assert format_for_speech("Say foobarbaz clearly.") == "Say F O O bar baz clearly."


def test_formatter_omits_stack_traces_and_log_like_output():
    text = """Traceback (most recent call last):
  File "app.py", line 2, in <module>
Exception: nope"""

    assert format_for_speech(text) == "Technical output omitted, shown on screen."


def test_formatter_speaks_plain_short_voice_status_replies():
    assert format_for_speech("Screen share is on.") == "Screen share is on."
    assert format_for_speech("Screen share is off.") == "Screen share is off."


def test_formatter_speaks_plain_explanations_about_technical_terms():
    text = (
        "The approval popup appears when a tool call needs permission, "
        "so you can approve it."
    )

    assert (
        format_for_speech(text)
        == "The approval popup appears when a tool call needs permission, so you can approve it."
    )


def test_formatter_speaks_plain_explanations_with_internal_marker_names():
    text = (
        "The approval popup is probably caused by sandbox_permissions "
        "on an exec_command call."
    )

    assert (
        format_for_speech(text)
        == "The approval popup is probably caused by sandbox permissions on an exec command call."
    )


def test_formatter_speaks_plain_reply_about_technical_output_bug():
    text = (
        "I think the bug is that a normal response mentioning a tool call "
        "is being treated as technical output."
    )

    assert (
        format_for_speech(text)
        == "I think the bug is that a normal response mentioning a tool call is being treated as technical output."
    )


def test_formatter_still_omits_structured_tool_and_json_output():
    assert (
        format_for_speech('tool call: {"name": "exec_command", "arguments": {}}')
        == "Technical output omitted, shown on screen."
    )
    assert format_for_speech('{"status": "ok", "items": []}') == (
        "Technical output omitted, shown on screen."
    )


def test_formatter_omits_tool_failure_lines_before_safe_prose():
    text = """setsummary failed because the command was unavailable.

The current working directory is /Users/example."""

    assert (
        format_for_speech(text)
        == "The current working directory is example in users. Technical output omitted, shown on screen."
    )


def test_formatter_truncates_after_speech_formatting():
    text = "- First API item.\n- Second TTS item.\n- Third STT item."

    assert (
        format_for_speech(text, SpeechFormatOptions(max_chars=48))
        == "First A P I item. Second T T S item."
    )
