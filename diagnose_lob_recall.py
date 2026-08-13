#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在"实际漏检的行"上量测各条兜底路径能救回多少，改检测逻辑前先拿数据。

背景：Mac 的无贴率 35-36%，且 100% 的失败都是"未找到含'扫码即领'的背面图"
（锚点根本没读到），不是位置判定判出去的。但 Mac 目前只有一次机会——单张 0°
整图 OCR；Watch/AirPods/配件才有紫贴局部 OCR 和旋转兜底。本脚本在真实漏检行
上对比几条路径的召回，避免盲目照搬参数换来一堆误报。

变体：
  A 现状        0° 整图 OCR（长边缩到 OCR_MAX_SIDE=3000）
  B 紫贴crop    A + 0° 紫色封贴局部 OCR（不放大，即现有 Watch/AirPods 做法）
  C 紫贴crop放大 A + 局部 crop 放大到长边 --upscale 再 OCR
  D 整图高分辨率 0° 整图但长边上限提到 --hires（测"拍太远被缩小"是否为主因）
                 需要重建 OCR 引擎，故单独一轮；用 --no-hires 跳过

用法（在项目目录、GPU 空闲时跑）：
    python diagnose_lob_recall.py 出库照片处理后_0809_results.csv
    python diagnose_lob_recall.py 出库照片处理后_0809_results.csv --lob Mac --sample 20
    python diagnose_lob_recall.py ... --save-images ./mac_fail_samples   # 存图人工看

