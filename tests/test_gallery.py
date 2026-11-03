"""目标图库接口：隔离数据库、真实文件和采样帧坐标。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.api.gallery import _crop_jpeg
from app.db import dao, database
from app.main import app
from config import settings


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gallery_test_")
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.previous_conn = getattr(database._local, "conn", None)
        database._local.conn = None
        self.patches = [patch.object(settings, "db_path", self.root / "test.db"),
                        patch.object(settings, "upload_dir", self.uploads)]
        for item in self.patches:
            item.start()
        with patch.object(database, "_register_exit_hook"):
            database.init_db()
        # HTTP 处理发生在线程池中；共用隔离连接，确保 Windows 清理临时数据库时句柄已关闭。
        gallery_connection = patch("app.api.gallery.get_connection", return_value=database.get_connection())
        gallery_connection.start()
        self.patches.append(gallery_connection)
        self.client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
        image = np.zeros((80, 100, 3), dtype=np.uint8)
        image[10:50, 20:60] = (0, 0, 255)
        success, encoded = cv2.imencode(".png", image)
        self.assertTrue(success)
        (self.uploads / "source.png").write_bytes(encoded.tobytes())
        self.image_id = self.save("image", "source.png", "2026-10-01 12:00:00", [
            {"class_id": 0, "class_name": "海参", "confidence": 0.91, "bbox": [20, 10, 60, 50]},
            {"class_id": 1, "class_name": "海胆", "confidence": 0.43, "bbox": [0, 0, 10, 10]},
        ])

    def tearDown(self):
        self.client.close()
        _crop_jpeg.cache_clear()
        database.close_connection()
        database._local.conn = self.previous_conn
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def save(self, kind, name, date, items, snapshot=None):
        return dao.save_detection({
            "task_type": kind, "source_name": name, "saved_name": name,
            "created_at": date, "status": "success", "total_objects": len(items),
            "inference_snapshot": snapshot or {},
        }, items, [])

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_list_filters_pagination_order_and_source(self):
        self.save("video", "clip.mp4", "2026-10-02 12:00:00", [
            {"class_id": 0, "class_name": "海参", "confidence": 0.7,
             "bbox": [1, 2, 3, 4], "frame_index": 12},
        ])
        data = self.get("/api/gallery?page_size=1")
        self.assertEqual((data["total"], data["total_pages"]), (3, 3))
        self.assertEqual(data["items"][0]["task_type"], "video")
        data = self.get("/api/gallery?source=image&min_confidence=0.5&category=海参")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["detection_id"], self.image_id)
        self.assertEqual(self.get("/api/gallery?keyword=absent")["total"], 0)
        self.assertEqual(self.get("/api/gallery?order=confidence")["items"][0]["confidence"], 0.91)
        self.assertEqual(self.client.get("/api/gallery?source=invalid").status_code, 422)

    def test_image_thumbnail_crops_original_and_missing_file_is_404(self):
        item = self.get("/api/gallery?category=海参")["items"][0]
        response = self.client.get(f"/api/gallery/items/{item['id']}/thumbnail")
        self.assertEqual(response.status_code, 200)
        crop = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(crop.shape[:2], (40, 40))
        self.assertGreater(float(crop[:, :, 2].mean()), 240)
        (self.uploads / "source.png").unlink()
        self.assertEqual(self.client.get(f"/api/gallery/items/{item['id']}/thumbnail").status_code, 404)

    def test_video_thumbnail_uses_original_frame_and_saved_resize(self):
        path = self.uploads / "视频.avi"
        # 写入 ASCII 名称再重命名，测试 Windows 上的中文素材名读取。
        ascii_path = self.uploads / "clip.avi"
        writer = cv2.VideoWriter(str(ascii_path), cv2.VideoWriter_fourcc(*"MJPG"), 5, (200, 100))
        self.assertTrue(writer.isOpened())
        for _ in range(3):
            frame = np.zeros((100, 200, 3), dtype=np.uint8)
            frame[10:50, 20:60] = (0, 0, 255)
            writer.write(frame)
        writer.release()
        ascii_path.rename(path)
        self.save("video", path.name, "2026-10-02 12:00:00", [{
            "class_id": 0, "class_name": "海参", "confidence": 0.8,
            "bbox": [10, 5, 30, 25], "frame_index": 1,
        }], {"parameters": {"video_max_side": 100}})
        item = self.get("/api/gallery?source=video")["items"][0]
        response = self.client.get(f"/api/gallery/items/{item['id']}/thumbnail")
        self.assertEqual(response.status_code, 200, response.text)
        crop = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(crop.shape[:2], (20, 20))
        self.assertGreater(float(crop[:, :, 2].mean()), 200)

    def test_video_without_original_resize_metadata_is_not_miscropped(self):
        (self.uploads / "old.mp4").write_bytes(b"placeholder")
        self.save("video", "old.mp4", "2026-10-02 12:00:00", [{
            "class_id": 0, "class_name": "海参", "confidence": 0.8, "bbox": [1, 2, 3, 4],
        }])
        item = self.get("/api/gallery?source=video")["items"][0]
        self.assertEqual(self.client.get(f"/api/gallery/items/{item['id']}/thumbnail").status_code, 404)


if __name__ == "__main__":
    unittest.main()
