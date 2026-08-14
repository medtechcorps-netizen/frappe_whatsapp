"""Webhook."""
import frappe
import json
import requests
import time
from frappe import _
from werkzeug.wrappers import Response
import frappe.utils

from frappe_whatsapp.utils import get_whatsapp_account
from frappe_whatsapp.utils.webhook_security import verify_meta_signature


MAX_META_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024


@frappe.whitelist(allow_guest=True)
def webhook():
	"""Meta webhook."""
	if frappe.request.method == "GET":
		return get()

	raw_body = _get_raw_webhook_body()
	data = _decode_webhook_body(raw_body)
	_authenticate_meta_webhook(data, raw_body)
	return post(data)


def get():
	"""Get."""
	hub_challenge = frappe.form_dict.get("hub.challenge")
	verify_token = frappe.form_dict.get("hub.verify_token")
	webhook_verify_token = frappe.db.get_value(
		'WhatsApp Account',
		{"webhook_verify_token": verify_token},
		'webhook_verify_token'
	)
	if not webhook_verify_token:
		frappe.throw("No matching WhatsApp account")

	if frappe.form_dict.get("hub.verify_token") != webhook_verify_token:
		frappe.throw("Verify token does not match")

	return Response(hub_challenge, status=200)

def _get_raw_webhook_body():
	"""Return exact request bytes used by Meta to calculate its signature."""
	raw_body = frappe.request.get_data(cache=True, as_text=False)
	if isinstance(raw_body, str):
		raw_body = raw_body.encode("utf-8")
	if not isinstance(raw_body, bytes) or not raw_body:
		_reject_webhook()
	if len(raw_body) > MAX_META_WEBHOOK_BODY_BYTES:
		_reject_webhook()
	return raw_body


def _decode_webhook_body(raw_body):
	"""Parse an authenticated candidate without re-serializing its bytes."""
	try:
		data = json.loads(raw_body.decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError):
		_reject_webhook()

	if not isinstance(data, dict):
		_reject_webhook()
	return data


def _candidate_account_names(data):
	"""Resolve only accounts referenced by the untrusted routing metadata."""
	entries = data.get("entry", [])
	if isinstance(entries, dict):
		entries = [entries]
	if not isinstance(entries, list):
		return []

	phone_ids = set()
	business_ids = set()
	for entry in entries[:100]:
		if not isinstance(entry, dict):
			continue
		entry_id = entry.get("id")
		if isinstance(entry_id, str) and entry_id:
			business_ids.add(entry_id[:255])

		changes = entry.get("changes", [])
		if isinstance(changes, dict):
			changes = [changes]
		if not isinstance(changes, list):
			continue
		for change in changes[:100]:
			if not isinstance(change, dict):
				continue
			value = change.get("value", {})
			if not isinstance(value, dict):
				continue
			metadata = value.get("metadata", {})
			if not isinstance(metadata, dict):
				continue
			phone_id = metadata.get("phone_number_id")
			if isinstance(phone_id, str) and phone_id:
				phone_ids.add(phone_id[:255])

	account_names = []
	for phone_id in phone_ids:
		name = frappe.db.get_value("WhatsApp Account", {"phone_id": phone_id}, "name")
		if name and name not in account_names:
			account_names.append(name)

	# Some valid events (for example template status changes) have no phone ID.
	# Their entry ID is the subscribed WhatsApp Business Account ID.
	if not account_names:
		for business_id in business_ids:
			for name in frappe.get_all(
				"WhatsApp Account",
				filters={"business_id": business_id},
				pluck="name",
				limit_page_length=100,
			):
				if name not in account_names:
					account_names.append(name)

	return account_names


def _authenticate_meta_webhook(data, raw_body):
	"""Fail closed before logging or processing an unsigned Meta request."""
	signature = frappe.request.headers.get("X-Hub-Signature-256", "")
	for account_name in _candidate_account_names(data):
		account = frappe.get_doc("WhatsApp Account", account_name)
		app_secret = account.get_password("app_secret", raise_exception=False)
		if verify_meta_signature(app_secret, raw_body, signature):
			return account

	_reject_webhook()


def _reject_webhook():
	"""Return one indistinguishable error for missing account, secret, or signature."""
	frappe.throw(_("Webhook authentication failed"), frappe.AuthenticationError)


def _non_empty_text(value):
	if isinstance(value, str):
		value = value.strip()
		return value or None
	return None


class _MessageEnvelopeError(ValueError):
	pass


def _message_sender_id(message):
	"""Return Meta's phone-number ID or its username-era business-scoped ID."""
	if not isinstance(message, dict):
		raise _MessageEnvelopeError("WhatsApp message must be an object")

	for fieldname in ("from", "from_user_id"):
		sender_id = _non_empty_text(message.get(fieldname))
		if sender_id:
			return sender_id

	raise _MessageEnvelopeError("WhatsApp message has no sender identifier")


