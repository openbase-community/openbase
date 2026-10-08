from openbase_coder_cli import dispatcher_instructions as instructions


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
    result = instructions.with_dispatcher_rules('Load the skill through tools.')
    assert result == 'Load the skill through tools.\n\n' + instructions.CURRENT_STATE_RULES
