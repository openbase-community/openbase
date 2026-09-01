from __future__ import annotations

import re
from dataclasses import dataclass

from openbase_coder_cli.livekit_agent.speech_replacements import (
    CODE_BLOCK_OMISSION_SPEECH,
    EXTENSION_SPEECH,
    NUMBER_WORDS,
    TTSReplacements,
    current_tts_replacements,
)

DEFAULT_MAX_CHARS = 1600

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_+.-]*)?.*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(
    r"^(?P<indent>\s*)(?P<marker>(?:[-*+])|(?:\d+[.)]))\s+(?P<body>.*)$"
)
_TASK_RE = re.compile(r"^\[(?P<state>[ xX])\]\s+(?P<body>.*)$")
_ORDERED_RE = re.compile(r"^(\d+)[.)]$")
_URL_RE = re.compile(r"https?://\S+")
_LINK_RE = re.compile(r"!\[[^\]]*]\([^)]+\)|\[([^\]]+)]\([^)]+\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_COMMIT_HASH_RE = re.compile(
    r"`[A-Fa-f0-9]{7,40}`|"
    r"(?<![\w])"
    r"(?=[A-Fa-f0-9]{7,40}(?![\w]))"
    r"(?=[A-Fa-f0-9]*[A-Fa-f])"
    r"(?=[A-Fa-f0-9]*\d)"
    r"[A-Fa-f0-9]{7,40}(?![\w])"
)
_PATH_RE = re.compile(
    r"(?<![\w@])(?:~?/|\.{1,2}/)?(?:[\w@.+-]+/)+[\w@.+-]+|(?<![\w@.-])[\w@+-]+(?:[._-][\w@+-]+)*\.[A-Za-z0-9]{1,8}(?![\w@.-])"
)
_IDENTIFIER_RE = re.compile(
    r"\b(?:[A-Za-z]+[A-Z][A-Za-z0-9]*|[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)+|[A-Z]{2,}(?:_[A-Z0-9]+)+)\b"
)
_README_RE = re.compile(r"\bREADME\b|\breadme\b")
_UV_RE = re.compile(r"\buv\b")
_CODE_LANGUAGE_PATTERN = (
    r"(?:bash|zsh|shell|sh|python|py|swift|typescript|ts|tsx|javascript|js|jsx|"
    r"json|markdown|md|html|css|sql|yaml|yml|toml|rust|rs|go|ruby|rb|java|"
    r"kotlin|kt|c|cpp|c\+\+|csharp|c#)"
)
_LINE_COUNT_PATTERN = (
    r"(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+lines?)"
)
_CODE_OMISSION_NARRATION_RE = re.compile(
    rf"\b(?:code\s+block\s+omitted|{_CODE_LANGUAGE_PATTERN}\s+code\s+omitted)"
    rf"(?:,\s*shown\s+on\s+screen)?"
    rf"(?:\.\s*(?:it\s+is\s+)?{_CODE_LANGUAGE_PATTERN}\s*,?\s*{_LINE_COUNT_PATTERN})?"
    r"[.!?]?",
    re.IGNORECASE,
)
_CODE_LINE_COUNT_NARRATION_RE = re.compile(
    rf"\b(?:it\s+is\s+)?{_CODE_LANGUAGE_PATTERN}\s*,?\s*{_LINE_COUNT_PATTERN}[.!?]?",
    re.IGNORECASE,
)
_TECHNICAL_OUTPUT_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"traceback\b|"
    r"setsummary\b|"
    r"exec_command\b|"
    r"apply_patch\b|"
    r"sandbox_permissions\b|"
    r"tool call\b|"
    r"stack trace\b|"
    r"exception in thread\b"
    r")",
    re.IGNORECASE,
)
_TECHNICAL_OUTPUT_FAILURE_RE = re.compile(
    r"\b(?:failed|unavailable|error|exception|timed out|not found)\b",
    re.IGNORECASE,
)
_STRUCTURED_OUTPUT_RE = re.compile(r"[:={}\[\]<>]|(?:\b\w+_\w+\b)")


@dataclass(frozen=True)
class SpeechFormatOptions:
    max_chars: int = DEFAULT_MAX_CHARS
    summarize_code_blocks: bool = True
    summarize_omitted_output: bool = True


@dataclass
class _ListItem:
    prefix: str
    body: str
    indent: int


def format_for_speech(
    text: str,
    options: SpeechFormatOptions | None = None,
) -> str:
    segments = format_for_speech_segments(text, options=options)
    return _truncate(" ".join(segments), (options or SpeechFormatOptions()).max_chars)


