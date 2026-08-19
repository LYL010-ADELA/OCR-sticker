#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按天批量跑 OCR：逐个 xlsx → 跑 v7 → 自动验收 → 下一天，全程无人值守。

设计要点（都是踩过的坑）：
  · 只把"源表"当输入。`*_processed.xlsx` 是本流程的**输出**，若被 glob 进来当输入
    喂回去，会产出 `xxx_processed_results.csv` 这种垃圾并浪费数小时。
  · 已完成的天自动跳过：判定标准是"结果 CSV 存在且验收硬检查通过"，而不是只看
    文件在不在——半途中断的结果文件也在，但行数不全。
  · 严格串行：一天必须"跑完 + 验收硬检查过 + ERROR 行占比达标"三道闸全过，
    才进入下一天；任何一道不过就停下来等人处理（--keep-going 可改成继续）。
    这样不会出现"中间某天悄悄坏了、后面几天照跑"的情况。
  · 实时输出 + 落盘日志同时要：用 tee 而非 capture，这样 screen 里能看进度条，
    事后也有完整日志可查。exit code 用 pipefail 保住，不会被 tee 吞掉。
  · 补下载重试轮缺省开启（每天多约 20-30min）。实测恢复率很低（0811 是 0/1337、
    0807/0808 约 1.0-1.3%），但它是下载抖动的安全网，且能把 ERROR 行压到接近 0，
    配合上面的 ERROR 闸更稳。赶时间可用 --no-retry 关掉。

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


def result_quality(xlsx: str) -> dict:
    """读结果 CSV，返回质量指标：行数、ERROR 行数/占比、各 LOB 无贴率。

    ERROR 行占比是"这一天到底跑干净了没有"的关键指标：V7.6 起没有结果的行会被
    回填成 ERROR 占位，所以 ERROR 行多意味着有 worker 被放弃或大量行没跑成。
    """
    out = {'rows': 0, 'errors': 0, 'error_pct': 0.0, 'lob': '(无结果)'}
    csv = results_csv_for(xlsx)
    if not os.path.exists(csv):
        return out
    try:
        df = pd.read_csv(csv, encoding='utf-8-sig', dtype=str,
                         usecols=['识别LOB', '封口贴存在', '位置说明'],
                         on_bad_lines='skip')
    except Exception as e:
        out['lob'] = f'(读取失败 {type(e).__name__})'
        return out
    out['rows'] = len(df)
    if len(df):
        err = df['位置说明'].astype(str).str.startswith('ERROR', na=False)
        out['errors'] = int(err.sum())
        out['error_pct'] = out['errors'] / len(df) * 100
    seal = pd.to_numeric(df['封口贴存在'], errors='coerce')
    parts = []
    for lob in ['iPhone', 'Mac', 'iPad', 'Watch', 'AirPods', 'Accy.']:
        m = (df['识别LOB'] == lob)
        n = int(m.sum())
        if not n:
            continue
        bad = int((m & (seal == 0)).sum())
        parts.append(f'{lob} {bad / n * 100:.1f}%')
    if parts:
        out['lob'] = '无贴率: ' + '  '.join(parts)
    return out


