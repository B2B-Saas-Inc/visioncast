"""Print the application version (``VERSION`` file) and, when the image was built by
``deploy/build-image.sh``, the git revision and build time stamped into it.

    python manage.py show_version            # 0.2.0-pre
    python manage.py show_version --verbose  # version, revision, built (ISO 8601), Django version
"""
import os

import django
from django.core.management.base import BaseCommand

from webinars import __version__


class Command(BaseCommand):
    help = "Print the webinar platform version."

    def add_arguments(self, parser):
        parser.add_argument("--verbose", action="store_true", help="Also print revision, build time and Django version.")

    def handle(self, *args, **options):
        if not options["verbose"]:
            self.stdout.write(__version__)
            return
        self.stdout.write(f"version: {__version__}")
        self.stdout.write(f"revision: {os.environ.get('WEBINAR_BUILD_REVISION') or 'unknown'}")
        self.stdout.write(f"built: {os.environ.get('WEBINAR_BUILD_CREATED') or 'unknown'}")
        self.stdout.write(f"django: {django.get_version()}")
