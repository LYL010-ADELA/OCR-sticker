#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量OCR识别 V2 —— 基于包装盒坐标系的封口贴位置验证

改进逻辑（彻底替代纯文字组合匹配）：
  1. 用 OpenCV 检测图片中的产品包装盒边界框（三级降级策略）
  2. 用 PaddleOCR 定位"扫码即领"文字的多边形坐标（换算回原图尺寸）
  3. 将贴纸中心转换为包装盒相对坐标系 (0.0~1.0)
  4. 验证贴纸是否在规范位置：宽度 60%~95%，高度 0%~30%
 ▼ Step 0  非官方贴纸检测（detect_unofficial_sticker_color）
           检测区域：整个包装盒（内缩 2% 边距）—— 不限制 rel_x / rel_y
           【改进版：包装盒颜色归一化 + 形状/边缘验证】
           ① 白平衡归一化：采样盒面高亮低饱和像素估计光照色偏基准（bg_sat_ref），
              有效饱和度 = 原始S − bg_sat_ref − 10，消除阴影/灯光带来的全局色偏
           ② 高效饱和掩码：有效饱和度 > 55 且亮度在合理范围 → 候选彩色像素
           ③ 形状紧实度（Solidity）过滤：连通区 Solidity < 0.45 → 阴影/反光，跳过
           ④ 边缘梯度过滤：边界处饱和度梯度 < 6 → 渐变色偏（阴影），跳过
           ┌─ 未检出合规彩色区    → 继续
           └─ 检出合规彩色区      → position_valid=4，立即返回（疑似经销商自贴）
根因修复：
  - 原方案同时检测"扫码即领"+"授权经销商"误判的原因：
    sticker 的 Apple Logo 中心本身印有"授权经销商"，
    正常贴合时也会触发"错误"标志 —— 本版本彻底废弃该逻辑。

输出新增列：
  贴纸存在(0/1) | 贴纸位置规范(1=规范/0=位置异常/2=平铺错误/-1=无贴纸) |
  贴纸相对X | 贴纸相对Y | 包装盒检测方式 | 位置说明

position_valid 含义：
  -1 : 未找到封口贴（OCR未检测到"扫码即领"）
   0 : 贴纸位置不符合规范
   1 : 贴纸位置规范（合格）
   2 : 贴纸"平铺"错误——整条贴条铺在一个面，
       端片（Authorized Reseller）未绕到侧面，未实现封口目的。
       判断依据：贴纸文字簇的宽高比 > FLAT_STICKER_ASPECT_RATIO。
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
from concurrent.futures import ThreadPoolExecutor, Future

# ─── 可调参数 ────────────────────────────────────────────────────────────────
IMAGE_COLUMNS    = ['图片地址', 'Unnamed: 17', 'Unnamed: 18', 'Unnamed: 19']
DOWNLOAD_WORKERS = 8     # 并行下载线程数
PREFETCH_ROWS    = 3     # 向前预下载的行数

# ── "扫码即领"贴纸规范位置（相对于包装盒坐标系，左上角为原点）──────────────
STICKER_X_MIN = 0.50   # 贴纸中心 X 坐标下限（盒子宽度方向）
STICKER_X_MAX = 0.95   # 贴纸中心 X 坐标上限
STICKER_Y_MIN = 0.00   # 贴纸中心 Y 坐标下限（盒子高度方向，从上往下）
STICKER_Y_MAX = 0.30   # 贴纸中心 Y 坐标上限

# ── "Apple授权专营店"贴纸规范位置（双贴第二张，底部区域）─────────────────────
AUTH_X_MIN = 0.50      # 右侧 50%~95%（与上贴纸对齐）
AUTH_X_MAX = 0.95
AUTH_Y_MIN = 0.70      # 底部 30%（从上往下 70%~100%）
AUTH_Y_MAX = 1.00

# ── 正向完整拍摄判断阈值 ──────────────────────────────────────────────────────
# 包装盒面积 / 图片面积 >= 此值才视为"正向完整"拍摄
BOX_FRONTAL_MIN_RATIO = 0.20

# 平铺错误检测参数（已改为语义信号，此处保留供注释说明）
FLAT_STICKER_ASPECT_RATIO = 4.0   # 保留，供扩展用
FLAT_STICKER_WIDTH_RATIO  = 0.65

# ── 贴纸粘贴角度验证 ──────────────────────────────────────────────────────────
# 贴纸长轴与包装盒水平方向的最大允许偏角（门店拍照允许轻微倾斜）
# 30° 为业务推荐值；可根据误杀率调整
STICKER_ANGLE_MAX_DEG = 30.0

# ── 非官方贴纸（经销商自贴）颜色检测 ─────────────────────────────────────────
# 改进版：先对包装盒做白平衡归一化，再检测"相对于背景"的超饱和区域。
# 归一化消除门店灯光色偏、阴影等环境因素，避免误判。
# 额外校验：形状紧实度（Solidity）+ 边缘清晰度（梯度），进一步排除阴影。
UNOFFICIAL_SAT_ABOVE_BG  = 55   # 归一化后有效饱和度阈值（相对于背景基准）
UNOFFICIAL_VAL_RANGE     = (40, 230)   # 亮度范围，排除纯黑/过曝
UNOFFICIAL_AREA_RATIO    = 0.015       # 彩色连通区最小面积占检测区比例（1.5%）
UNOFFICIAL_SOLIDITY_MIN  = 0.45        # 最低形状紧实度，低于此视为阴影/反光
UNOFFICIAL_EDGE_GRAD_MIN = 6.0         # 边缘饱和度梯度最低值，低于此为渐变阴影

# 包装盒检测：最大内部分辨率（过大会慢，过小轮廓会失真）
BOX_DETECT_MAX_SIDE = 1200

# ── 多 LOB 配置（key 严格对齐 Excel `LOB` 列枚举）──────────────────────────────
#   sticker_count:   "single_only"    仅单贴（AirPods/Accy.）
#                    "single_or_dual" 单贴或双贴均合规（iPhone/Watch/iPad/Mac）
#                    "dual_required"  必须双贴（当前未启用，保留扩展口径）
#   scan_sticker:    一贴（扫码即领）规范相对坐标 {x_min,x_max,y_min,y_max}
#                    y_max 为上限约束；y_min 用于文档说明，实际检测仅校验 x 范围与 y_max
#   auth_sticker:    二贴（Apple 授权专营店）规范相对坐标，None 表示该 LOB 无二贴
#   unofficial_color: 非官方贴纸颜色检测配置
#     enabled:        是否启用
#     mode:           "white_box" 白盒（白平衡归一化 + 相对饱和度，适用 iPhone/Watch/AirPods/Accy./iPad）
#                     "brown_box" 棕盒（排除棕 + 排除白 + 绝对饱和度，适用 Mac）
#     其他阈值:       详见各 LOB 字段
LOB_CONFIGS: dict[str, dict] = {
    "iPhone": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        "auth_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.70, "y_max": 1.00},
        "unofficial_color": {
            "enabled": True,
            "mode": "white_box",
            "sat_above_bg": 55,
            "val_range": (40, 230),
            "area_ratio": 0.015,
            "solidity_min": 0.45,
            "edge_grad_min": 6.0,
        },
    },
    "Watch": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.15, "x_max": 0.70, "y_min": 0.05, "y_max": 0.40},
        "auth_sticker": {"x_min": 0.15, "x_max": 0.70, "y_min": 0.60, "y_max": 0.95},
        "unofficial_color": {
            "enabled": True,
            "mode": "white_box",
            "sat_above_bg": 55,
            "val_range": (40, 230),
            "area_ratio": 0.015,
            "solidity_min": 0.45,
            "edge_grad_min": 6.0,
        },
    },
    "AirPods": {
        "sticker_count": "single_only",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.50},
        "auth_sticker": None,
        "unofficial_color": {
            "enabled": True,
            "mode": "white_box",
            "sat_above_bg": 55,
            "val_range": (40, 230),
            "area_ratio": 0.015,
            "solidity_min": 0.45,
            "edge_grad_min": 6.0,
        },
    },
    "Accy.": {
        "sticker_count": "single_only",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.50},
        "auth_sticker": None,
        "unofficial_color": {
            "enabled": True,
            "mode": "white_box",
            "sat_above_bg": 55,
            "val_range": (40, 230),
            "area_ratio": 0.015,
            "solidity_min": 0.45,
            "edge_grad_min": 6.0,
        },
    },
    "iPad": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        "auth_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.70, "y_max": 1.00},
        "unofficial_color": {
            "enabled": True,
            "mode": "white_box",
            "sat_above_bg": 55,
            "val_range": (40, 230),
            "area_ratio": 0.015,
            "solidity_min": 0.45,
            "edge_grad_min": 6.0,
        },
    },
    "Mac": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.25, "x_max": 0.75, "y_min": 0.70, "y_max": 1.00},
        "auth_sticker": {"x_min": 0.05, "x_max": 0.50, "y_min": 0.00, "y_max": 0.30},
        "unofficial_color": {
            "enabled": True,
            "mode": "brown_box",
            "brown_hue_range": (5, 30),
            "brown_sat_min": 30,
            "brown_val_range": (40, 200),
            "white_sat_max": 30,
            "white_val_min": 200,
            "sat_min_abs": 80,
            "val_range": (50, 240),
            "area_ratio": 0.015,
            "solidity_min": 0.45,
            "edge_grad_min": 6.0,
        },
    },
}

# 无法识别 LOB 时使用的默认配置（iPhone 规则，向下兼容）
DEFAULT_LOB = "iPhone"

