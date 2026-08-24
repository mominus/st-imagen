from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.failure_classifier import (
    ACCOUNT_SCOPE,
    ROUTE_CONFIG_SCOPE,
    UPSTREAM_ROUTE_SCOPE,
    classify_stackai_error,
)
from app.services.guard import (
    RouteCircuitBreaker,
    GenerationAdmission,
)
from app.services.stackai_client import StackAIClient, StackAIError
from app.services.account_pool import AccountPoolService
from app.routers import generate as generate_mod


class FailureClassificationTests(unittest.TestCase):
    def test_public_throttle_is_route_scoped(self) -> None:
        decision = classify_stackai_error(StackAIError("rate limited", 429))
        self.assertEqual(decision.scope, UPSTREAM_ROUTE_SCOPE)
        self.assertFalse(decision.isolate_account)
        self.assertFalse(decision.failover)

    def test_explicit_key_error_is_account_scoped(self) -> None:
        decision = classify_stackai_error(StackAIError("invalid API key", 401))
        self.assertEqual(decision.scope, ACCOUNT_SCOPE)
        self.assertTrue(decision.isolate_account)
        self.assertTrue(decision.failover)

    def test_route_configuration_error_is_local_to_selected_account(self) -> None:
        decision = classify_stackai_error(StackAIError("flow not found", 404))
        self.assertEqual(decision.scope, ROUTE_CONFIG_SCOPE)
        self.assertTrue(decision.isolate_account)


class RouteBreakerTests(unittest.TestCase):
    def test_sliding_window_and_single_half_open_probe(self) -> None:
        breaker = RouteCircuitBreaker(
            window_seconds=60,
            min_samples=3,
            failure_rate=0.6,
            open_seconds=10,
        )
        breaker.record(False, now=1)
        breaker.record(False, now=2)
        breaker.record(True, now=3)
        self.assertFalse(breaker.allow(now=4)[0])
        self.assertTrue(breaker.allow(now=14)[0])
        self.assertFalse(breaker.allow(now=14.1)[0])
        breaker.record(True, now=14.2)
        self.assertTrue(breaker.allow(now=15)[0])


class SharedClientMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_keeps_retry_after_and_request_id(self) -> None:
        class FakeClient:
            async def post(self, *args, **kwargs):
                import httpx

                return httpx.Response(
                    429,
                    headers={"Retry-After": "7", "X-Request-ID": "req-123"},
                    json={"detail": "rate limited"},
                )

        client = StackAIClient(base_url="https://example.test")
        with patch.object(client, "_get_client", return_value=FakeClient()):
            with self.assertRaises(StackAIError) as caught:
                await client.run_inference("org", "flow", "key", {"in-0": "p"})
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, 7.0)
        self.assertEqual(caught.exception.request_id, "req-123")
        self.assertEqual(caught.exception.payload["detail"], "rate limited")


class GenerationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixty_slots_admit_and_sixty_first_is_rejected_immediately(self) -> None:
        admission = GenerationAdmission(total_slots=60)
        results = await asyncio.gather(
            *(admission.try_acquire("Nano Banana Pro") for _ in range(61))
        )

        self.assertEqual(sum(results), 60)
        self.assertEqual(admission.snapshot()["in_flight"], 60)
        for admitted in results:
            if admitted:
                admission.release("Nano Banana Pro")
        self.assertEqual(admission.snapshot()["in_flight"], 0)

    async def test_try_acquire_rejects_immediately_when_full(self) -> None:
        admission = GenerationAdmission(total_slots=1)
        self.assertTrue(await admission.try_acquire("Nano Banana Pro"))

        started = asyncio.get_running_loop().time()
        self.assertFalse(await admission.try_acquire("GPT Image 2"))
        elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 0.05)
        self.assertEqual(admission.snapshot()["rejected"], 1)
        admission.release("Nano Banana Pro")

    async def test_admission_is_immediate_when_full(self) -> None:
        admission = GenerationAdmission(total_slots=1)
        self.assertTrue(await admission.try_acquire("Nano Banana Pro"))
        self.assertFalse(await admission.try_acquire("Nano Banana Pro"))
        admission.release("Nano Banana Pro")

    async def test_shared_capacity_can_be_used_by_one_model(self) -> None:
        admission = GenerationAdmission(total_slots=3)
        self.assertTrue(await admission.try_acquire("GPT Image 2"))
        self.assertTrue(await admission.try_acquire("GPT Image 2"))
        self.assertTrue(await admission.try_acquire("GPT Image 2"))
        snapshot = admission.snapshot()
        self.assertEqual(snapshot["total_slots"], 3)
        self.assertEqual(snapshot["in_flight"], 3)
        self.assertEqual(snapshot["models"]["GPT Image 2"]["in_flight"], 3)
        self.assertFalse(await admission.try_acquire("GPT Image 2"))
        admission.release("GPT Image 2")
        admission.release("GPT Image 2")
        admission.release("GPT Image 2")
        self.assertEqual(admission.snapshot()["in_flight"], 0)


class RuntimeTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_runtime_release_is_idempotent(self) -> None:
        pool = AccountPoolService()
        pool._runtime_in_flight["acc-1"] = 1
        pool._runtime_tokens["token-1"] = "acc-1"

        await pool.release_account(None, "acc-1", slot_token="token-1")
        await pool.release_account(None, "acc-1", slot_token="token-1")

        self.assertEqual(pool.runtime_in_flight("acc-1"), 0)


class ImagePersistenceSnapshotTests(unittest.TestCase):
    def test_idle_snapshot_exposes_async_history_status(self) -> None:
        snapshot = generate_mod.image_persistence_snapshot()
        self.assertTrue(snapshot["history_log_async"])
        self.assertIn("started", snapshot)
        self.assertIn("active_tasks", snapshot)


class ImagePersistenceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_returns_only_local_urls_after_download(self) -> None:
        class FakeSession:
            def add(self, _item):
                return None

            async def commit(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _tb):
                return False

        job = {
            "generation_id": "test-generation",
            "source_images": ["https://upstream.example/image.png"],
            "public_base_url": "https://local.example/",
            "user_id": None,
            "account_id": "account",
            "mode": "text2img",
            "model": "Nano Banana Pro",
            "aspect_ratio": "1:1",
            "resolution": "2K",
            "prompt_preview": "test",
            "reference_url": None,
            "response_time_ms": 1,
            "is_stream": True,
        }
        with (
            patch.object(
                generate_mod,
                "_save_generated_images",
                new=AsyncMock(return_value=["https://local.example/uploads/generated/test.png"]),
            ),
            patch(
                "app.models.database.get_session_factory",
                return_value=lambda: FakeSession(),
            ),
        ):
            try:
                result = await generate_mod._save_and_log_images(job)
                self.assertEqual(result, ["https://local.example/uploads/generated/test.png"])
                self.assertNotIn("upstream.example", result[0])
            finally:
                await generate_mod._stop_image_persistence()


class StreamingImageSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_download_streams_to_temp_file_and_atomically_publishes(self) -> None:
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "image/png"}

            async def aiter_bytes(self):
                yield b"\x89PNG\r\n\x1a\n" + (b"x" * 32)

            async def aclose(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            with (
                patch.object(generate_mod, "REFERENCE_UPLOAD_DIR", root),
                patch.object(generate_mod, "GENERATED_IMAGE_DIR", generated),
                patch.object(generate_mod, "GENERATED_IMAGE_MIN_FREE_BYTES", 0),
                patch.object(generate_mod, "GENERATED_IMAGE_MAX_BYTES", 1024),
                patch.object(generate_mod, "GENERATED_IMAGE_DOWNLOAD_ATTEMPTS", 1),
                patch.object(generate_mod, "ensure_safe_outbound_url", new=AsyncMock()),
                patch.object(generate_mod, "get_downloads_client", new=AsyncMock(return_value=object())),
                patch.object(
                    generate_mod,
                    "open_safe_stream",
                    new=AsyncMock(return_value=FakeResponse()),
                ),
                patch.object(generate_mod, "_maybe_cleanup_uploads", new=AsyncMock()),
            ):
                result = await generate_mod._save_generated_image(
                    None,
                    "https://cdn.example/image.png",
                    public_base_url="https://local.example/",
                )

            self.assertTrue(result.startswith("https://local.example/uploads/generated/"))
            files = list(generated.iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertFalse(any(path.name.startswith(".gen-") for path in files))
