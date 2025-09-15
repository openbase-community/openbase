from __future__ import annotations

from textwrap import dedent


def dedent_strip(text):
    """
    Dedent and strip a multi-line string.
    """
    return dedent(text).strip()


def dedent_strip_format(text, **kwargs):
    """
    Dedent and strip a multi-line string, then format it with the given kwargs.
    """
    return dedent(text).strip().format(**kwargs)
