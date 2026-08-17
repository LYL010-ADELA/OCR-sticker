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
  E crop旋转     C + 把 crop 旋转 90/180/270 再试（测"贴纸折过盒边、文字侧躺"）
                 只在 C 对该图失败后才跑；用 --no-rot 跳过

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
                  upscale_to: int | None, lob: str | None = None) -> tuple[bool, int]:
    """返回 (是否命中, 紫色候选个数)。候选数为 0 说明紫贴检测本身没找到东西。

    lob 必须透传：紫色饱和度下限与 padding 倍数是按 LOB 取的
    （SCAN_PURPLE_PROFILE_BY_LOB），不传就回落到默认值，量出来的是旧行为。
    """
    # 不传 max_candidates：让它按 LOB 专属配置解析（显式传全局值会盖掉专属配置，
    # 诊断脚本曾因类似的参数没透传而量出旧行为）
    boxes = V7.find_purple_scan_candidate_boxes(img, lob=lob)
    for ci, (x1, y1, x2, y2) in enumerate(boxes, 1):
        crop = img.crop((x1, y1, x2, y2))
        if upscale_to:
            crop = upscale(crop, upscale_to)
        _, texts, _, _, _ = V7.ocr_image_full(crop, f"{image_id}_p{ci}")
        if V7.has_scan_text(texts):
            return True, len(boxes)
    return False, len(boxes)


def crop_compare(img: Image.Image, image_id: str, upscale_to: int,
                 dump_dir: str | None, tag: str, records: list,
                 dumped: list, dump_limit: int = 40, dump_all: bool = False,
                 lob: str | None = None) -> tuple[bool, bool, int]:
    """逐个紫贴候选同时跑"不放大"和"放大"两种 OCR，便于逐框对照放大的效果。

    返回 (不放大是否命中, 放大是否命中, 候选个数)。两边各自"命中即停"，
    与生产逻辑一致。

    存盘策略（dump_dir 非空时）：默认只存"放大后才读出锚点"的那几对——它们才
    说明放大起了什么作用；其余候选存了没信息量，20 行样本能产生近 400 张图纯粹
    占地方。dump_all=True 才全量存，dump_limit 是文件数硬上限。
    """
    # 不传 max_candidates：让它按 LOB 专属配置解析（显式传全局值会盖掉专属配置，
    # 诊断脚本曾因类似的参数没透传而量出旧行为）
    boxes = V7.find_purple_scan_candidate_boxes(img, lob=lob)
    hit_plain = hit_up = False
    for ci, (x1, y1, x2, y2) in enumerate(boxes, 1):
        crop = img.crop((x1, y1, x2, y2))
        crop_up = upscale(crop, upscale_to)
        up_factor = max(crop_up.size) / max(1, max(crop.size))

        # 已命中的那一路不再跑后续候选（与生产的"命中即停"一致），记录里显式
        # 标注为跳过，避免看 CSV 时把"没跑"误读成"读不出来"。
        txt_plain = '' if not hit_plain else '(已命中,跳过)'
        txt_up = '' if not hit_up else '(已命中,跳过)'
        plain_read_anchor = up_read_anchor = False
        if not hit_plain:
            t, texts, _, _, _ = V7.ocr_image_full(crop, f"{image_id}_p{ci}")
            txt_plain = t
            plain_read_anchor = V7.has_scan_text(texts)
            hit_plain = hit_plain or plain_read_anchor
        if not hit_up:
            t, texts, _, _, _ = V7.ocr_image_full(crop_up, f"{image_id}_p{ci}up")
            txt_up = t
            up_read_anchor = V7.has_scan_text(texts)
            hit_up = hit_up or up_read_anchor

        # 只存"放大后才读出锚点"的那几对——它们才说明放大起了什么作用。
        # 其余候选（两边都读不出、或不放大就已读出）存了也没信息量，
        # 20 行样本会产生近 400 张图纯属占地方。全量存用 --dump-all。
        worth_dumping = (up_read_anchor and not plain_read_anchor) or dump_all
        if dump_dir and worth_dumping and len(dumped) < dump_limit:
            os.makedirs(dump_dir, exist_ok=True)
            base = f"{tag}_{image_id.split('_')[-1]}_p{ci}"
            p1 = os.path.join(dump_dir, f"{base}_1原始{crop.size[0]}x{crop.size[1]}.jpg")
            p2 = os.path.join(dump_dir, f"{base}_2放大{crop_up.size[0]}x{crop_up.size[1]}.jpg")
            crop.save(p1, quality=92)
            crop_up.save(p2, quality=92)
            dumped.extend([p1, p2])
        records.append({
            '订单号': tag, '图': image_id.split('_')[-1], '候选': ci,
            'crop尺寸': f"{crop.size[0]}x{crop.size[1]}",
            '放大倍数': round(up_factor, 2),
            '不放大读到': txt_plain[:60], '放大后读到': txt_up[:60],
        })
        if hit_plain and hit_up:
            break
    return hit_plain, hit_up, len(boxes)


