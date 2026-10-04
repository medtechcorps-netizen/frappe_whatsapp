"""ReReply transport for existing Meta-shaped ERP messages.

This module deliberately has no Frappe dependency. It never retries a message
POST: a lost response is an unknown delivery, not permission to send twice.
"""

import ipaddress
import http.client
import json
import mimetypes
import re
import socket
import ssl
from urllib.parse import unquote, urlsplit
from uuid import UUID


MAX_UPLOAD_BYTES = 14 * 1024 * 1024  # ReReply's entire multipart request is <15 MiB.
HTTP_TIMEOUT = (10, 45)


class ReReplyError(Exception):
    """A sanitized error; safe_to_retry means no message may have been sent."""

    def __init__(self, message, *, safe_to_retry=True, status_code=None):
        super().__init__(message)
        self.safe_to_retry = safe_to_retry
        self.status_code = status_code


class ReReplyAmbiguousSendError(ReReplyError):
    def __init__(self, message="ReReply delivery is unknown; reconcile before retrying."):
        super().__init__(message, safe_to_retry=False)


def _uuid(value, label):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ReReplyError("A valid ReReply {} is required.".format(label)) from None


def _phone(value):
    value = re.sub(r"[\s()+.-]", "", str(value or ""))
    if not re.fullmatch(r"[1-9][0-9]{5,14}", value):
        raise ReReplyError("Use the recipient's complete international phone number.")
    return value


def _origin(value):
    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or port not in (None, 443)
        ):
            raise ValueError
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        return "https://" + parsed.netloc.rstrip("/")
    except (ValueError, TypeError):
        raise ReReplyError("ReReply base URL must be a public HTTPS origin without a path.") from None


def validate_config(account):
    """Validate non-secret account settings without network access."""
    config = {
        "base_url": _origin(account.get("rereply_base_url")),
        "workspace_id": _uuid(account.get("rereply_workspace_id"), "workspace ID"),
        "account_id": _uuid(account.get("rereply_account_id"), "account ID"),
        "account_name": str(account.get("rereply_account_name") or "").strip(),
    }
    if not config["account_name"]:
        raise ReReplyError("The exact ReReply WhatsApp account name is required.")
    return config


def load_public_media(url):
    """Fetch one bounded HTTPS file with DNS pinned to a checked public IP.

    No credentials, cookies, proxy configuration, or redirects are accepted.
    ERP private files are supplied by the caller's site-local loader instead.
    """
    try:
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError
        hostname = parsed.hostname.encode("idna").decode("ascii")
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError
        address = addresses[0][4][0]

        class PinnedHTTPSConnection(http.client.HTTPSConnection):
            def connect(self):
                raw_socket = socket.create_connection((address, 443), timeout=10)
                try:
                    self.sock = self._context.wrap_socket(raw_socket, server_hostname=hostname)
                    self.sock.settimeout(45)
                except Exception:
                    raw_socket.close()
                    raise

        connection = PinnedHTTPSConnection(hostname, timeout=45, context=ssl.create_default_context())
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"Accept": "*/*"})
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if response.status != 200 or (length and int(length) > MAX_UPLOAD_BYTES):
                raise ValueError
            content = response.read(MAX_UPLOAD_BYTES + 1)
            if not content or len(content) > MAX_UPLOAD_BYTES:
                raise ValueError
            mime = (response.getheader("Content-Type") or "").split(";", 1)[0].strip()
            filename = unquote(parsed.path.rsplit("/", 1)[-1]) or "attachment"
            return filename, content, mime
        finally:
            connection.close()
    except Exception:
        raise ReReplyError("Public attachment could not be loaded; no message was sent.") from None


