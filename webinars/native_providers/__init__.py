"""Self-contained native provider modules, one per vendor (docs/native-integrations.md).

``webinars.native_integrations`` imports this package at the end of its own import and
registers every module listed in ``PROVIDER_MODULES`` (schema, hosts, run and preview hooks).
A module imports the shared helpers *from* ``webinars.native_integrations`` (``Field``,
``PROCESSING_ACK``, ``Skip``, ``_Stop``, ``Outcome``, ``_headers``, ``_first_id`` …); every
name it needs is defined before the registration import runs.

Add a vendor by writing ``webinars/native_providers/<provider>.py`` and listing it here.
"""
from importlib import import_module

PROVIDER_MODULES = ("activecampaign", "mailchimp", "salesforce", "pipedrive", "resend", "ses")


def modules():
    return [import_module(f"webinars.native_providers.{name}") for name in PROVIDER_MODULES]
