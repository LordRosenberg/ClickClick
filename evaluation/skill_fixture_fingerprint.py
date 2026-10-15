"""Opt-in self-learning diagnostics for official task-owned file fixtures.

No task behavior, oracle, prompt or action changes. Snapshot data stays with the
evaluator; source and candidate must use the same declared scope and protocol.
"""
from hashlib import sha256
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import json


def recording_settings_fingerprint(directory):
    """Bind persisted encoding/storage options, not transient recording identity."""
    selected=[]
    for path in sorted(Path(directory).glob('*.xml')):
        for node in ET.parse(path).getroot():
            key=node.get('name','')
            if re.search(r'format|encoder|encoding|bitrate|sample.?rate|channel|audio.?source|storage|extension',key,re.I):
                value={'type':node.tag,'attributes':{k:v for k,v in node.attrib.items() if k!='name'},
                    'text':node.text if node.tag=='string' else None,
                    'items':sorted(child.text or '' for child in node)}
                selected.append({'file':path.name,'key':key,
                    'value_sha256':sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()})
    return selected


def directory_fingerprint(directory):
    root=Path(directory).resolve(strict=True)
    if not root.is_dir():raise ValueError('fixture root is not a directory')
    files=[];directories=[]
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('fixture snapshot cannot follow links outside owned data')
        relative=path.relative_to(root).as_posix()
        if path.is_dir():directories.append(relative)
        elif path.is_file():
            content=path.read_bytes()
            files.append({'name':relative,'bytes':len(content),'sha256':sha256(content).hexdigest()})
        else:raise ValueError('unsupported fixture entry')
    return {'directories':directories,'files':files}


def capture_owned_file_fixture(task,env):
    apps=set(task.app_names)
    # Exact single-app scopes only: a Markor fingerprint cannot cover clipboard,
    # SMS, gallery or expense state in a composite task.
    if apps not in ({'audio recorder'},{'markor'}):return None
    from android_world.env import device_constants
    from android_world.utils import file_utils
    app=next(iter(apps))
    data_path=(device_constants.AUDIORECORDER_DATA if app=='audio recorder'
        else device_constants.MARKOR_DATA)
    with file_utils.tmp_directory_from_device(data_path,env.controller) as copied:
        data=directory_fingerprint(copied)
    settings=None
    if app=='audio recorder':
        preferences='/data/data/com.dimowner.audiorecorder/shared_prefs'
        if file_utils.check_directory_exists(preferences,env.controller):
            with file_utils.tmp_directory_from_device(preferences,env.controller) as copied:
                settings={'status':'observed','entries':recording_settings_fingerprint(copied)}
        else:settings={'status':'absent','entries':[]}
    return {'schema':'official-task-owned-files-v1','app':app,'scope':data_path,
        **data,'recording_settings':settings,
        'coverage':'Owned file names/topology/bytes; AudioRecorder persisted format/encoder/bitrate/sample-rate/channel/audio-source/storage/extension keys when present. Excludes timestamps, recording IDs and unlisted preferences. Evaluator-only; no raw setting values or oracle answers. A changed observed setting must be restored by GUI before reset verification; the task initializer is not a whole-app preferences rollback.'}
