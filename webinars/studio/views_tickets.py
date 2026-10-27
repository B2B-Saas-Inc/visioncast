"""Studio pages for seats and tickets (docs/tickets.md).

* Payments (Stripe keys): superusers only (charge configuration is owner-controlled).
  Secrets are set-only password inputs; only "Set" / "Not set" is ever rendered.
* Session seats & tickets: Editors (``webinars.change_occurrence``) set capacity and
  the show-remaining toggle; prices and order bookkeeping need an Administrator or
  superuser. Analysts never reach these pages. No attendee data is shown.
"""
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect

from webinars import capacity, payments
from webinars.models import Occurrence, PaymentAccount, TicketOrder
from webinars.roles import ADMINISTRATOR

from .access import forbidden, render, studio_view

T = "studio/tickets/"


def can_price(user):
    return bool(user.is_superuser or user.groups.filter(name=ADMINISTRATOR).exists())


def _errors(exc):
    if hasattr(exc, "message_dict"):
        return {k: " ".join(v) for k, v in exc.message_dict.items()}
    return {"__all__": " ".join(exc.messages)}


@studio_view()
def payments_settings(request):
    if not request.user.is_superuser:
        return forbidden(request, "Only the workspace owner configures payments.")
    account = PaymentAccount.objects.order_by("created_at").first()
    errors = {}
    if request.method == "POST":
        try:
            account = payments.save_account(
                mode=request.POST.get("mode", ""),
                secret_key=request.POST.get("secret_key", ""),
                webhook_secret=request.POST.get("webhook_secret", ""),
                expected_account_id=request.POST.get("expected_account_id", ""),
                enabled=request.POST.get("enabled") == "on",
                user=request.user,
            )
            return redirect("studio:payments")
        except ValidationError as exc:
            errors = _errors(exc)
    return render(request, T + "payments.html", {
        "account": payments.serialize_account(account),
        "modes": PaymentAccount.Mode.choices,
        "errors": errors,
    }, status=400 if errors else 200)


@studio_view(perm=("webinars.view_occurrence", "webinars.change_occurrence"))
def session_tickets(request, pk):
    occ = get_object_or_404(Occurrence.objects.select_related("event"), pk=pk)
    pricing = can_price(request.user)
    errors = {}
    if request.method == "POST":
        op = request.POST.get("op", "")
        try:
            if op == "seats":
                capacity.update_ticket_settings(
                    occ, capacity=request.POST.get("capacity", ""),
                    show_remaining=request.POST.get("show_remaining") == "on", user=request.user,
                )
            elif op in ("price", "event_price", "refunded", "release"):
                if not pricing:
                    return forbidden(request, "Only Administrators change prices or orders.")
                if op == "price":
                    currency = request.POST.get("currency", "")
                    amount = payments.parse_major_amount(request.POST.get("price", ""), currency) if request.POST.get("price", "").strip() else None
                    capacity.update_ticket_settings(occ, price_minor=amount, currency=currency if amount is not None else "",
                                                    user=request.user)
                elif op == "event_price":
                    currency = request.POST.get("event_currency", "")
                    amount = payments.parse_major_amount(request.POST.get("event_price", ""), currency) if request.POST.get("event_price", "").strip() else None
                    payments.update_event_price(occ.event, price_minor=amount, currency=currency if amount is not None else "",
                                                user=request.user)
                else:
                    order = get_object_or_404(TicketOrder, pk=request.POST.get("order", ""), occurrence=occ)
                    if op == "refunded":
                        payments.mark_refunded(order, user=request.user)
                    else:
                        payments.release_unpaid(order, user=request.user)
            else:
                raise ValidationError("Unknown action.")
            return redirect("studio:session-tickets", pk=occ.pk)
        except ValidationError as exc:
            errors = _errors(exc)
    price = payments.effective_price(occ)
    orders = [payments.serialize_order_admin(o) for o in TicketOrder.objects.filter(occurrence=occ).order_by("-created_at")[:200]]
    return render(request, T + "session_tickets.html", {
        "occ": occ,
        "event": occ.event,
        "remaining": capacity.remaining(occ),
        "price": price,
        "price_display": payments.format_amount(price["amountMinor"], price["currency"]) if price else "Free",
        "occ_price": payments.format_amount(occ.ticket_price_minor, occ.ticket_currency) if occ.ticket_price_minor is not None else "",
        "event_price": payments.format_amount(occ.event.ticket_price_minor, occ.event.ticket_currency) if occ.event.ticket_price_minor is not None else "",
        "currencies": payments.CURRENCY_CHOICES,
        "payments_ready": payments.payments_available(payments.active_account()),
        "can_price": pricing,
        "orders": orders,
        "counts": payments.order_counts(occ),
        "errors": errors,
    }, status=400 if errors else 200)