# 产品名/MPN 关键词降级匹配表（仅在 Excel LOB 列缺失或值异常时使用）
_LOB_KEYWORDS: list[tuple[str, list[str]]] = [
    ("iPhone",  ["iPhone", "iphone"]),
    ("Watch",   ["Apple Watch", "AppleWatch", "Watch"]),
    ("AirPods", ["AirPods", "airpods"]),
    ("iPad",    ["iPad", "ipad"]),
    ("Mac",     ["MacBook", "iMac", "Mac mini", "Mac Pro", "Mac Studio", "Mac"]),
    ("Accy.",   ["Adapter", "Cable", "MagSafe", "Lightning", "USB-C Power",
                 "充电器", "数据线", "保护壳", "配件"]),
]

UNRECOGNIZED_LOB = "无法识别"

def detect_lob(row) -> str:
    """
    识别订单行对应的 LOB（产品线）。

    优先级：
      1. 直接读 row["LOB"]，与 LOB_CONFIGS key 精确匹配（strip）
      2. 关键词匹配 row["平台对接码(MPN)"] / "品牌对接码(UPC)" / 订单描述
      3. 任一路径均无法命中 → 返回 UNRECOGNIZED_LOB（"无法识别"）

    返回值：LOB_CONFIGS 中的 key，或 "无法识别"（调用方负责兜底）。
    """
    try:
        raw = row.get("LOB", None)
        if raw is not None and not (isinstance(raw, float) and np.isnan(raw)):
            key = str(raw).strip()
            if key in LOB_CONFIGS:
                return key
    except Exception:
        pass

    # 降级：关键词匹配 MPN / 产品描述相关列
    candidates = []
    for col in ("平台对接码(MPN)", "品牌对接码(UPC)", "门店名称"):
        try:
            v = row.get(col, "")
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                candidates.append(str(v))
        except Exception:
            continue
    joined = " ".join(candidates)
    if joined:
        for lob_key, kws in _LOB_KEYWORDS:
            if any(kw.lower() in joined.lower() for kw in kws):
                return lob_key

    return UNRECOGNIZED_LOB
# ─────────────────────────────────────────────────────────────────────────────

print("=" * 80)
print("正在初始化 PaddleOCR (GPU加速，显存友好)...")
print("=" * 80)
ocr = PaddleOCR(
    use_textline_orientation=True,
    lang='ch',
    device='gpu',
    enable_mkldnn=False,
    text_det_limit_side_len=2000,
)
print("PaddleOCR 初始化完成！\n")

_dl_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)


# ═══════════════════════════════════════════════════════════════════════════════
# 一、图像工具
# ═══════════════════════════════════════════════════════════════════════════════

def pil_to_cv(image_pil: Image.Image) -> np.ndarray:
    """PIL RGB → OpenCV BGR"""
    return cv2.cvtColor(np.array(image_pil), cv2.COLOR_RGB2BGR)


def resize_for_ocr(image: Image.Image, max_side: int = 2000) -> Image.Image:
    """按最大边长等比缩放（降低 GPU 推理显存）"""
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
# 三、OCR（坐标换算回原图尺寸）
# ═══════════════════════════════════════════════════════════════════════════════

def ocr_image_full(image: Image.Image, image_id: str = "unknown"):
    """
    对完整图片做一次 OCR。

    返回:
      full_text  : 所有文字拼接
      texts      : 文字段列表
      polys_orig : 多边形坐标列表，已换算回原图尺寸 [[[x,y]×4], ...]
      orig_h, orig_w : 原图高宽（像素）
    """
    if image is None:
        return "", [], [], 0, 0
    try:
        orig_w, orig_h = image.size
        image_resized = resize_for_ocr(image, max_side=2000)
        res_w, res_h = image_resized.size

        temp_path = f"/tmp/temp_ocr_{image_id}_{int(time.time() * 1000)}.jpg"
        image_resized.save(temp_path, 'JPEG')
        result = ocr.predict(input=temp_path)

        texts, polys_res = [], []
        if result and len(result) > 0:
            ocr_result = result[0]
            if hasattr(ocr_result, 'json'):
                res = ocr_result.json.get('res', {})
                texts = res.get('rec_texts', [])
                polys_res = res.get('dt_polys', res.get('boxes', []))

        if os.path.exists(temp_path):
            os.remove(temp_path)

        # 将 OCR 坐标从缩放图还原到原图坐标系
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
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
        return "", [], [], 0, 0


# ═══════════════════════════════════════════════════════════════════════════════
# 四、包装盒检测（OpenCV，三级降级）
# ═══════════════════════════════════════════════════════════════════════════════

def detect_box_bbox(image_pil: Image.Image) -> tuple[int, int, int, int, str]:
    """
    检测图片中产品包装盒的边界框。

    检测策略（按优先级降级）：
      1. Canny 边缘 + 形态学膨胀 → 最大矩形轮廓（通用，适配各色包装）
      2. 亮色区域阈值 → 最大外包矩形（适配 Apple 白色包装）
      3. 降级：返回整图区域（不丢数据，仍输出相对坐标供人工核查）

    参数:
      image_pil: PIL Image，不缩放输入
    返回:
      (x, y, w, h, method_used)
      - x, y, w, h 均为原图像素坐标
      - method_used: 'edge' | 'bright' | 'fallback'
    """
    img_cv = pil_to_cv(image_pil)
    H, W = img_cv.shape[:2]

    # 为提速，统一缩小到 BOX_DETECT_MAX_SIDE 内做轮廓检测，最后坐标放大回原图
    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    # 双边滤波：保留边缘的同时消除纹理噪声
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)

    MIN_AREA_RATIO = 0.08   # 包装盒至少占图像面积 8%
    MAX_ASPECT     = 5.0    # 宽高比不超过 5:1

    # ── 策略 1：Canny 边缘检测 ──────────────────────────────────────────────
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
                int(bw / scale), int(bh / scale),
                'edge'
            )

    # ── 策略 2：亮色区域（Apple 白色包装盒）───────────────────────────────
    _, thresh = cv2.threshold(filtered, 190, 255, cv2.THRESH_BINARY)
    # 去掉图像边界 1% 的高亮噪声
    border = max(5, int(min(dW, dH) * 0.01))
    thresh[:border, :]  = 0;  thresh[-border:, :] = 0
    thresh[:, :border]  = 0;  thresh[:, -border:] = 0

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
                int(bw / scale), int(bh / scale),
                'bright'
            )

    # ── 策略 3：降级，使用整图 ───────────────────────────────────────────────
    return 0, 0, W, H, 'fallback'


# ─── 包装盒透视矫正 ──────────────────────────────────────────────────────────

def _order_quad_corners(pts: np.ndarray) -> np.ndarray:
    """
    对 4 个点按「左上(TL), 右上(TR), 右下(BR), 左下(BL)」顺序重排。
    典型 sum/diff trick：TL 的 x+y 最小、BR 最大；TR 的 x-y 最大、BL 最小。
    """
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = pts[:, 0] - pts[:, 1]
    rect = np.zeros((4, 2), dtype=np.float32)
    rect[0] = pts[np.argmin(s)]   # TL
    rect[2] = pts[np.argmax(s)]   # BR
    rect[1] = pts[np.argmax(d)]   # TR
    rect[3] = pts[np.argmin(d)]   # BL
    return rect


