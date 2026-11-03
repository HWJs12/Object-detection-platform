"""YOLO 类别编号、历史模型类表和空负样本的纯内存导出回归。"""
import io
import unittest
import zipfile

import yaml

from app.core import exporter


class YoloCategoryExportTests(unittest.TestCase):
    def image(self, detections=None, classes=None, name="sample.jpg"):
        item = {
            "source_name": name, "width": 100, "height": 100,
            "detections": detections or [],
        }
        if classes is not None:
            item["inference_snapshot"] = {"model": {"classes": classes}}
        return item

    def detection(self, cid, name):
        return {"class_id": cid, "class_name": name, "bbox": [10, 20, 30, 40]}

    def unpack(self, items):
        content, extension, media_type = exporter.export_labels(items, "yolo")
        self.assertEqual((extension, media_type), (".zip", "application/zip"))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
        classes = files["classes.txt"].splitlines()
        config = yaml.safe_load(files["README.txt"].split("data.yaml 示例:\n", 1)[1])
        self.assertEqual(config["nc"], len(classes))
        self.assertEqual(config["names"], classes)
        for name, text in files.items():
            if name.startswith("labels/"):
                for line in text.splitlines():
                    cid = int(line.split()[0])
                    self.assertGreaterEqual(cid, 0)
                    self.assertLess(cid, config["nc"])
        return files, classes

    def test_sparse_class_two_keeps_label_id_and_fills_lower_slots(self):
        items = [self.image([self.detection(2, "海参")], name=name)
                 for name in ("one.jpg", "two.jpg")]
        files, classes = self.unpack(items)
        self.assertEqual(classes, ["__unknown_class_0__", "__unknown_class_1__", "海参"])
        for name in ("one", "two"):
            self.assertEqual(files[f"labels/{name}.txt"].split()[0], "2")
        self.assertIn("从 0 开始编号", files["README.txt"])
        self.assertIn("占位", files["README.txt"])

    def test_explicit_model_classes_preserve_unobserved_and_negative_classes(self):
        names = ["海胆", "海星", "海参", "扇贝"]
        sources = ([{"id": i, "name": name} for i, name in enumerate(names)],
                   {str(i): name for i, name in enumerate(names)}, names)
        for source in sources:
            with self.subTest(source=source):
                items = [self.image([self.detection(2, "海参")], classes=source),
                         self.image(classes=source, name="negative.jpg")]
                files, classes = self.unpack(items)
                self.assertEqual(classes, names)
                self.assertEqual(files["labels/negative.txt"], "")
                files, classes = self.unpack([self.image(classes=source)])
                self.assertEqual(classes, names)
                self.assertEqual(files["labels/sample.txt"], "")

    def test_no_detections_or_model_source_is_explicitly_not_trainable(self):
        for items in ([], [self.image()]):
            with self.subTest(items=items):
                files, classes = self.unpack(items)
                self.assertEqual(classes, [])
                self.assertEqual(files["classes.txt"], "")
                self.assertIn("不能使用 nc: 0 直接训练", files["README.txt"])

    def test_yaml_escapes_quotes_and_class_lines_keep_their_indices(self):
        names = ["sea's \"urchin\" \\ #1", "海参: [A, B]", "first\nsecond"]
        files, classes = self.unpack([self.image(classes=names)])
        self.assertEqual(classes, [names[0], names[1], "first second"])
        self.assertEqual(files["labels/sample.txt"], "")

    def test_invalid_ids_are_not_falsely_exported_as_class_zero(self):
        dets = [self.detection(cid, "invalid") for cid in (None, -1, 1.5, "bad")]
        dets.append(self.detection(2, None))
        files, classes = self.unpack([self.image(dets)])
        self.assertEqual(classes, [f"__unknown_class_{i}__" for i in range(3)])
        self.assertEqual(len(files["labels/sample.txt"].splitlines()), 1)
        self.assertTrue(files["labels/sample.txt"].startswith("2 "))

    def test_contiguous_classes_and_coordinates_remain_unchanged(self):
        files, classes = self.unpack([self.image([
            self.detection(0, "urchin"), self.detection(1, "scallop")])])
        self.assertEqual(classes, ["urchin", "scallop"])
        self.assertEqual(files["labels/sample.txt"],
                         "0 0.200000 0.300000 0.200000 0.200000\n"
                         "1 0.200000 0.300000 0.200000 0.200000\n")


if __name__ == "__main__":
    unittest.main()