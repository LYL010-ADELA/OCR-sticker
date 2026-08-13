#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR 封口贴检测 V7 —— 配件宽松判定（扫码即 = 封口贴存在），多进程并行

═══════════════════════════════════════════════════════════════════════════════
V7 相对 V6 的核心改动：配件（Accessory）走"扫码即"宽松判定
  背景：除 iPhone/iPad/Mac/Watch/AirPods 五大核心品类外，门店还要贴大量配件
  （Beats、原厂配件——充电器/线缆/转换器/EarPods/Pencil/HomePod/AirTag/妙控键鼠、
  甄选三方配件 3PP——ANKER/UGREEN/TORRAS/MOPHIE… 保护壳/钢化膜/移动电源 等）。
  这些配件包装形状、尺寸、拍摄角度千差万别，原有"封口贴必须落在盒面某相对区域"
  的严格位置规则完全不适用，会把贴对了的配件大面积误判为不合规。

  V7 规则（用户确认）：
    1. LOB 归类：凡 LOB ∈ {iPhone,iPad,Mac,Watch,AirPods} 走原严格位置校验；
       其它任何"非空"LOB 一律视为配件，走宽松判定；LOB 为空/缺失仍判 UNKNOWN LOB。
    2. 配件宽松判定：只要在任一出库图中（含 0/90/180/270 旋转 + 紫色封贴局部放大
       OCR）检出"扫码即"锚点，即认为封口贴存在且规范粘贴：
         封口贴存在=1、是否规范粘贴=1、贴纸位置规范=1；
       未检出则 封口贴存在=0、是否规范粘贴=0、贴纸位置规范=-1。
    3. 配件不做位置校验、不做双贴校验、不做非官方贴纸颜色检测（纯锚点判定），
       避免配件包装多样性带来的误报。

  五大核心品类的检测逻辑（位置/双贴/颜色）与 V6 逐字节一致。

V7 补充：下载稳健性改进（解决"下载速度跟不上识别速度，第二次重跑才能识别上"）
  现象：服务器下载图片的并发量（--workers × --download-workers，默认 12×6=72）
  超过图片服务器/CDN 承受能力时，部分请求超时/被限流，耗尽重试后被判定为下载
  失败 → 找不到背面图 → 误判不合规；重新单独跑这批失败行时并发骤降，反而成功。
  注意：架构上 OCR 本就严格等该图片的下载 future 完成（成功或耗尽重试）才会执行，
  不存在"边下边识别"的竞态；真正瓶颈是下载本身在高并发下的失败率。

  1. 全局下载并发上限（`--download-concurrency`，默认 32）：新增跨所有 worker
     进程共享的 `multiprocessing.BoundedSemaphore`，把实际同时发起的下载请求数
     卡在一个固定上限内，不再随 --workers/--download-workers 乘积失控增长。
     只包住实际网络请求（`requests.get`），不影响退避等待和 OCR 的 GPU 并发。
  2. 下载完整性校验：校验响应 `Content-Length` 与实际收到字节数，不一致（部分
     CDN 会返回 200 但内容被截断而不报错）时视为失败并重试，而不是把截断的
     图片喂给 OCR。
  3. 自动补下载重试轮（`--extra-retry-passes`，默认 1，设 0 关闭）：主批次跑完
     后，自动收集"封口贴存在≠1"（含下载失败/未写入结果）的订单号，在并发已
     大幅降低的情况下自动再跑一轮，等价于把你现在手动重跑第二次的动作内建进
     单次运行；仍未成功可继续下一轮，直至用完设定轮数或不再有新的失败行。
     注：该重试轮不区分"真下载失败"与"确实没贴封口贴"，两者都会被重试一次，
     属于有意为之的取舍（换取召回率），多数情况下额外耗时有限。

V7.1 热修（2026-08-04，0803 批次事故复盘）：显存挤兑导致整图 OCR 大面积静默失败
  事故：12 个 worker 进程的 paddle auto_growth 显存缓存只增不还，跑数小时后
  32GB 单卡仅剩几十 MB 可分配（12 进程合计囤 31GB、实际在用不到 2GB）；
  3000px 整图 OCR 需要的 400~800MB 大块激活分配集体 OOM，异常被 ocr_image_full
  的 except 吞掉后返回空文本 → 约 65% 行被误判"未找到背面图"；同时每次 OOM
  前 paddle 的 GC/重试等待把速率从 ~2 行/秒拖到 0.14 行/秒（CPU/GPU 双双空转，
  仅输入尺寸小的紫贴裁剪 OCR 能挤进显存碎片存活）。
  1. worker 每处理 GPU_CACHE_RELEASE_EVERY_ROWS 行主动 empty_cache() 归还空闲
     显存，根治多进程缓存挤兑；
  2. OCR 遇显存 OOM：先清本进程缓存重试一次，仍失败则向上抛出 → 该行记 ERROR
     （重跑可续传），绝不再静默当成"图上没字"；
  3. 旋转扫描（90/180/270）只保留紫色封贴局部 OCR，去掉旋转整图 OCR——0803
     批次实测 1746 次命中中旋转整图 OCR 仅贡献 1 次，却是显存峰值与耗时的
     主要来源；且旋转轮后置为第二轮：所有图 0° 均未命中才逐图旋转，不再
     "第 1 张图 4 角度试完才看第 2 张"；
  4. 每 worker 默认 OMP_NUM_THREADS=4，避免多进程 CPU 线程互相踩踏。

V7.2 加固（同日）：瞬时 OOM 当场消化（V7.1 重跑首 5min 实测约 2.5% 行仍撞峰值）
  1. 整图 OCR 增加跨进程并发闸（--ocr-concurrency，默认 5）：仅长边≥1500px 的
     大图推理占名额，防止 8 个 worker 同时到达 3000px 推理显存峰值互相挤兑；
     紫贴裁剪小图/水印 OCR 不占名额、不受影响。
  2. OOM 处理从"清缓存立即重试 1 次"改为"清缓存 + 退避 0.5/1/2s，最多 3 次"：
     瞬时峰值几秒内自然消散，绝大多数行当场恢复，不再攒到结尾的自动重试轮。

V7.3 根治（同日）：每进程显存硬上限（--gpu-mem-limit-mb，默认 3300）
  V7.2 实测：8 worker 下仍有 ~2% 行 OOM，且退避 3 次仍失败、连紫贴小图都会
  OOM——说明显存被邻居进程的 auto_growth 缓存囤走（有单进程囤到 6.3GB），
  empty_cache 只能清本进程的缓存，救不了跨进程囤积。
  FLAGS_gpu_memory_limit_mb 让每个进程到达自己额度时必须回收自己的空闲缓存，
  而不是伸手抢占全卡剩余，从机制上杜绝跨进程挤兑；配合缓存释放频率 10→5 行。
  默认 3300MB 按 8 worker 设计；调整 --workers 时按 (显存MB-2000)/workers 同步调整。
  实测结论：3000px 整图 OCR 每进程真实工作集约 4-5GB，8×3300 会让每次大图推理
  撞自己额度（速度崩到 0.3 行/秒）——推荐 --workers 6 --gpu-mem-limit-mb 4500
  （实测 2.6-2.7 行/秒），宁少进程、勿紧额度。

V7.4 自愈（同日）：OOM 死循环自动重建 OCR 引擎
  实测：worker 中过一次 OOM 后可能进入"每行必 OOM"死循环——异常路径在 paddle
  内部泄漏的活跃张量占死本进程额度，empty_cache 无法释放活跃块，该 worker 分片
  剩余行以每行 ~40s 的速度全部报废，并把整个 run 吊死在这一个分片上。
  worker 连续 OOM_REINIT_AFTER_CONSECUTIVE_ROWS(=2) 行 OOM 失败即整体销毁重建
  PaddleOCR predictor（释放其持有的全部显存，代价 ~10s 模型加载），继续处理。

V7.5（2026-08-06）：OOM 自愈从"进程内重建引擎"升级为"worker 退出 + 主进程自动重启"
  0806 批次实测 V7.4 的进程内重建无效：重建完成后小图能过、大图立刻再 OOM，
  且 4/6 个 worker 相继死亡——泄漏的活跃张量在 paddle C++ 侧，Python 端把
  predictor 置 None + gc 收不回来，每次重建反而在每进程显存硬上限内再叠一份
  模型，进程贴着自己额度空转（重建 30s + 每行退避 ~40s 全报废）。
  1. worker 连续 OOM_EXIT_AFTER_CONSECUTIVE_ROWS(=2) 行 OOM → 直接退出
     （退出码 WORKER_EXIT_OOM_RESPAWN=43），显存随进程终止由 CUDA 驱动整体
     回收（唯一彻底的释放手段）；主进程识别该退出码后自动重启该 worker，
     靠分片内断点续传接着跑。连续 OOM_RESPAWN_MAX_STRIKES(=3) 次重启之间
     没有任何新增落盘行（原地 OOM 无进展）则放弃——那说明不是泄漏而是
     配置问题（显存上限对这批大图太紧），盲目重启只会空转。
  2. OOM 行不再落 "ERROR: GPU显存不足" 结果行：V7.4 落盘后，断点续传把它
     当"已处理"永久跳过，自动补下载重试轮也不会再碰它（current_ids 只含本次
     pending 行）——失败被永久固化进最终输出。现在 OOM 行保持未写入，由
     重启后的进程 / 本轮补下载重试轮 / 下次重跑自然重试。
  3. 历史污染兜底：_read_processed_rows 把遗留的 "ERROR:" 行视为未处理
     （重跑自动重试）；_merge_frames 保证同一订单的正常结果行永远优先于
     ERROR 行（不管落在哪个分片/轮次）。配套 purge_oom_error_rows.py 可把
     已污染分片里的 OOM ERROR 行一次性物理清除。

V7.6（2026-08-12）：行完整性根治——输出行数必须等于源表行数，一行都不能少
  0808 批次核对发现两条独立的静默丢行路径，合计丢了 981 行（源表 27756 →
  输出 26775），而且历史每一批都在丢：
  1. 按订单号去重 collapse 掉同订单多行（约 970 行）：订单号在源表里可能重复
     ——同一订单含多个商品/多个 LOB 时各占一行、各有各的出库图。
     _merge_frames 的 drop_duplicates(subset='订单号') 把它们合并成一行。
     修复：结果行携带源行号(SRC_ROW_COL)作为稳定行身份，按它去重；旧分片没有
     该列时用"订单号+全部图片地址列"的行指纹回推源行，仍可完整恢复。
  2. 被放弃 worker 的 OOM 行整行消失（11 行）：V7.5 让 OOM 行不落盘以便重试，
     但 worker 被放弃时这些行就再没人写 → 最终输出里整行不存在（连订单号
     都没有），比留个 ERROR 行更危险。
     修复：_merge_frames 改为以源表为基准左连接回填——每一源行恰好输出一行，
     没有结果的行保留原始列并把 位置说明 标记为 ERROR，数值列留空（留空而非
     填 0，避免被下游误读成"确认无封口贴"）。
  3. merge_shards 增加行数硬校验：输出行数 != 源表行数就拒绝写出并保留分片，
     丢行不可能再静默发生。
  4. 断点续传同步改为按源行号判定，订单号重复时不会误跳过兄弟行。

V7.7（2026-08-13）：Mac 召回改进——紫贴局部 OCR 对 Mac 开放 + 补上真正的放大
  0808/0809 两批 Mac 无贴率 35-36%（其余 LOB：iPhone 2.8%、Watch 1.1%），
  且失败 100% 是"未找到含'扫码即领'的背面图"——锚点根本没读到，不是位置判
  出去的。根因是 Mac 的检测路径最薄：紫贴局部 OCR 与旋转兜底此前只对
  Watch/AirPods/配件开放，Mac 只有一次机会（单张 0° 整图 OCR）；而 MacBook
  是最大的包装盒，拍全就必然拍远，封口贴占的像素最少，再被 OCR_MAX_SIDE
  整体缩小（实测源图长边 4839 → 3000，仅 0.62 倍）后字就没了。
  1. Mac 加入 SCAN_LOCAL_CROP_LOBS，启用 0° 紫贴局部 OCR。真实漏检行实测
     救回 35%（紫色候选检出率 20/20，该路径对棕色纸箱同样有效）。
     旋转兜底仍由 SCAN_ORIENTATION_FALLBACK_LOBS 控制、Mac 不在其中——旋转
     对 Mac 未实测，且开销是 0° 的数倍，不盲目开。
  2. 紫贴 crop 送 OCR 前放大到长边 SCAN_CROP_UPSCALE_TO=2000。此前"局部放大
     OCR"名不副实：resize_for_ocr 只会缩小、从不放大，小 crop 原样进 OCR，
     字还是那么小。补上后实测救回 35% → 45%。放大后 crop 最多 4M 像素，
     比整图最坏的 9M 还省显存，不引入新 OOM 风险。
     V7.7.2：放大只对 SCAN_CROP_UPSCALE_LOBS（缺省仅 Mac）生效。放大是为
     "盒大、拍得远、贴纸占像素少"准备的；Watch 1.1%、AirPods 4.4% 的漏检率
     本就很低（盒小、距离近、贴纸相对够大），而它们和配件恰是紫贴路径的调用
     大头（配件还带 3 个旋转角度），一律放大等于把 6 倍像素开销花在没有收益
     的地方。限定 Mac 后新增算力压在全批 1.4% 的行上。--scan-crop-upscale-lobs
     可调整，实测某 LOB 有收益再加。
  3. 放大后必须把 OCR 返回的 poly 按放大倍数除回原 crop 尺度再偏移到整图
     坐标，否则贴纸位置系统性外扩 → rel_x/rel_y 越界 → 位置校验误判。
  验证工具见 diagnose_lob_recall.py（在真实漏检行上量测各路径召回）。
  注：Mac 的位置校验实际早已形同虚设——两个镜像区域配 ±0.25 容差展开后是
  X∈[0,1]、Y∈[-0.25,1.25]，任何落在盒面内的贴纸都算规范；仅有的判否是
  rel_x>1.0（贴纸被检到盒外，属坐标映射误差）。故本次只改召回、不动判定口径。

以下为 V5 说明（Watch/AirPods 锚点增强，V6/V7 未改动核心检测）：

V5 相对 V4 的核心改动：
  1. Watch / AirPods 找背面图时增加 0/90/180/270 度 OCR，解决竖拍、倒拍、
     贴纸小字方向不正导致的"扫码即领"漏检。
  2. Watch / AirPods 在整图 OCR 未命中时，检测紫色官方封贴候选区域，对局部
     crop 放大 OCR，作为"扫码即领"锚点兜底。
  3. Watch 增加相对坐标硬边界，避免包装盒透视框选歪后 rel_x/rel_y 明显越界
     仍被判为位置规范。

V4 相对 V3 的唯一改动：并行架构（检测逻辑 100% 不变）
═══════════════════════════════════════════════════════════════════════════════
背景：V3 单进程串行处理，每行内部在「下载(网络) → OCR(GPU) → 透视矫正/颜色检测
(CPU OpenCV)」三个阶段间串行切换。CPU 跑 OpenCV 时 GPU 空闲，GPU 跑 OCR 时
CPU 空闲 → 单进程永远喂不满 5090（实测 GPU 利用率仅 ~4%，5.5G/32G 显存）。

V4 做法：启动 N 个独立 worker 进程，每个进程各自加载一份 PaddleOCR 到同一块
GPU（32G 显存可轻松容纳 8~10 份，每份约 2G）。GPU 驱动会自动交错调度各进程的
计算 —— 当 worker A 在跑 CPU OpenCV 时，B~H 正好在喂 GPU。于是：
  • GPU 利用率 4% → 40~70%
  • 吞吐 ~N 倍（受 CPU 核数上限约束）
  • 检测结果与 V3 逐字节一致（process_row 及所有检测函数原样保留）

进程协作：
  • 主进程读 Excel、剔除幽灵行、按「跨步(strided)」把待处理行分给各 worker
  • 每个 worker 写自己的分片文件 {stem}.wshard{K}.csv / .jsonl（无锁，崩溃安全）
  • worker 的详细逐行日志重定向到 {stem}.wshard{K}.log，终端只显示聚合进度
  • 全部完成后主进程合并分片 → 按原始行序输出最终 CSV + Excel
  • 断点续传：启动时扫描已有分片，跳过已处理订单号；崩溃/中断可直接重跑

用法：
  python ocr_batch_process_v5.py \\
      --input /path/W13.xlsx \\
      --output-csv /path/W13_results.csv \\
      --output-excel /path/W13_processed.xlsx \\
      --workers 12
  （--workers 缺省 = min(12, CPU核数)；其余输出路径缺省由 --input 推导）

───────────────────────────────────────────────────────────────────────────────
以下为 V3 原始说明（检测逻辑，V4 未改动）：

OCR 封口贴检测 V3 —— 简化逻辑，大幅提升检出率

相对 V2 的核心改动：
  1. 背面识别宽松化：只要 OCR 检测到"扫码即领"即视为背面，
     包装盒检测失败（fallback）不再跳过该图片。
  2. 找到第一张背面即停止扫描，节省约 50% OCR 时间。
  3. 去除角度验证步骤（贴纸偏角不再影响合规判定）。
  4. 位置容差放宽：STICKER_POSITION_TOLERANCE 0.15 → 0.25，
     贴纸适当超出包装盒边界仍视为合规。
  5. 新增输出列"是否存在非官方贴纸"（0/1）。

检测流程：
  Step 1  遍历图片，找第一张含"扫码即领"的图 → 背面
          无背面 → 是否规范粘贴=0
  Step 2  仅对背面图检测非官方贴纸（颜色检测）
          检出 → 是否存在非官方贴纸=1，是否规范粘贴=0
  Step 3  无非官方贴纸 → 单/双贴位置验证（放宽容差，无角度约束）
          单贴或双贴位置合规 → 是否规范粘贴=1

position_valid 编码：
  -1 : 未找到封口贴（无背面图）
   0 : 贴纸位置不符合规范
   1 : 贴纸位置规范（合格）
   2 : 贴纸平铺错误（整条贴条未绕折封口）
   4 : 检测到非官方贴纸
