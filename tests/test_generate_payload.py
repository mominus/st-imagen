from __future__ import annotations

import unittest

from app.routers import generate as generate_mod


class GeneratePayloadTests(unittest.TestCase):
    def test_prompt_accepts_up_to_eight_thousand_characters(self) -> None:
        req = generate_mod.GenerateRequest(prompt="画" * 8000, model="Nano Banana Pro")
        self.assertEqual(len(req.prompt), 8000)
        with self.assertRaises(ValueError):
            generate_mod.GenerateRequest(prompt="画" * 8001, model="Nano Banana Pro")

    def test_text2img_nano_banana_pro_uses_aspect_ratio_and_resolution(self) -> None:
        req = generate_mod.GenerateRequest(
            prompt="a red apple on a white table",
            model="Nano Banana Pro",
            aspect_ratio="16:9",
            resolution="4K",
        )

        self.assertEqual(
            generate_mod._build_payload(req),
            {
                "in-0": "a red apple on a white table",
                "in-1": "16:9",
                "in-2": "4K",
                "in-3": "Nano Banana Pro",
                "in-4": "",
                "in-5": "",
                "in-6": "",
            },
        )

    def test_text2img_gpt_image_2_uses_size_and_quality(self) -> None:
        req = generate_mod.GenerateRequest(
            prompt="a cinematic mountain landscape",
            model=generate_mod.GPT_IMAGE_2_MODEL,
            size="3840x2160",
            quality="high",
        )

        self.assertEqual(
            generate_mod._build_payload(req),
            {
                "in-0": "a cinematic mountain landscape",
                "in-1": "",
                "in-2": "",
                "in-3": "GPT Image 2",
                "in-4": "3840x2160",
                "in-5": "high",
                "in-6": "",
            },
        )

    def test_img2img_uses_new_nano_banana_pro_model_and_reference_field(self) -> None:
        req = generate_mod.GenerateRequest(
            prompt="turn this into a watercolor painting",
            model="gemini-3-pro-image-preview",
            mode="img2img",
            image_urls=["https://example.com/reference.png"],
        )

        self.assertEqual(
            generate_mod._build_payload(req),
            {
                "in-0": "turn this into a watercolor painting",
                "in-1": generate_mod.IMG2IMG_DEFAULT_ASPECT_RATIO,
                "in-2": generate_mod.IMG2IMG_DEFAULT_RESOLUTION,
                "in-3": "gemini-3-pro-image-preview",
                "in-4": "",
                "in-5": "",
                "in-6": "https://example.com/reference.png",
            },
        )

        self.assertEqual(generate_mod.IMG2IMG_DEFAULT_RESOLUTION, "1K")
        self.assertEqual(generate_mod._generation_log_dimensions(req), ("", "1K"))

    def test_default_model_and_parameter_options_match_supported_models(self) -> None:
        self.assertIn(generate_mod.GPT_IMAGE_2_MODEL, generate_mod.TEXT2IMG_MODELS)
        # sizes 的 value 是提交/in-4 传参用的标准参数；label 只用于前端展示
        self.assertEqual(
            [item["value"] for item in generate_mod.GPT_IMAGE_2_SIZES],
            ["auto", "1024x1024", "1536x1024", "1024x1536", "2048x2048", "3840x2160"],
        )
        for item in generate_mod.GPT_IMAGE_2_SIZES:
            self.assertTrue(item["label"])
            self.assertNotIn("(", item["value"])
        self.assertEqual(
            generate_mod.DEFAULT_ASPECT_RATIOS,
            ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
        )
        self.assertIn(
            {"label": "Nano Banana Pro", "value": "gemini-3-pro-image-preview"},
            generate_mod.IMG2IMG_MODELS,
        )
        self.assertIn(
            {"label": "gpt-image-1.5", "value": "gpt-image-1.5"},
            generate_mod.IMG2IMG_MODELS,
        )
        self.assertNotIn("flux-kontext-max", {item["value"] for item in generate_mod.IMG2IMG_MODELS})
