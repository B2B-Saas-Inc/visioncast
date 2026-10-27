"""Hosting providers (Zoom, Wistia, Sequel, Riverside, StreamYard). See docs/hosting.md.

``ADAPTERS`` maps the ``HostingAccount.provider`` value to its adapter; ``service`` holds
the provisioning state machine (preview -> confirm -> execute, reconciliation).
"""
from .riverside import RiversideAdapter
from .sequel import SequelAdapter
from .streamyard import StreamYardAdapter
from .wistia import WistiaAdapter
from .zoom import ZoomAdapter

ADAPTERS = {
    a.provider: a
    for a in (ZoomAdapter(), WistiaAdapter(), SequelAdapter(), RiversideAdapter(), StreamYardAdapter())
}


def get_adapter(provider):
    return ADAPTERS.get(provider)
