"""One persistent emulator episode; only public goal and limits cross this boundary."""
import argparse
import asyncio
from contextlib import suppress
import hashlib
import json
import os
import shutil
from pathlib import Path
import sys
import time
import traceback

def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


class ObservationPreflightError(RuntimeError):
    """No usable current pixels before any model invocation."""


async def capture_preflight(driver, output, *, timeout_s=20.0):
    """Use the runtime's fresh capture path, including its supported ADB fallback.

    This readiness check does not hand its image to the agent: run_task obtains
    its own observation. It must not require a tree or a particular pixel backend.
    """
    from perception.observation import decoded_image_size

    report = {'policy': 'decoded-current-pixels-v2', 'accepted': False, 'attempts': []}
    try:
        async with asyncio.timeout(timeout_s):
            try:
                report['warmup_ready'] = bool(await driver.warm_observation_provider())
            except Exception as exc:
                # A stream startup error must not prevent the normal ADB fallback.
                report['warmup_error'] = f'{type(exc).__name__}: {exc}'
            for ordinal in range(1, 3):
                attempt = {'ordinal': ordinal}
                try:
                    tree, pixels = await driver.get_frame()
                    capture = tree.get('_capture', {})
                    size = decoded_image_size(pixels)
                    attempt.update(pixel_provider=capture.get('pixel_provider'),
                                   decoded_size=size, pixel_bytes=len(pixels or b''), capture=capture)
                    # get_frame captures afresh; never substitute a cached preview
                    # or merely trust dimensions reported by the provider.
                    accepted = (capture.get('pixel_provider') in {'scrcpy', 'adb_screencap'}
                                and all(size))
                    attempt['accepted'] = accepted
                    if not accepted:
                        attempt['error'] = 'unsupported pixel source or undecodable image'
                except Exception as exc:
                    attempt.update(accepted=False, error=f'{type(exc).__name__}: {exc}')
                report['attempts'].append(attempt)
                report.update({k: attempt[k] for k in
                               ('pixel_provider', 'decoded_size', 'pixel_bytes', 'capture') if k in attempt})
                if attempt['accepted']:
                    report['accepted'] = True
                    return report
                # At most one fresh retry; no model call or device action involved.
    except TimeoutError:
        report['error'] = 'observation preflight deadline exceeded'
    finally:
        save(output / 'capture-preflight.json', report)
    raise ObservationPreflightError('No usable current screenshot; see capture-preflight.json')


def prepare_skill_root(branch, output, profile):
    """Keep evaluation guidance tied to the selected frozen runtime."""
    if profile == 'app':
        return branch/'skills'
    if profile != 'androidworld':
        raise ValueError(f'Unknown skill profile: {profile}')
    overlay = branch/'evaluation/androidworld/skills'
    if not overlay.is_dir() or not any(overlay.rglob('SKILL.md')):
        raise RuntimeError(f'Frozen runtime is missing AndroidWorld skill profile: {overlay}')
    skill_root = output/'skills-profile'
    shutil.copytree(branch/'skills', skill_root)
    shutil.copytree(overlay, skill_root, dirs_exist_ok=True)
    return skill_root

