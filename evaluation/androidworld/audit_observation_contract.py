"""Static compatibility gate for the pinned official observation consumers.

This detects direct State reads and converter node fields; it complements live
regressions, not dynamic Python execution or app-level correctness tests.
"""
import ast
import hashlib
import re
from pathlib import Path


def audit_observation_contract(official_root: Path, java_source: Path) -> dict:
    consumers = []
    hashes = {}
    def get_state_call(node):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get_state'
    for path in sorted((official_root / 'task_evals').rglob('*.py')):
        if path.name.endswith('_test.py'):
            continue
        module = ast.parse(path.read_text(encoding='utf-8'))
        for function in ast.walk(module):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(function) if get_state_call(n)]
            if not calls:
                continue
            variables = {t.id for n in ast.walk(function) if isinstance(n, ast.Assign) and get_state_call(n.value)
                         for t in n.targets if isinstance(t, ast.Name)}
            fields = {n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute) and (
                get_state_call(n.value) or isinstance(n.value, ast.Name) and n.value.id in variables)}
            unsupported = fields - {'forest', 'ui_elements', 'pixels'}
            if unsupported:
                raise ValueError(f'Unreviewed official State fields: {path.name}:{function.name}: {unsupported}')
            consumers.append(dict(file=path.relative_to(official_root).as_posix(),
                                  function=function.name, fields=sorted(fields), reads=len(calls)))
            hashes[path.relative_to(official_root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    converter = official_root / 'env/representation_utils.py'
    functions = [n for n in ast.walk(ast.parse(converter.read_text(encoding='utf-8')))
                 if isinstance(n, ast.FunctionDef) and n.name in ('accessibility_node_to_ui_element', 'forest_to_ui_elements')]
    required = {n.attr for f in functions for n in ast.walk(f)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'node'}
    supplied = set(re.findall(r'row\.put\("([^"]+)"', java_source.read_text(encoding='utf-8')))
    if required - supplied:
        raise ValueError(f'Native collector omits official converter fields: {required - supplied}')
    if not consumers or len(functions) != 2:
        raise ValueError('Official observation layout changed; review before freezing')
    hashes['env/representation_utils.py'] = hashlib.sha256(converter.read_bytes()).hexdigest()
    return dict(consumers=consumers, required_native_fields=sorted(required), source_hashes=hashes,
                limits='Direct State reads and official conversion only; runtime branches require live tests')
