"""Signed, durable ReReply webhook ingress.

ReReply does not include workspace/account IDs in its webhook envelope. The
X-ReReply-ERP-Account header therefore selects a locally configured account;
an account-specific secret and the signed, exact ReReply account name bind it
to that workspace. Never accept Meta payloads or route by the default account.

Receipts are permanent replay guards. Workers commit their Processing claim
before calling document hooks. An interrupted/failed worker is held for human
review, never automatically replayed: hooks may already have caused effects.
Only Pending receipts are recovered automatically.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

import frappe


RECEIPT_DOCTYPE = "ReReply Webhook Receipt"
ACCOUNT_HEADER = "X-ReReply-ERP-Account"
SIGNATURE_HEADER = "X-Webhook-Signature"
MAX_BODY_BYTES = 2 * 1024 * 1024
EVENTS = {"message.incoming", "message.sent", "message.outgoing"}
_SIGNATURE = re.compile(r"sha256=[0-9a-f]{64}\Z")
_PHONE = re.compile(r"\+?[1-9][0-9]{6,14}\Z")


def verify_signature(secret, raw_body, signature):
    """Verify the provider's exact raw-body HMAC, without reserializing JSON."""
    if not isinstance(secret, str) or not secret:
        return False
    if not isinstance(raw_body, bytes) or not isinstance(signature, str):
        return False
    if not _SIGNATURE.fullmatch(signature):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _reject():
    frappe.throw("Webhook authentication failed", frappe.AuthenticationError)


def _text(value, required=False, max_length=140):
    if isinstance(value, str) and value and len(value) <= max_length:
        return value
    if required:
        raise ValueError("Invalid ReReply webhook identifier")
    return None


def _account(account_name):
    if not _text(account_name):
        _reject()
    if not frappe.db.exists("WhatsApp Account", account_name):
        _reject()
    account = frappe.get_doc("WhatsApp Account", account_name)
    if (
        account.get("transport_provider") != "ReReply"
        or account.get("status") != "Active"
        or not account.get("rereply_inbound_enabled")
        or not account.get("rereply_outbound_enabled")
        or not _text(account.get("rereply_workspace_id"))
        or not _text(account.get("rereply_account_id"))
        or not _text(account.get("rereply_account_name"))
    ):
        _reject()
    return account


def _decode(raw_body):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Invalid ReReply webhook JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Invalid ReReply webhook envelope")
    return payload


def _envelope(payload, account):
    data = payload["data"]
    event = _text(payload.get("event"), required=True)
    if event not in EVENTS:
        raise ValueError("Unsupported ReReply webhook event")
    if data.get("whatsapp_account") != account.rereply_account_name:
        _reject()
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Invalid ReReply webhook metadata")
    if metadata.get("whatsapp_account", account.rereply_account_name) != account.rereply_account_name:
        _reject()
    message_id = _text(data.get("message_id"), required=True, max_length=120)
    event_id = _text(data.get("outbox_event_id"), required=event == "message.incoming")
    if not event_id:
        # Outgoing events have no outbox_event_id; their message ID is stable.
        event_id = event + ":" + message_id
    return data, event, message_id, event_id


