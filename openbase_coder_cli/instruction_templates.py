from __future__ import annotations

import re

from openbase_coder_cli.services.console_settings import (
    get_dangerous_confirmation_phrase,
    get_user_address_name,
)

DANGEROUS_CONFIRMATION_PHRASE_TOKEN = "${dangerous_confirmation_phrase}"
USER_ADDRESS_NAME_TOKEN = "${user_address_name}"
# Renders to the cloud-workspace persistence bullet (host_kind) on a cloud
# workspace and to nothing on a user's own computer.
CLOUD_WORKSPACE_NOTE_TOKEN = "${cloud_workspace_note}"
INSTRUCTION_TEMPLATE_TOKENS = (
    DANGEROUS_CONFIRMATION_PHRASE_TOKEN,
    USER_ADDRESS_NAME_TOKEN,
    CLOUD_WORKSPACE_NOTE_TOKEN,
)


def has_instruction_template_tokens(text: str) -> bool:
    return any(token in text for token in INSTRUCTION_TEMPLATE_TOKENS)


def render_instruction_template(text: str) -> str:
    from openbase_coder_cli.host_kind import cloud_workspace_note

    replacements = (
        (DANGEROUS_CONFIRMATION_PHRASE_TOKEN, get_dangerous_confirmation_phrase()),
        (USER_ADDRESS_NAME_TOKEN, get_user_address_name()),
        (CLOUD_WORKSPACE_NOTE_TOKEN, cloud_workspace_note()),
    )
    rendered = text
    for token, value in replacements:
        rendered = rendered.replace(token, value)
    return rendered


def text_matches_instruction_template(text: str, template: str) -> bool:
    if not has_instruction_template_tokens(template):
        return text == template

    pattern = re.escape(template)
    for token in INSTRUCTION_TEMPLATE_TOKENS:
        pattern = pattern.replace(re.escape(token), r".*?")
    return re.fullmatch(pattern, text, flags=re.DOTALL) is not None
