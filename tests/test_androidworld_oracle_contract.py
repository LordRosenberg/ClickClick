"""Run in the AndroidWorld venv against the frozen official package."""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("android_world")
from android_world.env import android_world_controller, representation_utils
from android_world.task_evals.single.contacts import _contact_info_is_entered
from evaluation.androidworld import scoring


def node(text, hint="", description="", visible=True):
    strings = dict(text=text, hint_text=hint, content_description=description,
                   class_name="android.widget.EditText", package_name="com.contacts",
                   view_id_resource_name="field")
    flags = {key: False for key in (
        'is_checked', 'is_checkable', 'is_clickable', 'is_editable', 'is_enabled',
        'is_focused', 'is_focusable', 'is_long_clickable', 'is_scrollable', 'is_selected')}
    return dict(**strings, **flags, is_visible_to_user=visible,
                bounds_in_screen=dict(left=0, right=100, top=100, bottom=150), child_ids=[])


def payload():
    nodes = [node('', visible=False), node('Jane', 'First name'), node('Smith', 'Last name'),
             node('123-456-7890', 'Phone'), node('Work', description='Work Phone')]
    for i, row in enumerate(nodes): row['id'] = i
    nodes[0]['child_ids'] = list(range(1, len(nodes)))
    return dict(format='oracle-forest-v1', windows=[dict(tree=dict(nodes=nodes))])


def test_real_hints_keep_official_positive_and_negative_predicates():
    data = payload()
    forest = scoring.parse_oracle_forest(json.dumps(data))
    elements = representation_utils.forest_to_ui_elements(forest, exclude_invisible_elements=False)
    check = lambda items: _contact_info_is_entered('Jane', 'Smith', '123-456-7890', 'Work', items)
    assert check(elements)
    for index, field, value in [(1, 'hint_text', ''), (2, 'text', 'Wrong'), (3, 'text', '999')]:
        changed = payload()
        changed['windows'][0]['tree']['nodes'][index][field] = value
        assert not check(representation_utils.forest_to_ui_elements(
            scoring.parse_oracle_forest(json.dumps(changed))))


@pytest.mark.parametrize('field', ['hint_text', 'text', 'is_visible_to_user', 'bounds_in_screen', 'child_ids'])
def test_missing_native_fields_are_errors_not_protobuf_defaults(field):
    data = payload()
    del data['windows'][0]['tree']['nodes'][1][field]
    with pytest.raises(scoring.OracleObservationError):
        scoring.parse_oracle_forest(json.dumps(data))


def test_official_controller_provides_both_views_and_restores_on_error(monkeypatch, tmp_path):
    forest = scoring.parse_oracle_forest(json.dumps(payload()))
    controller = android_world_controller.AndroidWorldController.__new__(android_world_controller.AndroidWorldController)
    old_method = android_world_controller.A11yMethod.UIAUTOMATOR
    controller._a11y_method = old_method
    monkeypatch.setattr(scoring, 'read_fresh_forest', lambda *args: forest)
    env = SimpleNamespace(controller=controller)
    with pytest.raises(RuntimeError, match='predicate failure'):
        with scoring.fresh_oracle_observations(env, tmp_path, []):
            timestep = SimpleNamespace(observation={})
            controller._process_timestep(timestep)
            assert timestep.observation['forest'] is forest
            assert any(e.hint_text == 'First name' for e in timestep.observation['ui_elements'])
            raise RuntimeError('predicate failure')
    assert controller._a11y_method == old_method
    assert 'get_a11y_forest' not in vars(controller)


@pytest.mark.parametrize('failures', [0, 1, 3])
def test_capture_retries_are_bounded_and_never_retry_predicate(monkeypatch, tmp_path, failures):
    calls = []
    monkeypatch.setattr(scoring.Path, 'is_file', lambda self: True)
    monkeypatch.setattr(scoring.time, 'sleep', lambda _: None)
    def capture(*args, **kwargs):
        calls.append(1)
        if len(calls) <= failures:
            raise scoring.OracleObservationError('window changed')
        return json.dumps(payload())
    monkeypatch.setattr(scoring, 'fresh_uiautomator_dump', capture)
    if failures == 3:
        with pytest.raises(scoring.OracleObservationError):
            scoring.read_fresh_forest(None, tmp_path, [])
    else:
        assert scoring.read_fresh_forest(None, tmp_path, []).windows
    assert len(calls) == min(failures + 1, 3)
