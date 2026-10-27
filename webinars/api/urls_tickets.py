from django.urls import path

from webinars.api import tickets

urlpatterns = [
    # Stripe (signature-authenticated only) and public order status (opaque token)
    path("api/v1/payments/stripe/webhook", tickets.stripe_webhook, name="stripe-webhook"),
    path("api/v1/orders/<uuid:pk>", tickets.order_status, name="order-status"),
    path("tickets/return", tickets.tickets_return, name="tickets-return"),
    # Management (payments_admin / content_write; docs/tickets.md)
    path("api/v1/manage/payments/account", tickets.payment_account, name="manage-payment-account"),
    path("api/v1/manage/events/<uuid:pk>/tickets", tickets.event_tickets, name="manage-event-tickets"),
    path("api/v1/manage/occurrences/<uuid:pk>/tickets", tickets.occurrence_tickets, name="manage-occurrence-tickets"),
    path("api/v1/manage/occurrences/<uuid:pk>/orders", tickets.occurrence_orders, name="manage-occurrence-orders"),
    path("api/v1/manage/orders/<uuid:pk>/mark-refunded", tickets.order_mark_refunded, name="manage-order-refunded"),
    path("api/v1/manage/orders/<uuid:pk>/release", tickets.order_release, name="manage-order-release"),
]
