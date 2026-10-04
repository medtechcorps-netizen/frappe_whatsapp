"""Real Frappe/SQL tests for signed ingress and durable receipt processing.

Run only on a disposable test site:
``bench --site test_site run-tests --module
frappe_whatsapp.utils.test_rereply_webhook_integration``.
HTTP and Redis enqueue are blocked/mocked; document hooks and SQL are real.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils.password import set_encrypted_password
from werkzeug.wrappers import Request

from frappe_whatsapp.testing import IntegrationTestCase
from frappe_whatsapp.utils import rereply_webhook


class TestReReplyWebhookIntegration(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.original_user = frappe.session.user
        frappe.set_user("Administrator")
        self.suffix = uuid4().hex[:12]
        self.phone = "919902" + str(uuid4().int % 10**7).zfill(7)
        self.secret = uuid4().hex + uuid4().hex
        self.account_name = "ReReply webhook test " + self.suffix
        self._patches = [
            patch("frappe.enqueue"),
            patch("requests.sessions.Session.request", side_effect=AssertionError("No HTTP in webhook tests")),
            patch("frappe_whatsapp.utils.rereply_client.send_via_rereply",
                  side_effect=AssertionError("Webhook ingestion must not send")),
            patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.make_post_request",
                  side_effect=AssertionError("ReReply webhook must not call Meta")),
        ]
        self.mocks = [patcher.start() for patcher in self._patches]
        self.enqueue, self.http, self.send, self.meta_send = self.mocks
        # Also clean up if fixture construction fails before tearDown runs.
        self.addCleanup(self._cleanup)
        self.account = frappe.get_doc({
            "doctype": "WhatsApp Account", "account_name": self.account_name,
            "status": "Active", "transport_provider": "ReReply",
            "url": "https://graph.facebook.com", "version": "v24.0",
            "phone_id": "webhook-phone-" + self.suffix,
            "business_id": "webhook-business-" + self.suffix,
            "webhook_verify_token": "webhook-verify-" + self.suffix,
            "is_default_incoming": 0, "is_default_outgoing": 0,
            "rereply_base_url": "https://app.rereply.app",
            "rereply_workspace_id": str(uuid4()), "rereply_account_id": str(uuid4()),
            "rereply_account_name": "Webhook provider " + self.suffix,
            "rereply_integration_user_id": str(uuid4()),
            "rereply_inbound_enabled": 0, "rereply_outbound_enabled": 0,
        }).insert(ignore_permissions=True)
        set_encrypted_password("WhatsApp Account", self.account.name,
                               "webhook-test-key-not-a-real-credential", "rereply_api_key")
        set_encrypted_password("WhatsApp Account", self.account.name,
                               self.secret, "rereply_webhook_secret")
        self.account.rereply_inbound_enabled = 1
        self.account.rereply_outbound_enabled = 1
        self.account.save(ignore_permissions=True)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- fixture must survive worker rollback
        self.enqueue.reset_mock()

    def _cleanup(self):
        if getattr(self, "_cleaned_up", False):
            return
        self._cleaned_up = True
        try:
            frappe.set_user("Administrator")
            frappe.db.rollback()
            frappe.db.delete("ReReply Webhook Receipt", {"whatsapp_account": self.account_name})
            frappe.db.delete("WhatsApp Message", {"whatsapp_account": self.account_name})
            frappe.db.delete("WhatsApp Profiles", {"whatsapp_account": self.account_name})
            if frappe.db.exists("DocType", "WhatsApp Bot Pause"):
                frappe.db.delete("WhatsApp Bot Pause", {"phone": self.phone})
            if frappe.db.exists("WhatsApp Account", self.account_name):
                frappe.delete_doc("WhatsApp Account", self.account_name, ignore_permissions=True, force=True)
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove only these committed fixtures
        finally:
            for patcher in reversed(self._patches):
                patcher.stop()
            frappe.set_user(self.original_user)

    def tearDown(self):
        try:
            self._cleanup()
        finally:
            super().tearDown()

    def _payload(self, event="message.incoming"):
        message_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        return {
            "event": event, "timestamp": now,
            "data": {
                "outbox_event_id": str(uuid4()), "message_id": message_id,
                "source_id": message_id, "source_type": "message", "event_type": event,
                "direction": "incoming" if event == "message.incoming" else "outgoing",
                "actor_type": "contact", "occurred_at": now,
                "whatsapp_account": self.account.rereply_account_name,
                "contact_id": str(uuid4()), "contact_phone": self.phone,
                "contact_name": "Webhook fixture " + self.suffix,
                "message_type": "text", "content": "Integration ingress 你好",
                "metadata": {"whatsapp_account": self.account.rereply_account_name},
            },
        }

    def _post(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        signature = "sha256=" + hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        request = Request.from_values(method="POST", data=raw, content_type="application/json", headers={
            "X-ReReply-ERP-Account": self.account.name, "X-Webhook-Signature": signature,
        })
        # Frappe v14-v16 expose frappe.request through this thread-local slot.
        with patch.object(frappe.local, "request", request, create=True):
            return rereply_webhook.webhook(), raw

    def _accept(self, payload):
        result, raw = self._post(payload)
        self.assertEqual(result["status"], "accepted")
        # Match the real POST lifecycle before dispatching a worker.
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- simulate successful POST commit
        return result["receipt"], raw

    def _count_messages(self):
        return frappe.db.count("WhatsApp Message", {"whatsapp_account": self.account.name})

    def _assert_no_send(self):
        self.http.assert_not_called()
        self.send.assert_not_called()
        self.meta_send.assert_not_called()

    def test_receipt_hash_name_and_unique_event_key_are_real_database_constraints(self):
        payload = self._payload()
        name, _ = self._accept(payload)
        expected = hashlib.sha256(json.dumps([
            self.account.rereply_workspace_id, self.account.rereply_account_id,
            payload["data"]["message_id"],
        ], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(name, expected)
        original = frappe.get_doc("ReReply Webhook Receipt", name)
        self.assertEqual(original.name, expected)
        self.assertEqual(original.status, "Pending")
        self.assertTrue(frappe.get_meta("ReReply Webhook Receipt").get_field("event_key").unique)

        duplicate = frappe.get_doc({
            "doctype": "ReReply Webhook Receipt", "event_key": original.event_key,
            "event_id": original.event_id, "provider_message_id": str(uuid4()),
            "whatsapp_account": self.account.name, "status": "Pending",
        })
        savepoint = "receipt_unique_" + self.suffix
        frappe.db.savepoint(savepoint)
        try:
            with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
                duplicate.insert(ignore_permissions=True, set_name=uuid4().hex + uuid4().hex)
        finally:
            frappe.db.rollback(save_point=savepoint)
        self.assertEqual(frappe.db.count("ReReply Webhook Receipt", {
            "whatsapp_account": self.account.name,
        }), 1)
        self._assert_no_send()

    def test_signed_post_persists_pending_and_only_enqueues_after_commit(self):
        payload = self._payload()
        with patch.object(frappe.db, "commit", side_effect=AssertionError("Ingress must let the POST commit")):
            result, raw = self._post(payload)
        self.assertEqual(result["status"], "accepted")
        receipt = frappe.get_doc("ReReply Webhook Receipt", result["receipt"])
        self.assertEqual(receipt.status, "Pending")
        self.assertEqual(receipt.payload.encode("utf-8"), raw)
        self.assertEqual(receipt.payload_hash, hashlib.sha256(raw).hexdigest())
        self.assertEqual(self._count_messages(), 0)
        self.enqueue.assert_called_once()
        self.assertEqual(self.enqueue.call_args.args[0], "frappe_whatsapp.utils.rereply_webhook.process_receipt")
        self.assertEqual(self.enqueue.call_args.kwargs["name"], receipt.name)
        self.assertTrue(self.enqueue.call_args.kwargs["enqueue_after_commit"])
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- exercise request durability
        frappe.db.rollback()
        self.assertEqual(frappe.db.get_value("ReReply Webhook Receipt", receipt.name, "status"), "Pending")
        self._assert_no_send()

    def test_real_message_hooks_run_once_across_duplicate_delivery_and_worker(self):
        payload = self._payload()
        name, _ = self._accept(payload)
        self.enqueue.reset_mock()
        rereply_webhook.process_receipt(name)
        receipt = frappe.get_doc("ReReply Webhook Receipt", name)
        self.assertEqual(receipt.status, "Processed", receipt.error)
        message = frappe.get_doc("WhatsApp Message", receipt.whatsapp_message)
        self.assertEqual(message.type, "Incoming")
        self.assertEqual(message.get("from"), self.phone)
        self.assertEqual(message.whatsapp_account, self.account.name)
        self.assertEqual(message.rereply_message_id, payload["data"]["message_id"])
        self.assertEqual(message.message_id, "rereply:" + payload["data"]["message_id"])
        # create_whatsapp_profile is a real before_insert hook: db_insert
        # or bypassing document hooks would fail this assertion.
        self.assertTrue(frappe.db.exists("WhatsApp Profiles", {
            "number": self.phone, "whatsapp_account": self.account.name,
        }))
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        repeated, _ = self._post(payload)
        self.assertEqual(repeated["status"], "duplicate")
        rereply_webhook.process_receipt(name)
        self.assertEqual(self._count_messages(), 1)
        self.assertEqual(frappe.db.count("ReReply Webhook Receipt", {
            "whatsapp_account": self.account.name,
        }), 1)
        self.enqueue.assert_not_called()
        self._assert_no_send()

    def test_passive_mobile_media_completes_without_creating_or_sending_a_message(self):
        payload = self._payload("message.outgoing")
        payload["data"].pop("outbox_event_id")
        payload["data"].update(message_type="image", content=None)
        name, _ = self._accept(payload)
        self.enqueue.reset_mock()
        rereply_webhook.process_receipt(name)
        rereply_webhook.process_receipt(name)
        receipt = frappe.get_doc("ReReply Webhook Receipt", name)
        self.assertEqual(receipt.status, "Processed", receipt.error)
        self.assertIn("media activity", receipt.error)
        self.assertFalse(receipt.whatsapp_message)
        self.assertEqual(self._count_messages(), 0)
        self.enqueue.assert_not_called()
        self._assert_no_send()
