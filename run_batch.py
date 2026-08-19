#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按天批量跑 OCR：逐个 xlsx → 跑 v7 → 自动验收 → 下一天，全程无人值守。

设计要点（都是踩过的坑）：
  · 只把"源表"当输入。`*_processed.xlsx` 是本流程的**输出**，若被 glob 进来当输入
    喂回去，会产出 `xxx_processed_results.csv` 这种垃圾并浪费数小时。
  · 已完成的天自动跳过：判定标准是"结果 CSV 存在且验收硬检查通过"，而不是只看
    文件在不在——半途中断的结果文件也在，但行数不全。
  · 单天失败不影响后续天，但连续失败达阈值即停止：那通常是环境级故障
    （GPU 掉了、显存被别人占满），继续跑只是白烧几小时。
  · 实时输出 + 落盘日志同时要：用 tee 而非 capture，这样 screen 里能看进度条，
    事后也有完整日志可查。exit code 用 pipefail 保住，不会被 tee 吞掉。
  · 缺省关掉补下载重试轮：0811 实测该轮 20.3min 恢复 0/1337 行（0.0%），
    0807/0808 也只有 1.0-1.3%。8 天累计要烧 3 小时换回个位数行。要开加 --with-retry。

用法（务必放进 screen/tmux，几小时的活）：
    screen -S ocr
    python3 run_batch.py                      # 跑当前目录下所有未完成的天
    python3 run_batch.py --only 0812,0813     # 只跑指定几天
    python3 run_batch.py --dry-run            # 只列出计划，不真跑
    python3 run_batch.py --rescan-lob Mac     # 顺带把已完成天的 Mac 漏检行重跑

跑完看 logs/summary.txt，或直接看终端最后的汇总表。
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import time

import pandas as pd

OCR_SCRIPT = 'ocr_batch_process_v7.py'
VERIFY_SCRIPT = 'verify_results.py'
LOG_DIR = 'logs'

# 输出文件的后缀——glob 到这些一律不当输入
OUTPUT_SUFFIXES = ('_processed.xlsx',)


def discover_inputs(pattern: str) -> list[str]:
    """找出所有"源表 xlsx"，排除本流程自己的输出。"""
    found = []
    for p in sorted(glob.glob(pattern)):
        base = os.path.basename(p)
        if base.startswith('~$'):            # Excel 打开时的锁文件
            continue
        if any(base.endswith(s) for s in OUTPUT_SUFFIXES):
            continue
        found.append(p)
    return found


def day_key(path: str) -> str:
    """从文件名里抽出日期片段，仅用于 --only 匹配和显示。"""
    m = re.search(r'(\d{4})(?=\.xlsx$)', os.path.basename(path))
    return m.group(1) if m else os.path.splitext(os.path.basename(path))[0]


def results_csv_for(xlsx: str) -> str:
    stem, _ = os.path.splitext(xlsx)
    return f'{stem}_results.csv'


def run_logged(cmd: list[str], log_path: str) -> int:
    """跑命令：实时输出到终端 + 追加到日志，返回真实 exit code。

    用 bash 的 pipefail 而不是 subprocess.capture——capture 会让 screen 里看不到
    进度条（3 小时的活没有进度很难受），而裸跑又没有日志可事后排查。
    """
    quoted = ' '.join(f"'{c}'" if ' ' in c or '(' in c else c for c in cmd)
    shell_cmd = f"set -o pipefail; {quoted} 2>&1 | tee -a '{log_path}'"
    return subprocess.run(['bash', '-c', shell_cmd]).returncode


def verify_quiet(xlsx: str) -> tuple[bool, str]:
    """跑验收脚本，返回 (硬检查是否全过, 摘要行)。不打印全文，避免刷屏。"""
    r = subprocess.run([sys.executable, VERIFY_SCRIPT, xlsx],
                       capture_output=True, text=True)
    ok = (r.returncode == 0)
    rows = '?'
    for line in r.stdout.split('\n'):
        if '源表' in line and '结果' in line and '差' in line:
            rows = line.strip()
            break
    return ok, rows


