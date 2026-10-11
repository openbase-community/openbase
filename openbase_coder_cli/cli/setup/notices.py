"""The ℹ️ notice lines setup prints for the developer."""

# The ℹ️ emoji renders two columns wide in most terminals, so a single space
# lets the text butt against it.
INFO_PREFIX = "ℹ️  "


def info_notice(text: str) -> str:
    return INFO_PREFIX + text