只读，不写任何结果文件，不影响既有输出。
"""

import argparse
import os
import sys
import time

import pandas as pd
from PIL import Image

import ocr_batch_process_v7 as V7


def upscale(img: Image.Image, target_long_side: int) -> Image.Image:
    """把小图放大到指定长边（只放大，不缩小）。

    这正是现有紫贴路径缺的一步：注释里叫"局部放大 OCR"，但 resize_for_ocr
    只会缩小，小 crop 原样进 OCR，字还是那么小。
    """
    w, h = img.size
    if max(w, h) >= target_long_side:
        return img
    s = target_long_side / max(w, h)
    return img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)


def full_ocr_hit(img: Image.Image, image_id: str) -> bool:
    _, texts, _, _, _ = V7.ocr_image_full(img, image_id)
    return V7.has_scan_text(texts)


def crop_scan_hit(img: Image.Image, image_id: str,
                  upscale_to: int | None) -> tuple[bool, int]:
    """返回 (是否命中, 紫色候选个数)。候选数为 0 说明紫贴检测本身没找到东西。"""
    boxes = V7.find_purple_scan_candidate_boxes(
        img, max_candidates=V7.SCAN_LOCAL_CROP_MAX_CANDIDATES)
    for ci, (x1, y1, x2, y2) in enumerate(boxes, 1):
        crop = img.crop((x1, y1, x2, y2))
        if upscale_to:
            crop = upscale(crop, upscale_to)
        _, texts, _, _, _ = V7.ocr_image_full(crop, f"{image_id}_p{ci}")
        if V7.has_scan_text(texts):
            return True, len(boxes)
    return False, len(boxes)


def collect_row_images(row, cols, save_dir, tag) -> list[tuple[str, Image.Image]]:
    out = []
    for ci, col in enumerate(cols, 1):
        url = row.get(col)
        if pd.isna(url) or str(url).strip() == '':
            continue
        got = V7.download_image(str(url))
        if not got.get('ok'):
            continue
        img = got['image']
        out.append((f"col{ci}", img))
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
            img.save(os.path.join(save_dir, f"{tag}_col{ci}.jpg"), quality=88)
    return out


def main():
    ap = argparse.ArgumentParser(
        description='在真实漏检行上量测各兜底路径的召回增益（只读，不改结果）')
    ap.add_argument('results_csv', help='已完成批次的结果 CSV')
    ap.add_argument('--lob', default='Mac', help='要诊断的识别LOB（缺省 Mac）')
    ap.add_argument('--sample', type=int, default=20, help='抽样行数（缺省 20）')
    ap.add_argument('--upscale', type=int, default=2000,
                    help='变体C 把局部 crop 放大到的长边（缺省 2000）')
    ap.add_argument('--hires', type=int, default=4000,
                    help='变体D 的整图长边上限（缺省 4000）')
    ap.add_argument('--no-hires', action='store_true', help='跳过变体D（省一次引擎重建）')
    ap.add_argument('--gpu-mem-limit-mb', type=int, default=0,
                    help='单进程诊断，缺省 0=不限制')
    ap.add_argument('--save-images', default=None, help='把漏检行的图片存到该目录供人工查看')
    args = ap.parse_args()

    df = pd.read_csv(args.results_csv, encoding='utf-8-sig', dtype=str)
    if '识别LOB' not in df.columns:
        sys.exit('结果 CSV 缺少「识别LOB」列')

    sub = df[(df['识别LOB'] == args.lob)
             & (pd.to_numeric(df['封口贴存在'], errors='coerce') == 0)]
    if sub.empty:
        sys.exit(f'{args.lob} 没有 封口贴存在=0 的行，无需诊断')

    n = min(args.sample, len(sub))
    # 均匀取样而非取前 N 行，避免全落在同一门店/同一天
    step = max(1, len(sub) // n)
    sample = sub.iloc[::step].head(n)
    cols = [c for c in V7.IMAGE_COLUMNS if c in df.columns]

    print('=' * 76)
    print(f"诊断 LOB: {args.lob}")
    print(f"该 LOB 漏检行(封口贴存在=0): {len(sub)} / {int((df['识别LOB'] == args.lob).sum())}")
    print(f"本次抽样: {len(sample)} 行；图片列 {len(cols)} 个")
    print(f"变体C 放大到长边 {args.upscale}；变体D 整图上限 {args.hires}"
          + ("（已跳过）" if args.no_hires else ""))
    print('=' * 76)

    print('\n初始化 PaddleOCR …')
    V7.init_worker_ocr(gpu_mem_limit_mb=args.gpu_mem_limit_mb)
    print('就绪，开始逐行测试（每行会重新下载图片）\n')

    hit_a = hit_b = hit_c = 0
    rows_with_purple = 0
    long_sides, n_images = [], 0
    cached = []          # (tag, [(cid, img)]) 供变体D 复用，避免重复下载
    # 每张图各阶段耗时（秒），用于回答"开了这个会慢多少"
    secs_a, secs_b, secs_c = [], [], []

    for i, (_, row) in enumerate(sample.iterrows(), 1):
        oid = str(row.get('订单号', ''))[:20]
        tag = f"{args.lob}_{oid}"
        imgs = collect_row_images(row, cols, args.save_images, tag)
        if not imgs:
            print(f"[{i}/{len(sample)}] {oid} → 图片全部下载失败，跳过")
            continue
        cached.append((tag, oid, imgs))
        n_images += len(imgs)
        long_sides += [max(im.size) for _, im in imgs]

        a = b = c = False
        purple_here = 0
        for cid, img in imgs:
            iid = f"{tag}_{cid}"
            t0 = time.perf_counter()
            if not a and full_ocr_hit(img, iid):
                a = True
            t1 = time.perf_counter()
            hb, nb = crop_scan_hit(img, iid, None)
            t2 = time.perf_counter()
            purple_here += nb
            if hb:
                b = True
            hc, _ = crop_scan_hit(img, iid, args.upscale)
            t3 = time.perf_counter()
            if hc:
                c = True
            secs_a.append(t1 - t0)
            secs_b.append(t2 - t1)
            secs_c.append(t3 - t2)
            if a and b and c:
                break

        hit_a += a
        hit_b += (a or b)
        hit_c += (a or b or c)
        if purple_here:
            rows_with_purple += 1
        print(f"[{i}/{len(sample)}] {oid}  图{len(imgs)}张 "
              f"长边{max(max(im.size) for _, im in imgs)}  "
              f"紫候选{purple_here}  "
              f"A={'✓' if a else '✗'} B={'✓' if b else '✗'} C={'✓' if c else '✗'}")

    hit_d = None
    if not args.no_hires and cached:
        print(f"\n重建 OCR 引擎（整图长边上限 {args.hires}）测变体D …")
        V7.OCR_MAX_SIDE = args.hires
        V7.OCR_DET_LIMIT_SIDE_LEN = args.hires
        V7.init_worker_ocr(gpu_mem_limit_mb=args.gpu_mem_limit_mb)
        hit_d = 0
        for i, (tag, oid, imgs) in enumerate(cached, 1):
            d = False
            for cid, img in imgs:
                try:
                    if full_ocr_hit(img, f"{tag}_{cid}_hires"):
                        d = True
                        break
                except Exception as e:
                    print(f"    ⚠ {oid} 高分辨率 OCR 失败（多为显存不足）: {str(e)[:60]}")
                    break
            hit_d += d
            print(f"[D {i}/{len(cached)}] {oid}  D={'✓' if d else '✗'}")

    tested = len(cached)
    if not tested:
        sys.exit('\n没有任何行成功下载到图片，无法得出结论。')

    def line(name, hits, note=''):
        print(f"  {name:<34} {hits:>3}/{tested}  ({hits / tested * 100:5.1f}%) {note}")

    print('\n' + '=' * 76)
    print(f"样本: {tested} 行 / {n_images} 张图")
    if long_sides:
        s = sorted(long_sides)
        print(f"图片长边: 中位数 {s[len(s) // 2]}, 最小 {s[0]}, 最大 {s[-1]}")
    print(f"紫色封贴候选: {rows_with_purple}/{tested} 行至少检出一个候选")
    print("\n各变体在这批漏检行上的召回（找到'扫码即'锚点即算救回）:")
    line('A 现状(0°整图,≤3000)', hit_a, '← 应为 0，否则抽样有问题')
    line('B A+紫贴crop(不放大)', hit_b, '← 只把 Mac 加进名单能拿到的')
    line(f'C A+紫贴crop放大到{args.upscale}', hit_c, '← 再补上真正的放大')
    if hit_d is not None:
        line(f'D 0°整图长边≤{args.hires}', hit_d, '← 缩放是否为主因')
    print('=' * 76)

    # ── 开销：只有"0° 未命中的图"才会付紫贴的钱，命中即停 ──────────────────
    if secs_a:
        avg_a = sum(secs_a) / len(secs_a)
        avg_b = sum(secs_b) / len(secs_b)
        avg_c = sum(secs_c) / len(secs_c)
        print(f"\n单张图平均耗时（{len(secs_a)} 张，均为 0° 未命中的图——"
              f"命中的图不会走紫贴路径，不付这个钱）:")
        print(f"  0° 整图 OCR（本来就有）      {avg_a * 1000:7.0f} ms")
        print(f"  + 紫贴crop 不放大            {avg_b * 1000:7.0f} ms  "
              f"(×{avg_b / avg_a:.2f} 于整图)")
        print(f"  + 紫贴crop 放大到{args.upscale:<5}       {avg_c * 1000:7.0f} ms  "
              f"(×{avg_c / avg_a:.2f} 于整图)")
        print(f"\n  → 生产开启后，一张 0° 未命中的图从 {avg_a * 1000:.0f}ms 变为 "
              f"{(avg_a + avg_c) * 1000:.0f}ms（{(avg_a + avg_c) / avg_a:.1f} 倍）")
        print(f"  → 换算到整批：只有漏检行的图付这个钱。若某 LOB 占全批 P%%、"
              f"其中 Q%% 的图 0° 未命中，")
        print(f"     整批增幅 ≈ P%% × Q%% × {(avg_c / avg_a):.1f}"
              f"（例：Mac 占 1.4%%、九成图未命中 → 约 "
              f"+{1.4 * 0.9 * (avg_c / avg_a):.1f}%%）")

    print("\n判读：")
    print("  · 紫色候选检出率低 → 紫贴路径对该 LOB 无效，得另想办法（分块 OCR 等）")
    print("  · C 明显高于 B    → 放大是关键，值得把放大补进生产路径")
    print("  · D 明显高于 A    → 3000px 缩放确实是主因，可考虑对大盒 LOB 提高上限")
    print("  · 都很低          → 贴纸多半真的不在画面里/被遮挡，属拍摄规范问题而非算法")


if __name__ == '__main__':
    main()