class ReReplyClient:
    def __init__(self, account, request=None, media_loader=None):
        self.account = account
        self.config = validate_config(account)
        try:
            api_key = account.get_password("rereply_api_key")
        except Exception:
            raise ReReplyError("The ReReply API key is unavailable.") from None
        if not api_key:
            raise ReReplyError("The ReReply API key is required.")
        if request is None:
            import requests

            request = requests.request
        self.request = request
        self.media_loader = media_loader
        self.headers = {
            "X-API-Key": api_key,
            "X-Organization-ID": self.config["workspace_id"],
            "Accept": "application/json",
        }

    def _call(self, method, path, *, sending=False, **kwargs):
        try:
            response = self.request(
                method,
                self.config["base_url"] + path,
                headers=self.headers.copy(),
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
                **kwargs,
            )
        except Exception:
            if sending:
                raise ReReplyAmbiguousSendError() from None
            raise ReReplyError("ReReply preparation request failed; no message was sent.") from None
        code = response.status_code
        if not 200 <= code < 300:
            # Never include the response body, which can contain private content.
            if sending and (code >= 500 or code == 408 or 300 <= code < 400):
                raise ReReplyAmbiguousSendError()
            raise ReReplyError(
                "ReReply rejected the request (HTTP {}).".format(code), status_code=code
            )
        try:
            body = response.json()
            data = body["data"]
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, KeyError, TypeError):
            if sending:
                raise ReReplyAmbiguousSendError() from None
            raise ReReplyError("ReReply returned an invalid preparation response.") from None

    def verify_account(self):
        data = self._call("GET", "/api/accounts/" + self.config["account_id"])
        if (
            data.get("id") != self.config["account_id"]
            or data.get("name") != self.config["account_name"]
            or str(data.get("status", "")).lower() != "active"
        ):
            raise ReReplyError("ReReply account identity or active status does not match ERP settings.")
        # New Coexistence IDs must be configured in ERP; a deleted old ID is not valid.
        for source, target in (("phone_id", "phone_id"), ("business_id", "business_id")):
            expected = self.account.get(source)
            if expected and str(data.get(target) or "") != str(expected):
                raise ReReplyError("ReReply phone or business account ID does not match ERP settings.")
        return data

    def _list(self, path, key, params):
        results = []
        for page in range(1, 11):
            data = self._call("GET", path, params=dict(params, page=page, limit=100))
            rows = data.get(key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ReReplyError("ReReply returned an invalid lookup response.")
            results.extend(rows)
            total = data.get("total")
            if len(rows) < 100 or (isinstance(total, int) and len(results) >= total):
                return results
        raise ReReplyError("ReReply lookup exceeded its safe result limit.")

    def template_details(self, name, language):
        """Read one exact template; never create, publish, or change it."""
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name):
            raise ReReplyError("Use the exact ReReply template name.")
        if not isinstance(language, str) or not language:
            raise ReReplyError("The exact ReReply template language is required.")
        rows = self._list(
            "/api/templates", "templates", {"account": self.config["account_name"], "search": name},
        )
        matches = [row for row in rows if row.get("name") == name
                   and row.get("language") == language
                   and row.get("whatsapp_account") == self.config["account_name"]]
        if len(matches) != 1:
            raise ReReplyError("Exactly one ReReply template must match account, name and language; manage templates in ReReply.")
        template_id = _uuid(matches[0].get("id"), "template ID")
        detail = self._call("GET", "/api/templates/" + template_id)
        if (detail.get("id") != template_id or detail.get("name") != name
                or detail.get("language") != language
                or detail.get("whatsapp_account") != self.config["account_name"]):
            raise ReReplyError("ReReply template identity does not match the requested account, name and language.")
        if not isinstance(detail.get("body_content"), str) or not detail["body_content"]:
            raise ReReplyError("ReReply template body is unavailable.")
        status = detail.get("status")
        if not isinstance(status, str) or not status:
            raise ReReplyError("ReReply template status is unavailable.")
        meta_id = detail.get("meta_template_id")
        if (meta_id and (not isinstance(meta_id, str) or not meta_id.isdigit())) or (
            status.upper() == "APPROVED" and not meta_id
        ):
            raise ReReplyError("ReReply template Meta identity is unavailable or invalid.")
        return detail

    def _find_contact(self, phone):
        rows = self._list("/api/contacts", "contacts", {"search": phone})
        matches = []
        for row in rows:
            try:
                if _phone(row.get("phone_number")) == phone:
                    matches.append(row)
            except ReReplyError:
                continue
        if len(matches) > 1:
            raise ReReplyError("Multiple ReReply contacts match this phone; reconcile them first.")
        return matches[0] if matches else None

    def contact(self, phone):
        existing = self._find_contact(phone)
        if existing:
            return _uuid(existing.get("id"), "contact ID")
        try:
            created = self._call(
                "POST",
                "/api/contacts",
                json={"phone_number": phone, "whatsapp_account": self.config["account_name"]},
            )
        except ReReplyError as error:
            if error.status_code != 409:
                raise
            # A concurrent create can win; reread, never repeat the create blindly.
            created = self._find_contact(phone)
            if not created:
                raise ReReplyError("ReReply contact exists but could not be resolved.") from None
        return _uuid(created.get("id"), "contact ID")

    def _messages(self, contact_id):
        path = "/api/contacts/" + _uuid(contact_id, "contact ID") + "/messages"
        params = {"account": self.config["account_name"], "acknowledge": "false", "limit": 100}
        before_id = None
        for _ in range(10):
            data = self._call("GET", path, params=dict(params, **({"before_id": before_id} if before_id else {})))
            rows = data.get("messages")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ReReplyError("ReReply returned an invalid message lookup response.")
            # The API returns each latest-first page in chronological order.
            # Yield immediately so finding a new message never loads all history.
            yield from reversed(rows)
            if not data.get("has_more") or not rows:
                return
            next_id = _uuid(rows[0].get("id"), "message cursor")
            if next_id == before_id:
                raise ReReplyError("ReReply message lookup cursor did not advance.")
            before_id = next_id
        raise ReReplyError("ReReply message lookup exceeded its safe history limit.")

    def message_status(self, contact_id, message_id):
        target = _uuid(message_id, "message ID")
        for row in self._messages(contact_id):
            if row.get("id") == target:
                if row.get("whatsapp_account") != self.config["account_name"]:
                    raise ReReplyError("ReReply message account does not match ERP settings.")
                return {
                    "id": target,
                    "wamid": row.get("wamid") or "",
                    "status": row.get("status") or "",
                    "contact_id": _uuid(contact_id, "contact ID"),
                }
        return None

    def _reply_id(self, contact_id, context):
        if not context:
            return None
        source = context.get("message_id")
        if not source:
            raise ReReplyError("Reply context requires a message ID.")
        if source.startswith("rereply:"):
            source = source[len("rereply:") :]
        for row in self._messages(contact_id):
            if source in (row.get("id"), row.get("wamid")):
                if row.get("whatsapp_account") != self.config["account_name"]:
                    continue
                return _uuid(row.get("id"), "reply message ID")
        raise ReReplyError("Original reply message was not found in this ReReply account.")

    def _media(self, descriptor):
        url = descriptor.get("link")
        if not url:
            raise ReReplyError("Media requires an ERP or public HTTPS file link.")
        # The caller owns site-local/public-file access. No Graph credential or
        # ReReply key is ever forwarded to a media URL by this client.
        try:
            loaded = self.media_loader(url) if self.media_loader is not None else None
            filename, content, mime_type = loaded if loaded is not None else load_public_media(url)
        except Exception:
            raise ReReplyError("ERP attachment could not be loaded; no message was sent.") from None
        if not isinstance(content, bytes) or not content or len(content) > MAX_UPLOAD_BYTES:
            raise ReReplyError("Attachment must contain between 1 byte and 14 MiB.")
        filename = descriptor.get("filename") or filename or "attachment"
        filename = str(filename).replace("\\", "/").split("/")[-1]
        if not filename or any(ord(char) < 32 for char in filename):
            raise ReReplyError("Attachment filename is invalid.")
        mime_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if not isinstance(mime_type, str) or "\r" in mime_type or "\n" in mime_type:
            raise ReReplyError("Attachment media type is invalid.")
        return filename, content, mime_type

    def _template(self, payload, contact_id):
        template = payload.get("template") or {}
        name = template.get("name")
        language = (template.get("language") or {}).get("code")
        if not name or not language:
            raise ReReplyError("Template name and language are required.")
        rows = self._list(
            "/api/templates", "templates",
            {"account": self.config["account_name"], "status": "APPROVED", "search": name},
        )
        matches = [row for row in rows if row.get("name") == name
                   and row.get("language") == language
                   and row.get("whatsapp_account") == self.config["account_name"]
                   and row.get("status") == "APPROVED"]
        if len(matches) != 1:
            raise ReReplyError("Exactly one approved ReReply template must match account, name and language.")
        body = {
            "contact_id": contact_id,
            "account_name": self.config["account_name"],
            "template_id": _uuid(matches[0].get("id"), "template ID"),
            "template_params": {}, "header_params": {}, "button_params": {},
        }
        media = None
        for component in template.get("components") or []:
            kind = component.get("type")
            params = component.get("parameters") or []
            if kind in ("body", "header"):
                target = "template_params" if kind == "body" else "header_params"
                for index, param in enumerate(params, 1):
                    if param.get("type") == "text":
                        key = param.get("parameter_name") or str(index)
                        body[target][key] = str(param.get("text") or "")
                    elif kind == "header" and param.get("type") in ("image", "video", "document"):
                        if media is not None:
                            raise ReReplyError("Only one template header attachment is supported.")
                        descriptor = param.get(param["type"]) or {}
                        if descriptor.get("id"):
                            raise ReReplyError("Use an ERP file link instead of a media ID from an old Meta account.")
                        media = self._media(descriptor)
                        body["header_media_filename"] = media[0]
                    else:
                        raise ReReplyError("This template parameter type is not supported by the ReReply connector.")
            elif kind == "button" and component.get("sub_type") == "url":
                if len(params) != 1 or params[0].get("type") != "text":
                    raise ReReplyError("Dynamic URL buttons require one text parameter.")
                body["button_params"][str(component.get("index", "0"))] = str(params[0].get("text") or "")
            else:
                raise ReReplyError("This template component is not supported by the ReReply connector.")
        if media is None:
            return {"json": body}
        form = {key: json.dumps(value) if isinstance(value, dict) else value for key, value in body.items()}
        return {"data": form, "files": {"header_file": media}}

    def send(self, payload):
        if not isinstance(payload, dict):
            raise ReReplyError("Outgoing WhatsApp payload must be an object.")
        kind = payload.get("type")
        if kind not in ("text", "template", "image", "document", "audio", "video"):
            raise ReReplyError("This outgoing message type is not supported by the ReReply connector.")
        phone = _phone(payload.get("to"))
        self.verify_account()
        contact_id = self.contact(phone)
        if kind == "text":
            body_text = (payload.get("text") or {}).get("body")
            if not isinstance(body_text, str) or not body_text.strip():
                raise ReReplyError("Text message body cannot be empty.")
            body = {"type": "text", "content": {"body": body_text}, "whatsapp_account": self.config["account_name"]}
            reply_id = self._reply_id(contact_id, payload.get("context"))
            if reply_id:
                body["reply_to_message_id"] = reply_id
            path, kwargs = "/api/contacts/" + contact_id + "/messages", {"json": body}
        elif kind == "template":
            if payload.get("context"):
                raise ReReplyError("Template reply context is not supported by the ReReply send API.")
            path, kwargs = "/api/messages/template", self._template(payload, contact_id)
        else:
            if payload.get("context"):
                raise ReReplyError("Media reply context is not supported by the ReReply send API.")
            descriptor = payload.get(kind) or {}
            media = self._media(descriptor)
            path, kwargs = "/api/messages/media", {
                "data": {"contact_id": contact_id, "type": kind,
                         "caption": str(descriptor.get("caption") or ""),
                         "whatsapp_account": self.config["account_name"]},
                "files": {"file": media},
            }
        data = self._call("POST", path, sending=True, **kwargs)
        try:
            message_id = _uuid(data.get("id"), "message ID")
            if data.get("contact_id") != contact_id or data.get("whatsapp_account") != self.config["account_name"]:
                raise ValueError
        except (ReReplyError, ValueError):
            raise ReReplyAmbiguousSendError("ReReply accepted the request but its message identity could not be verified.") from None
        wamid = data.get("wamid") or ""
        status = data.get("status") or "sent"
        # Standard ReReply send responses omit WAMID. A GET may recover it;
        # failure here never causes an already accepted message to be resent.
        if not wamid:
            try:
                matched = self.message_status(contact_id, message_id)
                if matched:
                    wamid, status = matched["wamid"], matched["status"] or status
            except ReReplyError:
                pass
        return {
            "messages": [{"id": wamid or "rereply:" + message_id}],
            "rereply_message_id": message_id,
            "rereply_contact_id": contact_id,
            "rereply_status": status,
            "wamid": wamid,
        }


def send_via_rereply(account, payload, request=None, media_loader=None):
    return ReReplyClient(account, request=request, media_loader=media_loader).send(payload)


def get_message_status(account, contact_id, message_id, request=None):
    """Read status without acknowledging customer messages or sending receipts."""
    client = ReReplyClient(account, request=request)
    client.verify_account()
    return client.message_status(contact_id, message_id)


def get_template_details(account, name, language, request=None):
    """Read a mirror only after verifying the configured current phone and WABA."""
    if not account.get("phone_id") or not account.get("business_id"):
        raise ReReplyError("Set the current ReReply phone ID and business account ID before importing templates.")
    client = ReReplyClient(account, request=request)
    client.verify_account()
    return client.template_details(name, language)
