#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量OCR识别 - CPU并行版
特性：
1. 纯CPU推理，完全不占GPU
2. 多进程并行（每进程独立PaddleOCR实例，充分利用多核）
3. 轻量化模型配置（禁用文档矫正/展平）
4. 实时保存CSV + JSON，支持断点续传
5. 封口贴粘贴错误检测 + 水印时间/地点提取

CPU并行架构：
  Pool(N进程) + imap_unordered → 每个进程独立下载+OCR一行
  主进程统一收结果并保存
"""

import re
import os
import time
import json
import traceback
import multiprocessing as mp
import pandas as pd
import requests
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

# ─── 可调参数 ─────────────────────────────────────────────────────────────────
IMAGE_COLUMNS    = ['图片地址', 'Unnamed: 17', 'Unnamed: 18', 'Unnamed: 19']
OCR_WORKERS      = max(2, min(8, (os.cpu_count() or 4) // 2))   # OCR并行进程数
DOWNLOAD_WORKERS = 6    # 每行内部图片下载并发线程数（在worker进程内）
MAX_SIDE         = 960  # 输入图片最大边长（越小越快，字符准确率略降）
# ─────────────────────────────────────────────────────────────────────────────

# 跳过启动时的网络连接检查（加快初始化）
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'

print(f"CPU版本初始化，将使用 {OCR_WORKERS} 个OCR并行进程（共{os.cpu_count()}核）")


# ════════════════════════════════════════════════════════════════════════════
#  Worker 进程初始化（每个进程只执行一次）
# ════════════════════════════════════════════════════════════════════════════

_worker_ocr = None

def _worker_init():
    """每个Worker进程独立初始化一个OCR实例（主OCR兼用水印提取）"""
    global _worker_ocr
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    from paddleocr import PaddleOCR
    _worker_ocr = PaddleOCR(
        use_textline_orientation=True,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        lang='ch',
        device='cpu',
        enable_mkldnn=True,
        text_det_limit_side_len=MAX_SIDE,
    )


# ════════════════════════════════════════════════════════════════════════════
#  工具函数（在worker进程中使用）
# ════════════════════════════════════════════════════════════════════════════

def _resize_for_ocr(image):
    """缩放图片到最大边长 MAX_SIDE，降低CPU推理耗时"""
    if image is None:
        return None
    w, h = image.size
    max_curr = max(w, h)
    if max_curr <= MAX_SIDE:
        return image
    scale = MAX_SIDE / float(max_curr)
    return image.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                        resample=Image.BICUBIC)


def _download_one(url, timeout=15):
    """下载单张图片 → PIL.Image"""
    try:
        if not url or (hasattr(url, '__class__') and url != url):  # nan check
            return None
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return Image.open(BytesIO(resp.content)).convert('RGB')
    except Exception:
        pass
    return None


def _run_ocr(image, image_id):
    """
    对一张图片做OCR，返回 (full_text, texts, polys, ocr_h, ocr_w)
    """
    if image is None:
        return "", [], [], 0, 0
    try:
        img = _resize_for_ocr(image)
        temp_path = f"/tmp/cpuocr_{os.getpid()}_{image_id}_{int(time.time()*1000)}.jpg"
        img.save(temp_path, 'JPEG')
        result = _worker_ocr.predict(input=temp_path)

        texts, polys = [], []
        if result and len(result) > 0:
            r = result[0]
            if hasattr(r, 'json'):
                res   = r.json.get('res', {})
                texts = res.get('rec_texts', [])
                polys = res.get('dt_polys', res.get('boxes', []))

        if os.path.exists(temp_path):
            os.remove(temp_path)

        return " ".join(texts), texts, polys, img.size[1], img.size[0]
    except Exception:
        if 'temp_path' in dir() and os.path.exists(temp_path):
            os.remove(temp_path)
        return "", [], [], 0, 0


def _check_official_seal(text):
    for kw in ["扫码即领", "Apple授权专营店"]:
        if kw in text:
            return True, kw
    return False, None


def _check_seal_error(text):
    return 1 if ("扫码即领" in text and "授权经销商" in text) else 0


def _parse_watermark_text(segs):
    """解析水印文字段，返回 (时间字符串, 地点字符串)"""
    t_pat  = re.compile(r'\d{1,2}:\d{2}')
    d_pat  = re.compile(r'\d{4}[-–\-]\d{2}[-–\-]\d{2}')
    wk_pat = re.compile(r'星期[一二三四五六日]')
    sep_pt = re.compile(r'^[|｜\s]+$')

    t_parts, l_parts = [], []
    for s in segs:
        s = s.strip()
        if not s or sep_pt.match(s):
            continue
        if t_pat.search(s) or d_pat.search(s) or wk_pat.search(s):
            t_parts.append(re.sub(r'[|｜]', ' ', s).strip())
        else:
            l_parts.append(s)

    t_str = re.sub(r'\s+', ' ', " ".join(t_parts)).strip()

    # 地点：只保留中文字符占比 >= 40% 的段落，过滤英文乱码噪声。
    # 不再截断到首个"市"——会错误丢掉"陕西省西安市…"中的省份前缀。
    clean_loc = [
        p for p in l_parts
        if p and (sum(1 for c in p if '\u4e00' <= c <= '\u9fff')
                  / max(len(p.replace(' ', '')), 1)) >= 0.4
    ]
    l_str = re.sub(r'\s+', ' ', " ".join(clean_loc)).strip()
    return t_str, l_str


def _extract_watermark_crop(image, image_id):
    """
    水印提取（裁剪版）：底部 18% × 左侧 60% 单独裁剪后 OCR。
    相比全图坐标过滤，可隔离盒子印刷文字在 y≈90% 处与水印重叠的干扰，
    日期识别更稳定。
    """
    if image is None:
        return "", ""
    try:
        w, h = image.size
        crop = image.crop((0, int(h * 0.82), int(w * 0.60), h))
        temp_path = f"/tmp/wm_crop_{image_id}_{os.getpid()}_{int(time.time()*1000)}.jpg"
        crop.save(temp_path, 'JPEG')
        result = _worker_ocr.predict(input=temp_path)
        texts = []
        if result and len(result) > 0:
            r = result[0]
            if hasattr(r, 'json'):
                texts = r.json.get('res', {}).get('rec_texts', [])
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return _parse_watermark_text(texts)
    except Exception:
        if 'temp_path' in dir() and os.path.exists(temp_path):
            os.remove(temp_path)
        return "", ""


# ════════════════════════════════════════════════════════════════════════════
#  Worker 主函数（每行的完整处理逻辑）
# ════════════════════════════════════════════════════════════════════════════

def _process_row_task(task):
    """
    在worker进程中处理一行数据：下载图片 → OCR → 封口贴检测 → 水印提取

    task = {
        'row_dict'  : dict,   # 原始行数据
        'idx'       : int,    # 行号（1-based，用于日志）
        'total'     : int,
    }
    返回 (row_dict_with_results, log_str)
    """
    row   = task['row_dict']
    idx   = task['idx']
    total = task['total']
    logs  = [f"Row {idx}/{total} (订单号: {row.get('订单号','N/A')})"]

    # 1. 并行下载所有图片
    urls = []
    for col in IMAGE_COLUMNS:
        v = row.get(col)
        if v and str(v) != 'nan' and str(v).strip():
            urls.append((col, str(v).strip()))

    def dl(cu):
        col, url = cu
        return col, url, _download_one(url)

    with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(urls) or 1)) as ex:
        downloaded = list(ex.map(dl, urls))

    # 按原始列顺序排列
    col_order = {c: i for i, c in enumerate(IMAGE_COLUMNS)}
    downloaded.sort(key=lambda x: col_order.get(x[0], 99))

    # 2. 逐张OCR，发现封口贴即停
    has_seal   = False
    found_kw   = None
    seal_err   = 0
    wm_time    = ""
    wm_loc     = ""
    wm_done    = False

    for col_idx, (col, url, image) in enumerate(downloaded, 1):
        if image is None:
            logs.append(f"  col{col_idx}: 下载失败，跳过")
            continue

        full_text, texts, polys, ocr_h, ocr_w = _run_ocr(image, f"{idx}_{col_idx}")

        if not wm_done:
            wm_time, wm_loc = _extract_watermark_crop(image, f"{idx}_{col_idx}")
            wm_done = True

        logs.append(f"  col{col_idx}: {full_text[:80]}")

        found, kw = _check_official_seal(full_text)
        if found:
            has_seal  = True
            found_kw  = kw
            seal_err  = _check_seal_error(full_text)
            logs.append(f"  ✓ 找到封口贴关键词: {kw} | 粘贴错误: {seal_err}")
            break

    # 3. 组装结果
    result = 1 if has_seal else 0
    row['是否存在官方封口贴'] = result
    row['找到的关键词']      = found_kw or ''
    row['封口贴粘贴错误']    = seal_err
    row['时间']              = wm_time
    row['地点']              = wm_loc

    logs.append(f"  封口贴={result}, 时间={wm_time or '未识别'}, 地点={wm_loc[:40] or '未识别'}")
    return row, "\n".join(logs)


# ════════════════════════════════════════════════════════════════════════════
#  保存 & 主流程
# ════════════════════════════════════════════════════════════════════════════

def save_result(result_dict, csv_file, json_file):
    df_row = pd.DataFrame([result_dict])
    if not os.path.exists(csv_file):
        df_row.to_csv(csv_file, index=False, mode='w', encoding='utf-8-sig')
    else:
        df_row.to_csv(csv_file, index=False, mode='a', header=False, encoding='utf-8-sig')
    with open(json_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result_dict, ensure_ascii=False) + '\n')


def main():
    input_file   = '/home/ubuntu/OCR/出库照片3.15-3.17.xlsx'
    output_csv   = '/home/ubuntu/OCR/出库照片3.15-3.17_cpu_results.csv'
    output_json  = '/home/ubuntu/OCR/出库照片3.15-3.17_cpu_results.jsonl'
    output_excel = '/home/ubuntu/OCR/出库照片3.15-3.17_cpu_processed.xlsx'

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

    total_rows = len(df)

    # 构建待处理任务列表
    tasks = []
    for idx, row in df.iterrows():
        if str(row.get('订单号', '')) in processed_orders:
            continue
        tasks.append({
            'row_dict': row.to_dict(),
            'idx'     : idx + 1,
            'total'   : total_rows,
        })

    print(f"待处理: {len(tasks)} 行  |  OCR并行进程: {OCR_WORKERS}\n{'='*80}")
    start_time  = time.time()
    done_count  = 0
    error_count = 0

    # ── 多进程Pool处理 ────────────────────────────────────────────────────────
    with mp.Pool(processes=OCR_WORKERS, initializer=_worker_init) as pool:
        for row_result, log_str in pool.imap_unordered(
                _process_row_task, tasks, chunksize=1):
            print(log_str)

            save_result(row_result, output_csv, output_json)
            done_count += 1
            print(f"  ✓ 已保存  [{done_count}/{len(tasks)}]")

            # 每50行进度统计
            if done_count % 50 == 0:
                elapsed  = time.time() - start_time
                avg      = elapsed / done_count
                remain   = (len(tasks) - done_count) * avg
                print(f"\n{'='*80}")
                print(f"进度: {done_count}/{len(tasks)}  avg={avg:.1f}s/行  "
                      f"剩余≈{remain/3600:.1f}h")
                print(f"{'='*80}\n")

    # ── 转换为Excel ───────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("所有行处理完成！正在生成最终Excel文件...")
    df_final = pd.read_csv(output_csv, encoding='utf-8-sig')
    df_final.to_excel(output_excel, index=False)
    print(f"Excel文件已保存: {output_excel}")

    col_seal    = '是否存在官方封口贴'
    col_err     = '封口贴粘贴错误'
    has_seal    = (df_final[col_seal] == 1).sum() if col_seal in df_final.columns else 0
    no_seal     = (df_final[col_seal] == 0).sum() if col_seal in df_final.columns else 0
    seal_errors = (df_final[col_err]  == 1).sum() if col_err  in df_final.columns else 0

    print(f"\n处理完成！")
    print(f"总行数: {total_rows} | 包含封口贴: {has_seal} | 无封口贴: {no_seal} | "
          f"粘贴错误: {seal_errors} | OCR异常: {error_count}")
    print(f"总耗时: {(time.time()-start_time)/3600:.2f} 小时")
    print(f"{'='*80}\n")


if __name__ == '__main__':
    # multiprocessing 在 Linux 下默认 fork，但为安全起见显式设置
    mp.set_start_method('fork', force=True)
    main()