def _find_box_quad(img_cv: np.ndarray) -> np.ndarray | None:
    """
    尝试在图像中找到包装盒的 4 顶点四边形（原图坐标系）。
    使用 Canny+膨胀 → 最大轮廓 → approxPolyDP；
    返回形状 (4, 2) 的 float32 数组；失败返回 None。
    """
    H, W = img_cv.shape[:2]
    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)
    edges = cv2.Canny(filtered, 20, 80)
    kernel = np.ones((9, 9), np.uint8)
    dilated = cv2.dilate(edges, kernel, iterations=3)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    MIN_AREA_RATIO = 0.08
    MAX_ASPECT = 5.0

    for cnt in contours[:10]:
        if cv2.contourArea(cnt) < MIN_AREA_RATIO * dW * dH:
            continue
        peri = cv2.arcLength(cnt, True)
        # 逐步放宽 eps，尽可能拟合到 4 点
        for eps_ratio in (0.02, 0.03, 0.04, 0.05):
            approx = cv2.approxPolyDP(cnt, eps_ratio * peri, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                quad = approx.reshape(4, 2).astype(np.float32)
                xs = quad[:, 0]; ys = quad[:, 1]
                w_hat = max(xs) - min(xs)
                h_hat = max(ys) - min(ys)
                if min(w_hat, h_hat) < 10:
                    break
                aspect = w_hat / max(h_hat, 1.0)
                if not ((1.0 / MAX_ASPECT) <= aspect <= MAX_ASPECT):
                    break
                # 还原到原图坐标系
                quad_orig = quad / scale
                return quad_orig.astype(np.float32)
            if len(approx) > 4:
                continue
            if len(approx) < 4:
                break
    return None


def rectify_package_box(image_pil: Image.Image) -> dict:
    """
    包装盒矫正（三级降级）：
        1) 四点透视矫正（perspective）：approxPolyDP 得到凸四边形 → warpPerspective
        2) minAreaRect 旋转矫正（rotation）：最大轮廓最小外接旋转矩形 → warpAffine 后裁剪
        3) 轴对齐兜底（axis_aligned）：复用现行 detect_box_bbox 的轴对齐 bbox

    OCR 仍在原图上跑，识别完成后通过返回的 `M` 把原图 OCR 多边形映射到矫正坐标系。
    矫正后坐标系中，包装盒覆盖 (0, 0) ~ (W_rect, H_rect)，贴纸相对坐标 = 除以 W_rect/H_rect。

    返回 dict：
        warped_img:   np.ndarray  矫正后 BGR 图（axis_aligned 兜底时为裁剪后的 bbox 子图）
        M:            np.ndarray | None  3×3 原图→矫正图矩阵；axis_aligned 时为 None
        W_rect,H_rect:int          矫正坐标系尺寸
        method:       "perspective" / "rotation" / "axis_aligned"
        box_quad_src: list | None  原图 4 角点坐标（顺序 TL,TR,BR,BL）
        box_x/y/w/h:  int          原图轴对齐 bbox（诊断用；perspective 时等于四点 bbox）
    """
    img_cv = pil_to_cv(image_pil)
    H_img, W_img = img_cv.shape[:2]

    # ── 策略 1：四点透视矫正 ─────────────────────────────────────────────────
    quad = _find_box_quad(img_cv)
    if quad is not None:
        try:
            ordered = _order_quad_corners(quad)
            tl, tr, br, bl = ordered
            # 取上下边与左右边的最长距离作为矫正尺寸
            w_top    = float(np.linalg.norm(tr - tl))
            w_bottom = float(np.linalg.norm(br - bl))
            h_left   = float(np.linalg.norm(bl - tl))
            h_right  = float(np.linalg.norm(br - tr))
            W_rect = max(1, int(round(max(w_top, w_bottom))))
            H_rect = max(1, int(round(max(h_left, h_right))))

            # clip 以控制后续检测耗时与显存
            clip_scale = min(1.0, BOX_DETECT_MAX_SIDE / max(W_rect, H_rect))
            if clip_scale < 1.0:
                W_rect = max(1, int(round(W_rect * clip_scale)))
                H_rect = max(1, int(round(H_rect * clip_scale)))

            dst = np.array([
                [0, 0], [W_rect - 1, 0],
                [W_rect - 1, H_rect - 1], [0, H_rect - 1],
            ], dtype=np.float32)
            M = cv2.getPerspectiveTransform(ordered, dst)
            warped = cv2.warpPerspective(img_cv, M, (W_rect, H_rect))

            # 轴对齐 bbox（诊断/兜底用）
            xs = ordered[:, 0]; ys = ordered[:, 1]
            box_x = int(max(0, np.floor(xs.min())))
            box_y = int(max(0, np.floor(ys.min())))
            box_w = int(min(W_img - box_x, np.ceil(xs.max()) - box_x))
            box_h = int(min(H_img - box_y, np.ceil(ys.max()) - box_y))

            return {
                "warped_img":    warped,
                "M":             M,
                "W_rect":        W_rect,
                "H_rect":        H_rect,
                "method":        "perspective",
                "box_quad_src":  ordered.tolist(),
                "box_x":         box_x,
                "box_y":         box_y,
                "box_w":         box_w,
                "box_h":         box_h,
            }
        except Exception as e:
            print(f"  ⚠ 透视矫正异常，降级旋转矫正: {type(e).__name__}: {e}")

    # ── 策略 2：minAreaRect 旋转矫正 ─────────────────────────────────────────
    try:
        scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H_img, W_img))
        dW, dH = max(1, int(W_img * scale)), max(1, int(H_img * scale))
        img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
        filtered = cv2.bilateralFilter(gray, 9, 75, 75)
        edges = cv2.Canny(filtered, 20, 80)
        kernel = np.ones((9, 9), np.uint8)
        dilated = cv2.dilate(edges, kernel, iterations=3)
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            cnt = max(contours, key=cv2.contourArea)
            if cv2.contourArea(cnt) >= 0.08 * dW * dH:
                rect = cv2.minAreaRect(cnt)
                box_pts = cv2.boxPoints(rect).astype(np.float32)
                # 还原到原图
                box_pts_orig = box_pts / scale
                ordered = _order_quad_corners(box_pts_orig)
                tl, tr, br, bl = ordered
                w_top    = float(np.linalg.norm(tr - tl))
                w_bottom = float(np.linalg.norm(br - bl))
                h_left   = float(np.linalg.norm(bl - tl))
                h_right  = float(np.linalg.norm(br - tr))
                W_rect = max(1, int(round(max(w_top, w_bottom))))
                H_rect = max(1, int(round(max(h_left, h_right))))
                clip_scale = min(1.0, BOX_DETECT_MAX_SIDE / max(W_rect, H_rect))
                if clip_scale < 1.0:
                    W_rect = max(1, int(round(W_rect * clip_scale)))
                    H_rect = max(1, int(round(H_rect * clip_scale)))
                dst = np.array([
                    [0, 0], [W_rect - 1, 0],
                    [W_rect - 1, H_rect - 1], [0, H_rect - 1],
                ], dtype=np.float32)
                M = cv2.getPerspectiveTransform(ordered, dst)
                warped = cv2.warpPerspective(img_cv, M, (W_rect, H_rect))

                xs = ordered[:, 0]; ys = ordered[:, 1]
                box_x = int(max(0, np.floor(xs.min())))
                box_y = int(max(0, np.floor(ys.min())))
                box_w = int(min(W_img - box_x, np.ceil(xs.max()) - box_x))
                box_h = int(min(H_img - box_y, np.ceil(ys.max()) - box_y))

                return {
                    "warped_img":    warped,
                    "M":             M,
                    "W_rect":        W_rect,
                    "H_rect":        H_rect,
                    "method":        "rotation",
                    "box_quad_src":  ordered.tolist(),
                    "box_x":         box_x,
                    "box_y":         box_y,
                    "box_w":         box_w,
                    "box_h":         box_h,
                }
    except Exception as e:
        print(f"  ⚠ 旋转矫正异常，降级轴对齐 bbox: {type(e).__name__}: {e}")

    # ── 策略 3：轴对齐 bbox 兜底（复用 detect_box_bbox）────────────────────
    bx, by, bw, bh, _method = detect_box_bbox(image_pil)
    warped = img_cv[by:by + bh, bx:bx + bw].copy()
    return {
        "warped_img":    warped if warped.size > 0 else img_cv,
        "M":             None,
        "W_rect":        bw,
        "H_rect":        bh,
        "method":        "axis_aligned",
        "box_quad_src":  None,
        "box_x":         bx,
        "box_y":         by,
        "box_w":         bw,
        "box_h":         bh,
    }


def transform_polys(polys_orig: list, M: np.ndarray | None,
                    box_x: int = 0, box_y: int = 0) -> list:
    """
    将原图 OCR 多边形映射到矫正坐标系。
      - M is not None：使用 cv2.perspectiveTransform（适用于 perspective / rotation）
      - M is None    ：轴对齐兜底，简单平移 (box_x, box_y)
    返回与输入同结构的 polys_rect。
    """
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
            out.append([
                [float(pt[0]) - box_x, float(pt[1]) - box_y] for pt in poly
            ])
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 五、贴纸定位与位置验证
# ═══════════════════════════════════════════════════════════════════════════════

def is_flat_sticker(
    texts: list[str],
    polys: list,
    anchor_idx: int,
    box_x: int = 0,
    box_y: int = 0,
    box_w: int = 0,
    box_h: int = 0,
) -> tuple[bool, str]:
    """
    平铺错误检测（语义信号优先，旋转不变）。

    根因分析：
      基于文字簇宽高比的方案会将包装盒自身的文字（iPhone型号、规格等）
      混入簇，导致簇宽高比虚高而误报。
      包装盒内容文字与贴纸文字在同一水平带内时无法通过几何手段区分。

    改进策略：
      直接检测平铺时才会出现的"端片语义标志"——
        英文"Authorized Reseller"仅印在贴纸端片上，不会出现在包装盒内容中。
        该文字只有在整条贴纸平铺（端片可见）时才被 OCR 识别到。
      此外，检测"授权经销商"大字（非 Apple Logo 内的小字）出现在离锚点
      一定距离处，也是端片可见的信号。

    当 box_w > 0 时，只检查中心点落在包装盒范围内的文字，
    以避免盒子外部（背景、桌面）的文字触发误报。

    返回: (is_flat: bool, detail: str)
    """
    if anchor_idx >= len(polys):
        return False, "锚点索引越界"

    use_box_filter = box_w > 0 and box_h > 0
    anchor_pts    = np.array(polys[anchor_idx], dtype=float)
    anchor_center = anchor_pts.mean(axis=0)
    rect_a        = cv2.minAreaRect(anchor_pts.astype(np.float32))
    anchor_h      = max(min(rect_a[1]), 1.0)  # "扫码即领"文字行高（短轴）

    for i, text in enumerate(texts):
        if i == anchor_idx or i >= len(polys):
            continue
        pts    = np.array(polys[i], dtype=float)
        center = pts.mean(axis=0)

        # 仅检查盒子范围内的文字，排除盒外背景文字
        if use_box_filter:
            cx, cy = float(center[0]), float(center[1])
            if not (box_x <= cx <= box_x + box_w and box_y <= cy <= box_y + box_h):
                continue

        dist = float(np.linalg.norm(center - anchor_center))

        # ── 信号 1：英文端片标识（最可靠，包装盒上不会有此词）──────────────
        t_lower = text.lower().strip()
        if ("authorized" in t_lower or "authorised" in t_lower) and "reseller" in t_lower:
            if dist > anchor_h * 2:   # 排除与锚点重叠的极近文字
                return True, f"检测到端片标识 'Authorized Reseller'（距锚点{dist:.0f}px）"

        # ── 信号 2：独立大字"授权经销商"出现在离锚点较远处 ─────────────────
        # Apple Logo 内的"授权经销商"与 QR 码中心几乎重叠，dist 很小
        # 端片上的"授权经销商"为独立大字，dist 约为 anchor_h * 5 以上
        if "授权经销商" in text and not ("扫码" in text or "Apple" in text):
            rect_i  = cv2.minAreaRect(pts.astype(np.float32))
            text_h  = max(min(rect_i[1]), 1.0)
            # 端片文字比 Logo 内字大，且距锚点远
            if text_h > anchor_h * 0.8 and dist > anchor_h * 5:
                return True, f"检测到远端'授权经销商'大字（距锚点{dist:.0f}px）"

    return False, "未检测到端片文字（Authorized Reseller），贴纸未平铺"


