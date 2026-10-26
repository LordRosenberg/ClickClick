"""POSIX input supervisor; its process group survives backend loss until drained.

The child lock is acquired before checking the fence. A delayed supervisor
cannot start input after takeover, even if it had not registered its PID yet.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from driver.execution_journal import ExecutionJournal
from shared.file_lock import FileLock


def main():
    root,envelope_text,*command=sys.argv[1:]
    envelope=json.loads(envelope_text)
    os.setsid()
    journal=ExecutionJournal(Path(root))
    with FileLock(journal.root/f"child-{envelope['operation_id']}.lock"):
        journal.check(envelope)
        journal.register_child(os.getpid(),envelope)
        try:
            # stdout/stderr remain the original pipes; no credential or command
            # payload is written to the execution ledger.
            process=subprocess.Popen(command)
            return process.wait()
        finally:
            journal.unregister_child(os.getpid())


if __name__=="__main__":
    sys.exit(main())
