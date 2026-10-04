"""Create whatsapp template."""

# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt
import json
import frappe
import magic
import requests
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request, make_request
from frappe.desk.form.utils import get_pdf_link

from frappe_whatsapp.utils import get_whatsapp_account

class WhatsAppTemplates(Document):  # nosemgrep: frappe-modifying-but-not-committing-other-method -- get_settings() sets self._token/_url/_version/_business_id/_app_id/_headers as in-memory scratch for the outbound Meta HTTP call; they are not DocType fields and must not be persisted
    """Create whatsapp template."""

    def validate(self):
        self.set_whatsapp_account()
        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        if account.get("transport_provider") == "ReReply":
            # ReReply owns the provider template. Saving here only verifies and
            # mirrors it; do this before any legacy media upload or Meta update.
            self.sync_from_rereply(account)
            return
        if not self.language_code or self.has_value_changed("language"):
            lang_code = frappe.db.get_value("Language", self.language) or "en"
            self.language_code = lang_code.replace("-", "_")

        if self.header_type in ["IMAGE", "DOCUMENT"] and self.sample:
            self.get_session_id(self.sample)
            self.get_media_id(self.sample)

        if not self.is_new():
            self.update_template()

    def sync_from_rereply(self, account=None):
        """Mirror a verified ReReply template without publishing local edits."""
        from frappe_whatsapp.utils.rereply_client import ReReplyError, get_template_details

        account = account or frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        if account.get("transport_provider") != "ReReply" or account.get("status") != "Active":
            frappe.throw("An active ReReply WhatsApp Account is required to mirror this template.")
        language = frappe.db.get_value("Language", self.language, "language_code") or self.language
        language = (language or "").replace("-", "_")
        name = self.actual_name or (self.template_name or "").lower().replace(" ", "_")
        try:
            remote = get_template_details(account, name, language)
            fields = _rereply_template_fields(remote)
        except ReReplyError as error:
            frappe.throw(str(error))
        if self.template != fields["template"]:
            frappe.throw("ERP template body must exactly match ReReply. Edit and publish the template in ReReply first.")
        local_buttons = [_template_button_signature(button) for button in self.get("buttons") or []]
        remote_buttons = [_template_button_signature(button) for button in fields["buttons"]]
        if (
            (self.header_type or "") != fields["header_type"]
            or (self.header or "") != fields["header"]
            or (self.footer or "") != fields["footer"]
            or local_buttons != remote_buttons
        ):
            frappe.throw("ERP template header, footer and buttons must exactly match ReReply.")
        # Normal insert/save persists these verified values. Preserve ERP field
        # mappings and samples; the provider may omit examples after Meta sync.
        for field in ("template", "header_type", "header", "footer", "category", "status", "id"):
            self.set(field, fields[field])
        self.actual_name = remote["name"]
        self.language_code = remote["language"]

    def set_whatsapp_account(self):
        """Set whatsapp account to default if missing"""
        if not self.whatsapp_account:
            default_whatsapp_account = get_whatsapp_account()
            if not default_whatsapp_account:
                throw(_("Please set a default outgoing WhatsApp Account or Select available WhatsApp Account"))
            else:
                self.whatsapp_account = default_whatsapp_account.name

    def get_session_id(self, file):
        """Upload media."""
        self.get_settings()

        # Check if it's a remote file, load data accordingly
        if file.startswith(('http://', 'https://')):
            remote_file_data = self._prepare_remote_file(file)
            file_type = remote_file_data['file_type']
            file_length = remote_file_data['file_size']
        else:
            file_content = self._read_local_file(file)
            mime = magic.Magic(mime=True)
            file_type = mime.from_buffer(file_content)
            file_length = len(file_content)

        payload = {
            'file_length': file_length,
            'file_type': file_type,
            'messaging_product': 'whatsapp'
        }

        response = make_post_request(
            f"{self._url}/{self._version}/{self._app_id}/uploads",
            headers=self._headers,
            data=json.loads(json.dumps(payload))
        )
        self._session_id = response['id']

    def _prepare_remote_file(self, file_url):
        """Download and return remote file content from URL."""
        try:
            response = requests.get(file_url, timeout=30)
            response.raise_for_status()
            
            file_content = response.content
            file_size = len(file_content)
            
            # Get MIME type from Content-Type header or detect from content
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip()
            if content_type:
                file_type = content_type
            else:
                # Fallback to magic detection from content
                mime = magic.Magic(mime=True)
                file_type = mime.from_buffer(file_content)
            
            return {
                'file_content': file_content,
                'file_size': file_size,
                'file_type': file_type
            }
        except Exception as e:
            frappe.throw(f"Failed to download file from URL: {str(e)}")

    def get_media_id(self, file):
        self.get_settings()

        headers = {
                "authorization": f"OAuth {self._token}"
            }
        
        # Check if it's a remote file, load data accordingly
        if file.startswith(('http://', 'https://')):
            remote_file_data = self._prepare_remote_file(file)
            file_content = remote_file_data['file_content']
        else:
            file_content = self._read_local_file(file)

        payload = file_content
        response = make_post_request(
            f"{self._url}/{self._version}/{self._session_id}",
            headers=headers,
            data=payload
        )

        self._media_id = response['h']

    def _read_local_file(self, file_url):
        # Routed through File so path resolution stays inside Frappe's
        # vetted file handling — never feed a raw URL to open().
        return frappe.get_doc("File", {"file_url": file_url}).get_content()


    def after_insert(self):  # nosemgrep: frappe-modifying-but-not-committing -- self.actual_name/id/status are persisted via self.db_update() after the Meta round-trip; the static check can't trace through the API call
        if frappe.get_doc("WhatsApp Account", self.whatsapp_account).get("transport_provider") == "ReReply":
            return  # validate already verified the mirror; never publish from ERP.
        # actual_name / id / status are persisted via self.db_update() below
        # after the Meta round-trip; the static check can't trace that call.
        if self.template_name:
            self.actual_name = self.template_name.lower().replace(" ", "_")  # nosemgrep: frappe-modifying-but-not-committing

        self.get_settings()
        data = {
            "name": self.actual_name,
            "language": self.language_code,
            "category": self.category,
            "components": [],
        }

        body = {
            "type": "BODY",
            "text": self.template,
        }
        if self.sample_values:
            body.update({"example": {"body_text": [self.sample_values.split(",")]}})

        data["components"].append(body)
        if self.header_type:
            data["components"].append(self.get_header())

        # add footer
        if self.footer:
            data["components"].append({"type": "FOOTER", "text": self.footer})

        # add buttons
        if self.buttons:
            button_block = {"type": "BUTTONS", "buttons": []}
            for btn in self.buttons:
                b = {"type": btn.button_type, "text": btn.button_label}

                if btn.button_type == "Visit Website":
                    b["type"] = "URL"
                    b["url"] = btn.website_url
                    if btn.url_type == "Dynamic" and btn.example_url:
                        b["example"] = btn.example_url.split(",")
                elif btn.button_type == "Call Phone":
                    b["type"] = "PHONE_NUMBER"
                    b["phone_number"] = btn.phone_number
                elif btn.button_type == "Quick Reply":
                    b["type"] = "QUICK_REPLY"
                elif btn.button_type == "Multi-Product Message":
                    b["type"] = "MPM"
                elif btn.button_type == "Catalog":
                    b["type"] = "CATALOG"

                button_block["buttons"].append(b)

            data["components"].append(button_block)

        try:
            response = make_post_request(
                f"{self._url}/{self._version}/{self._business_id}/message_templates",
                headers=self._headers,
                data=json.dumps(data),
            )
            self.id = response["id"]  # nosemgrep: frappe-modifying-but-not-committing
            self.status = response["status"]  # nosemgrep: frappe-modifying-but-not-committing
            self.db_update()
        except Exception as e:
            res = frappe.flags.integration_request.json().get("error", {})
            error_message = res.get("error_user_msg", res.get("message"))
            frappe.throw(
                msg=error_message,
                title=res.get("error_user_title", "Error"),
            )

    def update_template(self):
        """Update template to meta."""
        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        if account.get("transport_provider") == "ReReply":
            self.sync_from_rereply(account)
            return
        self.get_settings()
        data = {"components": []}

        body = {
            "type": "BODY",
            "text": self.template,
        }
        if self.sample_values:
            body.update({"example": {"body_text": [self.sample_values.split(",")]}})
        data["components"].append(body)
        if self.header_type:
            data["components"].append(self.get_header())
        if self.footer:
            data["components"].append({"type": "FOOTER", "text": self.footer})
        if self.buttons:
            button_block = {"type": "BUTTONS", "buttons": []}
            for btn in self.buttons:
                b = {"type": btn.button_type, "text": btn.button_label}

                if btn.button_type == "Visit Website":
                    b["type"] = "URL"
                    b["url"] = btn.website_url
                    if btn.url_type == "Dynamic" and btn.example_url:
                        b["example"] = btn.example_url.split(",")
                elif btn.button_type == "Call Phone":
                    b["type"] = "PHONE_NUMBER"
                    b["phone_number"] = btn.phone_number
                elif btn.button_type == "Quick Reply":
                    b["type"] = "QUICK_REPLY"
                elif btn.button_type == "Multi-Product Message":
                    b["type"] = "MPM"
                    # MPM buttons often require additional fields like catalog_id
                elif btn.button_type == "Catalog":
                    b["type"] = "CATALOG"

                button_block["buttons"].append(b)

            data["components"].append(button_block)

        try:
            # post template to meta for update
            make_post_request(
                f"{self._url}/{self._version}/{self.id}",
                headers=self._headers,
                data=json.dumps(data),
            )
        except Exception as e:
            raise e
            # res = frappe.flags.integration_request.json()['error']
            # frappe.throw(
            #     msg=res.get('error_user_msg', res.get("message")),
            #     title=res.get("error_user_title", "Error"),
            # )

    def get_settings(self):
        """Get whatsapp settings."""
        # Underscore-prefixed attributes below are in-memory scratch for the
        # outbound HTTP call — they are not DocType fields and must not be
        # persisted. Semgrep's static check can't tell the difference.
        settings = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        if settings.get("transport_provider") == "ReReply":
            frappe.throw("Manage template publishing and media uploads in ReReply for this account.")
        self._token = settings.get_password("token")  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._url = settings.url  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._version = settings.version  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._business_id = settings.business_id  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._app_id = settings.app_id  # nosemgrep: frappe-modifying-but-not-committing-other-method

        self._headers = {  # nosemgrep: frappe-modifying-but-not-committing-other-method
            "authorization": f"Bearer {self._token}",
            "content-type": "application/json",
        }

    def on_trash(self):
        if frappe.get_doc("WhatsApp Account", self.whatsapp_account).get("transport_provider") == "ReReply":
            return  # Delete only the local mirror, never the provider template.
        self.get_settings()
        url = f"{self._url}/{self._version}/{self._business_id}/message_templates?name={self.actual_name}"
        try:
            make_request("DELETE", url, headers=self._headers)
        except Exception:
            res = frappe.flags.integration_request.json().get("error", {})
            if res.get("error_user_title") == "Message Template Not Found":
                frappe.msgprint(
                    "Deleted locally", res.get("error_user_title", "Error"), alert=True
                )
            else:
                frappe.throw(
                    msg=res.get("error_user_msg"),
                    title=res.get("error_user_title", "Error"),
                )

    def get_header(self):
        """Get header format."""
        header = {"type": "header", "format": self.header_type}
        if self.header_type == "TEXT":
            header["text"] = self.header
            if self.sample:
                samples = self.sample.split(", ")
                header.update({"example": {"header_text": samples}})
        else:
            pdf_link = ''
            if not self.sample:
                key = frappe.get_doc(self.doctype, self.name).get_document_share_key()
                link = get_pdf_link(self.doctype, self.name)
                pdf_link = f"{frappe.utils.get_url()}{link}&key={key}"
            header.update({"example": {"header_handle": [self._media_id]}})

        return header


