#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR 封口贴检测 V3 —— 简化逻辑，大幅提升检出率

相对 V2 的核心改动：
  1. 背面识别宽松化：只要 OCR 检测到"扫码即领"即视为背面，
     包装盒检测失败（fallback）不再跳过该图片。
  2. 找到第一张背面即停止扫描，节省约 50% OCR 时间。
  3. 去除角度验证步骤（贴纸偏角不再影响合规判定）。
  4. 位置容差放宽：STICKER_POSITION_TOLERANCE 0.15 → 0.25，
     贴纸适当超出包装盒边界仍视为合规。
  5. 新增输出列"是否存在非官方贴纸"（0/1）。

检测流程：
  Step 1  遍历图片，找第一张含"扫码即领"的图 → 背面
          无背面 → 是否规范粘贴=0
  Step 2  仅对背面图检测非官方贴纸（颜色检测）
          检出 → 是否存在非官方贴纸=1，是否规范粘贴=0
  Step 3  无非官方贴纸 → 单/双贴位置验证（放宽容差，无角度约束）
          单贴或双贴位置合规 → 是否规范粘贴=1

position_valid 编码：
  -1 : 未找到封口贴（无背面图）
   0 : 贴纸位置不符合规范
   1 : 贴纸位置规范（合格）
   2 : 贴纸平铺错误（整条贴条未绕折封口）
   4 : 检测到非官方贴纸
