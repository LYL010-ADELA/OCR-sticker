#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清除 V7.4 及更早版本留下的 "ERROR: GPU显存不足…" 污染结果行。

背景（0806 批次）：worker 连续 GPU OOM 时，V7.4 会把失败行落成
    位置说明 = "ERROR: GPU显存不足，OCR失败（已退避重试3次）: rowXXXX_colY"
的结果行。断点续传（_read_processed_orders）把这些行当成"已处理"，自动补下载
重试轮也不会再碰它们（current_ids 只含本次 pending 行）→ 失败被永久固化进
最终输出。V7.5 起 OOM 行不再落盘，本脚本用于清理 V7.4 已污染的存量分片。

做法：把匹配行从 合并CSV（若存在）+ 全部分片CSV（含 .retryN 轮）以及分片配套
的 .jsonl 中物理删除，使下次重跑 ocr_batch_process_v7.py 时把它们当作未处理行
重试。写盘前原文件先备份为 <原名>.bak。

用法（在结果文件所在目录，把 <结果CSV> 换成实际的 *_results.csv 路径）：
    python purge_oom_error_rows.py <结果CSV>                # 预览，不写盘
    python purge_oom_error_rows.py <结果CSV> --apply        # 实际删除（先备份 .bak）
    python purge_oom_error_rows.py <结果CSV> --apply --all-errors
                                                            # 删除所有 "ERROR:" 行，不限 OOM
结果 CSV 本体不存在也没关系（run 没跑到合并阶段就中断的情况）——只要同目录下
有 <stem>.wshard*.csv 分片就会被发现并清理。
"""

import argparse
import glob
import json
import os
import shutil
import sys

import pandas as pd

DETAIL_COL = '位置说明'


def is_target(detail, all_errors: bool) -> bool:
    d = str(detail)
    if not d.startswith('ERROR:'):
        return False
    return all_errors or ('GPU显存不足' in d)


def purge_csv(path: str, all_errors: bool, apply: bool) -> tuple[int, int]:
    """返回 (匹配行数, 总行数)。apply=True 且有匹配时备份并重写。

    dtype=str：订单号等长数字列必须按原样字符串读写，避免被 pandas 转成
    浮点/科学计数法破坏断点续传的订单号比对。
    on_bad_lines='skip'：上次崩溃残留的半行会被丢弃——与 V7 读取分片时的行为
    一致（该订单被视为未处理，重跑补上）。
    """
    df = pd.read_csv(path, encoding='utf-8-sig', dtype=str, on_bad_lines='skip')
    if DETAIL_COL not in df.columns:
        return 0, len(df)
    mask = df[DETAIL_COL].map(lambda d: is_target(d, all_errors))
    n_bad = int(mask.sum())
    if n_bad and apply:
        shutil.copy2(path, path + '.bak')
        df[~mask].to_csv(path, index=False, encoding='utf-8-sig')
    return n_bad, len(df)


def purge_jsonl(path: str, all_errors: bool, apply: bool) -> int:
    """分片 .jsonl 与 csv 同步清理（仅供人工排查用，不影响续传/合并逻辑）。"""
    kept, removed = [], 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except Exception:
                removed += 1  # 崩溃残留的半行，一并清掉
                continue
            if is_target(rec.get(DETAIL_COL, ''), all_errors):
                removed += 1
            else:
                kept.append(s)
    if removed and apply:
        shutil.copy2(path, path + '.bak')
        with open(path, 'w', encoding='utf-8') as f:
            for s in kept:
                f.write(s + '\n')
    return removed


def main():
    ap = argparse.ArgumentParser(
        description='清除分片/合并 CSV 中 "ERROR: GPU显存不足" 污染行，使断点续传重跑它们')
    ap.add_argument('output_csv',
                    help='最终结果 CSV 路径（分片按 <stem>.wshard*.csv 自动发现，本体可不存在）')
    ap.add_argument('--apply', action='store_true',
                    help='实际写盘（默认只预览）；原文件先备份为 .bak')
    ap.add_argument('--all-errors', action='store_true',
                    help='删除所有 "ERROR:" 行（默认只删含 GPU显存不足 的）')
    args = ap.parse_args()

    stem, _ = os.path.splitext(args.output_csv)
    csvs = []
    if os.path.exists(args.output_csv):
        csvs.append(args.output_csv)
    csvs += sorted(glob.glob(f"{glob.escape(stem)}.wshard*.csv"))
    if not csvs:
        sys.exit(f"未找到 {args.output_csv}，也没有分片文件 {stem}.wshard*.csv")

    mode = '删除所有 ERROR 行' if args.all_errors else '只删 GPU显存不足 的 ERROR 行'
    print(f"模式: {'实际写盘' if args.apply else '预览（不写盘）'}｜{mode}\n")

    total = 0
    for p in csvs:
        try:
            n_bad, n_all = purge_csv(p, args.all_errors, args.apply)
        except Exception as e:
            print(f"  ⚠ 读取 {p} 失败（跳过）: {type(e).__name__}: {e}")
            continue
        total += n_bad
        flag = '' if n_bad == 0 else ('  ← 已删除(原文件备份为 .bak)' if args.apply else '  ← 待删除')
        print(f"  {p}: 匹配 {n_bad}/{n_all} 行{flag}")

        if p != args.output_csv:  # 分片才有配套 jsonl；合并 CSV 没有
            jp = p[:-len('.csv')] + '.jsonl'
            if os.path.exists(jp):
                try:
                    jn = purge_jsonl(jp, args.all_errors, args.apply)
                    if jn:
                        print(f"  {jp}: 匹配 {jn} 行"
                              + ('  ← 已同步清理' if args.apply else ''))
                except Exception as e:
                    print(f"  ⚠ 清理 {jp} 失败（不影响续传，可忽略）: {e}")

    print(f"\n合计匹配 {total} 行（以 CSV 计）。")
    if total and not args.apply:
        print("当前为预览模式，未写盘。确认无误后加 --apply 执行，"
              "然后重跑 ocr_batch_process_v7.py 即可续传重试这些行。")
    elif total and args.apply:
        print("已删除。现在重跑 ocr_batch_process_v7.py，这些行会被当作未处理自动重试。")


if __name__ == '__main__':
    main()
