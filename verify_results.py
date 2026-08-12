#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结果验收：核对最终 CSV 与源表的行完整性，并给出识别质量概览。

重点验的是 V7.6 之前两次静默丢行事故（0808 批次共丢 981 行）：
  A. 按订单号去重把"同一订单多商品/多 LOB"的兄弟行 collapse 成一行（约 970 行）
  B. 被放弃 worker 的 OOM 行未落盘 → 整行从输出消失，连订单号都没有（11 行）
因此这里不只比订单号集合，还比**每个订单号出现的次数**和**逐行位置对齐**——
只比集合的话 A 类丢行完全看不出来（集合相同、行数少了 970）。

用法：
    python verify_results.py 出库照片处理后_0808.xlsx
    python verify_results.py 出库照片处理后_0808.xlsx --results 自定义结果.csv

退出码 0=全部通过，1=有硬性检查未通过（此时不要使用该输出）。
"""

import argparse
import glob
import os
import sys

import pandas as pd

# 与 ocr_batch_process_v7.py 中标记未处理行的文案保持一致（前缀匹配即可）
NO_RESULT_PREFIX = 'ERROR: 未处理'
OOM_ERROR_MARK   = 'GPU显存不足'


class Report:
    def __init__(self):
        self.failed = []

    def check(self, ok: bool, msg: str, detail: str = ''):
        print(f"  {'✓' if ok else '✗'} {msg}")
        if detail:
            print(f"      {detail}")
        if not ok:
            self.failed.append(msg)

    def info(self, msg: str):
        print(f"    · {msg}")


def load_source(path: str) -> pd.DataFrame:
    """读源表并复刻主脚本的幽灵行剔除规则，保证行数基准一致。"""
    df = pd.read_excel(path, dtype=str)
    if '订单号' not in df.columns:
        sys.exit(f"源表 {path} 没有『订单号』列")
    keep = df['订单号'].notna() & (df['订单号'].astype(str).str.strip() != '')
    return df[keep].reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description='核对 OCR 结果与源表的行完整性并给出质量概览')
    ap.add_argument('input_excel', help='源表 xlsx（与跑批时的 --input 相同）')
    ap.add_argument('--results', help='结果 CSV（默认由源表名推导 <stem>_results.csv）')
    args = ap.parse_args()

    stem, _ = os.path.splitext(args.input_excel)
    results_csv = args.results or f"{stem}_results.csv"
    if not os.path.exists(results_csv):
        sys.exit(f"找不到结果文件 {results_csv}（用 --results 指定）")

    src = load_source(args.input_excel)
    res = pd.read_csv(results_csv, encoding='utf-8-sig', dtype=str, keep_default_na=False,
                      na_values=[''], on_bad_lines='skip')
    rep = Report()

    print("=" * 72)
    print(f"源表:   {args.input_excel}")
    print(f"结果:   {results_csv}")
    print("=" * 72)

    # ── 一、行完整性（硬性）──────────────────────────────────────────────
    print("\n【一】行完整性")
    rep.check(len(res) == len(src),
              f"结果行数 == 源表有效行数",
              f"源表 {len(src)} 行，结果 {len(res)} 行，差 {len(res) - len(src):+d}")

    s_ids = src['订单号'].astype(str).str.strip()
    r_ids = res['订单号'].astype(str).str.strip()

    missing = set(s_ids) - set(r_ids)
    extra   = set(r_ids) - set(s_ids)
    rep.check(not missing, "没有订单号从结果中消失",
              f"缺失 {len(missing)} 个: {sorted(missing)[:8]}" if missing else '')
    rep.check(not extra, "结果中没有源表以外的订单号",
              f"多出 {len(extra)} 个: {sorted(extra)[:8]}" if extra else '')

    # 关键：按出现次数比对，专门抓"同订单多商品行被 collapse"这类丢行
    s_cnt, r_cnt = s_ids.value_counts(), r_ids.value_counts()
    dup_ids = s_cnt[s_cnt > 1]
    diff = (s_cnt - r_cnt.reindex(s_cnt.index).fillna(0)).astype(int)
    bad_cnt = diff[diff != 0]
    rep.check(bad_cnt.empty, "每个订单号的出现次数与源表一致（同订单多商品行未被合并）",
              f"{len(bad_cnt)} 个订单号次数不符，合计差 {int(bad_cnt.sum())} 行；"
              f"例: {[(i, int(s_cnt[i]), int(r_cnt.get(i, 0))) for i in bad_cnt.index[:5]]}"
              if not bad_cnt.empty else '')
    if not dup_ids.empty:
        rep.info(f"源表含重复订单号 {len(dup_ids)} 个，涉及 "
                 f"{int(dup_ids.sum())} 行（比唯一订单数多 {int(dup_ids.sum() - len(dup_ids))} 行）"
                 f"——这些就是历史上被去重吃掉的行")

    # 逐行位置对齐：V7.6 以源表为基准回填，所以第 i 行应严格对应源表第 i 行
    if len(res) == len(src):
        aligned = (r_ids.to_numpy() == s_ids.to_numpy())
        n_bad = int((~aligned).sum())
        rep.check(n_bad == 0, "结果逐行与源表位置对齐",
                  f"{n_bad} 行错位，首个错位在第 {int((~aligned).argmax()) + 1} 行"
                  if n_bad else '')

    bad_id = r_ids.str.contains(r'[.eE+]', regex=True, na=False)
    rep.check(int(bad_id.sum()) == 0, "订单号未被转成科学计数法/浮点",
              f"{int(bad_id.sum())} 行异常: {r_ids[bad_id].head(5).tolist()}"
              if bad_id.any() else '')

    # ── 二、失败行（软性，仅报告）────────────────────────────────────────
    print("\n【二】未成功识别的行")
    detail = res['位置说明'].astype(str) if '位置说明' in res.columns else pd.Series([''] * len(res))
    is_err = detail.str.startswith('ERROR', na=False)
    no_result = detail.str.startswith(NO_RESULT_PREFIX, na=False)
    oom_err = is_err & detail.str.contains(OOM_ERROR_MARK, na=False)
    other_err = is_err & ~no_result & ~oom_err

    print(f"    ERROR 行合计: {int(is_err.sum())} / {len(res)} "
          f"（{is_err.mean() * 100:.2f}%）")
    rep.info(f"未处理（worker 被放弃/中断，回填占位）: {int(no_result.sum())}")
    rep.info(f"GPU 显存不足: {int(oom_err.sum())}")
    rep.info(f"其它处理异常: {int(other_err.sum())}")
    if is_err.any():
        rep.info("重跑本脚本会自动重试这些行（ERROR 行按未处理对待）")
        for d in detail[is_err].value_counts().head(3).items():
            rep.info(f"  {d[1]:>5} 次  {d[0][:90]}")

    # 数值列留空必须只出现在未处理行——填 0 会被下游误读成"确认无封口贴"
    if '封口贴存在' in res.columns:
        seal_blank = res['封口贴存在'].isna()
        leaked = seal_blank & ~no_result
        rep.check(int(leaked.sum()) == 0,
                  "『封口贴存在』留空的行仅限未处理行（不会被误读成无贴）",
                  f"{int(leaked.sum())} 行留空但不是未处理行" if leaked.any() else '')
        vals = set(res.loc[~seal_blank, '封口贴存在'].astype(str).str.replace('.0', '', regex=False))
        rep.check(vals <= {'0', '1'}, "『封口贴存在』取值只有 0/1",
                  f"异常取值: {sorted(vals - {'0', '1'})[:5]}" if not vals <= {'0', '1'} else '')

    # ── 三、识别质量概览 ────────────────────────────────────────────────
    print("\n【三】识别质量概览（仅报告，不作硬性判定）")
    if '图片下载状态' in res.columns:
        dl = res['图片下载状态'].fillna('(未处理)').astype(str).value_counts()
        rep.info(f"下载状态: {dl.to_dict()}")
    if '时间' in res.columns:
        wm = res['时间'].notna() & (res['时间'].astype(str).str.strip() != '')
        rep.info(f"水印时间识别率: {wm.mean() * 100:.1f}%")

    lob_col = '识别LOB' if '识别LOB' in res.columns else ('LOB' if 'LOB' in res.columns else None)
    if lob_col and '封口贴存在' in res.columns:
        print(f"\n  各 {lob_col} × 封口贴存在:")
        seal_disp = (res['封口贴存在'].fillna('(未处理)').astype(str)
                     .str.replace(r'\.0$', '', regex=True))
        ct = pd.crosstab(res[lob_col].fillna('(空LOB)'), seal_disp, dropna=False)
        for c in ('0', '1'):
            if c not in ct.columns:
                ct[c] = 0
        denom = (ct['0'] + ct['1']).astype(float)
        ct['无贴率%'] = (ct['0'].astype(float)
                        .div(denom.where(denom > 0))
                        .mul(100).round(1))
        print('  ' + ct.to_string().replace('\n', '\n  '))

    # ── 四、运行残留 ────────────────────────────────────────────────────
    print("\n【四】运行残留")
    res_stem, _ = os.path.splitext(results_csv)
    shards = glob.glob(f"{glob.escape(res_stem)}.wshard*.csv")
    if shards:
        rep.info(f"仍有 {len(shards)} 个分片 csv 未清理 → 上次有 worker 非正常退出；"
                 f"补完剩余行后会自动清理，现在别手动删")
    else:
        rep.info("分片已清理（所有 worker 正常退出，本次运行是干净的）")

    print("\n" + "=" * 72)
    if rep.failed:
        print(f"✗ {len(rep.failed)} 项硬性检查未通过，请勿使用本次输出：")
        for m in rep.failed:
            print(f"    - {m}")
        sys.exit(1)
    print("✓ 行完整性全部通过：结果与源表逐行一一对应，没有丢行。")
    if is_err.any():
        print(f"  注意仍有 {int(is_err.sum())} 行未成功识别（见【二】），重跑可自动重试。")
    sys.exit(0)


if __name__ == '__main__':
    main()
