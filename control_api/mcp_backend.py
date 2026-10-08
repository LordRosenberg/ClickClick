"""Start only after confirming a Windows client's job cannot kill this backend."""

import os


def main():
    if os.name == "nt":
        import pywintypes
        import win32job
        try:
            limits = win32job.QueryInformationJobObject(
                None, win32job.JobObjectExtendedLimitInformation)
        except (OSError, pywintypes.error) as exc:
            # ERROR_ACCESS_DENIED is the documented result when not in a job.
            if getattr(exc, "winerror", None) != 5:
                raise
        else:
            # Nested jobs can keep a child inside an outer client job even if
            # CREATE_BREAKAWAY_FROM_JOB succeeded for the immediate parent job.
            if limits["BasicLimitInformation"]["LimitFlags"] & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
                raise SystemExit(78)
    from control_api.main import main as run_backend
    run_backend()


if __name__ == "__main__":
    main()
