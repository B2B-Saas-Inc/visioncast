# MCP server

`POST /mcp` — Model Context Protocol over **stateless Streamable HTTP**: one JSON-RPC 2.0 message per POST, `application/json` responses, no SSE (`GET /mcp` → 405), no `Mcp-Session-Id`. Versions: `2025-06-18` (preferred), `2025-03-26`, `2024-11-05`; `initialize` echoes a supported requested version, else returns `2025-06-18`. An unsupported `MCP-Protocol-Version` header → 400. Notifications (no `id`) → 202. Batches → `-32600`. Origin header, when present, must be `PUBLIC_BASE_URL` or in `WEBINAR_ALLOWED_ORIGINS`. Body limit 64 KB. stdio transport is not provided.

Capabilities: `tools`, `resources` (`resources/list`, `resources/read`, `resources/templates/list`; no subscriptions), `prompts` (`prompts/list`, `prompts/get`).

Auth: API keys (`Authorization: Bearer wbk_...`). `tools/list` shows only tools the key's scopes allow; `tools/call` re-checks. Arguments are validated against each tool's input schema (unknown keys rejected). Writes reuse the management API logic, `webinars/services.py` and `webinars/invitations.py` (same validation, revisions, rescheduling, audit). Code: `webinars/mcp.py`, `webinars/mcp_config.py`, `webinars/mcp_resources.py`, `webinars/confirmation.py`.

## Scopes

| Scope | Grants | Who can grant |
|---|---|---|
| public_read | public read tools, non-secret resources, form resources | Administrator (studio), keys_admin key holding it, owner shell |
| content_write | catalogue/event content/session/form/waiting-room/cover/CTA/series writes, draft delete (gated). **No visibility changes.** | same |
| attendance_write | record_attendance, finalize_attendance (gated) | same |
| reporting | get_report_summary (aggregates) | same |
| config_read | redacted readiness, provisioning instructions, catalogue listing, redacted destinations, delivery counts, `webinar://config/current`. Over REST (not MCP): read-only `GET` on management events (drafts included), sessions and waiting rooms, with `waitingRoom.url` null ([management-api.md](management-api.md)) | same |
| publication | set_event_visibility, publish_recording (gated); REST visibility changes and recordings | **superuser (studio) or `manage.py create_api_key` only** |
| delivery_admin | destinations, set-only credentials and signing secret, contract preview, test send, requeue (gated) | superuser / owner shell only |
| keys_admin | list/create/revoke API keys, bounded delegation | superuser / owner shell only |
| team_admin | list team; invite, revoke, change role/email/active for **Editors and Analysts only**, bounded by the key's own scopes (gated; see below) | superuser / owner shell only |

`create_api_key` (MCP) can grant only public_read, register, proxy, reporting, content_write, attendance_write, config_read — and only those the calling key holds. Privileged scopes (`trusted`, publication, delivery_admin, keys_admin, team_admin) are never delegable and are granted only by a superuser in the studio or `manage.py create_api_key`. The MCP `create_api_key` tool also rejects names that are blank after trimming.

### team_admin: delegated human authority

A `team_admin` key creates and manages **people**, and people act in the studio without API keys or confirmation tokens. Treat it as at least as powerful as every scope it is paired with:

- Inviting, assigning, reactivating or re-emailing an **Editor** requires the key to also hold `content_write` and `publication` (Editors edit and publish in the studio without the API confirmation step).
- The same for an **Analyst** requires `reporting` (Analysts see registrations, masked, and reports).
- Administrator, staff and superuser accounts, and any account with direct permissions, more than one group or a group other than Editor/Analyst, are refused. Keys can never create superusers or Administrators.
- Every change is a gated dry run + confirmToken and is audited with the key as actor. Invitation links go by email only.
- Roles and groups (`sync_roles`) are unchanged; studio team management stays superuser-only.

## Gated actions and confirmation

Gated tools default to `dryRun: true`: no writes, no audit rows, no messages; the result contains a `preview` and a `confirmToken`. Repeat the identical call with `dryRun: false` and the token. Tokens (`webinars/confirmation.py`) are:

- bound to the tool, the calling key, an HMAC of the exact arguments (secret values never appear in the token) and the target's state (e.g. event revision, destination `updated_at`, member role/email/active);
- valid for 10 minutes and **single use** (burned nonce in `ConfirmationUse`).

**A token proves only that the same key holder asked twice; it is not independent human approval.** Obtain human approval out of band if your process requires it.