def _filter_color_candidates(
    candidate_mask: np.ndarray,
    signal_u8: np.ndarray,
    zone_area: int,
    color_cfg: dict,
    detail_prefix: str,
) -> tuple[bool, str]:
    """
    通用后段过滤：morphology → 连通区 → 面积占比 → 形状紧实度 → 边缘梯度。
    白盒/棕盒两个分支共用此段，差异只在前段（候选掩码与参考信号）。

    参数：
      candidate_mask: uint8 0/255，候选异常像素掩码
      signal_u8     : uint8，用于计算边缘梯度的单通道信号
                      （白盒=归一化有效饱和度 eff_sat；棕盒=原始饱和度 s_raw）
      zone_area     : int，检测区域总像素数
      color_cfg     : 阈值配置（读取 area_ratio / solidity_min / edge_grad_min）
      detail_prefix : 命中时详情文字前缀（用于区分分支来源）
    """
    area_ratio_min = float(color_cfg.get("area_ratio", 0.015))
    solidity_min   = float(color_cfg.get("solidity_min", 0.45))
    edge_grad_min  = float(color_cfg.get("edge_grad_min", 6.0))

    # 形态学去噪（合并相邻像素）
    k5 = np.ones((5, 5), np.uint8)
    mask = cv2.dilate(candidate_mask, k5, iterations=2)
    mask = cv2.erode(mask, k5, iterations=2)

    n_labels, label_img, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

    # 预计算边缘梯度图
    gx = cv2.Sobel(signal_u8, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(signal_u8, cv2.CV_32F, 0, 1, ksize=3)
    grad_mag = np.sqrt(gx ** 2 + gy ** 2)

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
        solidity = cv2.contourArea(cnt) / hull_area
        if solidity < solidity_min:
            continue

        # 连通区边缘像素环（膨胀 − 腐蚀）
        k3 = np.ones((3, 3), np.uint8)
        boundary = (cv2.dilate(comp_mask, k3) - cv2.erode(comp_mask, k3)).astype(bool)
        mean_edge_grad = float(grad_mag[boundary].mean()) if boundary.any() else 0.0
        if mean_edge_grad < edge_grad_min:
            continue

        return True, (
            f"{detail_prefix}：色块面积占比 {ratio:.1%}，"
            f"紧实度 {solidity:.2f}，边缘梯度 {mean_edge_grad:.1f}"
        )

    return False, ""


def _detect_unofficial_white_box(zone: np.ndarray, color_cfg: dict) -> tuple[bool, str]:
    """
    白盒分支：白平衡归一化 + 相对饱和度。
    原理：白背景 S≈0，任何彩色块都是饱和度显著升高的异常信号。
    """
    sat_above_bg = float(color_cfg.get("sat_above_bg", UNOFFICIAL_SAT_ABOVE_BG))
    v_min, v_max = color_cfg.get("val_range", UNOFFICIAL_VAL_RANGE)

    hsv_f = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV).astype(np.float32)
    s_raw = hsv_f[:, :, 1]
    v_raw = hsv_f[:, :, 2]

    # 高亮 + 低饱和像素 = 包装盒真实白色背景
    white_mask = (v_raw > 180) & (s_raw < 55)
    if white_mask.sum() > 200:
        bg_sat_ref = float(np.percentile(s_raw[white_mask], 90))
    else:
        bg_sat_ref = 0.0

    eff_sat = np.clip(s_raw - bg_sat_ref - 10.0, 0.0, 255.0)

    candidate = (
        (eff_sat > sat_above_bg) &
        (v_raw > v_min) & (v_raw < v_max)
    ).astype(np.uint8) * 255

    eff_sat_u8 = np.clip(eff_sat, 0, 255).astype(np.uint8)
    zone_area = zone.shape[0] * zone.shape[1]
    prefix = f"[white_box] 检测到非白色彩色区域 (bg_ref S={bg_sat_ref:.1f})"
    return _filter_color_candidates(candidate, eff_sat_u8, zone_area, color_cfg, prefix)


def _detect_unofficial_brown_box(zone: np.ndarray, color_cfg: dict) -> tuple[bool, str]:
    """
    棕盒分支（Mac 专用）：排除棕色（盒面本身）+ 排除白色（官方白贴与强反光）
    + 绝对饱和度阈值（无白参考，不用 sat_above_bg）。
    等于「非棕 ∩ 非白 ∩ 高饱和」的异色候选区。
    """
    brown_h_lo, brown_h_hi = color_cfg.get("brown_hue_range", (5, 30))
    brown_sat_min = float(color_cfg.get("brown_sat_min", 30))
    brown_v_lo, brown_v_hi = color_cfg.get("brown_val_range", (40, 200))
    white_sat_max = float(color_cfg.get("white_sat_max", 30))
    white_val_min = float(color_cfg.get("white_val_min", 200))
    sat_min_abs = float(color_cfg.get("sat_min_abs", 80))
    v_min, v_max = color_cfg.get("val_range", (50, 240))

    hsv_f = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV).astype(np.float32)
    h_raw = hsv_f[:, :, 0]   # OpenCV: 0~180
    s_raw = hsv_f[:, :, 1]
    v_raw = hsv_f[:, :, 2]

    brown_mask = (
        (h_raw >= brown_h_lo) & (h_raw <= brown_h_hi) &
        (s_raw >= brown_sat_min) &
        (v_raw >= brown_v_lo) & (v_raw <= brown_v_hi)
    )
    white_mask = (s_raw <= white_sat_max) & (v_raw >= white_val_min)

    foreign = (
        (~brown_mask) & (~white_mask) &
        (s_raw >= sat_min_abs) &
        (v_raw >= v_min) & (v_raw <= v_max)
    ).astype(np.uint8) * 255

    s_u8 = np.clip(s_raw, 0, 255).astype(np.uint8)
    zone_area = zone.shape[0] * zone.shape[1]
    prefix = "[brown_box] 检测到非棕非白高饱和异色区域"
    return _filter_color_candidates(foreign, s_u8, zone_area, color_cfg, prefix)


def detect_unofficial_sticker_color(
    warped_img: np.ndarray,
    color_cfg: dict,
) -> tuple[bool, str]:
    """
    非官方贴纸颜色检测（按 LOB 配置分流白盒 / 棕盒两种模式）。

    输入：
      warped_img : rectify_package_box 返回的矫正后 BGR 图（或 axis_aligned 兜底时的盒内裁剪图）
      color_cfg  : LOB_CONFIGS[lob]["unofficial_color"]，至少包含 enabled / mode

    行为：
      - enabled=False → 直接返回 (False, "跳过")
      - mode=white_box → 白平衡归一化 + 相对饱和度阈值
      - mode=brown_box → 排除棕 + 排除白 + 绝对饱和度阈值
      命中阈值则返回 True（调用方将 position_valid 硬判为 4）。

    返回: (has_unofficial: bool, detail: str)
    """
    if not color_cfg or not color_cfg.get("enabled", False):
        return False, "颜色检测已跳过（当前 LOB 未启用）"

    try:
        if warped_img is None or warped_img.size == 0:
            return False, ""

        H, W = warped_img.shape[:2]
        if H < 10 or W < 10:
            return False, ""

        # 内缩 2% 去除盒子边框干扰
        mx = max(1, int(W * 0.02))
        my = max(1, int(H * 0.02))
        zone = warped_img[my:H - my, mx:W - mx]
        if zone.shape[0] < 10 or zone.shape[1] < 10:
            return False, ""

        mode = color_cfg.get("mode", "white_box")
        if mode == "brown_box":
            return _detect_unofficial_brown_box(zone, color_cfg)
        # 默认白盒分支
        return _detect_unofficial_white_box(zone, color_cfg)

    except Exception as e:
        return False, f"颜色检测异常: {type(e).__name__}: {e}"


def find_all_scan_stickers(texts: list[str], polys: list) -> list[dict]:
    """
    找出图中所有"扫码即领"文字的位置（可能有多个贴纸）。
    返回: list of sticker_info dicts，每个含 cx, cy, text_idx
    """
    stickers = []
    for i, text in enumerate(texts):
        if "扫码即领" in text and i < len(polys):
            try:
                poly = np.array(polys[i], dtype=float)
                x1, y1 = float(poly[:, 0].min()), float(poly[:, 1].min())
                x2, y2 = float(poly[:, 0].max()), float(poly[:, 1].max())
                stickers.append({
                    "cx": (x1 + x2) / 2,
                    "cy": (y1 + y2) / 2,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "text_idx": i,
                })
            except Exception:
                continue
    return stickers


def has_dealer_only_sticker(texts: list[str]) -> tuple[bool, str]:
    """
    检测"无官方封口贴但疑似有其他内容"的文本辅助信号。

    注意：
      "授权经销商"/"Authorized Reseller" 是 Apple 官方贴纸上的字样，
      不能作为经销商自贴触发词，否则会误杀合规订单。
      主要非官方贴纸检测依赖颜色信号（detect_unofficial_sticker_color）；
      此函数仅作为"整单无候选图"场景下的补充说明信号。

    当前规则：
      图片有 OCR 文字但没有任何 Apple 官方关键词 → 疑似经销商自贴图片
    """
    if not texts:
        return False, ""
    if any("扫码即领" in t for t in texts):
        return False, ""

    apple_kw = [
        "扫码即领", "Apple", "apple",
        "授权经销商", "Authorized Reseller", "Authorised Reseller",
        "Apple授权专营店", "authorized reseller",
    ]
    has_any_apple = any(any(k in t for k in apple_kw) for t in texts)
    if not has_any_apple and len(texts) >= 2:
        sample = texts[0][:40]
        return True, sample
    return False, ""

