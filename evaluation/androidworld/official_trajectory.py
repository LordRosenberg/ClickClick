"""Evidence for the official Checkpointer, captured from actual agent observations.

This evaluation-only hook neither captures another frame nor changes an action.
Historical runs without raw observations are deliberately not accepted.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time


def agent_session_done(result: dict) -> bool:
    """Map explicit agent termination, not its belief about success, to done.

    Upstream AgentInteractionResult.done means the session ends; M3A returns
    True for both status=complete and status=infeasible. Preserve our explicit
    inconclusive decisions likewise. A watchdog/budget interruption or unknown
    failure is not silently reclassified as an intentional agent termination.
    """
    if result.get('stop_cause'):
        return False
    if result.get('status') == 'succeeded':
        return True
    return (result.get('status') == 'failed' and
            str(result.get('failure_reason') or '').startswith(
                ('planner_inconclusive:', 'review_inconclusive:')))


def install_observation_recorder(orchestrator, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    original = orchestrator._persist_observation

    def persist(package):
        original(package)
        observation_id = package.observation_id
        if not observation_id.startswith('obs_') or not observation_id.replace('_', '').isalnum():
            raise ValueError('Invalid observation identity')
        metadata = directory / f'{observation_id}.json'
        if metadata.exists():
            return
        if not package.clean_png:
            raise RuntimeError('Official evidence requires the actual raw observation image')
        image = directory / f'{observation_id}.png'
        image.write_bytes(package.clean_png)
        metadata.write_text(json.dumps({
            'observation_id': observation_id,
            'image': image.name,
            'image_sha256': hashlib.sha256(package.clean_png).hexdigest(),
            'recorded_at': time.time(),
            'frame_size': [package.frame_width, package.frame_height],
            'capture': package.capture_meta,
            'text_for_llm': package.text_for_llm,
            'annotated_ref': package.som_ref,
            'tree_ref': package.tree_ref,
            'source': 'live_clickclick_observation_before_action',
        }, ensure_ascii=False, indent=2), encoding='utf-8')

    orchestrator._persist_observation = persist


def _artifact(root: Path, reference: str):
    path = (root / reference).resolve()
    if root.resolve() not in path.parents:
        raise ValueError('Artifact reference escapes episode')
    return json.loads(path.read_text(encoding='utf-8'))


def episode_result(output: Path, *, max_steps: int, max_seconds: float, max_model_calls: int):
    """Return an official EpisodeResult; the official suite owns score and metadata."""
    import numpy as np
    from PIL import Image
    from android_world.episode_runner import EpisodeResult

    worker = json.loads((output / 'worker-result.json').read_text(encoding='utf-8'))
    steps = json.loads((output / 'steps.json').read_text(encoding='utf-8'))
    traces = json.loads((output / 'traces.json').read_text(encoding='utf-8'))
    artifacts = output / 'runtime/artifacts'
    observations = output / 'official-observations'
    data = {name: [] for name in ('step_number', 'raw_screenshot', 'action',
            'action_receipt', 'observation_id', 'before_ui_elements', 'action_prompt',
            'action_output', 'summary', 'clickclick_executor_step')}
    # Cognitive decisions remain in auxiliary data, rather than being counted as
    # physical device actions. Every action row, including a refusal, is retained.
    action_steps = [step for step in steps if step.get('action') is not None]
    if len(action_steps) != worker['episode_steps']:
        raise RuntimeError('Device action accounting differs from saved step evidence')
    source_hashes = {}
    for ordinal, step in enumerate(action_steps):
        oid = step.get('basis_observation_id') or step.get('observation_id')
        metadata_path = observations / f'{oid}.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        image_path = observations / metadata['image']
        if image_path.resolve().parent != observations.resolve():
            raise ValueError('Image path escapes observations')
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if digest != metadata['image_sha256'] or metadata['observation_id'] != oid:
            raise RuntimeError('Observation identity or raw image hash mismatch')
        with Image.open(image_path) as image:
            pixels = np.asarray(image.convert('RGB')).copy()
        values = {
            'step_number': ordinal,
            'raw_screenshot': pixels,
            'action': step['action'],
            'action_receipt': step.get('action_receipt'),
            'observation_id': oid,
            # This is the actual agent-visible tree, not a fabricated official
            # UIElement object reconstructed from labels or a later capture.
            'before_ui_elements': metadata['text_for_llm'],
            'action_prompt': _artifact(artifacts, step['llm_input_ref']),
            'action_output': _artifact(artifacts, step['llm_output_ref']),
            'summary': step.get('summary'),
            'clickclick_executor_step': step,
        }
        for key, value in values.items():
            data[key].append(value)
        source_hashes[image_path.name] = digest
    done = (agent_session_done(worker)
            and worker['episode_steps'] <= max_steps
            and worker['elapsed_s'] <= max_seconds
            and worker['role_invocations'] <= max_model_calls)
    return EpisodeResult(done=done, step_data=data, aux_data={
        'worker_result': worker, 'executor_decisions': steps, 'traces': traces,
        'raw_image_hashes': source_hashes,
        'agent_action_space': 'ClickClick (unchanged); actions are not remapped to AndroidWorld JSONAction',
        'tree_representation': 'ClickClick agent-visible tree text',
        'recording': 'live raw observations; official suite scoring and checkpoint lifecycle',
        'done_semantics': 'agent explicitly ended session, including inconclusive; success is determined by official predicate',
        'limits': {'device_actions': max_steps, 'seconds': max_seconds,
                   'model_calls': max_model_calls},
    })
