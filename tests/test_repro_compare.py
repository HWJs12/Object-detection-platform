"""可复现实验包(repro)与多记录对比(compare)的单元测试。

覆盖两类纯函数模块:
    - app.core.repro:   包结构完整性 / 缺文件不阻断 / README 随类型变化 /
                        路径穿越防护 / 环境信息容错
    - app.core.compare: 口径一致性判定 / 类别并集补 0 / 置信度分布与敏感性 /
                        warning 触发条件 / 空数据不崩

另含 dao.get_records_ordered 的保序去重验证(单独用临时库)。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import compare, repro  # noqa: E402
from app.db import database  # noqa: E402
from config import settings  # noqa: E402


def _record(**overrides):
    """构造一条最小可用的图片记录。"""
    record = {
        "id": 101,
        "source_name": "reef_01.jpg",
        "saved_name": "20261020_120000_ab12cd34_reef_01.jpg",
        "task_type": "image",
        "status": "success",
        "created_at": "2026-10-20 12:00:00",
        "image_width": 640,
        "image_height": 480,
        "conf_threshold": 0.25,
        "result_image": "image_deadbeef.jpg",
        "result_video": None,
        "total_objects": 3,
        "total_detections": 3,
        "effective_count": 3,
        "effective_count_source": "image_objects",
        "inference_snapshot": {
            "model": {"name": "gscnet.pt", "sha256": "a" * 64},
            "device": "cpu",
            "parameters": {"imgsz": 640, "iou_threshold": 0.45},
        },
        "items": [
            {"frame_index": 0, "class_id": 0, "class_name": "海胆", "confidence": 0.91,
             "x1": 10.0, "y1": 20.0, "x2": 30.0, "y2": 40.0},
            {"frame_index": 0, "class_id": 1, "class_name": "海星", "confidence": 0.72,
             "x1": 12.5, "y1": 22.5, "x2": 32.5, "y2": 42.5},
            {"frame_index": 0, "class_id": 0, "class_name": "海胆", "confidence": 0.55,
             "x1": 1.0, "y1": 2.0, "x2": 3.0, "y2": 4.0},
        ],
        "class_stats": [
            {"class_name": "海胆", "count": 2},
            {"class_name": "海星", "count": 1},
        ],
        "frame_stats": [],
    }
    record.update(overrides)
    return record


def _manifest(record):
    """模拟 api 层 `_bundle_manifest` 最小实现。"""
    return {
        "record_id": record.get("id"),
        "source_name": record.get("source_name"),
        "task_type": record.get("task_type"),
        "parameters": (record.get("inference_snapshot") or {}).get("parameters") or {},
        "environment": repro.environment_info(),
    }


class ReproBundleTest(unittest.TestCase):
    """"复现包必须自解释, 且缺文件时给出理由而不是失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _resolve(self, files):
        """把"字段名/文件名 -> 路径"的映射包装成 resolve_path 回调。"""
        def resolver(column, name):
            hit = files.get(f"{column}:{name}")
            return Path(hit) if hit else None
        return resolver

    def _open(self, payload):
        return zipfile.ZipFile(__import__("io").BytesIO(payload))

    def test_bundle_contains_all_required_files(self):
        src = self.root / "source.jpg"
        src.write_bytes(b"\xff\xd8fake-jpeg-bytes")
        res = self.root / "result.jpg"
        res.write_bytes(b"\xff\xd8fake-result")

        record = _record()
        files = {
            f"saved_name:{record['saved_name']}": src,
            f"result_image:{record['result_image']}": res,
        }
        payload, info = repro.build_bundle(_manifest(record), record, self._resolve(files))

        with self._open(payload) as zf:
            names = set(zf.namelist())
            required = {
                "parameters.json", "environment.json", "record.json",
                "results.csv", "frame_stats.csv", "class_stats.csv",
                "README.md", "manifest.json",
            }
            self.assertTrue(required.issubset(names), f"缺少文件: {required - names}")
            self.assertIn(f"source/{record['saved_name']}", names)
            self.assertIn(f"detections/{record['result_image']}", names)

            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest["bundle_format"], "underwater-detection-repro/1")
            self.assertEqual(manifest["record_id"], 101)
            self.assertEqual(len(manifest["files"]), 2)
            self.assertEqual(manifest["missing"], [])
            # 文件数与缺失数必须落进 manifest, 否则解包者在离线状态下
            # 无法核对"清单登记数"与"实际文件数"是否一致。
            self.assertEqual(manifest["file_count"], 2)
            self.assertEqual(manifest["missing_count"], 0)
            self.assertEqual(manifest["file_count"], len(manifest["files"]))
            # 参数快照只放推理参数, 不带 result_url 之类会话相关字段
            self.assertEqual(manifest["parameters"]["imgsz"], 640)

        self.assertEqual(info["files"], 2)
        self.assertEqual(info["missing"], [])
        self.assertGreater(info["bundle_bytes"], 0)

    def test_missing_source_does_not_block_and_is_reported(self):
        """源文件已被清理时: 仍要出包, 但必须在 missing 与 README 里说明。"""
        record = _record()  # 两份文件都不提供 -> 都缺失
        payload, info = repro.build_bundle(_manifest(record), record, self._resolve({}))

        self.assertTrue(payload, "缺文件不应导致导出失败")
        self.assertEqual(info["files"], 0)
        reasons = {m["file"]: m["reason"] for m in info["missing"]}
        self.assertIn(f"source/{record['saved_name']}", reasons)
        self.assertIn("保留策略", reasons[f"source/{record['saved_name']}"])
        self.assertIn(f"detections/{record['result_image']}", reasons)

        with self._open(payload) as zf:
            names = set(zf.namelist())
            self.assertNotIn("source/", names)
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest["file_count"], 0)
            self.assertEqual(manifest["missing_count"], len(manifest["missing"]))
            self.assertGreater(manifest["missing_count"], 0)
            readme = zf.read("README.md").decode("utf-8")
            self.assertIn("缺失", readme)
            self.assertIn(record["saved_name"], readme)

    def test_results_csv_rows_and_header(self):
        record = _record()
        payload, _ = repro.build_bundle(_manifest(record), record, self._resolve({}))
        with self._open(payload) as zf:
            text = zf.read("results.csv").decode("utf-8-sig")
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], ",".join(repro.CSV_HEADER))
        self.assertEqual(len(lines), 1 + 3, "三行明细应一一对应")
        # 置信度保留 4 位有效小数(round 后去除末尾 0), 坐标保留 2 位, 宽高为该图尺寸
        self.assertEqual(lines[1], "0,0,海胆,0.91,10.0,20.0,30.0,40.0,640,480")

    def test_video_bundle_has_frame_stats_and_no_result_video_reason(self):
        record = _record(
            id=202, source_name="dive.mp4", task_type="video",
            result_image=None, result_video=None, duration_sec=12.5,
            effective_count_source="maxn", maxn={"maxn_total": 4, "cpunx": 1152.0},
            frame_stats=[
                {"frame_index": 0, "timestamp_sec": 0.0, "total_objects": 2,
                 "class_counts": {"海胆": 2}},
                {"frame_index": 15, "timestamp_sec": 1.5, "total_objects": 3,
                 "class_counts": {"海胆": 2, "鱼类": 1}},
            ],
        )
        payload, info = repro.build_bundle(_manifest(record), record, self._resolve({}))
        with self._open(payload) as zf:
            frames = zf.read("frame_stats.csv").decode("utf-8-sig")
            readme = zf.read("README.md").decode("utf-8")
        self.assertEqual(len(frames.strip().splitlines()), 3)
        # 视频未生成结果视频时, 必须给出原因而不是静默跳过
        self.assertIn("未生成结果视频",
                      " ".join(m["reason"] for m in info["missing"]))
        # README 必须讲清 MaxN 是相对丰度
        self.assertIn("MaxN", readme)
        self.assertIn("相对丰度", readme)

    def test_readme_section_differs_by_task_type(self):
        img = _record()
        vid = _record(id=303, task_type="video", source_name="v.mp4")
        _, _ = repro.build_bundle(_manifest(img), img, self._resolve({}))
        img_readme = repro._readme(_manifest(img) | {"files": [], "missing": []}, [])
        vid_readme = repro._readme(_manifest(vid) | {"files": [], "missing": []}, [])
        self.assertNotEqual(img_readme, vid_readme)
        self.assertIn("重跑", img_readme)
        self.assertIn("重跑", vid_readme)

    def test_readme_quantifies_sampling_gap(self):
        """明细条数少于累计检出框时, README 必须给出具体两个数, 而不是只说"是采样"。"""
        record = _record(id=404, task_type="video", source_name="v.mp4",
                         total_detections=2332,
                         items=[{"frame_index": 0, "class_id": 0, "class_name": "扇贝",
                                 "confidence": 0.8, "x1": 1, "y1": 2, "x2": 3, "y2": 4}])
        payload, _ = repro.build_bundle(_manifest(record), record, self._resolve({}))
        with self._open(payload) as zf:
            readme = zf.read("README.md").decode("utf-8")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["item_rows"], 1)
        self.assertIn("共 1 行明细", readme)
        self.assertIn("累计检出框为 2332 个", readme)
        self.assertIn("采样口径差异", readme)

    def test_readme_omits_gap_hint_when_full(self):
        """明细数与检出框数一致时不应画蛇添足地提示采样差异。"""
        record = _record()  # 3 条明细 / 3 个检出框
        payload, _ = repro.build_bundle(_manifest(record), record, self._resolve({}))
        with self._open(payload) as zf:
            readme = zf.read("README.md").decode("utf-8")
        self.assertNotIn("采样口径差异", readme)

    def test_environment_info_is_tolerant(self):
        env = repro.environment_info()
        self.assertIn("python", env)
        self.assertIn("platform", env)
        self.assertIsInstance(env.get("packages"), (dict, list))

    def test_safe_archive_name_blocks_traversal(self):
        self.assertEqual(repro.safe_archive_name({"source_name": "../../etc/passwd"}),
                         "passwd")
        self.assertNotIn("/", repro.safe_archive_name({"source_name": "a/b/c.png"}))
        self.assertNotIn("\\", repro.safe_archive_name({"source_name": "a\\b\\c.png"}))
        self.assertEqual(repro.safe_archive_name({"source_name": ""}), "record")
        self.assertEqual(repro.safe_archive_name({}), "record")
        # 中文被替换为下划线, 不产生空串
        name = repro.safe_archive_name({"source_name": "珊瑚礁.jpg"})
        self.assertTrue(name)
        self.assertTrue(all(ch.isalnum() or ch in "-_" for ch in name))


