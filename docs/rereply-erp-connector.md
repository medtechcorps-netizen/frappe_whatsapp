# ERPNext through ReReply

ReReply owns the WhatsApp Cloud API connection and Coexistence webhook. ERPNext
retains business rules, the existing text bot, draft orders and notification
rendering. This connector is opt-in per WhatsApp Account; existing Meta accounts
retain their previous transport.

## Configuration and rollout

1. Install this app revision and migrate the site. Both ReReply enable flags
   default to off. Take the normal Frappe Cloud backup before changing versions.
2. Create a dedicated ReReply integration user belonging only to the intended
   workspace. Use its API key, not a shared administrator key. Grant only the
   contact, chat, account and template access required by the endpoints below.
3. On the existing ERP WhatsApp Account, select ReReply. Set its HTTPS origin,
   workspace UUID, WhatsApp account UUID, exact API `account.name` and dedicated
   integration user UUID. Update the phone-number ID and WABA ID to the new
   Coexistence account. Store the API key in the Password field.
4. Generate a dedicated webhook signing secret of at least 32 characters. It
   must differ from all other ERP ReReply account secrets. Store it in the ERP
   Password field and ReReply webhook settings.
5. Configure a ReReply outbound webhook, initially inactive:

   - URL: `https://<erp-host>/api/method/frappe_whatsapp.utils.rereply_webhook.webhook`
   - Events: `message.incoming`, `message.sent`, `message.outgoing`
   - Custom header: `X-ReReply-ERP-Account: <exact ERP WhatsApp Account name>`
   - HMAC signing secret: the dedicated secret above

   ReReply rejects query strings on webhook URLs. It signs exact request bytes
   with `X-Webhook-Signature: sha256=<hex>`. Its payload has no workspace/account
   UUID; the local routing header, unique secret and signed exact account name
   jointly establish the route. Do not reuse signing secrets across workspaces.
6. Verify the new account's approved templates in ReReply. Old ERP approval
   records belong to the old WABA and do not establish approval in the new one.
   Create and publish provider templates in ReReply. For a ReReply account,
   ERP template insert/save only verifies the exact account, name, language and
   content against ReReply and mirrors its status and Meta ID. It never publishes
   templates or uploads template samples through the former Meta credentials.
7. Validate outbound sending to an explicitly authorized test recipient before
   enabling ERP inbound automation. Keep competing ReReply automatic responders
   off for this account. Enable incoming processing and the outbound webhook
   only after the outbound queue, identifiers and template mapping are verified.
   Inspect all queued rows before enabling sending: the scheduler releases every
   queued row for an enabled account, including rows created while its gate was
   off. Hold pre-cutover customer messages for reconciliation and pause their
   producing jobs during controlled validation. Confirm the short worker and
   scheduler are running after the migration.

Do not deregister the number, call the ordinary phone registration action, or
point the shared Meta app webhook back to ERPNext.

## Transaction and retry behavior

Creating an ERP Outgoing WhatsApp Message stores a rendered payload and `Queued`
state in the same business transaction. HTTP occurs only in a background worker
after commit. The worker locks the row, verifies the saved destination hash, and
commits a `Sending` claim before attempting the request. Concurrent workers cannot
send the same row. Provider acceptance records a distinct ReReply UUID and status;
it does not prove customer delivery.

Business notices with a deferred source-document flag reserve a unique key for
the ERP account, source document, flag and value. Repeated document events return
the existing message, including when it is queued, failed or uncertain. A
definitive rejection must be retried on that same row; a new document event does
not silently create another send. Ordinary chat messages do not share this key.
Custom Server Scripts use `message.insert_rereply_notice(ignore_permissions=True)`
after setting `rereply_after_send` and the matching document reference.

An HTTP timeout, send 5xx, malformed success response, or process exit during a
send becomes `Unknown`. There is no automatic resend. Reconcile the ReReply
conversation before operator recovery. A definitive rejection may be retried
through the guarded bulk retry path only when no accepted ID exists. Changing
the configured destination does not reroute queued messages silently.

Incoming webhooks persist a unique receipt, then enqueue processing after commit.
The worker commits a `Processing` claim before running ERP document hooks.
Incoming message insertion, the bot's ERP changes, queued replies and the final
receipt are committed together. Interrupted or failed automation is held for
review; only untouched `Pending` receipts are recovered automatically. Do not
delete receipt records: they are permanent replay guards.

Polling reads ReReply message status with `acknowledge=false`; it never marks
customer messages read. Real WAMIDs are separate from ReReply UUIDs. An explicit
`rereply:<UUID>` placeholder is used until the real WAMID is available.

## Staff takeover and scope

Mobile-app outgoing events are passive activity, never new bot inputs. ReReply
UI replies are identified by a signed sender UUID different from the dedicated
integration user. Unmatched sends with missing/ambiguous identity are retained
without inventing staff attribution. Where the existing `WhatsApp Bot Pause`
DocType is installed, verified staff activity pauses the bot for the remaining
portion of five minutes from the signed event timestamp and preserves any
longer existing pause.

Incoming automation currently supports text, matching the existing custom ERP
bot's input. Unsupported incoming media, button/Flow responses and catalogue
payloads remain visible in ReReply and are retained as ignored ERP receipts.
ReReply's public webhook loses button action IDs; this connector does not invent
them. Outgoing text/replies, template text/URL parameters and media headers,
documents, images, audio and video are supported. Interactive/Flow/reaction
outbound payloads are rejected before sending.

Uploads are bounded to 14 MiB. Private ERP attachments and signed print documents
require the original requester's read permission; worker privileges do not grant
access. Public media fetches validate and pin public DNS addresses, verify TLS,
and do not follow redirects or forward ERP credentials.

The default incoming backlog limit is one hour (`rereply_inbound_max_age_seconds`
in site config). ReReply's event time is persistence time, not proof of the
original WhatsApp send time. History import normally emits no live event.

## Verification

```sh
python frappe_whatsapp/utils/test_rereply_client.py
python frappe_whatsapp/utils/test_rereply_webhook.py
bench --site test_site run-tests --module frappe_whatsapp.utils.test_rereply_integration
bench --site test_site run-tests --app frappe_whatsapp
```

Isolated contract tests do not replace the Frappe/MariaDB checks. Before cutover,
verify a consented live message, one controlled ERP send, duplicate webhook
delivery, delivery status, a staff takeover and a draft-order transaction. Do not
test by invoking the enabled live ERP bot with customer/order data accidentally.

Rollback starts by disabling ReReply ingress and sending. Retain receipts and
queued rows for reconciliation. Switching transport to Meta does not repair the
deleted original registration and is not a valid automatic rollback.
