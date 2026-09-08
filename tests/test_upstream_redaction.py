from __future__ import annotations

import unittest

from app.services.st_client import STError
from app.services.upstream_redaction import (
    redact_upstream_data,
    redact_upstream_event_text,
    redact_upstream_text,
)


class UpstreamRedactionTests(unittest.TestCase):
    def assert_upstream_identifiers_are_hidden(self, value: object) -> None:
        text = str(value).lower()
        provider = "sta" + "ckai"
        self.assertNotIn(provider, text)
        self.assertNotIn(provider.replace("ai", "-ai"), text)
        self.assertNotIn("support@" + provider + ".com", text)

    def test_redact_upstream_text_replaces_names_and_domains(self) -> None:
        provider = "sta" + "ckai"
        source = (
            f"{provider} request failed at https://sb.{provider.replace('ai', '-ai')}.com/path; "
            f"retry {provider}.com or {provider}."
        )
        redacted = redact_upstream_text(source)

        self.assert_upstream_identifiers_are_hidden(redacted)
        self.assertIn("Upstream request failed", redacted)
        self.assertIn("https://Upstream/path", redacted)

    def test_redact_upstream_data_recurses_without_mutating_input(self) -> None:
        source = {
            "message": ("sta" + "ckai error"),
            ("sta" + "ckai_source"): "hidden key",
            "nested": ["sta" + "ckAI", {"url": "https://api." + "sta" + "ckai.com/run"}],
        }

        redacted = redact_upstream_data(source)

        self.assertEqual(source["message"], "sta" + "ckai error")
        self.assert_upstream_identifiers_are_hidden(redacted)
        self.assertEqual(redacted["nested"][1]["url"], "https://Upstream/run")

    def test_redacts_short_name_and_support_addresses_case_insensitively(self) -> None:
        provider = "sta" + "ckai"
        source = f"ST error; contact SUPPORT@{provider}.COM or support@{provider.replace('ai', '-ai')}.com"
        redacted = redact_upstream_text(source)
        self.assertEqual(redacted, "Upstream error; contact Upstream or Upstream")

    def test_st_error_redacts_public_message_and_payload(self) -> None:
        error = STError(
            "sta" + "ckAI HTTP 502 from https://" + "sta" + "ckai.com",
            status_code=502,
            payload={"detail": "https://sb." + "sta" + "ck-ai.com/secret"},
        )

        self.assert_upstream_identifiers_are_hidden(error.message)
        self.assert_upstream_identifiers_are_hidden(error.payload)
        self.assertEqual(error.message, "Upstream HTTP 502 from https://Upstream")

    def test_event_text_preserves_non_provider_urls(self) -> None:
        source = '{"progress_data":{"current_node":"x"},"outputs":{"url":"https://cdn.example/image.png"},"text":"see http://other.example/run"}'

        redacted = redact_upstream_event_text(source)

        self.assertIn("https://cdn.example/image.png", redacted)
        self.assertIn("http://other.example/run", redacted)
        self.assertNotIn("hidden-url", redacted)

    def test_event_text_strips_node_prefix_and_keeps_complete_error(self) -> None:
        source = (
            "Error in Node **Image to Image** (`action-1`): Network or HTTP error: "
            "Server error '503 Service Unavailable' for url "
            "'https://generativelanguage.googleapis.com/v1beta/models/example'\n"
            "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503"
        )

        redacted = redact_upstream_event_text(source)

        self.assertNotIn("Error in Node", redacted)
        self.assertTrue(redacted.startswith("Network or HTTP error:"))
        self.assertIn("https://generativelanguage.googleapis.com/", redacted)
        self.assertIn("https://developer.mozilla.org/", redacted)

    def test_event_text_replaces_provider_domains_and_email_without_hiding_urls(self) -> None:
        provider = "sta" + "ckai"
        source = (
            f"See https://api.{provider}.com/run and https://sb.{provider.replace('ai', '-ai')}.com/x; "
            f"contact support@{provider}.com or support@{provider.replace('ai', '-ai')}.com"
        )

        redacted = redact_upstream_event_text(source)

        self.assert_upstream_identifiers_are_hidden(redacted)
        self.assertEqual(
            redacted,
            "See https://Upstream/run and https://Upstream/x; contact Upstream or Upstream",
        )
