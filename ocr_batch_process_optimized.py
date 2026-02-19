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
"""
import pandas as pd
import requests
from paddleocr import PaddleOCR
from PIL import Image
from io import BytesIO
import os
import time
import traceback
import json
from pathlib import Path

# 初始化PaddleOCR - 启用GPU加速
print("=" * 80)
print("正在初始化PaddleOCR (GPU加速)...")
print("=" * 80)
ocr = PaddleOCR(
    use_textline_orientation=True,
    lang='ch',
    device='gpu',
    enable_mkldnn=False
)
print("PaddleOCR初始化完成！\n")

def download_image(url, timeout=10):
    """下载图片"""
    try:
        if pd.isna(url) or url == '':
            return None
        response = requests.get(url, timeout=timeout)
        if response.status_code == 200:
            return Image.open(BytesIO(response.content))
        else:
            print(f"  下载失败 (状态码 {response.status_code}): {url}")
            return None
    except Exception as e:
        print(f"  下载异常: {url}, 错误: {str(e)[:50]}")
        return None

def ocr_image(image, image_id="unknown"):
    """对图片进行OCR识别，返回识别到的文字"""
    if image is None:
        return ""
    
    try:
        temp_path = f"/tmp/temp_ocr_{image_id}_{int(time.time()*1000)}.jpg"
        image.save(temp_path, 'JPEG')
        result = ocr.predict(input=temp_path)
        
        texts = []
        if result and len(result) > 0:
            ocr_result = result[0]
            if hasattr(ocr_result, 'json'):
                res = ocr_result.json.get('res', {})
                rec_texts = res.get('rec_texts', [])
                texts = rec_texts
        
        if os.path.exists(temp_path):
            os.remove(temp_path)
        
        full_text = " ".join(texts)
        return full_text
    except Exception as e:
        print(f"  OCR识别异常: {str(e)[:100]}")
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
        return ""

def check_official_seal(text):
    """检查文字中是否包含官方封口贴关键词"""
    keywords = ["扫码即领", "Apple授权专营店"]
    for keyword in keywords:
        if keyword in text:
            return True, keyword
    return False, None

def save_result_immediately(result_dict, csv_file, json_file):
    """立即保存单行结果到CSV和JSON"""
    # 追加到CSV（速度快）
    df_row = pd.DataFrame([result_dict])
    if not os.path.exists(csv_file):
        df_row.to_csv(csv_file, index=False, mode='w', encoding='utf-8-sig')
    else:
        df_row.to_csv(csv_file, index=False, mode='a', header=False, encoding='utf-8-sig')
    
    # 追加到JSON备份（双重保障）
    with open(json_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result_dict, ensure_ascii=False) + '\n')

def process_row(row, idx, total):
    """处理单行数据"""
    print(f"\n{'='*80}")
    print(f"处理第 {idx}/{total} 行 (订单号: {row.get('订单号', 'N/A')})")
    print('='*80)
    
    image_columns = ['图片地址', 'Unnamed: 17', 'Unnamed: 18', 'Unnamed: 19']
    has_official_seal = False
    found_keyword = None
    processed_images = 0
    
    for col_idx, col in enumerate(image_columns, 1):
        if col not in row.index:
            continue
            
        url = row[col]
        if pd.isna(url) or url == '':
            print(f"  第{col_idx}张图片：为空，跳过")
            continue
        
        print(f"\n  第{col_idx}张图片:")
        print(f"  URL: {url[:80]}...")
        
        image = download_image(url)
        if image is None:
            continue
        
        print(f"  图片尺寸: {image.size}")
        processed_images += 1
        
        image_id = f"row{idx}_col{col_idx}"
        text = ocr_image(image, image_id)
        print(f"  识别文字长度: {len(text)} 字符")
        print(f"  识别文字预览: {text[:150]}{'...' if len(text) > 150 else ''}")
        
        found, keyword = check_official_seal(text)
        if found:
            print(f"  ✓✓✓ 找到官方封口贴！关键词: '{keyword}'")
            has_official_seal = True
            found_keyword = keyword
            break
        
        time.sleep(0.3)
    
    result = 1 if has_official_seal else 0
    print(f"\n【结果】是否存在官方封口贴: {'是 (1)' if result == 1 else '否 (0)'}")
    if found_keyword:
        print(f"【关键词】{found_keyword}")
    print(f"【处理图片】{processed_images}/{len([c for c in image_columns if not pd.isna(row.get(c)) and row.get(c) != ''])}")
    
    return result, found_keyword

def main():
    # 文件路径
    input_file = '/home/ubuntu/OCR/forOCR.xlsx'
    output_csv = '/home/ubuntu/OCR/forOCR_results.csv'  # CSV实时保存
    output_json = '/home/ubuntu/OCR/forOCR_results.jsonl'  # JSON备份
    output_excel = '/home/ubuntu/OCR/forOCR_processed.xlsx'  # 最终Excel
    
    print(f"正在读取Excel文件: {input_file}")
    df = pd.read_excel(input_file)
    
    print(f"总共有 {len(df)} 行数据")
    
    # 检查是否有未完成的处理（从CSV恢复）
    if os.path.exists(output_csv):
        print(f"\n发现已存在的结果文件: {output_csv}")
        df_existing = pd.read_csv(output_csv, encoding='utf-8-sig')
        processed_orders = set(df_existing['订单号'].astype(str))
        print(f"已处理 {len(processed_orders)} 行，继续处理剩余行...")
    else:
        processed_orders = set()
        # 创建CSV文件头
        header_row = df.iloc[0].to_dict()
        header_row['是否存在官方封口贴'] = 0
        header_row['找到的关键词'] = ''
        df_header = pd.DataFrame([header_row])
        df_header.to_csv(output_csv, index=False, mode='w', encoding='utf-8-sig')
        # 删除这一行数据（只是为了创建表头）
        pd.DataFrame(columns=df_header.columns).to_csv(output_csv, index=False, mode='w', encoding='utf-8-sig')
    
    total_rows = len(df)
    start_time = time.time()
    processed_count = len(processed_orders)
    
    for idx, row in df.iterrows():
        # 跳过已处理的行
        order_id = str(row.get('订单号', ''))
        if order_id in processed_orders:
            print(f"\n第 {idx + 1}/{total_rows} 行 (订单号: {order_id}) 已处理，跳过...")
            continue
        
        try:
            result, keyword = process_row(row, idx + 1, total_rows)
            
            # 准备结果行
            result_row = row.to_dict()
            result_row['是否存在官方封口贴'] = result
            result_row['找到的关键词'] = keyword if keyword else ''
            
            # 立即保存（CSV + JSON双重保障）
            save_result_immediately(result_row, output_csv, output_json)
            print(f"  ✓ 已保存到CSV和JSON")
            
        except Exception as e:
            print(f"\n处理第 {idx + 1} 行时出错: {str(e)}")
            print(f"详细错误: {traceback.format_exc()[:300]}")
            
            # 即使出错也保存
            result_row = row.to_dict()
            result_row['是否存在官方封口贴'] = -1
            result_row['找到的关键词'] = 'ERROR'
            save_result_immediately(result_row, output_csv, output_json)
        
        # 每50行显示一次进度统计
        if (idx + 1) % 50 == 0:
            elapsed = time.time() - start_time
            current_processed = idx + 1 - processed_count
            avg_time = elapsed / current_processed if current_processed > 0 else 0
            remaining = (total_rows - idx - 1) * avg_time
            
            print(f"\n{'='*80}")
            print(f"进度统计")
            print(f"已处理: {idx + 1}/{total_rows} 行 ({(idx+1)/total_rows*100:.1f}%)")
            print(f"平均每行耗时: {avg_time:.2f} 秒")
            print(f"预计剩余时间: {remaining/3600:.1f} 小时")
            print(f"{'='*80}\n")
    
    # 最后转换为Excel格式
    print(f"\n{'='*80}")
    print("所有行处理完成！正在生成最终Excel文件...")
    df_final = pd.read_csv(output_csv, encoding='utf-8-sig')
    df_final.to_excel(output_excel, index=False)
    print(f"Excel文件已保存: {output_excel}")
    
    # 统计结果
    count_has_seal = (df_final['是否存在官方封口贴'] == 1).sum()
    count_no_seal = (df_final['是否存在官方封口贴'] == 0).sum()
    count_error = (df_final['是否存在官方封口贴'] == -1).sum()
    
    print(f"\n处理完成！")
    print(f"总共处理: {total_rows} 行")
    print(f"包含官方封口贴: {count_has_seal} 行")
    print(f"不包含官方封口贴: {count_no_seal} 行")
    print(f"处理出错: {count_error} 行")
    print(f"总耗时: {(time.time() - start_time)/3600:.1f} 小时")
    print(f"{'='*80}\n")

if __name__ == '__main__':
    main()