def check_dual_sticker_status(
    texts: list[str],
    polys: list,
    img_h: int,
    sticker_count_mode: str = "single_or_dual",
) -> dict:
    """
    检测双贴纸状态（单图内），按 LOB 的 sticker_count 约定分流。

    sticker_count_mode:
      - "single_only"    : 仅单贴（AirPods/Accy.）。不允许两张扫码贴；忽略 Auth 贴纸。
      - "single_or_dual" : 单/双贴均可（iPhone/Watch/iPad/Mac）。当前行为。
      - "dual_required"  : 必须双贴（保留扩展口径）。缺 Auth 记 dual_code=3。

    dual_code 编码（保持向下兼容）：
       0 = 单贴
       1 = 双贴合规（扫码 + 授权专营店）
       2 = 双贴错误（两张扫码）
       3 = 缺失二贴（dual_required 且未找到 Auth）
      -1 = 未找到扫码贴
    """
    scan_stickers = find_all_scan_stickers(texts, polys)
    has_auth_raw = any(
        kw in text
        for text in texts
        for kw in ["Apple授权专营店", "授权专营店", "在你身边"]
    )

    # 按 Y 坐标聚类统计独立贴纸数
    MIN_Y_GAP = max(img_h * 0.20, 50)
    distinct: list[dict] = []
    for s in scan_stickers:
        if not any(abs(s["cy"] - e["cy"]) < MIN_Y_GAP for e in distinct):
            distinct.append(s)
    scan_count = len(distinct)

    if sticker_count_mode == "single_only":
        # 仅单贴：忽略 Auth 贴纸；出现两张扫码仍然不合规
        has_auth = False
        if scan_count >= 2:
            dual_code, dual_detail = 2, (
                f"错误：检测到{scan_count}个'扫码即领'贴纸（该 LOB 仅允许单贴）"
            )
        elif scan_count == 1:
            dual_code, dual_detail = 0, "单贴：仅'扫码即领'（已忽略 Auth 关键词）"
        else:
            dual_code, dual_detail = -1, "未找到'扫码即领'贴纸"
        return {
            "scan_count":  scan_count,
            "has_auth":    has_auth,
            "dual_code":   dual_code,
            "dual_detail": dual_detail,
        }

    if sticker_count_mode == "dual_required":
        if scan_count >= 2:
            dual_code, dual_detail = 2, (
                f"错误：检测到{scan_count}个'扫码即领'贴纸，上下均为扫码贴"
            )
        elif scan_count == 1 and has_auth_raw:
            dual_code, dual_detail = 1, "合规双贴：'扫码即领' + 'Apple授权专营店'"
        elif scan_count == 1:
            dual_code, dual_detail = 3, (
                "缺失二贴：该 LOB 规定必须双贴，但未检测到'Apple授权专营店'"
            )
        else:
            dual_code, dual_detail = -1, "未找到'扫码即领'贴纸"
        return {
            "scan_count":  scan_count,
            "has_auth":    has_auth_raw,
            "dual_code":   dual_code,
            "dual_detail": dual_detail,
        }

    # 默认 single_or_dual（现行行为）
    if scan_count >= 2:
        dual_code, dual_detail = 2, (
            f"错误：检测到{scan_count}个'扫码即领'贴纸，上下均为扫码贴"
        )
    elif scan_count == 1 and has_auth_raw:
        dual_code, dual_detail = 1, "合规双贴：'扫码即领' + 'Apple授权专营店'"
    elif scan_count == 1:
        dual_code, dual_detail = 0, "单贴：仅'扫码即领'"
    else:
        dual_code, dual_detail = -1, "未找到'扫码即领'贴纸"

    return {
        "scan_count":  scan_count,
        "has_auth":    has_auth_raw,
        "dual_code":   dual_code,
        "dual_detail": dual_detail,
    }


def find_sticker_from_ocr(texts: list[str], polys: list) -> dict | None:
    """返回第一个'扫码即领'文字框信息，找不到返回 None。"""
    stickers = find_all_scan_stickers(texts, polys)
    return stickers[0] if stickers else None


def find_all_auth_stickers_in_box(
    texts: list[str],
    polys_rect: list,
    W_rect: int = 0,
    H_rect: int = 0,
) -> list[dict]:
    """
    返回所有中心点落在包装盒范围内（矫正坐标系 0~W_rect × 0~H_rect）
    的"Apple授权专营店"候选文字框列表。

    匹配关键词：'Apple授权专营店' / '授权专营店' / '在你身边'
    盒子外部（如桌面、背景、包装印刷）的同名文字会被过滤掉。
    """
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
                if use_box_filter:
                    if not (0 <= cx <= W_rect and 0 <= cy <= H_rect):
                        continue
                results.append({
                    "cx": cx, "cy": cy,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "text_idx": i,
                    "matched_text": text,
                })
            except Exception:
                continue
    return results


def find_auth_sticker_from_ocr(
    texts: list[str],
    polys_rect: list,
    W_rect: int = 0,
    H_rect: int = 0,
) -> dict | None:
    """返回盒子内第一个命中的 Auth 贴纸候选（兼容旧调用）。"""
    candidates = find_all_auth_stickers_in_box(texts, polys_rect, W_rect, H_rect)
    return candidates[0] if candidates else None


def validate_sticker_position(
    rel_cx: float,
    rel_cy: float,
    position_cfg: dict,
) -> dict:
    """
    验证已归一化的相对坐标是否满足规范位置（纯函数，无包装盒耦合）。

    position_cfg 结构：{"x_min", "x_max", "y_min", "y_max"}
    业务约定：
      - x 双侧硬约束： x_min <= rel_x <= x_max
      - y 在字段名上提供 y_min/y_max，以双侧约束判定（与历史"仅上限"略有差异，
        但配合矫正坐标系更直观；上下界若任何一侧为 None 则视为不约束）

    返回 dict:
      in_correct_position : bool
      rel_x, rel_y        : 相对坐标（四舍五入）
      x_ok, y_ok          : 各轴是否达标
      detail              : 文字说明
    """
    if position_cfg is None:
        return {
            "in_correct_position": False,
            "rel_x": round(rel_cx, 4) if rel_cx is not None else None,
            "rel_y": round(rel_cy, 4) if rel_cy is not None else None,
            "x_ok": False, "y_ok": False,
            "detail": "位置验证跳过（该 LOB 未配置规范位置）",
        }

    x_min = position_cfg.get("x_min", -float("inf"))
    x_max = position_cfg.get("x_max", float("inf"))
    y_min = position_cfg.get("y_min", -float("inf"))
    y_max = position_cfg.get("y_max", float("inf"))

    x_ok = x_min <= rel_cx <= x_max
    y_ok = y_min <= rel_cy <= y_max

    if x_ok and y_ok:
        detail = f"位置规范 (rel_x={rel_cx:.3f}, rel_y={rel_cy:.3f})"
    else:
        parts = []
        if not x_ok:
            parts.append(f"X={rel_cx:.3f} 不在 [{x_min},{x_max}]")
        if not y_ok:
            parts.append(f"Y={rel_cy:.3f} 不在 [{y_min},{y_max}]")
        detail = "位置异常：" + "；".join(parts)

    return {
        "in_correct_position": x_ok and y_ok,
        "rel_x": round(rel_cx, 4),
        "rel_y": round(rel_cy, 4),
        "x_ok": x_ok,
        "y_ok": y_ok,
        "detail": detail,
    }


def check_auth_sticker_position(
    texts: list[str],
    polys_rect: list,
    W_rect: int,
    H_rect: int,
    position_cfg: dict,
) -> dict:
    """
    验证"Apple 授权专营店"贴纸是否在规范位置（矫正坐标系）。

    盒内可能出现多个命中（贴纸本身 + 盒面印刷文字），
    只要任意一个满足 position_cfg 即视为合规，以避免"先遇到印刷文字"的误判。

    参数：
      polys_rect   : 已变换到矫正坐标系的 OCR 多边形（与 texts 对齐索引）
      W_rect, H_rect : 矫正后包装盒尺寸（用于 rel 归一化 + 盒内过滤）
      position_cfg : LOB_CONFIGS[lob]["auth_sticker"]，None 表示该 LOB 无二贴规范

    返回 dict: found / in_correct_position / rel_x / rel_y / detail
    """
    if position_cfg is None:
        return {
            "found": False,
            "in_correct_position": False,
            "rel_x": None, "rel_y": None,
            "detail": "该 LOB 无二贴规范",
        }

    candidates = find_all_auth_stickers_in_box(texts, polys_rect, W_rect, H_rect)
    if not candidates:
        return {
            "found": False,
            "in_correct_position": False,
            "rel_x": None, "rel_y": None,
            "detail": "未找到'Apple授权专营店'贴纸（盒子内）",
        }

    first_rel = None
    first_detail = ""
    for auth in candidates:
        rel_x = auth["cx"] / W_rect if W_rect > 0 else -1.0
        rel_y = auth["cy"] / H_rect if H_rect > 0 else -1.0
        v = validate_sticker_position(rel_x, rel_y, position_cfg)
        if first_rel is None:
            first_rel = (v["rel_x"], v["rel_y"])
            first_detail = v["detail"]
        if v["in_correct_position"]:
            return {
                "found": True,
                "in_correct_position": True,
                "rel_x": v["rel_x"],
                "rel_y": v["rel_y"],
                "detail": "Apple授权专营店位置规范 "
                          f"(rel_x={v['rel_x']:.3f}, rel_y={v['rel_y']:.3f})",
            }

    rel_x, rel_y = first_rel
    return {
        "found": True,
        "in_correct_position": False,
        "rel_x": rel_x,
        "rel_y": rel_y,
        "detail": "Apple授权专营店" + first_detail,
    }


