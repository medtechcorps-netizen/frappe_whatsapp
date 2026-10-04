"""Template lifecycle tests using real Frappe hooks and a read-only HTTP stub.

Run on a disposable bench site with ``run-tests --module
frappe_whatsapp.utils.test_rereply_templates_integration``. No provider request
escapes this module; the real ReReply client still verifies the account and
template identities against the stubbed HTTP contract.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe

from frappe_whatsapp.testing import IntegrationTestCase
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates import whatsapp_templates


class TestReReplyTemplatesIntegration(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.original_user = frappe.session.user
        frappe.set_user("Administrator")
        self.suffix = uuid4().hex[:12]
        self.account_names = []
        self.provider_calls = []
        self.list_rows = None
        self.account_override = {}
        self._patches = [
            patch("requests.sessions.Session.request", side_effect=AssertionError("No live HTTP in template tests")),
            patch.object(whatsapp_templates, "make_post_request", side_effect=AssertionError("ReReply template must not write to Meta")),
            patch.object(whatsapp_templates, "make_request", side_effect=AssertionError("ReReply template must not call Meta")),
            patch("requests.request", side_effect=self._provider_request),
        ]
        self.mocks = [patcher.start() for patcher in self._patches]
        self.addCleanup(self._cleanup)
        self.account = self._make_account("ReReply")
        self.template_name = "rereply_template_" + self.suffix
        self.language_name = frappe.db.get_value("Language", {"language_code": "en"}) or "en"
        self.provider_template = {
            "id": str(uuid4()), "meta_template_id": "1234567890123456",
            "name": self.template_name, "display_name": "Order notice",
            "language": "en", "whatsapp_account": self.account.rereply_account_name,
            "category": "UTILITY", "status": "APPROVED", "header_type": "NONE",
            "header_content": "", "body_content": "Hello {{1}}, your order {{2}} is confirmed.",
            "footer_content": "", "buttons": [],
            "sample_values": [
                {"component": "body", "index": 1, "value": "Customer"},
                {"component": "body", "index": 2, "value": "ORDER-TEST"},
            ],
        }

    def _cleanup(self):
        if getattr(self, "_cleaned_up", False):
            return
        self._cleaned_up = True
        try:
            frappe.set_user("Administrator")
            for account_name in self.account_names:
                for template_name in frappe.get_all("WhatsApp Templates", filters={"whatsapp_account": account_name}, pluck="name"):
                    frappe.db.delete("WhatsApp Button", {"parenttype": "WhatsApp Templates", "parent": template_name})
                    frappe.db.delete("WhatsApp Templates", {"name": template_name})
                if frappe.db.exists("WhatsApp Account", account_name):
                    frappe.delete_doc("WhatsApp Account", account_name, ignore_permissions=True, force=True)
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove only this test's uniquely scoped fixtures
        finally:
            for patcher in reversed(self._patches):
                patcher.stop()
            frappe.set_user(self.original_user)

    def _make_account(self, provider):
        account_name = "Template " + provider + " " + self.suffix
        self.account_names.append(account_name)
        account = frappe.get_doc({
            "doctype": "WhatsApp Account", "account_name": account_name,
            "status": "Active", "transport_provider": provider,
            "url": "https://graph.facebook.com", "version": "v24.0",
            "phone_id": str(uuid4().int % 10**15), "business_id": str(uuid4().int % 10**15),
            "app_id": "123456789", "webhook_verify_token": provider + "-" + self.suffix,
            "is_default_incoming": 0, "is_default_outgoing": 0,
            "rereply_base_url": "https://app.rereply.app",
            "rereply_workspace_id": str(uuid4()), "rereply_account_id": str(uuid4()),
            "rereply_account_name": "Template provider " + self.suffix,
            "rereply_outbound_enabled": 0, "rereply_inbound_enabled": 0,
        }).insert(ignore_permissions=True)
        account.rereply_api_key = "template-test-key-not-a-real-credential"
        account.token = "meta-test-token-not-a-real-credential"
        account.save(ignore_permissions=True)
        return frappe.get_doc("WhatsApp Account", account.name)

    def _provider_request(self, method, url, **kwargs):
        origin = "https://app.rereply.app"
        self.assertEqual(method, "GET", "Template mirroring must be read-only")
        self.assertTrue(url.startswith(origin + "/api/"))
        self.assertEqual(kwargs["headers"]["X-Organization-ID"], self.account.rereply_workspace_id)
        self.assertEqual(kwargs["headers"]["X-API-Key"], "template-test-key-not-a-real-credential")
        self.assertFalse(kwargs["allow_redirects"])
        path = url[len(origin):]
        self.provider_calls.append((method, path, deepcopy(kwargs.get("params", {}))))
        if path == "/api/accounts/" + self.account.rereply_account_id:
            data = {
                "id": self.account.rereply_account_id, "name": self.account.rereply_account_name,
                "status": "active", "phone_id": self.account.phone_id, "business_id": self.account.business_id,
            }
            data.update(self.account_override)
        elif path == "/api/templates":
            self.assertEqual(kwargs["params"]["account"], self.account.rereply_account_name)
            self.assertEqual(kwargs["params"]["search"], self.template_name)
            rows = [self.provider_template] if self.list_rows is None else self.list_rows
            data = {"templates": rows, "total": len(rows), "page": 1, "limit": 100}
        elif path == "/api/templates/" + self.provider_template["id"]:
            data = self.provider_template
        else:
            raise AssertionError("Unexpected template integration endpoint: " + path)
        return SimpleNamespace(status_code=200, json=lambda: {"status": "success", "data": deepcopy(data)})

    def _template(self, account=None, **overrides):
        values = {
            "doctype": "WhatsApp Templates", "template_name": self.template_name,
            "actual_name": self.template_name,
            "template": "Hello {{1}}, your order {{2}} is confirmed.",
            "sample_values": "Customer,ORDER-TEST", "field_names": "customer_name,name",
            "category": "UTILITY", "language": self.language_name, "language_code": "en",
            "whatsapp_account": (account or self.account).name,
            "header_type": "", "header": "", "footer": "",
        }
        values.update(overrides)
        return frappe.get_doc(values)

    def test_matching_create_imports_verified_identity_without_meta_write(self):
        document = self._template(status="PENDING", id="old-waba-template-id")
        document.insert(ignore_permissions=True)
        document.reload()
        self.assertEqual(document.id, self.provider_template["meta_template_id"])
        self.assertEqual(document.status, "APPROVED")
        self.assertEqual(document.actual_name, self.template_name)
        self.assertEqual(document.template, self.provider_template["body_content"])
        self.assertEqual(document.field_names, "customer_name,name")
        self.assertEqual(document.sample_values, "Customer,ORDER-TEST")
        self.assertTrue(any(path == "/api/accounts/" + self.account.rereply_account_id for _, path, _ in self.provider_calls))
        self.assertTrue(any(path == "/api/templates/" + self.provider_template["id"] for _, path, _ in self.provider_calls))
        self.mocks[1].assert_not_called()
        self.mocks[2].assert_not_called()

    def test_unverified_local_body_edit_is_rejected_and_original_stays_saved(self):
        document = self._template().insert(ignore_permissions=True)
        document.template = "Local edit that has not been approved by the provider"
        with self.assertRaises(frappe.ValidationError):
            document.save(ignore_permissions=True)
        self.assertEqual(frappe.db.get_value("WhatsApp Templates", document.name, "template"), self.provider_template["body_content"])
        self.mocks[1].assert_not_called()

    def test_provider_status_and_id_replace_untrusted_local_approval(self):
        document = self._template().insert(ignore_permissions=True)
        self.provider_template["status"] = "REJECTED"
        self.provider_template["meta_template_id"] = "9876543210987654"
        document.status = "APPROVED"
        document.save(ignore_permissions=True)
        document.reload()
        self.assertEqual(document.status, "REJECTED")
        self.assertEqual(document.id, "9876543210987654")
        self.mocks[1].assert_not_called()

    def test_provider_draft_clears_old_waba_approval_and_identity(self):
        self.provider_template["status"] = "DRAFT"
        self.provider_template["meta_template_id"] = ""
        document = self._template(status="APPROVED", id="old-waba-approved-id").insert(ignore_permissions=True)
        document.reload()
        self.assertEqual(document.status, "DRAFT")
        self.assertFalse(document.id)

    def test_missing_or_ambiguous_provider_template_cannot_create_mirror(self):
        for rows in ([], [deepcopy(self.provider_template), deepcopy(self.provider_template)]):
            with self.subTest(matches=len(rows)):
                self.list_rows = rows
                with self.assertRaises(frappe.ValidationError):
                    self._template().insert(ignore_permissions=True)
                self.assertFalse(frappe.db.exists("WhatsApp Templates", {"template_name": self.template_name}))
        self.mocks[1].assert_not_called()

    def test_other_language_or_account_does_not_satisfy_mirror_lookup(self):
        for field, value in (("language", "ms"), ("whatsapp_account", "Another provider account")):
            with self.subTest(field=field):
                row = deepcopy(self.provider_template)
                row[field] = value
                self.list_rows = [row]
                with self.assertRaises(frappe.ValidationError):
                    self._template().insert(ignore_permissions=True)
                self.assertFalse(frappe.db.exists("WhatsApp Templates", {"template_name": self.template_name}))

    def test_detail_account_must_still_match_after_correct_list_result(self):
        self.list_rows = [deepcopy(self.provider_template)]
        self.provider_template["whatsapp_account"] = "Different account in detail response"
        with self.assertRaises(frappe.ValidationError):
            self._template().insert(ignore_permissions=True)
        self.assertTrue(any(path == "/api/templates/" + self.provider_template["id"] for _, path, _ in self.provider_calls))
        self.assertFalse(frappe.db.exists("WhatsApp Templates", {"template_name": self.template_name}))

    def test_current_phone_or_waba_mismatch_blocks_template_import(self):
        for field in ("phone_id", "business_id"):
            with self.subTest(field=field):
                self.account_override = {field: "999999999999999999"}
                self.provider_calls.clear()
                with self.assertRaises(frappe.ValidationError):
                    self._template().insert(ignore_permissions=True)
                self.assertFalse(any(path.startswith("/api/templates") for _, path, _ in self.provider_calls))

    def test_media_header_mirror_does_not_upload_or_read_sample_attachment(self):
        self.provider_template["header_type"] = "DOCUMENT"
        document = self._template(header_type="DOCUMENT", sample="/private/files/not-a-real-sample.pdf")
        with patch.object(type(document), "_read_local_file", side_effect=AssertionError("A mirror must not read media")) as read:
            document.insert(ignore_permissions=True)
        document.reload()
        self.assertEqual(document.header_type, "DOCUMENT")
        self.assertEqual(document.id, self.provider_template["meta_template_id"])
        read.assert_not_called()
        self.mocks[1].assert_not_called()

    def test_matching_dynamic_url_button_is_saved_as_local_child(self):
        url = "https://example.com/order/{{1}}"
        self.provider_template["buttons"] = [{"type": "URL", "text": "View order", "url": url}]
        document = self._template()
        document.append("buttons", {
            "button_type": "Visit Website", "button_label": "View order",
            "website_url": url, "url_type": "Dynamic", "example_url": "https://example.com/order/TEST",
        })
        document.insert(ignore_permissions=True)
        document.reload()
        self.assertEqual(len(document.buttons), 1)
        self.assertEqual(document.buttons[0].website_url, url)
        self.assertEqual(document.buttons[0].url_type, "Dynamic")
        self.assertEqual(document.status, "APPROVED")

    def test_direct_meta_settings_and_upload_entrypoints_are_blocked(self):
        document = self._template()
        for method, args in (("get_settings", ()), ("get_session_id", ("/private/files/no-file.pdf",)),
                             ("get_media_id", ("/private/files/no-file.pdf",))):
            with self.subTest(method=method):
                with self.assertRaises(frappe.ValidationError):
                    getattr(document, method)(*args)
        self.assertFalse(self.provider_calls)
        self.mocks[1].assert_not_called()
        self.mocks[2].assert_not_called()

    def test_direct_update_template_uses_verified_read_only_provider_lookup(self):
        document = self._template().insert(ignore_permissions=True)
        self.provider_calls.clear()
        document.update_template()
        self.assertTrue(self.provider_calls)
        self.assertTrue(all(method == "GET" for method, _, _ in self.provider_calls))
        self.mocks[1].assert_not_called()

    def test_deleting_local_mirror_does_not_delete_provider_template(self):
        document = self._template().insert(ignore_permissions=True)
        self.provider_calls.clear()
        frappe.delete_doc("WhatsApp Templates", document.name, ignore_permissions=True)
        self.assertFalse(frappe.db.exists("WhatsApp Templates", document.name))
        self.assertFalse(self.provider_calls)
        self.mocks[1].assert_not_called()
        self.mocks[2].assert_not_called()

    def test_targeted_fetch_refreshes_existing_mirror_without_touching_meta_account(self):
        document = self._template().insert(ignore_permissions=True)
        stale = self._template(template_name=self.template_name + "_stale", actual_name=self.template_name + "_stale",
                               status="APPROVED", id="old-stale-template-id")
        stale.db_insert()  # Existing old-WABA mirror intentionally absent from the current provider.
        self._make_account("Meta")
        self.provider_template["status"] = "REJECTED"
        self.provider_calls.clear()
        whatsapp_templates.fetch(account_name=self.account.name, template_name=document.name)
        document.reload()
        stale.reload()
        self.assertEqual(document.status, "REJECTED")
        self.assertEqual(stale.status, "APPROVED")
        self.assertEqual(stale.id, "old-stale-template-id")
        self.assertTrue(self.provider_calls)
        self.assertTrue(all(method == "GET" for method, _, _ in self.provider_calls))
        self.mocks[1].assert_not_called()
        self.mocks[2].assert_not_called()

    def test_legacy_fetch_cannot_reassign_same_named_rereply_mirror(self):
        document = self._template().insert(ignore_permissions=True)
        legacy_account = self._make_account("Meta")
        response = {"data": [{
            "name": self.template_name, "language": "en", "category": "UTILITY",
            "id": "legacy-conflicting-template-id", "status": "APPROVED", "components": [],
        }]}
        with patch.object(whatsapp_templates, "make_request", return_value=response) as meta:
            with self.assertRaises(frappe.ValidationError):
                whatsapp_templates.fetch(account_name=legacy_account.name)
        meta.assert_called_once()
        document.reload()
        self.assertEqual(document.whatsapp_account, self.account.name)
        self.assertEqual(document.id, self.provider_template["meta_template_id"])

    def test_legacy_meta_template_create_still_uses_existing_transport(self):
        account = self._make_account("Meta")
        document = self._template(account=account, template_name=self.template_name + "_meta",
                                  actual_name=self.template_name + "_meta")
        with patch.object(whatsapp_templates, "make_post_request", return_value={"id": "legacy-meta-id", "status": "PENDING"}) as meta:
            document.insert(ignore_permissions=True)
        document.reload()
        meta.assert_called_once()
        self.assertIn("/message_templates", meta.call_args.args[0])
        self.assertEqual(document.id, "legacy-meta-id")
        self.assertEqual(document.status, "PENDING")
        self.assertFalse(self.provider_calls)
