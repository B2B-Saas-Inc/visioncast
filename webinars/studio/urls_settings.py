from django.urls import path

from webinars.studio import views_settings as v

urlpatterns = [
    path("integrations/", v.integrations, name="integrations"),
    path("integrations/new/", v.integration_new, name="integration-new"),
    path("integrations/<uuid:pk>/", v.integration_edit, name="integration-edit"),
    path("integrations/<uuid:pk>/secret", v.integration_secret, name="integration-secret"),
    path("integrations/<uuid:pk>/credentials", v.integration_credentials, name="integration-credentials"),
    path("integrations/<uuid:pk>/delete", v.integration_delete, name="integration-delete"),
    path("deliveries/", v.deliveries, name="deliveries"),
    path("deliveries/<uuid:pk>/retry", v.delivery_retry, name="delivery-retry"),
    path("api-keys/", v.api_keys, name="api-keys"),
    path("api-keys/new", v.api_key_new, name="api-key-new"),
    path("api-keys/<uuid:pk>/revoke", v.api_key_revoke, name="api-key-revoke"),
    path("team/", v.team, name="team"),
    path("team/invite", v.team_invite, name="team-invite"),
    path("team/invitations/<uuid:pk>/revoke", v.invitation_revoke, name="invitation-revoke"),
    path("team/members/<int:pk>/", v.member_edit, name="member-edit"),
    path("invite/", v.invite_accept, name="invite-accept-pending"),
    path("invite/<str:token>/", v.invite_accept, name="invite-accept"),
    path("mailbox/", v.mailbox, name="mailbox"),
    path("mailbox/<int:pk>/", v.mailbox_message, name="mailbox-message"),
    path("account/", v.account, name="account"),
]
