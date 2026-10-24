"""Durable outbox / reminder worker.

    python manage.py run_worker [--once] [--interval 5] [--worker-id ID]

Stops gracefully on SIGTERM/SIGINT after the current batch. Touches
``settings.WORKER_HEARTBEAT_FILE`` after every loop for the container healthcheck.
Logs contain only counts and ids, never PII.
"""
import logging
import os
import signal
import socket
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from webinars.delivery import run_once

logger = logging.getLogger("webinars.worker")


def _write_heartbeat(path):
    if not path:
        return
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="ascii") as fh:
            fh.write(str(int(time.time())))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write heartbeat file: %s", type(exc).__name__)


class Command(BaseCommand):
    help = "Run the durable outbox delivery and reminder worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one batch and exit.")
        parser.add_argument("--interval", type=float, default=5.0, help="Seconds to sleep when idle (default 5).")
        parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}"[:128])

    def handle(self, *args, **options):
        worker_id = options["worker_id"][:128]
        interval = max(0.1, float(options["interval"]))
        heartbeat = getattr(settings, "WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat")
        stop = threading.Event()

        def _stop(signum, _frame):
            logger.info("Worker %s received signal %s; stopping after current batch", worker_id, signum)
            stop.set()

        if not options["once"]:
            signal.signal(signal.SIGTERM, _stop)
            signal.signal(signal.SIGINT, _stop)

        logger.info("Worker %s started", worker_id)
        while not stop.is_set():
            close_old_connections()
            processed = 0
            try:
                processed = run_once(worker_id)
            except Exception as exc:  # DB outage etc.: keep running, never log exception text
                logger.error("Worker loop error: %s", type(exc).__name__)
            finally:
                close_old_connections()
            _write_heartbeat(heartbeat)
            if processed:
                logger.info("Worker %s processed %d message(s)", worker_id, processed)
            if options["once"]:
                break
            if not processed:
                stop.wait(interval)
        logger.info("Worker %s stopped", worker_id)
        if options["once"]:
            self.stdout.write(f"processed={processed}")
