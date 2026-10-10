from openbase_coder_cli import dispatcher_instructions as instructions
from openbase_coder_cli import host_kind


def test_canonical_skill_is_loaded_from_installed_link_and_not_duplicated(tmp_path, monkeypatch):
    root = tmp_path / 'codex'
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'SKILL.md').write_text('Inspect known folders before asking for a path.')
    (root / 'skills').mkdir(parents=True)
    (root / 'skills' / instructions.SKILL_NAME).symlink_to(source, target_is_directory=True)
    monkeypatch.setattr(instructions, 'CODEX_HOME_DIR', root)
    monkeypatch.setattr(instructions, 'CLAUDE_CONFIG_DIR', tmp_path / 'claude')
    monkeypatch.setattr(instructions, 'packaged_skills_dir', lambda: None)
    result = instructions.with_dispatcher_skill('Custom dispatcher policy.')
    assert result.startswith('Custom dispatcher policy.')
    assert 'Inspect known folders before asking for a path.' in result
    assert instructions.with_dispatcher_skill(result) == result
    (source / 'SKILL.md').write_text('Updated canonical routing rule.')
    assert 'Updated canonical routing rule.' in instructions.with_dispatcher_skill('Custom dispatcher policy.')


def test_missing_skill_preserves_existing_dispatcher_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(instructions, 'CODEX_HOME_DIR', tmp_path / 'codex')
    monkeypatch.setattr(instructions, 'CLAUDE_CONFIG_DIR', tmp_path / 'claude')
    monkeypatch.setattr(instructions, 'packaged_skills_dir', lambda: None)
    assert instructions.with_dispatcher_skill('Load the skill through tools.') == 'Load the skill through tools.'


def test_voice_worker_and_warmup_receive_the_same_canonical_procedure(tmp_path, monkeypatch):
    from openbase_coder_cli import livekit_voice_route
    from openbase_coder_cli.livekit_agent import config
    source = tmp_path / 'dispatcher.md'
    source.write_text('Dispatcher policy.')
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: 'Canonical task ownership.')
    monkeypatch.setattr(config, 'CODEX_DISPATCHER_INSTRUCTIONS_PATH', source)
    monkeypatch.setattr(livekit_voice_route, 'CODEX_DISPATCHER_INSTRUCTIONS_PATH', source)
    expected = instructions.with_dispatcher_rules('Dispatcher policy.')
    assert config._load_dispatcher_developer_instructions() == expected
    assert livekit_voice_route._dispatcher_developer_instructions() == expected
    assert instructions.CURRENT_STATE_HEADING in expected
    assert expected.endswith('Canonical task ownership.')


def test_dispatcher_rules_require_checking_current_state_every_time(monkeypatch):
    # Regression (2026-10-08 staging call): the dispatcher answered "your
    # desktop is empty" from a listing 100 minutes old and refused to open a
    # desktop folder created since, without a tool call.
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: 'Canonical procedure.')
    result = instructions.with_dispatcher_rules('Dispatcher policy.')
    assert result.startswith('Dispatcher policy.')
    rules = ' '.join(instructions.CURRENT_STATE_RULES.split())
    assert 'by checking it in this turn' in rules
    assert 'never from earlier turns of this conversation' in rules
    assert 'Never say that a file, folder or project does not exist' in rules
    assert 'instead of refusing based on earlier turns' in rules
    assert result.index(instructions.CURRENT_STATE_HEADING) < result.index(instructions.PROCEDURE_HEADING)
    assert instructions.with_dispatcher_rules(result) == result
    assert result.count(instructions.CURRENT_STATE_HEADING) == 1


def test_dispatcher_rules_apply_without_the_canonical_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: '')
    result = instructions.with_dispatcher_rules('Load the skill through tools.', host='mac')
    assert result == (
        'Load the skill through tools.\n\n' + host_kind.host_section('mac')
        + '\n\n' + instructions.CURRENT_STATE_RULES
        + '\n\n' + instructions.START_RULES
        + '\n\n' + instructions.SCREEN_CONTEXT_RULES
    )


def test_dispatcher_rules_say_a_cloud_workspace_has_no_desktop(monkeypatch):
    """Regression for the 2026-10-08 staging demo: a dispatcher on a cloud
    container answered "what's on my desktop" as if it sat at the user's Mac."""
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: '')
    cloud = instructions.with_dispatcher_rules('Base.', host=host_kind.HOST_KIND_CLOUD_WORKSPACE)
    assert host_kind.HOST_KIND_HEADING in cloud
    assert 'no screen, no Desktop folder' in cloud
    assert 'their cloud workspace' in cloud
    assert 'check whether available laptop tools can reach' in cloud
    assert 'If access is unavailable' in cloud
    assert "Never infer that another device's desktop is empty" in cloud
    assert cloud.index(host_kind.HOST_KIND_HEADING) < cloud.index(instructions.CURRENT_STATE_HEADING)
    assert cloud.count(host_kind.HOST_KIND_HEADING) == 1
    assert instructions.with_dispatcher_rules(cloud, host=host_kind.HOST_KIND_CLOUD_WORKSPACE) == cloud


