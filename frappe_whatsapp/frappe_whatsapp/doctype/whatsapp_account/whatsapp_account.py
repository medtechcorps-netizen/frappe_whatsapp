# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.integrations.utils import make_post_request
from frappe.model.document import Document


class WhatsAppAccount(Document):
	def validate(self):
		if self.get("transport_provider") != "ReReply":
			return
		from urllib.parse import urlsplit
		from uuid import UUID
		import hmac
		base = urlsplit(self.get("rereply_base_url") or "")
		if base.scheme != "https" or not base.hostname or base.username or base.password or base.query or base.fragment or base.path not in ("", "/"):
			frappe.throw(_("ReReply URL must be an HTTPS origin without credentials, path, or query."))
		for field in ("rereply_workspace_id", "rereply_account_id"):
			try:
				UUID(self.get(field) or "")
			except ValueError:
				frappe.throw(_("A valid ReReply workspace ID and WhatsApp account ID are required."))
		if not self.get("rereply_account_name"):
			frappe.throw(_("The exact ReReply WhatsApp account name is required."))
		if self.get("rereply_integration_user_id"):
			try:
				UUID(self.rereply_integration_user_id)
			except ValueError:
				frappe.throw(_("The ReReply integration user ID must be a UUID."))
		if self.get("rereply_inbound_enabled") and not self.get("rereply_outbound_enabled"):
			frappe.throw(_("Enable and validate ReReply sending before enabling incoming ERP automation."))
		if self.get("rereply_outbound_enabled") and not self.get_password("rereply_api_key", raise_exception=False):
			frappe.throw(_("A ReReply API key is required before sending is enabled."))
		if self.get("rereply_inbound_enabled") and not self.get_password("rereply_webhook_secret", raise_exception=False):
			frappe.throw(_("A dedicated ReReply webhook signing secret is required."))
		duplicates = frappe.get_all("WhatsApp Account", filters={
			"transport_provider": "ReReply", "rereply_workspace_id": self.rereply_workspace_id,
			"rereply_account_id": self.rereply_account_id, "name": ["!=", self.name or ""],
		}, limit_page_length=1)
		if duplicates:
			frappe.throw(_("This ReReply WhatsApp account is already connected to another ERP account."))
		secret = self.get_password("rereply_webhook_secret", raise_exception=False)
		if secret:
			if len(secret) < 32:
				frappe.throw(_("Use a dedicated webhook secret of at least 32 characters."))
			for other in frappe.get_all("WhatsApp Account", filters={
				"transport_provider": "ReReply", "name": ["!=", self.name or ""],
			}, pluck="name"):
				other_secret = frappe.get_doc("WhatsApp Account", other).get_password("rereply_webhook_secret", raise_exception=False)
				if other_secret and hmac.compare_digest(secret, other_secret):
					frappe.throw(_("Each ERP ReReply account must use its own webhook signing secret."))
	def on_update(self):
		"""Check there is only one default of each type."""
		self.there_must_be_only_one_default()

	def there_must_be_only_one_default(self):
		"""If current WhatsApp Account is default, un-default all other accounts."""
		for field in ("is_default_incoming", "is_default_outgoing"):
			if not self.get(field):
				continue

			for whatsapp_account in frappe.get_all("WhatsApp Account", filters={field: 1}):
				if whatsapp_account.name == self.name:
					continue

				whatsapp_account = frappe.get_doc("WhatsApp Account", whatsapp_account.name)
				whatsapp_account.set(field, 0)
				whatsapp_account.save()

	@frappe.whitelist()
	def subscribe_app(self):
		"""Subscribe this app to webhooks for the WhatsApp Business Account.

		Required after phone number registration to receive incoming messages.
		Calls POST /{version}/{business_id}/subscribed_apps on the Graph API.
		"""
		if self.get("transport_provider") == "ReReply":
			frappe.throw(_("Manage Meta webhook subscriptions in ReReply for this account."))
		for field in ("url", "version", "business_id"):
			if not self.get(field):
				frappe.throw(_("{0} is required to subscribe the app").format(
					frappe.bold(self.meta.get_label(field))
				))

		token = self.get_password("token")
		if not token:
			frappe.throw(_("Access token is required to subscribe the app"))

		endpoint = f"{self.url}/{self.version}/{self.business_id}/subscribed_apps"
		headers = {
			"authorization": f"Bearer {token}",
			"content-type": "application/json",
		}

		try:
			response = make_post_request(endpoint, headers=headers)
		except Exception as e:
			error_message = str(e)
			if frappe.flags.integration_request:
				err = frappe.flags.integration_request.json().get("error", {})
				if err:
					error_message = err.get("message") or err.get("Error") or error_message
			frappe.throw(_("Failed to subscribe app to webhooks: {0}").format(error_message))

		if not response.get("success"):
			frappe.throw(_("Subscription was not successful: {0}").format(frappe.as_json(response)))

		frappe.logger().info(
			f"WhatsApp app subscribed to webhooks for business_id={self.business_id}"
		)
		return response
