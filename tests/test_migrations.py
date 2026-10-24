import io

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_models_match_migrations():
    out = io.StringIO()
    try:
        call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
    except SystemExit as exc:  # makemigrations --check exits 1 when changes are detected
        pytest.fail(f"Models and migrations are out of sync:\n{out.getvalue()}\n(exit {exc.code})")