def normalize_horizontal_angle(rect_angle: float, rect_w: float, rect_h: float) -> float:
    """
    将 cv2.minAreaRect 返回的角度归一化为"长轴与水平方向的夹角"，范围 [-90, 90]。

    cv2.minAreaRect 约定：角度在 (-90, 0]，表示矩形"宽向量"与水平轴的夹角。
    当 w < h 时，宽向量指向短轴，"长轴"实为高方向，需要 +90° 修正。
    """
    angle = float(rect_angle)
    if rect_w < rect_h:
        angle += 90.0
    while angle > 90.0:
        angle -= 180.0
    while angle < -90.0:
        angle += 180.0
    return angle


def extract_poly_angle(poly) -> float | None:
    """
    从 OCR 四边形多边形提取文字框主轴角度。

    返回: 长轴与水平方向夹角（度，[-90, 90]）；解析失败返回 None。
    """
    try:
        pts = np.array(poly, dtype=np.float32)
        _, (w, h), angle = cv2.minAreaRect(pts)
        return normalize_horizontal_angle(angle, w, h)
    except Exception:
        return None


def validate_angle(
    sticker: dict,
    polys: list,
    box_angle_deg: float = 0.0,
    angle_max_deg: float = STICKER_ANGLE_MAX_DEG,
) -> tuple[bool, float | None, str]:
    """
    验证"扫码即领"贴纸的粘贴角度是否与包装盒水平方向对齐。

    原理：
      正向完整照片（Phase 1 已过滤）中包装盒水平边近似水平（box_angle ≈ 0°）。
      用 minAreaRect 提取贴纸文字框主轴角度，计算与包装盒水平方向的相对偏角。
      允许门店拍照轻微倾斜（±STICKER_ANGLE_MAX_DEG）。

    参数：
      sticker       : find_sticker_from_ocr() 的返回 dict（含 text_idx）
      polys         : OCR 多边形列表（原图尺寸）
      box_angle_deg : 包装盒水平基准角度（正向拍摄约为 0°）
      angle_max_deg : 允许的最大偏角（默认 STICKER_ANGLE_MAX_DEG）

    返回: (ok, delta_deg, detail)
      ok        : True = 角度合规
      delta_deg : 贴纸相对盒子水平方向的偏角（°），正值顺时针
      detail    : 文字说明
    """
    text_idx = sticker.get("text_idx")
    if text_idx is None or text_idx >= len(polys):
        return True, None, "角度验证跳过（索引越界）"

    sticker_angle = extract_poly_angle(polys[text_idx])
    if sticker_angle is None:
        return True, None, "角度验证跳过（多边形解析失败）"

    delta = sticker_angle - box_angle_deg
    # 归一化到 [-90, 90]
    while delta > 90.0:
        delta -= 180.0
    while delta < -90.0:
        delta += 180.0

    ok = abs(delta) <= angle_max_deg
    if ok:
        detail = f"角度规范（偏转 {delta:+.1f}°，阈值 ±{angle_max_deg:.0f}°）"
    else:
        detail = f"角度异常：偏转 {delta:+.1f}°，超过阈值 ±{angle_max_deg:.0f}°"

    return ok, round(delta, 2), detail


def check_sticker_placement(
    sticker_rect: dict,
    W_rect: int,
    H_rect: int,
    rectify_method: str,
    texts: list[str],
    polys_rect: list,
    scan_position_cfg: dict,
) -> dict:
    """
    对单张候选图的"扫码即领"贴纸做合规检测（矫正坐标系 + LOB 配置驱动）。

    参数：
      sticker_rect     : find_sticker_from_ocr(polys_rect) 定位的贴纸中心（矫正坐标）
      W_rect, H_rect   : 矫正后包装盒尺寸
      rectify_method   : "perspective" / "rotation" / "axis_aligned"（诊断用）
      polys_rect       : 已映射到矫正坐标系的 OCR 多边形
      scan_position_cfg: LOB_CONFIGS[lob]["scan_sticker"]

    检测顺序：
      Step 1  位置验证  → 不合规（0）立即返回
      Step 2  角度验证  → 不合规（3）立即返回（跳过平铺检测）
      Step 3  平铺检测  → 平铺错误（2）立即返回

    返回 dict:
      position_valid : 0=位置异常 | 1=规范 | 2=平铺错误 | 3=角度异常
      rel_x, rel_y   : 相对坐标
      angle_deg      : 贴纸偏角（度）；None 表示跳过
      detail         : 文字说明
    """
    rel_x = sticker_rect["cx"] / W_rect if W_rect > 0 else -1.0
    rel_y = sticker_rect["cy"] / H_rect if H_rect > 0 else -1.0

    # ── Step 1：位置验证 ──────────────────────────────────────────────────────
    pos = validate_sticker_position(rel_x, rel_y, scan_position_cfg)
    if not pos["in_correct_position"]:
        return {
            "position_valid": 0,
            "rel_x": pos["rel_x"],
            "rel_y": pos["rel_y"],
            "angle_deg": None,
            "detail": f"[{rectify_method}] {pos['detail']}",
        }

    # ── Step 2：角度验证（位置合规后才执行）─────────────────────────────────
    # 矫正后包装盒水平边位于 y=0 / y=H_rect，box_angle=0 由构造保证
    angle_ok, delta_deg, angle_detail = validate_angle(sticker_rect, polys_rect)
    if not angle_ok:
        return {
            "position_valid": 3,
            "rel_x": pos["rel_x"],
            "rel_y": pos["rel_y"],
            "angle_deg": delta_deg,
            "detail": f"[{rectify_method}] {angle_detail}",
        }

    # ── Step 3：平铺错误检测（位置 + 角度均合规后才执行）────────────────────
    flat, flat_detail = is_flat_sticker(
        texts, polys_rect, sticker_rect["text_idx"],
        0, 0, W_rect, H_rect,
    )
    if flat:
        return {
            "position_valid": 2,
            "rel_x": pos["rel_x"],
            "rel_y": pos["rel_y"],
            "angle_deg": delta_deg,
            "detail": f"[{rectify_method}] {flat_detail}",
        }

    return {
        "position_valid": 1,
        "rel_x": pos["rel_x"],
        "rel_y": pos["rel_y"],
        "angle_deg": delta_deg,
        "detail": f"[{rectify_method}] {pos['detail']}",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 六、水印提取（与 V1 相同）
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
        if p and (
            sum(1 for c in p if chr(0x4E00) <= c <= chr(0x9FFF))
            / max(len(p.replace(" ", "")), 1)
        ) >= 0.4
    ]
    location_str = re.sub(r"\s+", " ", " ".join(clean_loc)).strip()
    return time_str, location_str


def extract_watermark_crop(image: Image.Image, image_id: str) -> tuple[str, str]:
    """裁剪底部 18% × 左侧 60% 区域做轻量水印 OCR"""
    if image is None:
        return "", ""
    try:
        w, h = image.size
        crop = image.crop((0, int(h * 0.82), int(w * 0.60), h))
        temp_path = f"/tmp/wm_crop_{image_id}_{int(time.time() * 1000)}.jpg"
        crop.save(temp_path, 'JPEG')
        result = ocr.predict(input=temp_path)

        texts = []
        if result and len(result) > 0:
            r = result[0]
            if hasattr(r, 'json'):
                texts = r.json.get('res', {}).get('rec_texts', [])

        if os.path.exists(temp_path):
            os.remove(temp_path)
        return parse_watermark_text(texts)
    except Exception as e:
        print("  水印裁剪OCR异常:", type(e).__name__, str(e)[:80])
        if 'temp_path' in dir() and os.path.exists(temp_path):
            os.remove(temp_path)
        return "", ""


# ═══════════════════════════════════════════════════════════════════════════════
# 七、保存
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
# 八、行处理
# ═══════════════════════════════════════════════════════════════════════════════

