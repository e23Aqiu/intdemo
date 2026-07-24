#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
爱企查公司信息批量查询工具 - GUI 版
功能：上传Excel表格 → 批量查询法定代表人/地址/电话 → 回填保存

依赖：
  pip install DrissionPage pandas openpyxl PyQt5
"""

import sys
import os
import re
import time
import random
import threading
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressBar, QFileDialog, QMessageBox, QGroupBox, QTextEdit,
    QSplitter, QFrame, QCheckBox,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QColor
import pandas as pd
import openpyxl
from DrissionPage import ChromiumPage, ChromiumOptions

from ..browser import get_builtin_chromium_path


# ==================== 配置 ====================
# 需要跳过的关键词
SKIP_KEYWORDS = [
    "个体经营", "无营运信息", "无运输证号", "已补缴",
    "个体",    # 个体经营/个体户
    "个人",    # 个人所有者
    "无",      # 空值或"无"
]

# 表格列名映射（用户表格中的列名）
COMPANY_COL_NAME = "车辆所有人/企业"       # 源列：公司名称
TARGET_COLUMNS = ["负责人/法人代表", "地址", "电话"]  # 目标列（有数据则跳过）
LEGAL_COL_NAME = "负责人/法人代表"          # 目标列：法人
ADDR_COL_NAME = "地址"                      # 目标列：地址
PHONE_COL_NAME = "电话"                     # 目标列：电话
AIQICHA_RESULT_WAIT_SECONDS = 120
AIQICHA_BROWSER_RETRY_DELAY_SECONDS = 1


class AiqichaBrowserRecoveryError(RuntimeError):
    """爱企查浏览器或结果页面失效，需要重建浏览器并重试当前记录。"""


def is_recoverable_aiqicha_browser_error(error):
    if isinstance(error, AiqichaBrowserRecoveryError):
        return True
    message = str(error or "").casefold()
    markers = (
        "browser",
        "target page",
        "page closed",
        "tab closed",
        "connection closed",
        "connection disconnected",
        "connection reset",
        "disconnected",
        "cdp",
        "timeout",
        "浏览器",
        "页面已关闭",
        "连接已断开",
        "超时",
    )
    return any(marker in message for marker in markers)


def has_meaningful_value(value):
    text = str(value).strip()
    return bool(text and text not in {"nan", "NaN", "None", "无", "未查询到公司"})


# ==================== 浏览器控制与信息提取 ====================

def create_browser():
    """使用项目随 Playwright 安装的内置 Chromium 创建浏览器实例。"""
    co = ChromiumOptions()
    co.set_argument('--disable-blink-features=AutomationControlled')
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-infobars')
    co.auto_port()
    # Linux 上用 Linux UA，Windows 用 Windows UA
    if sys.platform.startswith("win"):
        ua = (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        )
    else:
        ua = (
            'Mozilla/5.0 (X11; Linux aarch64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        )
    co.set_user_agent(ua)
    co.set_browser_path(get_builtin_chromium_path())
    page = ChromiumPage(co)
    page.set.timeouts(
        base=AIQICHA_RESULT_WAIT_SECONDS,
        page_load=AIQICHA_RESULT_WAIT_SECONDS,
        script=AIQICHA_RESULT_WAIT_SECONDS,
    )
    return page


def search_company(page, company_name):
    """在爱企查搜索公司"""
    from urllib.parse import quote
    url = f"https://aiqicha.baidu.com/s?q={quote(company_name)}"
    page.get(url)
    # 等待由调用方的 _interruptible_sleep 处理，这里不再 sleep


def extract_info(page):
    """从搜索结果页提取公司信息（纯元素定位）"""
    return extract_by_elements(page)


def extract_from_ssr_json(html):
    """从SSR JSON数据提取"""
    info = {"法定代表人": None, "地址": None, "电话": None}
    if not html:
        return info
    
    json_patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*;?\s*</script>',
        r'window\.__NUXT__\s*=\s*(\{.+?\})\s*;?\s*</script>',
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(\{.+?\})</script>',
    ]
    
    for pattern in json_patterns:
        matches = re.findall(pattern, html, re.DOTALL)
        for match in matches:
            try:
                data = __import__("json").loads(match)
                result = find_company_in_data(data)
                if result and any(result.values()):
                    return result
            except (ValueError, KeyError):
                continue
    return info


def find_company_in_data(obj, depth=0):
    """递归查找公司信息字典"""
    if depth > 15 or not isinstance(obj, dict):
        return {"法定代表人": None, "地址": None, "电话": None}
    
    company_keys = {
        'frName', 'legalPersonName', 'legalRepresentative',
        'regAddress', 'address', 'phoneNum', 'telephone', 'phone',
    }
    matched = company_keys & set(obj.keys())
    company = {}
    
    if len(matched) >= 2:
        field_map = [
            ('frName', '法定代表人'), ('legalPersonName', '法定代表人'),
            ('legalRepresentative', '法定代表人'), ('regAddress', '地址'),
            ('address', '地址'), ('phoneNum', '电话'),
            ('telephone', '电话'), ('phone', '电话'),
        ]
        for key, fname in field_map:
            if key in obj and obj[key]:
                val = str(obj[key]).replace('\\u002F', '/').replace('\\n', '')
                if val and val not in ['-', '--', '暂无']:
                    company[fname] = val
        if any(company.values()):
            return company
    
    for v in obj.values():
        if isinstance(v, list):
            for item in v[:50]:
                if isinstance(item, dict):
                    result = find_company_in_data(item, depth + 1)
                    if any(result.values()):
                        return result
        elif isinstance(v, dict):
            result = find_company_in_data(v, depth + 1)
            if any(result.values()):
                return result
    
    return {"法定代表人": None, "地址": None, "电话": None}


def extract_by_elements(page):
    """通过DrissionPage元素定位提取 - 直接定位第一条结果卡片的各个字段"""
    info = {"法定代表人": None, "地址": None, "电话": None}
    
    try:
        # 根据HTML结构精确定位第一条搜索结果
        # 结构参考：
        # 电话: .telephone-lists-wrap > .item-first-data > span.first-data
        # 地址: .info-item-address (整个区域的文本)
        # 法人: .legal-txt

        # 1. 提取电话
        tel = None
        first_data = page.ele('css:.first-data', timeout=1)
        if first_data:
            span1 = first_data.ele('tag:span', timeout=0.5)
            if span1:
                span2_list = span1.eles('tag:span', timeout=0.5)
                raw = (span2_list[0].text if span2_list else span1.text) or ''
            else:
                raw = first_data.text or ''
            raw = raw.strip()
            if raw:
                # 如果含有省略号，只取省略号之后的部分（完整号码）
                if '...' in raw:
                    raw = raw.split('...')[-1].strip()
                # 再用空白/换行分割，只取第一段
                first_num = re.split(r'[\s\n]+', raw)[0].strip()
                if first_num:
                    tel = first_num
        
        if tel:
            info["电话"] = tel
        
        # 2. 提取地址 - .info-item-address 整个元素的文本
        addr_items = page.eles('css:.info-item-address')
        if addr_items and len(addr_items) > 0:
            addr_elem = addr_items[0]
            # 直接取元素文本（包含"地址："标签和实际地址）
            addr_text = addr_elem.text or ""
            if addr_text:
                # 去掉"地址："或"地址:"前缀
                addr_clean = re.sub(r'^地址\s*[:：]\s*', '', addr_text).strip()
                if addr_clean and len(addr_clean) >= 4:
                    info["地址"] = addr_clean
        
        # 3. 提取法定代表人/负责人 - .legal-txt
        legal_txts = page.eles('css:.legal-txt')
        if legal_txts and len(legal_txts) > 0:
            legal_text = legal_txts[0].text.strip()
            if legal_text:
                # 可能是 "法定代表人：xxx" 或 "负责人：xxx" 或纯名字
                legal_clean = re.sub(r'^(法定[代表]人|负责?人)\s*[:：]\s*', '', legal_text).strip()
                info["法定代表人"] = legal_clean or legal_text
        
    except Exception as e:
        print(f"[DEBUG] 元素定位异常: {e}")
    
    # 填充空值
    for k in info:
        if not info[k]:
            info[k] = None
    
    return info


def extract_from_html(html_content):
    """HTML正则兜底"""
    info = {"法定代表人": None, "地址": None, "电话": None}
    if not html_content:
        return info
    
    patterns_legal = [
        r'frName["\s:=]+["\u201c]?([^"\u201d,\n}{]{2,15})',
        r'"frName"\s*:\s*"([^"]+)"',
        r'"legalPersonName"\s*:\s*"([^"]+)"',
        r'法定[代表]人[：:\s]*([^\n<>"\u201c\u201d]{2,20}?)(?=\s*(?:地址|电话|成立|经营|<|$))',
    ]
    for p in patterns_legal:
        m = re.search(p, html_content)
        if m:
            v = m.group(1).strip().strip('",\';:\u201c\u201d ')
            if 0 < len(v) <= 20 and not v.isdigit() and '<' not in v and '{' not in v:
                info["法定代表人"] = v
                break
    
    patterns_addr = [
        r'regAddress["\s:=]+["\u201c]?([^"\u201d\n}{]{8,200})',
        r'"regAddress"\s*:\s*"([^"]{8,200})"',
        r'地\s*址[：:\s]*([^\n<>"]{8,100}?)(?=\s*(?:(?:电话|邮编|传真|邮箱|法人|成立|经营)|$))',
    ]
    for p in patterns_addr:
        m = re.search(p, html_content)
        if m:
            v = m.group(1).replace('\\u002F', '/').replace('\n', '').replace('\\n', '').strip()
            if 8 < len(v) < 300 and '<' not in v:
                info["地址"] = v
                break
    
    patterns_phone = [
        r'phoneNum["\s:=]+["\u201c]?([\d\-()]{7,20})',
        r'"phoneNum"\s*:\s*"([^"]+)"',
        r'(?:电话|联系电话)[：:\s]*(\d[\d\-()]{6,19}\d)',
        r'(1[3-9]\d{9})',
        r'(\d{3,4}[-–]\d{7,8})',
    ]
    for p in patterns_phone:
        m = re.search(p, html_content)
        if m:
            v = m.group(1).strip()
            if re.match(r'^[\d\-()]+$', v) and len(v) >= 7:
                info["电话"] = v
                break
    
    return info


def check_no_results(page):
    """检测爱企查搜索结果页是否显示「0条结果」
    核心判断：class="total" 元素的文本是否为 '0'。
    辅以 URL 和关键词兜底。
    """
    # 方法1（最可靠，优先）：直接读取 class="total" 元素的文本
    try:
        total_ele = page.ele('css:.total', timeout=1.5)
        if total_ele:
            t = total_ele.text.strip()
            if t == '0':
                return True
    except Exception:
        pass

    # 方法2：URL 中出现无结果特征
    try:
        url = page.url or ""
        if 'q=' in url and 'total=0' in url:
            return True
    except Exception:
        pass

    # 方法3：页面关键词兜底
    try:
        text = page.text or ""
        html = page.html or ""
        if '0家' in text or '0 家' in text:
            return True
        no_result_keywords = [
            "抱歉，没有找到相关结果",
            "未找到相关结果",
            "没有找到相关",
            "暂无相关结果",
            "无匹配结果",
            "搜索结果为空",
            "暂无结果",
        ]
        for kw in no_result_keywords:
            if kw in text:
                return True
            if kw in html:
                return True
    except Exception:
        pass

    return False


# ==================== 查询工作线程 ====================

class QueryWorker(QThread):
    """后台查询线程"""
    # 信号
    log_signal = pyqtSignal(str)           # 日志消息
    progress_signal = pyqtSignal(int, int)       # 当前进度, 总数
    row_done_signal = pyqtSignal(int, dict)      # 行号, {列名: 值}
    login_required_signal = pyqtSignal()         # 需要登录
    pause_signal = pyqtSignal()                   # 遇到验证码，通知主线程暂停UI
    resume_signal = pyqtSignal()                  # 验证码已处理，通知主线程恢复UI
    login_confirmed_signal = pyqtSignal()        # 用户处理完，继续执行
    finished_signal = pyqtSignal(bool)           # 完成(bool=是否有错误)
    retry_signal = pyqtSignal(str, str)           # 重试类型, 原因
    
    def __init__(self, df, company_col):
        super().__init__()
        self.df = df
        self.company_col = company_col
        self.page = None
        self._should_stop = False
        self._login_wait = threading.Event()
        # 暂停控制：set=运行中，clear=已暂停
        self._pause_event = threading.Event()
        self._pause_event.set()   # 默认处于运行状态

    def stop(self):
        self._should_stop = True
        self._pause_event.set()   # 确保暂停状态下也能响应停止
        self._login_wait.set()    # 确保登录等待/验证码等待也能立即中断

    def close_browser(self):
        """关闭浏览器（线程安全，可多次调用）"""
        if self.page is not None:
            try:
                self.page.quit()
            except Exception:
                pass
            self.page = None

    def pause(self):
        """暂停查询"""
        self._pause_event.clear()

    def resume(self):
        """恢复查询"""
        self._pause_event.set()

    def _interruptible_sleep(self, seconds, interval=0.2):
        """
        可中断的睡眠：每 interval 秒检查一次暂停/停止状态。
        暂停时阻塞等待，停止时立即返回 False，正常结束返回 True。
        """
        elapsed = 0.0
        while elapsed < seconds:
            if self._should_stop:
                return False
            # 暂停状态：阻塞直到恢复
            if not self._pause_event.is_set():
                self._pause_event.wait()
                if self._should_stop:
                    return False
            time.sleep(interval)
            elapsed += interval
        return True

    def _wait_for_results(self, timeout=AIQICHA_RESULT_WAIT_SECONDS):
        """
        等待搜索结果页面加载完成（结果区域或"无结果"提示出现）。
        优先用 DrissionPage 元素检测（更快更准），HTML 兜底。
        最多等待 timeout 秒，每 0.3 秒检查一次。
        返回 True 表示检测到页面已加载，False 表示超时。
        """
        elapsed = 0.0
        interval = 0.3
        while elapsed < timeout:
            if self._should_stop:
                return False

            try:
                # 方法1（最快最准）：直接检测页面关键元素
                # .total 元素出现，说明搜索已完成（无论是否有结果）
                total_ele = self.page.ele('css:.total', timeout=0.5)
                if total_ele:
                    return True

                # 有结果时的特征元素
                if self.page.ele('css:.zx-table-item-wrap', timeout=0.3):
                    return True
                if self.page.ele('css:.result-wrap', timeout=0.3):
                    return True

                # 无结果时的特征文本
                text = self.page.text or ""
                if '未找到' in text or '暂无' in text or '抱歉' in text:
                    return True
                if '0家' in text or '0 家' in text:
                    return True

                # URL 变化也能说明页面已加载
                url = self.page.url or ""
                if 'aiqicha.baidu.com/s?' in url and 'search' not in url:
                    # 已经在搜索结果页
                    html = self.page.html or ""
                    if len(html) > 10000:
                        return True
            except Exception as exc:
                if is_recoverable_aiqicha_browser_error(exc):
                    raise AiqichaBrowserRecoveryError(str(exc)) from exc

            # 暂停状态也阻塞
            if not self._pause_event.is_set():
                self._pause_event.wait()
                if self._should_stop:
                    return False
            time.sleep(interval)
            elapsed += interval
        return False

    def confirm_login(self):
        """主线程调用：用户点击了'继续执行'"""
        self._login_wait.set()

    def _wait_for_login_confirmation(self, timeout=1800):
        self._login_wait.clear()
        self.login_required_signal.emit()
        deadline = time.monotonic() + timeout
        while not self._should_stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.log_signal.emit("❌ 等待登录超时")
                return False
            if self._login_wait.wait(timeout=min(0.2, remaining)):
                return not self._should_stop
        return False

    def _start_browser_session(self, recovery=False):
        """持续创建爱企查浏览器，直到可用、登录超时或用户主动停止。"""
        attempt = 0
        while not self._should_stop:
            attempt += 1
            try:
                self.close_browser()
                self.log_signal.emit(
                    "♻️ 正在重新启动爱企查浏览器..."
                    if recovery
                    else "🚀 正在启动浏览器..."
                )
                self.page = create_browser()
                self.log_signal.emit("✅ 浏览器已启动")
                self.log_signal.emit("📌 正在打开爱企查首页...")
                self.page.get("https://aiqicha.baidu.com")
                if not self._interruptible_sleep(2):
                    return False
                self.log_signal.emit(
                    "⏳ 请在浏览器中登录爱企查，然后点击「继续执行」..."
                )
                return self._wait_for_login_confirmation()
            except Exception as exc:
                reason = str(exc).strip() or exc.__class__.__name__
                self.retry_signal.emit(
                    "aiqicha_browser_start",
                    f"爱企查浏览器启动第 {attempt} 次失败：{reason[:120]}",
                )
                self.log_signal.emit(
                    "⚠️ 爱企查浏览器启动失败，将继续重试"
                    f"（已尝试 {attempt} 次，无次数上限）：{reason[:120]}"
                )
                self.close_browser()
                if not self._interruptible_sleep(
                    AIQICHA_BROWSER_RETRY_DELAY_SECONDS
                ):
                    return False
        return False

    def _ensure_browser_available(self):
        page = self.page
        if page is None:
            raise AiqichaBrowserRecoveryError("爱企查浏览器不存在")
        try:
            states = getattr(page, "states", None)
            if states is not None and not states.is_alive:
                raise AiqichaBrowserRecoveryError("爱企查页面已关闭")
            browser = getattr(page, "browser", None)
            browser_states = getattr(browser, "states", None)
            if browser_states is not None and not browser_states.is_alive:
                raise AiqichaBrowserRecoveryError("爱企查浏览器已关闭")
        except AiqichaBrowserRecoveryError:
            raise
        except Exception as exc:
            raise AiqichaBrowserRecoveryError(str(exc)) from exc
    
    def _check_captcha(self):
        """检测页面是否出现验证码/安全验证"""
        try:
            url = self.page.url
            html = self.page.html
            text = self.page.text if hasattr(self.page, 'text') else ''
            
            # 检测URL是否跳转到安全验证页
            captcha_indicators = [
                'captcha',
                'verify',
                '安全验证',
                '验证码',
                '人机识别',
                '滑块验证',
                'vcode',
                'passport.baidu.com',
            ]
            
            for indicator in captcha_indicators:
                if indicator in str(url).lower():
                    return True
                if indicator in html:
                    # 排除正常页面中的"验证"等词（如"已验证"）
                    if indicator in ['安全验证', '人机识别', '滑块验证']:
                        return True
            
            # 额外检查：页面内容过少（可能被拦截到验证页）
            if len(html) < 5000 and ('验证' in html or '安全' in html or '请完成' in html):
                return True
            
            return False
        except Exception as e:
            if is_recoverable_aiqicha_browser_error(e):
                raise AiqichaBrowserRecoveryError(str(e)) from e
            self.log_signal.emit(f"  ⚠️ 验证码检测异常: {e}")
            return False
    
    def run(self):
        try:
            # 1. 创建浏览器并等待登录
            if not self._start_browser_session():
                self.finished_signal.emit(False)
                return

            if self._should_stop:
                self.finished_signal.emit(False)
                return
            
            self.log_signal.emit("✅ 开始查询！\n")
            self._interruptible_sleep(1)
            
            # 3. 逐条查询
            total = 0
            done = 0
            captcha_count = 0  # 验证码出现次数
            
            for idx, row in self.df.iterrows():
                if self._should_stop:
                    break

                # 暂停检查：暂停状态下阻塞，直到恢复或停止
                if not self._pause_event.is_set():
                    self.log_signal.emit("⏸️ 已暂停，等待继续...")
                    self._pause_event.wait()   # 阻塞直到 resume()
                    if self._should_stop:
                        break
                    self.log_signal.emit("▶️ 已恢复，继续查询...")

                company_name = str(row[self.company_col]).strip()
                
                # 检查是否需要跳过
                should_skip = False
                skip_reason = ""
                for kw in SKIP_KEYWORDS:
                    if kw in company_name:
                        should_skip = True
                        skip_reason = f"[跳过 - {kw}]"
                        break
                
                if not company_name or company_name == 'nan':
                    should_skip = True
                    skip_reason = "[跳过 - 空]"
                
                # 三个目标字段全部已有数据时才跳过；部分缺失仍继续补齐。
                if not should_skip:
                    completed_fields = [
                        target_col for target_col in TARGET_COLUMNS
                        if has_meaningful_value(row.get(target_col, ''))
                    ]
                    if len(completed_fields) == len(TARGET_COLUMNS):
                        should_skip = True
                        skip_reason = "[跳过 - 法人/地址/电话均已有数据]"
                
                total += 1
                
                if should_skip:
                    self.log_signal.emit(f"  [{total}] {company_name[:20]} {skip_reason}")
                    self.progress_signal.emit(total, len(self.df))
                    continue
                
                # 查询
                self.log_signal.emit(f"  [{total}] 🔍 查询：{company_name}")
                browser_restarts = 0
                while not self._should_stop:
                    try:
                        self._ensure_browser_available()
                        search_company(self.page, company_name)

                        # 搜索结果页面最多等待 2 分钟，可暂停或停止。
                        loaded = self._wait_for_results(
                            timeout=AIQICHA_RESULT_WAIT_SECONDS
                        )
                        if not loaded:
                            if self._should_stop:
                                break
                            raise AiqichaBrowserRecoveryError(
                                "等待爱企查搜索结果超时"
                                f"（{AIQICHA_RESULT_WAIT_SECONDS}秒）"
                            )
                        self._ensure_browser_available()

                        # 检测是否遇到验证码/安全验证
                        if self._check_captcha():
                            captcha_count += 1
                            self.retry_signal.emit(
                                "aiqicha_captcha",
                                f"爱企查触发第 {captcha_count} 次安全验证",
                            )
                            self.log_signal.emit(
                                f"  ⚠️ 检测到验证码（第{captcha_count}次），"
                                "请在浏览器中完成验证后点击「继续」"
                            )

                            # 通过暂停机制等待用户处理验证码
                            self._pause_event.clear()
                            self.pause_signal.emit()
                            self._pause_event.wait()
                            if self._should_stop:
                                break
                            self.resume_signal.emit()

                            self.log_signal.emit("  ✅ 验证码已处理，继续查询...")

                        # 先检测是否为0结果（非常快，只读一个元素）
                        # 如果是0结果，直接跳过耗时的 extract_info
                        if check_no_results(self.page):
                            self.log_signal.emit(f"  [{total}] ⚠️ 未查询到公司")
                            row_data = {}
                            if not has_meaningful_value(
                                row.get(LEGAL_COL_NAME, '')
                            ):
                                row_data[LEGAL_COL_NAME] = "未查询到公司"
                        else:
                            # 有搜索结果，执行完整的信息提取
                            info = extract_info(self.page)
                            self._ensure_browser_available()
                            # 回填数据
                            row_data = {}
                            if (
                                info.get("法定代表人")
                                and not has_meaningful_value(
                                    row.get(LEGAL_COL_NAME, '')
                                )
                            ):
                                row_data[LEGAL_COL_NAME] = info["法定代表人"]
                            if (
                                info.get("地址")
                                and not has_meaningful_value(
                                    row.get(ADDR_COL_NAME, '')
                                )
                            ):
                                row_data[ADDR_COL_NAME] = info["地址"]
                            if (
                                info.get("电话")
                                and not has_meaningful_value(
                                    row.get(PHONE_COL_NAME, '')
                                )
                            ):
                                row_data[PHONE_COL_NAME] = info["电话"]

                            status = (
                                f"  [{total}] ✅ 法人:{info['法定代表人'] or '-'}"
                                f" | 地址:{info['地址'] or '-'}"
                                f" | 电话:{info['电话'] or '-'}"
                            )
                            self.log_signal.emit(status)

                        self.row_done_signal.emit(idx, row_data)
                        break

                    except Exception as e:
                        if not is_recoverable_aiqicha_browser_error(e):
                            self.log_signal.emit(f"  [{total}] ❌ 出错: {e}")
                            break
                        browser_restarts += 1
                        reason = str(e).strip() or e.__class__.__name__
                        self.retry_signal.emit(
                            "aiqicha_browser",
                            f"爱企查浏览器异常重启：{reason[:120]}",
                        )
                        self.log_signal.emit(
                            "  ⚠️ 爱企查浏览器或结果页失效，正在重启并重试"
                            f"当前记录（第 {browser_restarts} 次，无次数上限）"
                        )
                        if not self._start_browser_session(recovery=True):
                            if self._should_stop:
                                break
                            raise RuntimeError(
                                "爱企查浏览器恢复期间等待登录超时"
                            )

                done += 1
                self.progress_signal.emit(total, len(self.df))

                # 随机延迟，避免请求过快触发验证码（可中断）
                delay = random.uniform(2, 5)
                if not self._interruptible_sleep(delay):
                    break

            # 判断是正常完成还是被停止
            if self._should_stop:
                self.log_signal.emit(f"\n⏹️ 已停止，共处理 {total} 条")
                self.finished_signal.emit(False)
            else:
                self.log_signal.emit(f"\n🎉 查询完成！共处理 {total} 条")
                self.finished_signal.emit(True)
            
        except Exception as e:
            self.log_signal.emit(f"❌ 致命错误: {e}")
            self.finished_signal.emit(False)
        finally:
            # 无论正常完成、停止还是异常，都关闭浏览器
            self.close_browser()
            self.log_signal.emit("🔒 浏览器已关闭")


# ==================== 主窗口 ====================

class MainWindow(QMainWindow):
    def __init__(self, stats_recorder=None):
        super().__init__()
        self._stats_recorder = stats_recorder
        self.setWindowTitle("爱企查批量查询工具")
        self.setMinimumSize(1100, 700)

        self.df = None
        self.file_path = None
        self.worker = None
        self._is_paused = False   # 当前是否处于暂停状态
        self._is_saved = True   # 是否已保存（无未保存变更）

        self._setup_ui()
    
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # ===== 顶部操作区 =====
        top_group = QGroupBox("操作")
        top_layout = QHBoxLayout(top_group)
        top_layout.setSpacing(8)

        # 按钮统一样式（用 em 相对单位，随系统 DPI 自适应）
        btn_style_default = (
            "font-size: 14px; font-weight: bold;"
            "padding: 6px 12px;"
        )
        btn_style_green = (
            "font-size: 14px; font-weight: bold;"
            "background-color: #4CAF50; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )
        btn_style_blue = (
            "font-size: 14px; font-weight: bold;"
            "background-color: #2196F3; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )
        btn_style_red = (
            "font-size: 14px; font-weight: bold;"
            "background-color: #f44336; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )

        btn_style_orange = (
            "font-size: 14px; font-weight: bold;"
            "background-color: #FF9800; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )

        self.upload_btn = QPushButton("📁 上传 Excel 表格")
        self.upload_btn.setMinimumHeight(44)
        self.upload_btn.setStyleSheet(btn_style_default)
        self.upload_btn.clicked.connect(self._upload_file)

        self.start_btn = QPushButton("🌐 打开浏览器")
        self.start_btn.setMinimumHeight(44)
        self.start_btn.setEnabled(False)
        self.start_btn.setStyleSheet(btn_style_green)
        self.start_btn.clicked.connect(self._start_query)

        self.continue_btn = QPushButton("▶️ 开始查询")
        self.continue_btn.setMinimumHeight(44)
        self.continue_btn.setEnabled(False)
        self.continue_btn.setStyleSheet(btn_style_blue)
        self.continue_btn.clicked.connect(self._confirm_login)

        self.pause_btn = QPushButton("⏸️ 暂停")
        self.pause_btn.setMinimumHeight(44)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setStyleSheet(btn_style_orange)
        self.pause_btn.clicked.connect(self._toggle_pause)

        self.stop_btn = QPushButton("⏹️ 停止")
        self.stop_btn.setMinimumHeight(44)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(btn_style_red)
        self.stop_btn.clicked.connect(self._stop_query)

        self.save_btn = QPushButton("💾 保存结果")
        self.save_btn.setMinimumHeight(44)
        self.save_btn.setEnabled(False)
        self.save_btn.setStyleSheet(btn_style_default)
        self.save_btn.clicked.connect(self._save_result)

        # 按钮等宽伸展，随窗口宽度自动分配
        for btn in (self.upload_btn, self.start_btn, self.continue_btn,
                    self.pause_btn, self.stop_btn, self.save_btn):
            top_layout.addWidget(btn, stretch=1)

        main_layout.addWidget(top_group)

        # ===== 保存选项行（勾选框，靠右对齐） =====
        save_opt_layout = QHBoxLayout()
        save_opt_layout.addStretch(1)

        self.new_file_chk = QCheckBox("📄 生成新表格")
        self.new_file_chk.setToolTip(
            "勾选后：点击「保存结果」时另存为新文件\n"
            "未勾选：点击「保存结果」时直接回写到原表格（不影响原有数据）"
        )
        self.new_file_chk.setStyleSheet("font-size: 13px; font-weight: bold;")
        save_opt_layout.addWidget(self.new_file_chk)

        self.auto_save_chk = QCheckBox("🔄 自动保存")
        self.auto_save_chk.setToolTip(
            "勾选后：停止、查询完成时自动回写到原表格（不影响原有数据）\n"
            "同时禁用「生成新表格」勾选框"
        )
        self.auto_save_chk.setChecked(True)
        self.auto_save_chk.setStyleSheet("font-size: 13px; font-weight: bold;")
        self.auto_save_chk.stateChanged.connect(self._on_auto_save_changed)
        # 触发一次联动，禁用「生成新表格」
        self._on_auto_save_changed(Qt.Checked)
        save_opt_layout.addWidget(self.auto_save_chk)

        main_layout.addLayout(save_opt_layout)

        # ===== 浏览器信息 =====
        browser_layout = QHBoxLayout()
        browser_label = QLabel("🌐 使用浏览器：内置 Chromium")
        browser_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #2d7d46;")
        browser_layout.addWidget(browser_label)
        browser_layout.addStretch(1)

        main_layout.addLayout(browser_layout)

        # ===== 文件信息 =====
        file_info_layout = QHBoxLayout()
        self.file_label = QLabel("未上传文件")
        self.file_label.setStyleSheet("color: gray;")
        self.row_count_label = QLabel("")
        self.row_count_label.setStyleSheet("color: gray;")
        file_info_layout.addWidget(QLabel("📂 文件:"))
        file_info_layout.addWidget(self.file_label, stretch=1)
        file_info_layout.addWidget(self.row_count_label)

        # 表格状态实时检测标签
        self.file_status_label = QLabel("📊 表格状态：未上传")
        self.file_status_label.setStyleSheet("color: gray; font-weight: bold; font-size: 13px;")
        self.file_status_label.setMinimumWidth(120)
        file_info_layout.addWidget(self.file_status_label)
        main_layout.addLayout(file_info_layout)

        # ===== 进度条 =====
        progress_layout = QHBoxLayout()
        progress_layout.addWidget(QLabel("进度:"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m (%p%)")
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar, stretch=1)
        self.status_label = QLabel("就绪")
        self.status_label.setMinimumWidth(140)
        progress_layout.addWidget(self.status_label)
        main_layout.addLayout(progress_layout)

        # ===== 分割：表格 + 日志 =====
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)  # 禁止完全折叠

        # --- 表格 ---
        table_group = QGroupBox("数据预览（可直接编辑单元格）")
        table_layout = QVBoxLayout(table_group)
        table_layout.setContentsMargins(4, 4, 4, 4)
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.verticalHeader().setVisible(False)
        # 用户编辑单元格后同步到 DataFrame
        self.table.cellChanged.connect(self._on_cell_changed)
        table_layout.addWidget(self.table)
        splitter.addWidget(table_group)

        # --- 日志 ---
        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(4, 4, 4, 4)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        # 移除 setMaximumHeight，让 splitter 控制比例
        self.log_text.setFont(QFont("Consolas", 10))
        log_layout.addWidget(self.log_text)
        splitter.addWidget(log_group)

        # 初始比例：表格占 65%，日志占 35%（随窗口拉伸同步变化）
        splitter.setStretchFactor(0, 65)
        splitter.setStretchFactor(1, 35)
        splitter.setSizes([480, 260])

        main_layout.addWidget(splitter, stretch=1)

        # ===== 表格状态定时检测（每 2 秒） =====
        self._file_check_timer = QTimer(self)
        self._file_check_timer.timeout.connect(self._check_file_status)
        self._file_check_timer.start(2000)

    def _check_file_status(self):
        """检测上传的表格是否被其他程序打开"""
        if not self.file_path or not os.path.exists(self.file_path):
            self.file_status_label.setText("📊 表格状态：未上传")
            self.file_status_label.setStyleSheet("color: gray; font-weight: bold; font-size: 13px;")
            return

        if sys.platform.startswith("win"):
            locked = self._is_file_locked_win(self.file_path)
        else:
            locked = self._is_file_locked_unix(self.file_path)

        if locked:
            self.file_status_label.setText("📊 表格正在被打开，请先关闭表格再操作")
            self.file_status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 13px;")
        else:
            self.file_status_label.setText("📊 表格状态正常")
            self.file_status_label.setStyleSheet("color: #4CAF50; font-weight: bold; font-size: 13px;")

    @staticmethod
    def _is_file_locked_win(filepath):
        """Windows: 尝试独占打开文件，失败则说明被占用"""
        try:
            import msvcrt
            fd = os.open(filepath, os.O_RDWR | os.O_EXCL)
            os.close(fd)
            return False
        except OSError:
            # 文件被其他进程占用
            return True

    @staticmethod
    def _is_file_locked_unix(filepath):
        """Linux: 通过 lsof 检查是否有进程打开文件"""
        import subprocess
        try:
            result = subprocess.run(
                ["lsof", "-t", filepath],
                capture_output=True, timeout=2
            )
            return bool(result.stdout.strip())
        except Exception:
            return False
    
    def _log(self, msg):
        """追加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {msg}")
        # 自动滚动到底面
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())
    
    def _upload_file(self):
        """上传Excel文件"""
        global COMPANY_COL_NAME
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Excel 表格",
            "", "Excel 文件 (*.xlsx *.xls);;所有文件 (*)"
        )
        if not path:
            return
        
        self.file_path = path
        self.file_label.setText(os.path.basename(path))
        self.file_label.setStyleSheet("color: #333; font-weight: bold;")
        
        try:
            # 读取所有sheet
            if path.endswith('.xls'):
                self.df = pd.read_excel(path, engine='xlrd')
            else:
                self.df = pd.read_excel(path)
            
            rows, cols = self.df.shape
            self.row_count_label.setText(f"共 {rows} 行 × {cols} 列")
            self._log(f"📂 已加载: {os.path.basename(path)} ({rows}行×{cols}列)")
            
            # 显示列名
            cols_str = ", ".join(str(c) for c in self.df.columns.tolist())
            self._log(f"   列名: {cols_str}")
            
            # 检查目标列是否存在
            if COMPANY_COL_NAME not in self.df.columns:
                # 尝试模糊匹配
                found = False
                for col in self.df.columns:
                    col_str = str(col)
                    if "车辆所有人" in col_str or "企业" in col_str or "公司" in col_str:
                        COMPANY_COL_NAME = col_str
                        self._log(f"   ⚠️ 未找到'{COMPANY_COL_NAME}'列，使用模糊匹配: '{col_str}'")
                        found = True
                        break
                if not found:
                    QMessageBox.warning(
                        self, "列名不匹配",
                        f"未找到「{COMPANY_COL_NAME}」列！\n\n当前表格列名:\n" +
                        "\n".join(f"  - {c}" for c in self.df.columns)
                    )
                    return

            # 目标列不存在时自动补齐，确保查询结果能够进入 DataFrame 并保存。
            for target_col in TARGET_COLUMNS:
                if target_col not in self.df.columns:
                    self.df[target_col] = ""
                    self._log(f"   ➕ 已自动新增目标列：{target_col}")

            if path.lower().endswith('.xls'):
                # openpyxl 无法安全原地写回旧版 xls，强制走另存为 xlsx。
                self.auto_save_chk.setChecked(False)
                self.new_file_chk.setChecked(True)
                self._log("   ⚠️ 旧版 .xls 将通过“生成新表格”另存为 .xlsx，不原地覆盖。")
            
            self._populate_table()
            self.start_btn.setEnabled(True)
            self.save_btn.setEnabled(True)
            self._is_saved = False   # 新文件尚未保存
            self._update_title()

        except Exception as e:
            QMessageBox.critical(self, "读取失败", f"无法读取文件:\n{e}")
            self._log(f"❌ 读取失败: {e}")
    
    def _populate_table(self):
        """将 DataFrame 填充到表格控件（可编辑）"""
        # 填充时先断开 cellChanged，避免触发同步逻辑
        self.table.cellChanged.disconnect(self._on_cell_changed)

        df_display = self.df
        rows, cols = df_display.shape
        self.table.setRowCount(rows)
        self.table.setColumnCount(cols)
        self.table.setHorizontalHeaderLabels([str(c) for c in df_display.columns])

        for r in range(rows):
            for c in range(cols):
                val = df_display.iloc[r, c]
                text = str(val) if pd.notna(val) else ""
                item = QTableWidgetItem(text)
                # 全部允许编辑（不再屏蔽 ItemIsEditable）

                # 高亮需要跳过的行
                company_val = str(self.df.iloc[r].get(COMPANY_COL_NAME, ""))
                for kw in SKIP_KEYWORDS:
                    if kw in company_val:
                        item.setBackground(QColor(255, 235, 238))  # 浅红色背景
                        break

                self.table.setItem(r, c, item)

        self.table.resizeColumnsToContents()

        # 高亮目标列表头
        target_cols = [COMPANY_COL_NAME, LEGAL_COL_NAME, ADDR_COL_NAME, PHONE_COL_NAME]
        for c, col_name in enumerate(df_display.columns):
            if str(col_name) in target_cols:
                header = self.table.horizontalHeaderItem(c)
                if header:
                    header.setBackground(QColor(144, 238, 144))  # 浅绿色

        # 重新连接信号
        self.table.cellChanged.connect(self._on_cell_changed)

    def _on_cell_changed(self, row, col):
        """用户手动编辑单元格 → 同步到底层 DataFrame"""
        if self.df is None:
            return
        if row >= len(self.df) or col >= len(self.df.columns):
            return

        item = self.table.item(row, col)
        new_text = item.text() if item else ""
        col_name = self.df.columns[col]

        # 写入 DataFrame（先确保列类型为 object，避免 FutureWarning）
        if self.df[col_name].dtype != object:
            self.df[col_name] = self.df[col_name].astype(object)
        self.df.at[row, col_name] = new_text
        # 标记为有未保存变更
        self._is_saved = False
        self._update_title()
    
    def _start_query(self):
        """开始查询"""
        if self.df is None:
            QMessageBox.warning(self, "提示", "请先上传 Excel 文件")
            return
        
        reply = QMessageBox.question(
            self, "确认开始",
            "查询流程：\n"
            "1. 将自动弹出浏览器窗口\n"
            "2. 请在浏览器中登录爱企查账号\n"
            "3. 登录后点击「开始查询」按钮\n"
            "4. 程序自动逐条查询并回填\n\n"
            "是否开始？",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        
        # 清空日志
        self.log_text.clear()

        # 重置暂停状态
        self._is_paused = False

        # 更新UI状态
        self.start_btn.setEnabled(False)
        self.upload_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("⏸️ 暂停")
        self.progress_bar.setValue(0)
        self.status_label.setText("准备中...")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        
        # 启动工作线程
        self._log("🌐 使用浏览器：内置 Chromium")
        self.worker = QueryWorker(self.df.copy(), COMPANY_COL_NAME)
        self.worker.log_signal.connect(self._log)
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.row_done_signal.connect(self._on_row_done)
        self.worker.login_required_signal.connect(self._on_login_required)
        self.worker.pause_signal.connect(self._on_captcha_pause)
        self.worker.resume_signal.connect(self._on_captcha_resume)
        self.worker.login_confirmed_signal.connect(lambda: None)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()
    
    def _on_login_required(self):
        """收到登录请求信号"""
        self.continue_btn.setEnabled(True)
        self.start_btn.setEnabled(False)
        self.status_label.setText("⚠️ 需要处理，请操作浏览器后点击「开始查询」")
        self.status_label.setStyleSheet("color: #FF9800; font-weight: bold; font-size: 13px;")
        # 让"开始查询"按钮更醒目
        self.continue_btn.setStyleSheet(
            "font-size: 14px; font-weight: bold;"
            "background-color: #FF9800; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )

    def _on_captcha_pause(self):
        """遇到验证码：暂停按钮变为「继续」，提示用户操作"""
        self._is_paused = True
        self.pause_btn.setText("▶️ 继续")
        self.pause_btn.setStyleSheet(
            "font-size: 14px; font-weight: bold;"
            "background-color: #4CAF50; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )
        self.status_label.setText("⚠️ 请在浏览器中完成验证，然后点击「继续」")
        self.status_label.setStyleSheet("color: #FF9800; font-weight: bold; font-size: 13px;")

    def _on_captcha_resume(self):
        """验证码已处理：恢复暂停按钮状态"""
        self._is_paused = False
        self.pause_btn.setText("⏸️ 暂停")
        self.pause_btn.setStyleSheet(
            "font-size: 14px; font-weight: bold;"
            "background-color: #FF9800; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )
        self.status_label.setText("查询中...")
        self.status_label.setStyleSheet("color: green; font-weight: bold;")
    
    def _confirm_login(self):
        """用户点击了继续执行"""
        if self.worker:
            self.worker.confirm_login()
        self.continue_btn.setEnabled(False)
        self.continue_btn.setStyleSheet(
            "font-size: 14px; font-weight: bold;"
            "background-color: #2196F3; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )
        self.status_label.setText("查询中...")
        self.status_label.setStyleSheet("color: green; font-weight: bold;")
        self._log("✅ 收到登录确认，开始查询！")
    
    def _on_progress(self, current, total):
        """更新进度条"""
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        pct = int(current / total * 100) if total > 0 else 0
        self.status_label.setText(f"{current}/{total} ({pct}%)")
    
    def _on_row_done(self, row_idx, data):
        """单行查询完成，回填到表格"""
        # 回填时断开 cellChanged，防止触发用户编辑回调
        self.table.cellChanged.disconnect(self._on_cell_changed)
        try:
            for col_name, value in data.items():
                # 更新 UI 对应行
                if col_name in self.df.columns:
                    col_idx = list(self.df.columns).index(col_name)
                    if row_idx < self.table.rowCount():
                        item = QTableWidgetItem(str(value))
                        item.setBackground(QColor(200, 255, 200))  # 绿色高亮
                        self.table.setItem(row_idx, col_idx, item)

                # 同时更新底层数据（转 object 避免 dtype 警告）
                if col_name in self.df.columns:
                    col = self.df[col_name]
                    if col.dtype != object:
                        self.df[col_name] = col.astype(object)
                    self.df.at[row_idx, col_name] = value
        finally:
            self.table.cellChanged.connect(self._on_cell_changed)
        # 有新数据回填，标记为未保存
        if data:
            self._is_saved = False
            self._update_title()
            if self._stats_recorder:
                self._stats_recorder("aiqicha_query_completed", 1, "aiqicha_tool")
    
    def _on_finished(self, success):
        """查询完成"""
        self.start_btn.setEnabled(True)
        self.upload_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.continue_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("⏸️ 暂停")
        self._is_paused = False

        if success:
            self.status_label.setText("完成 ✓")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.status_label.setText("已停止")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")

        # 自动保存（完成或停止时均触发）
        if self.auto_save_chk.isChecked():
            self._auto_save()
        elif success:
            self._log("\n💡 查询完成！如需保存结果，请点击「保存结果」按钮")
    
    def _stop_query(self):
        """停止查询：立即中断，关闭浏览器由工作线程 finally 块负责"""
        if self.worker:
            self.worker.stop()
            # 立即更新 UI，给用户即时反馈（不等线程结束）
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.continue_btn.setEnabled(False)
            self.status_label.setText("停止中...")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")
            self._log("⏹️ 正在停止并关闭浏览器...")

    def _toggle_pause(self):
        """暂停/继续查询（切换）"""
        if not self.worker:
            return

        btn_style_orange = (
            "font-size: 14px; font-weight: bold;"
            "background-color: #FF9800; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )
        btn_style_green = (
            "font-size: 14px; font-weight: bold;"
            "background-color: #4CAF50; color: white;"
            "border-radius: 5px; padding: 6px 12px;"
        )

        if not self._is_paused:
            # 当前运行中 → 暂停
            self.worker.pause()
            self._is_paused = True
            self.pause_btn.setText("▶️ 继续")
            self.pause_btn.setStyleSheet(btn_style_green)
            self.status_label.setText("⏸️ 已暂停")
            self.status_label.setStyleSheet("color: #FF9800; font-weight: bold;")
            self._log("⏸️ 查询已暂停")
        else:
            # 当前暂停中 → 恢复
            self.worker.resume()
            self._is_paused = False
            self.pause_btn.setText("⏸️ 暂停")
            self.pause_btn.setStyleSheet(btn_style_orange)
            self.status_label.setText("查询中...")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
            self._log("▶️ 查询已恢复")

    def _on_auto_save_changed(self, state):
        """自动保存勾选框状态变更：开启时禁用「生成新表格」"""
        checked = (state == Qt.Checked)
        self.new_file_chk.setEnabled(not checked)

    @staticmethod
    def _ensure_target_headers(ws):
        """确保原表存在所有结果列，并返回列名到列号的映射。"""
        header = {cell.value: cell.column for cell in ws[1] if cell.value is not None}
        next_col = max(header.values(), default=0) + 1
        for col_name in TARGET_COLUMNS:
            if col_name not in header:
                ws.cell(row=1, column=next_col, value=col_name)
                header[col_name] = next_col
                next_col += 1
        return header

    def _auto_save(self):
        """
        自动保存：将当前 df 的目标列数据回写到原表格。
        只更新目标列（法人/地址/电话），不影响原表格其他列/格式。
        空字符串也会写入（清空对应单元格）。
        """
        if self.df is None or not self.file_path:
            return
        try:
            wb = openpyxl.load_workbook(self.file_path)
            ws = wb.active

            # 读取表头行（第1行），建立列名→列号映射
            header = self._ensure_target_headers(ws)

            for df_row_idx, row in self.df.iterrows():
                # openpyxl 行号从2开始（第1行是表头）
                xl_row = df_row_idx + 2
                for col_name in [LEGAL_COL_NAME, ADDR_COL_NAME, PHONE_COL_NAME]:
                    if col_name not in header:
                        continue
                    xl_col = header[col_name]
                    val = self.df.at[df_row_idx, col_name]
                    # nan/None 跳过，空字符串也写入（清空单元格）
                    if pd.isna(val) or str(val).strip() in ('nan', 'None'):
                        continue
                    ws.cell(row=xl_row, column=xl_col, value=str(val).strip())

            wb.save(self.file_path)
            self._is_saved = True
            self._update_title()
            self._log(f"💾 已自动保存到原表格: {os.path.basename(self.file_path)}")
        except Exception as e:
            self._log(f"⚠️ 自动保存失败: {e}")

    def _save_to_original(self):
        """
        手动"保存结果"（未勾选生成新表格）：
        将目标列数据回写到原表格，不影响原有数据。
        空字符串也会写入（清空对应单元格）。
        """
        if self.df is None or not self.file_path:
            QMessageBox.warning(self, "提示", "找不到原始文件路径，请使用「生成新表格」模式另存。")
            return
        try:
            wb = openpyxl.load_workbook(self.file_path)
            ws = wb.active

            header = self._ensure_target_headers(ws)

            for df_row_idx, row in self.df.iterrows():
                xl_row = df_row_idx + 2
                for col_name in [LEGAL_COL_NAME, ADDR_COL_NAME, PHONE_COL_NAME]:
                    if col_name not in header:
                        continue
                    xl_col = header[col_name]
                    val = self.df.at[df_row_idx, col_name]
                    # nan/None 跳过，空字符串也写入（清空单元格）
                    if pd.isna(val) or str(val).strip() in ('nan', 'None'):
                        continue
                    ws.cell(row=xl_row, column=xl_col, value=str(val).strip())

            wb.save(self.file_path)
            self._is_saved = True
            self._update_title()
            self._log(f"💾 结果已保存到原表格: {self.file_path}")
            QMessageBox.information(self, "保存成功", f"已回写到原表格:\n{self.file_path}")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"无法保存文件:\n{e}")

    def _save_result(self):
        """保存结果：根据「生成新表格」勾选决定另存 or 回写原文件"""
        if self.df is None:
            return

        if self.new_file_chk.isChecked():
            # 另存为新文件
            default_name = ""
            if self.file_path:
                base = os.path.splitext(os.path.basename(self.file_path))[0]
                default_name = f"{base}_已查询.xlsx"

            save_path, _ = QFileDialog.getSaveFileName(
                self, "另存为新表格",
                default_name or "查询结果.xlsx",
                "Excel 文件 (*.xlsx);;所有文件 (*)"
            )
            if not save_path:
                return

            try:
                self.df.to_excel(save_path, index=False, engine='openpyxl')
                self._log(f"💾 结果已另存为新文件: {save_path}")
                self._is_saved = True
                self._update_title()
                QMessageBox.information(self, "保存成功", f"文件已保存至:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "保存失败", f"无法保存文件:\n{e}")
        else:
            # 回写原文件
            self._save_to_original()

    def _update_title(self):
        """根据保存状态更新窗口标题（未保存时显示 *）"""
        base = "爱企查批量查询工具"
        if self.df is not None and not self._is_saved:
            self.setWindowTitle(f"* {base}（有未保存的更改）")
        else:
            self.setWindowTitle(base)

    def closeEvent(self, event):
        """关闭窗口时，若有未保存数据则提醒用户"""
        if self.df is not None and not self._is_saved:
            msg = QMessageBox(self)
            msg.setWindowTitle("有未保存的更改")
            msg.setText("表格数据尚未保存，直接关闭将丢失所有更改。")
            msg.setInformativeText("是否在关闭前保存？")
            msg.setIcon(QMessageBox.Warning)

            save_btn    = msg.addButton("保存", QMessageBox.AcceptRole)
            discard_btn = msg.addButton("不保存，直接关闭", QMessageBox.DestructiveRole)
            cancel_btn  = msg.addButton("取消", QMessageBox.RejectRole)  # noqa: F841
            msg.setDefaultButton(save_btn)

            msg.exec_()
            clicked = msg.clickedButton()

            if clicked == save_btn:
                if self.auto_save_chk.isChecked():
                    self._auto_save()
                else:
                    self._save_result()
                # 若用户在保存文件对话框里又取消了，则阻止关闭
                if not self._is_saved:
                    event.ignore()
                    return
            elif clicked == discard_btn:
                pass   # 直接关闭，不保存
            else:
                # 点了"取消"或关闭了对话框
                event.ignore()
                return
        # 已保存 / 选择不保存 → 停止后台工作后正常关闭
        self.shutdown()
        event.accept()

    def shutdown(self, timeout_ms=5000):
        """停止仍在运行的查询线程，供统一客户端退出/注销时调用。"""
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            return self.worker.wait(timeout_ms)
        return True


# ==================== 入口 ====================

def main():
    # 高 DPI 支持（Windows 缩放 125%/150% 等场景下按钮/字体不会变小）
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # 全局字体：以系统默认字体大小为基准，不强制固定 px
    base_font = app.font()
    base_font.setPointSize(max(10, base_font.pointSize()))
    app.setFont(base_font)

    window = MainWindow()
    window.show()

    # 关闭 PyInstaller 内置闪屏（打包时通过 --splash splash.png 启用）
    try:
        import pyi_splash
        pyi_splash.close()
    except ImportError:
        pass

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
