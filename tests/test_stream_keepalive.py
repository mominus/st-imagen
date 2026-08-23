from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from app.routers import generate as generate_mod
from app.services.stackai_client import StackAIError


class StreamKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_text2img_4k_gets_relaxed_idle_timeout(self) -> None:
        req = generate_mod.GenerateRequest(
            prompt="museum crown infographic",
            model="Nano Banana Pro",
            resolution="4K",
            mode="text2img",
        )

        timeout_seconds = generate_mod._stream_idle_timeout_seconds_for_request(req)

        self.assertEqual(timeout_seconds, generate_mod.GENERATE_STREAM_TEXT2IMG_4K_IDLE_TIMEOUT_SECONDS)
        self.assertGreaterEqual(timeout_seconds, generate_mod.GENERATE_STREAM_IDLE_TIMEOUT_SECONDS)

    async def test_gpt_image_2_gets_relaxed_idle_timeout(self) -> None:
        req = generate_mod.GenerateRequest(
            prompt="high quality image",
            model=generate_mod.GPT_IMAGE_2_MODEL,
            size="3840x2160",
            quality="high",
            mode="text2img",
        )

        timeout_seconds = generate_mod._stream_idle_timeout_seconds_for_request(req)

        self.assertEqual(timeout_seconds, generate_mod.GENERATE_STREAM_GPT_IMAGE_2_IDLE_TIMEOUT_SECONDS)
        self.assertGreaterEqual(timeout_seconds, generate_mod.GENERATE_STREAM_IDLE_TIMEOUT_SECONDS)

    async def test_waiter_emits_keepalive_before_slow_first_event(self) -> None:
        async def upstream():
            await asyncio.sleep(0.025)
            yield '{"progress_data":{"total_nodes":1,"started_nodes":1}}'

        with (
            patch.object(generate_mod, "GENERATE_STREAM_KEEPALIVE_INTERVAL_SECONDS", 0.01),
            patch.object(generate_mod, "GENERATE_STREAM_TOTAL_TIMEOUT_SECONDS", 0.2),
        ):
            events = []
            async for item in generate_mod._iter_upstream_line_with_keepalive(
                upstream(),
                request_started_monotonic=time.monotonic(),
                idle_timeout_seconds=0.05,
            ):
                events.append(item)

        self.assertGreaterEqual(len(events), 2)
        self.assertIn(None, events[:-1])
        self.assertEqual(events[-1], '{"progress_data":{"total_nodes":1,"started_nodes":1}}')

    async def test_waiter_raises_idle_timeout_after_budget(self) -> None:
        async def upstream():
            await asyncio.sleep(0.08)
            yield '{"late": true}'

        with (
            patch.object(generate_mod, "GENERATE_STREAM_KEEPALIVE_INTERVAL_SECONDS", 0.01),
            patch.object(generate_mod, "GENERATE_STREAM_TOTAL_TIMEOUT_SECONDS", 0.2),
        ):
            agen = generate_mod._iter_upstream_line_with_keepalive(
                upstream(),
                request_started_monotonic=time.monotonic(),
                idle_timeout_seconds=0.03,
            )
            items = []
            with self.assertRaises(StackAIError) as ctx:
                async for item in agen:
                    items.append(item)

        self.assertTrue(items, "idle timeout path should emit at least one keepalive tick before failing")
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertEqual(ctx.exception.payload.get("reason"), "idle_timeout")
