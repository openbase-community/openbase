"""Compatibility exports for CLI API views.

Endpoint implementations live in focused modules by domain. Keep these re-exports
so existing URL imports and tests can continue importing from `.views`.
"""

from __future__ import annotations

from openbase_coder_cli.dispatcher_config import (
    dispatcher_voice,
    set_dispatcher_voice,
    set_stt_provider,
    set_tts_provider_and_dispatcher_voice,
)
from openbase_coder_cli.livekit_announcer import (
    _build_livekit_client,
    _resolve_target_room,
    publish_announcer_audio_file,
    publish_announcer_message,
)
from openbase_coder_cli.openbase_coder_cli_app import agents_md as _agents_md
from openbase_coder_cli.openbase_coder_cli_app import diagnostics as _diagnostics
from openbase_coder_cli.openbase_coder_cli_app import livekit as _livekit
from openbase_coder_cli.openbase_coder_cli_app import skills as _skills
from openbase_coder_cli.openbase_coder_cli_app.approvals import (
    approval_request_detail,
    approval_requests,
    skill_approval_request_consume,  # noqa: F401
    skill_approval_request_detail,  # noqa: F401
    skill_approval_requests,  # noqa: F401
)
from openbase_coder_cli.openbase_coder_cli_app.auth import (
    auth_logout,
    auth_refresh_jwt,
    auth_session,
)
from openbase_coder_cli.openbase_coder_cli_app.backend_settings import (
    claude_auth_settings,
    claude_plugin_settings,  # noqa: F401
    codex_plugin_settings,  # noqa: F401
    coding_backend_settings,
)
from openbase_coder_cli.openbase_coder_cli_app.brain_readiness import brain_readiness
from openbase_coder_cli.openbase_coder_cli_app.diagnostics import (
    devices_list,
    health_check,
)
from openbase_coder_cli.openbase_coder_cli_app.env_settings import env_settings
from openbase_coder_cli.openbase_coder_cli_app.ios_app_control import ios_app_control
from openbase_coder_cli.openbase_coder_cli_app.livekit import (
    apple_music_playback_entitlement,  # noqa: F401
    livekit_room_token,
    livekit_voice_route,
    livekit_voice_route_exit,
    livekit_voice_route_transfer,
)
from openbase_coder_cli.openbase_coder_cli_app.marketplace import (
    marketplace_routines,
    marketplace_skill_install,
    marketplace_skills,
)
from openbase_coder_cli.openbase_coder_cli_app.model_settings import (
    backend_model_settings,  # noqa: F401
)
from openbase_coder_cli.openbase_coder_cli_app.onboarding import (
    onboarding_cloud_state,
    onboarding_status,
)
from openbase_coder_cli.openbase_coder_cli_app.plugins_tools import (
    boilersync_templates,
    bootstrap_run,
    plugin_console_registry,
    plugin_detail,
    plugins_list,
    uv_tool_detail,
    uv_tool_executable_help,
    uv_tools_list,
)
from openbase_coder_cli.openbase_coder_cli_app.projects import (
    git_diff,
    project_status,
    recent_projects,
)
from openbase_coder_cli.openbase_coder_cli_app.reasoning_settings import (
    reasoning_settings,
)
from openbase_coder_cli.openbase_coder_cli_app.reports import (
    all_project_reports,
    global_reports_projects,
    project_reports,
    project_reports_action,
    project_reports_download,
    project_reports_file,
    project_reports_tags,
)
from openbase_coder_cli.openbase_coder_cli_app.routines import (
    routine_detail,
    routines_list,
    routines_run_due,
)
from openbase_coder_cli.openbase_coder_cli_app.service_tier_settings import (
    service_tier_settings,
)
from openbase_coder_cli.openbase_coder_cli_app.services_views import (
    agents_generation_settings,
    dangerous_confirmation_settings,
    keep_awake_settings,
    launchctl_ignored_settings,
    launchctl_service_action,
    launchctl_services_list,
    openbase_restart,
    openbase_service_action,
    openbase_services_list,
    service_status,
    thread_device_sync_conflict_resolve,
    thread_device_sync_conflicts,
    thread_device_sync_status,
    thread_sync_conflicts,
)
from openbase_coder_cli.openbase_coder_cli_app.skills import _home_skills_dir
from openbase_coder_cli.openbase_coder_cli_app.tags import tag_options
from openbase_coder_cli.openbase_coder_cli_app.threads import (
    thread_active_voice,  # noqa: F401
    thread_activity,  # noqa: F401
    thread_detail,
    thread_dispatcher,  # noqa: F401
    thread_favorite,
    thread_interrupt,
    thread_list,
    thread_queue_turn,
    thread_start_turn,
    thread_steer_turn,
    thread_tags,
)
from openbase_coder_cli.paths import (
    CODEX_AGENTS_MD_PATH,
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,
    CODEX_HOME_DIR,
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    DEFAULT_LOG_DIR,
    NORMAL_CODEX_AGENTS_MD_PATH,
    NORMAL_CODEX_HOME_DIR,
    OPENBASE_CLAUDE_CONFIG_DIR,
    OPENBASE_CLAUDE_MD_PATH,
)


