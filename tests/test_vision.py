import asyncio
import base64
import io
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from starlette.datastructures import Headers

from app import llm, main, vision
from app.config import Config


class VisionValidationTests(unittest.TestCase):
    def test_accepts_jpeg_signature(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(image, format="JPEG")
        self.assertEqual(
            vision.validate_image("image/jpeg", image.getvalue()),
            "image/jpeg",
        )

    def test_rejects_mismatched_file_signature(self):
        with self.assertRaisesRegex(ValueError, "不匹配"):
            vision.validate_image("image/png", b"not-a-png")

    def test_rejects_file_with_image_header_but_invalid_structure(self):
        with self.assertRaisesRegex(ValueError, "无法解码"):
            vision.validate_image("image/jpeg", b"\xff\xd8\xfffake")

    def test_rejects_oversized_image(self):
        with patch.object(Config, "MAX_IMAGE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "不能超过"):
                vision.validate_image("image/jpeg", b"\xff\xd8\xff12")


class VisionModelTests(unittest.TestCase):
    def test_complete_vision_sends_data_uri_and_uses_vision_config(self):
        response = {"choices": [{"message": {"content": "识别结果"}}]}
        with patch.object(Config, "VISION_MODEL", "vision-demo"), \
             patch.object(llm, "_call_api", return_value=response) as call_api:
            result = llm.complete_vision(
                "system", "请识别", b"\xff\xd8\xfffake", "image/jpeg"
            )

        self.assertEqual(result, "识别结果")
        messages = call_api.call_args.args[0]
        image_url = messages[1]["content"][1]["image_url"]["url"]
        self.assertEqual(
            image_url,
            "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xfffake").decode(),
        )
        self.assertEqual(call_api.call_args.kwargs["model"], "vision-demo")

    def test_complete_vision_requires_model_configuration(self):
        with patch.object(Config, "VISION_MODEL", ""):
            with self.assertRaisesRegex(RuntimeError, "VISION_MODEL"):
                llm.complete_vision("system", "请识别", b"data", "image/jpeg")


class UploadShapeTests(unittest.TestCase):
    def test_upload_file_can_be_constructed_for_endpoint_tests(self):
        upload = UploadFile(file=io.BytesIO(b"data"), filename="test.jpg")
        self.assertEqual(upload.filename, "test.jpg")

    def test_text_is_preserved_when_vision_fails(self):
        from PIL import Image

        image_data = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(image_data, format="JPEG")
        upload = UploadFile(
            file=io.BytesIO(image_data.getvalue()),
            filename="test.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
        record = {"status": "auto", "reply_source": "agent", "reply": "已收到"}
        with patch.object(main, "_continue_human_handoff", return_value=None), \
             patch.object(main.vision, "analyze", side_effect=RuntimeError("offline")), \
             patch.object(main.router, "process_ticket", return_value=record) as process, \
             patch.object(main.router, "save_processed", return_value=9), \
             patch.object(main.db, "insert_ticket_message"), \
             patch.object(main.db, "insert_ticket_log"):
            result = asyncio.run(
                main.create_multimodal_ticket(
                    text="商品有划痕，请帮我看看",
                    image=upload,
                    username="user",
                )
            )

        self.assertEqual(process.call_args.args[0], "商品有划痕，请帮我看看")
        self.assertEqual(result["image_analysis_status"], "unavailable")
        self.assertEqual(result["id"], 9)


if __name__ == "__main__":
    unittest.main()
