"""Restore recorded file absence in a dedicated, disposable AndroidWorld lab.

No app-specific filenames or semantic fingerprint exceptions. Existing baseline files,
directories, maps, preferences and database rows remain the official initializer's
responsibility; its complete measured state must still match after restoration.
"""
from pathlib import PurePosixPath
import re
import shlex
import subprocess


def owned_file_path(path):
    return (isinstance(path, str) and PurePosixPath(path).as_posix() == path
        and '..' not in PurePosixPath(path).parts and not any(c in path for c in '\r\n\x00')
        and re.fullmatch(r'/(?:data/data|data/user/0|data/media/0/Android/data|storage/emulated/0/Android/data)/'
            r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+/files/.+', path) is not None)


def absent_owned_files(state):
    paths = []
    for entry in state.get('files', []):
        if not isinstance(entry, dict) or entry.get('exists') is not False:
            continue
        path = entry.get('path')
        if (not owned_file_path(path)
                or entry.get('sha256') is not None or entry.get('bytes') is not None):
            raise ValueError('Recorded absent file is not an unambiguous owned app file')
        if path not in paths:
            paths.append(path)
    return paths


def restore_recorded_absence(adb, state):
    if not isinstance(state, dict):
        return {'checked_files':0,'removed_files':0,'empty_scope_restored':False}
    paths = absent_owned_files(state)  # Validate the complete set before mutation.
    empty_scope = None
    if (state.get('schema') == 'official-task-owned-files-v1'
            and state.get('files') == [] and state.get('directories') == []):
        empty_scope = state.get('scope')
        if not owned_file_path(empty_scope):
            raise ValueError('Recorded empty scope is not an owned app files subdirectory')
    if not paths and empty_scope is None:
        return {'checked_files': 0, 'removed_files': 0, 'empty_scope_restored': False}
    prefix = [adb, '-s', 'emulator-5554']
    identity = subprocess.check_output([*prefix, 'emu', 'avd', 'name'], timeout=10).decode().splitlines()[0].strip()
    if identity != 'AndroidWorldAvd':
        raise ValueError('Recorded absence restoration requires the dedicated AndroidWorldAvd')
    if subprocess.check_output([*prefix, 'shell', 'id', '-u'], timeout=10).strip() != b'0':
        raise ValueError('Recorded absence restoration requires existing rooted lab access')
    removed = 0
    for path in paths:
        quoted = shlex.quote(path)
        # Reject symlinks/directories; unlink precisely one baseline-absent file.
        # No recursive delete, wildcard, root escalation or agent-selected path.
        command = (f'if [ -L {quoted} ] || [ -d {quoted} ]; then exit 65; '
            f'elif [ -e {quoted} ]; then rm -f -- {quoted} || exit; '
            f'[ ! -e {quoted} ] && [ ! -L {quoted} ] || exit 66; printf removed; '
            'else printf absent; fi')
        status = subprocess.check_output([*prefix, 'shell', command], timeout=10).strip()
        if status not in {b'removed', b'absent'}:
            raise ValueError('Unrecognized recorded absence restoration receipt')
        removed += status == b'removed'
    if empty_scope is not None:
        quoted = shlex.quote(empty_scope)
        # The recorded directory was empty. Remove only current regular files;
        # unexpected symlinks or subdirectories block completion, never recurse.
        command = (f'if [ -L {quoted} ]; then exit 65; elif [ -d {quoted} ]; then '
            f'[ -z "$(find {quoted} -mindepth 1 -maxdepth 1 ! -type f -print -quit)" ] || exit 65; '
            f'find {quoted} -mindepth 1 -maxdepth 1 -type f -exec rm -f -- {{}} \\; || exit; '
            f'[ -z "$(find {quoted} -mindepth 1 -maxdepth 1 -print -quit)" ] || exit 67; '
            'printf empty; else printf absent; fi')
        status = subprocess.check_output([*prefix, 'shell', command], timeout=10).strip()
        if status not in {b'empty', b'absent'}:
            raise ValueError('Unrecognized empty fixture scope restoration receipt')
    return {'checked_files': len(paths), 'removed_files': removed,
        'empty_scope_restored':empty_scope is not None}
