"""Prepare and run the frozen October 2026 MobileWorld GUI-only evaluation.

Preparation has no device, Docker, model or credential access. Execution uses
separate original and clarified roots; it never selects the best attempt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BUNDLE = Path(__file__).with_name('reproduction')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding='utf8'))


def write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def verify(directory: Path, seal: dict):
    for name, expected in seal.items():
        path = directory/name
        if not path.is_file() or path.is_symlink() or digest(path) != expected:
            raise RuntimeError('Frozen input changed: ' + name)
    expected_code = {name for name in seal if name.endswith('.py')}
    actual_code = {p.relative_to(directory).as_posix() for p in directory.rglob('*.py')
                   if 'data' not in p.relative_to(directory).parts}
    if actual_code != expected_code:
        raise RuntimeError('Unexpected Python source in frozen workspace')


def prepare(root: Path) -> Path:
    seal = read(BUNDLE/'seal.json')
    verify(BUNDLE, seal)
    prepared = root/'input'
    if prepared.exists():
        verify(prepared, seal)
        if read(root/'input-seal.json') != seal:
            raise RuntimeError('Output belongs to another source release')
        return prepared
    root.mkdir(parents=True, exist_ok=True)
    prepared.mkdir()
    for name in seal:
        output = prepared/name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BUNDLE/name, output)
    write(root/'input-seal.json', seal)
    verify(prepared, seal)
    return prepared


def summarize(root: Path, profile: dict) -> dict:
    names = list(profile['tasks'])
    def results(arm):
        path = root/arm/'results.json'
        rows = read(path) if path.exists() else []
        indexed = {}
        for row in rows:
            name = row['task']
            if name not in names or name in indexed:
                raise RuntimeError('Unexpected or duplicate result: ' + name)
            indexed[name] = row
        return indexed
    original, changed = results('original'), results('clarified')
    if any(name not in profile['clarified_tasks'] for name in changed):
        raise RuntimeError('Unexpected clarified execution')
    # Substitute every disclosed variant, including its failures. No max-score selection.
    paired = dict(original)
    paired.update(changed)
    def count(rows, required):
        scored = {n: r for n,r in rows.items() if r.get('environment_status') != 'invalid_initialization'
                  and isinstance(r.get('score'), dict) and isinstance(r['score'].get('score'), (int,float))}
        passed = sum(float(r['score']['score']) >= 1.0 for r in scored.values())
        complete = set(required) <= set(scored)
        return {'passed':passed, 'total':117, 'scored':len(scored), 'complete':complete,
                'success_percent':round(100*passed/117,2) if complete else None}
    summary = {'original':count(original,names), 'clarified':count(paired,names),
               'clarified_variants_scored':len(changed), 'construction':'paired substitution'}
    if set(profile['clarified_tasks']) != set(changed):
        summary['clarified']['complete'] = False
        summary['clarified']['success_percent'] = None
    write(root/'public-summary.json',summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root',type=Path,default=Path('data/mobileworld-reproduction'))
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--install',action='store_true',help='Install frozen runtime dependencies into this Python environment')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--variant',choices=['paired','original','clarified'],default='paired')
    parser.add_argument('--env-file',type=Path,help='Local model/auth settings; copied only into ignored run workspace')
    parser.add_argument('--model',default='chatgpt/gpt-5.6-sol')
    parser.add_argument('--backend',default='http://127.0.0.1:6800')
    parser.add_argument('--target',default='127.0.0.1:5556')
    parser.add_argument('--expected-device-serial',help='Required serial read from this dedicated container')
    parser.add_argument('--environment-device',default='emulator-5554')
    parser.add_argument('--container',default='mobile_world_env_0')
    parser.add_argument('--collector-apk',type=Path)
    parser.add_argument('--ime-apk',type=Path)
    parser.add_argument('--no-recording',action='store_true')
    args = parser.parse_args()
    root = args.output_root.resolve()
    prepared = prepare(root)
    runtime = prepared/'runtime'
    profile = read(prepared/'profile.json')
    if args.install:
        subprocess.run([sys.executable,'-m','pip','install','-e',str(runtime)+'[decode]','requests>=2.32,<3'],check=True)
    if args.prepare_only:
        print(json.dumps({'prepared':True,'tasks':117,'variants':9,'device_access':False,'model_access':False}))
        return
    if not args.expected_device_serial or not args.collector_apk or not args.ime_apk:
        parser.error('Execution requires --expected-device-serial, --collector-apk and --ime-apk')
    for apk in (args.collector_apk,args.ime_apk):
        if not apk.is_file(): parser.error('APK not found: '+str(apk))
    env_file = args.env_file.resolve() if args.env_file else None
    identity = {'input':digest(root/'input-seal.json'), 'profile':digest(prepared/'profile.json'),
                'model':args.model,'variant':args.variant,'backend':args.backend,
                'target':args.target,'serial':args.expected_device_serial,'container':args.container,
                'environment_device':args.environment_device,'recording':not args.no_recording,
                'collector':digest(args.collector_apk),'ime':digest(args.ime_apk),
                'config':digest(env_file) if env_file else None,
                'environment_config':hashlib.sha256(json.dumps({k:v for k,v in os.environ.items()
                    if k.startswith('CLICKCLICK_')},sort_keys=True).encode()).hexdigest()}
    identity_path = root/'run-identity.json'
    if identity_path.exists():
        if not args.resume: parser.error('Run already exists; use --resume to preserve its outcomes')
        if read(identity_path) != identity: raise RuntimeError('Resume configuration changed')
    else:
        if args.resume: parser.error('No run exists to resume')
        write(identity_path,identity)
    if env_file: shutil.copyfile(env_file,runtime/'.env')
    elif (runtime/'.env').exists(): raise RuntimeError('Unexpected local configuration')
    arms = ['original','clarified'] if args.variant == 'paired' else [args.variant]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(runtime)
    env['CLICKCLICK_SKILLS_DIR'] = str(runtime/'skills')
    for arm in arms:
        case_names = list(profile['tasks']) if arm == 'original' else profile['clarified_tasks']
        cases = [{'task':name,'difficulty':'GUI-only','max_steps':50,'max_seconds':2400} for name in case_names]
        cases_path = root/(arm+'-cases.json')
        write(cases_path,cases)
        command = [sys.executable,str(prepared/'worker.py'),'--variant',arm,
                   '--profile',str(prepared/'profile.json'),'--model',args.model,
                   '--cases',str(cases_path),'--output-root',str(root/arm),
                   '--backend',args.backend,'--target',args.target,
                   '--expected-device-serial',args.expected_device_serial,
                   '--environment-device',args.environment_device,'--container',args.container,
                   '--collector-apk',str(args.collector_apk.resolve()),'--ime-apk',str(args.ime_apk.resolve())]
        if args.no_recording: command.append('--no-recording')
        result = subprocess.run(command,cwd=runtime,env=env)
        summary = summarize(root,profile)
        print(json.dumps(summary,ensure_ascii=False))
        if result.returncode: raise SystemExit(result.returncode)
        if (root/arm/'STOP').exists() or (root/arm/'PAUSE_AFTER_EPISODE').exists(): return
        if arm == 'original' and not summary['original']['complete']: return


if __name__ == '__main__':
    main()
