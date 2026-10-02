"""Frozen runtime launcher; official init/eval/teardown stay outside the agent."""
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse


def pop(name):
    index = sys.argv.index(name)
    value = sys.argv[index+1]
    del sys.argv[index:index+2]
    return value


def deliver(profile, variant, task, official):
    row = profile['tasks'][task]
    if official not in row['accepted_backend_goals']:
        raise RuntimeError('Official task wording changed: '+task)
    return row['original_goal'] if variant == 'original' else row['clarified_goal']


def verify_backend_source(profile, container):
    paths = {}
    for task,row in profile['tasks'].items():
        path = urlparse(row['source']).path.split('/src/mobile_world/',1)[1]
        if not path.startswith('tasks/definitions/') or '..' in path.split('/'):
            raise RuntimeError('Unexpected task source path')
        paths[task] = path
    code = ('import hashlib,json;from pathlib import Path;'
            'paths=json.loads('+repr(json.dumps(paths))+');'
            'root=Path("/app/service/src/mobile_world");'
            'print(json.dumps({name:hashlib.sha256((root/path).read_bytes()).hexdigest() '
            'for name,path in paths.items()}))')
    result = subprocess.run(['docker','exec',container,'/app/service/.venv/bin/python','-c',code],
                            check=True,capture_output=True,text=True,encoding='utf8',timeout=60)
    actual = json.loads(result.stdout)
    if set(actual) != set(profile['tasks']): raise RuntimeError('Incomplete backend source verification')
    for name,row in profile['tasks'].items():
        accepted = {row['source_file_sha256'],row['container_source_sha256']}
        if actual[name] not in accepted:
            raise RuntimeError('Official task implementation changed: '+name)


def main():
    variant, profile_path = pop('--variant'), Path(pop('--profile')).resolve()
    profile = json.loads(profile_path.read_text(encoding='utf8'))
    single = '--single' in sys.argv
    if single: sys.argv.remove('--single')
    runtime = profile_path.parent/'runtime'
    sys.path.insert(0,str(runtime))
    import agent.prompts as prompts
    import agent.revisable.roles as roles
    from shared.config import get_settings
    prompts._PROMPTS_DIR = roles.PROMPTS = runtime/'agent/prompts'
    # Override semantic settings after reading local auth/provider configuration.
    import evaluation.mobileworld.run_single as episode
    original_settings = get_settings
    def settings():
        value = original_settings()
        value.agent_architecture = 'plan_executor'
        value.executor_context_tokens = 16000
        value.chatgpt_history_tokens = 16000
        value.compaction_attempt_notes = False
        value.driver_url = ''
        value.driver_urls_json = ''
        value.use_fixture_driver = False
        model = sys.argv[sys.argv.index('--model')+1]
        providers = json.loads(value.models_json or '{}')
        provider = providers.setdefault(model,{})
        provider['reasoning'] = {'effort':'high','summary':'concise'}
        provider['context'] = {'history_tokens':16000}
        value.models_json = json.dumps(providers)
        return value
    episode.get_settings = settings
    if single:
        original_request = episode._request
        def request(method,url,**kwargs):
            result = original_request(method,url,**kwargs)
            if method == 'GET' and url.endswith('/task/goal'):
                return deliver(profile,variant,kwargs['params']['task_name'],result)
            return result
        episode._request = request
        original_run = episode._run_clickclick
        async def run(args,goal):
            if args.task not in profile['fixture_context_tasks']:
                return await original_run(args,goal)
            from evaluation.mobileworld.fixture_context import prepare_fixture_context,fixture_prompt_context
            from evaluation.mobileworld.long_run_health import MobileWorldTarget
            target = MobileWorldTarget(args.backend,args.target,args.expected_device_serial,args.environment_device,args.container)
            report = prepare_fixture_context(target,['Mastodon'],Path(args.data_dir))
            with fixture_prompt_context(report,Path(args.data_dir)):
                return await original_run(args,goal)
        episode._run_clickclick = run
        episode.main()
    else:
        if not any(flag in sys.argv for flag in ('--help','-h')):
            container = sys.argv[sys.argv.index('--container')+1]
            verify_backend_source(profile,container)
        from evaluation.mobileworld import run_full
        original_request = run_full.request
        def request(method,url,**kwargs):
            result = original_request(method,url,**kwargs)
            if method == 'GET' and url.endswith('/task/goal'):
                return deliver(profile,variant,kwargs['params']['task_name'],result)
            return result
        run_full.request = request
        original_subprocess = subprocess.run
        def launch(command,*args,**kwargs):
            if isinstance(command,list) and command[1:3] == ['-m','evaluation.mobileworld.run_single']:
                command = [command[0],str(Path(__file__).resolve()),'--single','--variant',variant,
                           '--profile',str(profile_path),*command[3:]]
            return original_subprocess(command,*args,**kwargs)
        subprocess.run = launch
        run_full.main()


if __name__ == '__main__':
    main()