"""

import re
import numpy as np
import cv2
import pandas as pd
import requests
from PIL import Image
from io import BytesIO
import os
import sys
import time
import traceback
import json
import argparse
import multiprocessing as mp
import glob
from concurrent.futures import ThreadPoolExecutor

# 注意：PaddleOCR 的导入被刻意推迟到每个 worker 进程的 init_worker_ocr() 内部，
# 目的有二：
#   1. 主进程完全不加载 paddle/CUDA（省显存、省启动时间，避免 fork 相关的 CUDA 问题）
#   2. 多进程用 'spawn' 启动，每个子进程在设置好 FLAGS 环境变量后再导入 paddle

# ─── 可调参数 ────────────────────────────────────────────────────────────────
IMAGE_COLUMNS    = ['图片地址', 'Unnamed: 16', 'Unnamed: 17', 'Unnamed: 18', 'Unnamed: 19', 'Unnamed: 20', 'Unnamed: 21', 'Unnamed: 22']
DOWNLOAD_WORKERS = 6
DOWNLOAD_RETRIES = 3
DOWNLOAD_TIMEOUT = 20
DOWNLOAD_RETRY_BACKOFF = 1.0

# V7：全局下载并发上限（跨所有 worker 进程共享的 BoundedSemaphore）。
# --workers × --download-workers 的乘积（默认 12×6=72）容易超过图片服务器/CDN
# 承受能力，导致高失败率；这里再加一层跨进程总闸，与 workers 数量解耦。
DEFAULT_GLOBAL_DOWNLOAD_CONCURRENCY = 32

# V7：主批次跑完后，自动对"封口贴存在≠1"的行做的补下载重试轮数（0=关闭）。
DEFAULT_EXTRA_RETRY_PASSES = 1

# V7.1：worker 每处理 N 行主动调用 paddle.device.cuda.empty_cache() 归还空闲显存。
# 多进程共享单卡时 auto_growth 缓存只增不还，是 0803 批次显存挤兑事故的根因。
# V7.3：10 → 5，配合每进程显存硬上限进一步压缩囤积窗口。
GPU_CACHE_RELEASE_EVERY_ROWS = 5

# V7.4→V7.5：worker 连续 N 行因 GPU OOM 失败即自愈。V7.4 的"进程内销毁重建
# predictor"实测救不回来：泄漏的活跃张量在 paddle C++ 侧，Python 端置 None +
# gc 收不回来，每次重建反而在每进程显存硬上限内再叠一份模型，之后大图推理必
# OOM（小图仍能挤进去）。V7.5 改为 worker 直接退出——显存随进程终止由 CUDA
# 驱动整体回收，这是唯一彻底的释放手段——主进程识别退出码后自动重启该 worker，
# 分片内断点续传接着跑。
OOM_EXIT_AFTER_CONSECUTIVE_ROWS = 2
# worker 请求"重启我"的专用退出码（区别于 0=正常完成、1=初始化失败等真崩溃）
WORKER_EXIT_OOM_RESPAWN = 43
# 连续 N 次重启之间没有任何新增落盘行（原地 OOM 无进展）则放弃该 worker：
# 说明不是泄漏而是配置问题（如显存上限对这批大图太紧），盲目重启只会空转。
OOM_RESPAWN_MAX_STRIKES = 3

# V7.3：每 worker 进程显存硬上限（MB，FLAGS_gpu_memory_limit_mb；0=不限制）。
# V7.2 实测：清缓存+退避重试救不了"显存被邻居进程囤走"的场景（连紫贴小图都会
# OOM）——empty_cache 只能清本进程的缓存。硬上限让任何进程到达自己额度时必须
# 回收自己的空闲缓存，而不是伸手抢占全卡剩余，从机制上杜绝跨进程囤积挤兑。
# 默认按 8 worker × 3300MB ≈ 26.4GB 设计（32GB 卡留 ~5GB 给桌面与峰值余量）；
# 调整 --workers 时请同步调整：limit ≈ (显存总量MB - 2000) / workers。
DEFAULT_GPU_MEM_LIMIT_MB = 3300

# V7.2：OOM 当场消化。
# 整图 OCR 跨进程并发闸：只有长边 ≥ OCR_GATE_MIN_SIDE 的输入占名额，防止多个
# 进程同时到达 3000px 推理的显存峰值互相挤兑；裁剪小图/水印 OCR 不占名额。
DEFAULT_OCR_CONCURRENCY = 5
OCR_GATE_MIN_SIDE       = 1500
# OOM 清缓存后的退避重试次数（0.5s/1s/2s 递增），瞬时峰值几秒内自然消散。
OCR_OOM_MAX_RETRIES     = 3

# 整图 OCR 输入尺寸。PaddleOCR 默认 text_det_limit_type='min' 会把短边放大到
# side_len，竖长图/横长图会被内部放成 5000~7000px 后再缩回 max_side_limit=4000，
# 非常拖慢。这里显式用 max：只限制长边不超过 3000，不做无谓放大。
OCR_MAX_SIDE = 3000
OCR_DET_LIMIT_TYPE = 'max'
OCR_DET_LIMIT_SIDE_LEN = 3000

# V5：Watch/AirPods 的封贴文字常因竖拍、倒拍、贴纸小而被整图 OCR 漏掉。
# 仅对这两个 LOB 启用增强，控制额外耗时和误触发面。
SCAN_ORIENTATION_FALLBACK_LOBS = {"Watch", "AirPods"}
# V7.7：加入 Mac —— MacBook 是最大的包装盒，拍全就必然拍远，封口贴占的像素最少，
# 再被 OCR_MAX_SIDE 整体缩小（实测源图长边 4839 → 3000，仅 0.62 倍）后字就没了。
# 0808/0809 两批 Mac 无贴率 35-36%，且失败 100% 是"未找到含'扫码即领'的背面图"
# （锚点没读到，而非位置判出去）。真实漏检行上实测：加本兜底可救回 35%，
# 配合下面的 crop 放大可救回 45%（详见 diagnose_lob_recall.py）。
# 注意本集合只控制 0° 紫贴局部 OCR；旋转兜底由 SCAN_ORIENTATION_FALLBACK_LOBS
# 决定，Mac 不在其中，因此加进来不会引入旋转扫描的额外开销（旋转对 Mac 未实测）。
SCAN_LOCAL_CROP_LOBS           = {"Watch", "AirPods", "Mac"}
SCAN_OCR_ANGLES                = (0, 90, 180, 270)
SCAN_LOCAL_CROP_MAX_CANDIDATES = 5

# V7.7：紫贴局部 crop 送入 OCR 前放大到的长边。
# 此前 "局部放大 OCR" 名不副实——resize_for_ocr 只会缩小、从不放大，小 crop 原样
# 进 OCR，字还是那么小，白付了一次推理。补上放大后 Mac 实测召回 35% → 45%。
# 2000 的另一个好处：放大后的 crop 最多 2000×2000=4M 像素，比整图最坏情况
# 3000×3000=9M 还省显存，不会引入新的 OOM 风险。
SCAN_CROP_UPSCALE_TO = 2000

# V7.7.2：只对这些 LOB 放大。放大是为"盒大、拍得远、贴纸占像素少"准备的，
# 只有 Mac 属于这种情况（无贴率 35-36%）。Watch 1.1%、AirPods 4.4% 的漏检率
# 本就很低——盒子小、拍摄距离近，贴纸在画面里相对够大，不放大也能读到，
# 再放大只是白付算力；而它们和配件恰恰是紫贴路径的调用大头（配件还带
# 3 个旋转角度），把放大限制在 Mac 可以把新增开销压在全批 1.4% 的行上。
# 逗号分隔的 --scan-crop-upscale-lobs 可调整；实测某 LOB 有收益再加进来。
SCAN_CROP_UPSCALE_LOBS = {"Mac"}

# 官方封口贴紫色条带 HSV 范围（OpenCV H: 0-179）。这里只作为局部 OCR 候选，
# 不直接判定有贴，因此阈值宁可略宽。
SCAN_PURPLE_HSV_LOW  = (105, 35, 35)
SCAN_PURPLE_HSV_HIGH = (165, 255, 245)

# Watch/AirPods 的位置坐标若明显超出矫正盒面，多半是透视框选歪或文本映射异常。
STRICT_UNIT_BOUNDS_MARGIN = 0.10

# 必须按“文本”读写的列：订单号是 19 位整数，超过 float64/Excel 双精度可精确表示的
# 上限(2^53≈16 位)。若按数字写入 .xlsx，Excel 会把它存成 double → 末几位被抹平
# (如 5119350049330019519 → 5119350049330019328)。全程当字符串处理即可保精度。
ID_TEXT_COLS = ['订单号']
_ID_DTYPE    = {c: str for c in ID_TEXT_COLS}

# 位置容差（相对坐标系）：封口贴绕折超出包装盒边界仍算合规
# V3 放宽：0.15 → 0.25
STICKER_POSITION_TOLERANCE = 0.25

# 包装盒检测：最大内部分辨率
BOX_DETECT_MAX_SIDE = 1200

# 极端兜底：包装盒最小面积占比（已宽松化，fallback 不再跳过）
BOX_FRONTAL_MIN_RATIO = 0.02

# 非官方贴纸颜色检测阈值
UNOFFICIAL_SAT_ABOVE_BG  = 55
UNOFFICIAL_VAL_RANGE     = (40, 230)
UNOFFICIAL_AREA_RATIO    = 0.05       # V3.1: 默认 5%（非 AirPods 白盒）
UNOFFICIAL_SOLIDITY_MIN  = 0.45
UNOFFICIAL_EDGE_GRAD_MIN = 6.0

# 白色背景最少像素数：低于此值说明拍照条件较差，无法可靠建立白平衡基准
UNOFFICIAL_WHITE_BG_MIN  = 800

# 官方印刷关键词：这些区域包含官方盒面印刷彩色元素，颜色检测时排除
# AirPods 盒子右侧紫色"Apple授权专营店 在你身边"条带是典型误触发来源
OFFICIAL_PRINT_KEYWORDS  = ["Apple授权专营店", "在你身边", "授权专营店"]

# 棕色瓦楞纸盒 HSV 范围（Mac 专用）
BROWN_HSV_LOW  = (5,  30, 40)
BROWN_HSV_HIGH = (30, 200, 220)

# ── 多 LOB 配置 ──────────────────────────────────────────────────────────────
LOB_CONFIGS: dict[str, dict] = {
    "iPhone": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        "auth_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.70, "y_max": 1.00},
        "front_face_aspect_range": (1.6, 2.4),
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            "area_ratio": 0.05, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    "Watch": {
        "sticker_count": "single_or_dual",
        "scan_sticker": [
            {"x_min": 0.00, "x_max": 1.00, "y_min": 0.00, "y_max": 0.45},
            {"x_min": 0.00, "x_max": 1.00, "y_min": 0.55, "y_max": 1.00},
        ],
        "auth_sticker": [
            {"x_min": 0.00, "x_max": 1.00, "y_min": 0.00, "y_max": 0.45},
            {"x_min": 0.00, "x_max": 1.00, "y_min": 0.55, "y_max": 1.00},
        ],
        "front_face_aspect_range": (2.5, 5.0),
        "unit_bounds_margin": STRICT_UNIT_BOUNDS_MARGIN,
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            "area_ratio": 0.05, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    "AirPods": {
        "sticker_count": "single_only",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.50},
        "auth_sticker": None,
        "front_face_aspect_range": (0.85, 1.35),
        "unit_bounds_margin": STRICT_UNIT_BOUNDS_MARGIN,
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            # AirPods 官方盒面彩色印刷（紫色授权条带 + 绿色回收箭头）最高可占
            # 整个盒面约 10%，需将阈值设在其上方才不会误判官方贴纸为非官方
            "area_ratio": 0.12, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    # 注意（V7）：配件不再逐一枚举进 LOB_CONFIGS。原 "Accy." 严格位置配置已废弃，
    # 现在凡非核心 5 类的 LOB（Accy./Beats/原厂配件/甄选三方配件 3PP/…）统一走
    # ACCESSORY_LOB_CONFIG 的"扫码即"宽松判定，详见 resolve_lob_config()。
    "iPad": {
        "sticker_count": "single_or_dual",
        "scan_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        "auth_sticker": {"x_min": 0.50, "x_max": 0.95, "y_min": 0.70, "y_max": 1.00},
        "front_face_aspect_range": (1.2, 1.7),
        "unofficial_color": {
            "enabled": True, "mode": "white_box",
            "sat_above_bg": 55, "val_range": (40, 230),
            "area_ratio": 0.05, "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
    "Mac": {
        "sticker_count": "single_or_dual",
        # 扫码即领：下方居中 或 上方居中（镜像）均合规
        "scan_sticker": [
            {"x_min": 0.25, "x_max": 0.75, "y_min": 0.70, "y_max": 1.00},
            {"x_min": 0.25, "x_max": 0.75, "y_min": 0.00, "y_max": 0.30},
        ],
        # Apple授权专营店：上方左侧 或 上方右侧（镜像）均合规
        "auth_sticker": [
            {"x_min": 0.05, "x_max": 0.50, "y_min": 0.00, "y_max": 0.30},
            {"x_min": 0.50, "x_max": 0.95, "y_min": 0.00, "y_max": 0.30},
        ],
        "front_face_aspect_range": (1.2, 2.0),
        # 非官方贴纸颜色检测对 Mac 关闭：Mac 多为棕色纸箱，箱上常贴大块白色物流标/
        # 授权店封签，会被 brown_box 检测大面积误判为"疑似非官方贴纸"（误报率高）。
        # 关闭后 Mac 不做颜色检测，颜色检测已执行=0、是否存在非官方贴纸=0，其余流程不变。
        "unofficial_color": {
            "enabled": False, "mode": "brown_box",
            "brown_hue_range": (5, 30), "brown_sat_min": 30,
            "brown_val_range": (40, 200), "white_sat_max": 30,
            "white_val_min": 200, "sat_min_abs": 80,
            "val_range": (50, 240), "area_ratio": 0.05,
            "solidity_min": 0.45, "edge_grad_min": 6.0,
        },
    },
}

UNRECOGNIZED_LOB = "UNKNOWN LOB"

# ── V7：核心 5 类 vs 配件 ──────────────────────────────────────────────────────
# 走"原严格位置校验"的核心品类。凡不在此集合、且 LOB 非空的行，一律按配件处理。
STRICT_LOBS = {"iPhone", "iPad", "Mac", "Watch", "AirPods"}

# 配件（Accessory）宽松判定配置：只要检出"扫码即"锚点即判封口贴存在且合规。
# 不做位置校验（scan_sticker=None）、不做双贴校验、不做非官方贴纸颜色检测。
ACCESSORY_LOB_CONFIG = {
    "sticker_count": "single_or_dual",
    "anchor_only": True,                       # V7 核心开关：扫码即 ⟹ 合规
    "scan_sticker": None,
    "auth_sticker": None,
    "front_face_aspect_range": None,
    "unofficial_color": {"enabled": False},
}


def is_accessory_lob(lob: str) -> bool:
    """非空且非核心 5 类的 LOB 一律视为配件（走扫码即宽松判定）。"""
    return lob != UNRECOGNIZED_LOB and lob not in STRICT_LOBS


def resolve_lob_config(lob: str) -> dict:
    """核心 5 类返回其严格配置；其它一切（配件）返回宽松锚点配置。"""
    if lob in STRICT_LOBS and lob in LOB_CONFIGS:
        return LOB_CONFIGS[lob]
    return ACCESSORY_LOB_CONFIG


def _normalize_position_cfg(position_cfg) -> list[dict]:
    if position_cfg is None:
        return []
    if isinstance(position_cfg, dict):
        return [position_cfg]
    if isinstance(position_cfg, (list, tuple)):
        return [c for c in position_cfg if isinstance(c, dict)]
    return []


def detect_lob(row) -> str:
    """返回原始 LOB 字符串（保留 Beats/原厂配件/3PP 等配件原名用于输出）。
    仅当 LOB 为空/缺失时返回 UNKNOWN LOB。核心与配件的分流交给 resolve_lob_config()。
    """
    try:
        raw = row.get("LOB", None)
        if raw is None or (isinstance(raw, float) and np.isnan(raw)):
            return UNRECOGNIZED_LOB
        key = str(raw).strip()
        if not key or key.lower() in ("nan", "none"):
            return UNRECOGNIZED_LOB
        return key
    except Exception:
        pass
    return UNRECOGNIZED_LOB


# ─── PaddleOCR 初始化（延迟到 worker 进程内） ───────────────────────────────
# V4：这两个全局对象在主进程中保持 None，只在每个 worker 进程调用 init_worker_ocr()
# 后被赋值。所有检测函数仍通过模块全局 `ocr` / `_dl_executor` 引用它们 —— 与 V3 完全
# 一致，无需改动任何检测代码。
ocr = None
_dl_executor = None
# V7：跨 worker 进程共享的下载并发闸（multiprocessing.BoundedSemaphore），由主进程
# 创建后传给每个 worker，在 init_worker_ocr() 中存入本进程全局，供 download_image()
# 在发起每次 HTTP 请求前 acquire/release，实现"全局并发上限"而非"每进程各自上限"。
_download_semaphore = None
# V7.2：跨 worker 进程共享的整图 OCR 并发闸。只在大图（长边≥OCR_GATE_MIN_SIDE）
# 推理时占名额，把同时到达显存峰值的进程数卡在 --ocr-concurrency 内。
_ocr_semaphore = None


def init_worker_ocr(gpu_mem_fraction: float | None = None,
                    download_workers: int = DOWNLOAD_WORKERS,
                    download_semaphore=None,
                    ocr_semaphore=None,
                    gpu_mem_limit_mb: int = 0):
    """在 worker 进程内初始化 PaddleOCR + 下载线程池。

    必须在导入 paddle 之前设置 FLAGS 环境变量，因此 paddleocr 的 import 放在这里。
    多进程共用一块 GPU 时用 auto_growth 分配策略：每个进程只按需申请显存，
    避免某个进程一次性抢占过大比例导致其他进程 OOM。
    """
    global ocr, _dl_executor, _download_semaphore, _ocr_semaphore
    _download_semaphore = download_semaphore
    _ocr_semaphore = ocr_semaphore

    # auto_growth：多进程共享单卡的关键。每进程按需增长显存，而非预占固定比例。
    os.environ.setdefault("FLAGS_allocator_strategy", "auto_growth")
    # V7.1：限制每 worker 的 CPU 线程数。多 worker 时若各自用默认线程数（=全部核心），
    # OCR 前后处理的 CPU 阶段会互相踩踏，上下文切换开销显著。
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    if gpu_mem_fraction is not None:
        # 高级调优：显式限制每进程可用显存比例（auto_growth 下通常无需设置）
        os.environ["FLAGS_fraction_of_gpu_memory_to_use"] = str(gpu_mem_fraction)
    if gpu_mem_limit_mb and gpu_mem_limit_mb > 0:
        # V7.3：每进程显存硬上限。到达额度时 paddle 会先回收本进程空闲缓存，
        # 仍不足才 OOM（由 ocr_image_full 的清缓存+退避重试兜底）。杜绝单进程
        # 无限囤积把邻居挤到 OOM——邻居清自己的缓存救不了被本进程囤走的显存。
        os.environ["FLAGS_gpu_memory_limit_mb"] = str(int(gpu_mem_limit_mb))

    ocr = _create_ocr_pipeline()
    _dl_executor = ThreadPoolExecutor(max_workers=download_workers)


def _create_ocr_pipeline():
    from paddleocr import PaddleOCR
    return PaddleOCR(
        use_textline_orientation=True,
        lang='ch',
        device='gpu',
        enable_mkldnn=False,
        text_det_limit_side_len=OCR_DET_LIMIT_SIDE_LEN,
        text_det_limit_type=OCR_DET_LIMIT_TYPE,
    )


# （V7.4 曾在此处提供 reinit_ocr_predictor() 做进程内销毁重建，V7.5 实测无效
#   已移除——OOM 自愈改为 worker 退出 + 主进程自动重启，见 worker_main / _run_worker_round。）


# ═══════════════════════════════════════════════════════════════════════════════
# 一、图像工具
# ═══════════════════════════════════════════════════════════════════════════════

def pil_to_cv(image_pil: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(image_pil), cv2.COLOR_RGB2BGR)


def resize_for_ocr(image: Image.Image, max_side: int = 2000) -> Image.Image:
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

def download_image(url: str,
                   timeout: int = DOWNLOAD_TIMEOUT,
                   retries: int = DOWNLOAD_RETRIES,
                   backoff: float = DOWNLOAD_RETRY_BACKOFF) -> dict:
    """下载并解码单张图片。

    返回结构中保留 status/attempts，避免下载失败被误当成"没有封口贴"。
    调用方仍按 submit_row_downloads 绑定的 (col_idx, col, url, future) 顺序取回，
    不依赖并发完成顺序，因此不会发生图片列错配。

    V7：
      1. 若设置了全局下载并发闸 `_download_semaphore`（跨所有 worker 进程共享），
         只包住实际的 `requests.get()` 网络请求，把同时在飞的下载请求数摁在
         --download-concurrency 设定的上限内，避免 --workers × --download-workers
         的乘积压垮图片服务器/CDN。退避等待和 PIL 解码不占用并发名额。
      2. 校验响应 Content-Length 与实际收到字节数：部分 CDN 在异常情况下会返回
         200 但内容被截断而不报错，直接喂给 PIL 有时仍能"看似"解码成功但内容不
         完整。这里提前识别为失败并重试，而不是让残缺图片进入 OCR。
    """
    if pd.isna(url) or url == '':
        return {"ok": False, "image": None, "status": "empty_url", "attempts": 0}

    last_status = "unknown"
    max_attempts = max(1, int(retries) + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            if _download_semaphore is not None:
                _download_semaphore.acquire()
            try:
                response = requests.get(url, timeout=timeout)
            finally:
                if _download_semaphore is not None:
                    _download_semaphore.release()

            status_code = response.status_code
            if status_code == 200:
                content = response.content
                expected_len_hdr = response.headers.get('Content-Length')
                if expected_len_hdr is not None:
                    try:
                        expected_len = int(expected_len_hdr)
                        if expected_len > 0 and len(content) < expected_len:
                            raise IOError(
                                f"下载不完整：收到 {len(content)}/{expected_len} 字节"
                            )
                    except ValueError:
                        pass  # Content-Length 非法值，跳过校验，走后续 PIL 解码兜底
                image = Image.open(BytesIO(content))
                image.load()
                return {
                    "ok": True,
                    "image": image.convert("RGB"),
                    "status": "ok",
                    "attempts": attempt,
                }
            last_status = f"http_{status_code}"
            print(f"  下载失败 attempt {attempt}/{max_attempts} "
                  f"(状态码 {status_code}): {url}")
        except Exception as e:
            last_status = f"{type(e).__name__}: {str(e)[:80]}"
            print(f"  下载异常 attempt {attempt}/{max_attempts}: {url}, 错误: {last_status}")

        if attempt < max_attempts:
            time.sleep(backoff * attempt)

    return {
        "ok": False,
        "image": None,
        "status": last_status,
        "attempts": max_attempts,
    }


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
# 三、OCR
# ═══════════════════════════════════════════════════════════════════════════════

def _is_gpu_oom_error(e: Exception) -> bool:
    """paddle 的显存不足没有稳定的 Python 异常类型（常包装成 MemoryError），按消息识别。"""
    msg = repr(e)
    return isinstance(e, MemoryError) or "ResourceExhausted" in msg or "Out of memory" in msg


def _release_gpu_cache() -> None:
    """把本进程 auto_growth 分配器囤积的空闲显存块归还给 CUDA 驱动。

    多 worker 共享单卡时每个进程的缓存池只增不还：0803 批次跑数小时后，
    32GB 卡只剩 68MB 可分配（12 进程合计囤 31GB、实际在用不到 2GB），
    3000px 整图 OCR 的大块激活分配集体 OOM，而小裁剪图仍能挤进碎片。
    worker 定期主动归还即可根治挤兑。
    """
    try:
        import paddle
        paddle.device.cuda.empty_cache()
    except Exception as e:
        print(f"  ⚠ 释放显存缓存失败(忽略): {type(e).__name__}: {str(e)[:80]}")


def ocr_image_full(image: Image.Image, image_id: str = "unknown",
                   _oom_attempt: int = 0):
    if image is None:
        return "", [], [], 0, 0
    try:
        orig_w, orig_h = image.size
        image_resized = resize_for_ocr(image, max_side=OCR_MAX_SIDE)
        res_w, res_h = image_resized.size
        img_cv = pil_to_cv(image_resized)

        # V7.2：大图推理占用跨进程并发闸，防止多个进程同时到达显存峰值互相挤兑；
        # 裁剪小图/水印（长边 < OCR_GATE_MIN_SIDE）不占名额，不受影响。
        use_gate = (_ocr_semaphore is not None
                    and max(res_w, res_h) >= OCR_GATE_MIN_SIDE)
        if use_gate:
            _ocr_semaphore.acquire()
        try:
            result = ocr.predict(input=img_cv)
        finally:
            if use_gate:
                _ocr_semaphore.release()

        texts, polys_res = [], []
        if result and len(result) > 0:
            ocr_result = result[0]
            if hasattr(ocr_result, 'json'):
                res = ocr_result.json.get('res', {})
                texts = res.get('rec_texts', [])
                polys_res = res.get('dt_polys', res.get('boxes', []))

        sx = orig_w / res_w if res_w > 0 else 1.0
        sy = orig_h / res_h if res_h > 0 else 1.0
        polys_orig = [
            [[pt[0] * sx, pt[1] * sy] for pt in poly]
            for poly in polys_res
        ]
        return " ".join(texts), texts, polys_orig, orig_h, orig_w

    except Exception as e:
        if _is_gpu_oom_error(e):
            if _oom_attempt < OCR_OOM_MAX_RETRIES:
                wait_s = 0.5 * (2 ** _oom_attempt)  # 0.5s → 1s → 2s
                print(f"  OCR 显存不足({image_id})，清缓存并等待 {wait_s:.1f}s 后重试"
                      f"（第{_oom_attempt + 1}/{OCR_OOM_MAX_RETRIES}次）…")
                _release_gpu_cache()
                time.sleep(wait_s)
                return ocr_image_full(image, image_id, _oom_attempt + 1)
            # 退避重试后仍 OOM：向上抛出，让该行被记为 ERROR（重跑可续传）。
            # 绝不能静默返回空文本——那会被误判成"图上没字/没有封口贴"，
            # 0803 批次约 65% 的行就是这样被污染的。
            raise RuntimeError(
                f"GPU显存不足，OCR失败（已退避重试{OCR_OOM_MAX_RETRIES}次）: "
                f"{image_id}") from e
        print("  OCR 识别异常:", type(e).__name__, repr(e))
        print(traceback.format_exc())
        return "", [], [], 0, 0


def rotate_image_for_scan_ocr(image: Image.Image, angle: int) -> Image.Image:
    """返回用于 OCR 的旋转图；angle=0 时保持原图对象，避免无谓拷贝。"""
    if angle == 0:
        return image
    return image.rotate(angle, expand=True)


def scan_ocr_angles_for_lob(lob: str) -> tuple[int, ...]:
    # 配件包装小、拍摄方向不定，与 Watch/AirPods 同样启用多角度 OCR 提升"扫码即"召回。
    if lob in SCAN_ORIENTATION_FALLBACK_LOBS or is_accessory_lob(lob):
        return SCAN_OCR_ANGLES
    return (0,)


def offset_polys(polys: list, dx: float, dy: float) -> list:
    out = []
    for poly in polys or []:
        try:
            out.append([[float(pt[0]) + dx, float(pt[1]) + dy] for pt in poly])
        except Exception:
            continue
    return out


def _box_iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / max(area_a + area_b - inter, 1)


def find_purple_scan_candidate_boxes(image_pil: Image.Image,
                                     max_candidates: int | None = None) -> list[tuple[int, int, int, int]]:
    """找紫色官方封贴条带附近的 crop，用于局部放大 OCR。

    注意：紫色只作为候选，不直接代表"封口贴存在"。最终仍必须 OCR 命中
    "扫码即领"锚点，避免把官方印刷紫色条带误当作封贴。

    max_candidates=None 时在调用时读取全局配置——不能把全局当默认参数值，
    那是在函数定义时求值的，worker 里改全局（--scan-crop-* 旋钮）不会生效。
    """
    if max_candidates is None:
        max_candidates = SCAN_LOCAL_CROP_MAX_CANDIDATES
    try:
        img_cv = pil_to_cv(image_pil)
        H, W = img_cv.shape[:2]
        if H < 80 or W < 80:
            return []

        hsv = cv2.cvtColor(img_cv, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array(SCAN_PURPLE_HSV_LOW, dtype=np.uint8),
            np.array(SCAN_PURPLE_HSV_HIGH, dtype=np.uint8),
        )
        k3 = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k3, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k3, iterations=2)

        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        img_area = max(1, H * W)
        candidates = []

        for label in range(1, n_labels):
            area = int(stats[label, cv2.CC_STAT_AREA])
            x = int(stats[label, cv2.CC_STAT_LEFT])
            y = int(stats[label, cv2.CC_STAT_TOP])
            w = int(stats[label, cv2.CC_STAT_WIDTH])
            h = int(stats[label, cv2.CC_STAT_HEIGHT])
            if area < max(35, int(img_area * 0.00003)):
                continue
            if min(w, h) < 3 or max(w, h) < 18:
                continue
            elongation = max(w, h) / max(min(w, h), 1)
            if elongation < 1.8:
                continue

            if w >= h:
                xpad = max(int(w * 0.70), int(W * 0.04), 80)
                ypad = max(int(h * 9.0), int(H * 0.06), 100)
            else:
                xpad = max(int(w * 9.0), int(W * 0.06), 100)
                ypad = max(int(h * 0.70), int(H * 0.04), 80)

            box = (
                max(0, x - xpad),
                max(0, y - ypad),
                min(W, x + w + xpad),
                min(H, y + h + ypad),
            )
            box_area = (box[2] - box[0]) * (box[3] - box[1])
            if box_area < 400:
                continue
            candidates.append((area, box_area, box))

        # 优先面积较大的紫色条带，同时做简单 NMS，避免同一贴纸重复 OCR 太多次。
        candidates.sort(key=lambda t: (t[0], -t[1]), reverse=True)
        out = []
        for _, _, box in candidates:
            if any(_box_iou(box, e) > 0.55 for e in out):
                continue
            out.append(box)
            if len(out) >= max_candidates:
                break
        return out
    except Exception as e:
        print(f"  紫色封贴候选检测异常: {type(e).__name__}: {e}")
        return []


def upscale_crop_for_ocr(crop: Image.Image,
                         target_long_side: int | None = None
                         ) -> tuple[Image.Image, float]:
    """把小 crop 放大到 target_long_side 长边，返回 (放大后图, 放大倍数)。

    V7.7：只放大、不缩小（更大的 crop 交给 ocr_image_full 里的 resize_for_ocr
    按 OCR_MAX_SIDE 处理，行为与此前一致，不引入回归）。

    target_long_side=None 时在调用时读取全局（见 find_purple_scan_candidate_boxes
    的同类说明）；<=0 表示关闭放大，退回 V7.6 及以前的行为。
    """
    if target_long_side is None:
        target_long_side = SCAN_CROP_UPSCALE_TO
    w, h = crop.size
    long_side = max(w, h)
    if target_long_side <= 0 or long_side <= 0 or long_side >= target_long_side:
        return crop, 1.0
    scale = target_long_side / long_side
    resized = crop.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                          resample=Image.LANCZOS)
    return resized, scale


def ocr_scan_candidate_crops(image: Image.Image,
                             base_texts: list[str],
                             base_polys: list,
                             image_id: str,
                             max_candidates: int | None = None,
                             lob: str | None = None):
    """对紫色候选 crop 做 OCR，命中扫码锚点时把 crop OCR 结果映射回整图坐标。

    V7.7.2：是否放大 crop 由 lob 决定（见 SCAN_CROP_UPSCALE_LOBS）——只有盒大、
    拍得远、贴纸占像素少的 LOB 才需要放大；Watch/AirPods/配件盒小距离近，
    不放大也能读到，放大只是白付算力。lob=None 时不放大（保守）。
    """
    boxes = find_purple_scan_candidate_boxes(image, max_candidates=max_candidates)
    if not boxes:
        return None

    upscale_to = SCAN_CROP_UPSCALE_TO if lob in SCAN_CROP_UPSCALE_LOBS else 0

    for ci, (x1, y1, x2, y2) in enumerate(boxes, 1):
        crop = image.crop((x1, y1, x2, y2))
        crop_in, up_scale = upscale_crop_for_ocr(crop, upscale_to)
        full_text, texts, polys, orig_h, orig_w = ocr_image_full(
            crop_in, f"{image_id}_purple{ci}")
        if has_scan_text(texts):
            # ocr_image_full 返回的 poly 已映射回"它收到的那张图"的尺寸，即放大后的
            # crop 坐标系；必须先按放大倍数除回原始 crop 尺度，再偏移到整图坐标。
            # 漏掉这一步会让贴纸位置整体外扩 up_scale 倍 → rel_x/rel_y 越界 →
            # 位置校验误判（Watch/AirPods 尤其明显，它们的规范区间比 Mac 严格）。
            if up_scale != 1.0:
                polys = [[[pt[0] / up_scale, pt[1] / up_scale] for pt in poly]
                         for poly in polys]
            mapped_polys = offset_polys(polys, x1, y1)
            return {
                "texts": list(base_texts or []) + list(texts or []),
                "polys_orig": list(base_polys or []) + mapped_polys,
                "orig_h": image.size[1],
                "orig_w": image.size[0],
                "crop_box": (x1, y1, x2, y2),
                "crop_text": full_text,
                "candidate_count": len(boxes),
            }
    return {"candidate_count": len(boxes)}


# ═══════════════════════════════════════════════════════════════════════════════
# 四、包装盒检测与透视矫正
# ═══════════════════════════════════════════════════════════════════════════════

def detect_box_bbox(image_pil: Image.Image, lob: str | None = None):
    img_cv = pil_to_cv(image_pil)
    H, W = img_cv.shape[:2]

    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)

    MIN_AREA_RATIO = 0.08
    MAX_ASPECT     = 5.0

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
                int(bw / scale), int(bh / scale), 'edge'
            )

    if lob == "Mac":
        hsv = cv2.cvtColor(img_small, cv2.COLOR_BGR2HSV)
        thresh = cv2.inRange(hsv, np.array(BROWN_HSV_LOW, dtype=np.uint8),
                                   np.array(BROWN_HSV_HIGH, dtype=np.uint8))
        method_name = 'brown'
        k = np.ones((7, 7), np.uint8)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k, iterations=2)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN,  k, iterations=1)
    else:
        _, thresh = cv2.threshold(filtered, 190, 255, cv2.THRESH_BINARY)
        method_name = 'bright'

    border = max(5, int(min(dW, dH) * 0.01))
    thresh[:border, :]  = 0; thresh[-border:, :] = 0
    thresh[:, :border]  = 0; thresh[:, -border:] = 0

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
                int(bw / scale), int(bh / scale), method_name
            )

    return 0, 0, W, H, 'fallback'


def _order_quad_corners(pts: np.ndarray) -> np.ndarray:
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = pts[:, 0] - pts[:, 1]
    rect = np.zeros((4, 2), dtype=np.float32)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    rect[1] = pts[np.argmax(d)]
    rect[3] = pts[np.argmin(d)]
    return rect


_QUAD_MAX_ASPECT = 8.0
_QUAD_MIN_EDGE   = 10


def _quad_from_contour(cnt: np.ndarray) -> np.ndarray | None:
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
            if max(w_hat, h_hat) / max(min(w_hat, h_hat), 1.0) > _QUAD_MAX_ASPECT:
                return None
            return cand
        if len(approx) > 4:
            continue
        if len(approx) < 4:
            break
    try:
        rect = cv2.minAreaRect(cnt)
        cw, ch = rect[1]
        if min(cw, ch) >= _QUAD_MIN_EDGE:
            if max(cw, ch) / max(min(cw, ch), 1.0) <= _QUAD_MAX_ASPECT:
                return cv2.boxPoints(rect).astype(np.float32)
    except Exception:
        return None
    return None


def _find_quads_canny(img_cv, max_candidates=8, min_area_ratio=0.03):
    H, W = img_cv.shape[:2]
    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)
    edges = cv2.Canny(filtered, 20, 80)
    dilated = cv2.dilate(edges, np.ones((9, 9), np.uint8), iterations=3)
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    quads = []
    for cnt in contours[:25]:
        if cv2.contourArea(cnt) < min_area_ratio * dW * dH:
            continue
        q = _quad_from_contour(cnt)
        if q is not None:
            quads.append((q / scale).astype(np.float32))
        if len(quads) >= max_candidates:
            break
    return quads


def _find_quads_brown_split(img_cv, max_candidates=8, min_area_ratio=0.03):
    H, W = img_cv.shape[:2]
    scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H, W))
    dW, dH = max(1, int(W * scale)), max(1, int(H * scale))
    img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)

    hsv = cv2.cvtColor(img_small, cv2.COLOR_BGR2HSV)
    brown_mask = cv2.inRange(hsv, np.array(BROWN_HSV_LOW, dtype=np.uint8),
                                  np.array(BROWN_HSV_HIGH, dtype=np.uint8))
    border = max(5, int(min(dW, dH) * 0.01))
    brown_mask[:border, :]  = 0; brown_mask[-border:, :] = 0
    brown_mask[:, :border]  = 0; brown_mask[:, -border:] = 0
    k7 = np.ones((7, 7), np.uint8)
    brown_mask = cv2.morphologyEx(brown_mask, cv2.MORPH_CLOSE, k7, iterations=2)
    brown_mask = cv2.morphologyEx(brown_mask, cv2.MORPH_OPEN,  k7, iterations=1)

    contours, _ = cv2.findContours(brown_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    box_outline = max(contours, key=cv2.contourArea)
    if cv2.contourArea(box_outline) < min_area_ratio * dW * dH:
        return []

    box_mask = np.zeros((dH, dW), dtype=np.uint8)
    cv2.drawContours(box_mask, [box_outline], -1, 255, -1)

    gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)
    inner_edges = cv2.Canny(filtered, 5, 25)
    inner_edges[box_mask == 0] = 0
    edges_dil = cv2.dilate(inner_edges, np.ones((5, 5), np.uint8), iterations=2)
    sub_mask = cv2.bitwise_and(box_mask, cv2.bitwise_not(edges_dil))

    sub_cnts, _ = cv2.findContours(sub_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    sub_cnts = sorted(sub_cnts, key=cv2.contourArea, reverse=True)

    quads = []
    for cnt in sub_cnts[:15]:
        if cv2.contourArea(cnt) < min_area_ratio * dW * dH:
            continue
        q = _quad_from_contour(cnt)
        if q is not None:
            quads.append((q / scale).astype(np.float32))
        if len(quads) >= max_candidates:
            break
    return quads


def _dedup_quads(quads, center_threshold=50.0):
    out = []
    for q in quads:
        cx, cy = float(q[:, 0].mean()), float(q[:, 1].mean())
        if not any(
            (cx - float(q2[:, 0].mean()))**2 + (cy - float(q2[:, 1].mean()))**2
            < center_threshold**2
            for q2 in out
        ):
            out.append(q)
    return out


def _quad_size_aspect(quad):
    ordered = _order_quad_corners(quad)
    tl, tr, br, bl = ordered
    w = max(float(np.linalg.norm(tr - tl)), float(np.linalg.norm(br - bl)))
    h = max(float(np.linalg.norm(bl - tl)), float(np.linalg.norm(br - tr)))
    return w, h, max(w, h) / max(min(w, h), 1.0)


def _score_quad(quad, scan_centers_orig, aspect_range, img_area):
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
            aspect_score = max(0.0, 1.0 - min(abs(aspect - lo), abs(aspect - hi)) * 0.6)

    area_score = min(1.0, (cv2.contourArea(quad) / max(img_area, 1.0)) ** 0.5)
    return contain_score + aspect_score + 0.15 * area_score


def _find_box_quads(img_cv, max_candidates=8, min_area_ratio=0.03, lob=None):
    quads = _find_quads_canny(img_cv, max_candidates, min_area_ratio)
    if lob == "Mac":
        quads = quads + _find_quads_brown_split(img_cv, max_candidates, min_area_ratio)
    quads = _dedup_quads(quads)
    quads.sort(key=lambda q: cv2.contourArea(q), reverse=True)
    return quads[:max_candidates]


def rectify_package_box(image_pil: Image.Image, lob=None, scan_polys_orig=None) -> dict:
    img_cv = pil_to_cv(image_pil)
    H_img, W_img = img_cv.shape[:2]

    # 策略 1：多候选四边形 + 正面打分透视矫正
    quads = _find_box_quads(img_cv, lob=lob)
    quad = None
    if quads:
        aspect_range = None
        if lob and lob in LOB_CONFIGS:
            aspect_range = LOB_CONFIGS[lob].get("front_face_aspect_range")

        scan_centers = []
        if scan_polys_orig:
            for poly in scan_polys_orig:
                try:
                    pts = np.array(poly, dtype=float)
                    scan_centers.append((float(pts[:, 0].mean()), float(pts[:, 1].mean())))
                except Exception:
                    continue

        quad = max(quads, key=lambda q: _score_quad(q, scan_centers, aspect_range, float(W_img * H_img)))

    if quad is not None:
        try:
            ordered = _order_quad_corners(quad)
            tl, tr, br, bl = ordered
            W_rect = max(1, int(round(max(
                float(np.linalg.norm(tr - tl)), float(np.linalg.norm(br - bl))
            ))))
            H_rect = max(1, int(round(max(
                float(np.linalg.norm(bl - tl)), float(np.linalg.norm(br - tr))
            ))))
            clip_scale = min(1.0, BOX_DETECT_MAX_SIDE / max(W_rect, H_rect))
            if clip_scale < 1.0:
                W_rect = max(1, int(round(W_rect * clip_scale)))
                H_rect = max(1, int(round(H_rect * clip_scale)))

            dst = np.array([[0, 0], [W_rect - 1, 0],
                            [W_rect - 1, H_rect - 1], [0, H_rect - 1]], dtype=np.float32)
            M = cv2.getPerspectiveTransform(ordered, dst)
            warped = cv2.warpPerspective(img_cv, M, (W_rect, H_rect))

            xs, ys = ordered[:, 0], ordered[:, 1]
            box_x = int(max(0, np.floor(xs.min())))
            box_y = int(max(0, np.floor(ys.min())))
            box_w = int(min(W_img - box_x, np.ceil(xs.max()) - box_x))
            box_h = int(min(H_img - box_y, np.ceil(ys.max()) - box_y))

            return {"warped_img": warped, "M": M, "W_rect": W_rect, "H_rect": H_rect,
                    "method": "perspective", "box_quad_src": ordered.tolist(),
                    "box_x": box_x, "box_y": box_y, "box_w": box_w, "box_h": box_h}
        except Exception as e:
            print(f"  ⚠ 透视矫正异常，降级: {type(e).__name__}: {e}")

    # 策略 2：minAreaRect 旋转矫正
    try:
        scale = min(1.0, BOX_DETECT_MAX_SIDE / max(H_img, W_img))
        dW, dH = max(1, int(W_img * scale)), max(1, int(H_img * scale))
        img_small = cv2.resize(img_cv, (dW, dH), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
        filtered = cv2.bilateralFilter(gray, 9, 75, 75)
        edges = cv2.Canny(filtered, 20, 80)
        dilated = cv2.dilate(edges, np.ones((9, 9), np.uint8), iterations=3)
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            cnt = max(contours, key=cv2.contourArea)
            if cv2.contourArea(cnt) >= 0.08 * dW * dH:
                rect = cv2.minAreaRect(cnt)
                box_pts_orig = cv2.boxPoints(rect).astype(np.float32) / scale
                ordered = _order_quad_corners(box_pts_orig)
                tl, tr, br, bl = ordered
                W_rect = max(1, int(round(max(
                    float(np.linalg.norm(tr - tl)), float(np.linalg.norm(br - bl))
                ))))
                H_rect = max(1, int(round(max(
                    float(np.linalg.norm(bl - tl)), float(np.linalg.norm(br - tr))
                ))))
                clip_scale = min(1.0, BOX_DETECT_MAX_SIDE / max(W_rect, H_rect))
                if clip_scale < 1.0:
                    W_rect = max(1, int(round(W_rect * clip_scale)))
                    H_rect = max(1, int(round(H_rect * clip_scale)))
                dst = np.array([[0, 0], [W_rect - 1, 0],
                                [W_rect - 1, H_rect - 1], [0, H_rect - 1]], dtype=np.float32)
                M = cv2.getPerspectiveTransform(ordered, dst)
                warped = cv2.warpPerspective(img_cv, M, (W_rect, H_rect))
                xs, ys = ordered[:, 0], ordered[:, 1]
                box_x = int(max(0, np.floor(xs.min())))
                box_y = int(max(0, np.floor(ys.min())))
                return {"warped_img": warped, "M": M, "W_rect": W_rect, "H_rect": H_rect,
                        "method": "rotation", "box_quad_src": ordered.tolist(),
                        "box_x": box_x, "box_y": box_y,
                        "box_w": int(min(W_img - box_x, np.ceil(xs.max()) - box_x)),
                        "box_h": int(min(H_img - box_y, np.ceil(ys.max()) - box_y))}
    except Exception as e:
        print(f"  ⚠ 旋转矫正异常，降级: {type(e).__name__}: {e}")

    # 策略 3：轴对齐 bbox 兜底（V3 不跳过，继续处理）
    bx, by, bw, bh, _ = detect_box_bbox(image_pil, lob=lob)
    warped = img_cv[by:by + bh, bx:bx + bw].copy()
    return {"warped_img": warped if warped.size > 0 else img_cv,
            "M": None, "W_rect": bw, "H_rect": bh,
            "method": "axis_aligned", "box_quad_src": None,
            "box_x": bx, "box_y": by, "box_w": bw, "box_h": bh}


def transform_polys(polys_orig: list, M, box_x: int = 0, box_y: int = 0) -> list:
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
            out.append([[float(pt[0]) - box_x, float(pt[1]) - box_y] for pt in poly])
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 五、非官方贴纸颜色检测
# ═══════════════════════════════════════════════════════════════════════════════

def _filter_color_candidates(candidate_mask, signal_u8, zone_area, color_cfg, detail_prefix):
    area_ratio_min = float(color_cfg.get("area_ratio", 0.015))
    solidity_min   = float(color_cfg.get("solidity_min", 0.45))
    edge_grad_min  = float(color_cfg.get("edge_grad_min", 6.0))

    k5 = np.ones((5, 5), np.uint8)
    mask = cv2.dilate(candidate_mask, k5, iterations=2)
    mask = cv2.erode(mask, k5, iterations=2)

    n_labels, label_img, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    gx = cv2.Sobel(signal_u8, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(signal_u8, cv2.CV_32F, 0, 1, ksize=3)
    grad_mag = np.sqrt(gx**2 + gy**2)

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
        if cv2.contourArea(cnt) / hull_area < solidity_min:
            continue
        k3 = np.ones((3, 3), np.uint8)
        boundary = (cv2.dilate(comp_mask, k3) - cv2.erode(comp_mask, k3)).astype(bool)
        mean_edge_grad = float(grad_mag[boundary].mean()) if boundary.any() else 0.0
        if mean_edge_grad < edge_grad_min:
            continue
        return True, (f"{detail_prefix}：面积占比 {ratio:.1%}，"
                      f"紧实度 {cv2.contourArea(cnt)/hull_area:.2f}，"
                      f"边缘梯度 {mean_edge_grad:.1f}")
    return False, ""


def _detect_unofficial_white_box(zone, color_cfg):
    """
    白盒分支：白平衡归一化 + 相对饱和度。

    改进（V3.1）：白色背景像素不足时跳过检测，防止拍摄条件差/透视矫正
    偏差时无法建立白平衡基准所产生的误判。
    """
    sat_above_bg = float(color_cfg.get("sat_above_bg", UNOFFICIAL_SAT_ABOVE_BG))
    v_min, v_max = color_cfg.get("val_range", UNOFFICIAL_VAL_RANGE)

    hsv_f = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV).astype(np.float32)
    s_raw = hsv_f[:, :, 1]
    v_raw = hsv_f[:, :, 2]

    white_mask = (v_raw > 180) & (s_raw < 55)
    white_count = int(white_mask.sum())

    # 白色背景像素不足 → 无法可靠建立白平衡基准，跳过
    if white_count < UNOFFICIAL_WHITE_BG_MIN:
        return False, f"白色背景像素不足({white_count}px)，跳过颜色检测"

    bg_sat_ref = float(np.percentile(s_raw[white_mask], 90))
    eff_sat = np.clip(s_raw - bg_sat_ref - 10.0, 0.0, 255.0)

    candidate = ((eff_sat > sat_above_bg) & (v_raw > v_min) & (v_raw < v_max)).astype(np.uint8) * 255
    eff_sat_u8 = np.clip(eff_sat, 0, 255).astype(np.uint8)
    return _filter_color_candidates(candidate, eff_sat_u8, zone.shape[0] * zone.shape[1],
                                    color_cfg, f"[white_box] 非白色彩色区域 (bg_ref S={bg_sat_ref:.1f})")


def _detect_unofficial_brown_box(zone, color_cfg):
    brown_h_lo, brown_h_hi = color_cfg.get("brown_hue_range", (5, 30))
    brown_sat_min = float(color_cfg.get("brown_sat_min", 30))
    brown_v_lo, brown_v_hi = color_cfg.get("brown_val_range", (40, 200))
    white_sat_max = float(color_cfg.get("white_sat_max", 30))
    white_val_min = float(color_cfg.get("white_val_min", 200))
    sat_min_abs = float(color_cfg.get("sat_min_abs", 80))
    v_min, v_max = color_cfg.get("val_range", (50, 240))

    hsv_f = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV).astype(np.float32)
    h_raw, s_raw, v_raw = hsv_f[:, :, 0], hsv_f[:, :, 1], hsv_f[:, :, 2]

    brown_mask = ((h_raw >= brown_h_lo) & (h_raw <= brown_h_hi) &
                  (s_raw >= brown_sat_min) & (v_raw >= brown_v_lo) & (v_raw <= brown_v_hi))
    white_mask = (s_raw <= white_sat_max) & (v_raw >= white_val_min)
    foreign = (~brown_mask & ~white_mask & (s_raw >= sat_min_abs) &
               (v_raw >= v_min) & (v_raw <= v_max)).astype(np.uint8) * 255

    s_u8 = np.clip(s_raw, 0, 255).astype(np.uint8)
    return _filter_color_candidates(foreign, s_u8, zone.shape[0] * zone.shape[1],
                                    color_cfg, "[brown_box] 非棕非白高饱和异色区域")


def detect_unofficial_sticker_color(warped_img, color_cfg) -> tuple[bool, str]:
    """
    非官方贴纸颜色检测（全图扫描）。

    对整个矫正后的包装盒面做颜色分析，通过 color_cfg 中的 area_ratio
    阈值来过滤盒面官方印刷元素（如 AirPods 紫色授权条带 ~9%、绿色回收
    箭头等）。各 LOB 根据自身盒面印刷特点配置不同的 area_ratio：
      • iPhone / Watch / iPad / Accy.：0.020（印刷元素少，对小贴纸敏感）
      • AirPods：0.120（官方印刷彩色元素可达 ~10%，阈值需高于此值）
    """
    if not color_cfg or not color_cfg.get("enabled", False):
        return False, "颜色检测已跳过"
    try:
        if warped_img is None or warped_img.size == 0:
            return False, ""
        H, W = warped_img.shape[:2]
        if H < 10 or W < 10:
            return False, ""
        mx, my = max(1, int(W * 0.02)), max(1, int(H * 0.02))
        zone = warped_img[my:H - my, mx:W - mx]
        if zone.shape[0] < 10 or zone.shape[1] < 10:
            return False, ""
        if color_cfg.get("mode") == "brown_box":
            return _detect_unofficial_brown_box(zone, color_cfg)
        return _detect_unofficial_white_box(zone, color_cfg)
    except Exception as e:
        return False, f"颜色检测异常: {type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# 六、贴纸定位与位置验证
# ═══════════════════════════════════════════════════════════════════════════════

# 扫码贴锚点：原为精确匹配"扫码即领"，但 OCR 常把"领"认错（锁/顿）或把四字切断，
# 导致整条漏检 → 找不到背面图 → 封口贴误判为 0（Watch/AirPods 小字/旋转尤为频繁）。
# V5 保留"扫码即"主锚点，并允许"扫码"后近邻窗口中出现"即/领"。
SCAN_ANCHOR = "扫码即"
SCAN_NEGATIVE_ANCHORS = ("扫码支付", "扫码登录", "扫码登陆")


def normalize_scan_text(text: str) -> str:
    return re.sub(r"[\s　|｜:：,，.。;；\-—_]+", "", str(text or ""))


def is_scan_text(text: str) -> bool:
    t = normalize_scan_text(text)
    if not t or any(neg in t for neg in SCAN_NEGATIVE_ANCHORS):
        return False
    if SCAN_ANCHOR in t or "扫码即领" in t:
        return True
    pos = t.find("扫码")
    if pos < 0:
        return False
    window = t[pos:pos + 8]
    return ("即" in window or "领" in window) and "支付" not in window


def has_scan_text(texts: list[str]) -> bool:
    if any(is_scan_text(t) for t in texts or []):
        return True
    joined = "".join(normalize_scan_text(t) for t in texts or [])
    return is_scan_text(joined)


def find_all_scan_stickers(texts: list[str], polys: list) -> list[dict]:
    stickers = []
    for i, text in enumerate(texts):
        if is_scan_text(text) and i < len(polys):
            try:
                poly = np.array(polys[i], dtype=float)
                x1, y1 = float(poly[:, 0].min()), float(poly[:, 1].min())
                x2, y2 = float(poly[:, 0].max()), float(poly[:, 1].max())
                stickers.append({
                    "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "text_idx": i,
                })
            except Exception:
                continue
    if stickers or not has_scan_text(texts):
        return stickers

    # OCR 有时把"扫码即领"拆成"扫码"和"即领"两个框；此时用"扫码"框作为锚点。
    for i, text in enumerate(texts):
        t = normalize_scan_text(text)
        if "扫码" in t and not any(neg in t for neg in SCAN_NEGATIVE_ANCHORS) and i < len(polys):
            try:
                poly = np.array(polys[i], dtype=float)
                x1, y1 = float(poly[:, 0].min()), float(poly[:, 1].min())
                x2, y2 = float(poly[:, 0].max()), float(poly[:, 1].max())
                stickers.append({
                    "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "text_idx": i,
                })
            except Exception:
                continue
    return stickers


def _scan_sticker_distance_to_zone(rel_x, rel_y, scan_position_cfg) -> float:
    zones = _normalize_position_cfg(scan_position_cfg)
    if not zones:
        return float("inf")
    best = float("inf")
    for zone in zones:
        x_min = zone.get("x_min", -float("inf"))
        x_max = zone.get("x_max",  float("inf"))
        y_min = zone.get("y_min", -float("inf"))
        y_max = zone.get("y_max",  float("inf"))
        dx = max(0.0, x_min - rel_x, rel_x - x_max)
        dy = max(0.0, y_min - rel_y, rel_y - y_max)
        d = (dx * dx + dy * dy) ** 0.5
        if d < best:
            best = d
    return best


def pick_best_scan_sticker(scan_stickers, W_rect, H_rect, scan_position_cfg):
    if not scan_stickers:
        return None
    if scan_position_cfg is None or W_rect <= 0 or H_rect <= 0:
        return scan_stickers[0]
    def _key(s):
        rel_x = s["cx"] / W_rect
        rel_y = s["cy"] / H_rect
        return (_scan_sticker_distance_to_zone(rel_x, rel_y, scan_position_cfg), rel_y)
    return min(scan_stickers, key=_key)


def validate_sticker_position(rel_cx, rel_cy, position_cfg,
                               tolerance: float = STICKER_POSITION_TOLERANCE,
                               unit_bounds_margin: float | None = None) -> dict:
    zones = _normalize_position_cfg(position_cfg)
    if not zones:
        return {"in_correct_position": False,
                "rel_x": round(rel_cx, 4) if rel_cx is not None else None,
                "rel_y": round(rel_cy, 4) if rel_cy is not None else None,
                "x_ok": False, "y_ok": False,
                "detail": "位置验证跳过（该 LOB 未配置规范位置）"}

    tol = max(float(tolerance), 0.0)

    if unit_bounds_margin is not None:
        margin = max(float(unit_bounds_margin), 0.0)
        if not (-margin <= rel_cx <= 1.0 + margin and -margin <= rel_cy <= 1.0 + margin):
            return {"in_correct_position": False,
                    "rel_x": round(rel_cx, 4), "rel_y": round(rel_cy, 4),
                    "x_ok": -margin <= rel_cx <= 1.0 + margin,
                    "y_ok": -margin <= rel_cy <= 1.0 + margin,
                    "detail": (f"位置异常：相对坐标超出盒面硬边界 "
                               f"(rel_x={rel_cx:.3f}, rel_y={rel_cy:.3f}, "
                               f"允许范围=[-{margin:.2f},{1.0 + margin:.2f}])")}

    def _check_zone(zone):
        x_min = zone.get("x_min", -float("inf"))
        x_max = zone.get("x_max",  float("inf"))
        y_min = zone.get("y_min", -float("inf"))
        y_max = zone.get("y_max",  float("inf"))
        x_lo = x_min - tol if x_min != -float("inf") else x_min
        x_hi = x_max + tol if x_max !=  float("inf") else x_max
        y_lo = y_min - tol if y_min != -float("inf") else y_min
        y_hi = y_max + tol if y_max !=  float("inf") else y_max
        return (x_lo <= rel_cx <= x_hi), (y_lo <= rel_cy <= y_hi), (x_min, x_max, y_min, y_max)

    best_fail = None
    for zone in zones:
        x_ok, y_ok, bounds = _check_zone(zone)
        if x_ok and y_ok:
            return {"in_correct_position": True,
                    "rel_x": round(rel_cx, 4), "rel_y": round(rel_cy, 4),
                    "x_ok": True, "y_ok": True,
                    "detail": f"位置规范 (rel_x={rel_cx:.3f}, rel_y={rel_cy:.3f}, 容差±{tol:.2f})"}
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
    return {"in_correct_position": False,
            "rel_x": round(rel_cx, 4), "rel_y": round(rel_cy, 4),
            "x_ok": x_ok, "y_ok": y_ok,
            "detail": f"位置异常{multi}：" + "；".join(parts)}


def find_all_auth_stickers_in_box(texts, polys_rect, W_rect=0, H_rect=0):
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
                if use_box_filter and not (0 <= cx <= W_rect and 0 <= cy <= H_rect):
                    continue
                results.append({"cx": cx, "cy": cy,
                                 "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                 "text_idx": i, "matched_text": text})
            except Exception:
                continue
    return results


def check_auth_sticker_position(texts, polys_rect, W_rect, H_rect, position_cfg) -> dict:
    if position_cfg is None:
        return {"found": False, "in_correct_position": False,
                "rel_x": None, "rel_y": None, "detail": "该 LOB 无二贴规范"}

    candidates = find_all_auth_stickers_in_box(texts, polys_rect, W_rect, H_rect)
    if not candidates:
        return {"found": False, "in_correct_position": False,
                "rel_x": None, "rel_y": None,
                "detail": "未找到'Apple授权专营店'贴纸（盒子内）"}

    first_rel = None
    for auth in candidates:
        rel_x = auth["cx"] / W_rect if W_rect > 0 else -1.0
        rel_y = auth["cy"] / H_rect if H_rect > 0 else -1.0
        v = validate_sticker_position(rel_x, rel_y, position_cfg)
        if first_rel is None:
            first_rel = (v["rel_x"], v["rel_y"])
        if v["in_correct_position"]:
            return {"found": True, "in_correct_position": True,
                    "rel_x": v["rel_x"], "rel_y": v["rel_y"],
                    "detail": f"Apple授权专营店位置规范 (rel_x={v['rel_x']:.3f}, rel_y={v['rel_y']:.3f})"}

    rel_x, rel_y = first_rel
    return {"found": True, "in_correct_position": False,
            "rel_x": rel_x, "rel_y": rel_y,
            "detail": "Apple授权专营店位置异常"}


def check_dual_sticker_status(texts, polys, img_h, sticker_count_mode="single_or_dual") -> dict:
    scan_stickers = find_all_scan_stickers(texts, polys)
    has_auth_raw = any(
        kw in text
        for text in texts
        for kw in ["Apple授权专营店", "授权专营店", "在你身边"]
    )

    MIN_Y_GAP = max(img_h * 0.20, 50)
    distinct = []
    for s in scan_stickers:
        if not any(abs(s["cy"] - e["cy"]) < MIN_Y_GAP for e in distinct):
            distinct.append(s)
    scan_count = len(distinct)

    if sticker_count_mode == "single_only":
        if scan_count >= 2:
            return {"scan_count": scan_count, "has_auth": False,
                    "dual_code": 2, "dual_detail": f"错误：检测到{scan_count}个'扫码即领'（该 LOB 仅允许单贴）"}
        elif scan_count == 1:
            return {"scan_count": scan_count, "has_auth": False,
                    "dual_code": 0, "dual_detail": "单贴：仅'扫码即领'"}
        else:
            return {"scan_count": 0, "has_auth": False,
                    "dual_code": -1, "dual_detail": "未找到'扫码即领'贴纸"}

    if scan_count >= 2:
        return {"scan_count": scan_count, "has_auth": has_auth_raw,
                "dual_code": 2, "dual_detail": f"检测到{scan_count}个'扫码即领'，上下均为扫码贴"}
    elif scan_count == 1 and has_auth_raw:
        return {"scan_count": 1, "has_auth": True,
                "dual_code": 1, "dual_detail": "合规双贴：'扫码即领' + 'Apple授权专营店'"}
    elif scan_count == 1:
        return {"scan_count": 1, "has_auth": False,
                "dual_code": 0, "dual_detail": "单贴：仅'扫码即领'"}
    else:
        return {"scan_count": 0, "has_auth": has_auth_raw,
                "dual_code": -1, "dual_detail": "未找到'扫码即领'贴纸"}


def is_flat_sticker(texts, polys, anchor_idx, box_x=0, box_y=0, box_w=0, box_h=0):
    """平铺错误检测：检测端片语义信号（Authorized Reseller 或远端授权经销商大字）"""
    if anchor_idx >= len(polys):
        return False, "锚点索引越界"

    use_box_filter = box_w > 0 and box_h > 0
    anchor_pts    = np.array(polys[anchor_idx], dtype=float)
    anchor_center = anchor_pts.mean(axis=0)
    rect_a        = cv2.minAreaRect(anchor_pts.astype(np.float32))
    anchor_h      = max(min(rect_a[1]), 1.0)

    for i, text in enumerate(texts):
        if i == anchor_idx or i >= len(polys):
            continue
        pts    = np.array(polys[i], dtype=float)
        center = pts.mean(axis=0)

        if use_box_filter:
            cx, cy = float(center[0]), float(center[1])
            if not (box_x <= cx <= box_x + box_w and box_y <= cy <= box_y + box_h):
                continue

        dist = float(np.linalg.norm(center - anchor_center))
        t_lower = text.lower().strip()

        if ("authorized" in t_lower or "authorised" in t_lower) and "reseller" in t_lower:
            if dist > anchor_h * 2:
                return True, f"检测到端片标识 'Authorized Reseller'（距锚点{dist:.0f}px）"

        if "授权经销商" in text and not ("扫码" in text or "Apple" in text):
            rect_i = cv2.minAreaRect(pts.astype(np.float32))
            text_h = max(min(rect_i[1]), 1.0)
            if text_h > anchor_h * 0.8 and dist > anchor_h * 5:
                return True, f"检测到远端'授权经销商'大字（距锚点{dist:.0f}px）"

    return False, "未检测到端片文字，贴纸未平铺"


# ═══════════════════════════════════════════════════════════════════════════════
# 七、水印提取
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
        if p and (sum(1 for c in p if chr(0x4E00) <= c <= chr(0x9FFF))
                  / max(len(p.replace(" ", "")), 1)) >= 0.4
    ]
    return time_str, re.sub(r"\s+", " ", " ".join(clean_loc)).strip()


def extract_watermark_crop(image: Image.Image, image_id: str) -> tuple[str, str]:
    if image is None:
        return "", ""
    try:
        w, h = image.size
        crop = image.crop((0, int(h * 0.82), int(w * 0.60), h))
        # 水印字大且清晰，无需 2000px。检测器默认 limit_type='min' 会把这条
        # 2177×872 的小图短边上采样到 2000（≈5000×2000，等于又跑一次全图 OCR）。
        # 改用 limit_type='max' + 1280：只缩不放，水印仍能识别，单张 ~3.2s → ~0.45s。
        result = ocr.predict(
            input=pil_to_cv(crop),
            text_det_limit_side_len=1280, text_det_limit_type='max',
        )
        texts = []
        if result and len(result) > 0:
            r = result[0]
            if hasattr(r, 'json'):
                texts = r.json.get('res', {}).get('rec_texts', [])
        return parse_watermark_text(texts)
    except Exception as e:
        print("  水印OCR异常:", type(e).__name__, str(e)[:80])
        return "", ""


# ═══════════════════════════════════════════════════════════════════════════════
# 八、保存
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
# 九、行处理（V3 核心逻辑）
# ═══════════════════════════════════════════════════════════════════════════════

def _make_result(is_compliant, seal_exists, position_valid,
                 rel_x, rel_y, box_method, detail,
                 dual_code, dual_detail,
                 watermark_time, watermark_location,
                 unofficial_sticker: int = 0,
                 lob: str = "",
                 rectify_method: str = "",
                 box_quad_src=None,
                 unofficial_color_checked: int = 0,
                 unofficial_color_mode: str = "",
                 download_status: str = "完整",
                 download_total: int | None = None,
                 download_success: int | None = None,
                 download_failed: int | None = None,
                 download_failed_detail: str = "") -> dict:
    return {
        "is_compliant":             is_compliant,
        "seal_exists":              seal_exists,
        "unofficial_sticker":       unofficial_sticker,
        "position_valid":           position_valid,
        "rel_x":                    rel_x,
        "rel_y":                    rel_y,
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
        "download_status":          download_status,
        "download_total":           download_total,
        "download_success":         download_success,
        "download_failed":          download_failed,
        "download_failed_detail":   download_failed_detail,
    }


def find_back_image_by_scan_anchor(image: Image.Image, lob: str, image_id: str) -> dict | None:
    """第一轮扫描：0° 整图 OCR + 0° 紫色封贴局部 OCR 查找背面图。

    V7.1：旋转扫描拆分到 _rotated_crop_scan()，由 process_row 在所有图片
    0° 均未命中后作为第二轮统一执行（背面图绝大多数 0° 即可命中，先把
    每张图的 0° 都试完，比在第 1 张图上烧完 4 个角度更快找到背面）。
    """
    full_text, texts, polys_orig, orig_h, orig_w = ocr_image_full(image, image_id)
    print(f"  原图 OCR文字: {full_text[:120]}{'...' if len(full_text) > 120 else ''}")

    if has_scan_text(texts):
        return {
            "image": image, "texts": texts, "polys_orig": polys_orig,
            "orig_h": orig_h, "orig_w": orig_w,
            "scan_angle": 0, "scan_method": "full_ocr",
        }

    if lob in SCAN_LOCAL_CROP_LOBS or is_accessory_lob(lob):
        crop_hit = ocr_scan_candidate_crops(image, texts, polys_orig, image_id, lob=lob)
        if crop_hit is not None:
            cand_count = int(crop_hit.get("candidate_count", 0))
            crop_box = crop_hit.get("crop_box")
            if crop_box:
                print(f"  原图 紫色封贴局部OCR命中'扫码即领'，"
                      f"候选数={cand_count}，crop={crop_box}")
                return {
                    "image": image,
                    "texts": crop_hit["texts"],
                    "polys_orig": crop_hit["polys_orig"],
                    "orig_h": image.size[1],
                    "orig_w": image.size[0],
                    "scan_angle": 0,
                    "scan_method": "purple_crop_ocr",
                }
            if cand_count:
                print(f"  原图 找到紫色封贴候选 {cand_count} 个，局部OCR未命中")

    return None


def _rotated_crop_scan(image: Image.Image, lob: str, image_id: str) -> dict | None:
    """第二轮扫描：90/180/270 旋转后只做紫色封贴局部 OCR。

    V7.1 起不再做旋转整图 OCR：0803 批次实测 1746 次命中中旋转整图 OCR 仅
    贡献 1 次，而旋转裁剪贡献 239 次；整图 OCR 的 3000px 大块显存激活正是
    多进程显存挤兑的主要来源，裁剪小图保住了旋转扫描几乎全部的实际召回。
    """
    if not (lob in SCAN_LOCAL_CROP_LOBS or is_accessory_lob(lob)):
        return None
    for angle in scan_ocr_angles_for_lob(lob):
        if angle == 0:
            continue
        scan_image = rotate_image_for_scan_ocr(image, angle)
        angle_id = f"{image_id}_rot{angle}"
        crop_hit = ocr_scan_candidate_crops(scan_image, [], [], angle_id, lob=lob)
        if crop_hit is None:
            continue
        cand_count = int(crop_hit.get("candidate_count", 0))
        crop_box = crop_hit.get("crop_box")
        if crop_box:
            print(f"  旋转{angle}° 紫色封贴局部OCR命中'扫码即领'，"
                  f"候选数={cand_count}，crop={crop_box}")
            return {
                "image": scan_image,
                "texts": crop_hit["texts"],
                "polys_orig": crop_hit["polys_orig"],
                "orig_h": scan_image.size[1],
                "orig_w": scan_image.size[0],
                "scan_angle": angle,
                "scan_method": "purple_crop_ocr",
            }
        if cand_count:
            print(f"  旋转{angle}° 找到紫色封贴候选 {cand_count} 个，局部OCR未命中")
    return None


def process_row(row, idx: int, total: int, prefetched_tasks=None) -> dict:
    """
    V3 简化版行处理：
      Step 1  找第一张含"扫码即领"的图 → 背面（不强求包装盒检测通过）
      Step 2  对背面图做非官方贴纸颜色检测
      Step 3  位置验证（放宽容差 STICKER_POSITION_TOLERANCE=0.25，无角度约束）
    """
    print(f"\n{'='*80}")
    print(f"处理第 {idx}/{total} 行 (订单号: {row.get('订单号', 'N/A')})")
    print('=' * 80)

    # Phase 0: LOB 识别
    lob = detect_lob(row)
    if lob == UNRECOGNIZED_LOB:
        print(f"  LOB: {lob} — 直接标记不合格")
        r = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=None,
            detail=f"{UNRECOGNIZED_LOB}: Excel LOB 列缺失或不在枚举内",
            dual_code=-1, dual_detail="跳过",
            watermark_time="", watermark_location="", lob=lob,
        )
        _print_summary(r)
        return r

    lob_cfg     = resolve_lob_config(lob)
    anchor_only = bool(lob_cfg.get("anchor_only", False))
    scan_cfg    = lob_cfg.get("scan_sticker")
    auth_cfg    = lob_cfg.get("auth_sticker")
    color_cfg   = lob_cfg.get("unofficial_color", {"enabled": False})
    sc_mode     = lob_cfg.get("sticker_count", "single_or_dual")

    print(f"  LOB: {lob}  (sticker_count={sc_mode}"
          f"{'，配件宽松判定：扫码即⟹合规' if anchor_only else ''})")

    watermark_time, watermark_location = "", ""
    watermark_extracted = False
    tasks = prefetched_tasks if prefetched_tasks is not None else submit_row_downloads(row)

    # Phase 1: 找第一张含"扫码即领"的图作为背面
    # V3 改动：找到即停，不再扫描所有图片
    # V7.1 改动：先把所有图的 0° 扫完（第一轮），全未命中才逐图旋转裁剪（第二轮）
    back = None
    download_success = 0
    download_failures = []
    rotation_candidates = []  # 0° 未命中的图，留给第二轮旋转裁剪扫描
    for col_idx, col, url, future in tasks:
        print(f"\n  第{col_idx}张图片: {url[:80]}...")
        try:
            dl = future.result()
        except Exception as e:
            dl = {
                "ok": False, "image": None,
                "status": f"future_{type(e).__name__}: {str(e)[:80]}",
                "attempts": 0,
            }

        if isinstance(dl, dict):
            image = dl.get("image")
            dl_status = str(dl.get("status", "unknown"))
            dl_attempts = dl.get("attempts", "")
        else:
            # 兼容旧式返回值；正常新代码不会走到这里。
            image = dl
            dl_status = "ok" if image is not None else "unknown"
            dl_attempts = ""

        if image is None:
            download_failures.append({
                "col_idx": col_idx,
                "col": col,
                "status": dl_status,
                "attempts": dl_attempts,
                "url": url,
            })
            print(f"  → 下载失败，跳过 (status={dl_status}, attempts={dl_attempts})")
            continue
        download_success += 1

        print(f"  图片尺寸: {image.size}")
        image_id = f"row{idx}_col{col_idx}"

        if not watermark_extracted:
            wm_time, wm_loc = extract_watermark_crop(image, image_id)
            watermark_time, watermark_location = wm_time, wm_loc
            watermark_extracted = True
            print(f"  水印时间: {wm_time or '(未识别)'}")
            print(f"  水印地点: {wm_loc or '(未识别)'}")

        back_candidate = find_back_image_by_scan_anchor(image, lob, image_id)

        if back_candidate is not None:
            back = back_candidate
            angle = back.get("scan_angle", 0)
            method = back.get("scan_method", "")
            angle_note = "原图" if angle == 0 else f"旋转{angle}°"
            print(f"  → ✓ 通过 {angle_note}/{method} 检测到'扫码即领'，确认为背面，停止扫描")
            break
        else:
            print(f"  → 未检测到'扫码即领'，继续下一张")
            rotation_candidates.append((col_idx, image, image_id))

    # V7.1 第二轮：所有图 0° 均未命中时，对可旋转 LOB（Watch/AirPods/配件）
    # 逐图做旋转紫贴裁剪扫描。
    if back is None and rotation_candidates:
        for rc_col_idx, rc_image, rc_image_id in rotation_candidates:
            hit = _rotated_crop_scan(rc_image, lob, rc_image_id)
            if hit is not None:
                back = hit
                print(f"  → ✓ 通过 旋转{hit['scan_angle']}°/{hit['scan_method']} "
                      f"检测到'扫码即领'（第{rc_col_idx}张图，第二轮旋转裁剪），确认为背面")
                break

    # 无背面图 → 不合格
    if back is None:
        if download_failures:
            failed_detail = json.dumps(download_failures, ensure_ascii=False)
            print(f"  → 未找到背面图，但存在图片下载失败：{failed_detail[:500]}")
            r = _make_result(
                is_compliant=None, seal_exists=None, position_valid=-2,
                rel_x=None, rel_y=None, box_method=None,
                detail=(f"图片下载不完整：{len(download_failures)}/{len(tasks)} 张失败，"
                        "本行未做完整识别，请重跑或检查失败URL"),
                dual_code=-1, dual_detail="下载不完整，跳过判定",
                watermark_time=watermark_time, watermark_location=watermark_location,
                lob=lob,
                download_status="下载不完整",
                download_total=len(tasks),
                download_success=download_success,
                download_failed=len(download_failures),
                download_failed_detail=failed_detail,
            )
            _print_summary(r)
            return r

        print(f"  → 未找到背面图")
        r = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=None,
            detail="未找到含'扫码即领'的背面图",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob,
            download_status="完整",
            download_total=len(tasks),
            download_success=download_success,
            download_failed=0,
        )
        _print_summary(r)
        return r

    # Phase A（V7 配件宽松判定）：已在某张出库图检出"扫码即"锚点（back 非空即代表命中）。
    # 配件包装形状/尺寸/拍摄角度千差万别，位置规则不适用 → 只要锚点存在即判定
    # 封口贴存在且规范粘贴，跳过位置校验、双贴校验与非官方贴纸颜色检测。
    if anchor_only:
        scan_angle  = back.get("scan_angle", 0)
        scan_method = back.get("scan_method", "")
        angle_note  = "原图" if scan_angle == 0 else f"旋转{scan_angle}°"
        print(f"  → ✓ 配件宽松判定：{angle_note}/{scan_method} 检出'扫码即'，"
              f"判定封口贴存在且合规")
        # 与其它"合规"返回路径一致：不填 download_* 明细（保留 _make_result 的
        # "完整"默认值）。锚点已命中即完成判定，下载列与核心品类合规行保持一致。
        r = _make_result(
            is_compliant=1, seal_exists=1, position_valid=1,
            rel_x=None, rel_y=None, box_method=None,
            detail=(f"配件宽松判定：检出'扫码即'锚点即视为封口贴存在且规范粘贴"
                    f"（angle={scan_angle}, method={scan_method}）"),
            dual_code=-1, dual_detail="配件不校验双贴",
            watermark_time=watermark_time, watermark_location=watermark_location,
            unofficial_sticker=0,
            lob=lob, rectify_method="", box_quad_src=None,
            unofficial_color_checked=0, unofficial_color_mode="",
        )
        _print_summary(r)
        return r

    # Phase 2: 包装盒透视矫正（V3: fallback 不跳过，继续处理）
    scan_polys_for_rect = [
        back["polys_orig"][i]
        for i, t in enumerate(back["texts"])
        if is_scan_text(t) and i < len(back["polys_orig"])
    ]
    rectify      = rectify_package_box(back["image"], lob=lob, scan_polys_orig=scan_polys_for_rect)
    rect_method  = rectify["method"]
    warped_img   = rectify["warped_img"]
    M            = rectify["M"]
    W_rect       = int(rectify["W_rect"])
    H_rect       = int(rectify["H_rect"])
    box_quad_src = rectify["box_quad_src"]

    scan_angle = back.get("scan_angle", 0)
    scan_method = back.get("scan_method", "")
    print(f"  矫正方式: {rect_method}  矫正尺寸: {W_rect}×{H_rect}  "
          f"背面识别: angle={scan_angle}, method={scan_method}")

    # Phase 2.5: 将 OCR 多边形提前映射到矫正坐标系
    # （提前到颜色检测前，供排除官方印刷区域掩码使用）
    polys_rect = transform_polys(
        back["polys_orig"], M,
        box_x=rectify["box_x"] if M is None else 0,
        box_y=rectify["box_y"] if M is None else 0,
    )

    # Phase 2.6: 非官方贴纸颜色检测（仅对背面图，排除官方印刷文字区域）
    unofficial_sticker = 0
    color_checked      = 0
    color_mode         = ""

    if color_cfg.get("enabled", False):
        color_mode    = color_cfg.get("mode", "white_box")
        color_checked = 1
        has_unoff, unoff_detail = detect_unofficial_sticker_color(warped_img, color_cfg)
        if has_unoff:
            unofficial_sticker = 1
            print(f"  ⚠ 颜色检测命中非官方贴纸：{unoff_detail}")
            r = _make_result(
                is_compliant=0, seal_exists=1, position_valid=4,
                rel_x=None, rel_y=None, box_method=rect_method,
                detail=f"检测到疑似非官方贴纸：{unoff_detail}",
                dual_code=-1, dual_detail="跳过",
                watermark_time=watermark_time, watermark_location=watermark_location,
                unofficial_sticker=1,
                lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
                unofficial_color_checked=1, unofficial_color_mode=color_mode,
            )
            _print_summary(r)
            return r
        else:
            print(f"  颜色检测 ({color_mode})：未发现非官方贴纸")
    else:
        print(f"  颜色检测：LOB={lob} enabled=False，跳过")

    # Phase 3: 位置验证（放宽容差 {STICKER_POSITION_TOLERANCE}，无角度约束）
    # polys_rect 已在 Phase 2.5 计算完毕
    scan_candidates = find_all_scan_stickers(back["texts"], polys_rect)
    sticker_rect    = pick_best_scan_sticker(scan_candidates, W_rect, H_rect, scan_cfg)

    if sticker_rect is None:
        r = _make_result(
            is_compliant=0, seal_exists=0, position_valid=-1,
            rel_x=None, rel_y=None, box_method=rect_method,
            detail="矫正坐标系中未能定位'扫码即领'贴纸",
            dual_code=-1, dual_detail="跳过",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    rel_x = sticker_rect["cx"] / W_rect if W_rect > 0 else -1.0
    rel_y = sticker_rect["cy"] / H_rect if H_rect > 0 else -1.0

    pos = validate_sticker_position(
        rel_x, rel_y, scan_cfg,
        tolerance=STICKER_POSITION_TOLERANCE,
        unit_bounds_margin=lob_cfg.get("unit_bounds_margin"),
    )

    # 平铺检测（无论位置是否合规都先检测，优先给出更准确原因）
    flat, flat_detail = is_flat_sticker(
        back["texts"], polys_rect, sticker_rect["text_idx"],
        0, 0, W_rect, H_rect,
    )

    if not pos["in_correct_position"]:
        pv     = 2 if flat else 0
        detail = flat_detail if flat else f"[{rect_method}] {pos['detail']}"
        r = _make_result(
            is_compliant=0, seal_exists=1, position_valid=pv,
            rel_x=pos["rel_x"], rel_y=pos["rel_y"],
            box_method=rect_method, detail=detail,
            dual_code=-1, dual_detail="单贴不合规，跳过双贴检测",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    if flat:
        r = _make_result(
            is_compliant=0, seal_exists=1, position_valid=2,
            rel_x=pos["rel_x"], rel_y=pos["rel_y"],
            box_method=rect_method, detail=f"[{rect_method}] {flat_detail}",
            dual_code=-1, dual_detail="平铺错误",
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    # Phase 4: 双贴检测（单贴位置合规后）
    dual = check_dual_sticker_status(
        back["texts"], polys_rect,
        H_rect if H_rect > 0 else back["orig_h"],
        sticker_count_mode=sc_mode,
    )

    if dual["dual_code"] == 2:
        # 两张扫码贴：位置规范，视为合规
        r = _make_result(
            is_compliant=1, seal_exists=1, position_valid=1,
            rel_x=pos["rel_x"], rel_y=pos["rel_y"],
            box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
            dual_code=2, dual_detail=dual["dual_detail"],
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    if dual["dual_code"] == 3:
        r = _make_result(
            is_compliant=0, seal_exists=1, position_valid=1,
            rel_x=pos["rel_x"], rel_y=pos["rel_y"],
            box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
            dual_code=3, dual_detail=dual["dual_detail"],
            watermark_time=watermark_time, watermark_location=watermark_location,
            lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
            unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
        )
        _print_summary(r)
        return r

    if dual["has_auth"] and auth_cfg is not None:
        auth_pos = check_auth_sticker_position(
            back["texts"], polys_rect, W_rect, H_rect, auth_cfg,
        )
        if not auth_pos["found"]:
            dual = {**dual, "dual_code": 0,
                    "dual_detail": "单贴：'Apple授权专营店'在盒子外，忽略"}
        elif not auth_pos["in_correct_position"]:
            r = _make_result(
                is_compliant=0, seal_exists=1, position_valid=1,
                rel_x=pos["rel_x"], rel_y=pos["rel_y"],
                box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
                dual_code=1, dual_detail=f"双贴第二张位置异常：{auth_pos['detail']}",
                watermark_time=watermark_time, watermark_location=watermark_location,
                lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
                unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
            )
            _print_summary(r)
            return r
        else:
            r = _make_result(
                is_compliant=1, seal_exists=1, position_valid=1,
                rel_x=pos["rel_x"], rel_y=pos["rel_y"],
                box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
                dual_code=1, dual_detail=f"双贴合规：{auth_pos['detail']}",
                watermark_time=watermark_time, watermark_location=watermark_location,
                lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
                unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
            )
            _print_summary(r)
            return r

    # 单贴合规
    r = _make_result(
        is_compliant=1, seal_exists=1, position_valid=1,
        rel_x=pos["rel_x"], rel_y=pos["rel_y"],
        box_method=rect_method, detail=f"[{rect_method}] {pos['detail']}",
        dual_code=dual["dual_code"], dual_detail=dual["dual_detail"],
        watermark_time=watermark_time, watermark_location=watermark_location,
        lob=lob, rectify_method=rect_method, box_quad_src=box_quad_src,
        unofficial_color_checked=color_checked, unofficial_color_mode=color_mode,
    )
    _print_summary(r)
    return r


def _print_summary(r: dict):
    print(f"\n【结果汇总】")
    print(f"  LOB             : {r.get('lob', '')}")
    print(f"  是否规范粘贴    : {'✓ 合规(1)' if r['is_compliant'] == 1 else '✗ 不合规(0)'}")
    print(f"  封口贴存在      : {r['seal_exists']}")
    print(f"  非官方贴纸      : {r['unofficial_sticker']}")
    print(f"  位置规范        : {r['position_valid']}")
    print(f"  贴纸相对X/Y     : {r['rel_x']} / {r['rel_y']}")
    print(f"  矫正方式        : {r.get('rectify_method', '')}  包装盒方式: {r['box_method']}")
    print(f"  颜色检测        : checked={r.get('unofficial_color_checked', 0)}  mode={r.get('unofficial_color_mode', '')}")
    print(f"  说明            : {r['detail']}")
    print(f"  双贴纸状态      : {r['dual_code']}  ({r['dual_detail']})")
    if r.get('download_status') and r.get('download_status') != '完整':
        print(f"  图片下载        : {r.get('download_status')}  "
              f"成功/总数={r.get('download_success', '')}/{r.get('download_total', '')}  "
              f"失败={r.get('download_failed', '')}")
    print(f"  水印时间        : {r['watermark_time'] or '(未识别)'}")
    print(f"  水印地点        : {r['watermark_location'] or '(未识别)'}")


# ═══════════════════════════════════════════════════════════════════════════════
# 十、主流程
# ═══════════════════════════════════════════════════════════════════════════════

# V7.6：结果行携带的"源行号"列（仅存在于 worker 分片文件中，最终输出会剔除）。
# 订单号在源表里可能重复——同一订单含多个商品/多个 LOB 时各占一行、各有各的
# 出库图。V7.5 及以前按订单号去重合并，会把这些行 collapse 成一行而静默丢数据
# （0808 批次：源表 27756 行 → 输出 26775 行，约 970 行是这样消失的）。
# 源行号是稳定的行身份，用于合并去重、断点续传，以及保证输出与源表行数一致。
SRC_ROW_COL = '__源行号__'

NEW_COLS = [
    '识别LOB',             # 核心: iPhone/Watch/AirPods/iPad/Mac；其它非空值=配件(原样保留, 如 Beats/原厂配件/3PP)
    '是否规范粘贴',        # 0=不合规 | 1=合规
    '封口贴存在',          # 0 / 1
    '是否存在非官方贴纸',  # 0=无 | 1=检出非官方贴纸（新增列）
    '贴纸位置规范',        # -1=无贴纸 | 0=位置异常 | 1=位置规范 | 2=平铺错误 | 4=非官方贴纸
    '贴纸相对X',
    '贴纸相对Y',
    '包装盒检测方式',
    '矫正方式',
    '包装盒四点坐标',
    '颜色检测已执行',
    '颜色检测模式',
    '位置说明',
    '双贴纸状态',          # -1=无贴 | 0=单贴 | 1=双贴合规 | 2=两扫码贴 | 3=缺二贴
    '双贴纸说明',
    '图片下载状态',        # 完整 / 下载不完整
    '图片下载总数',
    '图片下载成功数',
    '图片下载失败数',
    '图片下载失败详情',
    '时间',
    '地点',
]


def _result_to_row(row, r: dict, src_row: int | None = None) -> dict:
    """把 process_row 的返回值 r 合并进原始行 → 输出行字典。

    V7.6：src_row 为源表行号，写入 SRC_ROW_COL 作为稳定行身份（见该常量注释）。
    """
    result_row = row.to_dict()
    if src_row is not None:
        result_row[SRC_ROW_COL] = int(src_row)
    result_row['识别LOB']            = r.get('lob', '')
    result_row['是否规范粘贴']       = r['is_compliant']
    result_row['封口贴存在']         = r['seal_exists']
    result_row['是否存在非官方贴纸'] = r.get('unofficial_sticker', 0)
    result_row['贴纸位置规范']       = r['position_valid']
    result_row['贴纸相对X']          = r['rel_x']
    result_row['贴纸相对Y']          = r['rel_y']
    result_row['包装盒检测方式']     = r['box_method']
    result_row['矫正方式']           = r.get('rectify_method', '')
    result_row['包装盒四点坐标']     = (
        json.dumps(r['box_quad_src'], ensure_ascii=False)
        if r.get('box_quad_src') else ''
    )
    result_row['颜色检测已执行']     = r.get('unofficial_color_checked', 0)
    result_row['颜色检测模式']       = r.get('unofficial_color_mode', '')
    result_row['位置说明']           = r['detail']
    result_row['双贴纸状态']         = r['dual_code']
    result_row['双贴纸说明']         = r['dual_detail']
    result_row['图片下载状态']       = r.get('download_status', '')
    result_row['图片下载总数']       = r.get('download_total', '')
    result_row['图片下载成功数']     = r.get('download_success', '')
    result_row['图片下载失败数']     = r.get('download_failed', '')
    result_row['图片下载失败详情']   = r.get('download_failed_detail', '')
    result_row['时间']               = r['watermark_time']
    result_row['地点']               = r['watermark_location']
    return result_row


def _shard_paths(output_csv: str, worker_id: int, round_id: int = 0) -> tuple[str, str, str]:
    """由最终 CSV 路径推导某个 worker 的分片 csv / jsonl / log 路径。

    V7：round_id=0（主批次）文件名与 V6 完全一致，不破坏历史分片的断点续传；
    round_id>=1 为"自动补下载重试轮"，用独立文件名（.retryN）避免与主批次的
    分片内断点续传（shard_done）互相干扰——否则重试轮会把主批次里已写过的
    失败行误判为"已处理"而跳过，起不到重试作用。
    """
    stem, _ = os.path.splitext(output_csv)
    tag = f".retry{round_id}" if round_id else ""
    return (f"{stem}.wshard{worker_id}{tag}.csv",
            f"{stem}.wshard{worker_id}{tag}.jsonl",
            f"{stem}.wshard{worker_id}{tag}.log")


_SHARD_CSV_RE = re.compile(r'\.wshard(\d+)(?:\.retry(\d+))?\.csv$')


def _iter_shard_csvs(output_csv: str) -> list[str]:
    """发现某最终 CSV 对应的全部分片 csv（主批次 + 所有补下载重试轮），
    按 (round_id, worker_id) 排序——round 靠后的文件排在后面，
    使 merge 阶段 drop_duplicates(keep='last') 时更晚的重试轮结果覆盖更早的。
    仅按文件名字符串排序做不到这点：worker 编号会干扰 round 顺序（例如
    'wshard3.retry1.csv' 按字典序会排在 'wshard7.csv' 之前），必须显式按
    round 优先排序。
    """
    stem, _ = os.path.splitext(output_csv)
    paths = glob.glob(f"{glob.escape(stem)}.wshard*.csv")

    def _sort_key(p: str):
        m = _SHARD_CSV_RE.search(os.path.basename(p))
        if not m:
            return (0, 0)
        worker_id = int(m.group(1))
        round_id  = int(m.group(2)) if m.group(2) else 0
        return (round_id, worker_id)

    return sorted(paths, key=_sort_key)


def _collect_shard_frames(output_csv: str) -> list[pd.DataFrame]:
    """读取合并 CSV(若存在) + 全部分片 csv（主批次 + 各补下载重试轮），
    按"更晚处理的结果更权威"的顺序返回，供 merge_shards / _read_processed_rows /
    _orders_missing_seal 复用。

    兼容 V3：合并 CSV(output_csv) 可能是一份跑到一半的 V3 单文件结果，或
    上一次合并输出；不先纳入就直接覆盖会丢掉那些行。
    """
    frames = []
    if os.path.exists(output_csv):
        try:
            frames.append(pd.read_csv(output_csv, encoding='utf-8-sig',
                                      dtype=_ID_DTYPE, on_bad_lines='skip'))
        except Exception as e:
            print(f"  ⚠ 读取已有 CSV {output_csv} 失败: {e}")
    for csv_shard in _iter_shard_csvs(output_csv):
        try:
            # on_bad_lines='skip'：容忍上次崩溃残留的半行 —— 那一行会被当作未处理重跑
            frames.append(pd.read_csv(csv_shard, encoding='utf-8-sig',
                                      dtype=_ID_DTYPE, on_bad_lines='skip'))
        except Exception as e:
            print(f"  ⚠ 读取分片 {csv_shard} 失败: {e}")
    return frames


_NO_RESULT_DETAIL = ('ERROR: 未处理（worker 因显存不足被放弃或运行中断），'
                     '重跑本脚本可自动补上')


def _fingerprint_cols(df: pd.DataFrame) -> list[str]:
    """行指纹用的列集合，必须由**源表**决定（源表是权威）。"""
    return ['订单号'] + [c for c in IMAGE_COLUMNS if c in df.columns]


def _row_fingerprint(frame: pd.DataFrame, cols: list[str]) -> pd.Series:
    """行指纹 = 订单号 + 全部图片地址列，用于给缺少 SRC_ROW_COL 的旧结果定位
    它属于哪一源行。订单号重复时（同一订单多商品）各行的出库图不同，因此加上
    图片列即可区分；若连图片列都完全相同，那本就是完全重复的行，归到哪一行
    内容都一样。

    cols 必须由调用方从源表算出（_fingerprint_cols）并对参与比对的所有表用
    同一份：若按各表自身的列集合各算一次，某份 CSV 少一列就会导致所有指纹
    全部对不上（表现为"整批重跑"或"全部记录无法归属"）。frame 里缺的列按
    空值处理。

    空值一律归一成空串，不能直接用 astype(str)：内存里的 None 会变成 'None'
    而 CSV 往返后的 NaN 会变成 'nan'，同一行的指纹就对不上了。同理去掉数值
    列往返产生的尾随 '.0'（float64 的 '123.0' vs int64/str 的 '123'）。指纹
    只需在参与比对的各表之间自洽，不要求还原原值，所以这些归一是安全的。
    """
    parts = []
    for c in cols:
        if c in frame.columns:
            s = frame[c]
            s = s.astype(str).where(s.notna(), '')
            parts.append(s.str.strip().str.replace(r'\.0$', '', regex=True))
        else:
            parts.append(pd.Series('', index=frame.index, dtype=object))
    if not parts:
        return pd.Series('', index=frame.index, dtype=object)
    out = parts[0]
    for p in parts[1:]:
        out = out + '\x01' + p
    return out


def _resolve_src_positions(df: pd.DataFrame, merged: pd.DataFrame) -> list:
    """给每条结果记录解析出它对应的源行位置（0..len(df)-1），无法归属的记为 None。

    新分片直接读 SRC_ROW_COL；旧分片（V7.5 及以前，没有该列）退回行指纹匹配，
    同指纹多源行时按出现顺序依次占位。
    """
    n = len(df)
    fp_cols = _fingerprint_cols(df)
    fp_positions: dict[str, list[int]] = {}
    for pos, fp in enumerate(_row_fingerprint(df, fp_cols)):
        fp_positions.setdefault(fp, []).append(pos)

    key_vals = (pd.to_numeric(merged[SRC_ROW_COL], errors='coerce')
                if SRC_ROW_COL in merged.columns else None)
    merged_fp = _row_fingerprint(merged, fp_cols)
    fp_cursor: dict[str, int] = {}

    positions = []
    for i in range(len(merged)):
        pos = None
        if key_vals is not None:
            kv = key_vals.iat[i]
            if pd.notna(kv) and 0 <= int(kv) < n:
                pos = int(kv)
        if pos is None:
            plist = fp_positions.get(merged_fp.iat[i])
            if plist:
                c = fp_cursor.get(merged_fp.iat[i], 0)
                pos = plist[c % len(plist)]
                fp_cursor[merged_fp.iat[i]] = c + 1
        positions.append(pos)
    return positions


def _merge_frames(df: pd.DataFrame, frames: list[pd.DataFrame]) -> pd.DataFrame | None:
    """合并所有分片结果 → 与源表行数严格一致、行序一致的结果表。

    V7.6：行身份从"订单号"改为源行号（见 SRC_ROW_COL）。此前按订单号去重会把
    同一订单的多个商品行 collapse 成一行而静默丢数据；且被放弃 worker 的 OOM 行
    未落盘时会整行从输出里消失。现在以源表为基准左连接回填——每一源行恰好输出
    一行，没有结果的行标记为 ERROR 待重跑，绝不静默消失。
    """
    if not frames:
        return None
    merged = pd.concat(frames, ignore_index=True)
    if '订单号' not in merged.columns:
        return merged

    if '位置说明' in merged.columns:
        # V7.5：同一行同时存在 ERROR 行与正常结果行时（重跑修复后，旧的 ERROR
        # 行可能残留在排序更靠后的分片文件里），保证正常结果获胜：把 ERROR 行
        # 稳定地挪到最前，keep='last' 自然选中最后一条正常结果；只剩 ERROR 行
        # 的源行仍保留其最后一条 ERROR。
        is_err = merged['位置说明'].astype(str).str.startswith('ERROR:')
        merged = pd.concat([merged[is_err], merged[~is_err]], ignore_index=True)

    merged['__pos__'] = _resolve_src_positions(df, merged)
    n_orphan = int(merged['__pos__'].isna().sum())
    if n_orphan:
        # 归属不到任何源行：多为换了源文件却复用了同名结果/分片文件的残留。
        print(f"  ⚠ {n_orphan} 条结果记录无法归属到本源表的任何一行（已忽略）——"
              f"通常是换了 --input 却复用了同名输出/分片文件的残留。")
        merged = merged[merged['__pos__'].notna()]
    merged = merged.drop_duplicates(subset='__pos__', keep='last')

    # 以源表为基准：源列取源表自身的值（权威、且避免 CSV 往返的类型漂移），
    # 结果列按源行号左连接回填。
    res_cols = [c for c in NEW_COLS if c in merged.columns]
    joined = merged.set_index('__pos__')[res_cols].reindex(range(len(df)))
    out = df.copy()
    for col in NEW_COLS:
        out[col] = joined[col].to_numpy() if col in res_cols else None

    has_result = pd.Series(False, index=range(len(df)))
    has_result.loc[merged['__pos__'].astype(int).tolist()] = True
    n_missing = int((~has_result).sum())
    if n_missing:
        # 数值列保持为空而不是填 0——填 0 会被下游误读成"确认无封口贴"。
        out.loc[(~has_result).to_numpy(), '位置说明'] = _NO_RESULT_DETAIL
        print(f"  ⚠ 有 {n_missing} 行没有任何识别结果，已在输出中保留原行并标记 "
              f"ERROR（数值列留空，不会被误读成'无封口贴'）；重跑本脚本自动补上。")
    return out


def _orders_missing_seal(merged: pd.DataFrame | None, order_ids) -> list[str]:
    """V7：从当前合并结果中找出仍"封口贴存在≠1"的订单号（含未写入结果的，
    如 worker 崩溃导致该行完全没落盘），供自动补下载重试轮判断是否需要再来一轮。

    不区分"真下载失败"与"确实没贴封口贴"——两者都会被重试一次，用一轮额外
    计算换取召回率，是本功能有意为之的取舍。
    """
    order_ids = {str(o) for o in order_ids}
    if merged is None or '订单号' not in merged.columns or '封口贴存在' not in merged.columns:
        return sorted(order_ids)
    sub = merged[merged['订单号'].astype(str).isin(order_ids)]
    seal = pd.to_numeric(sub['封口贴存在'], errors='coerce').fillna(0)
    missing = set(sub.loc[seal != 1, '订单号'].astype(str))
    found_ids = set(sub['订单号'].astype(str))
    missing |= (order_ids - found_ids)   # 完全没写入结果的订单号也要重试
    return sorted(missing)


def _read_processed_rows(df: pd.DataFrame, output_csv: str) -> tuple[set[int], set[str]]:
    """收集已处理的行，用于断点续传，返回 (已处理源行号集合, 已处理订单号集合)。

    会扫描合并 CSV(output_csv) + 全部分片 csv（含各补下载重试轮，见
    _iter_shard_csvs），取并集。

    V7.6：新分片带 SRC_ROW_COL，按源行号判定；旧分片（V7.5 及以前，含合并 CSV
    本身——最终输出不含该列）用"订单号+全部图片地址列"的行指纹回推源行，精度
    与源行号等同，因此订单号重复时不会误把兄弟行当成已处理而跳过。
    只有连行指纹都对不上的记录才退回订单号判定（换过源文件的残留等）。
    兼容 V3 单文件输出同理。
    """
    processed_pos: dict[int, bool] = {}
    processed_oid: dict[str, bool] = {}

    fp_cols = _fingerprint_cols(df)
    fp_positions: dict[str, list[int]] = {}
    for pos_i, fp in enumerate(_row_fingerprint(df, fp_cols)):
        fp_positions.setdefault(fp, []).append(pos_i)

    for dfx in _collect_shard_frames(output_csv):
        if '订单号' not in dfx.columns:
            continue
        has_key = SRC_ROW_COL in dfx.columns
        # 行指纹按帧独立计游标：同一记录同时出现在合并 CSV 和分片里时，两次都应
        # 落到同一源行；若跨帧共用游标会被错当成两个不同的行而虚增"已处理"。
        frame_fps = None if has_key else _row_fingerprint(dfx, fp_cols)
        fp_cursor: dict[str, int] = {}
        try:
            for i, (_, r) in enumerate(dfx.iterrows()):
                oid = str(r.get('订单号', '')).strip()
                if not oid:
                    continue
                pos = None
                if has_key:
                    try:
                        pos = int(float(r.get(SRC_ROW_COL)))
                    except (TypeError, ValueError):
                        pos = None
                    if pos is not None and not (0 <= pos < len(df)):
                        pos = None
                if pos is None and frame_fps is not None:
                    fp = frame_fps.iat[i]
                    plist = fp_positions.get(fp)
                    if plist:
                        c = fp_cursor.get(fp, 0)
                        pos = plist[c % len(plist)]
                        fp_cursor[fp] = c + 1
                download_status = str(r.get('图片下载状态', '')).strip()
                detail = str(r.get('位置说明', '')).strip()
                position_valid = str(r.get('贴纸位置规范', '')).strip()
                needs_rerun = (
                    download_status == '下载不完整'
                    or detail.startswith('图片下载不完整')
                    or position_valid in {'-2', '-2.0'}
                )
                target = processed_pos if pos is not None else processed_oid
                key = pos if pos is not None else oid
                if detail.startswith('ERROR:'):
                    # V7.5：ERROR 行（含 V7.4 落盘的 "ERROR: GPU显存不足…"）不算
                    # 已处理——否则失败被永久固化进最终输出；但也不推翻同一行已有
                    # 的正常结果，与 _merge_frames 的"正常结果永远优先于 ERROR 行"
                    # 保持一致，避免已修复的行被无谓重跑。
                    target.setdefault(key, False)
                else:
                    # 与 _merge_frames 的 keep='last' 一致：后读到的结果覆盖前面的状态。
                    target[key] = not needs_rerun
        except Exception as e:
            print(f"  ⚠ 解析分片内容失败（忽略，相关行将重跑）: {e}")
    return ({p for p, done in processed_pos.items() if done},
            {o for o, done in processed_oid.items() if done})


# ═══════════════════════════════════════════════════════════════════════════════
# 十一、Worker 进程
# ═══════════════════════════════════════════════════════════════════════════════

def worker_main(worker_id: int,
                df_shard: pd.DataFrame,
                total_rows: int,
                output_csv: str,
                gpu_mem_fraction,
                download_workers: int,
                prefetch_rows: int,
                progress_counter,
                progress_lock,
                round_id: int = 0,
                download_semaphore=None,
                ocr_semaphore=None,
                gpu_mem_limit_mb: int = 0,
                worker_progress=None,
                scan_crop_upscale: int | None = None,
                scan_crop_candidates: int | None = None,
                scan_crop_upscale_lobs: set | None = None):
    """单个 worker 进程：初始化自己的 PaddleOCR，串行处理分到的行（内部预取下载），
    结果写入自己的分片文件。逐行详细日志重定向到分片 .log 文件，保持终端整洁。

    V7：round_id>=1 时使用独立分片文件名（.retryN，见 _shard_paths），代表
    "自动补下载重试轮"；download_semaphore 是跨所有 worker 进程共享的全局下载
    并发闸，透传给 init_worker_ocr() 供 download_image() 使用。

    V7.5：worker_progress 是跨进程共享的每 worker 已落盘行数数组——主进程用它
    判断"OOM 重启后是否有实际进展"（连续多次重启零进展则放弃重启）。

    V7.7：scan_crop_* 是紫贴局部 OCR 的调优旋钮。必须显式传进来并在此写回模块
    全局——CUDA 要求 spawn 启动方式，子进程是重新 import 本模块的，主进程解析
    到的 args 不会自动出现在这里。
    """
    global SCAN_CROP_UPSCALE_TO, SCAN_LOCAL_CROP_MAX_CANDIDATES, SCAN_CROP_UPSCALE_LOBS
    if scan_crop_upscale is not None:
        SCAN_CROP_UPSCALE_TO = scan_crop_upscale
    if scan_crop_candidates is not None:
        SCAN_LOCAL_CROP_MAX_CANDIDATES = scan_crop_candidates
    if scan_crop_upscale_lobs is not None:
        SCAN_CROP_UPSCALE_LOBS = set(scan_crop_upscale_lobs)
    csv_shard, json_shard, log_shard = _shard_paths(output_csv, worker_id, round_id)
    tag = f"[W{worker_id}]" if not round_id else f"[W{worker_id}][重试轮{round_id}]"

    # 详细日志 → 分片 .log（line-buffered）。终端只保留主进程的聚合进度。
    sys.stdout = open(log_shard, 'a', encoding='utf-8', buffering=1)

    def note(msg: str):
        """向真实终端(stderr)输出一行简短进度，不受 stdout 重定向影响。"""
        print(msg, file=sys.__stderr__, flush=True)

    try:
        note(f"{tag} 初始化 PaddleOCR … ({len(df_shard)} 行待处理)")
        init_worker_ocr(gpu_mem_fraction, download_workers, download_semaphore,
                        ocr_semaphore, gpu_mem_limit_mb=gpu_mem_limit_mb)
        note(f"{tag} PaddleOCR 就绪，开始处理")
    except Exception as e:
        note(f"{tag} ✗ PaddleOCR 初始化失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)

    # 分片内断点续传：跳过本分片已写过的行（重跑同一分片、OOM 重启后生效）
    # V7.6：优先按源行号判定；旧分片没有该列时退回订单号。
    done_pos: set[int] = set()
    done_oids: set[str] = set()
    if os.path.exists(csv_shard):
        try:
            dfx = pd.read_csv(csv_shard, encoding='utf-8-sig', dtype=_ID_DTYPE,
                              on_bad_lines='skip')
            if SRC_ROW_COL in dfx.columns:
                done_pos = {int(v) for v in
                            pd.to_numeric(dfx[SRC_ROW_COL], errors='coerce').dropna()}
            elif '订单号' in dfx.columns:
                # 旧分片：用行指纹回推本分片内的源行，避免把同订单的兄弟行
                # 一起当成已处理而跳过（与 _read_processed_rows 同一套判定）。
                fp_cols = _fingerprint_cols(df_shard)
                shard_fp = {}
                for pos_i, fp in zip(df_shard.index,
                                     _row_fingerprint(df_shard, fp_cols)):
                    shard_fp.setdefault(fp, []).append(pos_i)
                cursor: dict[str, int] = {}
                for fp in _row_fingerprint(dfx, fp_cols):
                    plist = shard_fp.get(fp)
                    if not plist:
                        continue        # 指纹对不上本分片任何行：视为未处理，重跑
                    c = cursor.get(fp, 0)
                    done_pos.add(plist[c % len(plist)])
                    cursor[fp] = c + 1
        except Exception:
            pass

    # df_shard 的原始 index 即源文件行号（主进程 reset_index 后 iloc 切片保留）
    rows = [(idx, row) for idx, row in df_shard.iterrows()
            if idx not in done_pos and str(row.get('订单号', '')) not in done_oids]

    # 行级预取下载（与 V3 主循环一致，只是作用于本 worker 的分片）
    prefetch_cache: dict[int, list] = {}

    def ensure_prefetched(target_pi: int):
        end = min(target_pi + prefetch_rows + 1, len(rows))
        for pi in range(target_pi, end):
            pidx, prow = rows[pi]
            if pidx not in prefetch_cache:
                prefetch_cache[pidx] = submit_row_downloads(prow)

    ensure_prefetched(0)

    consecutive_oom_rows = 0  # V7.5：连续 OOM 行计数，达到阈值即退出等待主进程重启

    for pi, (idx, row) in enumerate(rows):
        ensure_prefetched(pi + 1)
        tasks    = prefetch_cache.pop(idx, None)
        order_id = str(row.get('订单号', ''))

        row_written = False
        try:
            r = process_row(row, idx + 1, total_rows, prefetched_tasks=tasks)
            result_row = _result_to_row(row, r, src_row=idx)
            save_result_immediately(result_row, csv_shard, json_shard)
            consecutive_oom_rows = 0
            row_written = True
            # 逐行结果详情见分片 .log；终端只显示主进程聚合进度条
        except Exception as e:
            print(f"  ✗ 处理异常 (订单号: {order_id}): {type(e).__name__}: {e}")
            print(traceback.format_exc())

            if 'GPU显存不足' in str(e):
                # V7.5：OOM 行不落 ERROR 结果行——V7.4 落盘后断点续传把它当
                # "已处理"永久跳过，补下载重试轮也不再碰它，失败被固化进最终
                # 输出。保持未写入，由重启后的进程/补下载重试轮/下次重跑重试。
                consecutive_oom_rows += 1
                note(f"{tag} 行{idx + 1} 订单{order_id} → ✗ GPU OOM"
                     f"（不落盘，留待重试）: {str(e)[:60]}")
                if consecutive_oom_rows >= OOM_EXIT_AFTER_CONSECUTIVE_ROWS:
                    # V7.5：进程内销毁重建 predictor 救不了 C++ 侧泄漏的活跃
                    # 张量（0806 实测：重建后小图能过、大图立刻再 OOM）。显存
                    # 随进程终止由驱动整体回收才彻底——退出，主进程自动重启。
                    note(f"{tag} ⚠ 连续 {consecutive_oom_rows} 行 GPU OOM，"
                         f"退出进程等待主进程重启（显存随进程退出整体归还）…")
                    try:
                        _dl_executor.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    sys.stdout.flush()
                    sys.__stderr__.flush()
                    # os._exit：跳过解释器退出时对下载线程的 join（可能拖几十秒
                    # 甚至卡死导致主进程迟迟无法重启）；结果 csv/jsonl 逐行即写
                    # 即关、日志行缓冲，无数据丢失风险。
                    os._exit(WORKER_EXIT_OOM_RESPAWN)
            else:
                error_row = row.to_dict()
                error_row[SRC_ROW_COL] = int(idx)
                for col in NEW_COLS:
                    error_row[col] = f"ERROR: {str(e)[:80]}" if col == '位置说明' else None
                save_result_immediately(error_row, csv_shard, json_shard)
                note(f"{tag} 行{idx + 1} 订单{order_id} → ✗ 异常: {str(e)[:60]}")
                consecutive_oom_rows = 0
                row_written = True

        if row_written:
            # V7.5：OOM 未落盘的行不计入进度——它们终将被重试，现在计数、重试
            # 后再计数会让进度超过总数；worker_progress 每 worker 单写者递增，
            # 是主进程判断"重启后是否有实际进展"的依据。
            with progress_lock:
                progress_counter.value += 1
            if worker_progress is not None:
                worker_progress[worker_id] += 1

        # V7.1：定期归还空闲显存，防止多进程 auto_growth 缓存挤兑（0803 事故根因）
        if (pi + 1) % GPU_CACHE_RELEASE_EVERY_ROWS == 0:
            _release_gpu_cache()

    note(f"{tag} ✓ 分片完成，共处理 {len(rows)} 行")


# ═══════════════════════════════════════════════════════════════════════════════
# 十二、合并 & 主进程
# ═══════════════════════════════════════════════════════════════════════════════

def merge_shards(df: pd.DataFrame, output_csv: str, output_excel: str) -> bool:
    """合并所有分片（含各补下载重试轮） → 按源文件原始行序输出最终 CSV + Excel。"""
    frames = _collect_shard_frames(output_csv)
    if not frames:
        print("  ⚠ 没有任何分片结果可合并。")
        return False

    merged = _merge_frames(df, frames)

    # V7.6：行完整性硬校验——输出行数必须等于源表有效行数。丢行是过去两次事故
    # 的共同表现（按订单号去重 collapse 掉同订单多商品行、被放弃 worker 的 OOM
    # 行未落盘），绝不允许再静默发生。
    if len(merged) != len(df):
        print(f"  ✗ 行数不一致：源表 {len(df)} 行，合并结果 {len(merged)} 行。"
              f"已保留分片文件，请勿使用本次输出，并把该信息反馈排查。")
        return False
    n_err = int(merged['位置说明'].astype(str).str.startswith('ERROR:').sum())

    merged.to_csv(output_csv, index=False, encoding='utf-8-sig')
    print(f"✓ 合并完成，最终 CSV: {output_csv}（{len(merged)} 行，与源表一致）")
    if n_err:
        print(f"  ⚠ 其中 {n_err} 行为 ERROR（未成功识别），重跑本脚本会自动重试这些行。")

    print(f"正在生成 Excel: {output_excel}")
    with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
        merged.to_excel(writer, index=False, sheet_name='结果')
    print(f"✓ 完成！共 {len(merged)} 行，输出: {output_excel}")
    return True


def cleanup_shards(output_csv: str) -> int:
    """删除 worker 分片文件；仅应在所有 worker 成功且最终合并成功后调用。"""
    stem, _ = os.path.splitext(output_csv)
    shard_paths = []
    for suffix in ("csv", "jsonl", "log"):
        shard_paths.extend(glob.glob(f"{glob.escape(stem)}.wshard*.{suffix}"))

    removed = 0
    for path in sorted(set(shard_paths)):
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  ⚠ 删除分片失败 {path}: {e}")
    if removed:
        print(f"✓ 已删除 worker 分片文件 {removed} 个")
    return removed


def parse_args():
    p = argparse.ArgumentParser(
        description='OCR 封口贴检测 V7 —— 配件宽松判定（扫码即=封口贴存在），多进程并行')
    p.add_argument('input_file', nargs='?',
                   help='输入 Excel 路径（推荐：直接把每周文件路径放在命令最后）')
    p.add_argument('--input', default=None,
                   help='输入 Excel 路径（兼容旧写法）')
    p.add_argument('--output-csv', default=None,
                   help='最终合并 CSV 路径（缺省由 --input 推导为 {stem}_results.csv）')
    p.add_argument('--output-excel', default=None,
                   help='最终 Excel 路径（缺省由 --input 推导为 {stem}_processed.xlsx）')
    p.add_argument('--workers', type=int, default=min(12, os.cpu_count() or 4),
                   help='worker 进程数（缺省 = min(12, CPU核数)）')
    p.add_argument('--download-workers', type=int, default=DOWNLOAD_WORKERS,
                   help='每个 worker 内部的下载线程数')
    p.add_argument('--prefetch', type=int, default=3,
                   help='每个 worker 预取下载的行数')
    p.add_argument('--gpu-mem-fraction', type=float, default=None,
                   help='显式限制每进程显存比例（缺省用 auto_growth 按需分配）')
    p.add_argument('--gpu-mem-limit-mb', type=int, default=DEFAULT_GPU_MEM_LIMIT_MB,
                   help=f'每 worker 进程显存硬上限 MB（缺省 {DEFAULT_GPU_MEM_LIMIT_MB}，'
                        '0=不限制）。防止单进程 auto_growth 缓存无限囤积把邻居进程'
                        '挤到 OOM；调整 --workers 时按 (显存MB-2000)/workers 同步调整')
    p.add_argument('--progress-interval', type=float, default=5.0,
                   help='主进程聚合进度刷新间隔（秒）')
    p.add_argument('--download-concurrency', type=int,
                   default=DEFAULT_GLOBAL_DOWNLOAD_CONCURRENCY,
                   help='全局下载并发上限，跨所有 worker 进程共享（缺省 32）。'
                        '防止 --workers × --download-workers 的乘积压垮图片服务器/CDN，'
                        '导致高并发下大量下载超时失败')
    p.add_argument('--ocr-concurrency', type=int,
                   default=DEFAULT_OCR_CONCURRENCY,
                   help=f'整图 OCR 全局并发上限（缺省 {DEFAULT_OCR_CONCURRENCY}）。'
                        f'仅长边≥{OCR_GATE_MIN_SIDE}px 的大图推理占名额，'
                        '防止多个 worker 同时到达显存峰值互相挤兑（瞬时 OOM）')
    p.add_argument('--scan-crop-upscale', type=int, default=SCAN_CROP_UPSCALE_TO,
                   help=f'紫贴局部 crop 送 OCR 前放大到的长边（缺省 '
                        f'{SCAN_CROP_UPSCALE_TO}；0=关闭放大，退回 V7.6 行为）。'
                        '放大是 Mac 召回从 35%% 提到 45%% 的关键，但会增加 crop '
                        'OCR 的像素量；嫌慢可降到 1400 左右先试')
    p.add_argument('--scan-crop-upscale-lobs',
                   default=','.join(sorted(SCAN_CROP_UPSCALE_LOBS)),
                   help=f'只对这些 LOB 放大 crop，逗号分隔（缺省 '
                        f'{",".join(sorted(SCAN_CROP_UPSCALE_LOBS))}；空字符串=都不放大）。'
                        '放大是为"盒大、拍得远、贴纸占像素少"准备的，Watch/AirPods/'
                        '配件盒小距离近不需要，放大只是白付算力')
    p.add_argument('--scan-crop-candidates', type=int,
                   default=SCAN_LOCAL_CROP_MAX_CANDIDATES,
                   help=f'每张图最多做几个紫贴候选的局部 OCR（缺省 '
                        f'{SCAN_LOCAL_CROP_MAX_CANDIDATES}）。命中即停，所以只有'
                        '漏检行才会跑满；调小可直接线性削减漏检行的开销')
    p.add_argument('--extra-retry-passes', type=int,
                   default=DEFAULT_EXTRA_RETRY_PASSES,
                   help='主批次跑完后，自动对"封口贴存在≠1"的行做的补下载重试轮数'
                        '（缺省 1；设为 0 关闭，等价于 V6 行为，需手动重跑整个脚本）')
    args = p.parse_args()
    args.input = args.input or args.input_file
    args.scan_crop_upscale_lobs = {s.strip() for s in
                                   str(args.scan_crop_upscale_lobs or '').split(',')
                                   if s.strip()}
    if not args.input:
        p.error(
            '请提供输入 Excel 路径，例如：python ocr_batch_process_v7.py '
            '"/Users/yilinlin/Desktop/Apple/Temp_Tasks/OCR/OCR-sticker/Q4W1-出库照片.xlsx"'
        )
    return args


def _run_worker_round(pending_df: pd.DataFrame, total_rows: int, output_csv: str,
                      args, round_id: int, ctx, download_semaphore,
                      ocr_semaphore=None) -> bool:
    """跑一轮 worker：round_id=0 为主批次，round_id>=1 为 V7 自动补下载重试轮。

    返回 True 表示本轮所有 worker 正常退出（无异常）；False 表示有 worker 崩溃
    （此时调用方应停止后续重试轮并保留分片文件，供人工排查/续传）。
    """
    n_pending = len(pending_df)
    if n_pending == 0:
        return True

    round_label = "主批次" if round_id == 0 else f"补下载重试第{round_id}轮"
    num_workers = max(1, min(args.workers, n_pending))

    # 跨步(strided)分片：把连续的“难行”摊到不同 worker，负载更均衡
    shards = [pending_df.iloc[k::num_workers] for k in range(num_workers)]

    progress_counter = ctx.Value('i', 0)
    progress_lock    = ctx.Lock()
    # V7.5：每 worker 已落盘行数（worker 内单写者递增，主进程只读），用于判断
    # OOM 重启之间是否有实际进展——零进展的重启不解决问题，攒满 strikes 即放弃。
    worker_progress  = ctx.Array('i', num_workers)

    def _spawn(k: int):
        p = ctx.Process(
            target=worker_main,
            args=(k, shards[k], total_rows, output_csv,
                  args.gpu_mem_fraction, args.download_workers, args.prefetch,
                  progress_counter, progress_lock, round_id, download_semaphore,
                  ocr_semaphore, args.gpu_mem_limit_mb, worker_progress,
                  args.scan_crop_upscale, args.scan_crop_candidates,
                  args.scan_crop_upscale_lobs),
            name=f"ocr-worker-{k}" if round_id == 0 else f"ocr-retry{round_id}-worker-{k}",
        )
        p.start()
        return p

    # V7.5：slots 取代一次性的 procs 列表——worker 因连续 OOM 主动退出
    # （exitcode=WORKER_EXIT_OOM_RESPAWN）时自动重启，其余退出视为终态(settled)。
    slots: dict[int, dict] = {}
    for k in range(num_workers):
        if len(shards[k]) == 0:
            continue
        slots[k] = {'proc': _spawn(k), 'settled': False, 'gave_up': False,
                    'respawns': 0, 'strikes': 0, 'progress_mark': 0}

    print(f"已启动 {len(slots)} 个 worker 进程（{round_label}），正在处理 {n_pending} 行 …\n")

    start_time = time.time()
    total_respawns = 0
    while True:
        for k, slot in slots.items():
            p = slot['proc']
            if slot['settled'] or p.is_alive():
                continue
            p.join()
            if p.exitcode == WORKER_EXIT_OOM_RESPAWN:
                # 自上次 OOM 重启以来有没有新落盘的行？有则清零 strikes。
                made_progress = worker_progress[k] > slot['progress_mark']
                slot['progress_mark'] = worker_progress[k]
                slot['strikes'] = 0 if made_progress else slot['strikes'] + 1
                if slot['strikes'] >= OOM_RESPAWN_MAX_STRIKES:
                    slot['settled'] = True
                    slot['gave_up'] = True
                    print(f"\n⚠ [W{k}] 连续 {OOM_RESPAWN_MAX_STRIKES} 次重启之间零新增落盘行"
                          f"（原地 OOM 无进展），放弃重启——多半是显存配置对这批大图"
                          f"太紧（调大 --gpu-mem-limit-mb 或调小 --workers），"
                          f"剩余行保留给下次续传", file=sys.__stderr__, flush=True)
                else:
                    slot['respawns'] += 1
                    total_respawns += 1
                    print(f"\n♻ [W{k}] 连续 GPU OOM 主动退出（显存已随进程整体归还），"
                          f"自动重启（第 {slot['respawns']} 次）…",
                          file=sys.__stderr__, flush=True)
                    slot['proc'] = _spawn(k)
            else:
                slot['settled'] = True  # 0=正常完成；其它=真崩溃，不盲目重启

        n_alive = sum(1 for s in slots.values() if s['proc'].is_alive())
        done  = progress_counter.value
        elapsed = time.time() - start_time
        rate    = done / elapsed if elapsed > 0 else 0
        eta_s   = (n_pending - done) / rate if rate > 0 else 0
        respawn_note = f"(OOM重启{total_respawns}次)" if total_respawns else ""
        print(f"\r[{round_label}] 进度 {done}/{n_pending}  ({done / n_pending * 100:5.1f}%)  "
              f"存活worker {n_alive}/{len(slots)}{respawn_note}  "
              f"速率 {rate:4.1f} 行/秒  已用 {elapsed / 60:5.1f}min  "
              f"预计剩余 {eta_s / 60:5.1f}min      ",
              end='', file=sys.__stderr__, flush=True)
        if all(s['settled'] for s in slots.values()):
            break
        time.sleep(args.progress_interval)

    print(file=sys.__stderr__)  # 换行
    for s in slots.values():
        s['proc'].join()

    # 报告异常退出的 worker（分片可能未跑完 → 重跑本脚本即可续传）
    failed = []
    for k in sorted(slots):
        s = slots[k]
        if s['gave_up']:
            failed.append((f"W{k}", f"连续{OOM_RESPAWN_MAX_STRIKES}次OOM重启无进展，已放弃"))
        elif s['proc'].exitcode not in (0, None):
            failed.append((f"W{k}", s['proc'].exitcode))
    if failed:
        print(f"\n⚠ [{round_label}] 有 {len(failed)} 个 worker 非正常退出：{failed}")
        print("  重新运行本脚本即可从分片断点续传未完成的行。")
    if total_respawns:
        print(f"[{round_label}] 本轮因连续 GPU OOM 自动重启 worker 共 {total_respawns} 次。")

    final_done = progress_counter.value
    total_elapsed = time.time() - start_time
    print(f"[{round_label}] 本轮处理 {final_done} 行，用时 {total_elapsed / 60:.1f}min "
          f"（平均 {final_done / total_elapsed if total_elapsed > 0 else 0:.1f} 行/秒）")

    return not failed


def main():
    args = parse_args()

    stem, _ = os.path.splitext(args.input)
    output_csv   = args.output_csv   or f"{stem}_results.csv"
    output_excel = args.output_excel or f"{stem}_processed.xlsx"

    print("=" * 80)
    print("OCR 封口贴检测 V7 —— 配件宽松判定（扫码即=封口贴存在），多进程并行")
    print("=" * 80)
    print(f"输入:        {args.input}")
    print(f"输出 CSV:    {output_csv}")
    print(f"输出 Excel:  {output_excel}")
    print(f"worker 数:   {args.workers}  (CPU 核数={os.cpu_count()})")
    print(f"下载并发上限: {args.download_concurrency}（全局，跨所有 worker 进程共享）")
    print(f"整图OCR并发上限: {args.ocr_concurrency}"
          f"（全局，仅长边≥{OCR_GATE_MIN_SIDE}px 的大图推理占名额）")
    print(f"每进程显存硬上限: "
          f"{str(args.gpu_mem_limit_mb) + 'MB' if args.gpu_mem_limit_mb else '不限制'}"
          f"（workers={args.workers}，合计约 "
          f"{args.gpu_mem_limit_mb * args.workers // 1024 if args.gpu_mem_limit_mb else 0}GB）")
    _up_lobs = args.scan_crop_upscale_lobs
    print(f"紫贴局部OCR: 启用 LOB {'/'.join(sorted(SCAN_LOCAL_CROP_LOBS))} + 全部配件，"
          f"每图最多 {args.scan_crop_candidates} 个候选")
    print(f"  其中放大到长边 {args.scan_crop_upscale}: "
          f"{'/'.join(sorted(_up_lobs)) if _up_lobs and args.scan_crop_upscale > 0 else '无（全部不放大）'}"
          f"（其余 LOB 原尺寸送 OCR）")
    print(f"自动补下载重试轮数: {args.extra_retry_passes}（0=关闭）")
    print(f"分片文件:    {os.path.splitext(output_csv)[0]}.wshard*.csv / .jsonl / .log")
    print("=" * 80)

    print(f"\n正在读取Excel: {args.input}")
    df = pd.read_excel(args.input, dtype=_ID_DTYPE)
    raw_rows = len(df)
    # 剔除“幽灵行”：源文件常因某列(如 HQ Name)被一路填充到末尾，把 Excel 已使用
    # 区域撑大，pandas 会把这些只有个别列、订单号为空的空行也读进来。仅保留订单号
    # 非空的真实数据行。
    df = df[df['订单号'].notna() & (df['订单号'].astype(str).str.strip() != '')].reset_index(drop=True)
    if len(df) != raw_rows:
        print(f"已剔除 {raw_rows - len(df)} 行空行(订单号为空)")
    total_rows = len(df)
    print(f"总共 {total_rows} 行数据")

    # 断点续传：扫描已有分片，跳过已处理的行
    processed_pos, processed_oids = _read_processed_rows(df, output_csv)
    n_processed = len(processed_pos) + len(processed_oids)
    if n_processed:
        print(f"发现已有分片结果，已处理 {n_processed} 行，仅处理剩余行…")

    # V7.6：源行号（新分片）与订单号（旧分片回退）两条判定取"都不认为已处理"
    pending_mask = (~df.index.isin(processed_pos)) & (
        ~df['订单号'].astype(str).str.strip().isin(processed_oids))
    # df 已 reset_index → 其 index 即源文件行号；筛选后保留该行号（供日志 & 排序）
    pending_df = df[pending_mask]
    n_pending  = len(pending_df)
    print(f"待处理 {n_pending} 行\n")

    if n_pending == 0:
        print("没有待处理的行，直接合并输出。")
        if merge_shards(df, output_csv, output_excel):
            cleanup_shards(output_csv)
        return

    ctx = mp.get_context('spawn')          # CUDA 必须用 spawn，不能 fork
    # V7：全局下载并发闸，跨所有 worker 进程共享，防止 --workers × --download-workers
    # 的乘积压垮图片服务器/CDN（见文件顶部 V7 补充说明）。
    download_semaphore = ctx.BoundedSemaphore(max(1, args.download_concurrency))
    # V7.2：整图 OCR 跨进程并发闸——同时做大图推理的进程数上限，防止显存峰值叠加
    ocr_semaphore = ctx.BoundedSemaphore(max(1, args.ocr_concurrency))

    all_ok = _run_worker_round(pending_df, total_rows, output_csv, args,
                               round_id=0, ctx=ctx, download_semaphore=download_semaphore,
                               ocr_semaphore=ocr_semaphore)

    # V7：自动补下载重试轮 —— 把"手动重跑一次"内建进单次运行。主批次高并发下载
    # 拥堵导致的瞬时失败，会在这里以低得多的并发（剩余行数远少于主批次）自动重试，
    # 无需人工干预。仅在上一轮 worker 全部正常退出时才继续，异常退出说明基础设施
    # 有问题，不应盲目重试。
    current_ids = set(pending_df['订单号'].astype(str))
    round_id = 0
    while all_ok and round_id < args.extra_retry_passes and current_ids:
        merged = _merge_frames(df, _collect_shard_frames(output_csv))
        retry_ids = _orders_missing_seal(merged, current_ids)
        if not retry_ids:
            break
        round_id += 1
        # 诊断：本轮待重试行的 LOB 分布——若明显集中在配件（非核心5类），说明这批
        # "未找到背面图"更可能是配件本身检测难/真实无贴，而非下载拥堵，重试收益有限。
        retry_lob_counts = (df[df['订单号'].astype(str).isin(retry_ids)]['LOB']
                            .astype(str).value_counts().to_dict())
        print(f"\n发现 {len(retry_ids)} 行未识别到封口贴/背面图（疑似下载问题），"
              f"启动第 {round_id} 轮自动补下载重试…")
        print(f"  本轮 LOB 分布：{retry_lob_counts}")
        retry_df = df[df['订单号'].astype(str).isin(retry_ids)]
        all_ok = _run_worker_round(retry_df, total_rows, output_csv, args,
                                   round_id=round_id, ctx=ctx,
                                   download_semaphore=download_semaphore,
                                   ocr_semaphore=ocr_semaphore)

        # 诊断：本轮实际恢复了多少行（0→1），而不是仅仅"跑完了"。恢复率低说明
        # 大部分失败并非下载问题，加大并发/重试轮数意义有限，需要另查检测逻辑本身。
        post_merged = _merge_frames(df, _collect_shard_frames(output_csv))
        still_missing = set(_orders_missing_seal(post_merged, retry_ids))
        recovered = len(retry_ids) - len(still_missing)
        recovered_pct = recovered / len(retry_ids) * 100 if retry_ids else 0.0
        print(f"[补下载重试第{round_id}轮] 恢复情况：{recovered}/{len(retry_ids)} 行本轮成功识别到"
              f"封口贴（{recovered_pct:.1f}%），仍有 {len(still_missing)} 行未识别到。"
              + ("恢复率偏低，大概率不是下载问题（真实无贴/OCR检测难点），"
                 "继续调低并发或加重试轮数意义有限。" if recovered_pct < 30 else ""))
        current_ids = still_missing

    # 合并所有轮次的分片 → 最终输出
    print("\n正在合并所有 worker 分片 …")
    merge_ok = merge_shards(df, output_csv, output_excel)
    if merge_ok and all_ok:
        cleanup_shards(output_csv)
    elif not all_ok:
        print("  保留 worker 分片文件，便于下次断点续传。")


if __name__ == '__main__':
    main()
