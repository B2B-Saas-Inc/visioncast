"""MCP parity for hosting accounts, hosting reconcile/manual links and order bookkeeping:
scopes, gated dry runs (no mutation), confirmation binding, secret redaction, guides."""
import uuid
from pathlib import Path

import pytest

from tests.conftest import make_api_key, make_event, make_form, make_occurrence, make_registration
from tests.test_mcp import tool_names
from tests.test_mcp_config import call, confirm, run, text_of
from webinars.hosting import base, service
from webinars.models import HostedSession, HostingAccount, TicketOrder

pytestmark = pytest.mark.django_db

ZOOM_SECRETS = {"account_id": "acct_123456", "client_id": "client-id-abc", "client_secret": "client-secret-xyz"}
JOIN = "https://us02web.zoom.us/j/81234567890?pwd=room-passcode-123"


@pytest.fixture(autouse=True)
def hosting_settings(settings):
    settings.WEBHOOK_HOST_ALLOWLIST = ["zoom.us", "api.zoom.us", "us02web.zoom.us"]
    base.clear_token_cache()
    yield settings
    base.clear_token_cache()


@pytest.fixture
def admin_key(db):
    return make_api_key(("hosting_admin",), name="hadm")


@pytest.fixture
def provision_key(db):
    return make_api_key(("hosting_provision",), name="hprov")


@pytest.fixture
def pay_key(db):
    return make_api_key(("payments_admin",), name="pay")


@pytest.fixture
def occ(db):
    return make_occurrence(make_event(make_form(version="mx1"), slug="mcp-hosted"), join_url="")


def _zoom_account():
    account, _w = service.save_account(provider="zoom", name="zoom acct", config={"host_user_id": "host@example.com"},
                                       posted_secrets=ZOOM_SECRETS, enabled=False)
    return account


def _account_args():
    return {"provider": "zoom", "name": "Zoom main", "enabled": False,
            "config": {"host_user_id": "host@example.com"}, "credentials": dict(ZOOM_SECRETS)}


# ---------------------------------------------------------------- registry parity


def test_tools_listed_only_for_their_scopes(client, admin_key, provision_key, pay_key):
    admin = tool_names(client, admin_key)
    assert {"list_hosting_accounts", "create_hosting_account", "update_hosting_account"} <= admin
    assert not {"execute_session_hosting", "mark_order_refunded"} & admin
    prov = tool_names(client, provision_key)
    assert {"execute_session_hosting", "reconcile_session_hosting", "set_session_manual_link"} <= prov
    assert not {"create_hosting_account", "release_unpaid_order"} & prov
    pay = tool_names(client, pay_key)
    assert {"mark_order_refunded", "release_unpaid_order", "list_session_orders"} <= pay
    assert not {"create_hosting_account", "reconcile_session_hosting"} & pay


def test_management_routes_have_mcp_parity():
    from webinars.api import hosting, tickets
    from webinars.mcp_config import TOOLS

    handlers = {t["name"]: t for t in TOOLS}
    # Every essential management route is reachable through a named tool with the route's scopes.
    expected = {
        "list_hosting_accounts": hosting.hosting_accounts, "create_hosting_account": hosting.hosting_accounts,
        "update_hosting_account": hosting.hosting_account_detail, "execute_session_hosting": hosting.occurrence_hosting_execute,
        "reconcile_session_hosting": hosting.occurrence_hosting_reconcile,
        "set_session_manual_link": hosting.occurrence_hosting_manual_link,
        "mark_order_refunded": tickets.order_mark_refunded, "release_unpaid_order": tickets.order_release,
        "set_payment_account": tickets.payment_account, "list_session_orders": tickets.occurrence_orders,
    }
    for name in expected:
        assert name in handlers, name
    for name in ("create_hosting_account", "update_hosting_account", "reconcile_session_hosting",
                 "set_session_manual_link", "mark_order_refunded", "release_unpaid_order"):
        tool = handlers[name]
        assert tool.get("gated") and "dryRun" in tool["inputSchema"]["properties"], name
        assert tool["inputSchema"]["additionalProperties"] is False, name
    assert set(handlers["create_hosting_account"]["scopes"]) == {"hosting_admin"}
    assert set(handlers["reconcile_session_hosting"]["scopes"]) == {"hosting_provision"}
    assert set(handlers["mark_order_refunded"]["scopes"]) == {"payments_admin"}