def _template_button_signature(button):
    return tuple(button.get(key) or "" for key in (
        "button_type", "button_label", "website_url", "phone_number", "url_type",
    ))


def _rereply_template_fields(remote):
    """Map only component formats this ERP schema can represent faithfully."""
    from frappe_whatsapp.utils.rereply_client import ReReplyError

    category = str(remote.get("category") or "").upper()
    if category not in ("UTILITY", "MARKETING", "AUTHENTICATION"):
        raise ReReplyError("ReReply returned an unsupported template category.")
    header_type = str(remote.get("header_type") or "NONE").upper()
    if header_type not in ("NONE", "TEXT", "IMAGE", "DOCUMENT"):
        raise ReReplyError("This ReReply template header cannot be represented in ERP.")
    buttons = remote.get("buttons") or []
    if not isinstance(buttons, list):
        raise ReReplyError("ReReply returned invalid template buttons.")
    mapped = []
    types = {"URL": "Visit Website", "PHONE_NUMBER": "Call Phone", "QUICK_REPLY": "Quick Reply"}
    for button in buttons:
        if not isinstance(button, dict) or button.get("type") not in types:
            raise ReReplyError("This ReReply template button cannot be represented in ERP.")
        value = {"button_type": types[button["type"]], "button_label": button.get("text") or ""}
        if not value["button_label"]:
            raise ReReplyError("ReReply returned a template button without its label.")
        if button["type"] == "URL":
            value["website_url"] = button.get("url") or ""
            value["url_type"] = "Dynamic" if "{{" in value["website_url"] else "Static"
            if not value["website_url"]:
                raise ReReplyError("ReReply returned a template button without its URL.")
        elif button["type"] == "PHONE_NUMBER":
            value["phone_number"] = button.get("phone_number") or ""
            if not value["phone_number"]:
                raise ReReplyError("ReReply returned a template button without its phone number.")
        mapped.append(value)
    return {
        "template": remote["body_content"], "category": category,
        "status": remote["status"].upper(), "id": remote.get("meta_template_id") or "",
        "header_type": "" if header_type == "NONE" else header_type,
        "header": (remote.get("header_content") or "") if header_type == "TEXT" else "",
        "footer": remote.get("footer_content") or "", "buttons": mapped,
    }