"""

import re
import numpy as np
import cv2
import pandas as pd
import requests
from paddleocr import PaddleOCR
from PIL import Image
from io import BytesIO
import os
import time
import traceback
import json
from concurrent.futures import ThreadPoolExecutor

# ─── 可调参数 ────────────────────────────────────────────────────────────────
IMAGE_COLUMNS    = ['图片地址', 'Unnamed: 16', 'Unnamed: 17', 'Unnamed: 18', 'Unnamed: 19', 'Unnamed: 20', 'Unnamed: 21', 'Unnamed: 22']
DOWNLOAD_WORKERS = 12

# 必须按“文本”读写的列：订单号是 19 位整数，超过 float64/Excel 双精度可精确表示的
# 上限(2^53≈16 位)。若按数字写入 .xlsx，Excel 会把它存成 double → 末几位被抹平
# (如 5119350049330019519 → 5119350049330019328)。全程当字符串处理即可保精度。
ID_TEXT_COLS = ['订单号']
_ID_DTYPE    = {c: str for c in ID_TEXT_COLS}

# 位置容差（相对坐标系）：封口贴绕折超出包装盒边界仍算合规
# V3 放宽：0.15 → 0.25
STICKER_POSITION_TOLERANCE = 0.25

# 包装盒检测：最大内部分辨率
BOX_DETECT_MAX_SIDE = 1200

# 极端兜底：包装盒最小面积占比（已宽松化，fallback 不再跳过）
BOX_FRONTAL_MIN_RATIO = 0.02

# 非官方贴纸颜色检测阈值
UNOFFICIAL_SAT_ABOVE_BG  = 55
UNOFFICIAL_VAL_RANGE     = (40, 230)
UNOFFICIAL_AREA_RATIO    = 0.05       # V3.1: 默认 5%（非 AirPods 白盒）
UNOFFICIAL_SOLIDITY_MIN  = 0.45
UNOFFICIAL_EDGE_GRAD_MIN = 6.0

# 白色背景最少像素数：低于此值说明拍照条件较差，无法可靠建立白平衡基准
UNOFFICIAL_WHITE_BG_MIN  = 800

# 官方印刷关键词：这些区域包含官方盒面印刷彩色元素，颜色检测时排除
# AirPods 盒子右侧紫色"Apple授权专营店 在你身边"条带是典型误触发来源
OFFICIAL_PRINT_KEYWORDS  = ["Apple授权专营店", "在你身边", "授权专营店"]

# 棕色瓦楞纸盒 HSV 范围（Mac 专用）
BROWN_HSV_LOW  = (5,  30, 40)
BROWN_HSV_HIGH = (30, 200, 220)

# ── 多 LOB 配置 ──────────────────────────────────────────────────────────────
LOB_CONFIGS: dict[str, dict] = {
    "iPhone": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        "auth_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.70, "y_max": 1.00},
        "front_face_aspect_range": (1.6, 2.4),
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            "area_ratio": 0.05, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    "Watch": {
        "sticker_count": "single_or_dual",
        "scan_sticker": [
            {"y_min": 0.00, "y_max": 0.45},
            {"y_min": 0.55, "y_max": 1.00},
        ],
        "auth_sticker": [
            {"y_min": 0.00, "y_max": 0.45},
            {"y_min": 0.55, "y_max": 1.00},
        ],
        "front_face_aspect_range": (2.5, 5.0),
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            "area_ratio": 0.05, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    "AirPods": {
        "sticker_count": "single_only",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.50},
        "auth_sticker": None,
        "front_face_aspect_range": (0.85, 1.35),
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            # AirPods 官方盒面彩色印刷（紫色授权条带 + 绿色回收箭头）最高可占
            # 整个盒面约 10%，需将阈值设在其上方才不会误判官方贴纸为非官方
            "area_ratio": 0.12, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    "Accy.": {
        "sticker_count": "single_only",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.50},
        "auth_sticker": None,
        "front_face_aspect_range": None,
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            "area_ratio": 0.05, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    "iPad": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        "auth_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.70, "y_max": 1.00},
        "front_face_aspect_range": (1.2, 1.7),
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            "area_ratio": 0.05, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    "Mac": {
        "sticker_count": "single_or_dual",
        # 扫码即领：下方居中 或 上方居中（镜像）均合规
        "scan_sticker": [
            {"x_min": 0.25, "x_max": 0.75, "y_min": 0.70, "y_max": 1.00},
            {"x_min": 0.25, "x_max": 0.75, "y_min": 0.00, "y_max": 0.30},
        ],
        # Apple授权专营店：上方左侧 或 上方右侧（镜像）均合规
        "auth_sticker": [
            {"x_min": 0.05, "x_max": 0.50, "y_min": 0.00, "y_max": 0.30},
            {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        ],
        "front_face_aspect_range": (1.2, 2.0),
        # 非官方贴纸颜色检测对 Mac 关闭：Mac 多为棕色纸箱，箱上常贴大块白色物流标/
        # 授权店封签，会被 brown_box 检测大面积误判为"疑似非官方贴纸"（误报率高）。
        # 关闭后 Mac 不做颜色检测，颜色检测已执行=0、是否存在非官方贴纸=0，其余流程不变。
        "unofficial_color": {
            "enabled": False, "mode": "brown_box",
            "brown_hue_range": (5, 30), "brown_sat_min": 30,
            "brown_val_range": (40, 200), "white_sat_max": 30,
            "white_val_min": 200, "sat_min_abs": 80,
            "val_range": (50, 240), "area_ratio": 0.05,
            "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
}

UNRECOGNIZED_LOB = "UNKNOWN LOB"


def _normalize_position_cfg(position_cfg) -> list[dict]:
    if position_cfg is None:
        return []
    if isinstance(position_cfg, dict):
        return [position_cfg]
    if isinstance(position_cfg, (list, tuple)):
        return [c for c in position_cfg if isinstance(c, dict)]
    return []


def detect_lob(row) -> str:
    try:
        raw = row.get("LOB", None)
        if raw is None or (isinstance(raw, float) and np.isnan(raw)):
            return UNRECOGNIZED_LOB
        key = str(raw).strip()
        if key in LOB_CONFIGS:
            return key
    except Exception:
        pass
    return UNRECOGNIZED_LOB


# ─── PaddleOCR 初始化 ────────────────────────────────────────────────────────
print("=" * 80)
print("正在初始化 PaddleOCR (GPU加速)...")
print("=" * 80)
ocr = PaddleOCR(
    use_textline_orientation=True,
    lang='ch',
    device='gpu',
    enable_mkldnn=False,
    text_det_limit_side_len=2000,   # 注意：默认 limit_type='min'，此值是“短边放大目标”。
                                    # 曾误改为 3000，导致所有 LOB 图片短边被放大到 3000，
                                    # 超出检测模型训练尺度 → '扫码即领' 锚点漏检 → 背面图找不到
                                    # → 封口贴存在大量误判为 0。实测 2000 全面优于 3000，故回退。
)
print("PaddleOCR 初始化完成！\n")

_dl_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)


# ═══════════════════════════════════════════════════════════════════════════════
# 一、图像工具
# ═══════════════════════════════════════════════════════════════════════════════

def pil_to_cv(image_pil: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(image_pil), cv2.COLOR_RGB2BGR)


def resize_for_ocr(image: Image.Image, max_side: int = 2000) -> Image.Image:
    if image is None:
        return None
    w, h = image.size
    if max(w, h) <= max_side:
        return image
    scale = max_side / max(w, h)
    return image.resize(
        (max(1, int(w * scale)), max(1, int(h * scale))),
        resample=Image.BICUBIC
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 二、下载
# ═══════════════════════════════════════════════════════════════════════════════

def download_image(url: str, timeout: int = 15) -> Image.Image | None:
    try:
        if pd.isna(url) or url == '':
            return None
        response = requests.get(url, timeout=timeout)
        if response.status_code == 200:
            return Image.open(BytesIO(response.content))
        print(f"  下载失败 (状态码 {response.status_code}): {url}")
        return None
    except Exception as e:
        print(f"  下载异常: {url}, 错误: {str(e)[:50]}")
        return None


def submit_row_downloads(row) -> list[tuple]:
    tasks = []
    for col_idx, col in enumerate(IMAGE_COLUMNS, 1):
        if col not in row.index:
            continue
        url = row[col]
        if pd.isna(url) or url == '':
            continue
        future = _dl_executor.submit(download_image, url)
        tasks.append((col_idx, col, url, future))
    return tasks


# ═══════════════════════════════════════════════════════════════════════════════
# 三、OCR
# ═══════════════════════════════════════════════════════════════════════════════

def ocr_image_full(image: Image.Image, image_id: str = "unknown"):
    if image is None:
        return "", [], [], 0, 0
    try:
        orig_w, orig_h = image.size
        image_resized = resize_for_ocr(image, max_side=2000)
        res_w, res_h = image_resized.size

        result = ocr.predict(input=pil_to_cv(image_resized))

        texts, polys_res = [], []
        if result and len(result) > 0:
            ocr_result = result[0]
            if hasattr(ocr_result, 'json'):
                res = ocr_result.json.get('res', {})
                texts = res.get('rec_texts', [])
                polys_res = res.get('dt_polys', res.get('boxes', []))

        sx = orig_w / res_w if res_w > 0 else 1.0
        sy = orig_h / res_h if res_h > 0 else 1.0
        polys_orig = [
            [[pt[0] * sx, pt[1] * sy] for pt in poly]
            for poly in polys_res
        ]
        return " ".join(texts), texts, polys_orig, orig_h, orig_w

    except Exception as e:
        print("  OCR 识别异常:", type(e).__name__, repr(e))
        print(traceback.format_exc())
        return "", [], [], 0, 0


# ═══════════════════════════════════════════════════════════════════════════════
# 四、包装盒检测与透视矫正
# ═══════════════════════════════════════════════════════════════════════════════

def detect_box_bbox(image_pil: Image.Image, lob: str | None = None):
    img_cv = pil_to_cv(image_pil)
    H, W = img_cv.shape[:2]

    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)

    MIN_AREA_RATIO = 0.08
    MAX_ASPECT     = 5.0

    edges = cv2.Canny(filtered, 20, 80)
    kernel = np.ones((9, 9), np.uint8)
    dilated = cv2.dilate(edges, kernel, iterations=3)
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    for cnt in contours[:10]:
        if cv2.contourArea(cnt) < MIN_AREA_RATIO * dW * dH:
            continue
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.03 * peri, True)
        bx, by, bw, bh = cv2.boundingRect(approx)
        aspect = bw / max(bh, 1)
        if (1 / MAX_ASPECT) <= aspect <= MAX_ASPECT and bw * bh >= MIN_AREA_RATIO * dW * dH:
            return (
                int(bx / scale), int(by / scale),
                int(bw / scale), int(bh / scale), 'edge'
            )

    if lob == "Mac":
        hsv = cv2.cvtColor(img_small, cv2.COLOR_BGR2HSV)
        thresh = cv2.inRange(hsv, np.array(BROWN_HSV_LOW, dtype=np.uint8),
                                   np.array(BROWN_HSV_HIGH, dtype=np.uint8))
        method_name = 'brown'
        k = np.ones((7, 7), np.uint8)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k, iterations=2)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN,  k, iterations=1)
    else:
        _, thresh = cv2.threshold(filtered, 190, 255, cv2.THRESH_BINARY)
        method_name = 'bright'

    border = max(5, int(min(dW, dH) * 0.01))
    thresh[:border, :]  = 0; thresh[-border:, :] = 0
    thresh[:, :border]  = 0; thresh[:, -border:] = 0

    contours2, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours2 = sorted(contours2, key=cv2.contourArea, reverse=True)

    for cnt in contours2[:5]:
        if cv2.contourArea(cnt) < MIN_AREA_RATIO * dW * dH:
            continue
        bx, by, bw, bh = cv2.boundingRect(cnt)
        aspect = bw / max(bh, 1)
        if (1 / MAX_ASPECT) <= aspect <= MAX_ASPECT:
            return (
                int(bx / scale), int(by / scale),
                int(bw / scale), int(bh / scale), method_name
            )

    return 0, 0, W, H, 'fallback'


def _order_quad_corners(pts: np.ndarray) -> np.ndarray:
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = pts[:, 0] - pts[:, 1]
    rect = np.zeros((4, 2), dtype=np.float32)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    rect[1] = pts[np.argmax(d)]
    rect[3] = pts[np.argmin(d)]
    return rect


_QUAD_MAX_ASPECT = 8.0
_QUAD_MIN_EDGE   = 10


def _quad_from_contour(cnt: np.ndarray) -> np.ndarray | None:
    peri = cv2.arcLength(cnt, True)
    for eps_ratio in (0.02, 0.03, 0.04, 0.05):
        approx = cv2.approxPolyDP(cnt, eps_ratio * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            cand = approx.reshape(4, 2).astype(np.float32)
            xs, ys = cand[:, 0], cand[:, 1]
            w_hat = float(max(xs) - min(xs))
            h_hat = float(max(ys) - min(ys))
            if min(w_hat, h_hat) < _QUAD_MIN_EDGE:
                return None
            if max(w_hat, h_hat) / max(min(w_hat, h_hat), 1.0) > _QUAD_MAX_ASPECT:
                return None
            return cand
        if len(approx) > 4:
            continue
        if len(approx) < 4:
            break
    try:
        rect = cv2.minAreaRect(cnt)
        cw, ch = rect[1]
        if min(cw, ch) >= _QUAD_MIN_EDGE:
            if max(cw, ch) / max(min(cw, ch), 1.0) <= _QUAD_MAX_ASPECT:
                return cv2.boxPoints(rect).astype(np.float32)
    except Exception:
        return None
    return None


def _find_quads_canny(img_cv, max_candidates=8, min_area_ratio=0.03):
    H, W = img_cv.shape[:2]
    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)
    edges = cv2.Canny(filtered, 20, 80)
    dilated = cv2.dilate(edges, np.ones((9, 9), np.uint8), iterations=3)
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    quads = []
    for cnt in contours[:25]:
        if cv2.contourArea(cnt) < min_area_ratio * dW * dH:
            continue
        q = _quad_from_contour(cnt)
        if q is not None:
            quads.append((q / scale).astype(np.float32))
        if len(quads) >= max_candidates:
            break
    return quads


def _find_quads_brown_split(img_cv, max_candidates=8, min_area_ratio=0.03):
    H, W = img_cv.shape[:2]
    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    hsv = cv2.cvtColor(img_small, cv2.COLOR_BGR2HSV)
    brown_mask = cv2.inRange(hsv, np.array(BROWN_HSV_LOW, dtype=np.uint8),
                                  np.array(BROWN_HSV_HIGH, dtype=np.uint8))
    border = max(5, int(min(dW, dH) * 0.01))
    brown_mask[:border, :]  = 0; brown_mask[-border:, :] = 0
    brown_mask[:, :border]  = 0; brown_mask[:, -border:] = 0
    k7 = np.ones((7, 7), np.uint8)
    brown_mask = cv2.morphologyEx(brown_mask, cv2.MORPH_CLOSE, k7, iterations=2)
    brown_mask = cv2.morphologyEx(brown_mask, cv2.MORPH_OPEN,  k7, iterations=1)

    contours, _ = cv2.findContours(brown_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    box_outline = max(contours, key=cv2.contourArea)
    if cv2.contourArea(box_outline) < min_area_ratio * dW * dH:
        return []

    box_mask = np.zeros((dH, dW), dtype=np.uint8)
    cv2.drawContours(box_mask, [box_outline], -1, 255, -1)

    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)
    inner_edges = cv2.Canny(filtered, 5, 25)
    inner_edges[box_mask == 0] = 0
    edges_dil = cv2.dilate(inner_edges, np.ones((5, 5), np.uint8), iterations=2)
    sub_mask = cv2.bitwise_and(box_mask, cv2.bitwise_not(edges_dil))

    sub_cnts, _ = cv2.findContours(sub_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    sub_cnts = sorted(sub_cnts, key=cv2.contourArea, reverse=True)

    quads = []
    for cnt in sub_cnts[:15]:
        if cv2.contourArea(cnt) < min_area_ratio * dW * dH:
            continue
        q = _quad_from_contour(cnt)
        if q is not None:
            quads.append((q / scale).astype(np.float32))
        if len(quads) >= max_candidates:
            break
    return quads


def _dedup_quads(quads, center_threshold=50.0):
    out = []
    for q in quads:
        cx, cy = float(q[:, 0].mean()), float(q[:, 1].mean())
        if not any(
            (cx - float(q2[:, 0].mean()))**2 + (cy - float(q2[:, 1].mean()))**2
            < center_threshold**2
            for q2 in out
        ):
            out.append(q)
    return out


def _quad_size_aspect(quad):
    ordered = _order_quad_corners(quad)
    tl, tr, br, bl = ordered
    w = max(float(np.linalg.norm(tr - tl)), float(np.linalg.norm(br - bl)))
    h = max(float(np.linalg.norm(bl - tl)), float(np.linalg.norm(br - tr)))
    return w, h, max(w, h) / max(min(w, h), 1.0)


def _score_quad(quad, scan_centers_orig, aspect_range, img_area):
    contain_score = 0.0
    if scan_centers_orig:
        quad_pts = quad.reshape(-1, 1, 2).astype(np.float32)
        for cx, cy in scan_centers_orig:
            try:
                if cv2.pointPolygonTest(quad_pts, (float(cx), float(cy)), False) >= 0:
                    contain_score = 2.0
                    break
            except Exception:
                continue

    aspect_score = 0.0
    if aspect_range is not None:
        _, _, aspect = _quad_size_aspect(quad)
        lo, hi = aspect_range
        if lo <= aspect <= hi:
            aspect_score = 1.5
        else:
            aspect_score = max(0.0, 1.0 - min(abs(aspect - lo), abs(aspect - hi)) * 0.6)

    area_score = min(1.0, (cv2.contourArea(quad) / max(img_area, 1.0)) ** 0.5)
    return contain_score + aspect_score + 0.15 * area_score


def _find_box_quads(img_cv, max_candidates=8, min_area_ratio=0.03, lob=None):
    quads = _find_quads_canny(img_cv, max_candidates, min_area_ratio)
    if lob == "Mac":
        quads = quads + _find_quads_brown_split(img_cv, max_candidates, min_area_ratio)
    quads = _dedup_quads(quads)
    quads.sort(key=lambda q: cv2.contourArea(q), reverse=True)
    return quads[:max_candidates]


def rectify_package_box(image_pil: Image.Image, lob=None, scan_polys_orig=None) -> dict:
    img_cv = pil_to_cv(image_pil)
    H_img, W_img = img_cv.shape[:2]

    # 策略 1：多候选四边形 + 正面打分透视矫正
    quads = _find_box_quads(img_cv, lob=lob)
    quad = None
    if quads:
        aspect_range = None
        if lob and lob in LOB_CONFIGS:
            aspect_range = LOB_CONFIGS[lob].get("front_face_aspect_range")

        scan_centers = []
        if scan_polys_orig:
            for poly in scan_polys_orig:
                try:
                    pts = np.array(poly, dtype=float)
                    scan_centers.append((float(pts[:, 0].mean()), float(pts[:, 1].mean())))
                except Exception:
                    continue

        quad = max(quads, key=lambda q: _score_quad(q, scan_centers, aspect_range, float(W_img * H_img)))

    if quad is not None:
        try:
            ordered = _order_quad_corners(quad)
            tl, tr, br, bl = ordered
            W_rect = max(1, int(round(max(
                float(np.linalg.norm(tr - tl)), float(np.linalg.norm(br - bl))
            ))))
            H_rect = max(1, int(round(max(
                float(np.linalg.norm(bl - tl)), float(np.linalg.norm(br - tr))
            ))))
            clip_scale = min(1.0, BOX_DETECT_MAX_SIDE / max(W_rect, H_rect))
            if clip_scale < 1.0:
                W_rect = max(1, int(round(W_rect * clip_scale)))
                H_rect = max(1, int(round(H_rect * clip_scale)))

            dst = np.array([[0, 0], [W_rect - 1, 0],
                            [W_rect - 1, H_rect - 1], [0, H_rect - 1]], dtype=np.float32)
            M = cv2.getPerspectiveTransform(ordered, dst)
            warped = cv2.warpPerspective(img_cv, M, (W_rect, H_rect))

            xs, ys = ordered[:, 0], ordered[:, 1]
            box_x = int(max(0, np.floor(xs.min())))
            box_y = int(max(0, np.floor(ys.min())))
            box_w = int(min(W_img - box_x, np.ceil(xs.max()) - box_x))
            box_h = int(min(H_img - box_y, np.ceil(ys.max()) - box_y))

            return {"warped_img": warped, "M": M, "W_rect": W_rect, "H_rect": H_rect,
                    "method": "perspective", "box_quad_src": ordered.tolist(),
                    "box_x": box_x, "box_y": box_y, "box_w": box_w, "box_h": box_h}
        except Exception as e:
            print(f"  ⚠ 透视矫正异常，降级: {type(e).__name__}: {e}")

    # 策略 2：minAreaRect 旋转矫正
    try:
        scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H_img, W_img))
        dW, dH = max(1, int(W_img * scale)), max(1, int(H_img * scale))
        img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
        filtered = cv2.bilateralFilter(gray, 9, 75, 75)
        edges = cv2.Canny(filtered, 20, 80)
        dilated = cv2.dilate(edges, np.ones((9, 9), np.uint8), iterations=3)
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            cnt = max(contours, key=cv2.contourArea)
            if cv2.contourArea(cnt) >= 0.08 * dW * dH:
                rect = cv2.minAreaRect(cnt)
                box_pts_orig = cv2.boxPoints(rect).astype(np.float32) / scale
                ordered = _order_quad_corners(box_pts_orig)
                tl, tr, br, bl = ordered
                W_rect = max(1, int(round(max(
                    float(np.linalg.norm(tr - tl)), float(np.linalg.norm(br - bl))
                ))))
                H_rect = max(1, int(round(max(
                    float(np.linalg.norm(bl - tl)), float(np.linalg.norm(br - tr))
                ))))
                clip_scale = min(1.0, BOX_DETECT_MAX_SIDE / max(W_rect, H_rect))
                if clip_scale < 1.0:
                    W_rect = max(1, int(round(W_rect * clip_scale)))
                    H_rect = max(1, int(round(H_rect * clip_scale)))
                dst = np.array([[0, 0], [W_rect - 1, 0],
                                [W_rect - 1, H_rect - 1], [0, H_rect - 1]], dtype=np.float32)
                M = cv2.getPerspectiveTransform(ordered, dst)
                warped = cv2.warpPerspective(img_cv, M, (W_rect, H_rect))
                xs, ys = ordered[:, 0], ordered[:, 1]
                box_x = int(max(0, np.floor(xs.min())))
                box_y = int(max(0, np.floor(ys.min())))
                return {"warped_img": warped, "M": M, "W_rect": W_rect, "H_rect": H_rect,
                        "method": "rotation", "box_quad_src": ordered.tolist(),
                        "box_x": box_x, "box_y": box_y,
                        "box_w": int(min(W_img - box_x, np.ceil(xs.max()) - box_x)),
                        "box_h": int(min(H_img - box_y, np.ceil(ys.max()) - box_y))}
    except Exception as e:
        print(f"  ⚠ 旋转矫正异常，降级: {type(e).__name__}: {e}")

    # 策略 3：轴对齐 bbox 兜底（V3 不跳过，继续处理）
    bx, by, bw, bh, _ = detect_box_bbox(image_pil, lob=lob)
    warped = img_cv[by:by + bh, bx:bx + bw].copy()
    return {"warped_img": warped if warped.size > 0 else img_cv,
            "M": None, "W_rect": bw, "H_rect": bh,
            "method": "axis_aligned", "box_quad_src": None,
            "box_x": bx, "box_y": by, "box_w": bw, "box_h": bh}


def transform_polys(polys_orig: list, M, box_x: int = 0, box_y: int = 0) -> list:
    if not polys_orig:
        return []
    out = []
    if M is not None:
        for poly in polys_orig:
            try:
                pts = np.array(poly, dtype=np.float32).reshape(-1, 1, 2)
                warped = cv2.perspectiveTransform(pts, M).reshape(-1, 2)
                out.append([[float(p[0]), float(p[1])] for p in warped])
            except Exception:
                out.append([[float(pt[0]), float(pt[1])] for pt in poly])
    else:
        for poly in polys_orig:
            out.append([[float(pt[0]) - box_x, float(pt[1]) - box_y] for pt in poly])
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 五、非官方贴纸颜色检测
# ═══════════════════════════════════════════════════════════════════════════════

def _filter_color_candidates(candidate_mask, signal_u8, zone_area, color_cfg, detail_prefix):
    area_ratio_min = float(color_cfg.get("area_ratio", 0.015))
    solidity_min   = float(color_cfg.get("solidity_min", 0.45))
    edge_grad_min  = float(color_cfg.get("edge_grad_min", 6.0))

    k5 = np.ones((5, 5), np.uint8)
    mask = cv2.dilate(candidate_mask, k5, iterations=2)
    mask = cv2.erode(mask, k5, iterations=2)

    n_labels, label_img, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    gx = cv2.Sobel(signal_u8, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(signal_u8, cv2.CV_32F, 0, 1, ksize=3)
    grad_mag = np.sqrt(gx**2 + gy**2)

    for label in range(1, n_labels):
        area = int(stats[label, cv2.CC_STAT_AREA])
        ratio = area / max(zone_area, 1)
        if ratio < area_ratio_min:
            continue
        comp_mask = (label_img == label).astype(np.uint8)
        contours, _ = cv2.findContours(comp_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea)
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        if hull_area < 1:
            continue
        if cv2.contourArea(cnt) / hull_area < solidity_min:
            continue
        k3 = np.ones((3, 3), np.uint8)
        boundary = (cv2.dilate(comp_mask, k3) - cv2.erode(comp_mask, k3)).astype(bool)
        mean_edge_grad = float(grad_mag[boundary].mean()) if boundary.any() else 0.0
        if mean_edge_grad < edge_grad_min:
            continue
        return True, (f"{detail_prefix}：面积占比 {ratio:.1%}，"
                      f"紧实度 {cv2.contourArea(cnt)/hull_area:.2f}，"
                      f"边缘梯度 {mean_edge_grad:.1f}")
    return False, ""


def _detect_unofficial_white_box(zone, color_cfg):
    """
    白盒分支：白平衡归一化 + 相对饱和度。

    改进（V3.1）：白色背景像素不足时跳过检测，防止拍摄条件差/透视矫正
    偏差时无法建立白平衡基准所产生的误判。
    """
    sat_above_bg = float(color_cfg.get("sat_above_bg", UNOFFICIAL_SAT_ABOVE_BG))
    v_min, v_max = color_cfg.get("val_range", UNOFFICIAL_VAL_RANGE)

    hsv_f = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV).astype(np.float32)
    s_raw = hsv_f[:, :, 1]
    v_raw = hsv_f[:, :, 2]

    white_mask = (v_raw > 180) & (s_raw < 55)
    white_count = int(white_mask.sum())

    # 白色背景像素不足 → 无法可靠建立白平衡基准，跳过
    if white_count < UNOFFICIAL_WHITE_BG_MIN:
        return False, f"白色背景像素不足({white_count}px)，跳过颜色检测"

    bg_sat_ref = float(np.percentile(s_raw[white_mask], 90))
    eff_sat = np.clip(s_raw - bg_sat_ref - 10.0, 0.0, 255.0)

    candidate = ((eff_sat > sat_above_bg) & (v_raw > v_min) & (v_raw < v_max)).astype(np.uint8) * 255
    eff_sat_u8 = np.clip(eff_sat, 0, 255).astype(np.uint8)
    return _filter_color_candidates(candidate, eff_sat_u8, zone.shape[0] * zone.shape[1],
                                    color_cfg, f"[white_box] 非白色彩色区域 (bg_ref S={bg_sat_ref:.1f})")


def _detect_unofficial_brown_box(zone, color_cfg):
    brown_h_lo, brown_h_hi = color_cfg.get("brown_hue_range", (5, 30))
    brown_sat_min = float(color_cfg.get("brown_sat_min", 30))
    brown_v_lo, brown_v_hi = color_cfg.get("brown_val_range", (40, 200))
    white_sat_max = float(color_cfg.get("white_sat_max", 30))
    white_val_min = float(color_cfg.get("white_val_min", 200))
    sat_min_abs = float(color_cfg.get("sat_min_abs", 80))
    v_min, v_max = color_cfg.get("val_range", (50, 240))

    hsv_f = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV).astype(np.float32)
    h_raw, s_raw, v_raw = hsv_f[:, :, 0], hsv_f[:, :, 1], hsv_f[:, :, 2]

    brown_mask = ((h_raw >= brown_h_lo) & (h_raw <= brown_h_hi) &
                  (s_raw >= brown_sat_min) & (v_raw >= brown_v_lo) & (v_raw <= brown_v_hi))
    white_mask = (s_raw <= white_sat_max) & (v_raw >= white_val_min)
    foreign = (~brown_mask & ~white_mask & (s_raw >= sat_min_abs) &
               (v_raw >= v_min) & (v_raw <= v_max)).astype(np.uint8) * 255

    s_u8 = np.clip(s_raw, 0, 255).astype(np.uint8)
    return _filter_color_candidates(foreign, s_u8, zone.shape[0] * zone.shape[1],
                                    color_cfg, "[brown_box] 非棕非白高饱和异色区域")


def detect_unofficial_sticker_color(warped_img, color_cfg) -> tuple[bool, str]:
    """
    非官方贴纸颜色检测（全图扫描）。

    对整个矫正后的包装盒面做颜色分析，通过 color_cfg 中的 area_ratio
    阈值来过滤盒面官方印刷元素（如 AirPods 紫色授权条带 ~9%、绿色回收
    箭头等）。各 LOB 根据自身盒面印刷特点配置不同的 area_ratio：
      • iPhone / Watch / iPad / Accy.：0.020（印刷元素少，对小贴纸敏感）
      • AirPods：0.120（官方印刷彩色元素可达 ~10%，阈值需高于此值）
    """
    if not color_cfg or not color_cfg.get("enabled", False):
        return False, "颜色检测已跳过"
    try:
        if warped_img is None or warped_img.size == 0:
            return False, ""
        H, W = warped_img.shape[:2]
        if H < 10 or W < 10:
            return False, ""
        mx, my = max(1, int(W * 0.02)), max(1, int(H * 0.02))
        zone = warped_img[my:H - my, mx:W - mx]
        if zone.shape[0] < 10 or zone.shape[1] < 10:
            return False, ""
        if color_cfg.get("mode") == "brown_box":
            return _detect_unofficial_brown_box(zone, color_cfg)
        return _detect_unofficial_white_box(zone, color_cfg)
    except Exception as e:
        return False, f"颜色检测异常: {type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# 六、贴纸定位与位置验证
# ═══════════════════════════════════════════════════════════════════════════════

# 扫码贴锚点：原为精确匹配"扫码即领"，但 OCR 常把"领"认错（锁/顿）或把四字切断，
# 导致整条漏检 → 找不到背面图 → 封口贴误判为 0（Mac 因贴纸小尤为频繁）。
# 放宽为前缀"扫码即"3 字：足够唯一（不会误匹配"扫码支付"等），覆盖末字认错的情况。
SCAN_ANCHOR = "扫码即"


def is_scan_text(text: str) -> bool:
    return SCAN_ANCHOR in text


def find_all_scan_stickers(texts: list[str], polys: list) -> list[dict]:
    stickers = []
    for i, text in enumerate(texts):
        if is_scan_text(text) and i < len(polys):
            try:
                poly = np.array(polys[i], dtype=float)
                x1, y1 = float(poly[:, 0].min()), float(poly[:, 1].min())
                x2, y2 = float(poly[:, 0].max()), float(poly[:, 1].max())
                stickers.append({
                    "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "text_idx": i,
                })
            except Exception:
                continue
    return stickers


def _scan_sticker_distance_to_zone(rel_x, rel_y, scan_position_cfg) -> float:
    zones = _normalize_position_cfg(scan_position_cfg)
    if not zones:
        return float("inf")
    best = float("inf")
    for zone in zones:
        x_min = zone.get("x_min", -float("inf"))
        x_max = zone.get("x_max",  float("inf"))
        y_min = zone.get("y_min", -float("inf"))
        y_max = zone.get("y_max",  float("inf"))
        dx = max(0.0, x_min - rel_x, rel_x - x_max)
        dy = max(0.0, y_min - rel_y, rel_y - y_max)
        d = (dx * dx + dy * dy) ** 0.5
        if d < best:
            best = d
    return best


def pick_best_scan_sticker(scan_stickers, W_rect, H_rect, scan_position_cfg):
    if not scan_stickers:
        return None
    if scan_position_cfg is None or W_rect <= 0 or H_rect <= 0:
        return scan_stickers[0]
    def _key(s):
        rel_x = s["cx"] / W_rect
        rel_y = s["cy"] / H_rect
        return (_scan_sticker_distance_to_zone(rel_x, rel_y, scan_position_cfg), rel_y)
    return min(scan_stickers, key=_key)


def validate_sticker_position(rel_cx, rel_cy, position_cfg,
                               tolerance: float = STICKER_POSITION_TOLERANCE) -> dict:
    zones = _normalize_position_cfg(position_cfg)
    if not zones:
        return {"in_correct_position": False,
                "rel_x": round(rel_cx, 4) if rel_cx is not None else None,
                "rel_y": round(rel_cy, 4) if rel_cy is not None else None,
                "x_ok": False, "y_ok": False,
                "detail": "位置验证跳过（该 LOB 未配置规范位置）"}

    tol = max(float(tolerance), 0.0)

    def _check_zone(zone):
        x_min = zone.get("x_min", -float("inf"))
        x_max = zone.get("x_max",  float("inf"))
        y_min = zone.get("y_min", -float("inf"))
        y_max = zone.get("y_max",  float("inf"))
        x_lo = x_min - tol if x_min != -float("inf") else x_min
        x_hi = x_max + tol if x_max !=  float("inf") else x_max
        y_lo = y_min - tol if y_min != -float("inf") else y_min
        y_hi = y_max + tol if y_max !=  float("inf") else y_max
        return (x_lo <= rel_cx <= x_hi), (y_lo <= rel_cy <= y_hi), (x_min, x_max, y_min, y_max)

    best_fail = None
    for zone in zones:
        x_ok, y_ok, bounds = _check_zone(zone)
        if x_ok and y_ok:
            return {"in_correct_position": True,
                    "rel_x": round(rel_cx, 4), "rel_y": round(rel_cy, 4),
                    "x_ok": True, "y_ok": True,
                    "detail": f"位置规范 (rel_x={rel_cx:.3f}, rel_y={rel_cy:.3f}, 容差±{tol:.2f})"}
        score = int(x_ok) + int(y_ok)
        if best_fail is None or score > best_fail[0]:
            best_fail = (score, x_ok, y_ok, bounds)

    _, x_ok, y_ok, (x_min, x_max, y_min, y_max) = best_fail
    parts = []
    if not x_ok:
        parts.append(f"X={rel_cx:.3f} 不在 [{x_min},{x_max}]±{tol:.2f}")
    if not y_ok:
        parts.append(f"Y={rel_cy:.3f} 不在 [{y_min},{y_max}]±{tol:.2f}")
    multi = " (多区域任一)" if len(zones) > 1 else ""
    return {"in_correct_position": False,
            "rel_x": round(rel_cx, 4), "rel_y": round(rel_cy, 4),
            "x_ok": x_ok, "y_ok": y_ok,
            "detail": f"位置异常{multi}：" + "；".join(parts)}


def find_all_auth_stickers_in_box(texts, polys_rect, W_rect=0, H_rect=0):
    AUTH_KW = ["Apple授权专营店", "授权专营店", "在你身边"]
    results = []
    use_box_filter = W_rect > 0 and H_rect > 0
    for i, text in enumerate(texts):
        if any(kw in text for kw in AUTH_KW) and i < len(polys_rect):
            try:
                poly = np.array(polys_rect[i], dtype=float)
                x1, y1 = float(poly[:, 0].min()), float(poly[:, 1].min())
                x2, y2 = float(poly[:, 0].max()), float(poly[:, 1].max())
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                if use_box_filter and not (0 <= cx <= W_rect and 0 <= cy <= H_rect):
                    continue
                results.append({"cx": cx, "cy": cy,
                                 "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                 "text_idx": i, "matched_text": text})
            except Exception:
                continue
    return results


def check_auth_sticker_position(texts, polys_rect, W_rect, H_rect, position_cfg) -> dict:
    if position_cfg is None:
        return {"found": False, "in_correct_position": False,
                "rel_x": None, "rel_y": None, "detail": "该 LOB 无二贴规范"}

    candidates = find_all_auth_stickers_in_box(texts, polys_rect, W_rect, H_rect)
    if not candidates:
        return {"found": False, "in_correct_position": False,
                "rel_x": None, "rel_y": None,
                "detail": "未找到'Apple授权专营店'贴纸（盒子内）"}

    first_rel = None
    for auth in candidates:
        rel_x = auth["cx"] / W_rect if W_rect > 0 else -1.0
        rel_y = auth["cy"] / H_rect if H_rect > 0 else -1.0
        v = validate_sticker_position(rel_x, rel_y, position_cfg)
        if first_rel is None:
            first_rel = (v["rel_x"], v["rel_y"])
        if v["in_correct_position"]:
            return {"found": True, "in_correct_position": True,
                    "rel_x": v["rel_x"], "rel_y": v["rel_y"],
                    "detail": f"Apple授权专营店位置规范 (rel_x={v['rel_x']:.3f}, rel_y={v['rel_y']:.3f})"}

    rel_x, rel_y = first_rel
    return {"found": True, "in_correct_position": False,
            "rel_x": rel_x, "rel_y": rel_y,
            "detail": "Apple授权专营店位置异常"}


def check_dual_sticker_status(texts, polys, img_h, sticker_count_mode="single_or_dual") -> dict:
    scan_stickers = find_all_scan_stickers(texts, polys)
    has_auth_raw = any(
        kw in text
        for text in texts
        for kw in ["Apple授权专营店", "授权专营店", "在你身边"]
    )

    MIN_Y_GAP = max(img_h * 0.20, 50)
    distinct = []
    for s in scan_stickers:
        if not any(abs(s["cy"] - e["cy"]) < MIN_Y_GAP for e in distinct):
            distinct.append(s)
    scan_count = len(distinct)

    if sticker_count_mode == "single_only":
        if scan_count >= 2:
            return {"scan_count": scan_count, "has_auth": False,
                    "dual_code": 2, "dual_detail": f"错误：检测到{scan_count}个'扫码即领'（该 LOB 仅允许单贴）"}
        elif scan_count == 1:
            return {"scan_count": scan_count, "has_auth": False,
                    "dual_code": 0, "dual_detail": "单贴：仅'扫码即领'"}
        else:
            return {"scan_count": 0, "has_auth": False,
                    "dual_code": -1, "dual_detail": "未找到'扫码即领'贴纸"}

    if scan_count >= 2:
        return {"scan_count": scan_count, "has_auth": has_auth_raw,
                "dual_code": 2, "dual_detail": f"检测到{scan_count}个'扫码即领'，上下均为扫码贴"}
    elif scan_count == 1 and has_auth_raw:
        return {"scan_count": 1, "has_auth": True,
                "dual_code": 1, "dual_detail": "合规双贴：'扫码即领' + 'Apple授权专营店'"}
    elif scan_count == 1:
        return {"scan_count": 1, "has_auth": False,
                "dual_code": 0, "dual_detail": "单贴：仅'扫码即领'"}
    else:
        return {"scan_count": 0, "has_auth": has_auth_raw,
                "dual_code": -1, "dual_detail": "未找到'扫码即领'贴纸"}


def is_flat_sticker(texts, polys, anchor_idx, box_x=0, box_y=0, box_w=0, box_h=0):
    """平铺错误检测：检测端片语义信号（Authorized Reseller 或远端授权经销商大字）"""
    if anchor_idx >= len(polys):
        return False, "锚点索引越界"

    use_box_filter = box_w > 0 and box_h > 0
    anchor_pts    = np.array(polys[anchor_idx], dtype=float)
    anchor_center = anchor_pts.mean(axis=0)
    rect_a        = cv2.minAreaRect(anchor_pts.astype(np.float32))
    anchor_h      = max(min(rect_a[1]), 1.0)

    for i, text in enumerate(texts):
        if i == anchor_idx or i >= len(polys):
            continue
        pts    = np.array(polys[i], dtype=float)
        center = pts.mean(axis=0)

        if use_box_filter:
            cx, cy = float(center[0]), float(center[1])
            if not (box_x <= cx <= box_x + box_w and box_y <= cy <= box_y + box_h):
                continue

        dist = float(np.linalg.norm(center - anchor_center))
        t_lower = text.lower().strip()

        if ("authorized" in t_lower or "authorised" in t_lower) and "reseller" in t_lower:
            if dist > anchor_h * 2:
                return True, f"检测到端片标识 'Authorized Reseller'（距锚点{dist:.0f}px）"

        if "授权经销商" in text and not ("扫码" in text or "Apple" in text):
            rect_i = cv2.minAreaRect(pts.astype(np.float32))
            text_h = max(min(rect_i[1]), 1.0)
            if text_h > anchor_h * 0.8 and dist > anchor_h * 5:
                return True, f"检测到远端'授权经销商'大字（距锚点{dist:.0f}px）"

    return False, "未检测到端片文字，贴纸未平铺"


# ═══════════════════════════════════════════════════════════════════════════════
# 七、水印提取
# ═══════════════════════════════════════════════════════════════════════════════

def parse_watermark_text(text_segments: list[str]) -> tuple[str, str]:
    time_pattern    = re.compile(r'\d{1,2}:\d{2}')
    date_pattern    = re.compile(r'\d{4}[-–\-]\d{2}[-–\-]\d{2}')
    weekday_pattern = re.compile(r'星期[一二三四五六日]')
    separator_pat   = re.compile(r'^[|｜\s]+$')

    time_parts, location_parts = [], []
    for seg in text_segments:
        seg = seg.strip()
        if not seg or separator_pat.match(seg):
            continue
        if (time_pattern.search(seg) or date_pattern.search(seg)
                or weekday_pattern.search(seg)):
            time_parts.append(re.sub(r'[|｜]', ' ', seg).strip())
        else:
            location_parts.append(seg)

    time_str = re.sub(r"\s+", " ", " ".join(time_parts)).strip()
    clean_loc = [
        p for p in location_parts
        if p and (sum(1 for c in p if chr(0x4E00) <= c <= chr(0x9FFF))
                  / max(len(p.replace(" ", "")), 1)) >= 0.4
    ]
    return time_str, re.sub(r"\s+", " ", " ".join(clean_loc)).strip()


def extract_watermark_crop(image: Image.Image, image_id: str) -> tuple[str, str]:
    if image is None:
        return "", ""
    try:
        w, h = image.size
        crop = image.crop((0, int(h * 0.82), int(w * 0.60), h))
        # 水印字大且清晰，无需 2000px。检测器默认 limit_type='min' 会把这条
        # 2177×872 的小图短边上采样到 2000（≈5000×2000，等于又跑一次全图 OCR）。
        # 改用 limit_type='max' + 1280：只缩不放，水印仍能识别，单张 ~3.2s → ~0.45s。
        result = ocr.predict(
            input=pil_to_cv(crop),
            text_det_limit_side_len=1280, text_det_limit_type='max',
        )
        texts = []
        if result and len(result) > 0:
            r = result[0]
            if hasattr(r, 'json'):
                texts = r.json.get('res', {}).get('rec_texts', [])
        return parse_watermark_text(texts)
    except Exception as e:
        print("  水印OCR异常:", type(e).__name__, str(e)[:80])
        return "", ""


# ═══════════════════════════════════════════════════════════════════════════════
# 八、保存
# ═══════════════════════════════════════════════════════════════════════════════

def save_result_immediately(result_dict: dict, csv_file: str, json_file: str):
    df_row = pd.DataFrame([result_dict])
    if not os.path.exists(csv_file):
        df_row.to_csv(csv_file, index=False, mode='w', encoding='utf-8-sig')
    else:
        df_row.to_csv(csv_file, index=False, mode='a', header=False, encoding='utf-8-sig')
    with open(json_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result_dict, ensure_ascii=False) + '\n')


# ═══════════════════════════════════════════════════════════════════════════════
# 九、行处理（V3 核心逻辑）
# ═══════════════════════════════════════════════════════════════════════════════

def _make_result(is_compliant, seal_exists, position_valid,
                 rel_x, rel_y, box_method, detail,
                 dual_code, dual_detail,
                 watermark_time, watermark_location,
                 unofficial_sticker: int = 0,
                 lob: str = "",
                 rectify_method: str = "",
                 box_quad_src=None,
                 unofficial_color_checked: int = 0,
                 unofficial_color_mode: str = "") -> dict:
    return {
        "is_compliant":             is_compliant,
        "seal_exists":              seal_exists,
        "unofficial_sticker":       unofficial_sticker,
        "position_valid":           position_valid,
        "rel_x":                    rel_x,
        "rel_y":                    rel_y,
        "box_method":               box_method,
        "detail":                   detail,
        "dual_code":                dual_code,
        "dual_detail":              dual_detail,
        "watermark_time":           watermark_time,
        "watermark_location":       watermark_location,
        "lob":                      lob,
        "rectify_method":           rectify_method,
        "box_quad_src":             box_quad_src,
        "unofficial_color_checked": unofficial_color_checked,
        "unofficial_color_mode":    unofficial_color_mode,
    }


def process_row(row, idx: int, total: int, prefetched_tasks=None) -> dict:
    """
    V3 简化版行处理：
      Step 1  找第一张含"扫码即领"的图 → 背面（不强求包装盒检测通过）
      Step 2  对背面图做非官方贴纸颜色检测
      Step 3  位置验证（放宽容差 STICKER_POSITION_TOLERANCE=0.25，无角度约束）
    """
    print(f"\n{'='*80}")
    print(f"处理第 {idx}/{total} 行 (订单号: {row.get('订单号', 'N/A')})")
    print('=' * 80)

    # Phase 0: LOB 识别
    lob = detect_lob(row)
    if lob == UNRECOGNIZED_LOB:
        print(f"  LOB: {lob} — 直接标记不合格")
        r = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=None,
            detail=f"{UNRECOGNIZED_LOB}: Excel LOB 列缺失或不在枚举内",
            dual_code=-1, dual_detail="跳过",
            watermark_time="", watermark_location="", lob=lob,
        )
        _print_summary(r)
        return r

    lob_cfg   = LOB_CONFIGS[lob]
    scan_cfg  = lob_cfg["scan_sticker"]
    auth_cfg  = lob_cfg.get("auth_sticker")
    color_cfg = lob_cfg.get("unofficial_color", {"enabled": False})
    sc_mode   = lob_cfg.get("sticker_count", "single_or_dual")

    print(f"  LOB: {lob}  (sticker_count={sc_mode})")

    watermark_time, watermark_location = "", ""
    watermark_extracted = False
    tasks = prefetched_tasks if prefetched_tasks is not None else submit_row_downloads(row)

    # Phase 1: 找第一张含"扫码即领"的图作为背面
    # V3 改动：找到即停，不再扫描所有图片
    back = None
    for col_idx, col, url, future in tasks:
        print(f"\n  第{col_idx}张图片: {url[:80]}...")
        image = future.result()
        if image is None:
            print(f"  → 下载失败，跳过")
            continue

        print(f"  图片尺寸: {image.size}")
        image_id = f"row{idx}_col{col_idx}"

        if not watermark_extracted:
            wm_time, wm_loc = extract_watermark_crop(image, image_id)
            watermark_time, watermark_location = wm_time, wm_loc
            watermark_extracted = True
            print(f"  水印时间: {wm_time or '(未识别)'}")
            print(f"  水印地点: {wm_loc or '(未识别)'}")

        full_text, texts, polys_orig, orig_h, orig_w = ocr_image_full(image, image_id)
        print(f"  识别文字: {full_text[:120]}{'...' if len(full_text) > 120 else ''}")

        if any(is_scan_text(t) for t in texts):
            back = {
                "image": image, "texts": texts, "polys_orig": polys_orig,
                "orig_h": orig_h, "orig_w": orig_w,
            }
            print(f"  → ✓ 检测到'扫码即领'，确认为背面，停止扫描")
            break
        else:
            print(f"  → 未检测到'扫码即领'，继续下一张")

    # 无背面图 → 不合格
    if back is None:
        print(f"  → 未找到背面图")
        r = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=None,
            detail="未找到含'扫码即领'的背面图",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob,
        )
        _print_summary(r)
        return r

    # Phase 2: 包装盒透视矫正（V3: fallback 不跳过，继续处理）
    scan_polys_for_rect = [
        back["polys_orig"][i]
        for i, t in enumerate(back["texts"])
        if is_scan_text(t) and i < len(back["polys_orig"])
    ]
    rectify      = rectify_package_box(back["image"], lob=lob, scan_polys_orig=scan_polys_for_rect)
    rect_method  = rectify["method"]
    warped_img   = rectify["warped_img"]
    M            = rectify["M"]
    W_rect       = int(rectify["W_rect"])
    H_rect       = int(rectify["H_rect"])
    box_quad_src = rectify["box_quad_src"]

    print(f"  矫正方式: {rect_method}  矫正尺寸: {W_rect}×{H_rect}")

    # Phase 2.5: 将 OCR 多边形提前映射到矫正坐标系
    # （提前到颜色检测前，供排除官方印刷区域掩码使用）
    polys_rect = transform_polys(
        back["polys_orig"], M,
        box_x=rectify["box_x"] if M is None else 0,
        box_y=rectify["box_y"] if M is None else 0,
    )

    # Phase 2.6: 非官方贴纸颜色检测（仅对背面图，排除官方印刷文字区域）
    unofficial_sticker = 0
    color_checked      = 0
    color_mode         = ""

    if color_cfg.get("enabled", False):
        color_mode    = color_cfg.get("mode", "white_box")
        color_checked = 1
        has_unoff, unoff_detail = detect_unofficial_sticker_color(warped_img, color_cfg)
        if has_unoff:
            unofficial_sticker = 1
            print(f"  ⚠ 颜色检测命中非官方贴纸：{unoff_detail}")
            r = _make_result(
                is_compliant=0, seal_exists=1, position_valid=4,
                rel_x=None, rel_y=None, box_method=rect_method,
                detail=f"检测到疑似非官方贴纸：{unoff_detail}",
                dual_code=-1, dual_detail="跳过",
                watermark_time=watermark_time, watermark_location=watermark_location,
                unofficial_sticker=1,
                lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
                unofficial_color_checked=1, unofficial_color_mode=color_mode,
            )
            _print_summary(r)
            return r
        else:
            print(f"  颜色检测 ({color_mode})：未发现非官方贴纸")
    else:
        print(f"  颜色检测：LOB={lob} enabled=False，跳过")

    # Phase 3: 位置验证（放宽容差 {STICKER_POSITION_TOLERANCE}，无角度约束）
    # polys_rect 已在 Phase 2.5 计算完毕
    scan_candidates = find_all_scan_stickers(back["texts"], polys_rect)
    sticker_rect    = pick_best_scan_sticker(scan_candidates, W_rect, H_rect, scan_cfg)

    if sticker_rect is None:
        r = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=rect_method,
            detail="矫正坐标系中未能定位'扫码即领'贴纸",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    rel_x = sticker_rect["cx"] / W_rect if W_rect > 0 else -1.0
    rel_y = sticker_rect["cy"] / H_rect if H_rect > 0 else -1.0

    pos = validate_sticker_position(rel_x, rel_y, scan_cfg,
                                    tolerance=STICKER_POSITION_TOLERANCE)

    # 平铺检测（无论位置是否合规都先检测，优先给出更准确原因）
    flat, flat_detail = is_flat_sticker(
        back["texts"], polys_rect, sticker_rect["text_idx"],
        0, 0, W_rect, H_rect,
    )

    if not pos["in_correct_position"]:
        pv     = 2 if flat else 0
        detail = flat_detail if flat else f"[{rect_method}] {pos['detail']}"
        r = _make_result(
            is_compliant=0, seal_exists=1, position_valid=pv,
            rel_x=pos["rel_x"], rel_y=pos["rel_y"],
            box_method=rect_method, detail=detail,
            dual_code=-1, dual_detail="单贴不合规，跳过双贴检测",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    if flat:
        r = _make_result(
            is_compliant=0, seal_exists=1, position_valid=2,
            rel_x=pos["rel_x"], rel_y=pos["rel_y"],
            box_method=rect_method, detail=f"[{rect_method}] {flat_detail}",
            dual_code=-1, dual_detail="平铺错误",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    # Phase 4: 双贴检测（单贴位置合规后）
    dual = check_dual_sticker_status(
        back["texts"], polys_rect,
        H_rect if H_rect > 0 else back["orig_h"],
        sticker_count_mode=sc_mode,
    )

    if dual["dual_code"] == 2:
        # 两张扫码贴：位置规范，视为合规
        r = _make_result(
            is_compliant=1, seal_exists=1, position_valid=1,
            rel_x=pos["rel_x"], rel_y=pos["rel_y"],
            box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
            dual_code=2, dual_detail=dual["dual_detail"],
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    if dual["dual_code"] == 3:
        r = _make_result(
            is_compliant=0, seal_exists=1, position_valid=1,
            rel_x=pos["rel_x"], rel_y=pos["rel_y"],
            box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
            dual_code=3, dual_detail=dual["dual_detail"],
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    if dual["has_auth"] and auth_cfg is not None:
        auth_pos = check_auth_sticker_position(
            back["texts"], polys_rect, W_rect, H_rect, auth_cfg,
        )
        if not auth_pos["found"]:
            dual = {**dual, "dual_code": 0,
                    "dual_detail": "单贴：'Apple授权专营店'在盒子外，忽略"}
        elif not auth_pos["in_correct_position"]:
            r = _make_result(
                is_compliant=0, seal_exists=1, position_valid=1,
                rel_x=pos["rel_x"], rel_y=pos["rel_y"],
                box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
                dual_code=1, dual_detail=f"双贴第二张位置异常：{auth_pos['detail']}",
                watermark_time=watermark_time, watermark_location=watermark_location,
                lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
                unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
            )
            _print_summary(r)
            return r
        else:
            r = _make_result(
                is_compliant=1, seal_exists=1, position_valid=1,
                rel_x=pos["rel_x"], rel_y=pos["rel_y"],
                box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
                dual_code=1, dual_detail=f"双贴合规：{auth_pos['detail']}",
                watermark_time=watermark_time, watermark_location=watermark_location,
                lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
                unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
            )
            _print_summary(r)
            return r

    # 单贴合规
    r = _make_result(
        is_compliant=1, seal_exists=1, position_valid=1,
        rel_x=pos["rel_x"], rel_y=pos["rel_y"],
        box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
        dual_code=dual["dual_code"], dual_detail=dual["dual_detail"],
        watermark_time=watermark_time, watermark_location=watermark_location,
        lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
        unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
    )
    _print_summary(r)
    return r


def _print_summary(r: dict):
    print(f"\n【结果汇总】")
    print(f"  LOB             : {r.get('lob', '')}")
    print(f"  是否规范粘贴    : {'✓ 合规(1)' if r['is_compliant'] == 1 else '✗ 不合规(0)'}")
    print(f"  封口贴存在      : {r['seal_exists']}")
    print(f"  非官方贴纸      : {r['unofficial_sticker']}")
    print(f"  位置规范        : {r['position_valid']}")
    print(f"  贴纸相对X/Y     : {r['rel_x']} / {r['rel_y']}")
    print(f"  矫正方式        : {r.get('rectify_method', '')}  包装盒方式: {r['box_method']}")
    print(f"  颜色检测        : checked={r.get('unofficial_color_checked', 0)}  mode={r.get('unofficial_color_mode', '')}")
    print(f"  说明            : {r['detail']}")
    print(f"  双贴纸状态      : {r['dual_code']}  ({r['dual_detail']})")
    print(f"  水印时间        : {r['watermark_time'] or '(未识别)'}")
    print(f"  水印地点        : {r['watermark_location'] or '(未识别)'}")


# ═══════════════════════════════════════════════════════════════════════════════
# 十、主流程
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    input_file   = '/home/ubuntu/OCR/W12.xlsx'
    output_csv   = '/home/ubuntu/OCR/MT&MP&eleme&TM W12_results.csv'
    output_json  = '/home/ubuntu/OCR/MT&MP&eleme&TM W12_results.jsonl'
    output_excel = '/home/ubuntu/OCR/MT&MP&eleme&TM W12_processed.xlsx'

    NEW_COLS = [
        '识别LOB',             # iPhone / Watch / AirPods / Accy. / iPad / Mac
        '是否规范粘贴',        # 0=不合规 | 1=合规
        '封口贴存在',          # 0 / 1
        '是否存在非官方贴纸',  # 0=无 | 1=检出非官方贴纸（新增列）
        '贴纸位置规范',        # -1=无贴纸 | 0=位置异常 | 1=位置规范 | 2=平铺错误 | 4=非官方贴纸
        '贴纸相对X',
        '贴纸相对Y',
        '包装盒检测方式',
        '矫正方式',
        '包装盒四点坐标',
        '颜色检测已执行',
        '颜色检测模式',
        '位置说明',
        '双贴纸状态',          # -1=无贴 | 0=单贴 | 1=双贴合规 | 2=两扫码贴 | 3=缺二贴
        '双贴纸说明',
        '时间',
        '地点',
    ]

    print(f"正在读取Excel: {input_file}")
    df = pd.read_excel(input_file, dtype=_ID_DTYPE)
    raw_rows = len(df)
    # 剔除“幽灵行”：源文件常因某列(如 HQ Name)被一路填充到末尾，把 Excel 已使用
    # 区域撑大，pandas 会把这些只有个别列、订单号为空的空行也读进来。仅保留订单号
    # 非空的真实数据行。
    df = df[df['订单号'].notna() & (df['订单号'].astype(str).str.strip() != '')].reset_index(drop=True)
    if len(df) != raw_rows:
        print(f"已剔除 {raw_rows - len(df)} 行空行(订单号为空)")
    print(f"总共 {len(df)} 行数据")

    # 断点续传
    if os.path.exists(output_csv):
        print(f"\n发现已有结果文件: {output_csv}")
        df_existing = pd.read_csv(output_csv, encoding='utf-8-sig', dtype=_ID_DTYPE)
        processed_orders = set(df_existing['订单号'].astype(str))
        print(f"已处理 {len(processed_orders)} 行，继续处理剩余行...")
    else:
        processed_orders = set()
        pd.DataFrame(columns=list(df.columns) + NEW_COLS).to_csv(
            output_csv, index=False, mode='w', encoding='utf-8-sig'
        )

    pending = [
        (idx, row)
        for idx, row in df.iterrows()
        if str(row.get('订单号', '')) not in processed_orders
    ]

    total_rows  = len(df)
    start_time  = time.time()

    # 流水线预下载（预取 3 行）
    PREFETCH_ROWS = 5
    prefetch_cache: dict[int, list] = {}

    def ensure_prefetched(target_pi: int):
        end = min(target_pi + PREFETCH_ROWS + 1, len(pending))
        for pi in range(target_pi, end):
            pidx, prow = pending[pi]
            if pidx not in prefetch_cache:
                prefetch_cache[pidx] = submit_row_downloads(prow)

    ensure_prefetched(0)

    for pi, (idx, row) in enumerate(pending):
        ensure_prefetched(pi + 1)
        tasks     = prefetch_cache.pop(idx, None)
        order_id  = str(row.get('订单号', ''))

        try:
            r = process_row(row, idx + 1, total_rows, prefetched_tasks=tasks)

            result_row = row.to_dict()
            result_row['识别LOB']            = r.get('lob', '')
            result_row['是否规范粘贴']       = r['is_compliant']
            result_row['封口贴存在']         = r['seal_exists']
            result_row['是否存在非官方贴纸'] = r.get('unofficial_sticker', 0)
            result_row['贴纸位置规范']       = r['position_valid']
            result_row['贴纸相对X']          = r['rel_x']
            result_row['贴纸相对Y']          = r['rel_y']
            result_row['包装盒检测方式']     = r['box_method']
            result_row['矫正方式']           = r.get('rectify_method', '')
            result_row['包装盒四点坐标']     = (
                json.dumps(r['box_quad_src'], ensure_ascii=False)
                if r.get('box_quad_src') else ''
            )
            result_row['颜色检测已执行']     = r.get('unofficial_color_checked', 0)
            result_row['颜色检测模式']       = r.get('unofficial_color_mode', '')
            result_row['位置说明']           = r['detail']
            result_row['双贴纸状态']         = r['dual_code']
            result_row['双贴纸说明']         = r['dual_detail']
            result_row['时间']               = r['watermark_time']
            result_row['地点']               = r['watermark_location']

            save_result_immediately(result_row, output_csv, output_json)

        except Exception as e:
            print(f"  ✗ 处理异常 (订单号: {order_id}): {type(e).__name__}: {e}")
            print(traceback.format_exc())
            error_row = row.to_dict()
            for col in NEW_COLS:
                error_row[col] = f"ERROR: {str(e)[:80]}" if col == '位置说明' else None
            save_result_immediately(error_row, output_csv, output_json)

        elapsed = time.time() - start_time
        done    = pi + 1
        remain  = len(pending) - done
        eta_s   = (elapsed / done * remain) if done > 0 else 0
        print(f"\n  进度: {done}/{len(pending)} | 已用时 {elapsed/60:.1f}min | "
              f"预计剩余 {eta_s/60:.1f}min")

    # 生成最终 Excel
    print(f"\n正在生成 Excel: {output_excel}")
    df_final = pd.read_csv(output_csv, encoding='utf-8-sig', dtype=_ID_DTYPE)
    with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
        df_final.to_excel(writer, index=False, sheet_name='结果')
    print(f"✓ 完成！共处理 {len(df_final)} 行，输出: {output_excel}")


if __name__ == '__main__':
    main()