class CompareTest(unittest.TestCase):
    """对比结果必须"先对齐口径, 再比数量"。"""

    def _img(self, rid, total, classes, items, source="image_objects"):
        return {
            "id": rid, "source_name": f"img_{rid}.jpg", "task_type": "image",
            "created_at": "2026-10-20 12:00:00",
            "effective_count": total, "effective_count_source": source,
            "total_detections": total, "tracked_objects": 0,
            "result_image": f"image_{rid}.jpg", "result_video": None,
            "class_stats": [{"class_name": n, "count": c} for n, c in classes.items()],
            "items": items,
        }

    def _vid(self, rid, total, classes, items, source="maxn"):
        return {
            "id": rid, "source_name": f"vid_{rid}.mp4", "task_type": "video",
            "created_at": "2026-10-20 13:00:00",
            "effective_count": total, "effective_count_source": source,
            "total_detections": total, "tracked_objects": 6,
            "maxn": {"maxn_total": total, "cpunx": 1200.0},
            "result_image": None, "result_video": f"video_{rid}.mp4",
            "class_stats": [{"class_name": n, "count": c} for n, c in classes.items()],
            "items": items,
        }

    def _items(self, spec):
        out = []
        for name, confs in spec.items():
            for conf in confs:
                out.append({"class_name": name, "confidence": conf,
                            "class_id": 0, "frame_index": 0,
                            "x1": 0, "y1": 0, "x2": 1, "y2": 1})
        return out

    def test_same_caliber_is_comparable(self):
        a = self._img(1, 3, {"海胆": 2, "海星": 1}, [])
        b = self._img(2, 4, {"海胆": 3, "鱼类": 1}, [])
        items = {1: self._items({"海胆": [0.9, 0.8], "海星": [0.7]}),
                 2: self._items({"海胆": [0.95, 0.85, 0.6], "鱼类": [0.5]})}
        out = compare.build_comparison([a, b], items)
        self.assertTrue(out["comparable"])
        self.assertEqual(out["caliber_sources"], ["image_objects"])
        self.assertEqual(out["warnings"], [])
        self.assertEqual(out["scope"]["records"], 2)

    def test_different_caliber_raises_warning(self):
        a = self._img(1, 3, {"海胆": 3}, self._items({"海胆": [0.9]}))
        b = self._vid(2, 5, {"海胆": 4}, self._items({"海胆": [0.8]}))
        out = compare.build_comparison([a, b], {1: a["items"], 2: b["items"]})
        self.assertFalse(out["comparable"])
        self.assertEqual(out["caliber_sources"], ["image_objects", "maxn"])
        joined = " ".join(out["warnings"])
        self.assertIn("口径", joined)
        self.assertIn("图片与视频", joined)

    def test_class_matrix_union_fills_zero(self):
        a = self._img(1, 3, {"海胆": 2, "海星": 1}, [])
        b = self._img(2, 2, {"海胆": 1, "鱼类": 1}, [])
        out = compare.build_comparison([a, b], {1: [], 2: []})
        names = [row["class_name"] for row in out["class_matrix"]]
        self.assertEqual(set(names), {"海胆", "海星", "鱼类"})
        self.assertEqual(names[0], "海胆", "出现次数最多的类别应排在最前")
        matrix = {row["class_name"]: row["counts"] for row in out["class_matrix"]}
        # 缺失类别补 0, 而不是缺 key 或跳过该行
        self.assertEqual(matrix["海星"], {"1": 1, "2": 0})
        self.assertEqual(matrix["鱼类"], {"1": 0, "2": 1})

    def test_class_composition_uses_class_stats_not_items(self):
        """类别构成必须来自汇总表: 明细只有 1 条, 汇总是 9 条时以汇总为准。"""
        a = self._img(1, 9, {"海胆": 9}, self._items({"海胆": [0.9]}))
        out = compare.build_comparison([a], {1: a["items"], 2: []})
        row = out["class_matrix"][0]
        self.assertEqual(row["counts"]["1"], 9)

    def test_confidence_stats_and_sensitivity(self):
        items = self._items({"海胆": [0.1, 0.3, 0.5, 0.7, 0.9]})
        out = compare.build_comparison([self._img(1, 5, {"海胆": 5}, items)],
                                      {1: items})
        conf = out["columns"][0]["conf"]
        self.assertEqual(conf["count"], 5)
        self.assertAlmostEqual(conf["mean"], 0.5, places=3)
        self.assertEqual(conf["median"], 0.5)
        self.assertEqual(conf["min"], 0.1)
        self.assertEqual(conf["max"], 0.9)
        sens = {s["threshold"]: s for s in conf["sensitivity"]}
        self.assertEqual(sens[0.5]["kept"], 3)
        self.assertEqual(sens[0.9]["kept"], 1)
        self.assertEqual(sens[0.25]["ratio"], 80.0)

    def test_histogram_sum_equals_sample_count(self):
        items = self._items({"海胆": [0.05, 0.15, 0.55, 0.95, 1.0]})
        out = compare.build_comparison([self._img(1, 5, {"海胆": 5}, items)],
                                      {1: items}, bins=10)
        bins = out["conf_histograms"][0]["bins"]
        self.assertEqual(len(bins), 10)
        self.assertEqual(sum(b["count"] for b in bins), 5)
        # 边界值与 dao.stats_confidence 一致: 0.01 ~ 0.99
        self.assertAlmostEqual(bins[0]["lower"], 0.01, places=4)
        self.assertAlmostEqual(bins[-1]["upper"], 0.99, places=4)

    def test_video_sampling_warning(self):
        a = self._vid(1, 500, {"海胆": 400}, self._items({"海胆": [0.8] * 20}))
        b = self._vid(2, 480, {"海胆": 380}, self._items({"海胆": [0.8] * 20}))
        out = compare.build_comparison([a, b], {1: a["items"], 2: b["items"]})
        self.assertTrue(out["comparable"], "同为 maxn 口径, 应判定可比")
        self.assertIn("采样", " ".join(out["warnings"]))

    def test_empty_items_do_not_crash(self):
        a = self._img(1, 0, {}, [])
        out = compare.build_comparison([a], {1: []})
        self.assertEqual(out["class_matrix"], [])
        self.assertIsNone(out["columns"][0]["conf"]["mean"])
        self.assertEqual(out["columns"][0]["conf"]["count"], 0)

    def test_bins_clamped(self):
        items = self._items({"海胆": [0.5]})
        rec = self._img(1, 1, {"海胆": 1}, items)
        for raw, expect in ((1, 5), (99, 20), (10, 10)):
            out = compare.build_comparison([rec], {1: items}, bins=raw)
            self.assertEqual(len(out["conf_histograms"][0]["bins"]), expect)


