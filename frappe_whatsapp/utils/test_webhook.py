# Copyright (c) 2025, Shridhar Patil and Contributors
# See license.txt

import json
from unittest.mock import patch, MagicMock, PropertyMock

import frappe
from frappe_whatsapp.testing import IntegrationTestCase

from frappe_whatsapp.utils.webhook import (
    update_message_status,
    update_status,
    update_template_status,
)
from frappe_whatsapp.utils.webhook_security import meta_signature, verify_meta_signature


class TestWebhookHelpers(IntegrationTestCase):
    """Tests for webhook helper functions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ensure_test_account()

    @classmethod
    def _ensure_test_account(cls):
        if not frappe.db.exists("WhatsApp Account", "Test WA Webhook Account"):
            account = frappe.get_doc({
                "doctype": "WhatsApp Account",
                "account_name": "Test WA Webhook Account",
                "status": "Active",
                "url": "https://graph.facebook.com",
                "version": "v17.0",
                "phone_id": "webhook_test_phone_id",
                "business_id": "webhook_test_business_id",
                "app_id": "webhook_test_app_id",
                "webhook_verify_token": "webhook_test_verify_token",
                "is_default_incoming": 1,
                "is_default_outgoing": 1,
            })
            account.insert(ignore_permissions=True)
            from frappe.utils.password import set_encrypted_password
            set_encrypted_password("WhatsApp Account", account.name, "test_webhook_token", "token")
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def setUp(self):
        # Set password within each test's transaction scope
        from frappe.utils.password import set_encrypted_password
        set_encrypted_password("WhatsApp Account", "Test WA Webhook Account", "test_webhook_token", "token")

    def tearDown(self):
        for name in frappe.get_all("WhatsApp Message", filters={"message_id": ["like", "wamid.webhook_%"]}, pluck="name"):
            frappe.delete_doc("WhatsApp Message", name, force=True)
        for name in frappe.get_all("WhatsApp Notification Log", filters={"template": "Webhook"}, pluck="name"):
            frappe.delete_doc("WhatsApp Notification Log", name, force=True)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def test_update_status_template_status(self):
        """Test update_status routes to template status update."""
        data = {
            "field": "message_template_status_update",
            "value": {
                "event": "APPROVED",
                "message_template_id": "999999",
            }
        }
        # Should not raise
        update_status(data)

    def test_update_message_status(self):
        """Test update_message_status updates WhatsApp Message status."""
        # Create a message first
        msg = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "919900112233",
            "message": "Status test",
            "message_id": "wamid.webhook_status_test",
            "content_type": "text",
            "whatsapp_account": "Test WA Webhook Account",
        })
        msg.flags.ignore_validate = True
        msg.db_insert()
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

        data = {
            "statuses": [{
                "id": "wamid.webhook_status_test",
                "status": "delivered",
                "conversation": {"id": "conv_123"}
            }]
        }
        update_message_status(data)

        msg.reload()
        self.assertEqual(msg.status, "delivered")
        self.assertEqual(msg.conversation_id, "conv_123")

    def test_update_message_status_without_conversation(self):
        """Test update_message_status when no conversation ID."""
        msg = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "919900112234",
            "message": "No conv test",
            "message_id": "wamid.webhook_no_conv",
            "content_type": "text",
            "whatsapp_account": "Test WA Webhook Account",
        })
        msg.flags.ignore_validate = True
        msg.db_insert()
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

        data = {
            "statuses": [{
                "id": "wamid.webhook_no_conv",
                "status": "sent",
            }]
        }
        update_message_status(data)

        msg.reload()
        self.assertEqual(msg.status, "sent")

    def test_update_template_status(self):
        """Test update_template_status updates template status via SQL."""
        # Create a template directly
        template_name = "test_webhook_tmpl-en"
        if not frappe.db.exists("WhatsApp Templates", template_name):
            doc = frappe.get_doc({
                "doctype": "WhatsApp Templates",
                "template_name": "test_webhook_tmpl",
                "actual_name": "test_webhook_tmpl",
                "template": "Webhook test template",
                "category": "TRANSACTIONAL",
                "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
                "language_code": "en",
                "whatsapp_account": "Test WA Webhook Account",
                "status": "PENDING",
                "id": "webhook_tmpl_id_123",
            })
            doc.db_insert()
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

        data = {
            "event": "APPROVED",
            "message_template_id": "webhook_tmpl_id_123",
        }
        update_template_status(data)

        status = frappe.db.get_value("WhatsApp Templates", {"id": "webhook_tmpl_id_123"}, "status")
        self.assertEqual(status, "APPROVED")


class TestWebhookEndpoint(IntegrationTestCase):
    """Tests for the webhook endpoint."""

    EP_APP_SECRET = "test_webhook_ep_app_secret"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not frappe.db.exists("WhatsApp Account", "Test WA Webhook EP Account"):
            account = frappe.get_doc({
                "doctype": "WhatsApp Account",
                "account_name": "Test WA Webhook EP Account",
                "status": "Active",
                "url": "https://graph.facebook.com",
                "version": "v17.0",
                "phone_id": "webhook_ep_phone_id",
                "business_id": "webhook_ep_business_id",
                "app_id": "webhook_ep_app_id",
                "webhook_verify_token": "Test WA Webhook EP Account",
                "is_default_incoming": 1,
                "is_default_outgoing": 1,
            })
            account.insert(ignore_permissions=True)
            from frappe.utils.password import set_encrypted_password
            set_encrypted_password("WhatsApp Account", account.name, "ep_token", "token")
            set_encrypted_password(
                "WhatsApp Account", account.name, cls.EP_APP_SECRET, "app_secret"
            )
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def setUp(self):
        # Set password within each test's transaction scope
        from frappe.utils.password import set_encrypted_password
        set_encrypted_password("WhatsApp Account", "Test WA Webhook EP Account", "ep_token", "token")
        set_encrypted_password(
            "WhatsApp Account",
            "Test WA Webhook EP Account",
            self.EP_APP_SECRET,
            "app_secret",
        )
        # Clear ALL defaults then set ours (db.set_value bypasses on_update hooks)
        frappe.db.sql("UPDATE `tabWhatsApp Account` SET is_default_outgoing=0, is_default_incoming=0")
        frappe.db.set_value("WhatsApp Account", "Test WA Webhook EP Account", {
            "is_default_outgoing": 1,
            "is_default_incoming": 1,
        })

    def tearDown(self):
        for name in frappe.get_all("WhatsApp Message", filters={"message_id": ["like", "wamid.webhook_ep_%"]}, pluck="name"):
            frappe.delete_doc("WhatsApp Message", name, force=True)
        for name in frappe.get_all("WhatsApp Notification Log", filters={"template": "Webhook"}, pluck="name"):
            frappe.delete_doc("WhatsApp Notification Log", name, force=True)
        for name in frappe.get_all("WhatsApp Profiles", filters={"number": ["like", "9199%"]}, pluck="name"):
            frappe.delete_doc("WhatsApp Profiles", name, force=True)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def _make_mock_request(self, method="GET", payload=None):
        """Create a mock request object."""
        mock_request = MagicMock()
        mock_request.method = method
        mock_request.headers = {}
        if payload is not None:
            raw_body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            mock_request.get_data.return_value = raw_body
            mock_request.headers["X-Hub-Signature-256"] = meta_signature(
                self.EP_APP_SECRET, raw_body
            )
        return mock_request

    def test_webhook_get_verification(self):
        """Test GET webhook verification."""
        mock_request = self._make_mock_request("GET")
        frappe.local.form_dict = frappe._dict({
            "hub.challenge": "test_challenge_123",
            "hub.verify_token": "Test WA Webhook EP Account",
            "hub.mode": "subscribe",
        })

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            response = webhook()
            self.assertEqual(response.status_code, 200)

    def test_webhook_get_wrong_token(self):
        """Test GET webhook with wrong verify token."""
        mock_request = self._make_mock_request("GET")
        frappe.local.form_dict = frappe._dict({
            "hub.challenge": "test_challenge",
            "hub.verify_token": "wrong_token",
        })

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            with self.assertRaises(frappe.ValidationError):
                webhook()

    def test_webhook_post_text_message(self):
        """Test POST webhook with incoming text message."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "contacts": [{"profile": {"name": "Test Sender"}}],
                        "messages": [{
                            "from": "919900112288",
                            "id": "wamid.webhook_ep_text_1",
                            "type": "text",
                            "text": {"body": "Hello from webhook test"},
                        }]
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        frappe.local.form_dict = frappe._dict(payload)

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            webhook()

        self.assertTrue(
            frappe.db.exists("WhatsApp Message", {"message_id": "wamid.webhook_ep_text_1"})
        )
        msg = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.webhook_ep_text_1"})
        self.assertEqual(msg.type, "Incoming")
        self.assertEqual(msg.message, "Hello from webhook test")
        self.assertEqual(msg.content_type, "text")

    def test_webhook_rejects_invalid_signature_before_writes(self):
        """An invalid signature must not create a log or message."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "contacts": [{"profile": {"name": "Forged Sender"}}],
                        "messages": [{
                            "from": "919900112280",
                            "id": "wamid.webhook_ep_invalid_signature",
                            "type": "text",
                            "text": {"body": "This must not be stored"},
                        }],
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        mock_request.headers["X-Hub-Signature-256"] = "sha256=" + ("0" * 64)
        log_count = frappe.db.count(
            "WhatsApp Notification Log", filters={"template": "Webhook"}
        )

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            with self.assertRaises(frappe.AuthenticationError):
                webhook()

        self.assertFalse(
            frappe.db.exists(
                "WhatsApp Message",
                {"message_id": "wamid.webhook_ep_invalid_signature"},
            )
        )
        self.assertEqual(
            log_count,
            frappe.db.count(
                "WhatsApp Notification Log", filters={"template": "Webhook"}
            ),
        )

    def test_webhook_rejects_missing_signature_before_writes(self):
        """A missing signature must fail exactly like an invalid signature."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "messages": [{
                            "from": "919900112279",
                            "id": "wamid.webhook_ep_missing_signature",
                            "type": "text",
                            "text": {"body": "This must not be stored"},
                        }],
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        mock_request.headers.pop("X-Hub-Signature-256")
        log_count = frappe.db.count(
            "WhatsApp Notification Log", filters={"template": "Webhook"}
        )

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            with self.assertRaises(frappe.AuthenticationError):
                webhook()

        self.assertFalse(
            frappe.db.exists(
                "WhatsApp Message",
                {"message_id": "wamid.webhook_ep_missing_signature"},
            )
        )
        self.assertEqual(
            log_count,
            frappe.db.count(
                "WhatsApp Notification Log", filters={"template": "Webhook"}
            ),
        )

    def test_webhook_rejects_unknown_account_before_writes(self):
        """A valid HMAC cannot authorize routing metadata for an unknown account."""
        payload = {
            "entry": [{
                "id": "unknown_business_id",
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "unknown_phone_id"},
                        "messages": [{
                            "from": "919900112278",
                            "id": "wamid.webhook_ep_unknown_account",
                            "type": "text",
                            "text": {"body": "This must not be stored"},
                        }],
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        log_count = frappe.db.count(
            "WhatsApp Notification Log", filters={"template": "Webhook"}
        )

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            with self.assertRaises(frappe.AuthenticationError):
                webhook()

        self.assertFalse(
            frappe.db.exists(
                "WhatsApp Message",
                {"message_id": "wamid.webhook_ep_unknown_account"},
            )
        )
        self.assertEqual(
            log_count,
            frappe.db.count(
                "WhatsApp Notification Log", filters={"template": "Webhook"}
            ),
        )

    def test_webhook_rejects_account_without_app_secret(self):
        """Signature enforcement must fail closed when the routed secret is absent."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "messages": [{
                            "from": "919900112277",
                            "id": "wamid.webhook_ep_missing_secret",
                            "type": "text",
                            "text": {"body": "This must not be stored"},
                        }],
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        account_without_secret = MagicMock()
        account_without_secret.get_password.return_value = None
        log_count = frappe.db.count(
            "WhatsApp Notification Log", filters={"template": "Webhook"}
        )

        with (
            patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request),
            patch(
                "frappe_whatsapp.utils.webhook.frappe.get_doc",
                return_value=account_without_secret,
            ),
        ):
            from frappe_whatsapp.utils.webhook import webhook
            with self.assertRaises(frappe.AuthenticationError):
                webhook()

        self.assertFalse(
            frappe.db.exists(
                "WhatsApp Message",
                {"message_id": "wamid.webhook_ep_missing_secret"},
            )
        )
        self.assertEqual(
            log_count,
            frappe.db.count(
                "WhatsApp Notification Log", filters={"template": "Webhook"}
            ),
        )

    def test_signature_helper_rejects_missing_or_malformed_inputs(self):
        """The low-level verifier must fail closed for every absent input."""
        raw_body = b'{"entry":[]}'
        valid_signature = meta_signature(self.EP_APP_SECRET, raw_body)

        self.assertFalse(verify_meta_signature("", raw_body, valid_signature))
        self.assertFalse(
            verify_meta_signature(self.EP_APP_SECRET, raw_body, "")
        )
        self.assertFalse(
            verify_meta_signature(
                self.EP_APP_SECRET,
                raw_body,
                "sha256=not-a-valid-sha256-digest",
            )
        )
        self.assertFalse(
            verify_meta_signature(self.EP_APP_SECRET, "not-bytes", valid_signature)
        )

    def test_webhook_signature_is_bound_to_exact_raw_bytes(self):
        """Semantically equal JSON with different bytes must need a new signature."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "messages": [{
                            "from": "919900112281",
                            "id": "wamid.webhook_ep_tampered_bytes",
                            "type": "text",
                            "text": {"body": "Signed body"},
                        }],
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        mock_request.get_data.return_value += b"\n"

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            with self.assertRaises(frappe.AuthenticationError):
                webhook()

        self.assertFalse(
            frappe.db.exists(
                "WhatsApp Message",
                {"message_id": "wamid.webhook_ep_tampered_bytes"},
            )
        )

    def test_webhook_post_reaction_message(self):
        """Test POST webhook with reaction message."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "contacts": [{"profile": {"name": "Reactor"}}],
                        "messages": [{
                            "from": "919900112289",
                            "id": "wamid.webhook_ep_reaction_1",
                            "type": "reaction",
                            "reaction": {
                                "emoji": "\U0001f44d",
                                "message_id": "wamid.original_reacted_to"
                            },
                        }]
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        frappe.local.form_dict = frappe._dict(payload)

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            webhook()

        self.assertTrue(
            frappe.db.exists("WhatsApp Message", {"message_id": "wamid.webhook_ep_reaction_1"})
        )
        msg = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.webhook_ep_reaction_1"})
        self.assertEqual(msg.content_type, "reaction")
        self.assertEqual(msg.message, "\U0001f44d")

    def test_webhook_post_button_message(self):
        """Test POST webhook with button reply message."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "contacts": [{"profile": {"name": "Button User"}}],
                        "messages": [{
                            "from": "919900112290",
                            "id": "wamid.webhook_ep_button_1",
                            "type": "button",
                            "button": {"text": "Yes, confirm"},
                        }]
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        frappe.local.form_dict = frappe._dict(payload)

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            webhook()

        msg = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.webhook_ep_button_1"})
        self.assertEqual(msg.content_type, "button")
        self.assertEqual(msg.message, "Yes, confirm")

    def test_webhook_post_reply_message(self):
        """Test POST webhook with reply context."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "contacts": [{"profile": {"name": "Reply User"}}],
                        "messages": [{
                            "from": "919900112291",
                            "id": "wamid.webhook_ep_reply_1",
                            "type": "text",
                            "text": {"body": "This is a reply"},
                            "context": {"id": "wamid.original_msg_replied_to"},
                        }]
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        frappe.local.form_dict = frappe._dict(payload)

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            webhook()

        msg = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.webhook_ep_reply_1"})
        self.assertEqual(msg.is_reply, 1)
        self.assertEqual(msg.reply_to_message_id, "wamid.original_msg_replied_to")

    def test_webhook_post_status_update(self):
        """Test POST webhook with status update (no messages)."""
        # Create a message to update
        msg = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "919900112292",
            "message": "Status update test",
            "message_id": "wamid.webhook_ep_status_1",
            "content_type": "text",
            "whatsapp_account": "Test WA Webhook EP Account",
        })
        msg.flags.ignore_validate = True
        msg.db_insert()
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

        payload = {
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "statuses": [{
                            "id": "wamid.webhook_ep_status_1",
                            "status": "read",
                            "conversation": {"id": "conv_456"}
                        }]
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        frappe.local.form_dict = frappe._dict(payload)

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            webhook()

        msg.reload()
        self.assertEqual(msg.status, "read")
        self.assertEqual(msg.conversation_id, "conv_456")

    def test_webhook_post_template_status_update(self):
        """Template status updates have no metadata.phone_number_id; the handler
        must still route them to update_status. Regression for PR #231."""
        template_id = "webhook_ep_tmpl_id_status"
        template_name = "ep_status_template-en"

        # Seed the template (or reset its status if a prior run left it APPROVED)
        if frappe.db.exists("WhatsApp Templates", template_name):
            frappe.db.set_value(
                "WhatsApp Templates", template_name, "status", "PENDING"
            )
        else:
            doc = frappe.get_doc({
                "doctype": "WhatsApp Templates",
                "template_name": "ep_status_template",
                "actual_name": "ep_status_template",
                "template": "Status webhook regression template",
                "category": "TRANSACTIONAL",
                "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
                "language_code": "en",
                "whatsapp_account": "Test WA Webhook EP Account",
                "status": "PENDING",
                "id": template_id,
            })
            doc.db_insert()
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

        payload = {
            "entry": [{
                "id": "webhook_ep_business_id",
                "changes": [{
                    "field": "message_template_status_update",
                    "value": {
                        "event": "APPROVED",
                        "message_template_id": template_id,
                        "message_template_name": "ep_status_template",
                        "message_template_language": "en_US",
                        "reason": None,
                    },
                }],
            }],
        }
        mock_request = self._make_mock_request("POST", payload)
        frappe.local.form_dict = frappe._dict(payload)

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            webhook()

        status = frappe.db.get_value(
            "WhatsApp Templates", {"id": template_id}, "status"
        )
        self.assertEqual(status, "APPROVED")

    def test_webhook_creates_notification_log(self):
        """Test that webhook POST creates a notification log entry."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "webhook_ep_phone_id"},
                        "contacts": [{"profile": {"name": "Log Test"}}],
                        "messages": [{
                            "from": "919900112293",
                            "id": "wamid.webhook_ep_log_1",
                            "type": "text",
                            "text": {"body": "Log test"},
                        }]
                    }
                }]
            }]
        }
        mock_request = self._make_mock_request("POST", payload)
        frappe.local.form_dict = frappe._dict(payload)

        with patch("frappe_whatsapp.utils.webhook.frappe.request", mock_request):
            from frappe_whatsapp.utils.webhook import webhook
            webhook()

        # Should have created a notification log
        logs = frappe.get_all("WhatsApp Notification Log", filters={"template": "Webhook"})
        self.assertTrue(len(logs) > 0)