def day_problems(xlsx: str, max_error_pct: float,
                 rc: int | None = None) -> tuple[list[str], str, dict]:
    """判定"这天有没有问题"，返回 (问题列表, 验收摘要行, 质量指标)。

    计划阶段（判断能否跳过）和跑完之后（判断能否进入下一天）**必须共用这一个**
    函数：若计划阶段只查验收、跑完之后又多查 ERROR 占比，那么一个 ERROR 超标的
    天会在下次重跑时被当成"已完成"跳过，永远修不上。
    """
    problems = []
    if rc is not None and rc != 0:
        problems.append(f'v7 退出码 {rc}')
        return problems, '', {'rows': 0, 'errors': 0, 'error_pct': 0.0, 'lob': '—'}
    if not os.path.exists(results_csv_for(xlsx)):
        return ['无结果文件'], '', {'rows': 0, 'errors': 0, 'error_pct': 0.0, 'lob': '—'}
    ok, rows_line = verify_quiet(xlsx)
    if not ok:
        problems.append('验收硬检查未过（行数/订单号对不上）')
    q = result_quality(xlsx)
    if q['error_pct'] > max_error_pct:
        problems.append(f"ERROR 行 {q['errors']} 占 {q['error_pct']:.2f}%"
                        f" > {max_error_pct}%")
    return problems, rows_line, q


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
    ap.add_argument('--no-retry', action='store_true',
                    help='关闭补下载重试轮（缺省开启，每天多约 20-30min）')
    ap.add_argument('--rescan-lob', default='',
                    help='透传给 v7：强制重跑指定 LOB 的漏检行（如 Mac）。'
                         '注意这会让"已完成"的天也需要重跑')
    ap.add_argument('--keep-going', action='store_true',
                    help='某天出问题也继续跑后面的天（缺省是停下来等人处理）')
    ap.add_argument('--max-error-pct', type=float, default=0.5,
                    help='ERROR 行占比超过此百分比即视为"这天有问题"（缺省 0.5）')
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
          f'  补下载重试轮: {"关" if args.no_retry else "开"}'
          + (f'  --rescan-lob {args.rescan_lob}' if args.rescan_lob else ''))
    print(f'出问题时: {"继续跑后面的天" if args.keep_going else "停下来等人处理（严格模式）"}'
          f'  ｜ 判定为有问题: 验收硬检查未过，或 ERROR 行占比 > {args.max_error_pct}%')
    print('=' * 78)
    plan = []
    for p in inputs:
        if args.force or args.rescan_lob:
            plan.append((p, '待跑(强制)'))
            continue
        if not os.path.exists(results_csv_for(p)):
            plan.append((p, '待跑(无结果)'))
            continue
        probs, _, _ = day_problems(p, args.max_error_pct)
        plan.append((p, '已完成(跳过)' if not probs
                     else f'待跑({probs[0][:20]})'))
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
    stopped_at = None
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
               '--extra-retry-passes', '0' if args.no_retry else '1']
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

        # ── 三道闸全过才算"这天没问题"，否则默认停下来等人 ──────────────
        problems, rows_line, q = day_problems(xlsx, args.max_error_pct, rc=rc)
        status = '✓ 通过' if not problems else '✗ ' + '；'.join(problems)
        results.append((day, status, f'{mins:.0f}min', q['lob']))

        if not problems:
            print(f'\n✓ {day} 完成并验收通过（{mins:.0f}min）  {rows_line}'
                  + (f"  ERROR 行 {q['errors']}" if q['errors'] else '  无 ERROR 行'))
            print(f'  {q["lob"]}')
            continue

        print(f'\n✗ {day} 有问题（{mins:.0f}min）：')
        for p in problems:
            print(f'    · {p}')
        print(f'  完整日志: {log}')
        print(f'  手工复查: python3 {VERIFY_SCRIPT} {xlsx}')
        print(f'  修好后重跑本脚本即可——已通过的天会自动跳过，'
              f'这天也会从分片断点续传')
        if not args.keep_going:
            stopped_at = day
            remaining = [day_key(x) for x in todo[i:]]
            print(f'\n⚠ 严格模式：停止批量，不再跑后面的天。'
                  + (f'剩余未跑: {", ".join(remaining)}' if remaining else '（本来就是最后一天）'))
            print(f'  想让它跳过问题天继续跑，加 --keep-going')
            break

    # ── 汇总 ────────────────────────────────────────────────────────────
    lines = ['', '=' * 78,
             f'批量结束，共 {len(results)} 天，总耗时 {(time.time() - t_all) / 3600:.1f} 小时',
             '=' * 78,
             f'{"日期":<8}{"状态":<16}{"耗时":<10}质量概览']
    for day, st, mins, lob in results:
        lines.append(f'{day:<8}{st:<16}{mins:<10}{lob}')
    n_ok = sum(1 for _, st, _, _ in results if st.startswith('✓'))
    lines += ['-' * 78, f'通过 {n_ok}/{len(results)}']
    if stopped_at:
        lines.append(f'因 {stopped_at} 有问题而提前停止（严格模式）。'
                     f'处理完该天后重跑本脚本，已通过的天会自动跳过。')
    else:
        lines.append('未通过的天可单独重跑，会断点续传。')
    lines.append('=' * 78)
    out = '\n'.join(lines)
    print(out)
    with open(os.path.join(LOG_DIR, 'summary.txt'), 'a', encoding='utf-8') as f:
        f.write(f'\n\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} =====\n{out}\n')
    print(f'\n汇总已追加到 {os.path.join(LOG_DIR, "summary.txt")}')
    sys.exit(0 if n_ok == len(results) else 1)


if __name__ == '__main__':
    main()