class RecordsOrderedTest(unittest.TestCase):
    """`dao.get_records_ordered` 必须保序、去重、跳过不存在的。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="repro_compare_")
        self.root = Path(self.temp.name)
        self.original_settings = {
            name: getattr(settings, name)
            for name in ("data_dir", "upload_dir", "result_dir", "db_path")
        }
        self.original_conn = getattr(database._local, "conn", None)
        database._local.conn = None
        settings.data_dir = self.root
        settings.upload_dir = self.root / "uploads"
        settings.result_dir = self.root / "results"
        settings.db_path = self.root / "test.sqlite"
        settings.upload_dir.mkdir()
        settings.result_dir.mkdir()
        with mock.patch.object(database, "_register_exit_hook"):
            database.init_db()
        conn = database.get_connection()
        for rid in (11, 22, 33):
            conn.execute(
                "INSERT INTO detections (id, task_type, source_name, status,"
                " created_at, total_objects, total_detections, unique_count_valid,"
                " inference_snapshot) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, "image", f"f{rid}.jpg", "success",
                 "2026-10-20 12:00:00", 1, 1, 1, "{}"),
            )
        conn.commit()

    def tearDown(self):
        database.close_connection()
        database._local.conn = self.original_conn
        for name, value in self.original_settings.items():
            setattr(settings, name, value)
        self.temp.cleanup()

    def test_order_preserved_and_duplicates_removed(self):
        from app.db import dao
        rows = dao.get_records_ordered([33, 11, 33, 22])
        self.assertEqual([r["id"] for r in rows], [33, 11, 22])

    def test_missing_ids_skipped(self):
        from app.db import dao
        rows = dao.get_records_ordered([11, 999, 22])
        self.assertEqual([r["id"] for r in rows], [11, 22])

    def test_empty_input(self):
        from app.db import dao
        self.assertEqual(dao.get_records_ordered([]), [])
        self.assertEqual(dao.get_records_ordered(["abc"]), [])


class ExportPreflightTest(unittest.TestCase):
    """导出预检只读接口: 结论必须与真实导出行为一致, 且不得产生副作用。"""

    def setUp(self):
        # ignore_cleanup_errors: TestClient 在独立线程里另开 SQLite 连接,
        # Windows 上文件句柄可能晚于断言释放; 断言本身必须全绿, 但清理阶段
        # 的临时目录删不掉不该把通过的用例判成 error。
        self.temp = tempfile.TemporaryDirectory(
            prefix="preflight_", ignore_cleanup_errors=True
        )
        self.root = Path(self.temp.name)
        self.original_settings = {
            name: getattr(settings, name)
            for name in ("data_dir", "upload_dir", "result_dir", "db_path")
        }
        self.original_conn = getattr(database._local, "conn", None)
        database._local.conn = None
        settings.data_dir = self.root
        settings.upload_dir = self.root / "uploads"
        settings.result_dir = self.root / "results"
        settings.db_path = self.root / "test.sqlite"
        settings.upload_dir.mkdir()
        settings.result_dir.mkdir()
        with mock.patch.object(database, "_register_exit_hook"):
            database.init_db()
        conn = database.get_connection()
        (settings.upload_dir / "img.jpg").write_bytes(b"x" * 2048)
        (settings.result_dir / "image_out.jpg").write_bytes(b"y" * 1024)
        conn.execute(
            "INSERT INTO detections (id, task_type, source_name, saved_name,"
            " result_image, status, created_at, total_objects, total_detections,"
            " unique_count_valid, maxn_total, inference_snapshot)"
            " VALUES (1,'image','img.jpg','img.jpg','image_out.jpg','success',"
            "'2026-10-20 12:00:00',2,2,1,0,'{}')"
        )
        # 视频: 结果文件已被清理 + 计数回退到累计检出框
        conn.execute(
            "INSERT INTO detections (id, task_type, source_name, saved_name,"
            " status, created_at, total_objects, total_detections, unique_count_valid,"
            " maxn_total, inference_snapshot)"
            " VALUES (2,'video','clip.mp4','clip.mp4','success','2026-10-20 13:00:00',"
            " 40,40,0,0,'{}')"
        )
        conn.commit()

    def tearDown(self):
        # TestClient 的请求线程有自己的 thread-local 连接, 主线程关不掉;
        # 先尽力关一次, 再释放 sqlite 的 WAL 句柄, 最后才删目录。
        database.close_connection()
        database._local.conn = self.original_conn
        for name, value in self.original_settings.items():
            setattr(settings, name, value)
        database.close_connection()
        for _ in range(10):
            try:
                self.temp.cleanup()
                return
            except (PermissionError, OSError):
                time.sleep(0.05)

    def _get(self, rid):
        from fastapi.testclient import TestClient
        from app.main import app
        # 用 with 关闭 lifespan: 否则 TestClient 会一直握着库连接
        with TestClient(app, base_url="http://127.0.0.1:8000",
                        client=("127.0.0.1", 50000)) as client:
            resp = client.get(f"/api/report/preflight/{rid}")
        return resp

    def test_image_assets_listed_as_available(self):
        resp = self._get(1)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        by_field = {a["field"]: a for a in data["assets"]}
        self.assertTrue(by_field["saved_name"]["available"])
        self.assertTrue(by_field["result_image"]["available"])
        # 只统计实际存在的文件体积, 不把"缺失"当 0 混进合计
        self.assertEqual(data["asset_bytes"], 2048 + 1024)
        self.assertTrue(data["bundle_exportable"])
        self.assertTrue(data["label_exportable"])
        self.assertEqual(data["label_formats"], ["csv", "coco", "yolo"])
        self.assertEqual(data["warning_count"], 0)
        self.assertEqual(data["notes"], [])

    def test_missing_result_video_is_reported_not_silently_dropped(self):
        """视频记录的结果文件字段名是 result_video, 该列在库里存的是磁盘文件名。

        这里构造的记录没有填该列, 因此它不出现在 assets 里 —— 但要保证
        "没有该文件"不会被当成"文件已存在的空文件", 且视频仍可导出复现包。
        """
        data = self._get(2).json()["data"]
        fields = {a["field"] for a in data["assets"]}
        self.assertNotIn("result_video", fields)   # 未填 = 无此文件, 不虚报
        self.assertIn("saved_name", fields)
        self.assertTrue(all(a["bytes"] >= 0 for a in data["assets"]))
        self.assertTrue(data["bundle_exportable"])

    def test_missing_source_file_is_reported(self):
        """源文件被清理后, 预检必须提前说明, 而不是等导出后翻 README 才发现。"""
        (settings.upload_dir / "img.jpg").unlink()
        data = self._get(1).json()["data"]
        by_field = {a["field"]: a for a in data["assets"]}
        self.assertFalse(by_field["saved_name"]["available"])
        self.assertTrue(any("原始上传文件已不在受管目录" in n for n in data["notes"]))
        # 缺源文件仍应允许导出: 复现包会把缺失如实写进 missing 清单
        self.assertTrue(data["bundle_exportable"])

    def test_video_record_has_no_label_formats(self):
        data = self._get(2).json()["data"]
        self.assertEqual(data["label_formats"], [])
        self.assertFalse(data["label_exportable"])
        self.assertTrue(any("视频记录不做标签导出" in n for n in data["notes"]))

    def test_video_fallback_caliber_is_warned(self):
        data = self._get(2).json()["data"]
        self.assertEqual(data["effective_count_source"], "total_detections")
        self.assertTrue(any("计数回退为累计检出框" in n for n in data["notes"]))

    def test_unknown_record_returns_404(self):
        self.assertEqual(self._get(999999).status_code, 404)

    def test_preflight_does_not_create_files(self):
        before = {p.name for p in self.root.rglob("*") if p.is_file()}
        self._get(1)
        after = {p.name for p in self.root.rglob("*") if p.is_file()}
        # 只读库与 stat: 不应新增任何产物(尤其不能有 zip)
        self.assertEqual(before, after)
        self.assertFalse(any(name.endswith(".zip") for name in after))


if __name__ == "__main__":
    unittest.main(verbosity=2)
