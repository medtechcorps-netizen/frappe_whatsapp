"""Transport contract tests; run directly without a Frappe site or network."""

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("rereply_client_contract", Path(__file__).with_name("rereply_client.py"))
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

WORKSPACE = "00000000-0000-4000-8000-000000000001"
ACCOUNT = "00000000-0000-4000-8000-000000000002"
CONTACT = "00000000-0000-4000-8000-000000000003"
MESSAGE = "00000000-0000-4000-8000-000000000004"
TEMPLATE = "00000000-0000-4000-8000-000000000005"
REPLY = "00000000-0000-4000-8000-000000000006"
NAME = "Medtech Business"


class Account(dict):
    def get_password(self, field):
        assert field == "rereply_api_key"
        return "test-api-key-never-in-errors"


def account(**changes):
    result = Account(rereply_base_url="https://app.rereply.app", rereply_workspace_id=WORKSPACE,
                     rereply_account_id=ACCOUNT, rereply_account_name=NAME,
                     phone_id="new-phone", business_id="new-waba")
    result.update(changes)
    return result


def response(data=None, status=200):
    return Mock(status_code=status, json=Mock(return_value={"data": data}))


def verified(**changes):
    result = dict(id=ACCOUNT, name=NAME, status="active", phone_id="new-phone", business_id="new-waba")
    result.update(changes)
    return response(result)


def contacts(rows=None):
    if rows is None:
        rows = [{"id": CONTACT, "phone_number": "60123456789", "whatsapp_account": NAME}]
    return response({"contacts": rows, "total": len(rows)})


def accepted(**changes):
    result = dict(id=MESSAGE, contact_id=CONTACT, whatsapp_account=NAME, status="sent")
    result.update(changes)
    return response(result)


def history(rows=None, more=False):
    if rows is None:
        rows = [{"id": MESSAGE, "wamid": "wamid.verified", "status": "delivered", "whatsapp_account": NAME}]
    return response({"messages": rows, "has_more": more})


def text_payload():
    return {"messaging_product": "whatsapp", "to": "+60 12-345 6789", "type": "text", "text": {"body": "Hello"}}


