#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量OCR识别 - 优化版
特性：
1. 实时保存到CSV（速度快100倍）
2. 每行立即保存，不丢失数据
3. 同时保存JSON备份
4. 支持断点续传
5. 最后转换为Excel格式
6. 封口贴粘贴错误检测（同时含"扫码即领"+"授权经销商"）
7. 提取图片左下角水印中的时间和地点

性能优化：
- 同行多张图片并行下载（ThreadPoolExecutor）
- 流水线预下载：OCR当前行时同步下载后续行
- 单次全图OCR同时完成封口贴检测+水印坐标提取（不再单独裁剪OCR）
"""
import re
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
IMAGE_COLUMNS   = ['图片地址', 'Unnamed: 17', 'Unnamed: 18', 'Unnamed: 19']
DOWNLOAD_WORKERS = 8    # 并行下载线程数（网络I/O密集，可以多开）
PREFETCH_ROWS    = 3    # 向前预下载的行数（隐藏下载延迟）
# ─────────────────────────────────────────────────────────────────────────────

print("=" * 80)
print("正在初始化PaddleOCR (GPU加速，显存友好)...")
print("=" * 80)
# 主 OCR：显存友好，限制检测端输入尺寸
ocr = PaddleOCR(
    use_textline_orientation=True,
    lang='ch',
    device='gpu',
    enable_mkldnn=False,
    text_det_limit_side_len=2000,
)
# 水印裁剪图直接复用主 OCR 实例（裁剪后图片本身已很小，推理快，无需独立实例）
print("PaddleOCR初始化完成！\n")


def resize_for_ocr(image, max_side=2000):
    """将图片按最大边长缩放，显著降低 GPU 推理显存占用。"""
    if image is None:
        return None
    w, h = image.size
    max_curr = max(w, h)
    if max_curr <= max_side:
        return image
    scale = max_side / float(max_curr)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    return image.resize((new_w, new_h), resample=Image.BICUBIC)

# 全局下载线程池（整个程序生命周期共享）
_dl_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)


# ─── 下载 ─────────────────────────────────────────────────────────────────────

def download_image(url, timeout=15):
    """下载单张图片"""
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


def submit_row_downloads(row) -> list[Future]:
    """
    向全局线程池提交一行所有图片的下载任务。
    返回 [(col_idx, col, url, Future[Image|None]), ...]，保持列顺序。
    """
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


# ─── OCR ──────────────────────────────────────────────────────────────────────

def ocr_image_full(image, image_id="unknown"):
    """
    对完整图片做一次OCR，返回：
      full_text  : 所有文字拼接字符串（用于封口贴检测）
      texts      : 文字段列表
      polys      : 对应的多边形坐标列表 [[[x,y]×4], ...]（用于水印定位）
    """
    if image is None:
        return "", [], []

    try:
        # 为降低显存占用，先缩放输入
        image_resized = resize_for_ocr(image, max_side=2000)
        if image_resized is None:
            return "", [], [], 0

        temp_path = f"/tmp/temp_ocr_{image_id}_{int(time.time()*1000)}.jpg"
        image_resized.save(temp_path, 'JPEG')
        result = ocr.predict(input=temp_path)

        texts, polys = [], []
        if result and len(result) > 0:
            ocr_result = result[0]
            if hasattr(ocr_result, 'json'):
                res    = ocr_result.json.get('res', {})
                texts  = res.get('rec_texts', [])
                polys  = res.get('dt_polys',  res.get('boxes', []))

        if os.path.exists(temp_path):
            os.remove(temp_path)

        return " ".join(texts), texts, polys, image_resized.size[1], image_resized.size[0]

    except Exception as e:
        # PaddleOCR 底层错误有时 str(e 为空)，因此这里打印完整 traceback 便于定位根因
        print("  OCR识别异常类型:", type(e).__name__)
        print("  OCR识别异常repr:", repr(e))
        print("  OCR识别异常traceback:\n", traceback.format_exc())
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
        return "", [], [], 0, 0


# ─── 封口贴检测 ───────────────────────────────────────────────────────────────

def check_official_seal(text):
    """检查是否含官方封口贴关键词"""
    for kw in ["扫码即领", "Apple授权专营店"]:
        if kw in text:
            return True, kw
    return False, None


def check_seal_error(text):
    """同一张图同时含'扫码即领'和'授权经销商'→封口贴粘贴错误，返回1；否则0"""
    return 1 if ("扫码即领" in text and "授权经销商" in text) else 0


# ─── 水印提取 ─────────────────────────────────────────────────────────────────

def parse_watermark_text(text_segments):
    """
    水印固定格式：
      第一行：HH:MM  |  YYYY-MM-DD  星期X   →  「时间」列
      第二行：地址文字                        →  「地点」列
    """
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

    # 地点：只保留中文字符占比 >= 40% 的段落，过滤英文乱码噪声。
    # 不再截断到首个"市"——会错误丢掉"陕西省西安市…"中的省份前缀。
    clean_loc = [
        p for p in location_parts
        if p and (sum(1 for c in p if chr(0x4e00) <= c <= chr(0x9fff))
                  / max(len(p.replace(" ", "")), 1)) >= 0.4
    ]
    location_str = re.sub(r"\s+", " ", " ".join(clean_loc)).strip()
    return time_str, location_str


def extract_watermark_crop(image, image_id):
    """
    水印提取（裁剪版）：
    将底部 18% × 左侧 60% 区域单独裁剪后，用轻量水印专用 OCR 识别。
    - 裁剪区域极小（~576×173px），用 ocr_wm（禁用方向识别、det输入仅480px），
      单次耗时约为全图 OCR 的 10~15%，额外计算量可忽略。
    """
    if image is None:
        return "", ""
    try:
        w, h = image.size
        crop = image.crop((0, int(h * 0.82), int(w * 0.60), h))
        temp_path = f"/tmp/wm_crop_{image_id}_{int(time.time()*1000)}.jpg"
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


# ─── 保存 ─────────────────────────────────────────────────────────────────────

def save_result_immediately(result_dict, csv_file, json_file):
    df_row = pd.DataFrame([result_dict])
    if not os.path.exists(csv_file):
        df_row.to_csv(csv_file, index=False, mode='w', encoding='utf-8-sig')
    else:
        df_row.to_csv(csv_file, index=False, mode='a', header=False, encoding='utf-8-sig')

    with open(json_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result_dict, ensure_ascii=False) + '\n')


# ─── 行处理 ───────────────────────────────────────────────────────────────────

def process_row(row, idx, total, prefetched_tasks=None):
    """
    处理单行数据。
    prefetched_tasks: submit_row_downloads() 返回的 Future 列表；
                      若为 None 则退化为串行下载（兼容）。
    返回 (封口贴结果, 关键词, 封口贴错误, 水印时间, 水印地点)
    """
    print(f"\n{'='*80}")
    print(f"处理第 {idx}/{total} 行 (订单号: {row.get('订单号', 'N/A')})")
    print('='*80)

    has_official_seal   = False
    found_keyword       = None
    seal_error          = 0
    processed_images    = 0
    watermark_time      = ""
    watermark_location  = ""
    watermark_extracted = False

    # 若没有预下载，退化为临时提交（串行效果）
    tasks = prefetched_tasks if prefetched_tasks is not None else submit_row_downloads(row)

    for col_idx, col, url, future in tasks:
        print(f"\n  第{col_idx}张图片: {url[:80]}...")

        image = future.result()   # 等待下载完成（大概率已经完成）
        if image is None:
            continue

        print(f"  图片尺寸: {image.size}")
        processed_images += 1

        image_id = f"row{idx}_col{col_idx}"
        full_text, texts, polys, ocr_height, ocr_width = ocr_image_full(image, image_id)

        # 第一张图单独裁剪水印区域精准识别（避免全图坐标方式受盒子文字干扰）
        if not watermark_extracted:
            watermark_time, watermark_location = extract_watermark_crop(image, image_id)
            watermark_extracted = True
            print(f"  水印时间: {watermark_time or '(未识别)'}")
            print(f"  水印地点: {watermark_location or '(未识别)'}")

        print(f"  识别文字预览: {full_text[:150]}{'...' if len(full_text) > 150 else ''}")

        found, keyword = check_official_seal(full_text)
        if found:
            print(f"  ✓✓✓ 找到官方封口贴！关键词: '{keyword}'")
            has_official_seal = True
            found_keyword     = keyword
            seal_error        = check_seal_error(full_text)
            if seal_error:
                print(f"  ⚠️  封口贴粘贴错误！同时存在'扫码即领'和'授权经销商'")
            break

    result = 1 if has_official_seal else 0
    print(f"\n【结果】是否存在官方封口贴: {'是 (1)' if result == 1 else '否 (0)'}")
    if found_keyword:
        print(f"【关键词】{found_keyword}")
    print(f"【封口贴粘贴错误】{seal_error}")
    print(f"【水印时间】{watermark_time or '(未识别)'}")
    print(f"【水印地点】{watermark_location or '(未识别)'}")
    print(f"【处理图片数】{processed_images}")

    return result, found_keyword, seal_error, watermark_time, watermark_location


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    input_file   = '/home/ubuntu/OCR/出库照片3.18.xlsx'
    output_csv   = '/home/ubuntu/OCR/出库照片3.18_results.csv'
    output_json  = '/home/ubuntu/OCR/出库照片3.18_results.jsonl'
    output_excel = '/home/ubuntu/OCR/出库照片3.18_processed.xlsx'

    print(f"正在读取Excel文件: {input_file}")
    df = pd.read_excel(input_file)
    print(f"总共有 {len(df)} 行数据")

    # 断点续传
    if os.path.exists(output_csv):
        print(f"\n发现已存在的结果文件: {output_csv}")
        df_existing      = pd.read_csv(output_csv, encoding='utf-8-sig')
        processed_orders = set(df_existing['订单号'].astype(str))
        print(f"已处理 {len(processed_orders)} 行，继续处理剩余行...")
    else:
        processed_orders = set()
        pd.DataFrame(columns=list(df.columns) + [
            '是否存在官方封口贴', '找到的关键词', '封口贴粘贴错误', '时间', '地点'
        ]).to_csv(output_csv, index=False, mode='w', encoding='utf-8-sig')

    # 过滤出待处理行，建立索引便于预取
    pending = [
        (idx, row)
        for idx, row in df.iterrows()
        if str(row.get('订单号', '')) not in processed_orders
    ]

    total_rows    = len(df)
    start_time    = time.time()

    # ── 流水线预下载 ─────────────────────────────────────────────────────────
    # prefetch_cache[idx] = [(col_idx, col, url, Future), ...]
    prefetch_cache: dict[int, list] = {}

    def ensure_prefetched(target_pending_idx):
        """确保 pending[target_pending_idx] 及其后 PREFETCH_ROWS 行已提交下载"""
        end = min(target_pending_idx + PREFETCH_ROWS + 1, len(pending))
        for pi in range(target_pending_idx, end):
            pidx, prow = pending[pi]
            if pidx not in prefetch_cache:
                prefetch_cache[pidx] = submit_row_downloads(prow)

    # 预提交开头几行
    ensure_prefetched(0)
    # ─────────────────────────────────────────────────────────────────────────

    for pi, (idx, row) in enumerate(pending):
        order_id = str(row.get('订单号', ''))

        # 提前提交后续行的下载任务
        ensure_prefetched(pi + 1)

        tasks = prefetch_cache.pop(idx, None)

        try:
            result, keyword, seal_error, wm_time, wm_location = process_row(
                row, idx + 1, total_rows, prefetched_tasks=tasks
            )

            result_row = row.to_dict()
            result_row['是否存在官方封口贴'] = result
            result_row['找到的关键词']      = keyword if keyword else ''
            result_row['封口贴粘贴错误']    = seal_error
            result_row['时间']              = wm_time
            result_row['地点']              = wm_location

            save_result_immediately(result_row, output_csv, output_json)
            print(f"  ✓ 已保存到CSV和JSON")

        except Exception as e:
            print(f"\n处理第 {idx + 1} 行时出错: {str(e)}")
            print(f"详细错误: {traceback.format_exc()[:300]}")

            result_row = row.to_dict()
            result_row['是否存在官方封口贴'] = -1
            result_row['找到的关键词']      = 'ERROR'
            result_row['封口贴粘贴错误']    = -1
            result_row['时间']              = ''
            result_row['地点']              = ''
            save_result_immediately(result_row, output_csv, output_json)

        # 每50行进度统计
        if (pi + 1) % 50 == 0:
            elapsed   = time.time() - start_time
            avg_time  = elapsed / (pi + 1)
            remaining = (len(pending) - pi - 1) * avg_time
            print(f"\n{'='*80}")
            print(f"进度统计")
            print(f"已处理: {pi + 1}/{len(pending)} 待处理行  "
                  f"({(idx + 1)/total_rows*100:.1f}% of 全量)")
            print(f"平均每行耗时: {avg_time:.2f} 秒")
            print(f"预计剩余时间: {remaining/3600:.1f} 小时")
            print(f"{'='*80}\n")

    _dl_executor.shutdown(wait=False)

    # 转换为Excel
    print(f"\n{'='*80}")
    print("所有行处理完成！正在生成最终Excel文件...")
    df_final = pd.read_csv(output_csv, encoding='utf-8-sig')
    df_final.to_excel(output_excel, index=False)
    print(f"Excel文件已保存: {output_excel}")

    count_has_seal = (df_final['是否存在官方封口贴'] == 1).sum()
    count_no_seal  = (df_final['是否存在官方封口贴'] == 0).sum()
    count_error    = (df_final['是否存在官方封口贴'] == -1).sum()
    count_seal_err = (df_final['封口贴粘贴错误'] == 1).sum() \
                     if '封口贴粘贴错误' in df_final.columns else 0

    print(f"\n处理完成！")
    print(f"总共处理: {total_rows} 行")
    print(f"包含官方封口贴: {count_has_seal} 行")
    print(f"不包含官方封口贴: {count_no_seal} 行")
    print(f"封口贴粘贴错误: {count_seal_err} 行")
    print(f"处理出错: {count_error} 行")
    print(f"总耗时: {(time.time() - start_time)/3600:.1f} 小时")
    print(f"{'='*80}\n")


if __name__ == '__main__':
    main()
