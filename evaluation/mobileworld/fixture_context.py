"""Public fixture login information for MobileWorld, never task answers.

Run after every official task initialization, including after a container restart.
Validate documented credentials read-only; GUI login remains an agent action.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess

from evaluation.mobileworld.long_run_health import MobileWorldTarget, atomic_json


SOURCE = "https://github.com/Tongyi-MAI/MobileWorld/blob/e41d1478e252325c513003d3d191b4c164b4af2c/docker/mastodon-docker/readme.md"
ACCOUNTS = {"owner": "owner@gmail.com", "demo": "demo@gmail.com", "test": "test@gmail.com"}
HEADER = "## MobileWorld fixture account information"
CONTEXT = """## MobileWorld fixture account information

Official Mastodon fixture at https://10.0.2.2:
- Administrator owner: owner@gmail.com / password
- Example user demo: demo@gmail.com / password
- Test user test: test@gmail.com / password
These are the benchmark fixture login credentials. They do not change which account the task requests. Perform any required login through the GUI.
"""

# Only booleans and a public-document hash leave the container. No account rows,
# password hashes, cookies, tokens, task data, or login requests are returned.
PROBE = r'''
import crypt, hashlib, json
from pathlib import Path
from mobile_world.runtime.app_helpers import mastodon
expected = {'owner': 'owner@gmail.com', 'demo': 'demo@gmail.com', 'test': 'test@gmail.com'}
p = Path('/app/mastodon-docker/readme.md')
raw = p.read_bytes()
doc = raw.decode('utf8')
conn, cursor = mastodon.connect_to_postgres()
if conn is None or cursor is None:
    raise RuntimeError('fixture database unavailable')
try:
    cursor.execute('SET TRANSACTION READ ONLY')
    checks = {}
    for username, email in expected.items():
        cursor.execute('SELECT u.email, u.encrypted_password, u.confirmed_at, u.approved, '
                       'u.disabled FROM users u JOIN accounts a ON u.account_id=a.id '
                       'WHERE a.username=%s AND a.domain IS NULL', (username,))
        rows = cursor.fetchall()
        row = rows[0] if len(rows) == 1 else None
        checks[username] = {
            'documented': (email + ' / password') in doc,
            'email_matches': bool(row and row[0] == email),
            'password_matches': bool(row and crypt.crypt('password', row[1]) == row[1]),
            'enabled': bool(row and row[2] and row[3] and not row[4]),
        }
    print(json.dumps({'document_sha256': hashlib.sha256(raw).hexdigest(), 'accounts': checks}))
finally:
    conn.rollback()
    cursor.close()
    conn.close()
'''


def prepare_fixture_context(target: MobileWorldTarget, apps: list[str], data_dir: Path) -> dict:
    """Fail closed before model execution if documented login data does not work."""
    if "Mastodon" not in apps:
        return {"required": False, "ready": True}
    report = {"required": True, "ready": False, "source": SOURCE,
              "version": "mastodon-public-fixture-accounts-v1",
              "model_visible_context_changed": True, "original_goal_unchanged": True,
              "automatic_login": False, "browser_authenticated": "not_asserted",
              "context_sha256": hashlib.sha256(CONTEXT.encode()).hexdigest()}
    try:
        process = subprocess.run(
            ["docker", "exec", target.container, "/app/service/.venv/bin/python", "-c", PROBE],
            capture_output=True, text=True, encoding="utf8", timeout=30,
        )
        if process.returncode:
            raise RuntimeError("fixture probe failed")
        probe = json.loads(process.stdout.strip())
        checks = probe["accounts"]
        if set(checks) != set(ACCOUNTS):
            raise ValueError("incomplete fixture account checks")
        fields = {"documented", "email_matches", "password_matches", "enabled"}
        if any(set(row) != fields or any(type(v) is not bool for v in row.values())
               for row in checks.values()):
            raise ValueError("invalid fixture account evidence")
        report.update(accounts=checks, document_sha256=probe["document_sha256"],
                      ready=all(all(row.values()) for row in checks.values()))
        if not report["ready"]:
            report["failure"] = "documented_fixture_credentials_unavailable"
    except Exception as exc:
        report.update(failure="fixture_credentials_probe_failed", error_type=type(exc).__name__)
    atomic_json(Path(data_dir) / "fixture-context.json", report)
    return report


@contextmanager
def fixture_prompt_context(report: dict, data_dir: Path):
    """Append verified fixture information only inside this evaluation process.

Preserve private/frozen prompt overrides. Restore both prompt readers on error.
The task instruction stays byte-for-byte unchanged in state and scoring records.
"""
    if not report.get("required"):
        yield
        return
    if report.get("ready") is not True:
        raise RuntimeError("Refusing model execution without verified fixture context")
    import agent.prompts as prompts
    import agent.revisable.roles as roles
    previous_prompts, previous_roles = prompts._PROMPTS_DIR, roles.PROMPTS
    output = Path(data_dir) / "environment-prompts"
    output.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in ("revisable_common.md", "revisable_planner.md", "revisable_reviewer.md", "revisable_executor.md"):
        original = (previous_prompts / name).read_text(encoding="utf8")
        if original != (previous_roles / name).read_text(encoding="utf8"):
            raise RuntimeError("Prompt readers disagree before fixture context delivery")
        if HEADER in original:
            raise RuntimeError("Fixture context already present; refusing duplicate or conflicting injection")
        rendered = original + "\n\n" + CONTEXT if name == "revisable_common.md" else original
        (output / name).write_text(rendered, encoding="utf8")
        hashes[name] = {"baseline": hashlib.sha256(original.encode()).hexdigest(),
                        "delivered": hashlib.sha256(rendered.encode()).hexdigest()}
    atomic_json(Path(data_dir) / "fixture-context-delivery.json", {
        "source": SOURCE, "context": CONTEXT, "prompt_hashes": hashes,
        "original_goal_unchanged": True, "automatic_login": False,
    })
    try:
        prompts._PROMPTS_DIR = output
        roles.PROMPTS = output
        yield
    finally:
        prompts._PROMPTS_DIR, roles.PROMPTS = previous_prompts, previous_roles