def _sync_agents_md_compat_globals() -> None:
    _agents_md.CODEX_AGENTS_MD_PATH = CODEX_AGENTS_MD_PATH
    _agents_md.CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH = (
        CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH
    )
    _agents_md.CODEX_DISPATCHER_INSTRUCTIONS_PATH = CODEX_DISPATCHER_INSTRUCTIONS_PATH
    _agents_md.CODEX_HOME_DIR = CODEX_HOME_DIR
    _agents_md.CODEX_SUPER_AGENT_INSTRUCTIONS_PATH = CODEX_SUPER_AGENT_INSTRUCTIONS_PATH
    _agents_md.NORMAL_CODEX_AGENTS_MD_PATH = NORMAL_CODEX_AGENTS_MD_PATH
    _agents_md.NORMAL_CODEX_HOME_DIR = NORMAL_CODEX_HOME_DIR
    _agents_md.OPENBASE_CLAUDE_CONFIG_DIR = OPENBASE_CLAUDE_CONFIG_DIR
    _agents_md.OPENBASE_CLAUDE_MD_PATH = OPENBASE_CLAUDE_MD_PATH


def _sync_livekit_compat_globals() -> None:
    _livekit.dispatcher_voice = dispatcher_voice
    _livekit.set_dispatcher_voice = set_dispatcher_voice
    _livekit.set_tts_provider_and_dispatcher_voice = (
        set_tts_provider_and_dispatcher_voice
    )
    _livekit.set_stt_provider = set_stt_provider
    _livekit.publish_announcer_message = publish_announcer_message
    _livekit.publish_announcer_audio_file = publish_announcer_audio_file
    _livekit._build_livekit_client = _build_livekit_client
    _livekit._resolve_target_room = _resolve_target_room


def _sync_skills_compat_globals() -> None:
    _skills.CODEX_HOME_DIR = CODEX_HOME_DIR
    _skills.OPENBASE_CLAUDE_CONFIG_DIR = OPENBASE_CLAUDE_CONFIG_DIR
    _skills._home_skills_dir = _home_skills_dir


def agents_md(request):
    _sync_agents_md_compat_globals()
    return _agents_md.agents_md(request)


def ios_logs_upload(request):
    _diagnostics.DEFAULT_LOG_DIR = DEFAULT_LOG_DIR
    return _diagnostics.ios_logs_upload(request)


def user_say(request):
    _sync_livekit_compat_globals()
    return _livekit.user_say(request)


def user_play(request):
    _sync_livekit_compat_globals()
    return _livekit.user_play(request)


def cartesia_voice_settings(request):
    _sync_livekit_compat_globals()
    return _livekit.cartesia_voice_settings(request)


def tts_settings(request):
    _sync_livekit_compat_globals()
    return _livekit.tts_settings(request)


