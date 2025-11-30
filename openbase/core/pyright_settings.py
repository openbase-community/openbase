from __future__ import annotations

pyright_settings = {
    "extraPaths": ["web", "openbase-api", "openbase-react"],
    "reportUnknownVariableType": False,
    "reportMissingTypeStubs": False,
    "reportAny": False,
    "reportUnannotatedClassAttribute": False,  # Django doesn't support this rule
    "reportAttributeAccessIssue": True,
    "reportUnknownMemberType": False,
    "reportExplicitAny": False,
    "reportUnknownArgumentType": False,
    "reportIgnoreCommentWithoutRule": False,
    "reportPrivateImportUsage": True,
    "reportUnknownParameterType": False,
    "reportMissingParameterType": False,
    "reportMissingTypeArgument": False,
    "reportUnknownLambdaType": False,
    "reportImplicitOverride": False,
    "reportIncompatibleVariableOverride": False,  # Django doesn't support this rule
    "reportUnusedCallResult": False,
    "reportIncompatibleMethodOverride": False,  # Django doesn't support this rule? Maybe with some tweaking it could
    "reportUnreachable": False,  # this rule has bugs with Django - 6/25
    "reportImportCycles": False,
    "reportInvalidTypeForm": False,
}