def format_for_speech_segments(
    text: str,
    options: SpeechFormatOptions | None = None,
) -> list[str]:
    opts = options or SpeechFormatOptions()
    normalized = _normalize_preflight(text)
    if not normalized.strip():
        return []

    segments = _parse_markdown_blocks(normalized, opts)
    cleaned = [_finalize_segment(segment) for segment in segments]
    cleaned = [segment for segment in cleaned if segment]
    return _collapse_repeated_omissions(cleaned)


def _normalize_preflight(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ANSI_RE.sub("", normalized)
    normalized = _CONTROL_RE.sub("", normalized)
    return normalized.strip()


def _parse_markdown_blocks(text: str, opts: SpeechFormatOptions) -> list[str]:
    lines = text.splitlines()
    segments: list[str] = []
    paragraph: list[str] = []
    pending_item: _ListItem | None = None
    in_fence = False
    fence_marker = ""
    fence_language = ""
    fence_lines = 0
    omitted_output = False

    def flush_paragraph() -> None:
        nonlocal paragraph, omitted_output
        if not paragraph:
            return
        raw = "\n".join(paragraph).strip()
        paragraph = []
        if not raw:
            return
        if _looks_like_non_speech_output(raw):
            omitted_output = True
            return
        segments.append(_humanize_inline(raw))

    def flush_item() -> None:
        nonlocal pending_item, omitted_output
        if pending_item is None:
            return
        body = pending_item.body.strip()
        item = pending_item
        pending_item = None
        if not body:
            return
        if _looks_like_non_speech_output(body):
            omitted_output = True
            return
        spoken_body = _humanize_inline(body)
        if item.prefix:
            segments.append(f"{item.prefix}: {spoken_body}")
        else:
            segments.append(spoken_body)

    def append_code_summary(language: str, line_count: int) -> None:
        if not opts.summarize_code_blocks:
            return
        segments.append(CODE_BLOCK_OMISSION_SPEECH)

    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE_RE.match(line)
        if in_fence:
            if fence and line.lstrip().startswith(fence_marker[:3]):
                append_code_summary(fence_language, fence_lines)
                in_fence = False
                fence_marker = ""
                fence_language = ""
                fence_lines = 0
            else:
                fence_lines += 1
            index += 1
            continue

        if fence:
            flush_item()
            flush_paragraph()
            in_fence = True
            fence_marker = fence.group(1)
            fence_language = fence.group(2) or ""
            fence_lines = 0
            index += 1
            continue

        if not line.strip():
            flush_item()
            flush_paragraph()
            index += 1
            continue

        if _is_table_start(lines, index):
            flush_item()
            flush_paragraph()
            while index < len(lines) and "|" in lines[index]:
                index += 1
            segments.append("Table omitted, shown on screen.")
            continue

        if _is_indented_code(line):
            flush_item()
            flush_paragraph()
            code_lines = 0
            while index < len(lines) and (
                _is_indented_code(lines[index]) or not lines[index].strip()
            ):
                if lines[index].strip():
                    code_lines += 1
                index += 1
            append_code_summary("", code_lines)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_item()
            flush_paragraph()
            segments.append(f"Section: {_humanize_inline(heading.group(1))}")
            index += 1
            continue

        list_item = _LIST_RE.match(line)
        if list_item:
            flush_item()
            flush_paragraph()
            pending_item = _parse_list_item(list_item)
            index += 1
            continue

        if pending_item is not None and _is_list_continuation(
            line, pending_item.indent
        ):
            pending_item.body = f"{pending_item.body} {line.strip()}"
            index += 1
            continue

        if line.lstrip().startswith(">"):
            flush_item()
            flush_paragraph()
            quote = line.lstrip()[1:].strip()
            if quote:
                segments.append(f"Quote: {_humanize_inline(quote)}")
            index += 1
            continue

        if re.fullmatch(r"\s{0,3}[-*_]{3,}\s*", line):
            flush_item()
            flush_paragraph()
            index += 1
            continue

        flush_item()
        paragraph.append(line.strip())
        index += 1

    if in_fence:
        append_code_summary(fence_language, fence_lines)
    flush_item()
    flush_paragraph()

    if omitted_output and opts.summarize_omitted_output:
        segments.append("Technical output omitted, shown on screen.")

    return segments


def _parse_list_item(match: re.Match[str]) -> _ListItem:
    marker = match.group("marker")
    indent = len(match.group("indent").replace("\t", "    "))
    body = match.group("body").strip()

    task = _TASK_RE.match(body)
    if task:
        prefix = "Done" if task.group("state").lower() == "x" else "To do"
        return _ListItem(prefix=prefix, body=task.group("body"), indent=indent)

    ordered = _ORDERED_RE.match(marker)
    if ordered:
        prefix = _number_word(int(ordered.group(1)))
    else:
        prefix = ""
    return _ListItem(prefix=prefix, body=body, indent=indent)


def _is_list_continuation(line: str, indent: int) -> bool:
    if _LIST_RE.match(line):
        return False
    leading = len(line) - len(line.lstrip(" "))
    return leading > indent and bool(line.strip())


def _is_indented_code(line: str) -> bool:
    return line.startswith("    ") or line.startswith("\t")


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index]
    separator = lines[index + 1]
    return "|" in header and bool(
        re.fullmatch(r"\s*\|?[\s:|-]+\|[\s:|-]+\|?\s*", separator)
    )


