from __future__ import annotations

import unittest

from app.services.stackai_client import StackAIError
from app.services.upstream_redaction import redact_upstream_data, redact_upstream_text


class UpstreamRedactionTests(unittest.TestCase):
    def assert_upstream_identifiers_are_hidden(self, value: object) -> None:
        text = str(value).lower()
        self.assertNotIn("stackai", text)
        self.assertNotIn("stack-ai", text)
        self.assertNotIn("sb.stack-ai.com", text)
        self.assertNotIn("stackai.com", text)

    def test_redact_upstream_text_replaces_names_and_domains(self) -> None:
        source = (
            "StackAI request failed at https://sb.stack-ai.com/path; "
            "retry stackai.com or stack-ai."
        )

        redacted = redact_upstream_text(source)

        self.assert_upstream_identifiers_are_hidden(redacted)
        self.assertIn("st request failed", redacted)
        self.assertIn("https://st/path", redacted)

    def test_redact_upstream_data_recurses_without_mutating_input(self) -> None:
        source = {
            "message": "stackai error",
            "nested": ["StackAI", {"url": "https://api.stack-ai.com/run"}],
        }

        redacted = redact_upstream_data(source)

        self.assertEqual(source["message"], "stackai error")
        self.assert_upstream_identifiers_are_hidden(redacted)
        self.assertEqual(redacted["nested"][1]["url"], "https://st/run")

    def test_stackai_error_redacts_public_message_and_payload(self) -> None:
        error = StackAIError(
            "StackAI HTTP 502 from https://stackai.com",
            status_code=502,
            payload={"detail": "https://sb.stack-ai.com/secret"},
        )

        self.assert_upstream_identifiers_are_hidden(error.message)
        self.assert_upstream_identifiers_are_hidden(error.payload)
