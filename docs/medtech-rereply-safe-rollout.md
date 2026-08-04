# Medtech Healthcare: safe ReReply WhatsApp rollout

This runbook keeps the existing Frappe receiver available until one controlled,
non-overlapping cutover to ReReply's native WhatsApp integration.

## Confirmed production assets

- Meta business: Medtech Healthcare (`2018290039073161`)
- Meta app: Medtech Bot (`1717100139651354`)
- WhatsApp Business Account: Medtech Healthcare (`2016202792313686`)
- Phone number ID: `1124535634077572`
- Phone: `+60 11-3309 3929`
- Current callback: the MHTC Frappe webhook
- Target WABA-specific native callback after saving the Medtech workspace Meta
  settings: `https://app.rereply.app/api/webhook?workspace=248a579d-a02a-46f9-ae03-2a1bc9688c58`

Identifiers are safe to record. Never add the Meta App Secret, access token,
embedded-signup code, PIN, or ReReply credentials to this file or Git.

## Guardrails

1. Do not mirror the same live Meta event into both Frappe and ReReply. The two
   ingestion paths do not share a deduplication scope and can create duplicate
   contacts, conversations, messages, unread counts, and notifications.
2. Do not use a generic `whatsapp` / `relay` channel account as the production
   Tech Provider connection. Production must use ReReply's native
   `whatsapp_accounts` Embedded Signup flow.
3. Do not publish Medtech Bot or change its callback while App Review is still
   in progress.
4. Do not enable `history`, `smb_message_echoes`, or `smb_app_state_sync` merely
   for a shadow test. Add coexistence fields only as part of the reviewed native
   rollout.
5. Do not test outbound sending from ReReply until the cutover window and an
   explicitly approved test recipient are ready.

## Phase A - secure the current Frappe receiver

Do not deploy the field and enforcement code together. The enforcement code
fails closed when the secret is absent, so a combined deployment would reject
all live inbound webhooks until someone entered the secret.

1. Take an on-demand Frappe Cloud backup containing the database, public files,
   private files, and site configuration/encryption key.
2. Update the bench app source from `shridarpatil/frappe_whatsapp` to the Medtech
   fork's **parity branch**, which must point to the exact live upstream commit
   `08bc1f6`. Proceed only when Frappe Cloud says `frappe_whatsapp` already exists
   and offers **Update App**. Use a normal Deploy and Update, then confirm normal
   inbound and outbound traffic is unchanged.
3. Take another on-demand backup. Change to the **secret-field branch**, deploy,
   and run the normal migration so the encrypted **App Secret** field exists on
   WhatsApp Account. This release must not enforce signatures yet.
4. Enter Medtech Bot's App Secret into the Medtech Healthcare WhatsApp Account.
   Do not paste it into chat, shell history, logs, screenshots, or source code.
5. Take another on-demand backup. Change to the **signature-enforcement branch**
   and deploy the verifier.
6. From the approved test phone, send one brand-new real inbound WhatsApp
   message with a unique marker and confirm Frappe accepts it. Do not construct
   a signed request manually or put the App Secret into shell history.
7. Send a separate request with the correct routing IDs but a missing or bogus
   signature and a unique message ID. Confirm it receives HTTP 401 and is
   rejected before a notification log or WhatsApp message is written.
8. Monitor Frappe/nginx HTTP 401s and Meta webhook delivery failures during the
   rollout window. Roll back the enforcement branch immediately if real Meta
   deliveries start receiving unexpected 401 responses.
9. Keep the existing Frappe callback unchanged.

Rollback: change back to the previous tested branch. The new field is additive,
so leaving it in the database is harmless. Frappe does not provide reverse
schema migrations; restore the complete backup if a full schema rollback is
required. Do not uninstall the app or use a temporary App Patch as a shortcut.

## Phase B - readiness checks before native onboarding

1. Wait for `whatsapp_business_messaging` and
   `whatsapp_business_management` App Review to be approved.
2. Sign in to the production ReReply workspace as an administrator and confirm
   the WhatsApp/omnichannel entitlement is active.
3. Confirm ReReply's production Meta settings use Medtech Bot. Open ReReply on
   its canonical `https://app.rereply.app` origin after saving the workspace
   settings, then copy the generated workspace callback. It must include the
   selector `?workspace=248a579d-a02a-46f9-ae03-2a1bc9688c58`; the selector is
   required so GET verification resolves this workspace's stored credential.
   Keep Medtech Bot's app-level default callback on Frappe; the cutover should
   use a WABA-specific callback override for Medtech Healthcare only.
4. Confirm the phone is still connected and high quality in WhatsApp Manager.
5. Confirm coexistence through Meta's phone-number fields:
   `is_on_biz_app=true` and `platform_type=CLOUD_API`.
6. Record a rollback owner, a 15-minute test window, the last Frappe message ID,
   and a test customer number.

Stop if any check fails. Onboarding should not be used as a diagnostic for a
missing permission, plan, app configuration, or login.

## Phase C - single non-overlapping cutover

1. Pause user activity in ReReply and prevent campaigns/chatbots from sending.
2. Drain any Frappe webhook work and record the last accepted WhatsApp message
   ID. Do not replay older messages into ReReply.
3. Complete ReReply's native **Sync with Mobile App** Embedded Signup for the
   existing Medtech Healthcare WABA and phone.
4. Subscribe only the required user fields: `messages`, `smb_app_state_sync`,
   and `smb_message_echoes`. Do not enable `history`, because the current ReReply
   native handler does not import it. ReReply's current Subscribe action only
   subscribes the app and does not create an alternate callback. Separately add
   the WABA-specific callback override for Medtech Healthcare using the exact
   workspace callback and verify token. Meta's
   override is not field-granular: all supported user fields go to the alternate
   callback, while other app-level fields continue to use the default Frappe
   callback. Do not leave any Frappe-to-ReReply forwarding active.
5. From the approved test number, send a brand-new inbound message containing a
   unique marker. Confirm exactly one ReReply contact, conversation, and message.
6. Confirm one delivery/read status update.
7. Send one controlled ReReply reply and confirm it appears in the WhatsApp
   Business mobile app.
8. Confirm the mobile app can send a reply and that ReReply receives the
   coexistence echo once the required fields are subscribed.
9. Keep Frappe disabled but available for the rollback window. Do not replay its
   old events.

Rollback: stop ReReply sending, remove the Medtech Healthcare WABA callback
override so Meta falls back to the verified app-level Frappe callback, and test
with a new unique message. Reconcile any event accepted after the recorded
cutover marker before another attempt.

## Acceptance criteria

- One inbound customer message creates exactly one ReReply message.
- No duplicate contact or conversation is created.
- Delivery/read status reaches the correct message.
- One ReReply reply reaches the customer and is visible in the mobile app.
- One mobile-app reply/echo reaches ReReply after coexistence subscriptions are
  enabled.
- Frappe remains available as a rollback receiver, with no event replay overlap.
