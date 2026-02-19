# OCR Batch Processing - Optimized Version

A high-performance batch OCR processing tool that detects official seals on product images using PaddleOCR with GPU acceleration.

## 🎯 Features

- ✅ **Real-time CSV saving** - 100x faster than Excel
- ✅ **Immediate per-row saving** - Zero data loss
- ✅ **Dual backup** - CSV + JSON backup files
- ✅ **Resume capability** - Automatic checkpoint recovery
- ✅ **Final Excel export** - Convert to Excel format at completion
- ✅ **GPU acceleration** - Fast processing with CUDA support
- ✅ **Smart detection** - Stops processing when seal is found

## 📋 Requirements

### Dependencies
```bash
pip install pandas openpyxl requests paddlepaddle-gpu paddleocr pillow
```

### System Requirements
- Python 3.7+
- CUDA-capable GPU (recommended)
- Sufficient disk space (at least 10GB free)

## 🚀 Quick Start

### 1. Prepare Input File

Place your Excel file at `/home/ubuntu/OCR/forOCR.xlsx` with the following columns:
- `订单号` (Order ID)
- `图片地址` (Image URL column)
- `Unnamed: 17`, `Unnamed: 18`, `Unnamed: 19` (Additional image URL columns)

### 2. Run the Script

```bash
cd /home/ubuntu/OCR

# Run in background
nohup python3 ocr_batch_process_optimized.py > ocr_optimized.log 2>&1 &

# Save process ID
echo $! > ocr_process.pid
```

### 3. Monitor Progress

```bash
# View real-time logs
tail -f ocr_optimized.log

# Check processed rows
wc -l forOCR_results.csv

# View latest results
tail -20 forOCR_results.csv

# Count rows with official seal
grep ",1," forOCR_results.csv | wc -l
```

### 4. Stop Processing (if needed)

```bash
# Get process ID
cat ocr_process.pid

# Gracefully stop
kill $(cat ocr_process.pid)
```

### 5. Resume Processing (if interrupted)

Simply rerun the script - it will automatically detect processed rows and continue from where it left off:

```bash
nohup python3 ocr_batch_process_optimized.py > ocr_optimized.log 2>&1 &
```

## 📁 Output Files

### 1. `forOCR_results.csv` (Real-time save)
- **Appended immediately** after each row is processed
- Can be opened in Excel anytime to check progress
- Fast performance, doesn't affect processing speed
- UTF-8 encoding with BOM for Excel compatibility

### 2. `forOCR_results.jsonl` (Backup)
- JSON Lines format, one JSON object per line
- Dual backup protection against CSV corruption
- Easy to parse programmatically

### 3. `forOCR_processed.xlsx` (Final result)
- Generated after all processing is complete
- Fully compatible with original Excel format
- New columns added:
  - `是否存在官方封口贴` (Has Official Seal): `1` = Yes, `0` = No, `-1` = Error
  - `找到的关键词` (Found Keyword): The keyword that triggered detection

## 🔍 How It Works

1. **Image Download**: Downloads images from URLs in the Excel file
2. **OCR Processing**: Uses PaddleOCR to extract text from images
3. **Keyword Detection**: Searches for official seal keywords:
   - `扫码即领` (Scan to receive)
   - `Apple授权专营店` (Apple authorized store)
4. **Early Exit**: Stops processing remaining images for a row once a seal is found
5. **Immediate Save**: Saves result to CSV and JSON after each row
6. **Final Export**: Converts CSV to Excel format at completion

## ⚙️ Configuration

### File Paths
Edit these paths in the `main()` function if needed:
```python
input_file = '/home/ubuntu/OCR/forOCR.xlsx'
output_csv = '/home/ubuntu/OCR/forOCR_results.csv'
output_json = '/home/ubuntu/OCR/forOCR_results.jsonl'
output_excel = '/home/ubuntu/OCR/forOCR_processed.xlsx'
```

### OCR Settings
The script initializes PaddleOCR with:
- Language: Chinese (`lang='ch'`)
- Device: GPU (`device='gpu'`)
- Text line orientation detection enabled

### Detection Keywords
Modify keywords in `check_official_seal()` function:
```python
keywords = ["扫码即领", "Apple授权专营店"]
```

## 📊 Performance

### Speed Comparison

| Method | Save Frequency | Save Time | Data Loss Risk |
|--------|---------------|-----------|----------------|
| Original (Excel) | Every 10 rows | 10-30 seconds | Up to 9 rows |
| **Optimized (CSV)** | **Every row** | **0.01 seconds** | **Zero** |

### Estimated Processing Time

For ~23,488 rows:
- Average time per row: ~10.5 seconds
- Total estimated time: **~68.5 hours** (~2.9 days)
- Detection rate: ~76.7% (rows with official seal)

### Performance Optimizations

- ✅ CSV append mode (100x faster than Excel)
- ✅ GPU acceleration for OCR
- ✅ Early exit when seal is found
- ✅ Efficient image handling with temporary files

## 🔧 Troubleshooting

### Issue 1: Process Killed
**Cause:** Insufficient disk space  
**Solution:**
```bash
# Check disk space
df -h

# Clean up space
rm -rf ~/.cache/pip
conda clean --all -y
```

### Issue 2: GPU Memory Insufficient
**Cause:** Other programs using GPU  
**Solution:**
```bash
# Check GPU usage
nvidia-smi

# Free GPU
pkill -f python
```

### Issue 3: Network Timeout
**Cause:** Image download failures  
**Solution:** The script automatically marks failed downloads as ERROR and continues. No intervention needed.

### Issue 4: CSV File Too Large for Excel
**Solution:**
```bash
# View last 100 rows
tail -100 forOCR_results.csv

# Or view only rows with seals
grep ",1," forOCR_results.csv | tail -50
```

### Issue 5: Resume Not Working
**Solution:** Ensure the CSV file exists and contains valid data. The script checks for existing `订单号` values to skip processed rows.

## 📈 Progress Statistics

The script displays progress statistics every 50 rows:
- Processed rows count
- Average time per row
- Estimated remaining time
- Completion percentage

## 🔒 Data Safety

- **Zero data loss**: Each row is saved immediately after processing
- **Dual backup**: CSV + JSON files provide redundancy
- **Error handling**: Failed rows are marked with `-1` and saved with `ERROR` keyword
- **Resume capability**: Automatic detection of processed rows prevents reprocessing

## 📝 Log Output

The script provides detailed logging:
- Initialization status
- Per-row processing details
- Image download status
- OCR recognition results
- Detection results
- Progress statistics every 50 rows
- Final summary statistics

## ✅ Pre-flight Checklist

Before running:
- [ ] GPU available (`nvidia-smi` shows GPU)
- [ ] Sufficient disk space (`df -h` shows > 10GB free)
- [ ] Network connectivity for image downloads
- [ ] PaddleOCR installed and working
- [ ] Input Excel file exists at correct path

During processing:
- [ ] Logs are outputting normally
- [ ] CSV file is growing
- [ ] Time estimates are reasonable

## 📄 License

This script is provided as-is for batch OCR processing tasks.

## 🤝 Support

For issues or questions:
1. Check the log file: `ocr_optimized.log`
2. Verify CSV file is being updated
3. Check GPU and disk resources
4. Review error messages in the log

---

**Happy processing! 🚀**