def test_scope_denied_for_wrong_key(client, provision_key, pay_key, admin_key, occ):
    # Denied before the handler runs (JSON-RPC error, not a tool result); a header token cannot help.
    for key, name, args in (
        (provision_key, "create_hosting_account", {**_account_args(), "dryRun": False, "confirmToken": "x"}),
        (admin_key, "mark_order_refunded", {"orderId": str(uuid.uuid4())}),
        (pay_key, "reconcile_session_hosting", {"occurrenceId": str(occ.pk), "decision": "not_created"}),
        (pay_key, "create_hosting_account", _account_args()),
    ):
        data = call(client, key, name, args)
        assert "error" in data and "result" not in data, (name, data)
    assert not HostingAccount.objects.exists()


def test_schema_rejects_unknown_fields(client, admin_key):
    out = run(client, admin_key, "create_hosting_account", {**_account_args(), "credentialsEncrypted": "x"})
    assert out["isError"]
    assert not HostingAccount.objects.exists()


# ---------------------------------------------------------------- hosting accounts


def test_create_account_dry_run_writes_nothing_and_hides_secrets(client, admin_key):
    dry = run(client, admin_key, "create_hosting_account", _account_args())
    assert not dry["isError"], dry
    assert not HostingAccount.objects.exists()
    text = text_of(dry)
    for secret in ZOOM_SECRETS.values():
        assert secret not in text
    assert sorted(dry["structuredContent"]["preview"]["changes"]["credentialsSet"]) == sorted(ZOOM_SECRETS)


def test_create_account_confirmed_returns_flags_only(client, admin_key):
    _dry, done = confirm(client, admin_key, "create_hosting_account", _account_args())
    assert not done["isError"], done
    body = done["structuredContent"]
    assert body["secretsSet"] == {k: True for k in ZOOM_SECRETS}
    for secret in ZOOM_SECRETS.values():
        assert secret not in text_of(done)
    listed = run(client, admin_key, "list_hosting_accounts")
    assert not listed["isError"]
    for secret in ZOOM_SECRETS.values():
        assert secret not in text_of(listed)


def test_create_account_token_bound_to_args_and_key(client, admin_key):
    dry = run(client, admin_key, "create_hosting_account", _account_args())
    token = dry["structuredContent"]["confirmToken"]
    tampered = run(client, admin_key, "create_hosting_account",
                   {**_account_args(), "name": "Other", "dryRun": False, "confirmToken": token})
    assert tampered["isError"]
    other = make_api_key(("hosting_admin",), name="hadm2")
    stolen = run(client, other, "create_hosting_account", {**_account_args(), "dryRun": False, "confirmToken": token})
    assert stolen["isError"]
    assert not HostingAccount.objects.exists()
    done = run(client, admin_key, "create_hosting_account", {**_account_args(), "dryRun": False, "confirmToken": token})
    assert not done["isError"], done
    replay = run(client, admin_key, "create_hosting_account", {**_account_args(), "dryRun": False, "confirmToken": token})
    assert replay["isError"]
    assert HostingAccount.objects.count() == 1


