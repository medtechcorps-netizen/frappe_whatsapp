"""Security helpers for authenticating Meta webhook requests."""

import hashlib
import hmac
import re


META_SIGNATURE_PREFIX = "sha256="
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def meta_signature(app_secret, raw_body):
	"""Return Meta's X-Hub-Signature-256 value for exact request bytes."""
	if not isinstance(raw_body, bytes):
		raise TypeError("raw_body must be bytes")
	if not isinstance(app_secret, str) or not app_secret:
		raise ValueError("app_secret is required")

	digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
	return f"{META_SIGNATURE_PREFIX}{digest}"


def verify_meta_signature(app_secret, raw_body, signature_header):
	"""Verify a Meta signature without exposing timing differences."""
	if not isinstance(raw_body, bytes):
		return False
	if not isinstance(app_secret, str) or not app_secret:
		return False
	if not isinstance(signature_header, str):
		return False

	provided = signature_header.strip()
	if not provided.startswith(META_SIGNATURE_PREFIX):
		return False

	digest = provided[len(META_SIGNATURE_PREFIX):]
	if not _SHA256_HEX.fullmatch(digest):
		return False

	expected = meta_signature(app_secret, raw_body)
	return hmac.compare_digest(expected, f"{META_SIGNATURE_PREFIX}{digest.lower()}")
