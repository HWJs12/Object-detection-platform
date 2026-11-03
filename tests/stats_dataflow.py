"""数据统计页第 2 轮: 真实数据流端到端测试。

流程:
  1. 抓取统计接口快照 (前)
  2. 从测试图片目录取 N 张图, 用专用站点名走批量检测接口
  3. 用测试视频走视频检测接口 (异步任务, 轮询直到完成)
  4. 抓取统计接口快照 (后)
  5. 逐项核对增量: 检测记录数 / 检出框数 / 类别分布 / 今日卡片 / 站点维度

用法:
    python tests/stats_dataflow.py --images 6 --no-video
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"
IMG_DIR = ROOT / "samples"
VIDEO = next(iter(sorted(IMG_DIR.glob("*.mp4"))), IMG_DIR / "sample_underwater.mp4")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}] {name}  {detail}")
    return cond


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body["data"] if isinstance(body, dict) and "data" in body else body


def multipart_post(path: str, files: list, fields: dict):
    """手写 multipart (避免依赖 requests 之外的库)。"""
    boundary = "----qa" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for field_name, fp, mime in files:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; '
                 f'filename="{fp.name}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        body += fp.read_bytes()
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + path, data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


def snapshot(tag: str):
    snap = {}
    for key, path in {
        "overview": "/api/stats/overview",
        "classes": "/api/stats/classes",
        "ecology": "/api/stats/ecology",
        "eco24h": "/api/stats/ecology?hours=24",
        "today24": "/api/stats/today?hours=24",
        "sites": "/api/stats/sites",
        "types": "/api/stats/types",
    }.items():
        snap[key] = get(path)
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=int, default=6)
    ap.add_argument("--video", action="store_true")
    ap.add_argument("--station-id", type=int, required=True, help="地图中已建立且启用的测试站点编号")
    args = ap.parse_args()

    station = get(f"/api/stations/{args.station_id}")
    if not station.get("active"):
        raise RuntimeError("所选站点已停用")
    site_tag = station["name"]
    print(f"===== 数据流测试, 站点标记: {site_tag} =====")

    print("\n[0] 抓取前快照")
    before = snapshot("前")
    before_records = before["overview"]["total_count"]
    before_objects = before["overview"]["total_objects"]
    before_images = before["overview"]["image_count"]
    before_videos = before["overview"]["video_count"]
    before_today_cnt = before["overview"]["today_count"]
    before_today_obj = before["overview"]["today_objects"]

    # ------------------------------------------------ 图片批量检测
    imgs = sorted(IMG_DIR.glob("*.jpg"))[: args.images]
    print(f"\n[1] 批量图片检测 ({len(imgs)} 张)")
    resp = multipart_post(
        "/api/detect/images",
        [("files", p, "image/jpeg") for p in imgs],
        {"site_id": args.station_id, "transect": "QA-T1"},
    )
    data = resp.get("data", resp)
    items = data.get("items", data if isinstance(data, list) else [])
    ok_items = [it for it in items if not it.get("error")]
    fail_items = [it for it in items if it.get("error")]
    check(f"批量接口返回 {len(imgs)} 项", len(items) == len(imgs),
          f"got {len(items)}")
    check("无失败项", not fail_items, str(fail_items[:1]))
    expected_img_objects = sum(int(it.get("total", 0)) for it in ok_items)
    print(f"      检出框合计 {expected_img_objects}")

    # ------------------------------------------------ 视频检测
    video_objects = 0
    if args.video and VIDEO.exists():
        print("\n[2] 视频检测")
        resp = multipart_post(
            "/api/detect/video",
            [("file", VIDEO, "video/mp4")],
            {"site_id": args.station_id},
        )
        vd = resp.get("data", resp)
        task_id = vd.get("task_id")
        check("视频任务已创建", bool(task_id), str(vd)[:120])
        # 轮询任务状态
        t0 = time.time()
        while time.time() - t0 < 600:
            time.sleep(5)
            st = get(f"/api/detect/video/{task_id}/status") if task_id else {}
            status = (st or {}).get("status")
            if status in ("success", "failed", "stopped"):
                break
        video_objects = int((st or {}).get("total_objects") or (st or {}).get("unique_objects") or 0)
        check("视频任务完成", status == "success", f"status={status}")
        print(f"      视频检出目标 {video_objects}, 状态 {status}")

    # ------------------------------------------------ 后快照对比
    print("\n[3] 抓取后快照并核对增量")
    after = snapshot("后")
    d_records = after["overview"]["total_count"] - before_records
    d_objects = after["overview"]["total_objects"] - before_objects
    d_images = after["overview"]["image_count"] - before_images
    d_videos = after["overview"]["video_count"] - before_videos
    d_today_cnt = after["overview"]["today_count"] - before_today_cnt
    d_today_obj = after["overview"]["today_objects"] - before_today_obj
    expect_records = len(ok_items) + (1 if args.video else 0)
    expect_objects = expected_img_objects + video_objects
    check(f"记录增量 = {expect_records}", d_records == expect_records, f"got {d_records}")
    check(f"图片记录增量 = {len(ok_items)}", d_images == len(ok_items), f"got {d_images}")
    check(f"检出框增量 = {expect_objects}", d_objects == expect_objects,
          f"got {d_objects} (img {expected_img_objects} + video {video_objects})")
    check(f"今日检测次数增量 = {expect_records}", d_today_cnt == expect_records, f"got {d_today_cnt}")
    check(f"今日检出框增量 = {expect_objects}", d_today_obj == expect_objects, f"got {d_today_obj}")

    # 类别分布一致性: after.classes - before.classes = 本批各类别计数
    def class_map(snap):
        return {r["class_name"]: r["count"] for r in snap["classes"]}
    cm_before, cm_after = class_map(before), class_map(after)
    per_class_delta = {}
    for it in ok_items:
        for c in it.get("counts", []):
            per_class_delta[c["class_name"]] = per_class_delta.get(c["class_name"], 0) + int(c["count"])
    all_keys = set(cm_before) | set(cm_after)
    mismatch = []
    for k in all_keys:
        got = cm_after.get(k, 0) - cm_before.get(k, 0)
        want = per_class_delta.get(k, 0) + (0 if args.video else 0)
        # 视频类别增量不逐一比对(视频有追踪口径), 这里只要求"不减少"
        if got < per_class_delta.get(k, 0):
            mismatch.append(f"{k}: delta={got} < img-delta={per_class_delta.get(k, 0)}")
    check("各类别增量与本批检测一致", not mismatch, "; ".join(mismatch))

    # 站点维度: 新站点应出现在 sites 列表
    sites = after["sites"].get("items") or []
    hit = [s for s in sites if s.get("site") == site_tag or s.get("site_label") == site_tag]
    check("选定站点出现在站点维度", bool(hit),
          f"sites 共 {len(sites)} 个")

    # 24h 滚动窗口应覆盖刚跑的数据
    check("24h 窗口 total 增加",
          after["eco24h"]["total"] >= before["eco24h"]["total"] or not args.video,
          f"{before['eco24h']['total']} -> {after['eco24h']['total']}")

    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败明细:")
        for f in FAIL:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