def _webhook_contacts(data):
	contacts = []
	entries = data.get("entry", [])
	if isinstance(entries, dict):
		entries = [entries]
	if not isinstance(entries, list):
		return contacts

	for entry in entries:
		if not isinstance(entry, dict):
			continue
		changes = entry.get("changes", [])
		if isinstance(changes, dict):
			changes = [changes]
		if not isinstance(changes, list):
			continue
		for change in changes:
			if not isinstance(change, dict):
				continue
			value = change.get("value", {})
			if not isinstance(value, dict):
				continue
			change_contacts = value.get("contacts", [])
			if isinstance(change_contacts, list):
				contacts.extend(
					contact for contact in change_contacts if isinstance(contact, dict)
				)
	return contacts


def _sender_profile_name(contacts, sender_id):
	"""Match profile identity by either legacy wa_id or the newer user_id."""
	fallback = None
	for contact in contacts:
		profile = contact.get("profile", {})
		if not isinstance(profile, dict):
			profile = {}
		display_name = (
			_non_empty_text(profile.get("name"))
			or _non_empty_text(contact.get("username"))
		)
		if fallback is None:
			fallback = display_name

		contact_ids = {
			_non_empty_text(contact.get("wa_id")),
			_non_empty_text(contact.get("user_id")),
		}
		if sender_id in contact_ids:
			return display_name

	return fallback


def _log_message_processing_error(message):
	message_id = message.get("id") if isinstance(message, dict) else None
	message_type = message.get("type") if isinstance(message, dict) else None
	frappe.log_error(
		title="WhatsApp webhook message processing failed",
		message=(
			f"Message ID: {message_id or '<missing>'}\n"
			f"Message type: {message_type or '<missing>'}\n\n"
			f"{frappe.get_traceback()}"
		),
	)


def _process_incoming_message(message, whatsapp_account, contacts, sender_id):
	sender_profile_name = _sender_profile_name(contacts, sender_id)
	message_type = message['type']
	is_reply = True if message.get('context') and 'forwarded' not in message.get('context') else False
	reply_to_message_id = message['context']['id'] if is_reply else None
	if message_type == 'text':
		frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": sender_id,
			"message": message['text']['body'],
			"message_id": message['id'],
			"reply_to_message_id": reply_to_message_id,
			"is_reply": is_reply,
			"content_type":message_type,
			"profile_name":sender_profile_name,
			"whatsapp_account":whatsapp_account.name
		}).insert(ignore_permissions=True)
	elif message_type == 'reaction':
		frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": sender_id,
			"message": message['reaction']['emoji'],
			"reply_to_message_id": message['reaction']['message_id'],
			"message_id": message['id'],
			"content_type": "reaction",
			"profile_name":sender_profile_name,
			"whatsapp_account":whatsapp_account.name
		}).insert(ignore_permissions=True)
	elif message_type == 'interactive':
		interactive_data = message['interactive']
		interactive_type = interactive_data.get('type')

		# Handle button reply
		if interactive_type == 'button_reply':
			frappe.get_doc({
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"from": sender_id,
				"message": interactive_data['button_reply']['id'],
				"message_id": message['id'],
				"reply_to_message_id": reply_to_message_id,
				"is_reply": is_reply,
				"content_type": "button",
				"profile_name": sender_profile_name,
				"whatsapp_account": whatsapp_account.name
			}).insert(ignore_permissions=True)
		# Handle list reply
		elif interactive_type == 'list_reply':
			frappe.get_doc({
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"from": sender_id,
				"message": interactive_data['list_reply']['id'],
				"message_id": message['id'],
				"reply_to_message_id": reply_to_message_id,
				"is_reply": is_reply,
				"content_type": "button",
				"profile_name": sender_profile_name,
				"whatsapp_account": whatsapp_account.name
			}).insert(ignore_permissions=True)
		# Handle WhatsApp Flows (nfm_reply)
		elif interactive_type == 'nfm_reply':
			nfm_reply = interactive_data['nfm_reply']
			response_json_str = nfm_reply.get('response_json', '{}')

			# Parse the response JSON
			try:
				flow_response = json.loads(response_json_str)
			except json.JSONDecodeError:
				flow_response = {}

			# Create a summary message from the flow response
			summary_parts = []
			for key, value in flow_response.items():
				if value:
					summary_parts.append(f"{key}: {value}")
			summary_message = ", ".join(summary_parts) if summary_parts else "Flow completed"

			frappe.get_doc({
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"from": sender_id,
				"message": summary_message,
				"message_id": message['id'],
				"reply_to_message_id": reply_to_message_id,
				"is_reply": is_reply,
				"content_type": "flow",
				"flow_response": json.dumps(flow_response),
				"profile_name": sender_profile_name,
				"whatsapp_account": whatsapp_account.name
			}).insert(ignore_permissions=True)

			# Publish realtime event for flow response
			frappe.publish_realtime(  # nosemgrep: frappe-realtime-pick-room -- intentional site-wide fan-out for chat UIs (whatsapp_chat companion app) listening for inbound flow responses
				"whatsapp_flow_response",
				{
					"phone": sender_id,
					"message_id": message['id'],
					"flow_response": flow_response,
					"whatsapp_account": whatsapp_account.name
				}
			)
	# NEW: Handle Shopping Cart / Orders from MPM
	elif message_type == 'order':
		order_data = message['order']

		# Inject the raw data into product_catalog_json
		frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": sender_id,
			"message": _("New Order Received via WhatsApp"),
			"message_id": message['id'],
			"content_type": "order",
			"profile_name": sender_profile_name,
			"whatsapp_account": whatsapp_account.name,
			"product_catalog_json": json.dumps(order_data)
		}).insert(ignore_permissions=True)
	elif message_type in ["image", "audio", "video", "document"]:
		token = whatsapp_account.get_password("token")
		url = f"{whatsapp_account.url}/{whatsapp_account.version}/"

		media_id = message[message_type]["id"]
		headers = {
			'Authorization': 'Bearer ' + token

		}
		response = requests.get(f'{url}{media_id}/', headers=headers)

		if response.status_code == 200:
			media_data = response.json()
			media_url = media_data.get("url")
			mime_type = media_data.get("mime_type")
			file_extension = mime_type.split('/')[1]

			media_response = requests.get(media_url, headers=headers)
			if media_response.status_code == 200:

				file_data = media_response.content
				file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

				message_doc = frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": sender_id,
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"message": message[message_type].get("caption", ""),
					"content_type" : message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)

				file = frappe.get_doc(
					{
						"doctype": "File",
						"file_name": file_name,
						"attached_to_doctype": "WhatsApp Message",
						"attached_to_name": message_doc.name,
						"content": file_data,
						"attached_to_field": "attach"
					}
				).save(ignore_permissions=True)


				message_doc.attach = file.file_url
				message_doc.save()
	elif message_type == "button":
		frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": sender_id,
			"message": message['button']['text'],
			"message_id": message['id'],
			"reply_to_message_id": reply_to_message_id,
			"is_reply": is_reply,
			"content_type": message_type,
			"profile_name":sender_profile_name,
			"whatsapp_account":whatsapp_account.name
		}).insert(ignore_permissions=True)
	else:
		frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": sender_id,
			"message_id": message['id'],
			"message": message[message_type].get(message_type),
			"content_type" : message_type,
			"profile_name":sender_profile_name,
			"whatsapp_account":whatsapp_account.name
		}).insert(ignore_permissions=True)


