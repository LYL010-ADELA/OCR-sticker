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


def detect_unofficial_sticker_color(
    image_pil: Image.Image,
    box_x: int, box_y: int, box_w: int, box_h: int,
) -> tuple[bool, str]:
    """
    改进版：基于包装盒颜色归一化的非官方贴纸检测。

    核心改进：
      1. 白平衡归一化：采样包装盒高亮低饱和区域（真实白色背景），
         估计全局光照/阴影引起的色偏，将整个区域饱和度减去该基准偏置，
         消除门店灯光色偏、手部阴影等环境因素。
      2. 形状紧实度（Solidity）过滤：真实贴纸是紧凑矩形块（Solidity > 0.45），
         阴影/反光形状不规则，Solidity 低，可有效区分。
      3. 边缘清晰度过滤：贴纸边缘饱和度梯度大（界限清晰），
         阴影是渐变，梯度小，可进一步排除渐变色偏区域。

    检测区域：整个包装盒（内缩 2% 边距，排除盒子边框干扰）。

    返回: (has_unofficial: bool, detail: str)
    """
    try:
        img_cv = pil_to_cv(image_pil)

        # 截取整个包装盒区域（内缩 2% 避免盒子边框色块干扰）
        margin_x = max(1, int(box_w * 0.02))
        margin_y = max(1, int(box_h * 0.02))
        x1 = max(0, box_x + margin_x)
        y1 = max(0, box_y + margin_y)
        x2 = min(img_cv.shape[1], box_x + box_w - margin_x)
        y2 = min(img_cv.shape[0], box_y + box_h - margin_y)

        zone = img_cv[y1:y2, x1:x2]
        if zone.shape[0] < 10 or zone.shape[1] < 10:
            return False, ""

        # ── Step 1: 白平衡归一化 ──────────────────────────────────────────────
        # 将包装盒区域转为 HSV，浮点以便后续运算
        hsv_f = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV).astype(np.float32)
        s_raw = hsv_f[:, :, 1]   # 0~255
        v_raw = hsv_f[:, :, 2]   # 0~255

        # 高亮（V > 180）+ 低饱和（S < 55）的像素 = 包装盒真实白色背景
        white_mask = (v_raw > 180) & (s_raw < 55)
        if white_mask.sum() > 200:
            # 取白色参考区域的 90th 百分位饱和度作为光照色偏基准
            bg_sat_ref = float(np.percentile(s_raw[white_mask], 90))
        else:
            # 白色参考不足（深色盒子等特殊情况），不做归一化
            bg_sat_ref = 0.0

        # 有效饱和度 = 原始饱和度 − 背景基准（再减10点宽容量）
        eff_sat = np.clip(s_raw - bg_sat_ref - 10.0, 0.0, 255.0)

        # ── Step 2: 彩色像素掩码（归一化后） ────────────────────────────────
        v_min, v_max = UNOFFICIAL_VAL_RANGE
        color_mask = (
            (eff_sat > UNOFFICIAL_SAT_ABOVE_BG) &
            (v_raw > v_min) & (v_raw < v_max)
        ).astype(np.uint8) * 255

        # 形态学：合并相邻像素，消除噪点
        k5 = np.ones((5, 5), np.uint8)
        color_mask = cv2.dilate(color_mask, k5, iterations=2)
        color_mask = cv2.erode(color_mask, k5, iterations=2)

        # ── Step 3: 连通区分析 + 形状/边缘验证 ──────────────────────────────
        n_labels, label_img, stats, _ = cv2.connectedComponentsWithStats(
            color_mask, connectivity=8
        )
        zone_area = zone.shape[0] * zone.shape[1]

        # 预计算归一化饱和度梯度图（用于边缘清晰度检测）
        eff_sat_u8 = np.clip(eff_sat, 0, 255).astype(np.uint8)
        gx = cv2.Sobel(eff_sat_u8, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(eff_sat_u8, cv2.CV_32F, 0, 1, ksize=3)
        grad_mag = np.sqrt(gx ** 2 + gy ** 2)

        for label in range(1, n_labels):   # 跳过背景(0)
            area = int(stats[label, cv2.CC_STAT_AREA])
            ratio = area / max(zone_area, 1)
            if ratio < UNOFFICIAL_AREA_RATIO:
                continue

            comp_mask = (label_img == label).astype(np.uint8)

            # ── 形状紧实度（Solidity）────────────────────────────────────────
            # 贴纸是紧凑矩形，Solidity 高；阴影轮廓不规则，Solidity 低
            contours, _ = cv2.findContours(
                comp_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            if not contours:
                continue
            cnt = max(contours, key=cv2.contourArea)
            hull = cv2.convexHull(cnt)
            hull_area = cv2.contourArea(hull)
            if hull_area < 1:
                continue
            solidity = cv2.contourArea(cnt) / hull_area

            if solidity < UNOFFICIAL_SOLIDITY_MIN:
                # 形状太不规则，判定为阴影/光照伪影，跳过
                continue

            # ── 边缘清晰度（饱和度梯度） ─────────────────────────────────────
            # 提取连通区边界像素环（膨胀 − 腐蚀）
            k3 = np.ones((3, 3), np.uint8)
            boundary = (cv2.dilate(comp_mask, k3) - cv2.erode(comp_mask, k3)).astype(bool)
            mean_edge_grad = float(grad_mag[boundary].mean()) if boundary.any() else 0.0

            if mean_edge_grad < UNOFFICIAL_EDGE_GRAD_MIN:
                # 边缘为渐变，判定为阴影/环境光色偏，跳过
                continue

            return True, (
                f"包装盒内检测到非白色彩色区域"
                f"（归一化有效饱和色块占盒内面积 {ratio:.1%}，"
                f"紧实度 {solidity:.2f}，边缘梯度 {mean_edge_grad:.1f}，"
                f"背景色偏基准 S={bg_sat_ref:.1f}，疑为经销商自贴）"
            )

        return False, ""
    except Exception as e:
        return False, f"颜色检测异常: {type(e).__name__}"


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

def check_dual_sticker_status(texts: list[str], polys: list, img_h: int) -> dict:
    """
    检测双贴纸状态（单图内）。

    规范说明：
      单贴场景：只有一张"扫码即领"贴纸                     → dual_code=0
      双贴合规：一张"扫码即领" + 一张"Apple授权专营店"      → dual_code=1
      双贴错误：两张"扫码即领"（上下均为扫码贴）            → dual_code=2

    返回 dict:
      scan_count  : int  ── 独立"扫码即领"贴纸数量（按 Y 坐标聚类）
      has_auth    : bool ── 是否检测到"Apple授权专营店"贴纸
      dual_code   : int  ── 0=单贴 | 1=双贴合规 | 2=双贴错误
      dual_detail : str
    """
    scan_stickers = find_all_scan_stickers(texts, polys)
    has_auth = any(
        kw in text
        for text in texts
        for kw in ["Apple授权专营店", "授权专营店", "在你身边"]
    )

    # 通过 Y 坐标聚类统计独立贴纸数（Y 差 > 图片高度20% 视为不同贴纸）
    MIN_Y_GAP = max(img_h * 0.20, 50)
    distinct: list[dict] = []
    for s in scan_stickers:
        if not any(abs(s["cy"] - e["cy"]) < MIN_Y_GAP for e in distinct):
            distinct.append(s)
    scan_count = len(distinct)

    if scan_count >= 2:
        dual_code   = 2
        dual_detail = f"错误：检测到{scan_count}个'扫码即领'贴纸，上下均为扫码贴"
    elif scan_count == 1 and has_auth:
        dual_code   = 1
        dual_detail = "合规双贴：'扫码即领' + 'Apple授权专营店'"
    elif scan_count == 1:
        dual_code   = 0
        dual_detail = "单贴：仅'扫码即领'"
    else:
        dual_code   = -1
        dual_detail = "未找到'扫码即领'贴纸"

    return {
        "scan_count":  scan_count,
        "has_auth":    has_auth,
        "dual_code":   dual_code,
        "dual_detail": dual_detail,
    }


def find_sticker_from_ocr(texts: list[str], polys: list) -> dict | None:
    """返回第一个'扫码即领'文字框信息，找不到返回 None。"""
    stickers = find_all_scan_stickers(texts, polys)
    return stickers[0] if stickers else None


def find_all_auth_stickers_in_box(
    texts: list[str],
    polys: list,
    box_x: int,
    box_y: int,
    box_w: int,
    box_h: int,
) -> list[dict]:
    """
    返回所有中心点落在包装盒范围内的"Apple授权专营店"候选文字框列表。

    匹配关键词：'Apple授权专营店' / '授权专营店' / '在你身边'
    盒子外部（如桌面、背景、包装印刷）的同名文字会被过滤掉。
    """
    AUTH_KW = ["Apple授权专营店", "授权专营店", "在你身边"]
    results = []
    use_box_filter = box_w > 0 and box_h > 0
    for i, text in enumerate(texts):
        if any(kw in text for kw in AUTH_KW) and i < len(polys):
            try:
                poly = np.array(polys[i], dtype=float)
                x1, y1 = float(poly[:, 0].min()), float(poly[:, 1].min())
                x2, y2 = float(poly[:, 0].max()), float(poly[:, 1].max())
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                if use_box_filter:
                    if not (box_x <= cx <= box_x + box_w and box_y <= cy <= box_y + box_h):
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


# 保留旧名兼容内部调用（check_dual_sticker_status 里的全局 has_auth 检测）
def find_auth_sticker_from_ocr(
    texts: list[str],
    polys: list,
    box_x: int = 0,
    box_y: int = 0,
    box_w: int = 0,
    box_h: int = 0,
) -> dict | None:
    """返回盒子内第一个命中的 Auth 贴纸候选（兼容旧调用）。"""
    candidates = find_all_auth_stickers_in_box(texts, polys, box_x, box_y, box_w, box_h)
    return candidates[0] if candidates else None


def check_auth_sticker_position(
    texts: list[str], polys: list,
    box_x: int, box_y: int, box_w: int, box_h: int,
) -> dict:
    """
    验证"Apple授权专营店"贴纸是否在规范位置（底部区域）。

    规范：rel_x ∈ [AUTH_X_MIN, AUTH_X_MAX]
          rel_y ∈ [AUTH_Y_MIN, AUTH_Y_MAX]（底部 30%，即从上往下 70%~100%）

    盒子内可能存在多个命中项（贴纸本身 + 盒面印刷文字），
    只要任意一个满足位置条件即视为合规，以避免"先遇到印刷文字"的误判。

    返回 dict:
      found              : bool  ── 盒子内是否找到任意候选项
      in_correct_position: bool
      rel_x, rel_y       : float | None  ── 第一个合规候选的坐标（无合规则为第一个候选）
      detail             : str
    """
    candidates = find_all_auth_stickers_in_box(texts, polys, box_x, box_y, box_w, box_h)
    if not candidates:
        return {
            "found": False,
            "in_correct_position": False,
            "rel_x": None, "rel_y": None,
            "detail": "未找到'Apple授权专营店'贴纸（盒子内）",
        }

    # 遍历所有候选，任意一个满足位置条件即合规
    first_candidate_rel = None
    for auth in candidates:
        rel_x = (auth["cx"] - box_x) / box_w if box_w > 0 else -1.0
        rel_y = (auth["cy"] - box_y) / box_h if box_h > 0 else -1.0
        if first_candidate_rel is None:
            first_candidate_rel = (rel_x, rel_y)
        x_ok = AUTH_X_MIN <= rel_x <= AUTH_X_MAX
        y_ok = AUTH_Y_MIN <= rel_y <= AUTH_Y_MAX
        if x_ok and y_ok:
            return {
                "found": True,
                "in_correct_position": True,
                "rel_x": round(rel_x, 4),
                "rel_y": round(rel_y, 4),
                "detail": f"Apple授权专营店位置规范 (rel_x={rel_x:.3f}, rel_y={rel_y:.3f})",
            }

    # 所有候选均不合规，报告第一个候选的坐标
    rel_x, rel_y = first_candidate_rel
    parts = []
    if not (AUTH_X_MIN <= rel_x <= AUTH_X_MAX):
        parts.append(f"X={rel_x:.3f} 不在 [{AUTH_X_MIN},{AUTH_X_MAX}]")
    if not (AUTH_Y_MIN <= rel_y <= AUTH_Y_MAX):
        parts.append(f"Y={rel_y:.3f} 不在 [{AUTH_Y_MIN},{AUTH_Y_MAX}]（底部30%）")
    return {
        "found": True,
        "in_correct_position": False,
        "rel_x": round(rel_x, 4),
        "rel_y": round(rel_y, 4),
        "detail": "Apple授权专营店位置异常：" + "；".join(parts),
    }


def validate_sticker_position(
    sticker: dict,
    box_x: int, box_y: int, box_w: int, box_h: int
) -> dict:
    """
    将贴纸中心转换为包装盒相对坐标系，验证是否在规范位置。

    返回 dict:
      in_correct_position : bool
      rel_x, rel_y        : 相对坐标（0.0~1.0；超界则 <0 或 >1）
      x_ok, y_ok          : 各轴是否达标
      detail              : 文字说明
    """
    rel_x = (sticker["cx"] - box_x) / box_w if box_w > 0 else -1.0
    rel_y = (sticker["cy"] - box_y) / box_h if box_h > 0 else -1.0

    x_ok = STICKER_X_MIN <= rel_x <= STICKER_X_MAX
    # 业务更新：Y 仅做上限约束（rel_y <= 0.30）
    y_ok = rel_y <= STICKER_Y_MAX

    if x_ok and y_ok:
        detail = f"位置规范 (rel_x={rel_x:.3f}, rel_y={rel_y:.3f})"
    else:
        parts = []
        if not x_ok:
            parts.append(
                f"X={rel_x:.3f} 不在 [{STICKER_X_MIN},{STICKER_X_MAX}]"
            )
        if not y_ok:
            parts.append(
                f"Y={rel_y:.3f} 超过上限 {STICKER_Y_MAX}"
            )
        detail = "位置异常：" + "；".join(parts)

    return {
        "in_correct_position": x_ok and y_ok,
        "rel_x": round(rel_x, 4),
        "rel_y": round(rel_y, 4),
        "x_ok": x_ok,
        "y_ok": y_ok,
        "detail": detail,
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
    sticker: dict,
    box_x: int, box_y: int, box_w: int, box_h: int,
    box_method: str,
    texts: list[str],
    polys: list,
) -> dict:
    """
    对单张候选图的"扫码即领"贴纸做合规检测。

    调用前提：
      - sticker 已由 find_sticker_from_ocr() 定位（不为 None）
      - box_x/y/w/h 已由 detect_box_bbox() 计算（非 fallback）

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
    # ── Step 1：位置验证 ──────────────────────────────────────────────────────
    pos = validate_sticker_position(sticker, box_x, box_y, box_w, box_h)
    if not pos["in_correct_position"]:
        return {
            "position_valid": 0,
            "rel_x": pos["rel_x"],
            "rel_y": pos["rel_y"],
            "angle_deg": None,
            "detail": f"[{box_method}] {pos['detail']}",
        }

    # ── Step 2：角度验证（位置合规后才执行）─────────────────────────────────
    # 正向完整照片（Phase 1 已过滤）包装盒水平边近似水平，故 box_angle=0
    angle_ok, delta_deg, angle_detail = validate_angle(sticker, polys)
    if not angle_ok:
        return {
            "position_valid": 3,
            "rel_x": pos["rel_x"],
            "rel_y": pos["rel_y"],
            "angle_deg": delta_deg,
            "detail": f"[{box_method}] {angle_detail}",
        }

    # ── Step 3：平铺错误检测（位置 + 角度均合规后才执行）────────────────────
    flat, flat_detail = is_flat_sticker(texts, polys, sticker["text_idx"],
                                        box_x, box_y, box_w, box_h)
    if flat:
        return {
            "position_valid": 2,
            "rel_x": pos["rel_x"],
            "rel_y": pos["rel_y"],
            "angle_deg": delta_deg,
            "detail": f"[{box_method}] {flat_detail}",
        }

    return {
        "position_valid": 1,
        "rel_x": pos["rel_x"],
        "rel_y": pos["rel_y"],
        "angle_deg": delta_deg,
        "detail": f"[{box_method}] {pos['detail']}",
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
                 sticker_angle=None) -> dict:
    """构造统一的返回 dict（避免各处重复写键名）。"""
    return {
        "is_compliant":        is_compliant,
        "seal_exists":         seal_exists,
        "position_valid":      position_valid,
        "rel_x":               rel_x,
        "rel_y":               rel_y,
        "sticker_angle":       sticker_angle,   # 贴纸偏角（°），None=跳过/不可用
        "box_method":          box_method,
        "detail":              detail,
        "dual_code":           dual_code,
        "dual_detail":         dual_detail,
        "watermark_time":      watermark_time,
        "watermark_location":  watermark_location,
    }


def process_row(row, idx: int, total: int, prefetched_tasks=None) -> dict:
    """
    处理单行订单（1~4 张图片），返回合规判定结果。

    新流程：
      Phase 1  下载全部图片，对每张做 OCR + 包装盒检测
               筛选"正向完整背面照片"候选：
                 • 含"扫码即领"文字（确认为背面）
                 • 包装盒检测成功（非 fallback）
                 • 盒子面积 / 图片面积 ≥ BOX_FRONTAL_MIN_RATIO
               无候选 → 整单不合格（is_compliant=0）

      Phase 2  取盒子面积占比最大的候选图作为唯一检测对象

      Phase 3  单贴检测（短路逻辑，不合格立即返回）
               Step A  位置验证（先做）
               Step B  平铺错误检测（位置合规才执行）

      Phase 4  双贴纸检测（仅单贴全部合格后进行）
               • dual_code=2（双扫码）→ 不合格
               • dual_code=1（有授权专营店贴纸）→ 验证第二张位置
               • dual_code=0（单贴）→ 合格

      返回 dict（含 is_compliant / seal_exists / position_valid 等，见 _make_result）
    """
    print(f"\n{'='*80}")
    print(f"处理第 {idx}/{total} 行 (订单号: {row.get('订单号', 'N/A')})")
    print('=' * 80)

    watermark_time, watermark_location = "", ""
    watermark_extracted = False
    tasks = prefetched_tasks if prefetched_tasks is not None else submit_row_downloads(row)

    # ═══ Phase 1：遍历所有图片，筛选候选 ════════════════════════════════════════
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

        # 水印只取第一张
        if not watermark_extracted:
            wm_time, wm_loc = extract_watermark_crop(image, image_id)
            watermark_time, watermark_location = wm_time, wm_loc
            watermark_extracted = True
            print(f"  水印时间: {wm_time or '(未识别)'}")
            print(f"  水印地点: {wm_loc or '(未识别)'}")

        # OCR
        full_text, texts, polys_orig, orig_h, orig_w = ocr_image_full(image, image_id)
        print(f"  识别文字预览: {full_text[:120]}{'...' if len(full_text) > 120 else ''}")

        # 快速筛选：必须含"扫码即领"（背面确认）
        if not any("扫码即领" in t for t in texts):
            dealer_only, hit_text = has_dealer_only_sticker(texts)
            if dealer_only and not dealer_only_hit:
                dealer_only_hit = True
                dealer_only_text = hit_text
                print(f"  → 检测到疑似经销商自贴（关键词: {hit_text[:30]}），且无官方'扫码即领'")
            print("  → 未检测到'扫码即领'，非背面图，跳过")
            continue

        # 包装盒检测
        box_x, box_y, box_w, box_h, box_method = detect_box_bbox(image)

        # 正向完整判断
        if box_method == "fallback":
            print(f"  → 包装盒检测失败（fallback），不视为正向完整照片，跳过")
            continue
        box_ratio = (box_w * box_h) / max(orig_w * orig_h, 1)
        if box_ratio < BOX_FRONTAL_MIN_RATIO:
            print(f"  → 包装盒面积占比={box_ratio:.2f} < {BOX_FRONTAL_MIN_RATIO}，非完整照片，跳过")
            continue

        # 经销商非官方贴纸颜色检测（在盒子下半部分检测高饱和彩色区域）
        has_unoff, unoff_detail = detect_unofficial_sticker_color(
            image, box_x, box_y, box_w, box_h
        )
        if has_unoff:
            print(f"  ⚠ 颜色检测：{unoff_detail}")

        print(f"  → 候选图片 ✓  盒子占比={box_ratio:.2f}，方法={box_method}")
        candidates.append({
            "texts": texts, "polys": polys_orig,
            "orig_h": orig_h, "orig_w": orig_w,
            "box_x": box_x, "box_y": box_y, "box_w": box_w, "box_h": box_h,
            "box_method": box_method, "box_ratio": box_ratio,
            "has_unofficial": has_unoff,
            "unofficial_detail": unoff_detail,
        })

    # ═══ Phase 2：无候选 → 不合格 ════════════════════════════════════════════
    if not candidates:
        if dealer_only_hit:
            result = _make_result(
                is_compliant=0, seal_exists=0, position_valid=0,
                rel_x=None, rel_y=None, box_method=None,
                detail=f"检测到经销商自贴且无官方封口贴（关键词: {dealer_only_text[:50]}）",
                dual_code=-1, dual_detail="跳过",
                watermark_time=watermark_time, watermark_location=watermark_location,
            )
            _print_summary(result)
            return result
        result = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=None,
            detail="无正向完整包装盒背面照片（含'扫码即领'且盒子可见）",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
        )
        _print_summary(result)
        return result

    # 取盒子占比最大的图片（最完整的正向拍摄）
    best = max(candidates, key=lambda c: c["box_ratio"])
    print(f"\n  ✓ 选定候选图：盒子占比={best['box_ratio']:.2f}，方法={best['box_method']}")

    # ═══ Phase 3：单贴检测 ════════════════════════════════════════════════════

    # Step 0：非官方贴纸检测（颜色信号，优先判断）
    if best.get("has_unofficial"):
        result = _make_result(
            is_compliant=0, seal_exists=1, position_valid=4,
            rel_x=None, rel_y=None, box_method=best["box_method"],
            detail=f"检测到疑似经销商非官方贴纸：{best['unofficial_detail']}",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
        )
        _print_summary(result)
        return result

    sticker = find_sticker_from_ocr(best["texts"], best["polys"])
    if sticker is None:
        # 保底处理（正常不会走到这里）
        result = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=best["box_method"],
            detail="候选图OCR二次定位'扫码即领'失败",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
        )
        _print_summary(result)
        return result

    # Step A：位置验证（先做，不合规立即返回）
    placement = check_sticker_placement(
        sticker,
        best["box_x"], best["box_y"], best["box_w"], best["box_h"],
        best["box_method"], best["texts"], best["polys"],
    )

    _angle = placement.get("angle_deg")   # 便于后续所有分支复用

    if placement["position_valid"] != 1:
        # 位置异常(0) / 角度异常(3) / 平铺错误(2) → 不合格，跳过双贴检测
        result = _make_result(
            is_compliant=0, seal_exists=1,
            position_valid=placement["position_valid"],
            rel_x=placement["rel_x"], rel_y=placement["rel_y"],
            box_method=best["box_method"], detail=placement["detail"],
            dual_code=-1,
            dual_detail="单贴不合规，跳过双贴检测",
            watermark_time=watermark_time, watermark_location=watermark_location,
            sticker_angle=_angle,
        )
        _print_summary(result)
        return result

    # ═══ Phase 4：双贴纸检测（单贴合规后进行）════════════════════════════════
    dual = check_dual_sticker_status(best["texts"], best["polys"], best["orig_h"])

    if dual["dual_code"] == 2:
        # 两张"扫码即领"贴纸 → 不合格
        result = _make_result(
            is_compliant=0, seal_exists=1, position_valid=1,
            rel_x=placement["rel_x"], rel_y=placement["rel_y"],
            box_method=best["box_method"], detail=placement["detail"],
            dual_code=2, dual_detail=dual["dual_detail"],
            watermark_time=watermark_time, watermark_location=watermark_location,
            sticker_angle=_angle,
        )
        _print_summary(result)
        return result

    if dual["has_auth"]:
        # 存在"Apple授权专营店"关键词 → 验证其在盒子内的位置
        # （过滤盒子外部包装印刷文字导致的误触发）
        auth_pos = check_auth_sticker_position(
            best["texts"], best["polys"],
            best["box_x"], best["box_y"], best["box_w"], best["box_h"],
        )
        if not auth_pos["found"]:
            # 关键词来自盒子外部，盒子范围内无 Auth 贴纸，视为单贴合规，
            # 不从此 if 块 return，继续向下执行单贴合规逻辑
            dual = {**dual, "dual_code": 0, "dual_detail": "单贴：'Apple授权专营店'在盒子外，忽略"}
        elif not auth_pos["in_correct_position"]:
            result = _make_result(
                is_compliant=0, seal_exists=1, position_valid=1,
                rel_x=placement["rel_x"], rel_y=placement["rel_y"],
                box_method=best["box_method"], detail=placement["detail"],
                dual_code=1,
                dual_detail=f"双贴第二张位置异常：{auth_pos['detail']}",
                watermark_time=watermark_time, watermark_location=watermark_location,
                sticker_angle=_angle,
            )
            _print_summary(result)
            return result
        else:
            # 双贴且两张均合规
            result = _make_result(
                is_compliant=1, seal_exists=1, position_valid=1,
                rel_x=placement["rel_x"], rel_y=placement["rel_y"],
                box_method=best["box_method"], detail=placement["detail"],
                dual_code=1, dual_detail=f"双贴合规：{auth_pos['detail']}",
                watermark_time=watermark_time, watermark_location=watermark_location,
                sticker_angle=_angle,
            )
            _print_summary(result)
            return result

    # 单贴，位置合规，无平铺 → 合规
    result = _make_result(
        is_compliant=1, seal_exists=1, position_valid=1,
        rel_x=placement["rel_x"], rel_y=placement["rel_y"],
        box_method=best["box_method"], detail=placement["detail"],
        dual_code=dual["dual_code"], dual_detail=dual["dual_detail"],
        watermark_time=watermark_time, watermark_location=watermark_location,
        sticker_angle=_angle,
    )
    _print_summary(result)
    return result


def _print_summary(r: dict):
    print(f"\n【结果汇总】")
    print(f"  是否规范粘贴  : {'✓ 合规(1)' if r['is_compliant'] == 1 else '✗ 不合规(0)'}")
    print(f"  封口贴存在    : {r['seal_exists']}")
    print(f"  位置规范      : {r['position_valid']}")
    angle_str = f"{r['sticker_angle']:+.1f}°" if r.get('sticker_angle') is not None else "(未计算)"
    print(f"  贴纸相对X/Y   : {r['rel_x']} / {r['rel_y']}  偏角: {angle_str}")
    print(f"  包装盒检测方式: {r['box_method']}")
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
        '是否规范粘贴',     # 0=不合规 | 1=合规  ← 总体判断，放最前
        '封口贴存在',       # 0 / 1
        '贴纸位置规范',     # -1=无贴纸 | 0=位置异常 | 1=位置规范 | 2=平铺错误 | 3=角度异常 | 4=非官方贴纸
        '贴纸相对X',        # 0.0~1.0（相对包装盒宽度）
        '贴纸相对Y',        # 0.0~1.0（相对包装盒高度）
        '贴纸角度',         # 贴纸长轴与包装盒水平方向的偏角（°）；空=未计算
        '包装盒检测方式',   # edge / bright / fallback
        '位置说明',         # 文字说明（含角度/位置/平铺详情）
        '双贴纸状态',       # -1=无贴 | 0=单贴 | 1=双贴合规 | 2=双贴错误(两个扫码贴)
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
            result_row['是否规范粘贴']   = r['is_compliant']
            result_row['封口贴存在']     = r['seal_exists']
            result_row['贴纸位置规范']   = r['position_valid']
            result_row['贴纸相对X']      = r['rel_x'] if r['rel_x'] is not None else ''
            result_row['贴纸相对Y']      = r['rel_y'] if r['rel_y'] is not None else ''
            result_row['贴纸角度']       = r['sticker_angle'] if r.get('sticker_angle') is not None else ''
            result_row['包装盒检测方式'] = r['box_method'] if r['box_method'] else ''
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

    cnt_compliant  = _cnt('是否规范粘贴', 1)
    cnt_fail       = _cnt('是否规范粘贴', 0)
    cnt_seal       = _cnt('封口贴存在', 1)
    cnt_no_seal    = _cnt('封口贴存在', 0)
    cnt_pos_bad    = _cnt('贴纸位置规范', 0)
    cnt_angle_bad  = _cnt('贴纸位置规范', 3)
    cnt_flat       = _cnt('贴纸位置规范', 2)
    cnt_no_frontal = _cnt('贴纸位置规范', -1)
    cnt_unofficial = _cnt('贴纸位置规范', 4)
    cnt_dual_ok    = _cnt('双贴纸状态', 1)
    cnt_dual_err   = _cnt('双贴纸状态', 2)

    print(f"\n{'='*80}")
    print(f"总共处理               : {total_rows} 行")
    print(f"是否规范粘贴 ✓ 合规    : {cnt_compliant} 行")
    print(f"是否规范粘贴 ✗ 不合规  : {cnt_fail} 行")
    print(f"  ├─ 无正向完整照片    : {cnt_no_frontal} 行")
    print(f"  ├─ 无封口贴          : {cnt_no_seal} 行")
    print(f"  ├─ 位置异常          : {cnt_pos_bad} 行")
    print(f"  ├─ 角度异常          : {cnt_angle_bad} 行  (偏角 > {STICKER_ANGLE_MAX_DEG:.0f}°)")
    print(f"  ├─ 平铺错误          : {cnt_flat} 行  (端片未绕侧面)")
    print(f"  ├─ 非官方贴纸        : {cnt_unofficial} 行  (经销商彩色自贴)")
    print(f"  └─ 双贴纸错误        : {cnt_dual_err} 行  (两个扫码贴)")
    print(f"双贴纸合规             : {cnt_dual_ok} 行  (扫码+授权专营)")
    print(f"总耗时                 : {(time.time() - start_time) / 3600:.2f} 小时")
    print(f"{'='*80}\n")


if __name__ == '__main__':
    main()
