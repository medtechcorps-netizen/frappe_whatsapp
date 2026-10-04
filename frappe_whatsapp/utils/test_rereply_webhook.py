"""Isolated contract tests; no bench, network, real secrets or live database.

Run with ``python frappe_whatsapp/utils/test_rereply_webhook.py``.
The small transaction stub exercises the durable handoff and failure policy;
it does not replace a Frappe/MariaDB integration check before enabling ingress.
"""

import copy
import hashlib
import hmac
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase, main
from unittest.mock import MagicMock, patch


class _Doc:
    def __init__(self, app, values):
        self._app = app
        self.flags = SimpleNamespace()
        self.__dict__.update(copy.deepcopy(values))

    def get(self, name, default=None):
        return getattr(self, name, default)

    def get_password(self, field, raise_exception=False):
        return self._app.secrets.get((self.name, field))

    def _values(self):
        return {key: value for key, value in self.__dict__.items()
                if key not in {"_app", "flags"}}

    def insert(self, ignore_permissions=False, set_name=None):
        app = self._app
        app.operations.append(("insert", self.doctype))
        self.name = set_name or self.get("name") or "message-%s" % len(app.db.rows)
        key = (self.doctype, self.name)
        if key in app.db.rows:
            raise app.DuplicateEntryError()
        if self.doctype == "ReReply Webhook Receipt":
            if app.force_unique_conflict or app.db.get_value(
                self.doctype, {"event_key": self.event_key}, "name"
            ):
                raise app.UniqueValidationError()
        if self.doctype == "WhatsApp Message":
            app.inserted_message_flags.append(vars(self.flags).copy())
            app.hook_calls += 1
            if app.fail_message_insert:
                raise RuntimeError("simulated hook failure")
        self.creation = self.modified = datetime.now()
        app.db.rows[key] = copy.deepcopy(self._values())
        return self

    def save(self, ignore_permissions=False):
        self.modified = datetime.now()
        self._app.db.rows[(self.doctype, self.name)] = copy.deepcopy(self._values())
        return self


class _DB:
    def __init__(self, app):
        self.app = app
        self.rows = {}
        self.committed = {}
        self.savepoints = {}

    def _matches(self, values, filters):
        for key, expected in filters.items():
            actual = values.get(key)
            if isinstance(expected, list) and expected[0] == "<":
                if not actual < expected[1]:
                    return False
            elif actual != expected:
                return False
        return True

    def exists(self, doctype, name):
        if isinstance(name, dict):
            return self.get_value(doctype, name, "name")
        return name if (doctype, name) in self.rows else None

    def get_value(self, doctype, filters, field):
        for (kind, _), values in self.rows.items():
            if kind == doctype and self._matches(values, filters):
                return values.get(field)
        return None

    def savepoint(self, name):
        self.savepoints[name] = copy.deepcopy(self.rows)

    def commit(self):
        self.app.operations.append(("commit", None))
        self.committed = copy.deepcopy(self.rows)

    def rollback(self, save_point=None):
        self.app.operations.append(("rollback", save_point))
        self.rows = copy.deepcopy(self.savepoints[save_point] if save_point else self.committed)

    def sql(self, query, args):
        self.app.operations.append(("lock", args[0]))
        if "SELECT paused_until" in query:
            pauses = [row for (kind, _), row in self.rows.items()
                      if kind == "WhatsApp Bot Pause" and row["phone"] == args[0]]
            ordering = "creation" if "ORDER BY creation DESC" in query else "paused_until"
            latest = max(pauses, key=lambda row: row[ordering]) if pauses else None
            return [(latest["paused_until"],)] if latest else []
        return [(args[0],)] if ("ReReply Webhook Receipt", args[0]) in self.rows else []


def _fake_frappe():
    app = ModuleType("frappe")
    app.operations = []
    app.secrets = {}
    app.hook_calls = 0
    app.inserted_message_flags = []
    app.fail_message_insert = False
    app.force_unique_conflict = False
    app.conf = {}
    app.db = _DB(app)
    app.utils = SimpleNamespace(now_datetime=datetime.now,
                                get_datetime=lambda value: value if isinstance(value, datetime) else datetime.fromisoformat(value))
    app.pause_fields = {"phone", "paused_until", "source", "note"}
    app.get_meta = lambda kind: SimpleNamespace(has_field=lambda field: field in app.pause_fields)
    app.enqueue = MagicMock()
    app.whitelist = lambda **kwargs: lambda method: method
    for name in ("AuthenticationError", "ValidationError", "DuplicateEntryError", "UniqueValidationError"):
        setattr(app, name, type(name, (Exception,), {}))

    def throw(message, exception):
        raise exception(message)

    def get_doc(kind, name=None):
        return _Doc(app, kind if isinstance(kind, dict) else app.db.rows[(kind, name)])

    def get_all(kind, filters, pluck, **kwargs):
        return [row[pluck] for (doctype, _), row in app.db.rows.items()
                if doctype == kind and app.db._matches(row, filters)]

    app.throw = throw
    app.get_doc = get_doc
    app.get_all = get_all
    return app


