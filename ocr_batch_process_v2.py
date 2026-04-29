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

# ── 贴纸位置"绕折容差" ───────────────────────────────────────────────────────
# 封口贴物理上跨越盒面与侧面/顶面/底面，OCR 识别"扫码即领"/"Apple授权专营店"
# 的文字中心点经过透视矫正后，可能落在盒面边界外侧（常见 5%~15%，窄盒或大贴纸
# 极端情况下更多）。为避免把正常绕折的合规贴纸误判为"位置异常"，在各 LOB
# 规范区域四周统一放宽 ±STICKER_POSITION_TOLERANCE（相对坐标系）。
# 该容差对"扫码即领"（scan_sticker）与"Apple授权专营店"（auth_sticker）同时生效。
STICKER_POSITION_TOLERANCE = 0.15


def _normalize_position_cfg(position_cfg) -> list[dict]:
    """
    把 LOB 的 scan_sticker / auth_sticker 配置统一转成"区域列表"：

      - None          → [] （无规范）
      - {..}          → [{..}]
      - [{..}, {..}]  → 原样返回

    目的：支持"多合规区域（任一即可）"的场景，例如 Watch 长条盒子封口贴
    贴在顶端 **或** 底端对称位置，两处都算规范。
    """
    if position_cfg is None:
        return []
    if isinstance(position_cfg, dict):
        return [position_cfg]
    if isinstance(position_cfg, (list, tuple)):
        return [c for c in position_cfg if isinstance(c, dict)]
    return []

