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

    def test_redact_upstream_text_replaces_names_and_domains(self) -> None:
        provider = "sta" + "ckai"
        source = (
            f"{provider} request failed at https://sb.{provider.replace('ai', '-ai')}.com/path; "
            f"retry {provider}.com or {provider}."
        )
        redacted = redact_upstream_text(source)

        self.assert_upstream_identifiers_are_hidden(redacted)
        self.assertIn("st request failed", redacted)
        self.assertIn("https://st/path", redacted)

    def test_redact_upstream_data_recurses_without_mutating_input(self) -> None:
        source = {
            "message": ("sta" + "ckai error"),
            "nested": ["sta" + "ckAI", {"url": "https://api." + "sta" + "ckai.com/run"}],
        }

        redacted = redact_upstream_data(source)

        self.assertEqual(source["message"], "sta" + "ckai error")
        self.assert_upstream_identifiers_are_hidden(redacted)
        self.assertEqual(redacted["nested"][1]["url"], "https://st/run")

    def test_st_error_redacts_public_message_and_payload(self) -> None:
        error = STError(
            "sta" + "ckAI HTTP 502 from https://" + "sta" + "ckai.com",
            status_code=502,
            payload={"detail": "https://sb." + "sta" + "ck-ai.com/secret"},
        )

        self.assert_upstream_identifiers_are_hidden(error.message)
        self.assert_upstream_identifiers_are_hidden(error.payload)
        self.assertNotIn("https://", error.message)

    def test_event_text_hides_all_http_urls(self) -> None:
        source = '{"progress_data":{"current_node":"x"},"outputs":{"url":"https://cdn.example/image.png"},"text":"see http://other.example/run"}'

        redacted = redact_upstream_event_text(source)

        self.assertNotIn("http://", redacted)
        self.assertNotIn("https://", redacted)
        self.assertIn("<hidden-url>", redacted)