Publishing a not-ready event (no upcoming session, Attention/summary or Action) is refused on every path (MCP, REST, studio and Django `/admin/`, all via `services.publish_problems`); REST checks the patched result and returns `409 not_ready` without a token, and Django admin checks the submitted form values. Note that human Editors (studio and Django admin) publish without tokens; see team_admin above. Publication has one API path per interface: MCP `set_event_visibility` / `publish_recording`, and REST `PATCH /api/v1/manage/events/{id}` (visibility change, needs content_write + publication) / `POST /api/v1/manage/occurrences/{id}/recording` (publication). REST returns `428` with `preview` and `confirmToken`; repeat the identical request with header `Webinar-Confirm-Token: <token>`. `update_event` refuses visibility changes.

## Tool matrix

| Tool | Scopes | Gated |
|---|---|---|
| list_events, get_event, get_form_schema | public_read or content_write | |
| create_event, update_event (no visibility change), schedule_session, update_session, cancel_session, configure_waiting_room, create_host, create_topic | content_write | |
| update_host, update_topic, create_form_version, update_form_version, set_event_cta, set_event_cover, create_series, schedule_recurring_sessions (optional dryRun) | content_write | |
| delete_draft_event | content_write | yes |
| list_catalog | config_read or content_write | |
| record_attendance | attendance_write | |
| finalize_attendance | attendance_write | yes |
| get_report_summary | reporting | |
| get_configuration_readiness, get_host_provisioning_instructions | config_read | |
| list_destinations, get_delivery_status | config_read or delivery_admin | |
| set_event_visibility, publish_recording | publication | yes |
| save_destination, set_destination_credentials, set_destination_signing_secret, send_test_delivery, requeue_dead_messages | delivery_admin | yes |
| preview_destination_contract | delivery_admin | |
| list_api_keys | keys_admin | |
| create_api_key, revoke_api_key | keys_admin | yes |
| list_team | team_admin | |
| invite_member, revoke_invitation, update_member | team_admin | yes |

`send_test_delivery` queues one fixed synthetic payload (`eventType: webhook.test`, no person, no links) for an existing **enabled** destination; the worker delivers it through the normal path (SSRF-pinned signed webhook, or the destination's native provider). No URL, header or payload is accepted. Invitation links are delivered by email only and never returned.

Covers: `set_event_cover` accepts PNG/JPEG/WebP up to 40 KB (base64 inside the 64 KB body). For larger images use the studio upload or set `heroImageUrl` (https) with `update_event`.

**Not supported over MCP:** Administrator/superuser management and creation, writing process-environment settings (readiness is read-only; the playbook lists exact deployment steps), host provisioning (instructions only), arbitrary HTTP, shell or file access, returning any stored secret, join link, private link, invitation link or registrant personal data.

## Resources

| URI | Scopes |
|---|---|
| webinar://playbook/implementation (checklist + full README, operations and integration guides) | any key |
| webinar://guides/{name} (readme, implementation-guide, operations, integration, mcp, management-api, api-reference, webhooks, field-dictionary, field-mapping, waiting-room, studio, email, browser-integration, native-integrations, security) | any key |
| webinar://docs/field-dictionary (generated from code) | any key |
| webinar://openapi (generated from `webinars/openapi.build_spec`) | any key |
| webinar://contracts/webhooks | any key |
| webinar://mcp/tools | any key |
| webinar://forms/{formVersion} (schema + context; active or used by a public event) | public_read, content_write or config_read |
| webinar://config/current (redacted) | config_read |

Guides are read from a fixed name → file allowlist (`mcp_resources.GUIDES`); the URI suffix is only a dictionary key, never a path. The Dockerfile and `.dockerignore` package exactly these files. Text shaped like an API key or private key is redacted. Unknown and unauthorised URIs both return `-32002`.

## Prompts

`guided_deployment(environment?)`, `website_launch(topic)`, `delivery_diagnosis(destination?)`.

## Client configuration

```json
{"mcpServers": {"webinars": {"type": "http", "url": "https://webinars.example.com/mcp",
  "headers": {"Authorization": "Bearer ${WEBINAR_MCP_KEY}"}}}}
```

```sh
# Owner shell: a publishing agent key (privileged scopes are owner-granted only)
python manage.py create_api_key --name launch-agent --scope content_write --scope publication --scope config_read

curl -s https://webinars.example.com/mcp -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"set_event_visibility","arguments":{"eventId":"<uuid>","visibility":"public"}}}'
# then repeat with "dryRun": false, "confirmToken": "<token from the preview>"
```

Tests: `tests/test_mcp.py`, `tests/test_mcp_config.py`, `tests/test_management_api.py`.