# ── 正向完整拍摄判断阈值（极端兜底）────────────────────────────────────────
# 下游 rel_x/rel_y 已经按"贴纸 / 盒子"做了归一化，盒子绝对大小不应影响位置判定，
# 因此不能把"盒子占图比例小"等同于"非正向完整"——AirPods 这种小盒子 + 远拍场景，
# 占比 5%~15% 也是合规图。这里只保留极松的兜底，过滤纯模糊远景或盒子被裁飞的边角图。
BOX_FRONTAL_MIN_RATIO = 0.02

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
#   scan_sticker:    一贴（扫码即领）规范相对坐标，两种写法：
#                      - dict  {x_min,x_max,y_min,y_max}  单一合规区域
#                      - list[dict]                       多合规区域，命中任一即可
#                    （Watch 长条盒封口贴可在顶端 **或** 底端对称位置，故用 list）
#   auth_sticker:    二贴（Apple 授权专营店）规范相对坐标（同样支持 dict 或 list[dict]），
#                    None 表示该 LOB 无二贴
#   unofficial_color: 非官方贴纸颜色检测配置
#     enabled:        是否启用
#     mode:           "white_box" 白盒（白平衡归一化 + 相对饱和度，适用 iPhone/Watch/AirPods/Accy./iPad）
#                     "brown_box" 棕盒（排除棕 + 排除白 + 绝对饱和度，适用 Mac）
#     其他阈值:       详见各 LOB 字段
#   front_face_aspect_range:
#     正面长宽比（长边/短边，永远 >= 1.0）的合理区间，仅用于矫正阶段从多个候选
#     凸四边形中挑选"封口贴所在的正面"。Mac 等立体盒子拍照常同时露出正面+侧面
#     +顶面，axis-aligned bbox 会把它们框在一起导致 rel_x/rel_y 失真；
#     给定该 LOB 的正面长宽比后，矫正器优先挑长宽比落入该区间、且内部包含
#     "扫码即领"文字框的那个面作为坐标系基准。值为 None 时不约束。
LOB_CONFIGS: dict[str, dict] = {
    "iPhone": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        "auth_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.70, "y_max": 1.00},
        "front_face_aspect_range": (1.6, 2.4),   # iPhone 包装盒正面 ≈ 1.7~2.2
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
        # Watch 盒为长条形，封口贴沿盒子中轴线贴合并绕折到两侧面，业务约束：
        #   - 单贴：可出现在"顶端"或"底端"任一位置（用户业务允许对称位置）
        #   - 双贴：一张顶端 + 一张底端
        # x 方向不作硬约束（原因：长条盒矫正器稳定性较弱，rotation/perspective 误差大，
        #   文字中心相对坐标常溢出 [0,1] 很多；关键判据是 y 位置——是否落在顶端/底端）。
        # 因此 scan/auth 都声明两个"只约束 y"的合规区域（list[dict]），命中任一即合规。
        "scan_sticker": [
            {"y_min": 0.00, "y_max": 0.45},
            {"y_min": 0.55, "y_max": 1.00},
        ],
        "auth_sticker": [
            {"y_min": 0.00, "y_max": 0.45},
            {"y_min": 0.55, "y_max": 1.00},
        ],
        "front_face_aspect_range": (2.5, 5.0),   # Watch 长条盒正面长宽比大
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
        "front_face_aspect_range": (0.85, 1.35),  # AirPods 盒近似正方形
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
        "front_face_aspect_range": None,         # 配件类目尺寸差异大，不约束
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
        "front_face_aspect_range": (1.2, 1.7),   # iPad 包装盒正面 ≈ 1.3~1.5
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
        "front_face_aspect_range": (1.2, 2.0),   # MacBook 包装盒正面 ≈ 1.4~1.7
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

# 无法识别 LOB 时的输出字符串。
# 说明：不再使用 iPhone 兜底 —— LOB 未知时直接判不合格，
# 避免用错误规则跑出"看似正常"的 rel_x/rel_y，遮盖真实问题。
UNRECOGNIZED_LOB = "UNKNOWN LOB"

def detect_lob(row) -> str:
    """
    识别订单行对应的 LOB（产品线）。

    权威来源：Excel 的 `LOB` 列（枚举：iPhone / Watch / AirPods / Accy. / iPad / Mac）。
    为避免关键词降级里「门店名称包含 Mac/iPad 等字样」误匹配错误产品线，
    本版本移除了 MPN / UPC / 门店名称的关键词降级路径。

    返回值：
      - 命中 LOB_CONFIGS key（精确匹配，strip 后比对）→ 返回该 key
      - 其他任何情况（缺失 / NaN / 不在枚举内 / 异常）→ 返回 UNRECOGNIZED_LOB

    调用方拿到 UNRECOGNIZED_LOB 时应直接跳过 OCR 流程、标记不合格，
    不要再用 iPhone 规则兜底。
    """
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

# 棕色瓦楞纸盒 HSV 范围（适配 Mac 包装箱）
# H: 5~30°（OpenCV 0~180 范围内的橙棕色带）
# S: 30~200  避开纯灰 / 过饱和异常
# V: 40~220  避开阴影与过曝
BROWN_HSV_LOW  = (5,  30, 40)
BROWN_HSV_HIGH = (30, 200, 220)


def detect_box_bbox(
    image_pil: Image.Image,
    lob: str | None = None,
) -> tuple[int, int, int, int, str]:
    """
    检测图片中产品包装盒的边界框。

    检测策略（按优先级降级）：
      1. Canny 边缘 + 形态学膨胀 → 最大矩形轮廓（通用，不分 LOB）
      2. 颜色区域阈值 → 最大外包矩形，按 LOB 分流：
           lob == 'Mac'          → HSV 棕色范围（瓦楞纸盒）     method='brown'
           其他（含 lob is None） → 灰度亮色阈值（白盒）          method='bright'
      3. 降级：返回整图区域（不丢数据，仍输出相对坐标供人工核查）

    参数:
      image_pil : PIL Image，不缩放输入
      lob       : LOB key；None 时按白盒处理（向下兼容旧调用）
    返回:
      (x, y, w, h, method_used)
      - x, y, w, h  均为原图像素坐标
      - method_used : 'edge' | 'bright' | 'brown' | 'fallback'
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

    # ── 策略 1：Canny 边缘检测（通用，不分 LOB）─────────────────────────────
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

    # ── 策略 2：颜色区域阈值（按 LOB 分流）─────────────────────────────────
    if lob == "Mac":
        hsv = cv2.cvtColor(img_small, cv2.COLOR_BGR2HSV)
        thresh = cv2.inRange(hsv, np.array(BROWN_HSV_LOW, dtype=np.uint8),
                                   np.array(BROWN_HSV_HIGH, dtype=np.uint8))
        method_name = 'brown'
    else:
        _, thresh = cv2.threshold(filtered, 190, 255, cv2.THRESH_BINARY)
        method_name = 'bright'

    # 去掉图像边界 1% 的噪声（灯光反射 / 相机暗角）
    border = max(5, int(min(dW, dH) * 0.01))
    thresh[:border, :]  = 0;  thresh[-border:, :] = 0
    thresh[:, :border]  = 0;  thresh[:, -border:] = 0

    # 棕盒走形态学闭运算 + 开运算消除瓦楞条纹空洞，白盒保持原逻辑（不做额外形态学）
    if lob == "Mac":
        k = np.ones((7, 7), np.uint8)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k, iterations=2)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN,  k, iterations=1)

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
                method_name
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


# ─── 内部常量：候选凸四边形的几何最小要求 ────────────────────────────────────
_QUAD_MAX_ASPECT = 8.0   # 允许细长侧面进入候选，由打分把它们排出去
_QUAD_MIN_EDGE   = 10    # 任一边短于 10px → 噪点


def _quad_from_contour(cnt: np.ndarray) -> np.ndarray | None:
    """
    把单个轮廓拟合成形状为 (4,2) 的凸四边形（缩放图坐标系）。

    优先 approxPolyDP（边缘清晰），不行则用 minAreaRect 兜底。
    AirPods/Mac 这类实拍场景里 Canny 主轮廓常呈现 5~6 凹点
    （边角磨圆 + 阴影边缘），必须靠 minAreaRect 才能得到可矫正的四边形。
    """
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
            aspect = max(w_hat, h_hat) / max(min(w_hat, h_hat), 1.0)
            if aspect > _QUAD_MAX_ASPECT:
                return None
            return cand
        if len(approx) > 4:
            continue
        if len(approx) < 4:
            break

    # minAreaRect 兜底
    try:
        rect = cv2.minAreaRect(cnt)
        cw, ch = rect[1]
        if min(cw, ch) >= _QUAD_MIN_EDGE:
            aspect = max(cw, ch) / max(min(cw, ch), 1.0)
            if aspect <= _QUAD_MAX_ASPECT:
                return cv2.boxPoints(rect).astype(np.float32)
    except Exception:
        return None
    return None


def _find_quads_canny(
    img_cv: np.ndarray,
    max_candidates: int = 8,
    min_area_ratio: float = 0.03,
) -> list[np.ndarray]:
    """
    通用路径：Canny 边缘 + 形态学膨胀 → 外部轮廓 → 候选凸四边形。

    适用于白色包装盒（iPhone/iPad/Watch/AirPods/Accy.）以及 Mac 平铺正面拍摄。
    立体棕盒拍摄（多个面共面）效果差，需配合 _find_quads_brown_split 补充。
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

    quads: list[np.ndarray] = []
    for cnt in contours[:25]:
        if cv2.contourArea(cnt) < min_area_ratio * dW * dH:
            continue
        quad_small = _quad_from_contour(cnt)
        if quad_small is not None:
            quads.append((quad_small / scale).astype(np.float32))
        if len(quads) >= max_candidates:
            break
    return quads


def _find_quads_brown_split(
    img_cv: np.ndarray,
    max_candidates: int = 8,
    min_area_ratio: float = 0.03,
) -> list[np.ndarray]:
    """
    Mac 棕盒专用：HSV 棕色掩码 → 盒子主体 → 在主体内部用敏感 Canny
    检测折线 → 把折线膨胀作为"分割线"切分主体 → 每个子面用 minAreaRect
    得到候选四边形。

    设计动机：
      Mac 包装是棕色瓦楞纸盒，门店常斜放立体拍摄（正面 + 顶面 + 侧面同框）。
      _find_quads_canny 的 Canny+膨胀策略在棕色低对比场景下会把"盒子+背景反光"
      合并成一个外包络，得不到分开的"正面/顶面"候选，rel_x/rel_y 完全失真。
      本函数先用棕色掩码隔离盒子主体，再在内部用低阈值 Canny + 膨胀切分子面，
      让正面/顶面/侧面各自成为独立候选，配合 _pick_front_quad 长宽比 + 含
      扫码即领文字框 打分即可挑出真正的"封口贴所在面"。

    实测：row738_img2（立体盒）能切出正面（占图 26.5%，长宽比 1.17）。
    """
    H, W = img_cv.shape[:2]
    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    # ── 棕色掩码（盒子主体）──────────────────────────────────────────────
    hsv = cv2.cvtColor(img_small, cv2.COLOR_BGR2HSV)
    brown_mask = cv2.inRange(
        hsv,
        np.array(BROWN_HSV_LOW, dtype=np.uint8),
        np.array(BROWN_HSV_HIGH, dtype=np.uint8),
    )
    border = max(5, int(min(dW, dH) * 0.01))
    brown_mask[:border, :]  = 0; brown_mask[-border:, :] = 0
    brown_mask[:, :border]  = 0; brown_mask[:, -border:] = 0
    k7 = np.ones((7, 7), np.uint8)
    brown_mask = cv2.morphologyEx(brown_mask, cv2.MORPH_CLOSE, k7, iterations=2)
    brown_mask = cv2.morphologyEx(brown_mask, cv2.MORPH_OPEN,  k7, iterations=1)

    contours, _ = cv2.findContours(brown_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    box_outline = contours[0]
    if cv2.contourArea(box_outline) < min_area_ratio * dW * dH:
        return []

    box_mask = np.zeros((dH, dW), dtype=np.uint8)
    cv2.drawContours(box_mask, [box_outline], -1, 255, -1)

    # ── 在盒子内部用敏感 Canny 找折线，bitwise 切分子面 ──────────────────
    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)
    inner_edges = cv2.Canny(filtered, 5, 25)   # 比通用路径敏感得多
    inner_edges[box_mask == 0] = 0
    edges_dil = cv2.dilate(inner_edges, np.ones((5, 5), np.uint8), iterations=2)
    sub_mask = cv2.bitwise_and(box_mask, cv2.bitwise_not(edges_dil))

    sub_cnts, _ = cv2.findContours(sub_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    sub_cnts = sorted(sub_cnts, key=cv2.contourArea, reverse=True)

    quads: list[np.ndarray] = []
    for cnt in sub_cnts[:15]:
        if cv2.contourArea(cnt) < min_area_ratio * dW * dH:
            continue
        quad_small = _quad_from_contour(cnt)
        if quad_small is not None:
            quads.append((quad_small / scale).astype(np.float32))
        if len(quads) >= max_candidates:
            break
    return quads


def _dedup_quads(
    quads: list[np.ndarray],
    center_threshold: float = 50.0,
) -> list[np.ndarray]:
    """
    按四边形中心点欧氏距离去重，避免两路检测合并后出现近似重复候选。
    后入候选若与已存在候选的中心距 < center_threshold，则丢弃。
    """
    out: list[np.ndarray] = []
    for q in quads:
        cx = float(q[:, 0].mean())
        cy = float(q[:, 1].mean())
        is_dup = False
        for q2 in out:
            cx2 = float(q2[:, 0].mean())
            cy2 = float(q2[:, 1].mean())
            if (cx - cx2) ** 2 + (cy - cy2) ** 2 < center_threshold ** 2:
                is_dup = True
                break
        if not is_dup:
            out.append(q)
    return out


def _find_box_quads(
    img_cv: np.ndarray,
    max_candidates: int = 8,
    min_area_ratio: float = 0.03,
    lob: str | None = None,
) -> list[np.ndarray]:
    """
    枚举图像中可能的包装盒"面"——返回多个候选凸四边形（原图坐标系）。

    检测路径按 LOB 自动分流：
      • 通用路径（_find_quads_canny）：所有 LOB 都跑。
      • 棕盒分割（_find_quads_brown_split）：仅 Mac 跑，弥补 Canny 在棕色
        低对比+立体拍摄下"多面合并外包络"的缺陷。
    两路结果合并后按中心点去重，按面积降序返回。

    上层 _pick_front_quad 配合 LOB 正面长宽比 + "扫码即领"文字框位置打分
    挑选真正的封口贴正面。

    返回：list[np.ndarray]，每个 shape=(4,2) float32；找不到返回 []。
    """
    quads = _find_quads_canny(img_cv, max_candidates, min_area_ratio)

    if lob == "Mac":
        brown_quads = _find_quads_brown_split(img_cv, max_candidates, min_area_ratio)
        # 棕盒分割提供"内部各面"，通用路径提供"外包络/侧面" —— 两路互补
        quads = quads + brown_quads

    quads = _dedup_quads(quads)
    quads.sort(key=lambda q: cv2.contourArea(q), reverse=True)
    return quads[:max_candidates]


def _quad_size_aspect(quad: np.ndarray) -> tuple[float, float, float]:
    """
    根据四边形的 4 个角点估计"矫正后"宽高与长宽比（长/短，永远 >= 1.0）。
    采用 _order_quad_corners 排序后，取 (上/下) 两条边最长边为宽，
    (左/右) 两条边最长边为高，与 rectify_package_box 用同一约定。
    """
    ordered = _order_quad_corners(quad)
    tl, tr, br, bl = ordered
    w = max(float(np.linalg.norm(tr - tl)), float(np.linalg.norm(br - bl)))
    h = max(float(np.linalg.norm(bl - tl)), float(np.linalg.norm(br - tr)))
    long_edge  = max(w, h)
    short_edge = max(min(w, h), 1.0)
    return w, h, long_edge / short_edge


def _score_quad(
    quad: np.ndarray,
    scan_centers_orig: list[tuple[float, float]],
    aspect_range: tuple[float, float] | None,
    img_area: float,
) -> float:
    """
    给单个候选四边形打分，越高越像"封口贴所在的正面"。

    打分项（权重经过 row5/row738/row395 实测调参）：
      - contain_score (0 / 2.0)：四边形是否包住任意"扫码即领"文字框中心。
        最强信号——封口贴必在正面，正面必含扫码即领。
      - aspect_score  (0 ~ 1.5)：长宽比是否落入 LOB 正面区间。
        【命中区间得满分 1.5（高于 contain_score 之外的所有项之和）】
        这是为了在两个候选"都含扫码"的场景下（典型如 Mac 立体盒子的
        外包络 + 棕盒分割得到的真正正面），让"长宽比刚好正面"的候选
        显著领先于"长宽比偏出但稍大"的外包络。
        区间外按线性距离扣分。
      - area_score    (0 ~ 0.15)：tie-breaker。仅在前两项打平时区分大小。
        权重压低是因为外包络 area 更大但语义错误。
    """
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
            d = min(abs(aspect - lo), abs(aspect - hi))
            aspect_score = max(0.0, 1.0 - d * 0.6)

    area = float(cv2.contourArea(quad))
    area_score = min(1.0, (area / max(img_area, 1.0)) ** 0.5)

    return contain_score + aspect_score + 0.15 * area_score


def _pick_front_quad(
    quads: list[np.ndarray],
    scan_centers_orig: list[tuple[float, float]],
    aspect_range: tuple[float, float] | None,
    img_area: float,
) -> np.ndarray | None:
    """
    从多个候选凸四边形中挑选最有可能是"封口贴所在的正面"的那一个。
    候选为空返回 None；只有 1 个直接返回；多个时按 _score_quad 取分数最高。
    """
    if not quads:
        return None
    if len(quads) == 1:
        return quads[0]
    return max(
        quads,
        key=lambda q: _score_quad(q, scan_centers_orig, aspect_range, img_area),
    )


# ─── 兼容入口（旧调用：单个四边形）─────────────────────────────────────────
def _find_box_quad(img_cv: np.ndarray) -> np.ndarray | None:
    """向下兼容旧调用：返回面积最大的候选四边形（不做正面打分）。"""
    quads = _find_box_quads(img_cv, max_candidates=1, min_area_ratio=0.08)
    return quads[0] if quads else None


def rectify_package_box(
    image_pil: Image.Image,
    lob: str | None = None,
    scan_polys_orig: list | None = None,
) -> dict:
    """
    包装盒矫正（三级降级）：
        1) 四点透视矫正（perspective）：approxPolyDP 得到多个候选凸四边形，
           结合 LOB 正面长宽比 + "扫码即领"文字框落在哪个面 → 打分挑正面 →
           warpPerspective 把正面拉成矩形坐标系
        2) minAreaRect 旋转矫正（rotation）：最大轮廓最小外接旋转矩形 → warpAffine 后裁剪
        3) 轴对齐兜底（axis_aligned）：复用现行 detect_box_bbox 的轴对齐 bbox

    OCR 仍在原图上跑，识别完成后通过返回的 `M` 把原图 OCR 多边形映射到矫正坐标系。
    矫正后坐标系中，包装盒覆盖 (0, 0) ~ (W_rect, H_rect)，贴纸相对坐标 = 除以 W_rect/H_rect。

    参数：
      image_pil       : PIL Image
      lob             : LOB key，用于：
                          • 策略 1：读取 front_face_aspect_range 给候选四边形打分
                          • 策略 3：选择白盒/棕盒阈值（见 detect_box_bbox）
                        策略 2 走纯 minAreaRect，不分 LOB。
      scan_polys_orig : 原图坐标系下"扫码即领"文字框的 OCR 多边形列表（可选）。
                        提供时策略 1 会优先选"包含扫码即领文字"的那个面作为正面，
                        显著降低立体盒子（Mac 等）误把侧面/外包络当正面的概率。

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

    # ── 策略 1：四点透视矫正（多候选 + 正面打分挑选）──────────────────────
    quads = _find_box_quads(img_cv, lob=lob)
    quad: np.ndarray | None = None
    if quads:
        aspect_range = None
        if lob and lob in LOB_CONFIGS:
            aspect_range = LOB_CONFIGS[lob].get("front_face_aspect_range")

        scan_centers: list[tuple[float, float]] = []
        if scan_polys_orig:
            for poly in scan_polys_orig:
                try:
                    pts = np.array(poly, dtype=float)
                    scan_centers.append(
                        (float(pts[:, 0].mean()), float(pts[:, 1].mean()))
                    )
                except Exception:
                    continue

        quad = _pick_front_quad(
            quads, scan_centers, aspect_range, float(W_img * H_img)
        )

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

    # ── 策略 3：轴对齐 bbox 兜底（复用 detect_box_bbox，按 LOB 分流白/棕盒）────
    bx, by, bw, bh, _method = detect_box_bbox(image_pil, lob=lob)
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
    """
    返回第一个'扫码即领'文字框信息，找不到返回 None。

    注意：此函数仅为向下兼容保留（不再用于主贴选择）。
    新主流程使用 pick_best_scan_sticker() 按 LOB 规范区域挑主贴，
    避免"背景/印刷/多贴"时误取到 OCR 顺序靠前的非主贴。
    """
    stickers = find_all_scan_stickers(texts, polys)
    return stickers[0] if stickers else None


def _scan_sticker_distance_to_zone(
    rel_x: float,
    rel_y: float,
    scan_position_cfg,
) -> float:
    """
    计算单个扫码贴（归一化相对坐标）到 LOB 规范矩形区域的距离。

    约定（相对坐标系 0.0~1.0）：
      - 贴纸落在 [x_min, x_max] × [y_min, y_max] 内 → 距离 0
      - 落在区域外        → 距离 = 到最近边/角的欧氏距离（矩形外距离）
      - scan_position_cfg 支持 dict 或 list[dict]：多区域时取到最近区域的距离

    区域四个边界任意侧 cfg 缺失时按 ±∞ 处理（不约束该侧），
    避免 None/缺省导致 TypeError。
    """
    zones = _normalize_position_cfg(scan_position_cfg)
    if not zones:
        return float("inf")

    best = float("inf")
    for zone in zones:
        x_min = zone.get("x_min", -float("inf"))
        x_max = zone.get("x_max",  float("inf"))
        y_min = zone.get("y_min", -float("inf"))
        y_max = zone.get("y_max",  float("inf"))

        dx = 0.0
        if rel_x < x_min:
            dx = x_min - rel_x
        elif rel_x > x_max:
            dx = rel_x - x_max

        dy = 0.0
        if rel_y < y_min:
            dy = y_min - rel_y
        elif rel_y > y_max:
            dy = rel_y - y_max

        d = float((dx * dx + dy * dy) ** 0.5)
        if d < best:
            best = d
    return best


def pick_best_scan_sticker(
    scan_stickers: list[dict],
    W_rect: int,
    H_rect: int,
    scan_position_cfg: dict,
) -> dict | None:
    """
    从多个"扫码即领"候选中选择最可能是"主贴（封口贴）"的那一张。

    为什么需要这一步：
      实际订单场景存在一贴 / 二贴 / 多贴混合，OCR 返回顺序与位置
      无关，若简单取 stickers[0] 很容易把背景/印刷/副贴当主贴，
      导致位置验证 rel_x/rel_y 基于错误坐标而误判。

    选贴策略（LOB-aware，规则由 scan_position_cfg 驱动）：
      1) 把每个候选的 (cx, cy) 用矫正坐标 W_rect / H_rect 归一化成
         (rel_x, rel_y)；
      2) 计算 rel 点到该 LOB 规范区域的"外距离"
         （落入区域内 → 0；落在外面 → 欧氏距离到区域边/角）；
      3) 主贴 = 距离最小者；
      4) 平手（同距离：常见于多张贴纸全部落在规范区域内）→
         取 rel_y 较小者（更靠上/更靠规范锚点，对 iPhone/iPad/
         AirPods/Accy. 符合"右上一定是太阳码贴"的业务共识）；
         对底部贴纸 LOB（Mac：y_min=0.70）仍按 rel_y 最小排序
         不会改变"已在区域内"的合规判定，因此不特别分支。

    W_rect / H_rect <= 0 或 scan_position_cfg 为 None 时，
    退化为返回 scan_stickers[0]（与旧行为一致）。

    返回：被选中的 sticker dict，候选为空时返回 None。
    """
    if not scan_stickers:
        return None
    if scan_position_cfg is None or W_rect <= 0 or H_rect <= 0:
        return scan_stickers[0]

    def _key(s):
        rel_x = s["cx"] / W_rect
        rel_y = s["cy"] / H_rect
        dist = _scan_sticker_distance_to_zone(rel_x, rel_y, scan_position_cfg)
        return (dist, rel_y)

    return min(scan_stickers, key=_key)


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
    position_cfg,
    tolerance: float = STICKER_POSITION_TOLERANCE,
) -> dict:
    """
    验证已归一化的相对坐标是否满足规范位置（纯函数，无包装盒耦合）。

    position_cfg 支持两种形式：
      - dict            {"x_min", "x_max", "y_min", "y_max"} —— 单一合规区域
      - list[dict]      多合规区域，命中任一即合规。适用于 Watch 长条盒子：
                        封口贴可贴在盒子顶端 **或** 底端对称位置，两处均视为规范。

    业务约定：
      - x/y 双侧硬约束（x_min/x_max/y_min/y_max 任一缺省视为该侧不约束）
      - tolerance：绕折容差。封口贴跨越盒面边界到侧面/顶面/底面，OCR 文字中心
        经过透视矫正后常溢出盒面 5%~10%，此容差避免合规贴纸被误判。

    返回 dict:
      in_correct_position : bool
      rel_x, rel_y        : 相对坐标（四舍五入）
      x_ok, y_ok          : 各轴是否达标（多区域时指失败解释里"最接近"的那个区域）
      detail              : 文字说明
    """
    zones = _normalize_position_cfg(position_cfg)
    if not zones:
        return {
            "in_correct_position": False,
            "rel_x": round(rel_cx, 4) if rel_cx is not None else None,
            "rel_y": round(rel_cy, 4) if rel_cy is not None else None,
            "x_ok": False, "y_ok": False,
            "detail": "位置验证跳过（该 LOB 未配置规范位置）",
        }

    tol = max(float(tolerance), 0.0)

    def _check_zone(zone: dict):
        x_min = zone.get("x_min", -float("inf"))
        x_max = zone.get("x_max",  float("inf"))
        y_min = zone.get("y_min", -float("inf"))
        y_max = zone.get("y_max",  float("inf"))

        x_lo = x_min - tol if x_min != -float("inf") else x_min
        x_hi = x_max + tol if x_max !=  float("inf") else x_max
        y_lo = y_min - tol if y_min != -float("inf") else y_min
        y_hi = y_max + tol if y_max !=  float("inf") else y_max

        x_ok = x_lo <= rel_cx <= x_hi
        y_ok = y_lo <= rel_cy <= y_hi
        return x_ok, y_ok, (x_min, x_max, y_min, y_max)

    # 逐个区域校验：命中任一即规范；失败时用"命中轴数最多"的区域作解释，减少 detail 噪音
    best_fail = None
    for zone in zones:
        x_ok, y_ok, bounds = _check_zone(zone)
        if x_ok and y_ok:
            return {
                "in_correct_position": True,
                "rel_x": round(rel_cx, 4),
                "rel_y": round(rel_cy, 4),
                "x_ok": True, "y_ok": True,
                "detail": (f"位置规范 (rel_x={rel_cx:.3f}, rel_y={rel_cy:.3f}, "
                           f"绕折容差±{tol:.2f})"),
            }
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
    detail = f"位置异常{multi}：" + "；".join(parts)

    return {
        "in_correct_position": False,
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
      sticker_rect     : pick_best_scan_sticker(polys_rect, ..., scan_cfg) 按 LOB
                         规范区域筛选出的主贴中心（矫正坐标）
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
    # LOB 列缺失 / 不在枚举 → 直接标记不合格。
    # 历史方案用 iPhone 规则兜底继续跑，会产出"看似正常"的 rel_x/rel_y，
    # 反而遮盖 Excel 源数据质量问题，后续排查也无从下手。
    lob = detect_lob(row)
    if lob == UNRECOGNIZED_LOB:
        print(f"  LOB: {lob} —— 跳过 OCR 检测，直接标记不合格")
        result = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=None,
            detail=f"{UNRECOGNIZED_LOB}：Excel LOB 列缺失或不在枚举内，未执行 OCR 检测",
            dual_code=-1, dual_detail="跳过",
            watermark_time="", watermark_location="",
            lob=lob,
        )
        _print_summary(result)
        return result

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

        box_x, box_y, box_w, box_h, box_method = detect_box_bbox(image, lob=lob)

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

    # ═══ Phase 2.5：包装盒透视矫正（只对 best 做一次，按 LOB 分流白/棕盒兜底）══════
    # 把"扫码即领"OCR 多边形（原图坐标）传给矫正器，让透视矫正在多个候选面中
    # 优先选"包含扫码即领"的那个面作为正面，避免 Mac 立体盒子把侧面/外包络当正面。
    scan_polys_orig_for_rect = [
        best["polys_orig"][i]
        for i, t in enumerate(best["texts"])
        if "扫码即领" in t and i < len(best["polys_orig"])
    ]
    rectify = rectify_package_box(
        best["image"], lob=lob, scan_polys_orig=scan_polys_orig_for_rect,
    )
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

    # 在矫正坐标系里定位扫码贴：
    #   1) 找出所有 "扫码即领" 文字框（可能是多贴 / 背景 / 印刷重复）
    #   2) 按 LOB 的 scan_sticker 规范区域挑距离最近的作为主贴，
    #      避免 stickers[0] 顺序偏差带来的误判
    scan_candidates = find_all_scan_stickers(best["texts"], polys_rect)
    sticker_rect = pick_best_scan_sticker(
        scan_candidates, W_rect, H_rect, scan_cfg,
    )
    if len(scan_candidates) > 1 and sticker_rect is not None:
        print(f"  → '扫码即领'候选 {len(scan_candidates)} 个，"
              f"按 LOB={lob} 规范区域选中 text_idx={sticker_rect['text_idx']}")
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
        # 业务约定：两个"扫码即领"虽然不规范（应为扫码 + Apple授权专营店），
        # 但已贴上封口贴 → 仍按"是否规范粘贴=1"计入合规，dual_code 保留 2
        # 供事后人工抽检/筛选；位置规范也保持 1。
        result = _make_result(
            is_compliant=1, seal_exists=1, position_valid=1,
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
    input_file   = '/home/ubuntu/OCR/出库照片_商派小程序_W1W2W4_top2850.xlsx'
    output_csv   = '/home/ubuntu/OCR/出库照片_商派小程序_W1W2W4_top2850_results.csv'
    output_json  = '/home/ubuntu/OCR/出库照片_商派小程序_W1W2W4_top2850_results.jsonl'
    output_excel = '/home/ubuntu/OCR/出库照片_商派小程序_W1W2W4_top2850_processed.xlsx'

    NEW_COLS = [
        '识别LOB',          # iPhone / Watch / AirPods / Accy. / iPad / Mac
        '是否规范粘贴',     # 0=不合规 | 1=合规  ← 总体判断，放最前
        '封口贴存在',       # 0 / 1
        '贴纸位置规范',     # -1=无贴纸 | 0=位置异常 | 1=位置规范 | 2=平铺错误 | 3=角度异常 | 4=非官方贴纸
        '贴纸相对X',        # 0.0~1.0（相对包装盒宽度，矫正坐标系）
        '贴纸相对Y',        # 0.0~1.0（相对包装盒高度，矫正坐标系）
        '贴纸角度',         # 贴纸长轴与包装盒水平方向的偏角（°）；空=未计算
        '包装盒检测方式',   # edge / bright(白盒) / brown(Mac棕盒) / fallback
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
    print(f"  └─ 其中两个扫码贴    : {cnt_dual_err} 行  (按规范粘贴计入，仅供抽检)")
    print(f"是否规范粘贴 ✗ 不合规  : {cnt_fail} 行")
    print(f"  ├─ 无正向完整照片    : {cnt_no_frontal} 行")
    print(f"  ├─ 无封口贴          : {cnt_no_seal} 行")
    print(f"  ├─ 位置异常          : {cnt_pos_bad} 行")
    print(f"  ├─ 角度异常          : {cnt_angle_bad} 行  (偏角 > {STICKER_ANGLE_MAX_DEG:.0f}°)")
    print(f"  ├─ 平铺错误          : {cnt_flat} 行  (端片未绕侧面)")
    print(f"  ├─ 非官方贴纸        : {cnt_unofficial} 行  (经销商彩色自贴)")
    print(f"  └─ 缺失二贴          : {cnt_dual_miss} 行  (dual_required)")
    print(f"双贴纸合规             : {cnt_dual_ok} 行  (扫码+授权专营)")
    print(f"总耗时                 : {(time.time() - start_time) / 3600:.2f} 小时")
    print(f"{'='*80}\n")


if __name__ == '__main__':
    main()