def test_update_account_token_invalidated_by_state_change(client, admin_key):
    account = _zoom_account()
    args = {"accountId": str(account.pk), "name": "Renamed"}
    dry = run(client, admin_key, "update_hosting_account", args)
    assert not dry["isError"], dry
    assert HostingAccount.objects.get(pk=account.pk).name == "zoom acct"
    # Someone else rotates the secret in between: the old token no longer matches.
    service.save_account(account, provider="zoom", name="zoom acct", config={"host_user_id": "host@example.com"},
                         posted_secrets={"client_secret": "rotated-secret"}, enabled=False)
    stale = run(client, admin_key, "update_hosting_account",
                {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert stale["isError"]
    assert HostingAccount.objects.get(pk=account.pk).name == "zoom acct"
    _dry, done = confirm(client, admin_key, "update_hosting_account", args)
    assert not done["isError"], done
    assert HostingAccount.objects.get(pk=account.pk).name == "Renamed"


# ---------------------------------------------------------------- reconcile / manual link


def test_reconcile_gated_and_state_bound(client, provision_key, occ):
    account = _zoom_account()
    args = {"occurrenceId": str(occ.pk), "decision": "not_created"}
    refused = run(client, provision_key, "reconcile_session_hosting", args)
    assert refused["isError"]
    session = service.get_or_create_session(occ, account)
    HostedSession.objects.filter(pk=session.pk).update(state=HostedSession.State.NEEDS_RECONCILIATION)
    dry = run(client, provision_key, "reconcile_session_hosting", args)
    assert not dry["isError"], dry
    assert HostedSession.objects.get(pk=session.pk).state == HostedSession.State.NEEDS_RECONCILIATION
    done = run(client, provision_key, "reconcile_session_hosting",
               {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert not done["isError"], done
    assert HostedSession.objects.get(pk=session.pk).state == HostedSession.State.FAILED


def test_manual_link_preview_hides_link(client, provision_key, occ):
    account = _zoom_account()
    args = {"occurrenceId": str(occ.pk), "accountId": str(account.pk), "joinUrl": JOIN}
    dry = run(client, provision_key, "set_session_manual_link", args)
    assert not dry["isError"], dry
    assert "room-passcode-123" not in text_of(dry)
    assert dry["structuredContent"]["preview"]["joinUrlHost"] == "us02web.zoom.us"
    assert service.session_for(occ) is None
    done = run(client, provision_key, "set_session_manual_link",
               {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert "room-passcode-123" not in text_of(done)


# ---------------------------------------------------------------- orders


def _order(occurrence, status):
    reg = make_registration(occurrence)
    return TicketOrder.objects.create(
        occurrence=occurrence, status=status, mode="test", amount_minor=2500, currency="usd",
        idempotency_key=f"k-{uuid.uuid4().hex}", request_hash="x", capability_hash=uuid.uuid4().hex,
        capability_expires_at=occurrence.starts_at, hold_expires_at=occurrence.starts_at, registration=reg,
    )


def test_mark_refunded_dry_run_then_confirm(client, pay_key, occ):
    order = _order(occ, "refund_required")
    args = {"orderId": str(order.pk)}
    dry = run(client, pay_key, "mark_order_refunded", args)
    assert not dry["isError"], dry
    assert "no money moves" in text_of(dry).lower()
    assert TicketOrder.objects.get(pk=order.pk).status == "refund_required"
    done = run(client, pay_key, "mark_order_refunded",
               {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert not done["isError"], done
    assert TicketOrder.objects.get(pk=order.pk).status == "refunded"


def test_order_token_bound_to_order_state(client, pay_key, occ):
    order = _order(occ, "refund_required")
    args = {"orderId": str(order.pk)}
    dry = run(client, pay_key, "mark_order_refunded", args)
    TicketOrder.objects.filter(pk=order.pk).update(status="fulfilled")
    stale = run(client, pay_key, "mark_order_refunded",
                {**args, "dryRun": False, "confirmToken": dry["structuredContent"]["confirmToken"]})
    assert stale["isError"]
    assert TicketOrder.objects.get(pk=order.pk).status == "fulfilled"


def test_release_refuses_non_reconciling_order(client, pay_key, occ):
    order = _order(occ, "fulfilled")
    _dry, done = confirm(client, pay_key, "release_unpaid_order", {"orderId": str(order.pk)})
    assert done["isError"]
    assert TicketOrder.objects.get(pk=order.pk).status == "fulfilled"


# ---------------------------------------------------------------- guides


def test_new_guides_served_packaged_and_clean(settings):
    from webinars.mcp_resources import _SECRET_PATTERNS, GUIDES, PLAYBOOK, read_guide

    base_dir = Path(settings.BASE_DIR)
    dockerfile = (base_dir / "Dockerfile").read_text()
    dockerignore = (base_dir / ".dockerignore").read_text().splitlines()
    for name in ("tickets", "hosting", "kits"):
        rel = GUIDES[name]
        assert rel in dockerfile and f"!{rel}" in dockerignore
        text = read_guide(name)
        assert text and not any(p.search(text) for p in _SECRET_PATTERNS)
    assert "webinar://guides/tickets" in PLAYBOOK and "webinar://guides/hosting" in PLAYBOOK
    mcp_doc = (base_dir / "docs" / "mcp.md").read_text()
    for tool in ("create_hosting_account", "reconcile_session_hosting", "set_session_manual_link",
                 "mark_order_refunded", "release_unpaid_order"):
        assert tool in mcp_doc