def kokoro_tts_download(request):
    _sync_livekit_compat_globals()
    return _livekit.kokoro_tts_download(request)


def stt_settings(request):
    _sync_livekit_compat_globals()
    return _livekit.stt_settings(request)


def local_stt_download(request):
    _sync_livekit_compat_globals()
    return _livekit.local_stt_download(request)


def dispatcher_voice_settings(request):
    _sync_livekit_compat_globals()
    return _livekit.dispatcher_voice_settings(request)


def livekit_companion_session(request):
    _sync_livekit_compat_globals()
    return _livekit.livekit_companion_session(request)


def livekit_companion_start(request):
    _sync_livekit_compat_globals()
    return _livekit.livekit_companion_start(request)


def skills_list(request):
    _sync_skills_compat_globals()
    return _skills.skills_list(request)


def skills_symlink(request):
    _sync_skills_compat_globals()
    return _skills.skills_symlink(request)


def skills_auto_link_settings(request):
    _sync_skills_compat_globals()
    return _skills.skills_auto_link_settings(request)


def printing_press_catalog(request):
    _sync_skills_compat_globals()
    return _skills.printing_press_catalog(request)


def printing_press_install(request):
    _sync_skills_compat_globals()
    return _skills.printing_press_install(request)


def skill_detail(request, skill_name):
    _sync_skills_compat_globals()
    return _skills.skill_detail(request, skill_name)


__all__ = [
    "agents_md",
    "agents_generation_settings",
    "all_project_reports",
    "approval_request_detail",
    "approval_requests",
    "auth_logout",
    "auth_refresh_jwt",
    "auth_session",
    "boilersync_templates",
    "bootstrap_run",
    "brain_readiness",
    "cartesia_voice_settings",
    "claude_auth_settings",
    "claude_plugin_settings",
    "coding_backend_settings",
    "dangerous_confirmation_settings",
    "devices_list",
    "dispatcher_voice_settings",
    "dispatcher_voice",
    "env_settings",
    "git_diff",
    "global_reports_projects",
    "health_check",
    "_home_skills_dir",
    "ios_logs_upload",
    "ios_app_control",
    "kokoro_tts_download",
    "keep_awake_settings",
    "launchctl_ignored_settings",
    "launchctl_service_action",
    "launchctl_services_list",
    "livekit_companion_session",
    "livekit_room_token",
    "livekit_voice_route",
    "livekit_voice_route_exit",
    "livekit_voice_route_transfer",
    "local_stt_download",
    "marketplace_skill_install",
    "marketplace_skills",
    "marketplace_routines",
    "onboarding_cloud_state",
    "onboarding_status",
    "openbase_restart",
    "openbase_service_action",
    "openbase_services_list",
    "plugin_console_registry",
    "plugin_detail",
    "plugins_list",
    "printing_press_catalog",
    "printing_press_install",
    "project_reports",
    "project_reports_action",
    "project_reports_download",
    "project_reports_file",
    "project_reports_tags",
    "project_status",
    "recent_projects",
    "reasoning_settings",
    "routine_detail",
    "routines_list",
    "routines_run_due",
    "service_status",
    "service_tier_settings",
    "thread_device_sync_status",
    "thread_device_sync_conflicts",
    "thread_device_sync_conflict_resolve",
    "thread_sync_conflicts",
    "set_dispatcher_voice",
    "skill_detail",
    "skills_list",
    "skills_auto_link_settings",
    "skills_symlink",
    "tag_options",
    "stt_settings",
    "tts_settings",
    "thread_active_voice",
    "thread_activity",
    "thread_detail",
    "thread_dispatcher",
    "thread_favorite",
    "thread_interrupt",
    "thread_list",
    "thread_queue_turn",
    "thread_start_turn",
    "thread_steer_turn",
    "thread_tags",
    "_build_livekit_client",
    "_resolve_target_room",
    "publish_announcer_audio_file",
    "publish_announcer_message",
    "user_play",
    "user_say",
    "uv_tool_detail",
    "uv_tool_executable_help",
    "uv_tools_list",
]
