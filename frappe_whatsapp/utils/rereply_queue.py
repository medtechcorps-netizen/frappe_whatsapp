"""Durable outbound delivery for ReReply.

ERP hooks only prepare the row. Workers commit a Sending claim before HTTP;
an uncertain result is held, never automatically resent. No commits occur in
document hooks or inside the ERP transaction creating an order/message.
"""
import json
import hashlib
import re
from uuid import uuid4

import frappe
from frappe.utils import add_to_date, now_datetime


def uses_rereply(account):
    return account.get("transport_provider") == "ReReply"


def route_key(account):
    fields = ("rereply_base_url", "rereply_workspace_id", "rereply_account_id", "rereply_account_name")
    return hashlib.sha256(json.dumps([account.get(f) for f in fields], separators=(",", ":")).encode()).hexdigest()


def compute_notice_key(doc, account):
    """Reserve one source-document flag update, without deduplicating other chat."""
    raw = doc.get("rereply_after_send")
    if not raw:
        return None
    try:
        descriptor = json.loads(raw)
        if not isinstance(descriptor, dict) or set(descriptor) != {"doctype", "name", "fieldname", "value"}:
            raise ValueError
        if not all(isinstance(descriptor[field], str) and descriptor[field] for field in ("doctype", "name", "fieldname")):
            raise ValueError
        if (descriptor["doctype"] != doc.get("reference_doctype")
                or descriptor["name"] != doc.get("reference_name")
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", descriptor["fieldname"])):
            raise ValueError
        canonical = json.dumps([
            account.name, descriptor["doctype"], descriptor["name"],
            descriptor["fieldname"], descriptor["value"],
        ], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        frappe.throw("The deferred ReReply update must match this message's source document.")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def insert_rereply_notice(doc, ignore_permissions=False):
    """Insert once per business notice, or return its existing durable intent.

    The unique database key closes the race after the optimistic lookup. A
    failed or uncertain prior attempt keeps its reservation; explicit retry
    must reuse that row so another document event cannot resend it silently.
    """
    doc.set_whatsapp_account()
    account = frappe.get_doc("WhatsApp Account", doc.whatsapp_account)
    if not uses_rereply(account) or not doc.get("rereply_after_send"):
        return doc.insert(ignore_permissions=ignore_permissions)
    if doc.type != "Outgoing":
        frappe.throw("Only outgoing ReReply messages can reserve a business notice.")
    if not ignore_permissions:
        doc.check_permission("create")
    key = compute_notice_key(doc, account)
    doc.rereply_notice_key = key
    existing = frappe.db.get_value("WhatsApp Message", {"rereply_notice_key": key}, "name")
    if existing:
        result = frappe.get_doc("WhatsApp Message", existing)
        if not ignore_permissions:
            result.check_permission("read")
        return result
    savepoint = "rereply_notice_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    message_log = getattr(frappe.local, "message_log", None)
    message_count = len(message_log) if isinstance(message_log, list) else None
    try:
        return doc.insert(ignore_permissions=ignore_permissions)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point=savepoint)
        # A normal SELECT can retain a pre-race repeatable-read snapshot.
        # The locking read sees the winning insert after its commit.
        rows = frappe.db.sql(
            "SELECT name FROM `tabWhatsApp Message` WHERE rereply_notice_key=%s FOR UPDATE", (key,)
        )
        if not rows:
            raise
        # Frappe may append a uniqueness popup before throwing. This race is
        # handled, so remove only messages from this failed insert attempt.
        if message_count is not None:
            del message_log[message_count:]
        result = frappe.get_doc("WhatsApp Message", rows[0][0])
        if not ignore_permissions:
            result.check_permission("read")
        return result


def prepare_message(doc, account, payload):
    if doc.get("rereply_send_state") in ("Sending", "Sent", "Unknown"):
        frappe.throw("This ReReply message was already attempted. Reconcile its delivery before sending again.")
    if account.get("status") != "Active":
        frappe.throw("The ReReply WhatsApp account is inactive.")
    doc.rereply_payload = json.dumps(payload, ensure_ascii=False, default=str)
    doc.rereply_send_state = "Queued"
    doc.rereply_error = None
    doc.rereply_source = "erp"
    doc.rereply_requested_by = frappe.session.user
    doc.rereply_route_key = route_key(account)
    doc.rereply_notice_key = compute_notice_key(doc, account)
    doc.status = "Queued"


def enqueue_message(doc, method=None):
    if doc.get("rereply_send_state") != "Queued":
        return
    frappe.enqueue(
        "frappe_whatsapp.utils.rereply_queue.send_queued_message",
        message_name=doc.name, queue="short", enqueue_after_commit=True,
    )


def _set_result(name, **values):
    frappe.db.set_value("WhatsApp Message", name, values)


def send_queued_message(message_name):
    """Background job only. A committed claim fences concurrent workers."""
    from frappe_whatsapp.utils.rereply_client import send_via_rereply, ReReplyError

    rows = frappe.db.sql(
        "SELECT name FROM `tabWhatsApp Message` WHERE name=%s FOR UPDATE",
        (message_name,), as_dict=True,
    )
    if not rows:
        frappe.db.rollback()
        return
    doc = frappe.get_doc("WhatsApp Message", message_name)
    account = frappe.get_doc("WhatsApp Account", doc.whatsapp_account)
    if (doc.type != "Outgoing" or doc.get("rereply_send_state") != "Queued"
            or not uses_rereply(account) or account.get("status") != "Active"
            or not account.get("rereply_outbound_enabled")):
        frappe.db.rollback()
        return
    if doc.get("rereply_route_key") != route_key(account):
        _set_result(doc.name, rereply_send_state="Failed", status="Failed",
                    rereply_error="The configured destination changed after this message was queued.")
        frappe.db.commit()
        return
    try:
        payload = json.loads(doc.rereply_payload)
    except (ValueError, TypeError):
        _set_result(doc.name, rereply_send_state="Failed", status="Failed", rereply_error="Invalid queued payload")
        frappe.db.commit()
        return

    _set_result(doc.name, rereply_send_state="Sending", status="Sending", rereply_error=None)
    # Background worker boundary: the durable claim must precede network I/O.
    frappe.db.commit()
    try:
        result = send_via_rereply(account, payload,
                                media_loader=lambda url: load_erp_media(url, doc.rereply_requested_by))
        values = {
            "rereply_send_state": "Sent", "status": (result.get("rereply_status") or "Pending").title(), "rereply_error": None,
            "message_id": result["messages"][0]["id"],
            "rereply_message_id": result.get("rereply_message_id"),
            "rereply_contact_id": result.get("rereply_contact_id"),
        }
        if result.get("rereply_conversation_id"):
            values["conversation_id"] = result["rereply_conversation_id"]
        _set_result(doc.name, **values)
        if values["status"] != "Failed" and doc.get("rereply_after_send"):
            update = json.loads(doc.rereply_after_send)
            frappe.db.set_value(update["doctype"], update["name"], update["fieldname"], update["value"])
        frappe.db.commit()
    except ReReplyError as exc:
        frappe.db.rollback()
        safe = bool(getattr(exc, "safe_to_retry", False))
        state = "Failed" if safe else "Unknown"
        # The client exposes sanitized errors, never upstream bodies or keys.
        _set_result(doc.name, rereply_send_state=state, status=state, rereply_error=str(exc)[:500])
        frappe.db.commit()
    except Exception:
        # Even a DB failure after a successful HTTP response is ambiguous.
        frappe.db.rollback()
        _set_result(doc.name, rereply_send_state="Unknown", status="Unknown",
                    rereply_error="Delivery outcome could not be recorded. Check ReReply before retrying.")
        frappe.db.commit()


def recover_queued_messages():
    """Scheduler backstop for a Redis outage or a worker process exit."""
    accounts = frappe.get_all("WhatsApp Account", filters={
        "transport_provider": "ReReply", "status": "Active", "rereply_outbound_enabled": 1,
    }, pluck="name")
    if not accounts:
        return
    stale_before = add_to_date(now_datetime(), minutes=-15)
    # A stale Sending row may already have reached WhatsApp. Never requeue it.
    frappe.db.sql(
        "UPDATE `tabWhatsApp Message` SET rereply_send_state='Unknown', status='Unknown', "
        "rereply_error=%s WHERE rereply_send_state='Sending' AND modified < %s",
        ("Worker stopped during delivery; reconcile in ReReply before retrying.", stale_before),
    )
    for name in frappe.get_all("WhatsApp Message", filters={
        "whatsapp_account": ["in", accounts], "rereply_send_state": "Queued", "type": "Outgoing",
    }, pluck="name", order_by="creation asc", limit_page_length=100):
        frappe.enqueue("frappe_whatsapp.utils.rereply_queue.send_queued_message",
                       message_name=name, queue="short", enqueue_after_commit=True)
    reconcile_statuses(accounts)


def reconcile_statuses(accounts):
    """Read status without marking customer messages read."""
    from frappe_whatsapp.utils.rereply_client import get_message_status

    rows = frappe.get_all("WhatsApp Message", filters={
        "whatsapp_account": ["in", accounts], "type": "Outgoing",
        "rereply_send_state": "Sent", "status": ["not in", ["Read", "Failed"]],
        "creation": [">", add_to_date(now_datetime(), days=-7)],
    }, fields=["name", "whatsapp_account", "rereply_message_id", "rereply_contact_id"],
        order_by="rereply_status_checked_at asc, creation asc", limit_page_length=50)
    for row in rows:
        if not row.rereply_message_id or not row.rereply_contact_id:
            continue
        try:
            account = frappe.get_doc("WhatsApp Account", row.whatsapp_account)
            result = get_message_status(account, row.rereply_contact_id, row.rereply_message_id)
            if not result:
                continue
            status = str(result.get("status", "")).lower()
            values = {}
            if status in ("sent", "delivered", "read", "failed"):
                values["status"] = status.title()
            if result.get("wamid"):
                values["message_id"] = result["wamid"]
            if values:
                frappe.db.set_value("WhatsApp Message", row.name, values, update_modified=False)
        except Exception:
            # Read-only reconciliation can retry on a later scheduler tick.
            continue
        finally:
            frappe.db.set_value("WhatsApp Message", row.name, "rereply_status_checked_at", now_datetime(), update_modified=False)


def retry_rejected_message(message_name):
    """Background bulk retry only: never erase an accepted or uncertain ID."""
    rows = frappe.db.sql("SELECT name FROM `tabWhatsApp Message` WHERE name=%s FOR UPDATE", (message_name,))
    if not rows:
        return False
    doc = frappe.get_doc("WhatsApp Message", message_name)
    if doc.get("rereply_send_state") != "Failed" or doc.get("rereply_message_id") or doc.get("message_id"):
        frappe.db.rollback()
        return False
    account = frappe.get_doc("WhatsApp Account", doc.whatsapp_account)
    if not uses_rereply(account) or doc.get("rereply_route_key") != route_key(account):
        frappe.db.rollback()
        return False
    _set_result(doc.name, rereply_send_state="Queued", status="Queued", rereply_error=None)
    frappe.db.commit()
    send_queued_message(doc.name)
    return True


def load_erp_media(url, requested_by=None):
    """Read same-site attachments locally; never forward ERP auth to a URL.

    Other HTTPS media URLs are handled by the client's bounded public fetch.
    Same-site private files require the original requester's read permission;
    the background worker's privileges never grant attachment access.
    """
    from urllib.parse import parse_qs, unquote, urlsplit

    parsed = urlsplit(url)
    site = urlsplit(frappe.utils.get_url())
    if parsed.netloc and parsed.netloc != site.netloc:
        return None
    path = unquote(parsed.path)
    if path.startswith(("/files/", "/private/files/")):
        file_name = frappe.db.get_value("File", {"file_url": path}, "name")
        if not file_name:
            raise ValueError("ERP attachment was not found")
        file_doc = frappe.get_doc("File", file_name)
        if not requested_by or not frappe.has_permission("File", "read", doc=file_doc, user=requested_by):
            raise PermissionError("The message requester cannot read this ERP attachment")
        if (file_doc.get("file_size") or 0) > 14 * 1024 * 1024:
            raise ValueError("The ERP attachment exceeds the ReReply upload limit")
        content = file_doc.get_content()
        if isinstance(content, str):
            content = content.encode()
        import mimetypes
        return file_doc.file_name, content, mimetypes.guess_type(file_doc.file_name)[0] or "application/octet-stream"
    if path == "/api/method/frappe.utils.print_format.download_pdf":
        query = parse_qs(parsed.query)
        doctype = query.get("doctype", [None])[0]
        name = query.get("name", [None])[0]
        if not doctype or not name or not requested_by:
            raise PermissionError("The ERP print attachment requires a document and requester")
        print_doc = frappe.get_doc(doctype, name)
        if not frappe.has_permission(doctype, "read", doc=print_doc, user=requested_by):
            raise PermissionError("The message requester cannot read this ERP document")
        # The client fetches this existing signed print link with no session
        # cookie or ERP credential, after the requester permission check.
    return None
