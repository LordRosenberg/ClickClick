"""Small real upstream Checkpointer contract tests; no emulator interaction."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

MODULE = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/official_trajectory.py'
spec = importlib.util.spec_from_file_location('official_trajectory', MODULE)
records = importlib.util.module_from_spec(spec)
spec.loader.exec_module(records)


def make_episode(tmp_path, status='succeeded', failure_reason=None, stop_cause=None):
    raw = io.BytesIO()
    Image.new('RGB', (3, 2), (12, 34, 56)).save(raw, format='PNG')
    package = SimpleNamespace(observation_id='obs_123', clean_png=raw.getvalue(),
        frame_width=3, frame_height=2, capture_meta={'accepted': True},
        text_for_llm='[0] Button Save', som_ref='som/example.png', tree_ref='trees/example.json')
    persisted = []
    orchestrator = SimpleNamespace(_persist_observation=persisted.append)
    records.install_observation_recorder(orchestrator, tmp_path/'official-observations')
    orchestrator._persist_observation(package)
    assert persisted == [package]
    art = tmp_path/'runtime/artifacts/llm'
    art.mkdir(parents=True)
    (art/'input.json').write_text('{"prompt": "test"}')
    (art/'output.json').write_text('{"action": "tap"}')
    steps = [dict(seq=0, action={'type':'tap', 'index':0},
                  observation_id='obs_123', basis_observation_id='obs_123',
                  action_receipt={'native_action_performed':True},
                  llm_input_ref='llm/input.json', llm_output_ref='llm/output.json'),
             dict(seq=1, action=None, executor_decision='request_replan')]
    for name, value in {
        'worker-result.json':dict(status=status, stop_cause=stop_cause,
                                 failure_reason=failure_reason, episode_steps=1,
                                 elapsed_s=5, role_invocations=3),
        'steps.json':steps, 'traces.json':[{'message':'complete'}],
    }.items():
        (tmp_path/name).write_text(json.dumps(value))
    return records.episode_result(tmp_path,max_steps=2,max_seconds=30,max_model_calls=12)


@pytest.mark.parametrize('status,reason,stop,predicate,expected', [
    ('succeeded', None, None, 1.0, 1.0),
    ('failed', None, None, 1.0, 0.0),
    ('failed', 'planner_inconclusive:remaining coverage unknown', None, 1.0, 1.0),
    ('failed', 'review_inconclusive:remaining coverage unknown', None, 0.0, 0.0),
    ('failed', 'planner_inconclusive:remaining coverage unknown', 'max_steps', 1.0, 0.0),
])
def test_original_suite_writes_real_readable_checkpoint_and_gates_completion(tmp_path,status,reason,stop,predicate,expected):
    pytest.importorskip('android_world')
    from android_world import checkpointer, suite_utils
    result = make_episode(tmp_path,status,reason,stop)
    events = []
    class Task:
        name='RecorderContractTest'
        goal='Tap Save'
        params={'seed':123}
        def initialize_task(self,env):events.append('initialize')
        def is_successful(self,env):events.append('score');return predicate
        def tear_down(self,env):events.append('teardown')
    cp=checkpointer.IncrementalCheckpointer(str(tmp_path/'checkpoints'))
    suite_utils._run_task_suite(suite_utils.Suite({'RecorderContractTest':[Task()]}),
        lambda task:result, object(), checkpointer=cp, agent_name='clickclick',
        process_episodes_fn=lambda *args,**kwargs:None)
    loaded=cp.load()[0]
    assert events==['initialize','score','teardown']
    assert loaded['is_successful']==expected
    assert loaded['episode_length']==1
    assert loaded['seed']==123
    assert loaded['episode_data']['raw_screenshot'][0].shape==(2,3,3)
    assert loaded['episode_data']['raw_screenshot'][0][0,0].tolist()==[12,34,56]
    assert len(loaded['aux_data']['executor_decisions'])==2


def test_missing_live_image_cannot_be_replaced_with_later_or_annotated_image(tmp_path):
    pytest.importorskip('android_world')
    make_episode(tmp_path)
    (tmp_path/'official-observations/obs_123.png').write_bytes(b'wrong-frame')
    with pytest.raises(RuntimeError,match='hash mismatch'):
        records.episode_result(tmp_path,max_steps=2,max_seconds=30,max_model_calls=12)