def post(data):
	"""Post."""
	frappe.get_doc({
		"doctype": "WhatsApp Notification Log",
		"template": "Webhook",
		"meta_data": json.dumps(data)
	}).insert(ignore_permissions=True)

	messages = []
	phone_id = None
	try:
		messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
		phone_id = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("metadata", {}).get("phone_number_id")
	except KeyError:
		messages = data["entry"]["changes"][0]["value"].get("messages", [])

	whatsapp_account = get_whatsapp_account(phone_id) if phone_id else None

	# Only `messages` events carry `metadata.phone_number_id`. Status-change
	# events (`message_template_status_update`, message status callbacks) have
	# no metadata, so `phone_id` is None and `whatsapp_account` is also None
	# for them by design. Gating the entire handler on `whatsapp_account`
	# silently drops every template-status update; gate only the message-
	# ingestion branch instead.
	if messages and not whatsapp_account:
		return

	if messages:
		contacts = _webhook_contacts(data)
		for message in messages:
			try:
				sender_id = _message_sender_id(message)
			except _MessageEnvelopeError:
				_log_message_processing_error(message)
				continue
			_process_incoming_message(message, whatsapp_account, contacts, sender_id)
	else:
		changes = None
		try:
			changes = data["entry"][0]["changes"][0]
		except KeyError:
			changes = data["entry"]["changes"][0]
		update_status(changes)
	return

def update_status(data):
	"""Update status hook."""
	if data.get("field") == "message_template_status_update":
		update_template_status(data['value'])

	elif data.get("field") == "messages":
		update_message_status(data['value'])

def update_template_status(data):
	"""Update template status."""
	frappe.db.sql(
		"""UPDATE `tabWhatsApp Templates`
		SET status = %(event)s
		WHERE id = %(message_template_id)s""",
		data
	)

def update_message_status(data):
	"""Update message status."""
	id = data['statuses'][0]['id']
	status = data['statuses'][0]['status']
	conversation = data['statuses'][0].get('conversation', {}).get('id')
	name = frappe.db.get_value("WhatsApp Message", filters={"message_id": id})

	doc = frappe.get_doc("WhatsApp Message", name)
	doc.status = status
	if conversation:
		doc.conversation_id = conversation
	doc.save(ignore_permissions=True)
