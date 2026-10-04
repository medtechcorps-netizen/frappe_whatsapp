"""Real Frappe/SQL regression tests for the ReReply transaction boundaries.

Run with ``bench --site test_site run-tests --module
frappe_whatsapp.utils.test_rereply_integration``. Only external transport and
Redis enqueue calls are mocked; DocType hooks, permissions and SQL are real.
"""

import json
from unittest.mock import patch
from uuid import uuid4

import frappe

from frappe_whatsapp.testing import IntegrationTestCase
from frappe_whatsapp.utils import rereply_queue
from frappe_whatsapp.utils.rereply_client import ReReplyAmbiguousSendError, ReReplyError


class TestReReplyIntegration(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.original_user = frappe.session.user
        frappe.set_user("Administrator")
        self.suffix = uuid4().hex[:12]
        self.phone = "919901" + str(uuid4().int % 10**7).zfill(7)
        self.file_names = []
        self.template_names = []
        self._patches = [
            patch("frappe.enqueue"),
            patch("requests.sessions.Session.request", side_effect=AssertionError("No live HTTP in integration tests")),
            patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.make_post_request",
                  side_effect=AssertionError("ReReply must not call Meta")),
            patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_notification.whatsapp_notification.make_post_request",
                  side_effect=AssertionError("ReReply notifications must not call Meta")),
        ]
        self.enqueue = self._patches[0].start()
        for patcher in self._patches[1:]:
            patcher.start()
        self.account = frappe.get_doc({
            "doctype": "WhatsApp Account",
            "account_name": "ReReply integration " + self.suffix,
            "status": "Active",
            "transport_provider": "ReReply",
            "url": "https://graph.facebook.com", "version": "v24.0",
            "phone_id": "test-phone-" + self.suffix,
            "business_id": "test-business-" + self.suffix,
            "webhook_verify_token": "test-verify-" + self.suffix,
            "rereply_base_url": "https://app.rereply.app",
            "rereply_workspace_id": str(uuid4()),
            "rereply_account_id": str(uuid4()),
            "rereply_account_name": "Test provider " + self.suffix,
            "rereply_outbound_enabled": 0, "rereply_inbound_enabled": 0,
            "is_default_incoming": 0, "is_default_outgoing": 0,
        }).insert(ignore_permissions=True)
        # Save through the Password field. Saving an empty field after writing
        # __Auth directly clears the encrypted value on Frappe 16.
        self.account.rereply_api_key = "integration-test-key-not-a-credential"
        self.account.rereply_outbound_enabled = 1
        self.account.save(ignore_permissions=True)
        self.account = frappe.get_doc("WhatsApp Account", self.account.name)
        self.assertEqual(self.account.get_password("rereply_api_key"),
                         "integration-test-key-not-a-credential")
        self.enqueue.reset_mock()

    def tearDown(self):
        try:
            frappe.set_user("Administrator")
            account_name = self.account.name
            # Worker tests intentionally exercise real commits. Remove only
            # this test's uniquely scoped fixtures even after an assertion fails.
            frappe.db.delete("WhatsApp Message", {"whatsapp_account": account_name})
            frappe.db.delete("WhatsApp Profiles", {"whatsapp_account": account_name})
            for template in self.template_names:
                frappe.db.delete("WhatsApp Notification Log", {"template": template})
                frappe.db.delete("WhatsApp Templates", {"name": template})
            for name in self.file_names:
                if frappe.db.exists("File", name):
                    frappe.delete_doc("File", name, ignore_permissions=True, force=True)
            frappe.delete_doc("WhatsApp Account", account_name, ignore_permissions=True, force=True)
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove committed worker-test fixtures
        finally:
            for patcher in reversed(self._patches):
                patcher.stop()
            frappe.set_user(self.original_user)
            super().tearDown()

    def _message(self, **overrides):
        values = {
            "doctype": "WhatsApp Message", "type": "Outgoing", "to": self.phone,
            "message": "Integration queue test", "message_type": "Manual",
            "content_type": "text", "whatsapp_account": self.account.name,
        }
        values.update(overrides)
        return frappe.get_doc(values).insert(ignore_permissions=True)

    def _accepted(self, status="sent"):
        message_id = str(uuid4())
        return {
            "messages": [{"id": "rereply:" + message_id}],
            "rereply_message_id": message_id,
            "rereply_contact_id": str(uuid4()),
            "rereply_status": status,
            "wamid": "",
        }

    def _notice(self, fieldname="allow_auto_read_receipt", value=1, **overrides):
        """An event notice referencing only this test's own real ERP document."""
        values = {
            "doctype": "WhatsApp Message", "type": "Outgoing", "to": self.phone,
            "message": "Business event notice", "message_type": "Manual", "content_type": "text",
            "whatsapp_account": self.account.name,
            "reference_doctype": "WhatsApp Account", "reference_name": self.account.name,
            "rereply_after_send": json.dumps({
                "doctype": "WhatsApp Account", "name": self.account.name,
                "fieldname": fieldname, "value": value,
            }),
        }
        values.update(overrides)
        return frappe.get_doc(values)

    def test_business_notice_repeated_event_returns_existing_in_every_delivery_state(self):
        first = self._notice().insert_rereply_notice(ignore_permissions=True)
        first_key = first.rereply_notice_key
        first_payload = first.rereply_payload
        self.assertTrue(first_key)
        self.enqueue.reset_mock()
        for state in ("Queued", "Sending", "Unknown", "Failed", "Sent"):
            with self.subTest(state=state):
                frappe.db.set_value("WhatsApp Message", first.name, {
                    "rereply_send_state": state, "status": state,
                })
                with patch.object(frappe.db, "commit", side_effect=AssertionError("Notice insert must not commit")):
                    duplicate = self._notice(message="Changed rendering of the same event").insert_rereply_notice(
                        ignore_permissions=True
                    )
                self.assertEqual(duplicate.name, first.name)
                self.assertEqual(duplicate.rereply_notice_key, first_key)
                self.assertEqual(duplicate.rereply_send_state, state)
                self.assertEqual(duplicate.rereply_payload, first_payload)
        self.assertEqual(frappe.db.count("WhatsApp Message", {"rereply_notice_key": first_key}), 1)
        self.enqueue.assert_not_called()

    def test_different_business_notice_flag_or_value_creates_distinct_intent(self):
        first = self._notice().insert_rereply_notice(ignore_permissions=True)
        other_flag = self._notice(fieldname="is_default_incoming").insert_rereply_notice(ignore_permissions=True)
        other_value = self._notice(value=0).insert_rereply_notice(ignore_permissions=True)
        rows = (first, other_flag, other_value)
        self.assertEqual(len({row.name for row in rows}), 3)
        self.assertEqual(len({row.rereply_notice_key for row in rows}), 3)
        self.assertEqual(self.enqueue.call_count, 3)

    def test_notice_key_is_enforced_by_database_unique_constraint(self):
        first = self._notice().insert_rereply_notice(ignore_permissions=True)
        duplicate = self._notice()
        duplicate.name = "notice-conflict-" + uuid4().hex
        duplicate.rereply_notice_key = first.rereply_notice_key
        savepoint = "notice_unique_" + self.suffix
        frappe.db.savepoint(savepoint)
        try:
            # Bypass document hooks deliberately: this proves that the database,
            # not only a prior application lookup, rejects a competing insert.
            with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
                duplicate.db_insert()
        finally:
            frappe.db.rollback(save_point=savepoint)
        self.assertEqual(frappe.db.count("WhatsApp Message", {"rereply_notice_key": first.rereply_notice_key}), 1)
        self.assertTrue(frappe.db.exists("WhatsApp Message", first.name))

    def test_notice_stale_precheck_recovers_winning_row_after_real_unique_error(self):
        first = self._notice().insert_rereply_notice(ignore_permissions=True)
        self.enqueue.reset_mock()
        get_value = frappe.db.get_value
        hidden_once = []

        def stale_precheck(*args, **kwargs):
            if (not hidden_once and len(args) >= 2 and args[0] == "WhatsApp Message"
                    and args[1] == {"rereply_notice_key": first.rereply_notice_key}):
                hidden_once.append(True)
                return None
            return get_value(*args, **kwargs)

        with patch.object(frappe.db, "get_value", side_effect=stale_precheck), \
                patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
            # Simulate the optimistic lookup missing a competing winner. The
            # ensuing INSERT and unique-constraint rejection still use real SQL.
            result = self._notice().insert_rereply_notice(ignore_permissions=True)
        self.assertEqual(hidden_once, [True])
        self.assertEqual(result.name, first.name)
        self.assertTrue(any("rereply_notice_key" in str(call.args[0])
                            and "FOR UPDATE" in str(call.args[0])
                            for call in sql.call_args_list if call.args))
        self.assertEqual(frappe.db.count("WhatsApp Message", {"rereply_notice_key": first.rereply_notice_key}), 1)
        self.enqueue.assert_not_called()

    def test_notice_descriptor_cannot_update_a_different_source_document(self):
        descriptor = {"doctype": "User", "name": "Administrator", "fieldname": "enabled", "value": 0}
        with self.assertRaises(frappe.ValidationError):
            self._notice(rereply_after_send=json.dumps(descriptor)).insert_rereply_notice(ignore_permissions=True)
        self.assertEqual(frappe.db.count("WhatsApp Message", {"whatsapp_account": self.account.name}), 0)
        self.enqueue.assert_not_called()

    def test_notice_retry_reuses_original_key_and_row(self):
        first = self._notice().insert_rereply_notice(ignore_permissions=True)
        first_key = first.rereply_notice_key
        with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply",
                   side_effect=ReReplyError("Preparation rejected before send")):
            rereply_queue.send_queued_message(first.name)
        first.reload()
        self.assertEqual(first.rereply_send_state, "Failed")
        duplicate = self._notice().insert_rereply_notice(ignore_permissions=True)
        self.assertEqual(duplicate.name, first.name)
        self.assertEqual(duplicate.rereply_send_state, "Failed")
        with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply", return_value=self._accepted()) as send:
            self.assertTrue(rereply_queue.retry_rejected_message(first.name))
            self.assertFalse(rereply_queue.retry_rejected_message(first.name))
        send.assert_called_once()
        first.reload()
        self.assertEqual(first.rereply_notice_key, first_key)
        self.assertEqual(self._notice().insert_rereply_notice(ignore_permissions=True).name, first.name)
        self.assertEqual(frappe.db.count("WhatsApp Message", {"rereply_notice_key": first_key}), 1)

    def test_ordinary_repeated_messages_are_not_business_notice_deduplicated(self):
        reference = {"reference_doctype": "WhatsApp Account", "reference_name": self.account.name}
        first = self._message(**reference)
        second = self._message(**reference)
        self.assertNotEqual(first.name, second.name)
        self.assertFalse(first.rereply_notice_key)
        self.assertFalse(second.rereply_notice_key)
        self.assertEqual(self.enqueue.call_count, 2)

    def test_inbound_enable_requires_integration_user_identity(self):
        self.account.rereply_inbound_enabled = 1
        self.account.rereply_webhook_secret = uuid4().hex + uuid4().hex
        self.account.rereply_integration_user_id = None
        with self.assertRaisesRegex(frappe.ValidationError, "integration user ID is required"):
            self.account.save(ignore_permissions=True)
        self.assertFalse(frappe.db.get_value("WhatsApp Account", self.account.name, "rereply_inbound_enabled"))

    def test_insert_stages_one_message_without_sending_or_committing(self):
        with patch.object(frappe.db, "commit", side_effect=AssertionError("A document hook must not commit")), \
                patch("frappe_whatsapp.utils.rereply_client.send_via_rereply") as send:
            message = self._message()
        self.assertEqual(message.rereply_send_state, "Queued")
        self.assertEqual(message.rereply_requested_by, "Administrator")
        self.assertEqual(json.loads(message.rereply_payload)["text"]["body"], message.message)
        send.assert_not_called()
        self.enqueue.assert_called_once()
        self.assertTrue(self.enqueue.call_args.kwargs["enqueue_after_commit"])
        self.assertEqual(self.enqueue.call_args.kwargs["message_name"], message.name)

    def test_rollback_removes_outbound_intent(self):
        savepoint = "rereply_" + self.suffix
        frappe.db.savepoint(savepoint)
        message = self._message()
        self.assertTrue(frappe.db.exists("WhatsApp Message", message.name))
        frappe.db.rollback(save_point=savepoint)
        self.assertFalse(frappe.db.exists("WhatsApp Message", message.name))

    def test_duplicate_worker_job_does_not_send_twice(self):
        message = self._message()
        accepted = self._accepted()
        with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply", return_value=accepted) as send:
            rereply_queue.send_queued_message(message.name)
            rereply_queue.send_queued_message(message.name)
        send.assert_called_once()
        message.reload()
        self.assertEqual(message.rereply_send_state, "Sent")
        self.assertEqual(message.rereply_message_id, accepted["rereply_message_id"])
        self.assertEqual(message.message_id, "rereply:" + accepted["rereply_message_id"])

    def test_ambiguous_delivery_is_held_on_repeated_job(self):
        message = self._message()
        with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply",
                   side_effect=ReReplyAmbiguousSendError()) as send:
            rereply_queue.send_queued_message(message.name)
            rereply_queue.send_queued_message(message.name)
        send.assert_called_once()
        message.reload()
        self.assertEqual(message.rereply_send_state, "Unknown")
        self.assertEqual(message.status, "Unknown")
        self.assertFalse(message.rereply_message_id)

    def test_disable_after_queue_prevents_delivery(self):
        message = self._message()
        frappe.db.set_value("WhatsApp Account", self.account.name, "rereply_outbound_enabled", 0)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- model a committed configuration change
        with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply") as send:
            rereply_queue.send_queued_message(message.name)
        send.assert_not_called()
        self.assertEqual(frappe.db.get_value("WhatsApp Message", message.name, "rereply_send_state"), "Queued")

    def test_changed_account_identity_never_reroutes_queued_message(self):
        message = self._message()
        frappe.db.set_value("WhatsApp Account", self.account.name, "rereply_account_id", str(uuid4()))
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- model a committed configuration change
        with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply") as send:
            rereply_queue.send_queued_message(message.name)
        send.assert_not_called()
        self.assertNotEqual(frappe.db.get_value("WhatsApp Message", message.name, "rereply_send_state"), "Sent")

    def test_provider_pending_or_failed_is_not_reported_sent(self):
        for status in ("pending", "failed"):
            with self.subTest(status=status):
                message = self._message()
                with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply",
                           return_value=self._accepted(status)):
                    rereply_queue.send_queued_message(message.name)
                message.reload()
                self.assertEqual(message.status, status.title())
                self.assertTrue(message.rereply_message_id)

    def test_bulk_retry_preserves_accepted_identity_and_unknown_outcome(self):
        bulk = frappe.get_doc({"doctype": "Bulk WhatsApp Message"})
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                message = self._message()
                outcome = {"side_effect": ReReplyAmbiguousSendError()} if uncertain else {
                    "return_value": self._accepted("failed")
                }
                with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply", **outcome):
                    rereply_queue.send_queued_message(message.name)
                message.reload()
                original_ids = (message.message_id, message.rereply_message_id)
                original_state = message.rereply_send_state
                with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply") as send:
                    self.assertFalse(bulk.resend_single_message(message.name))
                send.assert_not_called()
                message.reload()
                self.assertEqual((message.message_id, message.rereply_message_id), original_ids)
                self.assertEqual(message.rereply_send_state, original_state)

    def test_safe_rejected_message_can_be_retried_once(self):
        message = self._message()
        with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply",
                   side_effect=ReReplyError("Preparation rejected before send")):
            rereply_queue.send_queued_message(message.name)
        self.assertEqual(frappe.db.get_value("WhatsApp Message", message.name, "rereply_send_state"), "Failed")
        with patch("frappe_whatsapp.utils.rereply_client.send_via_rereply", return_value=self._accepted()) as send:
            self.assertTrue(rereply_queue.retry_rejected_message(message.name))
            self.assertFalse(rereply_queue.retry_rejected_message(message.name))
        send.assert_called_once()

    def _notification_fixture(self, **overrides):
        template_name = "rereply_notification_" + self.suffix
        template = frappe.get_doc({
            "doctype": "WhatsApp Templates", "name": template_name + "-en",
            "template_name": template_name, "actual_name": template_name,
            "template": "Hello {{1}}", "category": "UTILITY", "language_code": "en",
            "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
            "whatsapp_account": self.account.name, "status": "APPROVED",
        })
        template.db_insert()  # A provider-approved template fixture; never call Meta template creation.
        self.template_names.append(template.name)
        notification_data = {
            "doctype": "WhatsApp Notification", "notification_name": "Test " + self.suffix,
            "whatsapp_account": self.account.name, "template": template.name,
            "content_type": "text",
        }
        notification_data.update(overrides)
        notification = frappe.get_doc(notification_data)
        payload = {
            "messaging_product": "whatsapp", "to": self.phone, "type": "template",
            "template": {"name": template_name, "language": {"code": "en"},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": "Rendered value"}]}]},
        }
        return notification, payload

    def test_notification_uses_rendered_payload_and_creates_one_row(self):
        notification, payload = self._notification_fixture()
        with patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.WhatsAppMessage.send_template",
                   side_effect=AssertionError("Do not render/send a notification twice")), \
                patch.object(frappe.db, "commit", side_effect=AssertionError("Notification hooks must not commit")):
            message_name = notification.notify(payload)
        rows = frappe.get_all("WhatsApp Message", filters={"whatsapp_account": self.account.name}, pluck="name")
        self.assertEqual(rows, [message_name])
        self.assertEqual(json.loads(frappe.db.get_value("WhatsApp Message", message_name, "rereply_payload")), payload)
        self.enqueue.assert_called_once()

    def test_notification_flag_repeated_before_delivery_reuses_single_intent(self):
        notification, payload = self._notification_fixture(
            set_property_after_alert="allow_auto_read_receipt", property_value="1"
        )
        frappe.db.set_value("WhatsApp Account", self.account.name, "allow_auto_read_receipt", 0)
        self.account.reload()
        reference = self.account.as_dict()
        first_name = notification.notify(payload, reference)
        self.assertEqual(notification.notify(payload, reference), first_name)
        self.assertEqual(frappe.db.get_value("WhatsApp Account", self.account.name, "allow_auto_read_receipt"), 0)
        for state in ("Unknown", "Failed"):
            frappe.db.set_value("WhatsApp Message", first_name, {"rereply_send_state": state, "status": state})
            self.assertEqual(notification.notify(payload, reference), first_name)
        rows = frappe.get_all("WhatsApp Message", filters={"whatsapp_account": self.account.name}, pluck="name")
        self.assertEqual(rows, [first_name])
        self.assertTrue(frappe.db.get_value("WhatsApp Message", first_name, "rereply_notice_key"))
        self.enqueue.assert_called_once()

    def test_passive_outgoing_log_never_queues_delivery(self):
        message = frappe.get_doc({
            "doctype": "WhatsApp Message", "type": "Outgoing", "to": self.phone,
            "message": "Business App activity", "content_type": "text",
            "whatsapp_account": self.account.name, "rereply_source": "coexistence_app",
        })
        message.flags.rereply_passive_log = True
        message.insert(ignore_permissions=True)
        self.assertFalse(message.rereply_send_state)
        self.enqueue.assert_not_called()

    def test_private_file_uses_requester_permissions_not_worker_privileges(self):
        attachment = frappe.get_doc({
            "doctype": "File", "file_name": "rereply-private-" + self.suffix + ".txt",
            "is_private": 1, "content": b"Private integration test fixture",
        }).insert(ignore_permissions=True)
        self.file_names.append(attachment.name)
        self.assertEqual(frappe.session.user, "Administrator")
        with self.assertRaises(PermissionError):
            rereply_queue.load_erp_media(attachment.file_url, requested_by="Guest")
        filename, content, _ = rereply_queue.load_erp_media(attachment.file_url, requested_by="Administrator")
        self.assertEqual(filename, attachment.file_name)
        self.assertEqual(content, b"Private integration test fixture")

    def test_status_polling_advances_past_first_page(self):
        expected_ids = set()
        for _ in range(51):
            message = self._message()
            message_id = str(uuid4())
            expected_ids.add(message_id)
            frappe.db.set_value("WhatsApp Message", message.name, {
                "rereply_send_state": "Sent", "status": "Sent",
                "rereply_message_id": message_id, "rereply_contact_id": str(uuid4()),
            })
        checked = set()

        def status_lookup(account, contact_id, message_id):
            checked.add(message_id)
            return {"status": "sent", "wamid": ""}

        with patch("frappe_whatsapp.utils.rereply_client.get_message_status", side_effect=status_lookup):
            rereply_queue.reconcile_statuses([self.account.name])
            self.assertEqual(len(checked), 50)
            rereply_queue.reconcile_statuses([self.account.name])
        self.assertEqual(checked, expected_ids)