def _key(account, identifier):
    return hashlib.sha256(json.dumps(
        [account.rereply_workspace_id, account.rereply_account_id, identifier],
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


@frappe.whitelist(allow_guest=True)
def webhook():
    """Authenticate and persist quickly; ERP automation runs in a worker."""
    if frappe.request.method != "POST":
        _reject()
    raw_body = frappe.request.get_data(cache=True, as_text=False)
    if not isinstance(raw_body, bytes) or not raw_body or len(raw_body) > MAX_BODY_BYTES:
        _reject()
    account = _account(frappe.request.headers.get(ACCOUNT_HEADER))
    secret = account.get_password("rereply_webhook_secret", raise_exception=False)
    if not verify_signature(secret, raw_body, frappe.request.headers.get(SIGNATURE_HEADER)):
        _reject()
    try:
        payload = _decode(raw_body)
        _, event, message_id, event_id = _envelope(payload, account)
    except ValueError as exc:
        frappe.throw(str(exc), frappe.ValidationError)

    receipt_name = _key(account, message_id)
    event_key = _key(account, event_id)
    # The primary key and unique event_key enforce deduplication even when
    # two deliveries arrive concurrently, or a message gets a new event ID.
    existing = frappe.db.exists(RECEIPT_DOCTYPE, receipt_name) or frappe.db.get_value(
        RECEIPT_DOCTYPE, {"event_key": event_key}, "name"
    )
    if existing:
        return {"status": "duplicate", "receipt": existing}
    frappe.db.savepoint("rereply_receipt_insert")
    try:
        frappe.get_doc({
            "doctype": RECEIPT_DOCTYPE,
            "event_key": event_key,
            "event_id": event_id,
            "provider_message_id": message_id,
            "whatsapp_account": account.name,
            "workspace_id": account.rereply_workspace_id,
            "provider_account_id": account.rereply_account_id,
            "event_type": event,
            "payload_hash": hashlib.sha256(raw_body).hexdigest(),
            "payload": raw_body.decode("utf-8"),
            "status": "Pending",
        }).insert(ignore_permissions=True, set_name=receipt_name)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point="rereply_receipt_insert")
        return {"status": "duplicate"}
    frappe.enqueue(
        "frappe_whatsapp.utils.rereply_webhook.process_receipt",
        name=receipt_name, enqueue_after_commit=True, queue="long", timeout=900,
    )
    # Frappe commits successful POST requests before the after-commit enqueue.
    return {"status": "accepted", "receipt": receipt_name}


def _utc(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _uuid(value):
    """Accept only canonical UUID strings for the dedicated sender identity."""
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value.lower() else None


def _non_integration_sender(data, account):
    integration_user = _uuid(account.get("rereply_integration_user_id"))
    sender = _uuid(data.get("sent_by_user_id"))
    # This establishes non-integration-user activity, not proof that a human
    # typed the message. Conservatively pause AI for that activity.
    return bool(integration_user and sender and sender != integration_user)


def _incoming_ignore_reason(payload):
    data = payload["data"]
    if (
        data.get("direction") != "incoming"
        or data.get("event_type") != "message.incoming"
        or data.get("source_type") != "message"
        or data.get("source_id") != data.get("message_id")
        or data.get("actor_type") != "contact"
    ):
        return "Not a live incoming contact message"
    if data.get("message_type") != "text" or not isinstance(data.get("content"), str):
        return "Unsupported content; no action IDs or media are inferred"
    # ReReply history import emits no live webhook. occurred_at is ReReply's
    # persistence time, NOT the original WhatsApp time; this bounds backlog
    # only, and must not be represented as independent proof of live origin.
    if data.get("is_history") or (data.get("metadata") or {}).get("is_history"):
        return "History import"
    occurred_at = _utc(data.get("occurred_at"))
    try:
        max_age = int(frappe.conf.get("rereply_inbound_max_age_seconds", 3600))
    except (TypeError, ValueError):
        return "Invalid receipt backlog limit"
    if max_age < 1 or occurred_at is None:
        return "Missing live event timestamp or invalid backlog limit"
    age = (datetime.now(timezone.utc) - occurred_at).total_seconds()
    if age < -300 or age > max_age:
        return "Event outside permitted receipt backlog window"
    return None


def _message_values(payload, account):
    data = payload["data"]
    phone = data.get("contact_phone")
    if not isinstance(phone, str) or not _PHONE.fullmatch(phone):
        raise ValueError("Invalid ReReply contact phone")
    incoming = payload["event"] == "message.incoming"
    return {
        "doctype": "WhatsApp Message",
        "type": "Incoming" if incoming else "Outgoing",
        "from" if incoming else "to": phone.lstrip("+"),
        "message": data["content"],
        # ReReply UUIDs are not WhatsApp WAMIDs. Keep them distinguishable.
        "message_id": "rereply:" + data["message_id"],
        "rereply_message_id": data["message_id"],
        "rereply_contact_id": _text(data.get("contact_id")),
        "rereply_source": payload["event"],
        "content_type": "text",
        "profile_name": _text(data.get("contact_name")),
        "whatsapp_account": account.name,
    }


def _pause_erp_bot(phone, payload):
    """Use the site's optional pause integration without inventing a staff user.

The signed outgoing timestamp is the provider event time, not necessarily the
original WhatsApp send time. Delayed retries do not receive a fresh five-minute
pause. A site without this DocType must supply its own handoff integration.
"""
    if not frappe.db.exists("DocType", "WhatsApp Bot Pause"):
        return
    meta = frappe.get_meta("WhatsApp Bot Pause")
    if not meta.has_field("phone") or not meta.has_field("paused_until"):
        return
    occurred_at = _utc(payload.get("timestamp"))
    if occurred_at is None:
        return
    age = (datetime.now(timezone.utc) - occurred_at).total_seconds()
    if age < -300 or age >= 300:
        return
    # now_datetime is in the site's timezone; use an elapsed-time offset
    # rather than inserting a UTC wall time into a Frappe Datetime column.
    paused_until = frappe.utils.now_datetime() + timedelta(seconds=300 - max(age, 0))
    # The bot reads the newest creation, including explicit resume records.
    # An older, longer expiry must not hide a newer resume or shorter pause.
    existing = frappe.db.sql(
        "SELECT paused_until FROM `tabWhatsApp Bot Pause` WHERE phone=%s "
        "ORDER BY creation DESC LIMIT 1 FOR UPDATE", (phone,)
    )
    if existing and frappe.utils.get_datetime(existing[0][0]) >= paused_until:
        return
    values = {"doctype": "WhatsApp Bot Pause", "phone": phone, "paused_until": paused_until}
    if meta.has_field("source"):
        values["source"] = "ReReplyMobile" if payload["event"] == "message.outgoing" else "ReReplyStaff"
    if meta.has_field("note"):
        values["note"] = "Signed ReReply outside activity; pause ERP replies. Human identity was not inferred."
    frappe.get_doc(values).insert(ignore_permissions=True)


def _finish(receipt, status, reason=None, message_name=None):
    receipt.status = status
    receipt.error = reason
    if message_name:
        receipt.whatsapp_message = message_name
    receipt.save(ignore_permissions=True)
    frappe.db.commit()  # nosemgrep: frappe-manual-commit -- receipt and ERP writes complete atomically


def process_receipt(name):
    """Claim one durable receipt; an ambiguous prior attempt is never replayed."""
    rows = frappe.db.sql(
        "SELECT name FROM `tabReReply Webhook Receipt` WHERE name=%s FOR UPDATE", (name,)
    )
    if not rows:
        return
    receipt = frappe.get_doc(RECEIPT_DOCTYPE, name)
    if receipt.status != "Pending":
        frappe.db.rollback()
        return
    try:
        account = _account(receipt.whatsapp_account)
        raw_body = receipt.payload.encode("utf-8")
        if hashlib.sha256(raw_body).hexdigest() != receipt.payload_hash:
            raise ValueError("Stored receipt payload changed")
        payload = _decode(raw_body)
        data, event, message_id, _ = _envelope(payload, account)
        if (
            account.rereply_workspace_id != receipt.workspace_id
            or account.rereply_account_id != receipt.provider_account_id
            or message_id != receipt.provider_message_id
            or event != receipt.event_type
        ):
            raise ValueError("Receipt configuration changed")
    except Exception as exc:
        _finish(receipt, "Ignored", "Account disabled or changed (%s)" % type(exc).__name__)
        return

    existing = frappe.db.get_value("WhatsApp Message", {
        "whatsapp_account": account.name, "rereply_message_id": message_id,
    }, "name")
    if existing:
        _finish(receipt, "Processed", message_name=existing)
        return
    pause_only = False
    if event == "message.incoming":
        reason = _incoming_ignore_reason(payload)
        if reason:
            _finish(receipt, "Ignored", reason)
            return
    else:
        if event == "message.sent" and not _non_integration_sender(data, account):
            # API and UI sends share this schema. Only a separate, dedicated
            # integration identity lets another signed sender be treated as
            # outside activity. First allow the ERP sender to record its ID.
            delivered_at = _utc(payload.get("timestamp"))
            if delivered_at and (datetime.now(timezone.utc) - delivered_at).total_seconds() < 60:
                frappe.db.rollback()
                return
            _finish(receipt, "Ignored", "Unmatched send; API versus staff origin is ambiguous")
            return
        if data.get("direction") != "outgoing" or not _text(data.get("message_type")):
            _finish(receipt, "Ignored", "Unsupported outgoing echo")
            return
        pause_only = data["message_type"] != "text"
        if not pause_only and not isinstance(data.get("content"), str):
            _finish(receipt, "Ignored", "Invalid outgoing text echo")
            return

    receipt.status = "Processing"
    receipt.save(ignore_permissions=True)
    frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durable claim before hooks; never replay uncertain work
    try:
        if pause_only:
            phone = data.get("contact_phone")
            if not isinstance(phone, str) or not _PHONE.fullmatch(phone):
                raise ValueError("Invalid ReReply contact phone")
            _pause_erp_bot(phone.lstrip("+"), payload)
            _finish(receipt, "Processed", "Outgoing media activity; no ERP message row created")
            return
        message = frappe.get_doc(_message_values(payload, account))
        if event != "message.incoming":
            message.flags.rereply_passive_log = True
        message.insert(ignore_permissions=True)
        if event != "message.incoming":
            _pause_erp_bot(message.to, payload)
        _finish(receipt, "Processed", message_name=message.name)
    except Exception as exc:
        frappe.db.rollback()
        receipt = frappe.get_doc(RECEIPT_DOCTYPE, name)
        _finish(receipt, "Failed", "%s; held for manual review, not retried" % type(exc).__name__)


def recover_pending_webhooks():
    """Recover missed enqueue operations; never replay Processing/Failed rows."""
    for name in frappe.get_all(
        RECEIPT_DOCTYPE, filters={"status": "Pending"}, pluck="name",
        limit_page_length=100, order_by="creation asc",
    ):
        frappe.enqueue(
            "frappe_whatsapp.utils.rereply_webhook.process_receipt",
            name=name, queue="long", timeout=900,
        )
    # A worker has a 15-minute timeout. Give it twice that before flagging
    # an abandoned claim. Holding it does not imply any hook was rolled back.
    cutoff = frappe.utils.now_datetime() - timedelta(minutes=30)
    for name in frappe.get_all(
        RECEIPT_DOCTYPE, filters={"status": "Processing", "modified": ["<", cutoff]},
        pluck="name", limit_page_length=100,
    ):
        frappe.db.sql(
            "SELECT name FROM `tabReReply Webhook Receipt` WHERE name=%s FOR UPDATE", (name,)
        )
        receipt = frappe.get_doc(RECEIPT_DOCTYPE, name)
        if receipt.status == "Processing" and receipt.modified < cutoff:
            _finish(receipt, "Failed", "Worker interrupted; inspect effects before manual recovery")
        else:
            frappe.db.rollback()