def test_dispatcher_rules_say_the_users_own_mac_has_its_files_and_screen(monkeypatch):
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: '')
    mac = instructions.with_dispatcher_rules('Base.', host=host_kind.HOST_KIND_MAC)
    assert "runs on the user's own Mac" in mac
    assert 'Desktop' in mac and 'screen' in mac
    assert 'cloud workspace' not in mac


def test_host_kind_detects_a_cloud_workspace_before_the_platform(monkeypatch):
    import openbase_coder_cli.services.cloud_workspace as cloud_workspace

    monkeypatch.setattr(cloud_workspace, 'cloud_workspace_id', lambda: 'ca0a7c808edc')
    monkeypatch.setattr(host_kind.sys, 'platform', 'linux')
    assert host_kind.host_kind() == host_kind.HOST_KIND_CLOUD_WORKSPACE
    monkeypatch.setattr(cloud_workspace, 'cloud_workspace_id', lambda: None)
    assert host_kind.host_kind() == host_kind.HOST_KIND_LINUX
    monkeypatch.setattr(host_kind.sys, 'platform', 'darwin')
    assert host_kind.host_kind() == host_kind.HOST_KIND_MAC
    monkeypatch.setattr(host_kind.sys, 'platform', 'win32')
    assert host_kind.host_kind() == host_kind.HOST_KIND_WINDOWS


def test_host_section_precedes_existing_current_state_rules(monkeypatch):
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: '')
    existing = 'Base.\n\n' + instructions.CURRENT_STATE_RULES
    result = instructions.with_dispatcher_rules(existing, host='cloud_workspace')
    assert result.index(host_kind.HOST_KIND_HEADING) < result.index(instructions.CURRENT_STATE_HEADING)
    assert result.count(instructions.CURRENT_STATE_HEADING) == 1
    assert instructions.with_dispatcher_rules(result, host='cloud_workspace') == result


def test_dispatcher_rules_make_starting_a_super_agent_two_steps(monkeypatch):
    # Regression (2026-10-08 staging voice demo): the dispatcher created a
    # tic-tac-toe thread with the task in developerInstructions, never started
    # a turn, and said the agent was working; the thread showed no messages.
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: 'Canonical procedure.')
    result = instructions.with_dispatcher_rules('Dispatcher policy.')
    rules = ' '.join(instructions.START_RULES.split())
    assert 'super_agents_start only creates the thread' in rules
    assert 'Pass the task as `prompt` in that same call' in rules
    assert 'developerInstructions is standing guidance, never the task' in rules
    assert 'turnStarted true' in rules
    assert 'start the turn before confirming' in rules
    assert result.index(instructions.CURRENT_STATE_HEADING) < result.index(instructions.START_HEADING)
    assert result.index(instructions.START_HEADING) < result.index(instructions.PROCEDURE_HEADING)
    assert instructions.with_dispatcher_rules(result) == result
    assert result.count(instructions.START_HEADING) == 1


def test_dispatcher_rules_tell_it_to_act_on_the_thread_open_on_screen(monkeypatch):
    """BUG 18: "this thread" means the thread named in the screen note."""
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: '')
    rules = instructions.with_dispatcher_rules('Base.', host='mac')
    assert rules.count(instructions.SCREEN_CONTEXT_HEADING) == 1
    assert 'super_agents_start_turn' in rules and 'super_agents_steer' in rules
    assert '"this thread"' in rules
    assert instructions.with_dispatcher_rules(rules, host='mac') == rules


def test_dispatcher_requires_confirmed_steering_and_queue_outcomes(monkeypatch):
    monkeypatch.setattr(instructions, 'canonical_dispatcher_skill', lambda: '')
    result = instructions.with_dispatcher_rules('Dispatcher policy.')
    rules = ' '.join(result.split())
    assert 'only after a successful steer/queue call' in rules
    assert 'steered true or queued true' in rules
    assert 'Never promise automatic delivery when the SDK becomes ready' in rules
    assert 'super_agents_queue_turn' in rules
    assert 'nothing was delivered or queued' in rules
    assert 'queued fallback does not interrupt current work' in rules


def test_start_rules_have_the_dispatcher_choose_a_super_agent_cwd_by_looking():
    # Gabe, 2026-10-09: a Super Agent's cwd is the dispatcher's judgment after
    # listing folders, never an exact match of a spoken name (speech
    # recognition mangles names); the dispatcher's own directory never moves.
    rules = ' '.join(instructions.START_RULES.split())
    assert 'You always run in your own default directory' in rules
    assert "choose its `cwd` yourself by looking: `ls`" in rules
    assert 'Ask only when two folders are genuinely plausible' in rules
    assert 'Never start an agent in your own directory just because nothing matched exactly' in rules
    assert 'project-dir' not in rules
    assert 'MUST' not in rules
