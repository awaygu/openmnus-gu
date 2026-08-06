#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分析日期选择器的 HTML 结构"""

import re
import sys
from pathlib import Path

# 设置输出编码
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

html_file = Path("debug_html/20260121_134737_date_picker_opened.html")

if not html_file.exists():
    print(f"文件不存在: {html_file}")
    exit(1)

html = html_file.read_text(encoding="utf-8")

# 查找日期选择器模态框
calendar_match = re.search(r'<div class="calendar-modal[^"]*"[^>]*>', html)
if calendar_match:
    print("[OK] 找到日期选择器模态框")
    start_pos = calendar_match.start()
    # 查找模态框的结束位置（简单方法：查找下一个 </div></div></div>）
    end_match = re.search(r'</div>\s*</div>\s*</div>\s*</div>', html[start_pos:start_pos+5000])
    if end_match:
        calendar_html = html[start_pos:start_pos+end_match.end()]
        print(f"日历模态框 HTML 长度: {len(calendar_html)}")
        # 只输出 ASCII 字符
        safe_html = ''.join(c if ord(c) < 128 else '?' for c in calendar_html[:500])
        print(f"前500字符 (ASCII only):\n{safe_html}\n")
else:
    print("[FAIL] 未找到日期选择器模态框")

# 查找所有日期数字
date_days = re.findall(r'<span class="date-d">(\d+)</span>', html)
print(f"\n[OK] 找到 {len(date_days)} 个日期数字")
print(f"前30个日期: {date_days[:30]}")

# 查找日期元素（date-day）
date_day_elements = re.findall(r'<div class="date-day[^"]*"[^>]*>', html)
print(f"\n[OK] 找到 {len(date_day_elements)} 个 date-day 元素")

# 查找可点击的日期元素（包含 onclick 或 role="button"）
clickable_dates = re.findall(r'<div class="date-day[^"]*"[^>]*(?:onclick|role="button")[^>]*>', html)
print(f"[OK] 找到 {len(clickable_dates)} 个可点击的日期元素")

# 查找包含 "30" 的日期元素
date_30 = re.findall(r'<div class="date-day[^"]*"[^>]*>.*?<span class="date-d">30</span>', html, re.DOTALL)
print(f"\n[OK] 找到 {len(date_30)} 个包含 '30' 的日期元素")
if date_30:
    print(f"第一个包含30的元素:\n{date_30[0][:200]}")