class TestReReplyWebhook(TestCase):
    def setUp(self):
        self.app = _fake_frappe()
        spec = importlib.util.spec_from_file_location(
            "rereply_webhook_under_test", Path(__file__).with_name("rereply_webhook.py")
        )
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"frappe": self.app}):
            spec.loader.exec_module(self.module)
        self.app.db.rows[("WhatsApp Account", "ERP Concierge")] = {
            "doctype": "WhatsApp Account", "name": "ERP Concierge",
            "transport_provider": "ReReply", "status": "Active",
            "rereply_inbound_enabled": 1, "rereply_outbound_enabled": 1,
            "rereply_workspace_id": "workspace-1",
            "rereply_account_id": "account-1", "rereply_account_name": "Medtech Concierge",
        }
        self.app.secrets[("ERP Concierge", "rereply_webhook_secret")] = "local-test-only"
        self.app.db.commit()
        self.app.operations.clear()
        self.payload = {
            "event": "message.incoming", "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": {
                "outbox_event_id": "event-1", "message_id": "provider-message-1",
                "source_id": "provider-message-1", "source_type": "message",
                "event_type": "message.incoming", "direction": "incoming",
                "actor_type": "contact", "occurred_at": datetime.now(timezone.utc).isoformat(),
                "whatsapp_account": "Medtech Concierge", "contact_phone": "+60123334444",
                "contact_name": "Test Sender", "contact_id": "contact-1",
                "message_type": "text", "content": "Hello, 你好",
                "metadata": {"whatsapp_account": "Medtech Concierge"},
            },
        }

    def request(self, payload=None, raw=None, secret="local-test-only", account="ERP Concierge"):
        raw = raw if raw is not None else json.dumps(payload or self.payload, ensure_ascii=False).encode()
        signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        self.app.request = SimpleNamespace(
            method="POST", headers={self.module.ACCOUNT_HEADER: account,
                                   self.module.SIGNATURE_HEADER: signature},
            get_data=MagicMock(return_value=raw),
        )
        return self.app.request

    def ingest(self, payload=None):
        self.request(payload)
        result = self.module.webhook()
        # Simulate Frappe's successful POST commit before a worker runs.
        self.app.db.commit()
        return result["receipt"]

    def receipt(self, name):
        return self.app.db.rows[(self.module.RECEIPT_DOCTYPE, name)]

    def messages(self):
        return [row for (kind, _), row in self.app.db.rows.items() if kind == "WhatsApp Message"]

    def test_exact_bytes_signature_rejects_tampering_before_writes(self):
        request = self.request()
        request.get_data.return_value += b" "
        with self.assertRaises(self.app.AuthenticationError):
            self.module.webhook()
        self.assertFalse(self.app.operations)
        self.app.enqueue.assert_not_called()

    def test_invalid_signature_precedes_json_decode(self):
        self.request(raw=b"not-json", secret="wrong")
        with self.assertRaises(self.app.AuthenticationError):
            self.module.webhook()
        self.assertFalse(self.app.operations)

    def test_missing_unknown_inactive_meta_and_disabled_accounts_fail_closed(self):
        for header in (None, "unknown"):
            with self.subTest(header=header):
                self.request(account=header)
                with self.assertRaises(self.app.AuthenticationError):
                    self.module.webhook()
        account = self.app.db.rows[("WhatsApp Account", "ERP Concierge")]
        for key, value in (("transport_provider", "Meta"), ("status", "Inactive"),
                           ("rereply_inbound_enabled", 0), ("rereply_workspace_id", ""),
                           ("rereply_account_id", ""), ("rereply_outbound_enabled", 0)):
            with self.subTest(key=key):
                original = account[key]
                account[key] = value
                self.request()
                with self.assertRaises(self.app.AuthenticationError):
                    self.module.webhook()
                account[key] = original
        self.assertFalse(self.app.operations)

    def test_signed_wrong_account_and_meta_payload_cannot_write(self):
        self.payload["data"]["whatsapp_account"] = "Different Business"
        self.request()
        with self.assertRaises(self.app.AuthenticationError):
            self.module.webhook()
        self.request({"entry": [{"id": "meta-waba"}]})
        with self.assertRaises(self.app.ValidationError):
            self.module.webhook()
        self.assertFalse(self.app.operations)

    def test_get_empty_body_and_oversize_body_are_rejected(self):
        request = self.request()
        request.method = "GET"
        with self.assertRaises(self.app.AuthenticationError):
            self.module.webhook()
        for raw in (b"", b"x" * (self.module.MAX_BODY_BYTES + 1)):
            self.request(raw=raw)
            with self.assertRaises(self.app.AuthenticationError):
                self.module.webhook()
        self.assertFalse(self.app.operations)

    def test_acceptance_queues_after_commit_without_running_hooks(self):
        name = self.ingest()
        self.assertEqual(self.receipt(name)["status"], "Pending")
        self.assertEqual(self.app.hook_calls, 0)
        kwargs = self.app.enqueue.call_args.kwargs
        self.assertTrue(kwargs["enqueue_after_commit"])
        self.assertEqual(kwargs["name"], name)

    def test_raw_delivery_timestamp_changes_are_still_deduplicated(self):
        name = self.ingest()
        self.payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        self.payload["data"]["outbox_event_id"] = "different-event-same-message"
        self.request()
        self.assertEqual(self.module.webhook()["status"], "duplicate")
        self.module.process_receipt(name)
        self.module.process_receipt(name)
        self.assertEqual(self.app.hook_calls, 1)
        message = self.messages()[0]
        self.assertEqual(message["type"], "Incoming")
        self.assertEqual(message["message_id"], "rereply:provider-message-1")
        self.assertEqual(message["rereply_message_id"], "provider-message-1")
        self.assertEqual(message["from"], "60123334444")
        self.assertEqual(message["whatsapp_account"], "ERP Concierge")

    def test_event_id_and_concurrent_insert_conflicts_are_deduplicated(self):
        self.ingest()
        self.payload["data"]["message_id"] = "different-message-same-event"
        self.request()
        self.assertEqual(self.module.webhook()["status"], "duplicate")
        self.payload["data"]["outbox_event_id"] = "new-event"
        self.app.force_unique_conflict = True
        self.request()
        self.assertEqual(self.module.webhook()["status"], "duplicate")
        self.assertIn(("rollback", "rereply_receipt_insert"), self.app.operations)

    def test_failure_after_claim_is_held_and_never_replays_automation(self):
        name = self.ingest()
        self.app.fail_message_insert = True
        self.app.operations.clear()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["status"], "Failed")
        self.assertLess(self.app.operations.index(("commit", None)),
                        self.app.operations.index(("insert", "WhatsApp Message")))
        self.app.fail_message_insert = False
        self.module.process_receipt(name)
        self.request()
        self.assertEqual(self.module.webhook()["status"], "duplicate")
        self.assertEqual(self.app.hook_calls, 1)
        self.assertEqual(self.messages(), [])

    def test_stale_history_outgoing_and_unsupported_incoming_never_run_bot(self):
        cases = [
            {"occurred_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()},
            {"is_history": True}, {"direction": "outgoing"},
            {"message_type": "image", "content": "image caption"},
            {"message_type": "button_reply", "content": "Displayed title"},
            {"source_type": "history"}, {"occurred_at": None},
        ]
        for index, changes in enumerate(cases):
            payload = copy.deepcopy(self.payload)
            payload["data"].update(changes)
            payload["data"].update(message_id="msg-%s" % index, source_id="msg-%s" % index,
                                    outbox_event_id="event-%s" % index)
            name = self.ingest(payload)
            self.module.process_receipt(name)
            self.assertEqual(self.receipt(name)["status"], "Ignored")
        self.assertEqual(self.app.hook_calls, 0)

    def test_account_disabled_after_acceptance_does_not_dispatch(self):
        name = self.ingest()
        self.app.db.rows[("WhatsApp Account", "ERP Concierge")]["rereply_inbound_enabled"] = 0
        self.app.db.commit()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["status"], "Ignored")
        self.assertEqual(self.app.hook_calls, 0)

    def test_mobile_echo_is_passive_and_sent_origin_is_not_guessed(self):
        self.payload["event"] = "message.outgoing"
        self.payload["data"].update(direction="outgoing")
        self.payload["data"].pop("outbox_event_id")
        name = self.ingest()
        self.module.process_receipt(name)
        self.assertEqual(self.messages()[0]["type"], "Outgoing")
        self.assertTrue(self.app.inserted_message_flags[0]["rereply_passive_log"])

        self.payload["event"] = "message.sent"
        self.payload["data"]["message_id"] = "unmatched-api-message"
        name = self.ingest()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["status"], "Pending")
        saved_payload = json.loads(self.receipt(name)["payload"])
        saved_payload["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.receipt(name)["payload"] = json.dumps(saved_payload)
        self.receipt(name)["payload_hash"] = hashlib.sha256(self.receipt(name)["payload"].encode()).hexdigest()
        self.app.db.commit()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["status"], "Ignored")
        self.assertEqual(self.app.hook_calls, 1)

    def test_changed_stored_payload_is_not_executed(self):
        name = self.ingest()
        self.receipt(name)["payload"] = self.receipt(name)["payload"].replace("Hello", "Changed")
        self.app.db.commit()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["status"], "Ignored")
        self.assertEqual(self.app.hook_calls, 0)

    def test_dedicated_integration_identity_allows_signed_staff_activity(self):
        self.app.db.rows[("WhatsApp Account", "ERP Concierge")]["rereply_integration_user_id"] = (
            "10000000-0000-4000-8000-000000000001"
        )
        self.payload["event"] = "message.sent"
        self.payload["data"].update(direction="outgoing", sent_by_user_id="20000000-0000-4000-8000-000000000002")
        self.payload["data"].pop("outbox_event_id")
        name = self.ingest()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["status"], "Processed")
        self.assertEqual(self.messages()[0]["type"], "Outgoing")
        self.assertEqual(self.messages()[0]["rereply_source"], "message.sent")
        self.assertTrue(self.app.inserted_message_flags[0]["rereply_passive_log"])

    def test_same_missing_or_invalid_sender_never_claims_staff_activity(self):
        integration_id = "10000000-0000-4000-8000-000000000001"
        other_id = "20000000-0000-4000-8000-000000000002"
        cases = [(integration_id, integration_id), (integration_id, None),
                 (None, other_id), (integration_id, "not-uuid"), ("not-uuid", other_id)]
        for index, (integration, sender) in enumerate(cases):
            self.app.db.rows[("WhatsApp Account", "ERP Concierge")]["rereply_integration_user_id"] = integration
            payload = copy.deepcopy(self.payload)
            payload["event"] = "message.sent"
            payload["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            payload["data"].update(direction="outgoing", sent_by_user_id=sender,
                                    message_id="sender-%s" % index, outbox_event_id="sender-event-%s" % index)
            name = self.ingest(payload)
            self.module.process_receipt(name)
            self.assertEqual(self.receipt(name)["status"], "Ignored")
        self.assertEqual(self.app.hook_calls, 0)

    def _enable_pause(self):
        self.app.db.rows[("DocType", "WhatsApp Bot Pause")] = {"name": "WhatsApp Bot Pause"}

    def _pauses(self):
        return [row for (kind, _), row in self.app.db.rows.items() if kind == "WhatsApp Bot Pause"]

    def test_mobile_handoff_has_remaining_duration_no_fake_user_and_no_replay(self):
        self._enable_pause()
        self.payload["event"] = "message.outgoing"
        self.payload["data"]["direction"] = "outgoing"
        self.payload["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        name = self.ingest()
        self.module.process_receipt(name)
        pauses = self._pauses()
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0]["source"], "ReReplyMobile")
        self.assertNotIn("paused_by", pauses[0])
        remaining = (pauses[0]["paused_until"] - datetime.now()).total_seconds()
        self.assertGreater(remaining, 175)
        self.assertLessEqual(remaining, 180)
        self.module.process_receipt(name)
        self.assertEqual(len(self._pauses()), 1)

    def test_old_echo_does_not_pause_current_conversation(self):
        self._enable_pause()
        self.payload["event"] = "message.outgoing"
        self.payload["data"]["direction"] = "outgoing"
        self.payload["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        name = self.ingest()
        self.module.process_receipt(name)
        self.assertEqual(self._pauses(), [])
        self.assertEqual(self.receipt(name)["status"], "Processed")

    def test_existing_longer_pause_is_not_shortened(self):
        self._enable_pause()
        longer = datetime.now() + timedelta(hours=1)
        self.app.db.rows[("WhatsApp Bot Pause", "existing-pause")] = {
            "name": "existing-pause", "phone": "60123334444", "paused_until": longer,
            "creation": datetime.now() - timedelta(minutes=1),
        }
        self.payload["event"] = "message.outgoing"
        self.payload["data"]["direction"] = "outgoing"
        name = self.ingest()
        self.module.process_receipt(name)
        self.assertEqual(len(self._pauses()), 1)
        self.assertEqual(self._pauses()[0]["paused_until"], longer)

    def test_new_activity_pauses_after_newer_resume_overrides_historical_long_pause(self):
        self._enable_pause()
        now = datetime.now()
        for name, created, expiry in (
            ("older-long-pause", now - timedelta(minutes=2), now + timedelta(hours=1)),
            ("newer-resume", now - timedelta(minutes=1), now - timedelta(minutes=1)),
        ):
            self.app.db.rows[("WhatsApp Bot Pause", name)] = {
                "name": name, "phone": "60123334444", "paused_until": expiry, "creation": created,
            }
        self.payload["event"] = "message.outgoing"
        self.payload["data"]["direction"] = "outgoing"
        name = self.ingest()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["status"], "Processed")
        pauses = self._pauses()
        self.assertEqual(len(pauses), 3)
        current = max(pauses, key=lambda row: row["creation"])
        self.assertEqual(current["source"], "ReReplyMobile")
        remaining = (current["paused_until"] - datetime.now()).total_seconds()
        self.assertGreater(remaining, 295)
        self.assertLessEqual(remaining, 300)

    def test_missing_pause_schema_logs_only(self):
        self._enable_pause()
        self.app.pause_fields = {"phone"}
        self.payload["event"] = "message.outgoing"
        self.payload["data"]["direction"] = "outgoing"
        name = self.ingest()
        self.module.process_receipt(name)
        self.assertEqual(self._pauses(), [])
        self.assertEqual(self.receipt(name)["status"], "Processed")

    def test_mobile_and_distinct_staff_media_pause_without_message_rows(self):
        self._enable_pause()
        self.app.db.rows[("WhatsApp Account", "ERP Concierge")]["rereply_integration_user_id"] = (
            "10000000-0000-4000-8000-000000000001"
        )
        for index, (event, media_type) in enumerate((("message.outgoing", "image"), ("message.sent", "audio"))):
            payload = copy.deepcopy(self.payload)
            payload["event"] = event
            payload["data"].update(
                direction="outgoing", message_type=media_type, content=None,
                contact_phone="6012333444%s" % index,
                message_id="media-%s" % index, outbox_event_id="media-event-%s" % index,
                sent_by_user_id="20000000-0000-4000-8000-000000000002",
            )
            name = self.ingest(payload)
            self.module.process_receipt(name)
            self.assertEqual(self.receipt(name)["status"], "Processed")
            self.assertIn("media activity", self.receipt(name)["error"])
        self.assertEqual({row["source"] for row in self._pauses()}, {"ReReplyMobile", "ReReplyStaff"})
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.app.hook_calls, 0)

    def test_integration_user_media_does_not_pause(self):
        self._enable_pause()
        integration_id = "10000000-0000-4000-8000-000000000001"
        self.app.db.rows[("WhatsApp Account", "ERP Concierge")]["rereply_integration_user_id"] = integration_id
        self.payload["event"] = "message.sent"
        self.payload["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.payload["data"].update(direction="outgoing", message_type="image", content=None,
                                     sent_by_user_id=integration_id)
        name = self.ingest()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["status"], "Ignored")
        self.assertEqual(self._pauses(), [])
        self.assertEqual(self.messages(), [])

    def test_known_outbound_echo_reconciles_without_second_document(self):
        self.payload["event"] = "message.sent"
        self.payload["data"].update(direction="outgoing")
        name = self.ingest()
        self.app.db.rows[("WhatsApp Message", "existing-outbound")] = {
            "name": "existing-outbound", "whatsapp_account": "ERP Concierge",
            "rereply_message_id": "provider-message-1", "type": "Outgoing",
        }
        self.app.db.commit()
        self.module.process_receipt(name)
        self.assertEqual(self.receipt(name)["whatsapp_message"], "existing-outbound")
        self.assertEqual(self.app.hook_calls, 0)

    def test_recovery_enqueues_pending_only_and_holds_abandoned_processing(self):
        name = self.ingest()
        self.payload["data"].update(message_id="msg-two", outbox_event_id="event-two")
        stuck = self.ingest()
        self.receipt(stuck).update(status="Processing", modified=datetime.now() - timedelta(hours=1))
        self.app.db.commit()
        self.app.enqueue.reset_mock()
        self.module.recover_pending_webhooks()
        self.assertEqual(self.app.enqueue.call_count, 1)
        self.assertEqual(self.app.enqueue.call_args.kwargs["name"], name)
        self.assertEqual(self.receipt(stuck)["status"], "Failed")


if __name__ == "__main__":
    main()
