"""Worker healthcheck: the heartbeat file must have been touched recently.

``run_worker`` updates ``WORKER_HEARTBEAT_FILE`` on every loop iteration. The
check fails if the file is missing or older than ``WORKER_HEARTBEAT_MAX_AGE``
seconds (default 60). Exit status 0 = healthy, 1 = unhealthy. Standard
library only; no Django import so the check is cheap and has no DB access.
"""
import os
import sys
import time

DEFAULT_FILE = "/tmp/worker-heartbeat"
DEFAULT_MAX_AGE = 60


def main() -> int:
    path = os.environ.get("WORKER_HEARTBEAT_FILE") or DEFAULT_FILE
    try:
        max_age = float(os.environ.get("WORKER_HEARTBEAT_MAX_AGE") or DEFAULT_MAX_AGE)
    except ValueError:
        max_age = DEFAULT_MAX_AGE
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        print(f"unhealthy: heartbeat file {path} missing", file=sys.stderr)
        return 1
    if age > max_age:
        print(f"unhealthy: heartbeat is {age:.0f}s old (limit {max_age:.0f}s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