def lob_summary(xlsx: str) -> str:
    """从结果 CSV 里取各 LOB 无贴率，用于汇总表（跑完一眼看质量是否异常）。"""
    csv = results_csv_for(xlsx)
    if not os.path.exists(csv):
        return '(无结果)'
    try:
        df = pd.read_csv(csv, encoding='utf-8-sig', dtype=str,
                         usecols=['识别LOB', '封口贴存在'], on_bad_lines='skip')
    except Exception as e:
        return f'(读取失败 {type(e).__name__})'
    seal = pd.to_numeric(df['封口贴存在'], errors='coerce')
    parts = []
    for lob in ['iPhone', 'Mac', 'iPad', 'Watch', 'AirPods', 'Accy.']:
        m = (df['识别LOB'] == lob)
        n = int(m.sum())
        if not n:
            continue
        bad = int((m & (seal == 0)).sum())
        parts.append(f'{lob} {bad / n * 100:.1f}%')
    return '无贴率: ' + '  '.join(parts) if parts else '(无 LOB 数据)'


def main():
    ap = argparse.ArgumentParser(
        description='按天批量跑 OCR + 自动验收（无人值守）')
    ap.add_argument('--pattern', default='出库照片处理后_*.xlsx',
                    help='源表 glob（缺省 出库照片处理后_*.xlsx；自动排除 _processed.xlsx）')
    ap.add_argument('--only', default='',
                    help='只跑这些日期，逗号分隔（如 0812,0813）')
    ap.add_argument('--skip', default='', help='跳过这些日期，逗号分隔')
    ap.add_argument('--workers', type=int, default=5)
    ap.add_argument('--gpu-mem-limit-mb', type=int, default=5600)
    ap.add_argument('--with-retry', action='store_true',
                    help='开启补下载重试轮（缺省关闭：0811 实测 20min 恢复 0.0%%）')
    ap.add_argument('--rescan-lob', default='',
                    help='透传给 v7：强制重跑指定 LOB 的漏检行（如 Mac）。'
                         '注意这会让"已完成"的天也需要重跑')
    ap.add_argument('--max-consecutive-failures', type=int, default=2,
                    help='连续失败达此数即停止（缺省 2；环境级故障时避免白烧几小时）')
    ap.add_argument('--force', action='store_true',
                    help='已验收通过的天也重跑（默认跳过）')
    ap.add_argument('--dry-run', action='store_true', help='只列计划，不执行')
    args = ap.parse_args()

    for f in (OCR_SCRIPT, VERIFY_SCRIPT):
        if not os.path.exists(f):
            sys.exit(f'找不到 {f}，请在项目目录下运行本脚本')

    inputs = discover_inputs(args.pattern)
    if not inputs:
        sys.exit(f'没有匹配 {args.pattern} 的源表')

    only = {s.strip() for s in args.only.split(',') if s.strip()}
    skip = {s.strip() for s in args.skip.split(',') if s.strip()}
    if only:
        inputs = [p for p in inputs if day_key(p) in only]
    if skip:
        inputs = [p for p in inputs if day_key(p) not in skip]
    if not inputs:
        sys.exit('过滤后没有要跑的天')

    os.makedirs(LOG_DIR, exist_ok=True)

    # ── 先算出计划：哪些要跑、哪些已完成 ─────────────────────────────────
    print('=' * 78)
    print(f'批量 OCR 计划（共发现 {len(inputs)} 个源表）')
    print(f'参数: --workers {args.workers} --gpu-mem-limit-mb {args.gpu_mem_limit_mb}'
          f'  补下载重试轮: {"开" if args.with_retry else "关"}'
          + (f'  --rescan-lob {args.rescan_lob}' if args.rescan_lob else ''))
    print('=' * 78)
    plan = []
    for p in inputs:
        if args.force or args.rescan_lob:
            plan.append((p, '待跑(强制)'))
            continue
        if not os.path.exists(results_csv_for(p)):
            plan.append((p, '待跑(无结果)'))
            continue
        ok, _ = verify_quiet(p)
        plan.append((p, '已完成(跳过)' if ok else '待跑(验收未过)'))
    for p, st in plan:
        mark = '·' if st.startswith('已完成') else '→'
        print(f'  {mark} {day_key(p):<8} {os.path.basename(p):<40} {st}')
    todo = [p for p, st in plan if not st.startswith('已完成')]
    print(f'\n实际要跑 {len(todo)} 天，预计每天约 3-3.5 小时'
          f'（按 2.7 行/秒、每天约 2.4-2.8 万行估算）')
    if args.dry_run:
        print('\n--dry-run：未执行任何任务。')
        return
    if not todo:
        print('\n全部已完成，无需运行。')
        return

    # ── 逐天执行 ────────────────────────────────────────────────────────
    results = []
    consec_fail = 0
    t_all = time.time()
    for i, xlsx in enumerate(todo, 1):
        day = day_key(xlsx)
        log = os.path.join(LOG_DIR, f'run_{day}.log')
        print('\n' + '=' * 78)
        print(f'[{i}/{len(todo)}] {os.path.basename(xlsx)}   日志: {log}')
        print('=' * 78, flush=True)

        cmd = [sys.executable, OCR_SCRIPT, xlsx,
               '--workers', str(args.workers),
               '--gpu-mem-limit-mb', str(args.gpu_mem_limit_mb),
               '--extra-retry-passes', '1' if args.with_retry else '0']
        if args.rescan_lob:
            cmd += ['--rescan-lob', args.rescan_lob]

        t0 = time.time()
        with open(log, 'a', encoding='utf-8') as f:
            f.write(f'\n\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} '
                    f'{" ".join(cmd)} =====\n')
        try:
            rc = run_logged(cmd, log)
        except KeyboardInterrupt:
            print('\n\n⚠ 收到 Ctrl-C，停止批量。已完成的天不受影响，'
                  '重跑本脚本会自动跳过；当天进度已逐行落盘，也会断点续传。')
            break
        mins = (time.time() - t0) / 60

        if rc != 0:
            consec_fail += 1
            results.append((day, f'✗ v7 退出码 {rc}', f'{mins:.0f}min', '—'))
            print(f'\n✗ {day} 运行失败（退出码 {rc}），详见 {log}')
            if consec_fail >= args.max_consecutive_failures:
                print(f'\n⚠ 连续 {consec_fail} 天失败，停止批量——通常是环境级问题'
                      f'（GPU/显存/磁盘），继续跑只是白烧时间。请先看 {log} 排查。')
                break
            continue

        ok, rows = verify_quiet(xlsx)
        consec_fail = 0 if ok else consec_fail + 1
        results.append((day, '✓ 通过' if ok else '✗ 验收未过',
                        f'{mins:.0f}min', lob_summary(xlsx)))
        print(f'\n{"✓" if ok else "✗"} {day} {"完成并验收通过" if ok else "验收未通过"}'
              f'（{mins:.0f}min）  {rows}')
        if not ok:
            print(f'  ⚠ 验收未通过，请手工跑: python3 {VERIFY_SCRIPT} {xlsx}')
            if consec_fail >= args.max_consecutive_failures:
                print(f'\n⚠ 连续 {consec_fail} 天验收未过，停止批量。')
                break

    # ── 汇总 ────────────────────────────────────────────────────────────
    lines = ['', '=' * 78,
             f'批量结束，共 {len(results)} 天，总耗时 {(time.time() - t_all) / 3600:.1f} 小时',
             '=' * 78,
             f'{"日期":<8}{"状态":<16}{"耗时":<10}质量概览']
    for day, st, mins, lob in results:
        lines.append(f'{day:<8}{st:<16}{mins:<10}{lob}')
    n_ok = sum(1 for _, st, _, _ in results if st.startswith('✓'))
    lines += ['-' * 78,
              f'通过 {n_ok}/{len(results)}；未通过的天可单独重跑，会断点续传',
              '=' * 78]
    out = '\n'.join(lines)
    print(out)
    with open(os.path.join(LOG_DIR, 'summary.txt'), 'a', encoding='utf-8') as f:
        f.write(f'\n\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} =====\n{out}\n')
    print(f'\n汇总已追加到 {os.path.join(LOG_DIR, "summary.txt")}')
    sys.exit(0 if n_ok == len(results) else 1)


if __name__ == '__main__':
    main()
