"""Contact categorization for the personal-messaging skills (WhatsApp, iMessage).

The messaging skills archive conversations on the device and gate agent access
behind per-contact approvals. This package builds the person-level view on top:
it lists every contact with recent activity across the configured sources,
merges identities that clearly belong to the same person, keeps the category
the user assigns, and lets the approval queue honor those categories. Names,
numbers and message text stay on the device and are never logged.
"""