async def main(args):
    branch = args.runtime.resolve()
    sys.path.insert(0, str(branch))
    os.chdir(branch)
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.factory import get_driver
    from driver.scrcpy_mirror import REGISTRY as mirror_registry
    from shared.artifacts import ArtifactStore
    from shared.config import Settings
    from shared.db import Database
    from agent.integrations.android_world import androidworld_agent_state
    from shared.schemas import TaskStatus
    from shared.revisable import TaskLimits

    request = json.loads(args.request.read_text(encoding='utf-8'))
    output = args.request.parent
    model = os.environ.get('CLICKCLICK_EVAL_MODEL', 'chatgpt/gpt-5.6-sol')
    skill_root = prepare_skill_root(branch, output, args.profile)
    os.environ['CLICKCLICK_SKILLS_DIR'] = str(skill_root)
    settings = Settings(_env_file=args.env_file, data_dir=output/'runtime',
        agent_architecture=args.architecture, default_model=model, manager_model=model,
        executor_model=model, skill_learner_model=model,
        driver_url='', driver_urls_json='', use_fixture_driver=False,
        executor_context_tokens=args.history_tokens, device_stay_awake_while_plugged=True)
    provider = settings.provider_for(model)
    # Isolate the experiment from deployment-specific model context overrides.
    providers = settings.model_providers()
    context = providers[model].setdefault('context', {})
    context.setdefault('executor', {})['history_tokens'] = args.history_tokens
    settings.models_json = json.dumps(providers)
    if not provider:
        raise ValueError('Missing requested model configuration')
    if model == 'chatgpt/gpt-5.6-sol' and provider.get('reasoning', {}).get('effort') != 'high':
        raise ValueError('The reference model requires reasoning effort high')
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = get_driver(settings, serial='emulator-5554')
    orch = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
        driver=driver, artifacts=artifacts, max_steps=request['max_model_calls'],
        max_role_invocations=request['max_model_calls'])
    if getattr(args, 'official_records', False):
        from evaluation.androidworld.official_trajectory import install_observation_recorder
        install_observation_recorder(orch, output / 'official-observations')
    record = db.create_task(request['goal'], androidworld_agent_state(request['goal']),device_serial='emulator-5554')
    save(output/'runtime-manifest.json', {'task_id':record.id,'branch':str(branch),
        'runtime_module':sys.modules['agent.runtime'].__file__, 'model':model,
        'provider':{k:v for k,v in provider.items() if k in ['provider','reasoning','stream','reasoning_supported']},
        'settings':{'architecture':settings.agent_architecture,'executor_context_tokens':settings.executor_context_tokens,
                    'skill_profile':args.profile,'skill_root':str(skill_root),
                    'skill_overlay_root':str(branch/'evaluation/androidworld/skills') if args.profile == 'androidworld' else None},
        'skill_hashes':{str(p.relative_to(skill_root)):hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in skill_root.rglob('SKILL.md')},
        'runtime_hashes':{str(p.relative_to(branch)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ['agent','driver','perception','shared','skills'] for p in (branch/folder).rglob('*') if p.is_file() and p.suffix in ['.py','.md','.json']}})
    began = time.monotonic()
    state = record.state
    state.revisable.limits = TaskLimits(device_actions=request['max_steps'],
        deadline_at=time.time()+request['max_seconds'])
    db.update_task(record.id, state=state)
    episode_steps = 0
    cause = None

    async def watchdog():
        nonlocal cause
        while time.monotonic()-began < request['max_seconds']:
            if (args.output_root/'STOP').exists():
                cause = 'operator_stop'
                break
            task = db.get_task(record.id)
            save(output/'progress.json', {'device_actions':task.state.revisable.execution_count,
                'executor_decisions':task.step_number,'model_calls':task.state.role_invocation_count,
                'status':task.status.value,'elapsed_s':time.monotonic()-began,
                'next_role':task.state.revisable.next_role})
            await asyncio.sleep(1)
        cause = cause or 'wall_time_limit'
        orch.request_cancel(record.id)

    watcher = asyncio.create_task(watchdog())
    try:
        await capture_preflight(driver, output)
        await orch.run_task(record.id)
    except ObservationPreflightError:
        cause = cause or 'observation_preflight_failed'
        (output/'worker-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
    except BaseException:
        cause = cause or 'runtime_exception'
        (output/'worker-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
    finally:
        elapsed = time.monotonic()-began
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        task = db.get_task(record.id)
        episode_steps = task.state.revisable.execution_count
        if task.failure_reason == 'task_deadline_exhausted':
            cause = cause or 'wall_time_limit'
        save(output/'task.json', task.model_dump(mode='json'))
        save(output/'steps.json', db.list_steps(record.id))
        save(output/'traces.json', [t.model_dump(mode='json') for t in db.list_traces(record.id)])
        save(output/'worker-result.json', {'task_id':record.id,'status':task.status.value,
            'failure_reason':task.failure_reason,'stop_cause':cause,'episode_steps':episode_steps,
            'executor_decisions':task.step_number,'role_invocations':task.state.role_invocation_count,
            'elapsed_s':elapsed,
            **({'infrastructure_failure': 'observation_unavailable'}
               if cause == 'observation_preflight_failed' else {}),
            'answer':task.state.revisable.completion_reason if task.status.value == 'succeeded' else ''})
        with suppress(Exception):
            await driver.close_observation_provider()
        try:
            # This worker owns the whole registry. Do not abandon its delayed
            # idle cleanup when asyncio.run() cancels background tasks on exit.
            await mirror_registry.shutdown()
        except Exception:
            (output/'worker-cleanup-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
            raise
        finally:
            db.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--architecture',required=True,choices=['plan_reviewer','plan_executor'])
    parser.add_argument('--request',required=True,type=Path)
    parser.add_argument('--runtime',required=True,type=Path)
    parser.add_argument('--env-file',required=True,type=Path)
    parser.add_argument('--output-root',required=True,type=Path)
    parser.add_argument('--profile',choices=['app','androidworld'],default='androidworld')
    parser.add_argument('--history-tokens',type=int,choices=[16000,24000,32000],default=16000)
    parser.add_argument('--official-records', action='store_true',
                        help='Save the raw live observation paired with each action for official checkpoints.')
    asyncio.run(main(parser.parse_args()))