class ReReplyClientTest(unittest.TestCase):
    def test_text_send_scopes_every_request_and_recovers_wamid(self):
        request = Mock(side_effect=[verified(), contacts(), accepted(), history()])
        result = client.send_via_rereply(account(), text_payload(), request=request)
        self.assertEqual(result["messages"], [{"id": "wamid.verified"}])
        self.assertEqual(result["rereply_message_id"], MESSAGE)
        self.assertEqual(result["rereply_contact_id"], CONTACT)
        sent = request.call_args_list[2]
        self.assertEqual(sent.args, ("POST", "https://app.rereply.app/api/contacts/" + CONTACT + "/messages"))
        self.assertEqual(sent.kwargs["json"], {"type": "text", "content": {"body": "Hello"}, "whatsapp_account": NAME})
        for call in request.call_args_list:
            self.assertEqual(call.kwargs["headers"]["X-Organization-ID"], WORKSPACE)
            self.assertEqual(call.kwargs["headers"]["X-API-Key"], "test-api-key-never-in-errors")
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(request.call_args.kwargs["params"]["acknowledge"], "false")
        self.assertEqual(request.call_args.kwargs["params"]["account"], NAME)

    def test_missing_wamid_readback_failure_preserves_unique_rereply_identity(self):
        request = Mock(side_effect=[verified(), contacts(), accepted(), TimeoutError("sensitive")])
        result = client.send_via_rereply(account(), text_payload(), request=request)
        self.assertEqual(result["messages"][0]["id"], "rereply:" + MESSAGE)
        self.assertEqual(result["wamid"], "")
        self.assertEqual(request.call_count, 4)

    def test_ambiguous_send_never_retries_or_exposes_error(self):
        for failure in [TimeoutError("test-api-key-never-in-errors"), response(status=500), response(status=408), response(status=302)]:
            with self.subTest(failure=type(failure).__name__):
                request = Mock(side_effect=[verified(), contacts(), failure])
                with self.assertRaises(client.ReReplyAmbiguousSendError) as raised:
                    client.send_via_rereply(account(), text_payload(), request=request)
                self.assertFalse(raised.exception.safe_to_retry)
                self.assertNotIn("test-api-key", str(raised.exception))
                self.assertEqual(request.call_count, 3)

    def test_definitive_rejection_is_safe_to_review_and_retry(self):
        request = Mock(side_effect=[verified(), contacts(), response(status=400)])
        with self.assertRaises(client.ReReplyError) as raised:
            client.send_via_rereply(account(), text_payload(), request=request)
        self.assertTrue(raised.exception.safe_to_retry)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(request.call_count, 3)

    def test_account_mismatch_prevents_any_send(self):
        for changed in [{"id": CONTACT}, {"name": "Another account"}, {"phone_id": "old-phone"}, {"business_id": "old-waba"}, {"status": "pending"}]:
            request = Mock(return_value=verified(**changed))
            with self.assertRaises(client.ReReplyError):
                client.send_via_rereply(account(), text_payload(), request=request)
            self.assertEqual(request.call_count, 1)

    def test_contact_lookup_uses_exact_phone_and_handles_create_race(self):
        request = Mock(side_effect=[verified(), contacts([{"id": REPLY, "phone_number": "601234567890"}]),
                                   response(status=409), contacts(), accepted(wamid="wamid.direct")])
        result = client.send_via_rereply(account(), text_payload(), request=request)
        self.assertEqual(result["wamid"], "wamid.direct")
        self.assertEqual(request.call_args_list[2].kwargs["json"], {"phone_number": "60123456789", "whatsapp_account": NAME})
        self.assertEqual(request.call_count, 5)

    def test_invalid_success_identity_is_unknown_not_failed(self):
        request = Mock(side_effect=[verified(), contacts(), accepted(contact_id=REPLY)])
        with self.assertRaises(client.ReReplyAmbiguousSendError):
            client.send_via_rereply(account(), text_payload(), request=request)

    def test_template_preserves_language_params_and_document_header(self):
        payload = {"to": "60123456789", "type": "template", "template": {
            "name": "invoice", "language": {"code": "ms"}, "components": [
                {"type": "body", "parameters": [{"type": "text", "text": "Customer"}, {"type": "text", "text": "RM 50"}]},
                {"type": "header", "parameters": [{"type": "document", "document": {"link": "/private/files/invoice.pdf", "filename": "Invoice.pdf"}}]},
                {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "invoice-1"}]},
            ]}}
        templates = [{"id": TEMPLATE, "name": "invoice", "language": "ms", "whatsapp_account": NAME, "status": "APPROVED"},
                     {"id": REPLY, "name": "invoice", "language": "en", "whatsapp_account": NAME, "status": "APPROVED"}]
        request = Mock(side_effect=[verified(), contacts(), response({"templates": templates, "total": 2}), accepted(wamid="wamid.template")])
        loader = Mock(return_value=("original.pdf", b"pdf-bytes", "application/pdf"))
        client.send_via_rereply(account(), payload, request=request, media_loader=loader)
        sent = request.call_args
        self.assertEqual(sent.args[1], "https://app.rereply.app/api/messages/template")
        self.assertEqual(sent.kwargs["data"]["template_id"], TEMPLATE)
        self.assertEqual(json.loads(sent.kwargs["data"]["template_params"]), {"1": "Customer", "2": "RM 50"})
        self.assertEqual(json.loads(sent.kwargs["data"]["button_params"]), {"0": "invoice-1"})
        self.assertEqual(sent.kwargs["files"]["header_file"], ("Invoice.pdf", b"pdf-bytes", "application/pdf"))

    def test_template_missing_or_ambiguous_match_does_not_send(self):
        payload = {"to": "60123456789", "type": "template", "template": {"name": "invoice", "language": {"code": "ms"}}}
        request = Mock(side_effect=[verified(), contacts(), response({"templates": [], "total": 0})])
        with self.assertRaises(client.ReReplyError):
            client.send_via_rereply(account(), payload, request=request)
        self.assertEqual(request.call_count, 3)

    def test_media_upload_uses_bytes_and_caption_with_no_url_credentials(self):
        for kind in ["image", "document", "audio", "video"]:
            with self.subTest(kind=kind):
                payload = {"to": "60123456789", "type": kind, kind: {"link": "/files/attachment", "caption": "Preview"}}
                loader = Mock(return_value=("file.dat", b"test", "application/octet-stream"))
                request = Mock(side_effect=[verified(), contacts(), accepted(wamid="wamid.media")])
                client.send_via_rereply(account(), payload, request=request, media_loader=loader)
                self.assertEqual(request.call_args.kwargs["files"]["file"][1], b"test")
                self.assertEqual(request.call_args.kwargs["data"]["type"], kind)
                self.assertEqual(request.call_args.kwargs["data"]["whatsapp_account"], NAME)

    def test_oversized_media_fails_before_send(self):
        payload = {"to": "60123456789", "type": "document", "document": {"link": "/files/test"}}
        request = Mock(side_effect=[verified(), contacts()])
        with patch.object(client, "MAX_UPLOAD_BYTES", 4):
            with self.assertRaises(client.ReReplyError):
                client.send_via_rereply(account(), payload, request=request, media_loader=lambda _: ("x", b"12345", "text/plain"))
        self.assertEqual(request.call_count, 2)

    def test_media_loader_none_uses_public_fetch_without_api_headers(self):
        payload = {"to": "60123456789", "type": "document", "document": {"link": "https://example.org/file.pdf"}}
        request = Mock(side_effect=[verified(), contacts(), accepted(wamid="wamid.media")])
        with patch.object(client, "load_public_media", return_value=("file.pdf", b"test", "application/pdf")) as fetch:
            client.send_via_rereply(account(), payload, request=request, media_loader=lambda _: None)
            fetch.assert_called_once_with("https://example.org/file.pdf")

    def test_public_media_rejects_private_dns_and_insecure_urls(self):
        for url in ["http://example.org/file", "https://user:pass@example.org/file", "https://example.org:8443/file"]:
            with patch.object(client.socket, "getaddrinfo") as dns:
                with self.assertRaises(client.ReReplyError):
                    client.load_public_media(url)
                dns.assert_not_called()
        with patch.object(client.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with patch.object(client.socket, "create_connection") as connect:
                with self.assertRaises(client.ReReplyError):
                    client.load_public_media("https://example.org/file")
                connect.assert_not_called()

    def test_public_media_pins_checked_ip_and_keeps_tls_hostname(self):
        public_dns = [(2, 1, 6, "", ("93.184.216.34", 443))]
        remote = Mock(status=200)
        remote.getheader.side_effect = lambda name: {"Content-Length": "4", "Content-Type": "application/pdf"}.get(name)
        remote.read.return_value = b"file"
        tls_context = Mock()
        raw_socket = Mock()
        with patch.object(client.socket, "getaddrinfo", return_value=public_dns), \
             patch.object(client.socket, "create_connection", return_value=raw_socket) as connect, \
             patch.object(client.ssl, "create_default_context", return_value=tls_context), \
             patch.object(client.http.client.HTTPSConnection, "request", autospec=True,
                          side_effect=lambda connection, *args, **kwargs: connection.connect()) as request, \
             patch.object(client.http.client.HTTPSConnection, "getresponse", return_value=remote):
            result = client.load_public_media("https://example.org/file.pdf")
        self.assertEqual(result, ("file.pdf", b"file", "application/pdf"))
        connect.assert_called_once_with(("93.184.216.34", 443), timeout=10)
        tls_context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.org")
        self.assertEqual(request.call_args.kwargs["headers"], {"Accept": "*/*"})

    def test_public_media_does_not_follow_redirect(self):
        remote = Mock(status=302)
        remote.getheader.return_value = None
        with patch.object(client.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
             patch.object(client.http.client.HTTPSConnection, "request") as request, \
             patch.object(client.http.client.HTTPSConnection, "getresponse", return_value=remote):
            with self.assertRaises(client.ReReplyError):
                client.load_public_media("https://example.org/file.pdf")
        request.assert_called_once()
        remote.read.assert_not_called()

    def test_reply_context_maps_wamid_to_rereply_message_uuid(self):
        payload = text_payload()
        payload["context"] = {"message_id": "wamid.original"}
        request = Mock(side_effect=[verified(), contacts(), history([
            {"id": REPLY, "wamid": "wamid.original", "whatsapp_account": NAME}
        ]), accepted(wamid="wamid.reply")])
        client.send_via_rereply(account(), payload, request=request)
        self.assertEqual(request.call_args.kwargs["json"]["reply_to_message_id"], REPLY)

    def test_status_poll_follows_cursor_without_marking_read(self):
        request = Mock(side_effect=[verified(), history([
            {"id": REPLY, "wamid": "wamid.other", "whatsapp_account": NAME}
        ], more=True), history()])
        result = client.get_message_status(account(), CONTACT, MESSAGE, request=request)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(request.call_args.kwargs["params"]["before_id"], REPLY)
        self.assertEqual(request.call_args.kwargs["params"]["acknowledge"], "false")

    def test_base_url_cannot_redirect_credentials_to_insecure_or_path_url(self):
        for url in ["http://app.rereply.app", "https://app.rereply.app/api", "https://user:pass@app.rereply.app", "https://127.0.0.1", "https://app.rereply.app?token=anything"]:
            with self.assertRaises(client.ReReplyError):
                client.validate_config(account(rereply_base_url=url))


if __name__ == "__main__":
    unittest.main()