def crop_rot_hit(img: Image.Image, image_id: str, upscale_to: int,
                 lob: str | None = None,
                 angles: tuple = (90, 180, 270)) -> tuple[bool, int]:
    """变体E：把放大后的 crop 再旋转 90/180/270 试读，返回 (是否命中, 命中角度)。

    动机：Mac 的封口贴是折过盒子边缘粘贴的，正面拍摄时贴纸上的"扫码即领"文字
    侧躺 90°。Mac 目前没有旋转兜底（SCAN_ORIENTATION_FALLBACK_LOBS 只含
    Watch/AirPods）。本变体只在变体C 对该图失败后才跑，与生产"旋转作为第二轮
    兜底"的语义一致，也避免给已命中的图白付成本。
    """
    boxes = V7.find_purple_scan_candidate_boxes(img, lob=lob)
    for ci, (x1, y1, x2, y2) in enumerate(boxes, 1):
        crop = upscale(img.crop((x1, y1, x2, y2)), upscale_to)
        for ang in angles:
            rot = crop.rotate(ang, expand=True)
            _, texts, _, _, _ = V7.ocr_image_full(rot, f"{image_id}_p{ci}r{ang}")
            if V7.has_scan_text(texts):
                return True, ang
    return False, 0


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
    ap.add_argument('--no-rot', action='store_true',
                    help='跳过变体E（旋转兜底）。E 只在 C 对该图失败后才跑')
    ap.add_argument('--gpu-mem-limit-mb', type=int, default=0,
                    help='单进程诊断，缺省 0=不限制')
    ap.add_argument('--save-images', default=None, help='把漏检行的整图存到该目录供人工查看')
    ap.add_argument('--dump-crops', default=None,
                    help='存 crop 图到该目录用于肉眼比对（缺省不存任何文件）。'
                         '只存"放大后才读出锚点"的那几对，其余候选没有信息量')
    ap.add_argument('--dump-limit', type=int, default=40,
                    help='最多存几个 crop 文件（缺省 40，即 20 对）')
    ap.add_argument('--dump-all', action='store_true',
                    help='存全部候选而不只是放大后才读出的那些（会产生几百张图，慎用）')
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
    # 实际生效的紫贴检测参数——按 LOB 取（SCAN_PURPLE_PROFILE_BY_LOB）。
    # 打印出来是为了让"参数没透传进去"这类问题当场暴露，而不是白跑一轮才发现。
    _prof = getattr(V7, 'SCAN_PURPLE_PROFILE_BY_LOB', {}).get(args.lob, {})
    _sat = _prof.get('sat_min', V7.SCAN_PURPLE_HSV_LOW[1])
    _pad = _prof.get('perp_pad_mult',
                     getattr(V7, 'SCAN_PURPLE_PERP_PAD_MULT', 9.0))
    _cand = _prof.get('max_candidates', V7.SCAN_LOCAL_CROP_MAX_CANDIDATES)
    print(f"本次生效的紫贴参数（LOB={args.lob}）: 饱和度下限 S≥{_sat}，"
          f"垂直padding {_pad}× 厚度，每图最多 {_cand} 个候选")
    if _prof:
        print(f"  ← 命中 {args.lob} 专属配置 {_prof}")
    else:
        print(f"  ← 使用默认配置（{args.lob} 没有专属配置）")
    print('=' * 76)

    print('\n初始化 PaddleOCR …')
    V7.init_worker_ocr(gpu_mem_limit_mb=args.gpu_mem_limit_mb)
    print('就绪，开始逐行测试（每行会重新下载图片）\n')

    hit_a = hit_b = hit_c = hit_e = 0
    rows_with_purple = 0
    rot_hit_angles = []
    long_sides, n_images = [], 0
    cached = []          # (tag, [(cid, img)]) 供变体D 复用，避免重复下载
    # 每张图各阶段耗时（秒），用于回答"开了这个会慢多少"
    secs_a, secs_bc, secs_e = [], [], []
    crop_records = []      # 逐候选的"不放大 vs 放大"读到了什么
    dumped_files = []      # 实际存盘的 crop 图（默认只存放大后才读出的那几对）

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

        a = b = c = e = False
        purple_here = 0
        rot_angles_hit = []
        for cid, img in imgs:
            iid = f"{tag}_{cid}"
            t0 = time.perf_counter()
            if not a and full_ocr_hit(img, iid):
                a = True
            t1 = time.perf_counter()
            hb, hc, nb = crop_compare(img, iid, args.upscale,
                                      args.dump_crops, oid, crop_records,
                                      dumped_files, args.dump_limit, args.dump_all,
                                      lob=args.lob)
            t2 = time.perf_counter()
            purple_here += nb
            if hb:
                b = True
            if hc:
                c = True
            secs_a.append(t1 - t0)
            secs_bc.append(t2 - t1)
            # 变体E：仅当本图 C 未命中时才试旋转（与生产"旋转作为第二轮兜底"一致）
            if not args.no_rot and not hc:
                t3 = time.perf_counter()
                hr, ang = crop_rot_hit(img, iid, args.upscale, lob=args.lob)
                secs_e.append(time.perf_counter() - t3)
                if hr:
                    e = True
                    rot_angles_hit.append(ang)
            if a and b and c:
                break

        hit_a += a
        hit_b += (a or b)
        hit_c += (a or b or c)
        hit_e += (a or b or c or e)
        rot_angles_hit and rot_hit_angles.extend(rot_angles_hit)
        if purple_here:
            rows_with_purple += 1
        print(f"[{i}/{len(sample)}] {oid}  图{len(imgs)}张 "
              f"长边{max(max(im.size) for _, im in imgs)}  "
              f"紫候选{purple_here}  "
              f"A={'✓' if a else '✗'} B={'✓' if b else '✗'} C={'✓' if c else '✗'}"
              + ("" if args.no_rot else f" E={'✓' if e else '✗'}"
                 + (f"({rot_angles_hit[0]}°)" if rot_angles_hit else "")))

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
    if not args.no_rot:
        line('E C+crop旋转90/180/270', hit_e, '← 贴纸折过盒边、文字侧躺?')
        if rot_hit_angles:
            from collections import Counter
            print(f"      命中角度分布: {dict(Counter(rot_hit_angles))}"
                  f"  → 若集中在某一角度，说明贴法固定，生产只需试那一个角度")
    if hit_d is not None:
        line(f'D 0°整图长边≤{args.hires}', hit_d, '← 缩放是否为主因')
    print('=' * 76)

    # ── 开销：只有"0° 未命中的图"才会付紫贴的钱，命中即停 ──────────────────
    if secs_a:
        avg_a = sum(secs_a) / len(secs_a)
        avg_bc = sum(secs_bc) / len(secs_bc)
        print(f"\n单张图平均耗时（{len(secs_a)} 张，均为 0° 未命中的图——"
              f"命中的图不会走紫贴路径，不付这个钱）:")
        print(f"  0° 整图 OCR（本来就有）              {avg_a * 1000:7.0f} ms")
        print(f"  + 紫贴crop（本测试同时跑了两种尺寸）  {avg_bc * 1000:7.0f} ms")
        print(f"  注：生产只跑其中一种，实际增量约为上面的一半"
              f"（≈{avg_bc / 2 * 1000:.0f} ms/图，即整图的 {avg_bc / 2 / avg_a:.2f} 倍）")
        est = 1.4 * 0.9 * (avg_bc / 2 / avg_a)
        print(f"  → 换算整批：只有漏检行付这个钱。Mac 占全批约 1.4%%、九成图未命中 "
              f"→ 增幅约 +{est:.1f}%%")

    # ── 逐框对照：放大到底改变了什么 ────────────────────────────────────
    if crop_records:
        flipped = [r for r in crop_records
                   if r['放大后读到'] and '扫码' in r['放大后读到']
                   and '扫码' not in (r['不放大读到'] or '')]
        print(f"\n逐候选对照：共 {len(crop_records)} 个紫贴候选，"
              f"其中 {len(flipped)} 个是【放大后才读出『扫码』】")
        show = (flipped or crop_records)[:8]
        for r in show:
            print(f"\n  订单{r['订单号']} {r['图']} 候选{r['候选']}  "
                  f"crop {r['crop尺寸']} → 放大 {r['放大倍数']}×")
            print(f"    不放大读到: {r['不放大读到'] or '(空)'}")
            print(f"    放大后读到: {r['放大后读到'] or '(空)'}")
        if args.dump_crops and dumped_files:
            csv_path = os.path.join(args.dump_crops, '_crop对照表.csv')
            pd.DataFrame(crop_records).to_csv(csv_path, index=False,
                                              encoding='utf-8-sig')
            mb = sum(os.path.getsize(p) for p in dumped_files) / 1e6
            print(f"\n  已存 {len(dumped_files)} 张 crop 图（{mb:.1f} MB）到 "
                  f"{args.dump_crops}/"
                  + ("" if args.dump_all else "——只含放大后才读出锚点的那几对"))
            print(f"  文件名带 _1原始 / _2放大 后缀，排序后自然成对")
            print(f"  逐框 OCR 文字对照表: {csv_path}")
            print(f"  看完直接删: rm -rf {args.dump_crops}")
        elif args.dump_crops:
            print("\n  没有『放大后才读出锚点』的候选，未存任何图片"
                  "（想看全部加 --dump-all）")
        else:
            print("\n  （未存任何文件；需要肉眼比对时加 --dump-crops 目录名）")

    print("\n判读：")
    print("  · 先核对上面『本次生效的紫贴参数』是否与预期一致——不一致说明参数没"
          "透传，量出来的是旧行为")
    print("  · crop 尺寸普遍接近整图 → 候选框住的不是贴纸而是背景/台面，"
          "该收紧饱和度下限或缩小 padding")
    print("  · 紫色候选检出率低 → 紫贴路径对该 LOB 无效，得另想办法（分块 OCR 等）")
    print("  · C 明显高于 B    → 放大是关键，值得把放大补进生产路径")
    if hit_d is not None:
        print("  · D 明显高于 A    → 整图缩放是主因，可考虑对大盒 LOB 提高上限")
    print("  · 都很低且 crop 已经很紧凑 → 多半是贴纸文字方向不正（折过盒边侧躺）"
          "或真的不在画面里")


if __name__ == '__main__':
    main()