def _make_result(is_compliant, seal_exists, position_valid,
                 rel_x, rel_y, box_method, detail,
                 dual_code, dual_detail,
                 watermark_time, watermark_location,
                 sticker_angle=None,
                 lob: str = "",
                 rectify_method: str = "",
                 box_quad_src=None,
                 unofficial_color_checked: int = 0,
                 unofficial_color_mode: str = "") -> dict:
    """构造统一的返回 dict（避免各处重复写键名）。"""
    return {
        "is_compliant":             is_compliant,
        "seal_exists":              seal_exists,
        "position_valid":           position_valid,
        "rel_x":                    rel_x,
        "rel_y":                    rel_y,
        "sticker_angle":            sticker_angle,
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
    处理单行订单（1~4 张图片），返回合规判定结果。

    多 LOB 流程：
      Phase 0  识别 LOB（Excel LOB 列优先 → 产品名关键词降级 → iPhone 兜底）

      Phase 1  遍历全部图片
                 • OCR
                 • 包装盒检测（detect_box_bbox）→ 验证正向完整
                 • 符合条件者记入 candidates

      Phase 2  选盒子占比最大的候选；对其执行包装盒透视矫正
                 rectify_package_box() → warped_img / M / W_rect×H_rect / method
               将原图 OCR 多边形映射到矫正坐标系

      Phase 3  单贴检测（以矫正坐标 + LOB 配置判定）
               Step 0  非官方贴纸颜色检测（按 LOB mode 分流，enabled=False 时跳过）
               Step A  位置验证
               Step B  角度验证
               Step C  平铺错误检测

      Phase 4  双贴纸检测（按 LOB sticker_count 与 auth_sticker 位置规范判定）
    """
    print(f"\n{'='*80}")
    print(f"处理第 {idx}/{total} 行 (订单号: {row.get('订单号', 'N/A')})")
    print('=' * 80)

    # ═══ Phase 0：LOB 识别 ══════════════════════════════════════════════════════
    lob = detect_lob(row)
    if lob == UNRECOGNIZED_LOB:
        # 无法识别时用 iPhone 规则兜底，但输出列保留 "无法识别"，方便后续人工复核
        lob_cfg = LOB_CONFIGS[DEFAULT_LOB]
        print(f"  LOB: {lob}（兜底规则: {DEFAULT_LOB}）")
    else:
        lob_cfg = LOB_CONFIGS[lob]
        print(f"  LOB: {lob}  (sticker_count={lob_cfg['sticker_count']})")

    scan_cfg     = lob_cfg["scan_sticker"]
    auth_cfg     = lob_cfg.get("auth_sticker")
    color_cfg    = lob_cfg.get("unofficial_color", {"enabled": False})
    sc_mode      = lob_cfg.get("sticker_count", "single_or_dual")

    watermark_time, watermark_location = "", ""
    watermark_extracted = False
    tasks = prefetched_tasks if prefetched_tasks is not None else submit_row_downloads(row)

    # ═══ Phase 1：遍历所有图片，筛选候选 ═══════════════════════════════════════
    candidates: list[dict] = []
    dealer_only_hit = False
    dealer_only_text = ""

    for col_idx, col, url, future in tasks:
        print(f"\n  第{col_idx}张图片: {url[:80]}...")
        image = future.result()
        if image is None:
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
        print(f"  识别文字预览: {full_text[:120]}{'...' if len(full_text) > 120 else ''}")

        if not any("扫码即领" in t for t in texts):
            dealer_only, hit_text = has_dealer_only_sticker(texts)
            if dealer_only and not dealer_only_hit:
                dealer_only_hit = True
                dealer_only_text = hit_text
                print(f"  → 检测到疑似经销商自贴（关键词: {hit_text[:30]}），且无官方'扫码即领'")
            print("  → 未检测到'扫码即领'，非背面图，跳过")
            continue

        box_x, box_y, box_w, box_h, box_method = detect_box_bbox(image)

        if box_method == "fallback":
            print(f"  → 包装盒检测失败（fallback），不视为正向完整照片，跳过")
            continue
        box_ratio = (box_w * box_h) / max(orig_w * orig_h, 1)
        if box_ratio < BOX_FRONTAL_MIN_RATIO:
            print(f"  → 包装盒面积占比={box_ratio:.2f} < {BOX_FRONTAL_MIN_RATIO}，非完整照片，跳过")
            continue

        print(f"  → 候选图片 ✓  盒子占比={box_ratio:.2f}，方法={box_method}")
        candidates.append({
            "image":       image,
            "texts":       texts,
            "polys_orig":  polys_orig,
            "orig_h":      orig_h,
            "orig_w":      orig_w,
            "box_x":       box_x, "box_y": box_y,
            "box_w":       box_w, "box_h": box_h,
            "box_method":  box_method,
            "box_ratio":   box_ratio,
        })

    # ═══ Phase 2：无候选 → 不合格 ═══════════════════════════════════════════
    if not candidates:
        if dealer_only_hit:
            result = _make_result(
                is_compliant=0, seal_exists=0, position_valid=0,
                rel_x=None, rel_y=None, box_method=None,
                detail=f"检测到经销商自贴且无官方封口贴（关键词: {dealer_only_text[:50]}）",
                dual_code=-1, dual_detail="跳过",
                watermark_time=watermark_time, watermark_location=watermark_location,
                lob=lob,
            )
            _print_summary(result)
            return result
        result = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=None,
            detail="无正向完整包装盒背面照片（含'扫码即领'且盒子可见）",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob,
        )
        _print_summary(result)
        return result

    best = max(candidates, key=lambda c: c["box_ratio"])
    print(f"\n  ✓ 选定候选图：盒子占比={best['box_ratio']:.2f}，方法={best['box_method']}")

    # ═══ Phase 2.5：包装盒透视矫正（只对 best 做一次）══════════════════════════
    rectify = rectify_package_box(best["image"])
    rect_method  = rectify["method"]
    warped_img   = rectify["warped_img"]
    M            = rectify["M"]
    W_rect       = int(rectify["W_rect"])
    H_rect       = int(rectify["H_rect"])
    box_quad_src = rectify["box_quad_src"]
    print(f"  矫正方式: {rect_method}  矫正尺寸: {W_rect}×{H_rect}")

    # 将原图 OCR 多边形映射到矫正坐标系
    polys_rect = transform_polys(
        best["polys_orig"], M,
        box_x=best["box_x"] if M is None else 0,
        box_y=best["box_y"] if M is None else 0,
    )

    # ═══ Phase 3：单贴检测 ════════════════════════════════════════════════════

    # Step 0：非官方贴纸颜色检测（按 LOB 配置分流）
    color_checked = 0
    color_mode = ""
    if color_cfg.get("enabled", False):
        color_mode = color_cfg.get("mode", "white_box")
        color_checked = 1
        has_unoff, unoff_detail = detect_unofficial_sticker_color(warped_img, color_cfg)
        if has_unoff:
            print(f"  ⚠ 颜色检测命中：{unoff_detail}")
            result = _make_result(
                is_compliant=0, seal_exists=1, position_valid=4,
                rel_x=None, rel_y=None, box_method=best["box_method"],
                detail=f"检测到疑似经销商非官方贴纸：{unoff_detail}",
                dual_code=-1, dual_detail="跳过",
                watermark_time=watermark_time, watermark_location=watermark_location,
                lob=lob, rectify_method=rect_method,
                box_quad_src=box_quad_src,
                unofficial_color_checked=1, unofficial_color_mode=color_mode,
            )
            _print_summary(result)
            return result
        else:
            print(f"  颜色检测 ({color_mode})：未发现异色区域")
    else:
        print(f"  颜色检测：LOB={lob} 当前 enabled=False，跳过")

    # 在矫正坐标系里定位扫码贴
    sticker_rect = find_sticker_from_ocr(best["texts"], polys_rect)
    if sticker_rect is None:
        result = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=best["box_method"],
            detail="候选图OCR二次定位'扫码即领'失败",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(result)
        return result

    # Step A/B/C：位置 + 角度 + 平铺
    placement = check_sticker_placement(
        sticker_rect, W_rect, H_rect, rect_method,
        best["texts"], polys_rect, scan_cfg,
    )

    _angle = placement.get("angle_deg")

    if placement["position_valid"] != 1:
        result = _make_result(
            is_compliant=0, seal_exists=1,
            position_valid=placement["position_valid"],
            rel_x=placement["rel_x"], rel_y=placement["rel_y"],
            box_method=best["box_method"], detail=placement["detail"],
            dual_code=-1,
            dual_detail="单贴不合规，跳过双贴检测",
            watermark_time=watermark_time, watermark_location=watermark_location,
            sticker_angle=_angle,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(result)
        return result

    # ═══ Phase 4：双贴纸检测（单贴合规后进行）════════════════════════════════
    dual = check_dual_sticker_status(
        best["texts"], polys_rect, H_rect if H_rect > 0 else best["orig_h"],
        sticker_count_mode=sc_mode,
    )

    if dual["dual_code"] == 2:
        result = _make_result(
            is_compliant=0, seal_exists=1, position_valid=1,
            rel_x=placement["rel_x"], rel_y=placement["rel_y"],
            box_method=best["box_method"], detail=placement["detail"],
            dual_code=2, dual_detail=dual["dual_detail"],
            watermark_time=watermark_time, watermark_location=watermark_location,
            sticker_angle=_angle,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(result)
        return result

    if dual["dual_code"] == 3:
        # dual_required 且缺二贴 → 不合格
        result = _make_result(
            is_compliant=0, seal_exists=1, position_valid=1,
            rel_x=placement["rel_x"], rel_y=placement["rel_y"],
            box_method=best["box_method"], detail=placement["detail"],
            dual_code=3, dual_detail=dual["dual_detail"],
            watermark_time=watermark_time, watermark_location=watermark_location,
            sticker_angle=_angle,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(result)
        return result

    # 有 Auth 关键词 且该 LOB 有 auth_sticker 规范 → 验证位置
    if dual["has_auth"] and auth_cfg is not None:
        auth_pos = check_auth_sticker_position(
            best["texts"], polys_rect, W_rect, H_rect, auth_cfg,
        )
        if not auth_pos["found"]:
            dual = {**dual, "dual_code": 0,
                    "dual_detail": "单贴：'Apple授权专营店'在盒子外，忽略"}
        elif not auth_pos["in_correct_position"]:
            result = _make_result(
                is_compliant=0, seal_exists=1, position_valid=1,
                rel_x=placement["rel_x"], rel_y=placement["rel_y"],
                box_method=best["box_method"], detail=placement["detail"],
                dual_code=1,
                dual_detail=f"双贴第二张位置异常：{auth_pos['detail']}",
                watermark_time=watermark_time, watermark_location=watermark_location,
                sticker_angle=_angle,
                lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
                unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
            )
            _print_summary(result)
            return result
        else:
            result = _make_result(
                is_compliant=1, seal_exists=1, position_valid=1,
                rel_x=placement["rel_x"], rel_y=placement["rel_y"],
                box_method=best["box_method"], detail=placement["detail"],
                dual_code=1, dual_detail=f"双贴合规：{auth_pos['detail']}",
                watermark_time=watermark_time, watermark_location=watermark_location,
                sticker_angle=_angle,
                lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
                unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
            )
            _print_summary(result)
            return result

    # 单贴 / single_only 模式 → 合规
    result = _make_result(
        is_compliant=1, seal_exists=1, position_valid=1,
        rel_x=placement["rel_x"], rel_y=placement["rel_y"],
        box_method=best["box_method"], detail=placement["detail"],
        dual_code=dual["dual_code"], dual_detail=dual["dual_detail"],
        watermark_time=watermark_time, watermark_location=watermark_location,
        sticker_angle=_angle,
        lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
        unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
    )
    _print_summary(result)
    return result


def _print_summary(r: dict):
    print(f"\n【结果汇总】")
    print(f"  LOB           : {r.get('lob', '')}")
    print(f"  是否规范粘贴  : {'✓ 合规(1)' if r['is_compliant'] == 1 else '✗ 不合规(0)'}")
    print(f"  封口贴存在    : {r['seal_exists']}")
    print(f"  位置规范      : {r['position_valid']}")
    angle_str = f"{r['sticker_angle']:+.1f}°" if r.get('sticker_angle') is not None else "(未计算)"
    print(f"  贴纸相对X/Y   : {r['rel_x']} / {r['rel_y']}  偏角: {angle_str}")
    print(f"  包装盒检测方式: {r['box_method']}  矫正方式: {r.get('rectify_method', '')}")
    print(f"  颜色检测      : checked={r.get('unofficial_color_checked', 0)}  mode={r.get('unofficial_color_mode', '')}")
    print(f"  说明          : {r['detail']}")
    print(f"  双贴纸状态    : {r['dual_code']}  ({r['dual_detail']})")
    print(f"  水印时间      : {r['watermark_time'] or '(未识别)'}")
    print(f"  水印地点      : {r['watermark_location'] or '(未识别)'}")


# ═══════════════════════════════════════════════════════════════════════════════
# 九、主流程
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    input_file   = '/home/ubuntu/OCR/????? 件附件内容-0407-V2.xlsx'
    output_csv   = '/home/ubuntu/OCR/????? 件附件内容-0407-V2_results.csv'
    output_json  = '/home/ubuntu/OCR/????? 件附件内容-0407-V2_results.jsonl'
    output_excel = '/home/ubuntu/OCR/????? 件附件内容-0407-V2_processed.xlsx'

    NEW_COLS = [
        '识别LOB',          # iPhone / Watch / AirPods / Accy. / iPad / Mac
        '是否规范粘贴',     # 0=不合规 | 1=合规  ← 总体判断，放最前
        '封口贴存在',       # 0 / 1
        '贴纸位置规范',     # -1=无贴纸 | 0=位置异常 | 1=位置规范 | 2=平铺错误 | 3=角度异常 | 4=非官方贴纸
        '贴纸相对X',        # 0.0~1.0（相对包装盒宽度，矫正坐标系）
        '贴纸相对Y',        # 0.0~1.0（相对包装盒高度，矫正坐标系）
        '贴纸角度',         # 贴纸长轴与包装盒水平方向的偏角（°）；空=未计算
        '包装盒检测方式',   # edge / bright / fallback
        '矫正方式',         # perspective / rotation / axis_aligned
        '包装盒四点坐标',   # 原图坐标系 4 角点 JSON（axis_aligned 时为空）
        '颜色检测已执行',   # 0 / 1
        '颜色检测模式',     # white_box / brown_box / 空
        '位置说明',         # 文字说明（含角度/位置/平铺详情）
        '双贴纸状态',       # -1=无贴 | 0=单贴 | 1=双贴合规 | 2=双贴错误 | 3=缺二贴(dual_required)
        '双贴纸说明',       # 文字说明
        '时间',
        '地点',
    ]

    print(f"正在读取Excel文件: {input_file}")
    df = pd.read_excel(input_file)
    print(f"总共有 {len(df)} 行数据")

    # 断点续传
    if os.path.exists(output_csv):
        print(f"\n发现已存在的结果文件: {output_csv}")
        df_existing = pd.read_csv(output_csv, encoding='utf-8-sig')
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

    total_rows = len(df)
    start_time = time.time()

    # 流水线预下载
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
        tasks = prefetch_cache.pop(idx, None)
        order_id = str(row.get('订单号', ''))

        try:
            r = process_row(row, idx + 1, total_rows, prefetched_tasks=tasks)

            result_row = row.to_dict()
            result_row['识别LOB']        = r.get('lob', '')
            result_row['是否规范粘贴']   = r['is_compliant']
            result_row['封口贴存在']     = r['seal_exists']
            result_row['贴纸位置规范']   = r['position_valid']
            result_row['贴纸相对X']      = r['rel_x'] if r['rel_x'] is not None else ''
            result_row['贴纸相对Y']      = r['rel_y'] if r['rel_y'] is not None else ''
            result_row['贴纸角度']       = r['sticker_angle'] if r.get('sticker_angle') is not None else ''
            result_row['包装盒检测方式'] = r['box_method'] if r['box_method'] else ''
            result_row['矫正方式']       = r.get('rectify_method', '')
            box_quad = r.get('box_quad_src')
            result_row['包装盒四点坐标'] = (
                json.dumps(box_quad, ensure_ascii=False) if box_quad else ''
            )
            result_row['颜色检测已执行'] = r.get('unofficial_color_checked', 0)
            result_row['颜色检测模式']   = r.get('unofficial_color_mode', '')
            result_row['位置说明']       = r['detail']
            result_row['双贴纸状态']     = r['dual_code']
            result_row['双贴纸说明']     = r['dual_detail']
            result_row['时间']           = r['watermark_time']
            result_row['地点']           = r['watermark_location']

            save_result_immediately(result_row, output_csv, output_json)
            print(f"  ✓ 已保存到 CSV 和 JSON")

        except Exception as e:
            print(f"\n处理第 {idx + 1} 行时出错: {str(e)}")
            print(traceback.format_exc()[:400])

            result_row = row.to_dict()
            for col in NEW_COLS:
                result_row[col] = 'ERROR'
            result_row['封口贴存在']   = -1
            result_row['贴纸位置规范'] = -1
            save_result_immediately(result_row, output_csv, output_json)

        # 每 50 行进度统计
        if (pi + 1) % 50 == 0:
            elapsed  = time.time() - start_time
            avg_time = elapsed / (pi + 1)
            remaining = (len(pending) - pi - 1) * avg_time
            print(f"\n{'='*80}")
            print(f"进度: {pi + 1}/{len(pending)} 待处理行  "
                  f"({(idx + 1) / total_rows * 100:.1f}% of 全量)")
            print(f"平均每行耗时: {avg_time:.2f}s  |  预计剩余: {remaining / 3600:.1f}h")
            print(f"{'='*80}\n")

    _dl_executor.shutdown(wait=False)

    print(f"\n{'='*80}")
    print("所有行处理完成！正在生成最终 Excel 文件...")
    df_final = pd.read_csv(output_csv, encoding='utf-8-sig')
    df_final.to_excel(output_excel, index=False)
    print(f"Excel 文件已保存: {output_excel}")

    def _cnt(col, val):
        return (df_final[col] == val).sum() if col in df_final.columns else 0

    cnt_compliant   = _cnt('是否规范粘贴', 1)
    cnt_fail        = _cnt('是否规范粘贴', 0)
    cnt_no_seal     = _cnt('封口贴存在', 0)
    cnt_pos_bad     = _cnt('贴纸位置规范', 0)
    cnt_angle_bad   = _cnt('贴纸位置规范', 3)
    cnt_flat        = _cnt('贴纸位置规范', 2)
    cnt_no_frontal  = _cnt('贴纸位置规范', -1)
    cnt_unofficial  = _cnt('贴纸位置规范', 4)
    cnt_dual_ok     = _cnt('双贴纸状态', 1)
    cnt_dual_err    = _cnt('双贴纸状态', 2)
    cnt_dual_miss   = _cnt('双贴纸状态', 3)

    print(f"\n{'='*80}")
    print(f"总共处理               : {total_rows} 行")
    if '识别LOB' in df_final.columns:
        print("LOB 分布:")
        for lob_val, cnt in df_final['识别LOB'].value_counts(dropna=False).items():
            print(f"  {lob_val}: {cnt}")
    if '矫正方式' in df_final.columns:
        print("矫正方式分布:")
        for rm, cnt in df_final['矫正方式'].value_counts(dropna=False).items():
            print(f"  {rm}: {cnt}")
    print(f"是否规范粘贴 ✓ 合规    : {cnt_compliant} 行")
    print(f"是否规范粘贴 ✗ 不合规  : {cnt_fail} 行")
    print(f"  ├─ 无正向完整照片    : {cnt_no_frontal} 行")
    print(f"  ├─ 无封口贴          : {cnt_no_seal} 行")
    print(f"  ├─ 位置异常          : {cnt_pos_bad} 行")
    print(f"  ├─ 角度异常          : {cnt_angle_bad} 行  (偏角 > {STICKER_ANGLE_MAX_DEG:.0f}°)")
    print(f"  ├─ 平铺错误          : {cnt_flat} 行  (端片未绕侧面)")
    print(f"  ├─ 非官方贴纸        : {cnt_unofficial} 行  (经销商彩色自贴)")
    print(f"  ├─ 双贴纸错误        : {cnt_dual_err} 行  (两个扫码贴)")
    print(f"  └─ 缺失二贴          : {cnt_dual_miss} 行  (dual_required)")
    print(f"双贴纸合规             : {cnt_dual_ok} 行  (扫码+授权专营)")
    print(f"总耗时                 : {(time.time() - start_time) / 3600:.2f} 小时")
    print(f"{'='*80}\n")


if __name__ == '__main__':
    main()
