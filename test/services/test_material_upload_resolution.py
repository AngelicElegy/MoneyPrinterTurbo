import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services import material_upload, video


def _png(width: int, height: int) -> io.BytesIO:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "black").save(buffer, "PNG")
    buffer.seek(0)
    return buffer


class TestMaterialUploadResolution(unittest.TestCase):
    """上传校验必须和合成阶段用同一个门槛。

    素材库过去只校验「是不是合法图片」，于是 466x831 的截图能顺利入库、被选中，
    用户要等任务跑到素材阶段才看到 "no valid local video materials were found"，
    而真正的原因只是宽少了 4 像素。这条门槛提前到上传入口后，两边必须完全一致，
    否则又会退回「能传进来但一定合成失败」的割裂状态。
    """

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        patcher = patch.object(
            material_upload,
            "uploaded_material_dir",
            return_value=self._directory.name,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stored_files(self):
        return sorted(entry.name for entry in Path(self._directory.name).iterdir())

    def test_minimum_matches_the_synthesis_threshold(self):
        minimum = video.minimum_material_dimension()
        self.assertEqual(
            minimum, video._MIN_MATERIAL_DIMENSION - video._MIN_DIMENSION_TOLERANCE
        )
        self.assertTrue(video.is_material_resolution_acceptable(minimum, minimum))
        self.assertFalse(
            video.is_material_resolution_acceptable(minimum - 1, minimum - 1)
        )

    def test_rejects_portrait_image_with_a_narrow_side(self):
        # 用户实际遇到的那张：466x831，长边足够、短边差 4 像素。
        minimum = video.minimum_material_dimension()
        with self.assertRaises(material_upload.MaterialResolutionError) as caught:
            material_upload.save_material_upload(
                "screenshot.png", _png(minimum - 4, 831)
            )
        # 尺寸和门槛要挂在异常上，界面才能拼出「差多少」的本地化提示。
        self.assertEqual(caught.exception.width, minimum - 4)
        self.assertEqual(caught.exception.height, 831)
        self.assertEqual(caught.exception.minimum, minimum)
        message = str(caught.exception)
        self.assertIn(f"{minimum - 4}x831", message)
        self.assertIn(f"{minimum}x{minimum}", message)
        # 被拒的上传不能留下任何文件。
        self.assertEqual(self._stored_files(), [])

    def test_rejects_landscape_image_with_a_short_side(self):
        minimum = video.minimum_material_dimension()
        with self.assertRaises(material_upload.MaterialResolutionError):
            material_upload.save_material_upload(
                "wide.png", _png(1920, minimum - 1)
            )
        self.assertEqual(self._stored_files(), [])

    def test_resolution_error_stays_a_plain_upload_error(self):
        # HTTP 接口按 MaterialUploadError 捕获并转 400，子类不能破坏这条契约。
        self.assertTrue(
            issubclass(
                material_upload.MaterialResolutionError,
                material_upload.MaterialUploadError,
            )
        )

    def test_accepts_image_exactly_at_the_minimum(self):
        minimum = video.minimum_material_dimension()
        stored = material_upload.save_material_upload(
            "ok.png", _png(minimum, minimum)
        )
        self.assertTrue((Path(self._directory.name) / stored).is_file())


class TestMaterialInspectResolution(unittest.TestCase):
    """inspect_material_resolution 只是量尺寸、不落盘，给 WebUI 即时裁剪入口用。

    它必须和 save_material_upload 共用同一份门槛判断，否则会出现「上传控件说能裁、
    真正生成时却整批丢弃」的割裂。同时它不能留下任何临时文件——只量一下就走。
    """

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        patcher = patch.object(
            material_upload,
            "uploaded_material_dir",
            return_value=self._directory.name,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stored_files(self):
        return sorted(entry.name for entry in Path(self._directory.name).iterdir())

    def test_acceptable_image_reports_full_dimensions(self):
        minimum = video.minimum_material_dimension()
        info = material_upload.inspect_material_resolution(
            "ok.png", _png(1920, 1080)
        )
        self.assertTrue(info["acceptable"])
        self.assertEqual(info["kind"], "image")
        self.assertEqual(info["width"], 1920)
        self.assertEqual(info["height"], 1080)
        self.assertEqual(info["minimum"], minimum)
        self.assertEqual(self._stored_files(), [])

    def test_low_resolution_portrait_image_is_flagged_croppable(self):
        minimum = video.minimum_material_dimension()
        info = material_upload.inspect_material_resolution(
            "screenshot.png", _png(minimum - 4, 831)
        )
        self.assertFalse(info["acceptable"])
        self.assertEqual(info["kind"], "image")
        self.assertEqual(info["width"], minimum - 4)
        self.assertEqual(info["height"], 831)
        self.assertEqual(info["minimum"], minimum)
        # 只量尺寸，绝不能把被拒的图留下来。
        self.assertEqual(self._stored_files(), [])

    def test_low_resolution_video_is_reported_not_croppable(self):
        minimum = video.minimum_material_dimension()
        info = material_upload.inspect_material_resolution(
            "small.png", _png(minimum - 10, minimum - 10)
        )
        self.assertFalse(info["acceptable"])
        self.assertEqual(info["kind"], "image")
        self.assertEqual(self._stored_files(), [])

    def test_unreadable_file_is_not_flagged(self):
        # 探测不到尺寸就交给后续流程，不能误报成「分辨率不足」。
        info = material_upload.inspect_material_resolution(
            "garbage.png", io.BytesIO(b"not an image")
        )
        self.assertTrue(info["acceptable"])
        self.assertEqual(self._stored_files(), [])


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for media fixtures")
class TestMaterialUploadResolutionVideo(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        patcher = patch.object(
            material_upload,
            "uploaded_material_dir",
            return_value=self._directory.name,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        ffmpeg_patcher = patch.object(
            material_upload.utils,
            "get_ffmpeg_binary",
            return_value=shutil.which("ffmpeg"),
        )
        ffmpeg_patcher.start()
        self.addCleanup(ffmpeg_patcher.stop)

    def _clip(self, width: int, height: int) -> io.BytesIO:
        path = Path(self._directory.name) / f"source-{width}x{height}.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={width}x{height}:r=10:d=0.4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return io.BytesIO(path.read_bytes())

    def test_rejects_video_below_the_minimum(self):
        minimum = video.minimum_material_dimension()
        with self.assertRaises(material_upload.MaterialResolutionError):
            material_upload.save_material_upload(
                "small.mp4", self._clip(320, 240)
            )
        self.assertLess(320, minimum)

    def test_accepts_video_above_the_minimum(self):
        stored = material_upload.save_material_upload(
            "ok.mp4", self._clip(640, 480)
        )
        self.assertTrue((Path(self._directory.name) / stored).is_file())


class TestMaterialCrop(unittest.TestCase):
    """裁剪是「分辨率不足」的补救路径，必须真的把素材救到能用。

    用户没法把 466px 宽的截图变宽，只能自己选一段画面。裁剪本身只会让图更小，
    所以低于门槛时必须放大；但放大多少直接决定成片糊不糊，只能放到刚好达标。
    """

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        patcher = patch.object(
            material_upload,
            "uploaded_material_dir",
            return_value=self._directory.name,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _crop(self, width, height, box, name="shot.png"):
        buffer = _png(width, height)
        stored = material_upload.save_material_crop(name, buffer, box)
        return Image.open(Path(self._directory.name) / stored)

    def test_crop_of_a_small_image_is_scaled_up_to_the_threshold(self):
        minimum = video.minimum_material_dimension()
        with self._crop(466, 831, (0, 0, 466, 831)) as image:
            self.assertGreaterEqual(image.width, minimum)
            self.assertGreaterEqual(image.height, minimum)
            # 只放到刚好够，不该顺手放大成 1080×1920 那种倍数。
            self.assertLessEqual(image.width, minimum + 2)
            # 比例保持：831/466 ≈ 1.783
            self.assertAlmostEqual(image.height / image.width, 831 / 466, places=2)

    def test_crop_of_a_large_image_is_not_scaled_at_all(self):
        with self._crop(2000, 3000, (100, 100, 1300, 1900)) as image:
            self.assertEqual(image.size, (1200, 1800))

    def test_crop_area_is_clamped_to_the_image(self):
        with self._crop(800, 600, (-50, -50, 99999, 99999)) as image:
            self.assertEqual(image.size, (800, 600))

    def test_tiny_crop_area_is_rejected(self):
        with self.assertRaises(material_upload.MaterialUploadError):
            material_upload.save_material_crop(
                "shot.png", _png(466, 831), (10, 10, 14, 14)
            )
        self.assertEqual(sorted(os.listdir(self._directory.name)), [])

    def test_cropped_result_passes_the_upload_check(self):
        # 裁完的结果必须能原样再上传一次，否则等于把不合格素材换个名字塞进库。
        stored = material_upload.save_material_crop(
            "shot.png", _png(466, 831), (0, 0, 466, 831)
        )
        path = Path(self._directory.name) / stored
        with patch.object(
            material_upload, "uploaded_material_dir", return_value=path.parent
        ):
            again = material_upload.save_material_upload(
                "reupload.png", io.BytesIO(path.read_bytes())
            )
        self.assertTrue((path.parent / again).is_file())

    def test_jpeg_input_keeps_a_jpeg_output(self):
        buffer = io.BytesIO()
        Image.new("RGB", (2000, 3000), "white").save(buffer, "JPEG")
        buffer.seek(0)
        stored = material_upload.save_material_crop(
            "photo.jpg", buffer, (0, 0, 1200, 1800)
        )
        self.assertTrue(stored.endswith(".jpg"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for media fixtures")
    def test_video_cannot_be_cropped(self):
        source = Path(self._directory.name) / "clip.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
                "-i", "color=c=black:s=320x240:r=10:d=0.3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        with self.assertRaises(material_upload.MaterialUploadError):
            material_upload.save_material_crop(
                "clip.mp4", io.BytesIO(source.read_bytes()), (0, 0, 320, 240)
            )
        self.assertEqual(sorted(entry.name for entry in Path(self._directory.name).iterdir()), ["clip.mp4"])


if __name__ == "__main__":
    unittest.main()
