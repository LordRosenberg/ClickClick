"""Reset discards tree acquisition but keeps official reset and oracle settings."""
import ast
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('fails', [False, True])
def test_reset_restores_oracle_observer_even_on_error(fails):
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'reset_for_agent')
    namespace = {
        'android_world_controller': SimpleNamespace(A11yMethod=SimpleNamespace(NONE='none')),
        'hide_pointer_location': lambda: None,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    controller = SimpleNamespace(_a11y_method='uiautomator')
    calls = []

    def reset(*, go_home):
        calls.append(go_home)
        assert controller._a11y_method == 'none'
        if fails:
            raise RuntimeError('reset failed')

    env = SimpleNamespace(controller=controller, reset=reset)
    if fails:
        with pytest.raises(RuntimeError, match='reset failed'):
            namespace['reset_for_agent'](env, go_home=True)
    else:
        namespace['reset_for_agent'](env, go_home=True)
    assert calls == [True]
    assert controller._a11y_method == 'uiautomator'


def test_existing_emulator_runner_uses_bundled_ffmpeg_for_audio_fixtures():
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    source = path.read_text(encoding='utf-8')

    assert 'import imageio_ffmpeg' in source
    assert 'AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()' in source


def test_existing_emulator_runner_submits_information_retrieval_answer_before_scoring():
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    source = path.read_text(encoding='utf-8')

    submit = source.index("env.interaction_cache = result.get('answer', '')")
    score = source.index("result.update(score_task(task,env,out,'final'))")
    assert submit < score


def test_retro_lazy_empty_queue_is_only_tolerated_for_initial_score():
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'initial_score')

    def missing_queue(*_args, **_kwargs):
        raise sqlite3.OperationalError('no such table: playing_queue')

    namespace = {'score_task': missing_queue, 'sqlite3': sqlite3}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)

    score, note = namespace['initial_score'](object(), object(), Path('.'), 'RetroPlayingQueue')
    assert score == 0.0
    assert 'created lazily' in note

    with pytest.raises(sqlite3.OperationalError, match='playing_queue'):
        namespace['initial_score'](object(), object(), Path('.'), 'OtherTask')
