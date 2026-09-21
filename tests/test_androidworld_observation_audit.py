from pathlib import Path

import pytest

from evaluation.androidworld.audit_observation_contract import audit_observation_contract


def fixture_sources(root: Path):
    (root / 'task_evals').mkdir()
    (root / 'env').mkdir()
    consumer = root / 'task_evals/example.py'
    consumer.write_text('def score(env):\n    state = env.get_state()\n    return state.forest\n')
    (root / 'env/representation_utils.py').write_text(
        'def accessibility_node_to_ui_element(node):\n    return node.hint_text\n'
        'def forest_to_ui_elements(node):\n    return node.child_ids\n')
    java = root / 'OracleDump.java'
    java.write_text('row.put("hint_text", node.getHintText()); row.put("child_ids", children);')
    return consumer, java


def test_new_official_field_cannot_silently_enter_frozen_batch(tmp_path):
    consumer, java = fixture_sources(tmp_path)
    report = audit_observation_contract(tmp_path, java)
    assert report['required_native_fields'] == ['child_ids', 'hint_text']
    consumer.write_text('def score(env):\n    return env.get_state().new_field\n')
    with pytest.raises(ValueError, match='Unreviewed official State fields'):
        audit_observation_contract(tmp_path, java)


def test_missing_converter_field_blocks_freeze(tmp_path):
    _, java = fixture_sources(tmp_path)
    java.write_text('row.put("child_ids", children);')
    with pytest.raises(ValueError, match='hint_text'):
        audit_observation_contract(tmp_path, java)