def _looks_like_non_speech_output(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if any(_looks_like_technical_marker_line(line) for line in lines):
        return True

    if len(lines) >= 3:
        stack_lines = sum(
            bool(
                re.search(
                    r'^\s*(file ".+", line \d+|at\s+\S+\(|caused by:|[-+]{3}\s|@@\s)',
                    line,
                    re.I,
                )
            )
            for line in lines
        )
        if stack_lines / len(lines) >= 0.35:
            return True

    code_chars = sum(text.count(char) for char in "{}[];=$<>")
    words = max(len(text.split()), 1)
    return code_chars / words > 0.55


def _looks_like_technical_marker_line(line: str) -> bool:
    if not _TECHNICAL_OUTPUT_MARKER_RE.search(line):
        return False
    stripped = line.strip()
    words = len(stripped.split())
    return (
        words <= 4
        or bool(_STRUCTURED_OUTPUT_RE.search(stripped))
        or bool(_TECHNICAL_OUTPUT_FAILURE_RE.search(stripped))
    )


def _humanize_inline(text: str) -> str:
    replacements = current_tts_replacements()
    text = _replace_code_omission_narration(text)
    text = _LINK_RE.sub(lambda match: match.group(1) or "", text)
    text = _COMMIT_HASH_RE.sub("commit reference omitted", text)
    text = _INLINE_CODE_RE.sub(lambda match: match.group(1), text)
    text = _URL_RE.sub("link omitted", text)
    text = _PATH_RE.sub(lambda match: _speak_path(match.group(0)), text)
    text = re.sub(r"(\*\*?|~~)(.+?)\1", r"\2", text)
    text = _README_RE.sub("read me", text)
    text = _UV_RE.sub("U V", text)
    text = _replace_term_pronunciations(text, replacements)
    text = _IDENTIFIER_RE.sub(
        lambda match: _split_identifier(match.group(0), replacements),
        text,
    )
    text = _replace_acronyms(text, replacements)
    text = re.sub(
        r"\b([A-Z]{2,6})\b", lambda match: _spell_letters(match.group(1)), text
    )
    text = re.sub(r"\(\)", "", text)
    text = text.replace("\\", " backslash ")
    return text


def _replace_code_omission_narration(text: str) -> str:
    text = _CODE_OMISSION_NARRATION_RE.sub(CODE_BLOCK_OMISSION_SPEECH, text)

    stripped = text.strip()
    if _CODE_LINE_COUNT_NARRATION_RE.fullmatch(stripped):
        return CODE_BLOCK_OMISSION_SPEECH
    return text


def _split_identifier(
    identifier: str, replacements: TTSReplacements | None = None
) -> str:
    replacements = replacements or current_tts_replacements()
    if identifier in EXTENSION_SPEECH:
        return EXTENSION_SPEECH[identifier]
    identifier = _replace_identifier_term_pronunciations(identifier, replacements)
    value = identifier.replace("_", " ").replace("-", " ")
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = value.lower()
    value = _README_RE.sub("read me", value)
    value = _UV_RE.sub("U V", value)
    value = _replace_acronyms(value, replacements)
    return value


def _speak_path(path: str) -> str:
    trailing = ""
    while path and path[-1] in ".,:;)":
        trailing = path[-1] + trailing
        path = path[:-1]

    normalized = path.strip()
    if not normalized:
        return trailing

    parts = [
        part
        for part in normalized.replace("\\", "/").split("/")
        if part and part != "."
    ]
    if not parts:
        return trailing

    basename = _speak_filename(parts[-1])
    if len(parts) <= 1:
        return f"{basename}{trailing}"

    parents = [_humanize_path_part(part) for part in parts[-3:-1]]
    parent_phrase = " slash ".join(part for part in parents if part)
    if parent_phrase:
        return f"{basename} in {parent_phrase}{trailing}"
    return f"{basename}{trailing}"


def _speak_filename(name: str) -> str:
    if "." not in name or name.startswith("."):
        return _humanize_path_part(name)

    stem, extension = name.rsplit(".", 1)
    extension_speech = EXTENSION_SPEECH.get(extension.lower(), extension.upper())
    return f"{_humanize_path_part(stem)} dot {extension_speech}"


def _humanize_path_part(part: str) -> str:
    replacements = current_tts_replacements()
    value = _README_RE.sub("read me", part)
    value = _UV_RE.sub("U V", value)
    value = _replace_identifier_term_pronunciations(value, replacements)
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = value.lower()
    value = _replace_acronyms(value, replacements)
    return value


def _humanize_language(language: str) -> str:
    normalized = language.strip().lower()
    return {
        "ts": "TypeScript",
        "tsx": "TypeScript T S X",
        "js": "JavaScript",
        "jsx": "JavaScript J S X",
        "py": "Python",
        "python": "Python",
        "sh": "shell",
        "bash": "bash",
        "zsh": "Z shell",
        "json": "JSON",
        "md": "Markdown",
    }.get(normalized, _humanize_inline(language))


def _replace_term_pronunciations(
    text: str,
    replacements: TTSReplacements,
) -> str:
    if not replacements.term_pronunciations:
        return text
    pattern = _term_pronunciation_re(replacements)
    return pattern.sub(
        lambda match: replacements.term_pronunciations[match.group(1)],
        text,
    )


def _replace_identifier_term_pronunciations(
    text: str,
    replacements: TTSReplacements,
) -> str:
    value = text
    for term, pronunciation in replacements.term_pronunciations.items():
        value = value.replace(term, f" {pronunciation} ")
    return value


def _replace_acronyms(text: str, replacements: TTSReplacements) -> str:
    if not replacements.acronyms:
        return text
    pattern = _acronym_re(replacements)
    return pattern.sub(lambda match: _spell_letters(match.group(1)), text)


def _acronym_re(replacements: TTSReplacements) -> re.Pattern[str]:
    return re.compile(
        rf"\b({'|'.join(re.escape(acronym) for acronym in replacements.acronyms)})\b",
        re.IGNORECASE,
    )


def _term_pronunciation_re(replacements: TTSReplacements) -> re.Pattern[str]:
    return re.compile(
        rf"\b({'|'.join(re.escape(term) for term in replacements.term_pronunciations)})\b"
    )


def _line_count_phrase(line_count: int) -> str:
    if line_count == 1:
        return "one line"
    if 1 < line_count <= 10:
        return f"{NUMBER_WORDS[line_count].lower()} lines"
    return f"{line_count} lines"


def _number_word(value: int) -> str:
    return NUMBER_WORDS.get(value, str(value))


def _spell_letters(value: str) -> str:
    return " ".join(value.upper())


def _finalize_segment(segment: str) -> str:
    value = re.sub(r"[ \t]+", " ", segment)
    value = re.sub(r"\s*\n\s*", " ", value)
    value = re.sub(r"\s+([.,:;!?])", r"\1", value)
    value = re.sub(r"([.,:;!?]){2,}", r"\1", value)
    value = value.strip()
    if value.endswith(":"):
        value = value[:-1]
    if value and value[-1] not in ".!?":
        value += "."
    return value


def _collapse_repeated_omissions(segments: list[str]) -> list[str]:
    collapsed: list[str] = []
    previous_omission = False
    for segment in segments:
        omission = segment.startswith(
            (
                CODE_BLOCK_OMISSION_SPEECH,
                "Code block",
                "Technical output omitted",
            )
        )
        if omission and previous_omission:
            continue
        collapsed.append(segment)
        previous_omission = omission
    return collapsed


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    sentences = re.split(r"(?<=[.!?])\s+", text)
    excerpt = ""
    for sentence in sentences:
        next_excerpt = f"{excerpt} {sentence}".strip()
        if len(next_excerpt) > max_chars:
            break
        excerpt = next_excerpt

    if excerpt:
        return excerpt
    return text[:max_chars].rsplit(" ", 1)[0].strip()