@frappe.whitelist()
def fetch(account_name=None, template_name=None):
    """Refresh templates; optional template_name is an existing ERP document name."""
    frappe.has_permission("WhatsApp Templates", "write", throw=True)
    if template_name and not account_name:
        frappe.throw("Select a WhatsApp Account for a targeted template refresh.")
    selected_template = None
    if account_name:
        account_doc = frappe.get_doc("WhatsApp Account", account_name)
        account_doc.check_permission("read")
        if account_doc.status != "Active":
            frappe.throw("Select an active WhatsApp Account.")
    if template_name:
        selected_template = frappe.get_doc("WhatsApp Templates", template_name)
        selected_template.check_permission("write")
        if selected_template.whatsapp_account != account_name:
            frappe.throw("The selected ERP template belongs to a different WhatsApp Account.")
    filters = {"status": "Active"}
    if account_name:
        filters["name"] = account_name
    whatsapp_accounts = frappe.get_all('WhatsApp Account', filters=filters, fields=['name', 'token', 'url', 'version', 'business_id', 'transport_provider'])

    for account in whatsapp_accounts:
        if account.get("transport_provider") == "ReReply":
            # Refresh existing mirrors through the same verified GET-only path.
            # New provider templates are created/published in ReReply first.
            mirror_filters = {"whatsapp_account": account.name}
            if template_name:
                mirror_filters["name"] = template_name
            for name in frappe.get_all("WhatsApp Templates", filters=mirror_filters, pluck="name"):
                frappe.get_doc("WhatsApp Templates", name).save()
            continue
        # get credentials
        token = frappe.get_doc("WhatsApp Account", account.name).get_password("token")
        url = account.url
        version = account.version
        business_id = account.business_id

        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}

        try:
            response = make_request(
                "GET",
                f"{url}/{version}/{business_id}/message_templates",
                headers=headers,
            )

            for template in response["data"]:
                if selected_template and (
                    selected_template.actual_name != template["name"]
                    or selected_template.language_code != template["language"]
                ):
                    continue
                # set flag to insert or update
                flags = 1
                identity = {"actual_name": template["name"]}
                if frappe.db.exists("WhatsApp Templates", identity):
                    doc = frappe.get_doc("WhatsApp Templates", identity)
                    if doc.whatsapp_account and frappe.get_doc("WhatsApp Account", doc.whatsapp_account).get("transport_provider") == "ReReply":
                        frappe.throw("A Meta template fetch cannot replace a ReReply template mirror.")
                else:
                    flags = 0
                    doc = frappe.new_doc("WhatsApp Templates")
                    doc.template_name = template["name"]
                    doc.actual_name = template["name"]

                doc.status = template["status"]
                doc.language_code = template["language"]
                doc.category = template["category"]
                doc.id = template["id"]
                doc.whatsapp_account = account.name

                # update components
                for component in template["components"]:

                    # update header
                    if component["type"] == "HEADER":
                        doc.header_type = component["format"]

                        # if format is text update sample text
                        if component["format"] == "TEXT":
                            doc.header = component["text"]
                    # Update footer text
                    elif component["type"] == "FOOTER":
                        doc.footer = component["text"]

                    # update template text
                    elif component["type"] == "BODY":
                        doc.template = component["text"]
                        if component.get("example"):
    			            # Check if 'body_text' exists before trying to access it
                            if component["example"].get("body_text"):
                                doc.sample_values = ",".join(
            	                    component["example"]["body_text"][0]
                    	        )

                    # Update buttons
                    elif component["type"] == "BUTTONS":
                        doc.set("buttons", [])
                        frappe.db.delete("WhatsApp Button", {"parent": doc.name, "parenttype": "WhatsApp Templates"})
                        typeMap = {
                            "URL": "Visit Website",
                            "PHONE_NUMBER": "Call Phone",
                            "QUICK_REPLY": "Quick Reply",
                            "FLOW": "Flow",
                            "MPM": "Multi-Product Message",
                            "CATALOG": "Catalog"
                        }

                        for i, button in enumerate(component.get("buttons", []), start=1):
                            btn_type_raw = button.get("type")
                            if btn_type_raw not in typeMap:
                                frappe.log_error("WhatsApp Fetch Error", f"Unknown WhatsApp Button Type: {btn_type_raw}")
                                continue

                            btn = {}
                            btn["button_type"] = typeMap[button["type"]]
                            btn["button_label"] = button.get("text")
                            btn["sequence"] = i

                            if button["type"] == "URL":
                                btn["website_url"] = button.get("url")
                                if "{{" in btn["website_url"]:
                                    btn["url_type"] = "Dynamic"
                                else:
                                    btn["url_type"] = "Static"

                                if button.get("example"):
                                    btn["example_url"] = ",".join(button["example"])
                            elif button["type"] == "PHONE_NUMBER":
                                btn["phone_number"] = button.get("phone_number")
                            elif button["type"] == "FLOW":
                                btn["flow"] = button.get("flow")

                            doc.append("buttons", btn)

                upsert_doc_without_hooks(doc, "WhatsApp Button", "buttons")

            return "Successfully fetched templates from meta"

        except Exception as e:
            # Check if frappe.flags.integration_request is set and has a .json() method
            if hasattr(frappe.flags.integration_request, 'json'):
                try:
                    res = frappe.flags.integration_request.json().get("error", {})
                    error_message = res.get("error_user_msg", res.get("message"))
                    frappe.throw(
                        msg=error_message,
                        title=res.get("error_user_title", "Error"),
                    )
                except (json.JSONDecodeError, KeyError):
                    # Handle cases where the response is not valid JSON or lacks the 'error' key
                    frappe.throw(f"An unexpected error occurred while fetching templates: {e}")
            else:
                # Handle cases where frappe.flags.integration_request doesn't exist or isn't a proper response object
                frappe.throw(f"An unexpected server error occurred: {e}")
    return "Successfully refreshed template mirrors"

def upsert_doc_without_hooks(doc, child_dt, child_field):
    """Insert or update a parent document and its children without hooks."""
    if frappe.db.exists(doc.doctype, doc.name):
        doc.db_update()
        frappe.db.delete(child_dt, {"parent": doc.name, "parenttype": doc.doctype})
    else:
        doc.db_insert()
    for d in doc.get(child_field):
        d.parent = doc.name
        d.parenttype = doc.doctype
        d.parentfield = child_field
        d.db_insert()
