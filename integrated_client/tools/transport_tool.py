# 正则表达式：用于解析车牌格式
import re
import base64
import hashlib
# 操作系统模块：用于文件路径判断
import os
# 时间模块：用于延时
import time
# pandas：用于Excel读写
import pandas as pd
# PyQt6界面组件：创建GUI
# PyQt6核心模块：线程、信号
import io
import cv2
import numpy as np
import sys
import subprocess
import difflib
import uuid
from datetime import datetime
from pathlib import Path

import openpyxl
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont

USING_PYQT6 = False
from ..browser import get_builtin_chromium_path
from ..platform_support import chromium_launch_args
from ..ui.file_dialogs import SystemFileDialog as QFileDialog
from playwright.sync_api import sync_playwright, Page, Browser
try:
    from ddddocr import DdddOcr
    DDDDOCR_IMPORT_ERROR = ""
except Exception as exc:
    # 允许客户端在 OCR 运行库缺失时正常启动，人工验证码模式仍可使用。
    DdddOcr = None
    DDDDOCR_IMPORT_ERROR = str(exc)
# 新增：导入Union，兼容Python3.9
from typing import Union
from pinyin import get_pinyin
from PIL import Image, ImageFilter, ImageEnhance, ImageOps, ImageFont, ImageDraw

os.environ["DDDOCR_NO_LOG"] = "1"
os.environ["PLAYWRIGHT_LOG"] = "none"
os.environ["PLAYWRIGHT_LOCAL_LOG_DIR"] = os.devnull

ASSISTED_PAYMENT_COLUMNS = ("备注", "已协助补缴")


def resolve_assisted_payment_column(columns):
    """返回当前业务表中的补缴标记列，优先使用新版“备注”。"""
    available = {
        str(column).strip()
        for column in columns
        if column is not None and str(column).strip()
    }
    return next(
        (name for name in ASSISTED_PAYMENT_COLUMNS if name in available),
        None,
    )


def has_assisted_payment(row):
    """新旧列任意一个包含内容时，沿用原规则视为已协助补缴。"""
    getter = getattr(row, "get", None)
    if not callable(getter):
        return False
    for column_name in ASSISTED_PAYMENT_COLUMNS:
        value = getter(column_name, None)
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        if str(value).strip():
            return True
    return False


class TargetedWorkbookWriter:
    """只更新指定结果单元格，并通过同目录原子替换保存工作簿。"""

    RESULT_COLUMN_ANCHORS = ASSISTED_PAYMENT_COLUMNS

    def __init__(self, file_path, target_columns):
        self.file_path = Path(file_path)
        self.workbook = openpyxl.load_workbook(self.file_path)
        self.sheet = self.workbook.active
        self.column_indexes = {
            str(cell.value).strip(): cell.column
            for cell in self.sheet[1]
            if cell.value is not None and str(cell.value).strip()
        }
        self.dirty = False
        for column_name in target_columns:
            if column_name in self.column_indexes:
                self._move_existing_result_column_left(column_name)
                continue
            column_index = self._next_result_column()
            self.sheet.cell(1, column_index, column_name)
            self.column_indexes[column_name] = column_index
            self.dirty = True

    @staticmethod
    def _is_empty_value(value):
        return value is None or (isinstance(value, str) and not value.strip())

    def _column_is_empty(self, column_index):
        return all(
            self._is_empty_value(self.sheet.cell(row, column_index).value)
            for row in range(1, self.sheet.max_row + 1)
        )

    def _first_empty_column(self, start_column, stop_column):
        for column_index in range(start_column, stop_column):
            if self._column_is_empty(column_index):
                return column_index
        return None

    def _next_result_column(self):
        anchor_index = self._result_column_anchor_index()
        last_used_column = max(
            self.sheet.max_column,
            max(self.column_indexes.values(), default=0),
        )
        if anchor_index is not None:
            empty_column = self._first_empty_column(
                anchor_index + 1,
                last_used_column + 1,
            )
            if empty_column is not None:
                return empty_column
        return last_used_column + 1

    def _move_existing_result_column_left(self, column_name):
        anchor_index = self._result_column_anchor_index()
        source_column = self.column_indexes[column_name]
        if anchor_index is None or source_column <= anchor_index + 1:
            return
        destination_column = self._first_empty_column(
            anchor_index + 1,
            source_column,
        )
        if destination_column is None:
            return
        for row in range(1, self.sheet.max_row + 1):
            source_cell = self.sheet.cell(row, source_column)
            destination_cell = self.sheet.cell(row, destination_column)
            destination_cell.value = source_cell.value
            source_cell.value = None
        self.column_indexes[column_name] = destination_column
        self.dirty = True

    def _result_column_anchor_index(self):
        return next(
            (
                self.column_indexes[name]
                for name in self.RESULT_COLUMN_ANCHORS
                if name in self.column_indexes
            ),
            None,
        )

    @staticmethod
    def _cell_value(value):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        return value

    def write_row(self, dataframe_row_index, values):
        excel_row = int(dataframe_row_index) + 2
        for column_name, value in values.items():
            column_index = self.column_indexes[column_name]
            self.sheet.cell(
                excel_row,
                column_index,
                self._cell_value(value),
            )
        self.dirty = True

    def save(self):
        if not self.dirty:
            return
        temporary_path = self.file_path.with_name(
            f".{self.file_path.stem}.{uuid.uuid4().hex}.tmp{self.file_path.suffix}"
        )
        try:
            self.workbook.save(temporary_path)
            os.replace(temporary_path, self.file_path)
            self.dirty = False
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def close(self):
        self.workbook.close()


# ==============================================
# 【关键】强制让 ddddocr 从 exe 内部加载模型
# ==============================================
def fix_ddddocr_in_exe():
    if hasattr(sys, 'frozen'):
        os.environ['DDDCR_MODEL_PATH'] = sys._MEIPASS

fix_ddddocr_in_exe()

# ====================== 全局配置（需根据实际网站修改） ======================
CONFIG = {
    "TARGET_URL": "http://www.jt-online.cn/industry_inquiry/vehicle_inquiry.html",
    "CAPTCHA_RETRY": 2,
    "PAGE_RETRY": 2,
    "DETAIL_RETRY": 9999,
    "CAPT_IMG_SELECTOR": "#verifyImg",
    "CAPT_INPUT_SELECTOR": "#verifyCode",
    "QUERY_BTN_SELECTOR": "#next_step",
    "TRANSPORT_ID_SELECTOR": "#TRANO",
    "PROVINCE_MAP": {
        "京": "北京市", "津": "天津市", "沪": "上海市", "渝": "重庆市",
        "冀": "河北省", "晋": "山西省", "辽": "辽宁省", "吉": "吉林省", "黑": "黑龙江省",
        "苏": "江苏省", "浙": "浙江省", "皖": "安徽省", "闽": "福建省", "赣": "江西省",
        "鲁": "山东省", "豫": "河南省", "鄂": "湖北省", "湘": "湖南省", "粤": "广东省",
        "琼": "海南省", "川": "四川省", "贵": "贵州省", "云": "云南省", "陕": "陕西省",
        "甘": "甘肃省", "青": "青海省", "蒙": "内蒙古自治区", "桂": "广西壮族自治",
        "宁": "宁夏回族自治", "新": "新疆维吾尔自", "藏": "西藏自治区",
        "港": "香港特别行政", "澳": "澳门特别行政", "台": "台湾省"
    },
    "BUSINESS_QUERY_URL": "https://ysfw.mot.gov.cn/NetRoadCGSS-web/information/query?searchType=car",
    "BROWSER_WAIT_TIMEOUT_MS": 120000,
    "CAPTCHA_WAIT_SEC": 5,
    "BUSINESS_WEB_TIMEOUT_MS": 120000,
    "BUSINESS_CAPTCHA_POLL_MS": 250,
    "BUSINESS_CAPTCHA_STABLE_MS": 800,
    "BROWSER_RETRY_DELAY_SEC": 1,
}


class BrowserRecoveryError(RuntimeError):
    """浏览器或目标网页失效，且需要重新创建浏览器后重试当前记录。"""


def is_recoverable_browser_error(error):
    if isinstance(error, BrowserRecoveryError):
        return True
    message = str(error or "").casefold()
    markers = (
        "target page",
        "browser has been closed",
        "context or browser",
        "page.goto",
        "connection closed",
        "connection reset",
        "timeout",
        "net::err",
        "http 404",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    return any(marker in message for marker in markers)


def ensure_successful_navigation(response, page_name):
    status = getattr(response, "status", None)
    if status is not None and int(status) >= 400:
        raise BrowserRecoveryError(f"{page_name}返回 HTTP {int(status)}")
    return response


def wait_before_browser_retry(check_stopped, seconds=None):
    """浏览器恢复失败后短暂等待，同时保持暂停和停止操作可响应。"""
    delay = float(
        CONFIG["BROWSER_RETRY_DELAY_SEC"] if seconds is None else seconds
    )
    deadline = time.monotonic() + max(0.0, delay)
    while True:
        check_stopped()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))

# ====================== 工具函数（通用功能） ======================
def clean_company_name(name):
    """
    清洗公司名称：去掉尾部的括号及其内容，保留中间的括号
    例如：
    - "北京某某有限公司（法定代表人：张三）" → "北京某某有限公司"
    - "某某（有 限）公司（法定代表人：王五）" → "某某（有 限）公司"
    - "某某集团（代理：李四）" → "某某集团"
    """
    if not name:
        return name
    # 循环去掉尾部的括号部分
    while True:
        match = re.search(r'（[^）]*）$', name)
        if match:
            name = name[:match.start()]
        else:
            break
    return name.strip()


def parse_plate(plate_str):
    if pd.isna(plate_str):
        return None, None, None
    s = str(plate_str).strip()
    pattern1 = r"([京津沪渝冀豫鲁琼陕晋吉黑蒙苏浙皖闽赣湘鄂粤川贵云辽甘青宁新桂藏港澳台]{1})([A-Z0-9]+)\((黄色|蓝色|渐变绿色|黑色|白色|黄绿色)\)"
    pattern2 = r"([京津沪渝冀豫鲁琼陕晋吉黑蒙苏浙皖闽赣湘鄂粤川贵云辽甘青宁新桂藏港澳台]{1})([A-Z0-9]+)_(黄色|蓝色|渐变绿|黑色|白色|黄绿色)"
    match1 = re.match(pattern1, s)
    if match1:
        return match1.group(1), match1.group(2), match1.group(3)
    match2 = re.match(pattern2, s)
    if match2:
        return match2.group(1), match2.group(2), match2.group(3)
    return None, None, None


def clean_transport_id(s):
    if pd.isna(s):
        return ""
    return re.sub(r"[^0-9]", "", str(s))


def has_nonempty_cell_value(value):
    """判断 Excel 单元格是否包含可见内容，排除常见空值文本。"""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() not in {"", "nan", "none", "null", "nat"}


# 普通验证码 OCR（纯数字·自动识别线条+延长补全·全流程可视化版）
ocr = DdddOcr(show_ad=False) if DdddOcr is not None else None


def ocr_code(
    page,
    selector,
    model=None,
    return_details=False,
    model_failure_callback=None,
):
    # Keep the browser capture immutable. Recognition may freely transform a
    # separate copy, but successful learning samples must contain the image
    # exactly as it appeared on the page.
    original_img_bytes = b""

    def result(code, version):
        if return_details:
            return code, original_img_bytes, version
        return code

    try:
        # 1. 仅截图获取字节流，不保存本地图片
        original_img_bytes = page.locator(selector).screenshot()

        if model is not None:
            try:
                custom_code = str(
                    model.predict_numeric(original_img_bytes) or ""
                )
            except Exception:
                custom_code = ""
            if re.fullmatch(r"[0-9]{4}", custom_code):
                return result(custom_code, str(model.version))
            if callable(model_failure_callback):
                try:
                    model_failure_callback(str(model.version))
                except Exception:
                    pass

        if ocr is None:
            print(f"ddddocr 不可用：{DDDDOCR_IMPORT_ERROR}")
            return result("", "ddddocr-unavailable")

        # 2. 转灰度图
        pil_img = Image.open(io.BytesIO(original_img_bytes)).convert('L')

        # 3. 右侧补边+水平拉伸
        pil_img = ImageOps.expand(pil_img, border=(0, 0, 25, 0), fill=255)
        w, h = pil_img.size
        pil_img = pil_img.resize((w + 15, h), Image.Resampling.LANCZOS)

        # 4. 对比度+锐化强化数字轮廓
        enhancer = ImageEnhance.Contrast(pil_img)
        pil_img = enhancer.enhance(2.5)
        pil_img = pil_img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=400, threshold=1))

        # ===================== 核心：自动识别线条+智能延长 =====================
        # 5. 转OpenCV格式，做边缘检测
        cv_img = np.array(pil_img)
        edges = cv2.Canny(cv_img, 50, 150)

        # 6. 仅在右侧区域检测线条
        h_img, w_img = edges.shape
        right_region = edges[:, int(w_img * 0.7):]

        # 7. 霍夫线变换识别线条
        lines = cv2.HoughLinesP(
            right_region,
            rho=1,
            theta=np.pi/180,
            threshold=10,
            minLineLength=12,
            maxLineGap=5
        )

        # 8. 找到最长目标线条
        target_line = None
        max_len = 0
        if lines is not None:
            for line in lines:
                x1, y1, x2, y2 = line[0]
                x1 += int(w_img * 0.7)
                x2 += int(w_img * 0.7)
                length = np.sqrt((x2 - x1)**2 + (y2 - y1)**2)
                if length > max_len:
                    max_len = length
                    target_line = (x1, y1, x2, y2)

        # 9. 自动延长线条
        if target_line is not None:
            x1, y1, x2, y2 = target_line
            dx = x2 - x1
            dy = y2 - y1
            norm = np.sqrt(dx**2 + dy**2)
            if norm > 0:
                dx /= norm
                dy /= norm
                extend_pixels = 25
                new_x2 = int(x2 + dx * extend_pixels)
                new_y2 = int(y2 + dy * extend_pixels)
                cv2.line(cv_img, (x2, y2), (new_x2, new_y2), color=0, thickness=3)

        # 10. 最终二值化处理
        pil_img_final = Image.fromarray(cv_img)
        pil_img_final = pil_img_final.point(lambda p: 255 if p > 110 else 0)

        # 转字节流用于OCR
        buf = io.BytesIO()
        pil_img_final.save(buf, format='PNG')
        recognition_img_bytes = buf.getvalue()
        # ==================================================================

        # 11. 错误替换字典
        def replace_common_errors(s):
            rep = {
                '一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6', '七': '7', '八': '8', '九': '9', '零': '0',
                '—': '1', '－': '1', '-': '1', 'Z': '2', 'z': '2', 's': '5', 'S': '5',
                'o': '0', 'O': '0', 'Q': '0', 'l': '1', 'I': '1', 'B': '8', 'b': '8', 'g': '9',
                'A': '4', 'a': '4', 'C': '0', 'c': '0', ' ': '', '　': ''
            }
            for c, n in rep.items():
                s = s.replace(c, n)
            return s

        # 12. 多次识别投票
        results = []
        for _ in range(12):
            raw = ocr.classification(recognition_img_bytes)
            raw = replace_common_errors(raw)
            code = re.sub(r'[^0-9]', '', raw)
            if 3 <= len(code) <= 4:
                results.append(code)

        if not results:
            print(f"⚠️ OCR识别失败，原始结果：{raw}")
            return result("", "ddddocr-builtin")

        # 13. 兜底补全
        final_code = ""
        code_4 = [c for c in results if len(c) == 4]
        if code_4:
            final_code = max(set(code_4), key=code_4.count)
        else:
            code_3 = [c for c in results if len(c) == 3]
            if code_3:
                base = max(set(code_3), key=code_3.count)
                all_digits = ''.join(results)
                last_digit = max(set(all_digits), key=all_digits.count) if all_digits else '1'
                final_code = base + last_digit

        print(f"【纯数字验证码·线条延长版】结果：{results} → {final_code}")
        return result(final_code, "ddddocr-builtin")

    except Exception as e:
        print(f"验证码识别异常：{str(e)}")
        return result("", "ddddocr-builtin")



# ====================== 原线程：查询运输证（支持自定义重试次数） ======================
class Worker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(str)
    pause_signal = pyqtSignal()
    input_signal = pyqtSignal(str)
    stat_event = pyqtSignal(str, int)
    retry_signal = pyqtSignal(str, str)
    captcha_attempt_signal = pyqtSignal(object)

    input_result = ""

    # 接收所有运行参数
    def __init__(
        self,
        src_path,
        is_auto_mode,
        manual_at_captcha,
        page_retry,
        auto_continue,
        captcha_retry,
        only_yellow_card,
        captcha_model_manager=None,
        captcha_collection_enabled=None,
        captcha_sample_collection_enabled=None,
    ):
        super().__init__()
        self.src = src_path
        self.auto_mode = is_auto_mode
        self.manual_at_captcha = manual_at_captcha
        self._running = True
        self._paused = False
        self._global_paused = False
        self.browser: Union[Browser, None] = None
        self.page: Union[Page, None] = None
        self.context = None
        self.playwright = None

        self.PAGE_RETRY = page_retry
        self.DETAIL_RETRY = 9999
        self.CAPTCHA_RETRY = captcha_retry

        # 自动继续配置
        self.auto_continue = auto_continue
        self.only_yellow_card = only_yellow_card
        self.captcha_model_manager = captcha_model_manager
        self.captcha_collection_enabled = captcha_collection_enabled
        self.captcha_sample_collection_enabled = (
            captcha_sample_collection_enabled
        )

    def stop(self):
        self._running = False
        self._paused = False
        self._global_paused = False

    def resume(self):
        self._paused = False
        self._global_paused = False

    def global_pause(self):
        self._global_paused = True

    def _check_stopped(self):
        while self._global_paused and self._running:
            time.sleep(0.2)
        if not self._running:
            raise Exception("手动停止")

    def _close_browser(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.context = None
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        self.browser = None
        self.page = None
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None

    def _collection_enabled(self):
        callback = self.captcha_collection_enabled
        if not callable(callback):
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    def _sample_collection_enabled(self):
        callback = self.captcha_sample_collection_enabled
        if callback is None:
            callback = self.captcha_collection_enabled
        if not callable(callback):
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    def _active_model(self, captcha_type):
        manager = self.captcha_model_manager
        if manager is None:
            return None
        try:
            return manager.get(captcha_type)
        except Exception:
            return None

    def _emit_captcha_attempt(
        self,
        *,
        success,
        model_version,
        assisted,
        image_bytes=None,
        answer=None,
    ):
        if not self._collection_enabled():
            return
        event = {
            "captcha_type": "numeric",
            "source": "transport_numeric",
            "model_version": str(model_version or "unknown"),
            "success": bool(success),
            "assisted": bool(assisted),
            "occurred_at": datetime.now().astimezone().isoformat(),
        }
        if success and image_bytes and isinstance(answer, dict):
            event.update(
                {
                    "image_bytes": bytes(image_bytes),
                    "image_mime": "image/png",
                    "answer": answer,
                }
            )
        self.captcha_attempt_signal.emit(event)

    def _report_successful_captcha_attempt(
        self,
        *,
        model_version,
        assisted,
        image_bytes=None,
        answer=None,
    ):
        if not self._collection_enabled():
            return
        attempt = {
            "model_version": model_version,
            "assisted": assisted,
        }
        if (
            self._sample_collection_enabled()
            and image_bytes
            and isinstance(answer, dict)
        ):
            attempt.update(
                {
                    "image_bytes": image_bytes,
                    "answer": answer,
                }
            )
        self._emit_captcha_attempt(success=True, **attempt)

    def _create_new_browser(self):
        self._close_browser()
        try:
            self.playwright = sync_playwright().start()
            browser_path = get_builtin_chromium_path(self.playwright)
            self.browser = self.playwright.chromium.launch(
                headless=False,
                slow_mo=600,
                executable_path=browser_path,
                args=chromium_launch_args(),
            )
            self.context = self.browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                    "Mobile/15E148 Safari/604.1"
                ),
            )
            self.page = self.context.new_page()
            self.log.emit(f"🔧 使用 Chromium 浏览器：{browser_path}")
            return True
        except Exception as exc:
            self.log.emit(f"❌ 浏览器启动失败：{str(exc)[:160]}")
            self._close_browser()
            return False

    def _create_browser_until_ready(self, retry_type, reason):
        """持续启动浏览器，直到成功或用户主动停止。"""
        attempt = 0
        while self._running:
            self._check_stopped()
            attempt += 1
            if self._create_new_browser():
                return True
            self.retry_signal.emit(
                retry_type,
                f"{reason}，浏览器启动第 {attempt} 次失败",
            )
            self.log.emit(
                f"⚠️ {reason}，将在稍后继续重试"
                f"（已尝试 {attempt} 次，无次数上限）"
            )
            wait_before_browser_retry(self._check_stopped)
        return False

    def run(self):
        workbook_writer = None
        current_row_index = None
        try:
            try:
                df = pd.read_excel(self.src, engine='openpyxl', dtype=str)
            except PermissionError:
                self.log.emit("❌ 无法读取原始表，请先关闭 Excel/WPS 表格后再运行程序！")
                self.finished.emit("失败")
                return

            required_cols = ["车辆标识"]
            missing_cols = [col for col in required_cols if col not in df.columns]
            if resolve_assisted_payment_column(df.columns) is None:
                missing_cols.append("备注（兼容“已协助补缴”）")
            if missing_cols:
                self.log.emit(f"❌ 源表格缺少必要列：{','.join(missing_cols)}")
                self.finished.emit("失败")
                return

            # 新增"运输证号_纯数字"列（如果不存在）
            if "运输证号_纯数字" not in df.columns:
                df["运输证号_纯数字"] = ""
            if "查询状态" not in df.columns:
                df["查询状态"] = ""

            workbook_writer = TargetedWorkbookWriter(
                self.src,
                ("运输证号_纯数字", "查询状态"),
            )

            total = len(df)
            self.log.emit(f"📌 共 {total} 条记录待处理")
            self.log.emit("📌 直接在原始表上查询运输证号")

            # 仅回写当前行的结果列，不重建原始工作簿。
            def save_results(row_index):
                try:
                    workbook_writer.write_row(
                        row_index,
                        {
                            "运输证号_纯数字": df.at[row_index, "运输证号_纯数字"],
                            "查询状态": df.at[row_index, "查询状态"],
                        },
                    )
                    workbook_writer.save()
                except PermissionError:
                    self.log.emit("❌ 无法保存原始表，请先关闭 Excel/WPS 表格后再运行程序！")
                    raise

            if not self._create_browser_until_ready(
                "transport_browser_start",
                "运输证查询浏览器启动失败",
            ):
                self.finished.emit("失败")
                return

            idx = 0
            browser_restarts = 0
            while idx < total and self._running:
                row = df.iloc[idx]
                current_row_index = idx
                self._check_stopped()

                progress = round((idx + 1) / total * 100)
                self.progress.emit(progress)

                plate_identifier = str(row["车辆标识"]).strip()
                self.log.emit(f"\n[{idx + 1}/{total}] 处理：{plate_identifier}")

                # 检查是否已有运输证号（断点续查）
                existing_cert = str(df.at[idx, "运输证号_纯数字"]).strip()
                if existing_cert not in ["", "nan", "NaN", "None"]:
                    self.log.emit("✅ 已有运输证号，跳过查询")
                    df.at[idx, "查询状态"] = "已存在（跳过）"
                    save_results(idx)
                    idx += 1
                    browser_restarts = 0
                    continue

                # 已有企业信息说明该行已完成后续回填，不再重复查询运输证号。
                existing_company = row.get("车辆所有人/企业", "")
                existing_query_status = str(row.get("查询状态", "")).strip()
                previous_browser_failure = is_recoverable_browser_error(
                    existing_query_status
                )
                if has_nonempty_cell_value(existing_company) and not previous_browser_failure:
                    self.log.emit("✅ 已有企业信息，跳过运输证查询")
                    df.at[idx, "查询状态"] = "已有企业信息（跳过）"
                    save_results(idx)
                    idx += 1
                    browser_restarts = 0
                    continue
                if previous_browser_failure:
                    self.log.emit("↻ 检测到上次浏览器异常，本行重新查询运输证号")

                # 检查是否已补缴
                if has_assisted_payment(row):
                    df.at[idx, "查询状态"] = "已补缴（无需查询）"
                    self.log.emit("✅ 已补缴，无需查询")
                    save_results(idx)
                    try:
                        self.page.goto(
                            CONFIG["TARGET_URL"],
                            timeout=CONFIG["BROWSER_WAIT_TIMEOUT_MS"],
                        )
                        self.page.wait_for_timeout(1000)
                    except:
                        pass
                    idx += 1
                    browser_restarts = 0
                    continue

                # 解析车牌
                province, plate_num, plate_color = parse_plate(plate_identifier)
                if not province:
                    df.at[idx, "查询状态"] = "车牌格式错误（解析失败）"
                    self.log.emit(f"⚠️ 车牌解析失败")
                    save_results(idx)
                    idx += 1
                    browser_restarts = 0
                    continue

                # 黄牌过滤
                if self.only_yellow_card:
                    if plate_color != "黄色":
                        df.at[idx, "查询状态"] = "已跳过（非黄牌）"
                        save_results(idx)
                        self.log.emit(f"⏭️ 非黄牌，已跳过")
                        idx += 1
                        browser_restarts = 0
                        continue

                full_plate = province + plate_num
                query_success = False
                error_msg = ""
                self.input_result = ""

                # 临时变量存储查询结果
                tr_original = ""
                tr_clean = ""
                try:
                    self._check_stopped()
                    response = self.page.goto(
                        CONFIG["TARGET_URL"],
                        timeout=CONFIG["BROWSER_WAIT_TIMEOUT_MS"],
                    )
                    ensure_successful_navigation(response, "运输证查询页")
                    self.log.emit("✓ 切换到外省查询")
                    self.page.locator("text='外省查询'").click()
                    self.page.wait_for_timeout(800)

                    self._check_stopped()
                    self.page.locator("#province").select_option(label=CONFIG["PROVINCE_MAP"][province])
                    self.page.locator("#BRANUM2").fill(full_plate)
                    self.page.locator("#BRACOLOR2").select_option(label=plate_color)

                    # ========== 勾选【验证码人工输入】：自动继续倒计时 + 错误重新倒计时 ==========
                    if not self.auto_mode and self.manual_at_captcha:
                        while self._running:
                            self._check_stopped()
                            manual_image_bytes = b""
                            if self._sample_collection_enabled():
                                try:
                                    manual_image_bytes = self.page.locator(
                                        CONFIG["CAPT_IMG_SELECTOR"]
                                    ).screenshot()
                                except Exception:
                                    pass

                            # 自动继续模式：同时检测【4位输入完成】和【输入框消失】，任一满足即触发提交
                            if self.auto_continue:
                                self.log.emit("⏳ 自动继续：等待您输入完4位验证码...")
                                captcha_passed = False
                                try:
                                    self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).fill("", timeout=2000)
                                except Exception:
                                    # 超时说明输入框已消失（页面已跳转），验证码已通过
                                    if self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).count() == 0:
                                        self.log.emit("✅ 验证码验证成功（输入框已消失）")
                                        captcha_passed = True
                                if captcha_passed:
                                    # 跳出外层 while self._running 循环，进入提交流程
                                    self.page.wait_for_timeout(800)
                                    break
                                while self._running:
                                    self._check_stopped()
                                    # 同时检测两个条件：4位输入 或 输入框消失
                                    try:
                                        value = self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).input_value(timeout=2000)
                                        if len(value) >= 4:
                                            self.log.emit("✅ 检测到4位验证码，自动提交查询！")
                                            break
                                    except Exception:
                                        pass
                                    if self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).count() == 0:
                                        self.log.emit("✅ 验证码验证成功（输入框已消失）")
                                        break
                                    time.sleep(0.1)
                            else:
                                # 非自动继续：手动点继续
                                self.log.emit("⏸️ 请手动输入验证码后，点击【继续执行】")
                                self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).fill("")
                                self._paused = True
                                self.pause_signal.emit()
                                while self._paused and self._running:
                                    time.sleep(0.2)
                            self._check_stopped()
                            try:
                                manual_code = self.page.locator(
                                    CONFIG["CAPT_INPUT_SELECTOR"]
                                ).input_value(timeout=2000)
                            except Exception:
                                manual_code = ""

                            # 把焦点切回浏览器，再提交查询
                            try:
                                self.page.bring_to_front()
                            except:
                                pass
                            # 提交查询
                            try:
                                self.page.evaluate("document.querySelector('#next_step').click()")
                            except:
                                pass
                            self.page.wait_for_timeout(800)

                            # 判断是否验证码错误：检测验证码输入框是否仍存在
                            # 输入框消失 = 页面已跳转 = 验证码正确；输入框还在 = 验证码错误（弹窗提示）
                            captcha_input_still_exists = self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).count() > 0

                            if captcha_input_still_exists:
                                self._emit_captcha_attempt(
                                    success=False,
                                    model_version="human-manual",
                                    assisted=True,
                                )
                                # 错误：刷新验证码，清空输入框，重新回到倒计时
                                self.log.emit("❌ 验证码错误，将重新刷新验证码...")
                                try:
                                    self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).fill("",timeout=2000)
                                    self.page.locator(CONFIG["CAPT_IMG_SELECTOR"]).click()
                                    self.page.wait_for_timeout(1000)
                                except:
                                    # 输入框已消失，说明验证码正确，无需刷新
                                    self.log.emit("✅ 验证码验证成功")
                                    break
                                # 回到循环开头，重新倒计时
                                continue
                            else:
                                # 正确，退出循环
                                self._report_successful_captcha_attempt(
                                    model_version="human-manual",
                                    assisted=True,
                                    image_bytes=manual_image_bytes,
                                    answer=(
                                        {"value": manual_code}
                                        if re.fullmatch(
                                            r"[0-9]{4}",
                                            manual_code or "",
                                        )
                                        else None
                                    ),
                                )
                                self.log.emit("✅ 验证码验证成功")
                                break
                            self.page.wait_for_selector(
                                ".user_zige, :has-text('查询不到信息')",
                                timeout=CONFIG["BROWSER_WAIT_TIMEOUT_MS"],
                            )
                        self._check_stopped()

                    list_loaded = False
                    # 页面重试循环
                    for page_try in range(self.PAGE_RETRY):
                        self._check_stopped()

                        # ==============================================
                        # 【核心修复】仅第一次执行验证码逻辑，后续重试/刷新绝不碰验证码
                        # ==============================================
                        if page_try == 0:
                            captcha_err = 0
                            # 验证码重试循环
                            while captcha_err < self.CAPTCHA_RETRY and self._running:
                                self._check_stopped()
                                captcha_code = ""
                                captcha_image_bytes = b""
                                captcha_model_version = "human-manual"

                                if not (not self.auto_mode and self.manual_at_captcha):
                                    captcha_result = ocr_code(
                                        self.page,
                                        CONFIG["CAPT_IMG_SELECTOR"],
                                        model=self._active_model("numeric"),
                                        return_details=True,
                                        model_failure_callback=(
                                            lambda version: self._emit_captcha_attempt(
                                                success=False,
                                                model_version=version,
                                                assisted=False,
                                            )
                                        ),
                                    )
                                    if (
                                        isinstance(captcha_result, tuple)
                                        and len(captcha_result) == 3
                                    ):
                                        (
                                            captcha_code,
                                            captcha_image_bytes,
                                            captcha_model_version,
                                        ) = captcha_result
                                    else:
                                        # Keep compatibility with older OCR
                                        # adapters and test doubles that return
                                        # only the recognized text.
                                        captcha_code = str(captcha_result or "")
                                        captcha_model_version = "ddddocr-builtin"
                                    if captcha_model_version not in {
                                        "human-manual",
                                        "ddddocr-builtin",
                                        "ddddocr-unavailable",
                                    }:
                                        self.log.emit(
                                            "✅ 已使用当前数字验证码模型："
                                            f"{captcha_model_version}"
                                        )
                                    if not captcha_code:
                                        self._emit_captcha_attempt(
                                            success=False,
                                            model_version=captcha_model_version,
                                            assisted=False,
                                        )
                                        captcha_err += 1
                                        self.retry_signal.emit(
                                            "transport_captcha_ocr",
                                            f"运输证验证码 OCR 第 {captcha_err} 次失败",
                                        )
                                        self.log.emit(
                                            f"⚠️ OCR失败，刷新验证码 | 重试次数：{captcha_err}")
                                        self.page.locator(CONFIG["CAPT_IMG_SELECTOR"]).click()
                                        continue
                                    self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).fill("")
                                    self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).fill(captcha_code)
                                # 1. 点击查询按钮（无超时卡死）
                                try:
                                    self.page.evaluate("document.querySelector('#next_step').click()")
                                except:
                                    pass

                                # 2. 等待页面响应（跳转或弹窗）
                                self.page.wait_for_timeout(800)

                                # 判断验证码是否正确：检测验证码输入框是否消失
                                # 输入框消失 = 页面已跳转到结果列表 = 验证码正确
                                captcha_input_still_exists = self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).count() > 0

                                if captcha_input_still_exists:
                                    self._emit_captcha_attempt(
                                        success=False,
                                        model_version=captcha_model_version,
                                        assisted=False,
                                    )
                                    captcha_err += 1
                                    self.retry_signal.emit(
                                        "transport_captcha",
                                        f"运输证验证码第 {captcha_err} 次错误",
                                    )
                                    self.log.emit(f"❌ 验证码错误 | 重试次数：{captcha_err}")

                                    # 仅真正验证码错误，才执行清空+刷新
                                    self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).fill("")
                                    self.page.locator(CONFIG["CAPT_IMG_SELECTOR"]).click()
                                    self.page.wait_for_timeout(1000)
                                    continue

                                # 3. 正常验证码成功，跳出循环
                                self._report_successful_captcha_attempt(
                                    model_version=captcha_model_version,
                                    assisted=False,
                                    image_bytes=captcha_image_bytes,
                                    answer=(
                                        {"value": captcha_code}
                                        if re.fullmatch(
                                            r"[0-9]{4}",
                                            captcha_code or "",
                                        )
                                        else None
                                    ),
                                )
                                self.log.emit("✅ 验证码验证成功")
                                break

                            if captcha_err >= self.CAPTCHA_RETRY:
                                # 人工接管模式 + 未勾选人工输验证码 → 自动重试耗尽，转人工输入
                                if not self.auto_mode and not self.manual_at_captcha:
                                    while self._running:
                                        self._check_stopped()
                                        self.log.emit(f"⚠️ 自动识别重试耗尽，切换为【人工输入验证码】")
                                        self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).fill("",timeout=2000)
                                        manual_image_bytes = b""
                                        if self._sample_collection_enabled():
                                            try:
                                                manual_image_bytes = self.page.locator(
                                                    CONFIG["CAPT_IMG_SELECTOR"]
                                                ).screenshot()
                                            except Exception:
                                                pass

                                        if self.auto_continue:
                                            # 自动继续：同时检测【4位输入完成】和【输入框消失】
                                            self.log.emit("⏳ 请输入4位验证码，等待自动提交...")
                                            while self._running:
                                                self._check_stopped()
                                                # 同时检测两个条件：4位输入 或 输入框消失
                                                try:
                                                    value = self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).input_value(timeout=2000)
                                                    if re.fullmatch(r"[0-9]{4}", value):
                                                        self.log.emit(f"✅ 检测到4位验证码，自动提交...")
                                                        break
                                                except:
                                                    pass
                                                if self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).count() == 0:
                                                    self.log.emit("✅ 验证码验证成功（输入框已消失）")
                                                    break
                                                time.sleep(1)
                                        else:
                                            # 非自动继续：暂停等待用户点继续
                                            self.log.emit("⏸️ 请手动输入验证码后，点击【继续执行】")
                                            self._paused = True
                                            self.pause_signal.emit()
                                            while self._paused and self._running:
                                                time.sleep(0.2)
                                        self._check_stopped()
                                        try:
                                            manual_code = self.page.locator(
                                                CONFIG["CAPT_INPUT_SELECTOR"]
                                            ).input_value(timeout=2000)
                                        except Exception:
                                            manual_code = ""

                                        # 人工输完后自动点击查询
                                        self.log.emit("✅ 已恢复执行，自动提交查询...")
                                        try:
                                            self.page.evaluate("document.querySelector('#next_step').click()")
                                        except:
                                            pass
                                        self.page.wait_for_timeout(800)
                                        # 判断是否验证码错误：检测输入框是否仍存在
                                        captcha_input_still_exists = self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).count() > 0

                                        if captcha_input_still_exists:
                                            self._emit_captcha_attempt(
                                                success=False,
                                                model_version="human-fallback",
                                                assisted=True,
                                            )
                                            # 人工输错 → 提示错误，清空输入框，刷新验证码，重新循环
                                            self.log.emit("❌ 人工输入验证码错误，请重新输入！")
                                            try:
                                                self.page.locator(CONFIG["CAPT_INPUT_SELECTOR"]).fill("",timeout=2000)
                                                self.page.locator(CONFIG["CAPT_IMG_SELECTOR"]).click()
                                                self.page.wait_for_timeout(1000)
                                            except:
                                                # 输入框已消失，说明验证码正确，无需刷新
                                                self.log.emit("✅ 验证码验证成功")
                                                break
                                            continue
                                        else:
                                            # 验证码正确，退出循环，继续流程
                                            self._report_successful_captcha_attempt(
                                                model_version="human-fallback",
                                                assisted=True,
                                                image_bytes=manual_image_bytes,
                                                answer=(
                                                    {"value": manual_code}
                                                    if re.fullmatch(
                                                        r"[0-9]{4}",
                                                        manual_code or "",
                                                    )
                                                    else None
                                                ),
                                            )
                                            self.log.emit("✅ 验证码验证成功")
                                            break


                                    # 重置错误次数，继续页面重试循环
                                    captcha_err = 0
                                    continue

                                # 全自动模式：重试耗尽直接跳过
                                else:
                                    self.log.emit(f"❌ 验证码重试{self.CAPTCHA_RETRY}次全部失败，跳过当前车牌！")
                                    df.at[idx, "查询状态"] = "验证码识别失败"
                                    save_results(idx)
                                    list_loaded = False
                                    break
                        # ======================================================================================

                        # 正常业务逻辑（仅刷新结果页，不碰任何验证码）
                        if self.page.locator(".user_zige").count() > 0:
                            self.log.emit(f"✅ 第 {page_try + 1} 次尝试加载列表成功")
                            list_loaded = True
                            break
                        elif "查询不到信息" in self.page.content():
                            self.retry_signal.emit(
                                "transport_result_page",
                                f"运输证结果页第 {page_try + 1} 次无数据",
                            )
                            self.log.emit(f"ℹ️ 第 {page_try + 1} 次无数据，刷新结果页重试")
                            self.page.reload()  # 仅刷新结果页，仍在结果页
                            self.page.wait_for_timeout(1500)
                        else:
                            break

                    if self.page.locator(".user_zige").count() > 0:
                        self.log.emit(f"✅ 最后一次检查：列表已加载成功")
                        list_loaded = True

                    # ===================== 如果验证码失败已标记，直接跳过后续所有查询代码 =====================
                    if not list_loaded and str(df.at[idx, "查询状态"]).strip() == "验证码识别失败":
                        idx += 1
                        browser_restarts = 0
                        continue
                    # ==============================================================================================

                    try:
                        self._check_stopped()
                        if not list_loaded and "查询不到信息" in self.page.content():
                            raise Exception("查询不到信息")

                        self.page.wait_for_selector(
                            ".user_zige",
                            state="visible",
                            timeout=CONFIG["BROWSER_WAIT_TIMEOUT_MS"],
                        )
                        self.log.emit("✅ 列表已加载，准备点击条目")
                        self.page.evaluate("document.querySelector('.user_zige').click()")
                        self.log.emit("✅ 已点击列表条目，等待详情页跳转")
                        self.page.wait_for_url(
                            "**/vehicle_out_inquiry_detail.html**",
                            timeout=CONFIG["BROWSER_WAIT_TIMEOUT_MS"],
                        )
                        self.log.emit("✅ 已进入详情页")

                        tr = ""
                        for detail_try in range(self.DETAIL_RETRY):
                            self._check_stopped()
                            try:
                                self.page.wait_for_selector(CONFIG["TRANSPORT_ID_SELECTOR"], state="visible",
                                                            timeout=1800)
                                tr = self.page.locator(CONFIG["TRANSPORT_ID_SELECTOR"]).inner_text().strip()

                                if tr:
                                    self.log.emit(f"✅ 第 {detail_try + 1} 次获取运输证号成功")
                                    break  # 成功就退出
                                else:
                                    # 内容为空才重试
                                    if detail_try < self.DETAIL_RETRY - 1:
                                        self.retry_signal.emit(
                                            "transport_detail",
                                            f"运输证详情第 {detail_try + 1} 次内容为空",
                                        )
                                        self.log.emit(
                                            f"⚠️ 详情页内容为空，刷新重试")
                                        self.page.reload()
                                        self.page.wait_for_timeout(800)
                            except:
                                # 加载异常
                                if detail_try < self.DETAIL_RETRY - 1:
                                    self.retry_signal.emit(
                                        "transport_detail",
                                        f"运输证详情第 {detail_try + 1} 次加载异常",
                                    )
                                    self.log.emit(f"⚠️ 详情页加载异常，刷新重试")
                                    self.page.reload()
                                    self.page.wait_for_timeout(800)

                        # 最后兜底：无论是否重试结束，只要页面有元素，就再拿一次（修复关键！）
                        if not tr:
                            try:
                                tr = self.page.locator(CONFIG["TRANSPORT_ID_SELECTOR"]).inner_text().strip()
                                if tr:
                                    self.log.emit(f"✅ 重试结束后最终获取到运输证号")
                            except:
                                pass

                        if tr:
                            tr_original = tr
                            tr_clean = clean_transport_id(tr)
                            query_success = True
                            self.log.emit(f"✅ 最终获取运输证号：{tr}")
                        else:
                            error_msg = "运输证号为空"
                            self.log.emit("⚠️ 多次刷新后仍无运输证号")

                    except Exception as e:
                        if "查询不到信息" in self.page.content():
                            df.at[idx, "查询状态"] = "查询无结果"
                            self.log.emit("ℹ️ 多次尝试后仍查询无结果")
                        else:
                            error_msg = f"获取信息失败：{str(e)[:50]}"
                            self.log.emit(f"⚠️ {error_msg}")

                except Exception as e:
                    if is_recoverable_browser_error(e):
                        browser_restarts += 1
                        reason = str(e).strip() or e.__class__.__name__
                        self.retry_signal.emit(
                            "transport_browser",
                            f"运输证浏览器异常重启：{reason[:120]}",
                        )
                        self.log.emit(
                            "⚠️ 浏览器或查询页失效，正在重新启动浏览器并重试"
                            f"当前记录（第 {browser_restarts} 次，无次数上限）"
                        )
                        if self._create_browser_until_ready(
                            "transport_browser_start",
                            "运输证查询浏览器恢复失败",
                        ):
                            continue
                        self._check_stopped()
                        raise BrowserRecoveryError(
                            "运输证浏览器恢复失败，当前记录未跳过，步骤 1 已停止"
                        ) from e
                    error_msg = str(e)[:50]
                    self.log.emit(f"💥 异常：{error_msg}")

                # 人工录入兜底
                try:
                    self._check_stopped()
                    if not query_success and not self.auto_mode and "查询不到信息" not in self.page.content():
                        self.log.emit("⏸️ 请输入运输证号")
                        self.input_signal.emit(plate_identifier)
                        self._paused = True
                        self.pause_signal.emit()
                        while self._paused and self._running:
                            time.sleep(0.2)
                        self._check_stopped()
                        if self.input_result.strip():
                            tr_original = self.input_result
                            tr_clean = clean_transport_id(self.input_result)
                            df.at[idx, "查询状态"] = "人工录入成功"
                            self.log.emit(f"✅ 已录入：{self.input_result}")
                        else:
                            df.at[idx, "查询状态"] = error_msg or "失败"
                        self.resume()
                except:
                    pass

                # 将结果写入原始表
                df.at[idx, "运输证号_纯数字"] = tr_clean
                if not query_success and tr_clean == "":
                    if df.at[idx, "查询状态"] == "":
                        df.at[idx, "查询状态"] = error_msg or "失败"
                elif query_success:
                    df.at[idx, "查询状态"] = "查询成功"
                save_results(idx)
                if tr_clean:
                    self.stat_event.emit("transport_query_completed", 1)
                idx += 1
                browser_restarts = 0

            workbook_writer.save()
            self.log.emit(f"\n📊 所有任务完成，原始表已保存：{self.src}")

        except Exception as e:
            if "手动停止" in str(e):
                self.log.emit("🛑 已手动立即停止任务")
                if workbook_writer is not None and current_row_index is not None:
                    save_results(current_row_index)
                self.log.emit("✅ 已保存所有已处理结果到原始表！")
            elif isinstance(e, PermissionError):
                self.log.emit("❌ 无法读写文件，请先关闭 Excel/WPS 表格后再运行程序！")
                self.finished.emit("失败")
                return
            else:
                self.log.emit(f"❌ 程序执行异常：{str(e)}")
                self.finished.emit("失败")
                return
        finally:
            self._close_browser()
            if workbook_writer is not None:
                workbook_writer.close()
            self._running = False

        self.finished.emit("完成")


# ====================== 回填线程（已优化：成功不重启浏览器，只刷新） ======================
class BusinessBackfillWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(str)
    pause_signal = pyqtSignal()
    input_signal = pyqtSignal(str)
    stat_event = pyqtSignal(str, int)
    retry_signal = pyqtSignal(str, str)
    captcha_attempt_signal = pyqtSignal(object)
    browser_loading_started = pyqtSignal()
    browser_loading_finished = pyqtSignal()

    input_result = ""

    def __init__(
        self,
        original_file,
        auto_continue,
        auto_mode,
        captcha_retry,
        manual_at_captcha,
        captcha_model_manager=None,
        captcha_collection_enabled=None,
        captcha_sample_collection_enabled=None,
    ):
        super().__init__()
        self.original_file = original_file
        self.auto_continue = auto_continue
        self.auto_mode = auto_mode
        self.web_timeout = CONFIG["BUSINESS_WEB_TIMEOUT_MS"]
        self.captcha_retry = captcha_retry
        self.manual_at_captcha = manual_at_captcha
        self.captcha_model_manager = captcha_model_manager
        self.captcha_collection_enabled = captcha_collection_enabled
        self.captcha_sample_collection_enabled = (
            captcha_sample_collection_enabled
        )
        self._running = True
        self._paused = False
        self._global_paused = False
        self.browser = None
        self.playwright = None
        self.page = None
        self._prefetched_manual_click_capture = None

    def _collection_enabled(self):
        callback = self.captcha_collection_enabled
        if not callable(callback):
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    def _sample_collection_enabled(self):
        callback = self.captcha_sample_collection_enabled
        if callback is None:
            callback = self.captcha_collection_enabled
        if not callable(callback):
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    def _active_model(self, captcha_type):
        manager = self.captcha_model_manager
        if manager is None:
            return None
        try:
            return manager.get(captcha_type)
        except Exception:
            return None

    def _predict_with_active_click_model(
        self,
        image_bytes,
        bboxes,
        expected_count,
    ):
        """使用管理员当前应用的点选模型，失败时交回内置识别结果。"""
        custom_model = self._active_model("click")
        if custom_model is None:
            return None
        try:
            positions = custom_model.predict_click_regions(
                image_bytes,
                bboxes,
            )
        except Exception:
            positions = {}
        if len(positions) >= int(expected_count):
            version = str(custom_model.version)
            self.log.emit(f"✅ 已使用当前点选验证码模型：{version}")
            return positions, version
        self._emit_captcha_attempt(
            success=False,
            model_version=custom_model.version,
            assisted=False,
        )
        return None

    def _emit_captcha_attempt(
        self,
        *,
        success,
        model_version,
        assisted,
        image_bytes=None,
        answer=None,
    ):
        if not self._collection_enabled():
            return
        event = {
            "captcha_type": "click",
            "source": "business_click",
            "model_version": str(model_version or "unknown"),
            "success": bool(success),
            "assisted": bool(assisted),
            "occurred_at": datetime.now().astimezone().isoformat(),
        }
        if success and image_bytes and isinstance(answer, dict):
            event.update(
                {
                    "image_bytes": bytes(image_bytes),
                    "image_mime": "image/png",
                    "answer": answer,
                }
            )
        self.captcha_attempt_signal.emit(event)

    def stop(self):
        self._running = False
        self._paused = False
        self._global_paused = False

    def resume(self):
        self._paused = False
        self._global_paused = False

    def global_pause(self):
        self._global_paused = True

    def _check_stopped(self):
        while self._global_paused and self._running:
            time.sleep(0.2)
        if not self._running:
            raise Exception("手动停止")

    def _close_browser(self):
        try:
            if self.browser:
                self.browser.close()
                self.browser = None
        except:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
                self.playwright = None
        except:
            pass
        self.page = None

    def _create_new_browser(self):
        self._close_browser()
        try:
            self.playwright = sync_playwright().start()
            launch_kwargs = {
                "headless": False,
                "slow_mo": 600,
                "executable_path": get_builtin_chromium_path(self.playwright),
                "args": chromium_launch_args(),
            }
            self.browser = self.playwright.chromium.launch(**launch_kwargs)
            self.log.emit(
                f"🔧 使用 Chromium 浏览器：{launch_kwargs['executable_path']}"
            )
            self.context = self.browser.new_context(
                no_viewport=True  # ← 禁用固定视口，窗口可调整
            )
            self.page = self.context.new_page()
            self.log.emit("✅ 浏览器已启动")
            return True
        except Exception as e:
            self.log.emit(f"❌ 浏览器启动失败: {e}")
            return False

    def _create_browser_with_retries(self, retry_type, reason):
        """持续启动浏览器，直到成功或用户主动停止。"""
        self.browser_loading_started.emit()
        attempt = 0
        while self._running:
            self._check_stopped()
            attempt += 1
            if self._create_new_browser():
                self.browser_loading_finished.emit()
                return True
            self.retry_signal.emit(
                retry_type,
                f"{reason}，浏览器启动第 {attempt} 次失败",
            )
            self.log.emit(
                "⚠️ 浏览器启动失败，正在继续重试"
                f"（已尝试 {attempt} 次，无次数上限）"
            )
            wait_before_browser_retry(self._check_stopped)
        return False

    def _load_business_query_page(self):
        """加载步骤 2 业务页，并向主界面标记不可计入的等待区间。"""
        self.browser_loading_started.emit()
        try:
            response = self.page.goto(
                CONFIG["BUSINESS_QUERY_URL"],
                timeout=self.web_timeout,
            )
            ensure_successful_navigation(response, "营运查询页")
            self.page.wait_for_selector(
                "a:has-text('营运车辆')",
                timeout=self.web_timeout,
            )
            self.page.click("a:has-text('营运车辆')")
            time.sleep(0.5)
        finally:
            self.browser_loading_finished.emit()

    def _captcha_prompt_is_visible(self):
        try:
            prompt = self.page.locator(".verify-msg")
            return bool(prompt.count() and prompt.first.is_visible())
        except Exception:
            return False

    @staticmethod
    def _result_field_value(locator):
        """读取网页结果字段，兼容 input.value 与普通文本节点。"""
        input_value = getattr(locator, "input_value", None)
        if callable(input_value):
            try:
                # input 的当前值以 DOM property 为准；空值也不能再回退到旧的
                # value attribute，否则可能误读上一条查询结果。
                return str(input_value() or "").strip()
            except Exception:
                pass

        for method_name in ("text_content", "inner_text"):
            method = getattr(locator, method_name, None)
            if not callable(method):
                continue
            try:
                value = method()
            except Exception:
                continue
            value = str(value or "").strip()
            if value:
                return value
        return ""

    def _business_owner_name(self, *, visible_only=False):
        # The current site renders vehicle results as table rows.  Prefer the
        # visible result table over the legacy input because the old field can
        # remain in the DOM as a hidden template with stale data.
        try:
            result_rows = self.page.locator("#licenseBody > tr")
            for row_index in range(result_rows.count()):
                row = result_rows.nth(row_index)
                if not row.is_visible():
                    continue
                cells = row.locator("td")
                if cells.count() < 2:
                    # Loading/empty placeholders commonly use one colspan cell.
                    continue
                owner_cell = cells.nth(1)
                if not owner_cell.is_visible():
                    continue
                owner_name = self._result_field_value(owner_cell)
                if owner_name:
                    return owner_name
        except Exception:
            # Retain compatibility with the previous page structure below.
            pass

        try:
            owner = self.page.locator("#ownerName2")
            if not owner.count():
                return ""
            field = owner.first
            if visible_only and not field.is_visible():
                return ""
            return self._result_field_value(field)
        except Exception:
            return ""

    def _captcha_image_is_ready(self):
        """验证码题目和图片均完整加载后才允许开始识别。"""
        try:
            prompt = self.page.locator(".verify-msg")
            if not prompt.count() or not prompt.first.is_visible():
                return False
            prompt_text = prompt.first.inner_text().strip()
            if not re.search(r"【([^】]+)】", prompt_text):
                return False

            image = self.page.locator(".back-img")
            if not image.count() or not image.first.is_visible():
                return False
            return bool(
                image.first.evaluate(
                    """
                    async element => {
                        const rect = element.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 20) return false;

                        const image = element.matches('img')
                            ? element
                            : element.querySelector('img');
                        if (image) {
                            return image.complete
                                && image.naturalWidth >= 20
                                && image.naturalHeight >= 20;
                        }

                        const canvas = element.matches('canvas')
                            ? element
                            : element.querySelector('canvas');
                        if (canvas) {
                            return canvas.width >= 20 && canvas.height >= 20;
                        }

                        const background = getComputedStyle(element).backgroundImage;
                        const match = background && background.match(/url\\(["']?(.*?)["']?\\)/);
                        if (!match || !match[1]) return false;
                        return await new Promise(resolve => {
                            const probe = new Image();
                            const timer = setTimeout(() => resolve(false), 5000);
                            probe.onload = () => {
                                clearTimeout(timer);
                                resolve(probe.naturalWidth >= 20 && probe.naturalHeight >= 20);
                            };
                            probe.onerror = () => {
                                clearTimeout(timer);
                                resolve(false);
                            };
                            probe.src = match[1];
                        });
                    }
                    """
                )
            )
        except Exception:
            return False

    def _business_result_is_ready(self):
        if self._captcha_prompt_is_visible():
            return False
        if self._business_owner_name(visible_only=True):
            return True
        try:
            tips = self.page.locator(".layui-layer-content")
            for index in range(tips.count()):
                tip = tips.nth(index)
                if not tip.is_visible():
                    continue
                tip_text = (tip.inner_text() or "").strip()
                if any(
                    keyword in tip_text
                    for keyword in ("系统无该营运车辆信息", "未查询到")
                ):
                    return True
        except Exception:
            pass
        return False

    def _wait_for_business_response(self):
        """等待验证码图片稳定就绪，或等待无需验证码的业务结果。"""
        deadline = time.monotonic() + self.web_timeout / 1000
        wait_logged = False
        prefetch_attempted = False
        self._prefetched_manual_click_capture = None
        while time.monotonic() < deadline:
            self._check_stopped()
            if self._business_result_is_ready():
                if self._prefetched_manual_click_capture is not None:
                    capture = self._prefetched_manual_click_capture
                    self._prefetched_manual_click_capture = None
                    points = self._manual_click_points(capture)
                    rapid_attempt = self._manual_click_attempt_from_events(
                        exclude_challenge_id=str(
                            capture.get("event_challenge_id")
                            or capture.get("challenge_id")
                            or ""
                        )
                    )
                    self._clear_manual_click_events(capture)
                    self.log.emit("✅ 用户已点完验证码")
                    if rapid_attempt is not None:
                        self._emit_captcha_attempt(
                            success=False,
                            model_version="human-manual",
                            assisted=True,
                        )
                        self._report_successful_click_captcha(
                            rapid_attempt["capture"],
                            rapid_attempt["points"],
                            model_version="human-manual",
                            assisted=True,
                        )
                        self._clear_manual_click_events(
                            rapid_attempt["capture"]
                        )
                    else:
                        self._report_successful_click_captcha(
                            capture,
                            points,
                            model_version="human-manual",
                            assisted=True,
                        )
                    self.log.emit("✅ 验证码通过，开始获取信息")
                return "result"
            if self._captcha_image_is_ready():
                if not wait_logged:
                    self.log.emit("⏳ 验证码已出现，等待图片完整加载...")
                    wait_logged = True
                if (
                    not prefetch_attempted
                    and not self.auto_mode
                    and self.manual_at_captcha
                ):
                    # 验证码首次完整显示就安装点击监听并保存原图，不再等
                    # 800ms 稳定期结束，避免用户先于采集线程完成点击。
                    prefetch_attempted = True
                    self._prefetched_manual_click_capture = (
                        self._prepare_manual_click_capture(reset_clicks=True)
                    )
                    # 人工模式在题图就绪并完成预采集后应立即进入点选状态；
                    # 继续等待稳定周期反而给快速刷新制造串题窗口。
                    self.log.emit("✅ 验证码图片加载完成，开始处理")
                    return "captcha"
                self.page.wait_for_timeout(CONFIG["BUSINESS_CAPTCHA_STABLE_MS"])
                self._check_stopped()
                if self._captcha_image_is_ready():
                    self.log.emit("✅ 验证码图片加载完成，开始处理")
                    return "captcha"
            elif not wait_logged:
                self.log.emit("⏳ 等待营运查询响应或验证码图片加载...")
                wait_logged = True
            self.page.wait_for_timeout(CONFIG["BUSINESS_CAPTCHA_POLL_MS"])
        raise BrowserRecoveryError(
            f"等待营运查询响应或验证码图片加载超时（{self.web_timeout // 1000}秒）"
        )

    def _wait_for_business_result(self):
        """验证码通过后继续等待公司信息或无数据提示真正出现。"""
        deadline = time.monotonic() + self.web_timeout / 1000
        self.log.emit("⏳ 等待营运企业信息加载完成...")
        while time.monotonic() < deadline:
            self._check_stopped()
            if self._business_result_is_ready():
                return True
            self.page.wait_for_timeout(CONFIG["BUSINESS_CAPTCHA_POLL_MS"])
        return False

    @staticmethod
    def _parse_manual_click_prompt(prompt_text):
        match = re.search(r"【([^】]+)】", str(prompt_text or ""))
        if not match:
            return []
        return [
            character.strip()
            for character in re.split(r"[,，、\s]+", match.group(1))
            if character.strip()
        ]

    @staticmethod
    def _normalize_manual_click_points(points, expected_count=0):
        normalized = []
        for point in points or []:
            if not isinstance(point, dict):
                continue
            try:
                x = float(point.get("x"))
                y = float(point.get("y"))
            except (TypeError, ValueError):
                continue
            if 0 <= x <= 1 and 0 <= y <= 1:
                normalized.append({"x": round(x, 6), "y": round(y, 6)})
        if expected_count:
            return normalized[:expected_count]
        return normalized

    @staticmethod
    def _click_challenge_id(
        source,
        image_bytes=None,
        prompt=None,
        generation=0,
        *,
        use_image=False,
    ):
        if use_image and image_bytes:
            value = bytes(image_bytes)
        elif source:
            value = str(source).encode("utf-8", errors="replace")
        else:
            value = bytes(image_bytes or b"")
        if not value:
            return ""
        digest = hashlib.sha256()
        digest.update(value)
        digest.update(b"\0")
        digest.update(
            "\x1f".join(str(item) for item in (prompt or [])).encode("utf-8")
        )
        digest.update(b"\0")
        digest.update(str(generation or 0).encode("ascii", errors="replace"))
        return digest.hexdigest()

    @staticmethod
    def _decode_click_image_source(source):
        value = str(source or "")
        if "," not in value:
            return None
        header, encoded_image = value.split(",", 1)
        if (
            not header.casefold().startswith("data:image/")
            or ";base64" not in header.casefold()
        ):
            return None
        try:
            raw_image = base64.b64decode(encoded_image, validate=False)
            if not raw_image or len(raw_image) > 4 * 1024 * 1024:
                return None
            with Image.open(io.BytesIO(raw_image)) as source_image:
                if source_image.width * source_image.height > 16_000_000:
                    return None
                source_image.load()
                mode = "RGBA" if "A" in source_image.getbands() else "RGB"
                normalized_image = source_image.convert(mode)
            output = io.BytesIO()
            normalized_image.save(output, format="PNG")
            return output.getvalue()
        except Exception:
            return None

    @staticmethod
    def _click_captcha_state(image):
        try:
            state = image.evaluate(
                """
                element => {
                    const ownerWindow = element.ownerDocument.defaultView || window;
                    const generationOf = current => {
                        if (!Number.isInteger(current.__intdemoCaptchaGeneration)) {
                            ownerWindow.__intdemoCaptchaGenerationCounter =
                                (ownerWindow.__intdemoCaptchaGenerationCounter || 0) + 1;
                            current.__intdemoCaptchaGeneration =
                                ownerWindow.__intdemoCaptchaGenerationCounter;
                        }
                        return current.__intdemoCaptchaGeneration;
                    };
                    const sourceOf = current => {
                        const image = current.matches('img')
                            ? current
                            : current.querySelector('img');
                        if (image) {
                            return image.currentSrc || image.src ||
                                image.getAttribute('src') || '';
                        }
                        const canvas = current.matches('canvas')
                            ? current
                            : current.querySelector('canvas');
                        if (canvas) {
                            try {
                                return canvas.toDataURL('image/png');
                            } catch (_) {
                                return '';
                            }
                        }
                        const background = getComputedStyle(current).backgroundImage || '';
                        const match = background.match(/url\\(["']?(.*?)["']?\\)/);
                        return match && match[1] ? match[1] : '';
                    };
                    const prompt = element.ownerDocument.querySelector('.verify-msg');
                    return {
                        source: sourceOf(element),
                        prompt_text: prompt ? (prompt.textContent || '').trim() : '',
                        generation: generationOf(element)
                    };
                }
                """
            )
            return dict(state or {})
        except Exception:
            return {}

    def _clean_manual_click_screenshot(self, image):
        style_id = "__intdemoCaptchaCleanCaptureStyle"
        image.evaluate(
            """
            (element, styleId) => {
                const documentRoot = element.ownerDocument;
                let style = documentRoot.getElementById(styleId);
                if (!style) {
                    style = documentRoot.createElement('style');
                    style.id = styleId;
                    documentRoot.documentElement.appendChild(style);
                }
                style.textContent =
                    '.point-area { visibility: hidden !important; opacity: 0 !important; }';
            }
            """,
            style_id,
        )
        try:
            return image.screenshot()
        finally:
            try:
                self.page.evaluate(
                    "styleId => document.getElementById(styleId)?.remove()",
                    style_id,
                )
            except Exception:
                pass

    def _install_manual_click_handler(self, image, *, reset_clicks):
        return image.evaluate(
            """
            (element, resetClicks) => {
                const documentRoot = element.ownerDocument;
                const ownerWindow = documentRoot.defaultView || window;
                const storageKey = '__intdemoCaptchaClickEventsV2';

                const restoreEvents = () => {
                    try {
                        const value = JSON.parse(
                            ownerWindow.sessionStorage.getItem(storageKey) || '[]'
                        );
                        return Array.isArray(value) ? value : [];
                    } catch (_) {
                        return [];
                    }
                };
                if (resetClicks) {
                    ownerWindow.__intdemoCaptchaClickEvents = [];
                } else if (!Array.isArray(ownerWindow.__intdemoCaptchaClickEvents)) {
                    ownerWindow.__intdemoCaptchaClickEvents = restoreEvents();
                }
                try {
                    ownerWindow.sessionStorage.setItem(
                        storageKey,
                        JSON.stringify(ownerWindow.__intdemoCaptchaClickEvents)
                    );
                } catch (_) {}

                const sourceOf = current => {
                    const image = current.matches('img')
                        ? current
                        : current.querySelector('img');
                    if (image) {
                        return image.currentSrc || image.src ||
                            image.getAttribute('src') || '';
                    }
                    const canvas = current.matches('canvas')
                        ? current
                        : current.querySelector('canvas');
                    if (canvas) {
                        try {
                            return canvas.toDataURL('image/png');
                        } catch (_) {
                            return '';
                        }
                    }
                    const background = getComputedStyle(current).backgroundImage || '';
                    const match = background.match(/url\\(["']?(.*?)["']?\\)/);
                    return match && match[1] ? match[1] : '';
                };
                const nextGeneration = current => {
                    ownerWindow.__intdemoCaptchaGenerationCounter =
                        (ownerWindow.__intdemoCaptchaGenerationCounter || 0) + 1;
                    current.__intdemoCaptchaGeneration =
                        ownerWindow.__intdemoCaptchaGenerationCounter;
                    return current.__intdemoCaptchaGeneration;
                };
                const generationOf = current =>
                    Number.isInteger(current.__intdemoCaptchaGeneration)
                        ? current.__intdemoCaptchaGeneration
                        : nextGeneration(current);
                generationOf(element);

                if (ownerWindow.__intdemoCaptchaLoadHandler) {
                    documentRoot.removeEventListener(
                        'load',
                        ownerWindow.__intdemoCaptchaLoadHandler,
                        true
                    );
                }
                const loadHandler = event => {
                    const target = event.target;
                    if (!(target instanceof ownerWindow.Element)) return;
                    const host = target.matches('.back-img')
                        ? target
                        : target.closest('.back-img');
                    if (
                        host &&
                        (target === host || target.matches('img,canvas'))
                    ) nextGeneration(host);
                };
                ownerWindow.__intdemoCaptchaLoadHandler = loadHandler;
                documentRoot.addEventListener('load', loadHandler, true);

                if (ownerWindow.__intdemoCaptchaMutationObserver) {
                    ownerWindow.__intdemoCaptchaMutationObserver.disconnect();
                }
                ownerWindow.__intdemoCaptchaObservedElement = element;
                const observer = new ownerWindow.MutationObserver(mutations => {
                    const current = documentRoot.querySelector('.back-img');
                    if (!current) return;
                    if (current !== ownerWindow.__intdemoCaptchaObservedElement) {
                        generationOf(current);
                        ownerWindow.__intdemoCaptchaObservedElement = current;
                        return;
                    }
                    for (const mutation of mutations) {
                        if (mutation.type === 'attributes') {
                            const target = mutation.target;
                            const host = target.matches('.back-img')
                                ? target
                                : target.closest('.back-img');
                            if (
                                host === current &&
                                (target === host || target.matches('img,canvas'))
                            ) {
                                nextGeneration(current);
                                return;
                            }
                        }
                        if (mutation.type === 'childList') {
                            const changedImage = Array.from(mutation.addedNodes)
                                .some(node =>
                                    node instanceof ownerWindow.Element &&
                                    current.contains(node) &&
                                    (node.matches('img,canvas') ||
                                        node.querySelector('img,canvas'))
                                );
                            if (changedImage) {
                                nextGeneration(current);
                                return;
                            }
                        }
                    }
                });
                observer.observe(documentRoot, {
                    subtree: true,
                    childList: true,
                    attributes: true,
                    attributeFilter: ['src']
                });
                ownerWindow.__intdemoCaptchaMutationObserver = observer;

                if (ownerWindow.__intdemoCaptchaCaptureHandler) {
                    documentRoot.removeEventListener(
                        'click',
                        ownerWindow.__intdemoCaptchaCaptureHandler,
                        true
                    );
                }
                const handler = event => {
                    const current = documentRoot.querySelector('.back-img');
                    if (!current) return;
                    const rect = current.getBoundingClientRect();
                    if (!rect.width || !rect.height) return;
                    if (
                        event.clientX < rect.left || event.clientX > rect.right ||
                        event.clientY < rect.top || event.clientY > rect.bottom
                    ) return;
                    const prompt = documentRoot.querySelector('.verify-msg');
                    ownerWindow.__intdemoCaptchaClickEvents.push({
                        x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
                        y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
                        source: sourceOf(current),
                        prompt_text: prompt ? (prompt.textContent || '').trim() : '',
                        generation: generationOf(current)
                    });
                    ownerWindow.__intdemoCaptchaClickEvents =
                        ownerWindow.__intdemoCaptchaClickEvents.slice(-12);
                    try {
                        ownerWindow.sessionStorage.setItem(
                            storageKey,
                            JSON.stringify(ownerWindow.__intdemoCaptchaClickEvents)
                        );
                    } catch (_) {}
                };
                ownerWindow.__intdemoCaptchaCaptureHandler = handler;
                documentRoot.addEventListener('click', handler, true);

                const rect = element.getBoundingClientRect();
                const markerPoints = !rect.width || !rect.height ? [] :
                    Array.from(documentRoot.querySelectorAll('.point-area'))
                        .filter(marker => {
                            const style = ownerWindow.getComputedStyle(marker);
                            const markerRect = marker.getBoundingClientRect();
                            const clientX = markerRect.left + markerRect.width / 2;
                            const clientY = markerRect.top + markerRect.height / 2;
                            return style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                markerRect.width > 0 && markerRect.height > 0 &&
                                clientX >= rect.left && clientX <= rect.right &&
                                clientY >= rect.top && clientY <= rect.bottom;
                        })
                        .map(marker => {
                            const markerRect = marker.getBoundingClientRect();
                            const clientX = markerRect.left + markerRect.width / 2;
                            const clientY = markerRect.top + markerRect.height / 2;
                            return {
                                x: Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)),
                                y: Math.max(0, Math.min(1, (clientY - rect.top) / rect.height))
                            };
                        });
                const prompt = documentRoot.querySelector('.verify-msg');
                return {
                    source: sourceOf(element),
                    prompt_text: prompt ? (prompt.textContent || '').trim() : '',
                    generation: generationOf(element),
                    marker_points: markerPoints
                };
            }
            """,
            bool(reset_clicks),
        )

    def _prepare_manual_click_capture(self, *, reset_clicks=True):
        if not self._sample_collection_enabled():
            return None
        last_error = "题图在采集期间持续刷新"
        should_reset = bool(reset_clicks)
        for _attempt in range(3):
            try:
                image = self.page.locator(".back-img").first

                # source、题目和现有 marker 必须在同一次 JS 调用中读取，
                # 防止刷新恰好夹在多个 DOM 调用之间而拼成跨题样本。
                snapshot = dict(
                    self._install_manual_click_handler(
                        image,
                        reset_clicks=should_reset,
                    )
                    or {}
                )
                should_reset = False
                source = str(snapshot.get("source") or "")
                generation = int(snapshot.get("generation") or 0)
                prompt_text = str(snapshot.get("prompt_text") or "").strip()
                prompt = self._parse_manual_click_prompt(prompt_text)
                if not prompt:
                    last_error = "无法解析点选验证码题目"
                    continue

                image_bytes = self._decode_click_image_source(source)
                identity_uses_image = image_bytes is None
                if image_bytes is None:
                    # 非 data URL / canvas 时使用安全截图；临时隐藏编号圆点，
                    # 避免用户点选图标被合成到训练图片里。
                    image_bytes = self._clean_manual_click_screenshot(image)

                current_image = self.page.locator(".back-img").first
                current_state = self._click_captcha_state(current_image)
                current_source = str(current_state.get("source") or "")
                current_generation = int(current_state.get("generation") or 0)
                current_prompt = self._parse_manual_click_prompt(
                    current_state.get("prompt_text")
                )
                if (
                    current_source != source
                    or current_prompt != prompt
                    or current_generation != generation
                ):
                    continue

                if identity_uses_image:
                    verified_image = self._clean_manual_click_screenshot(
                        current_image
                    )
                    if hashlib.sha256(verified_image).digest() != hashlib.sha256(
                        image_bytes
                    ).digest():
                        continue
                    image_bytes = verified_image

                return {
                    "image_bytes": image_bytes,
                    "prompt": prompt,
                    "prompt_text": prompt_text,
                    "challenge_id": self._click_challenge_id(
                        source,
                        image_bytes,
                        prompt,
                        generation,
                        use_image=identity_uses_image,
                    ),
                    "event_challenge_id": self._click_challenge_id(
                        source,
                        prompt=prompt,
                        generation=generation,
                    ),
                    "challenge_source": source,
                    "challenge_generation": generation,
                    "identity_uses_image": identity_uses_image,
                    "initial_points": self._normalize_manual_click_points(
                        snapshot.get("marker_points") or [],
                        len(prompt),
                    ),
                }
            except Exception as exc:
                last_error = str(exc)[:80]

        self.log.emit(
            "⚠️ 点选验证码原图采集失败，本次只能记录成功统计："
            f"{last_error}"
        )
        return None

    def _manual_click_events(self):
        try:
            events = self.page.evaluate(
                """
                () => {
                    if (Array.isArray(window.__intdemoCaptchaClickEvents)) {
                        return window.__intdemoCaptchaClickEvents.slice();
                    }
                    try {
                        const value = JSON.parse(
                            sessionStorage.getItem('__intdemoCaptchaClickEventsV2') || '[]'
                        );
                        return Array.isArray(value) ? value : [];
                    } catch (_) {
                        return [];
                    }
                }
                """
            )
            return list(events or [])
        except Exception:
            return []

    def _clear_manual_click_events(self, capture=None):
        challenge_source = str((capture or {}).get("challenge_source") or "")
        challenge_prompt_text = str((capture or {}).get("prompt_text") or "")
        challenge_generation = int(
            (capture or {}).get("challenge_generation") or 0
        )
        try:
            self.page.evaluate(
                """
                ([challengeSource, challengePromptText, challengeGeneration]) => {
                    const current = Array.isArray(
                        window.__intdemoCaptchaClickEvents
                    ) ? window.__intdemoCaptchaClickEvents : [];
                    window.__intdemoCaptchaClickEvents = challengeSource
                        ? current.filter(event =>
                            event && (
                                event.source !== challengeSource ||
                                event.prompt_text !== challengePromptText ||
                                Number(event.generation || 0) !== challengeGeneration
                            )
                        )
                        : [];
                    try {
                        sessionStorage.setItem(
                            '__intdemoCaptchaClickEventsV2',
                            JSON.stringify(window.__intdemoCaptchaClickEvents)
                        );
                    } catch (_) {}
                }
                """,
                [
                    challenge_source,
                    challenge_prompt_text,
                    challenge_generation,
                ],
            )
        except Exception:
            pass

    def _manual_click_marker_points(self):
        try:
            return self.page.evaluate(
                """
                () => {
                    const image = document.querySelector('.back-img');
                    if (!image) return [];
                    const rect = image.getBoundingClientRect();
                    if (!rect.width || !rect.height) return [];
                    return Array.from(document.querySelectorAll('.point-area'))
                        .filter(marker => {
                            const style = window.getComputedStyle(marker);
                            const markerRect = marker.getBoundingClientRect();
                            const clientX = markerRect.left + markerRect.width / 2;
                            const clientY = markerRect.top + markerRect.height / 2;
                            return style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                markerRect.width > 0 && markerRect.height > 0 &&
                                clientX >= rect.left && clientX <= rect.right &&
                                clientY >= rect.top && clientY <= rect.bottom;
                        })
                        .map(marker => {
                            const markerRect = marker.getBoundingClientRect();
                            const clientX = markerRect.left + markerRect.width / 2;
                            const clientY = markerRect.top + markerRect.height / 2;
                            return {
                                x: Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)),
                                y: Math.max(0, Math.min(1, (clientY - rect.top) / rect.height))
                            };
                        });
                }
                """
            )
        except Exception:
            return []

    def _manual_click_points(self, capture):
        if not capture:
            return []
        expected_count = len(capture.get("prompt") or [])
        challenge_id = str(capture.get("challenge_id") or "")
        event_challenge_id = str(
            capture.get("event_challenge_id") or challenge_id
        )
        event_points = []
        for event in self._manual_click_events():
            if not isinstance(event, dict):
                continue
            event_source = str(event.get("source") or "")
            if (
                event_challenge_id
                and (
                    not event_source
                    or self._click_challenge_id(
                        event_source,
                        prompt=self._parse_manual_click_prompt(
                            event.get("prompt_text")
                        ),
                        generation=event.get("generation") or 0,
                    )
                    != event_challenge_id
                )
            ):
                continue
            event_points.append(event)

        normalized_events = self._normalize_manual_click_points(event_points)
        if expected_count and len(normalized_events) >= expected_count:
            return normalized_events[:expected_count]

        current_challenge_id = self._current_manual_click_challenge_id(capture)
        marker_points = []
        if (
            not challenge_id
            or (
                current_challenge_id
                and current_challenge_id == challenge_id
            )
        ):
            marker_points = self._manual_click_marker_points()
            normalized_markers = self._normalize_manual_click_points(
                marker_points
            )
            if expected_count and len(normalized_markers) >= expected_count:
                return normalized_markers[:expected_count]

        sources = (
            capture.get("initial_points") or [],
            normalized_events,
            marker_points,
        )
        combined = []
        for source_points in sources:
            for point in self._normalize_manual_click_points(source_points):
                if any(
                    abs(point["x"] - existing["x"]) <= 0.015
                    and abs(point["y"] - existing["y"]) <= 0.015
                    for existing in combined
                ):
                    continue
                combined.append(point)
                if expected_count and len(combined) >= expected_count:
                    return combined[:expected_count]
        return combined[:expected_count] if expected_count else combined

    def _manual_click_attempt_from_events(self, *, exclude_challenge_id=""):
        groups = {}
        group_order = []
        for event in self._manual_click_events():
            if not isinstance(event, dict):
                continue
            source = str(event.get("source") or "")
            prompt_text = str(event.get("prompt_text") or "")
            prompt = self._parse_manual_click_prompt(prompt_text)
            generation = int(event.get("generation") or 0)
            challenge_id = self._click_challenge_id(
                source,
                prompt=prompt,
                generation=generation,
            )
            if not source or not challenge_id or challenge_id == exclude_challenge_id:
                continue
            if challenge_id not in groups:
                groups[challenge_id] = {
                    "source": source,
                    "prompt_text": prompt_text,
                    "prompt": prompt,
                    "generation": generation,
                    "events": [],
                }
                group_order.append(challenge_id)
            elif (
                not self._parse_manual_click_prompt(
                    groups[challenge_id]["prompt_text"]
                )
                and self._parse_manual_click_prompt(prompt_text)
            ):
                groups[challenge_id]["prompt_text"] = prompt_text
                groups[challenge_id]["prompt"] = prompt
            groups[challenge_id]["events"].append(event)

        for challenge_id in reversed(group_order):
            group = groups[challenge_id]
            prompt_text = group["prompt_text"]
            prompt = group["prompt"]
            if not prompt:
                continue
            points = self._normalize_manual_click_points(
                group["events"],
                len(prompt),
            )
            if len(points) != len(prompt):
                continue
            image_bytes = self._decode_click_image_source(
                group["source"]
            )
            capture = None
            if image_bytes is not None:
                capture = {
                    "image_bytes": image_bytes,
                    "prompt": prompt,
                    "prompt_text": prompt_text,
                    "challenge_id": challenge_id,
                    "event_challenge_id": challenge_id,
                    "challenge_source": group["source"],
                    "challenge_generation": group["generation"],
                    "identity_uses_image": False,
                    "initial_points": [],
                }
            return {"capture": capture, "points": points}
        return None

    def _current_manual_click_challenge_id(self, capture=None):
        try:
            image = self.page.locator(".back-img").first
            state = self._click_captcha_state(image)
            source = str(state.get("source") or "")
            prompt = self._parse_manual_click_prompt(
                state.get("prompt_text")
            )
            generation = int(state.get("generation") or 0)
            use_image = bool((capture or {}).get("identity_uses_image"))
            image_bytes = None
            if use_image:
                image_bytes = self._decode_click_image_source(source)
                if image_bytes is None:
                    image_bytes = self._clean_manual_click_screenshot(image)
            return self._click_challenge_id(
                source,
                image_bytes,
                prompt,
                generation,
                use_image=use_image,
            )
        except Exception:
            return ""

    def _business_captcha_loading_is_visible(self):
        try:
            loaders = self.page.locator(".layui-layer-loading2")
            for index in range(loaders.count()):
                if loaders.nth(index).is_visible():
                    return True
        except Exception:
            pass
        return False

    def _wait_for_manual_click_outcome(self, capture, expected_count):
        """等待旧题成功或真正换成新题，期间绝不重拍旧题或清空点位。"""
        deadline = time.monotonic() + self.web_timeout / 1000
        old_challenge_id = str((capture or {}).get("challenge_id") or "")
        old_event_challenge_id = str(
            (capture or {}).get("event_challenge_id")
            or old_challenge_id
        )
        fallback_retry_since = None
        fallback_retry_seconds = max(
            float(CONFIG["CAPTCHA_WAIT_SEC"]),
            CONFIG["BUSINESS_CAPTCHA_STABLE_MS"] / 1000,
        )

        while time.monotonic() < deadline:
            self._check_stopped()
            rapid_attempt = self._manual_click_attempt_from_events(
                exclude_challenge_id=old_event_challenge_id,
            )
            result_ready = self._business_result_is_ready()
            if result_ready:
                if rapid_attempt is not None:
                    return {"state": "passed_after_retry", **rapid_attempt}
                return {"state": "passed"}
            if rapid_attempt is not None:
                return {"state": "retry", **rapid_attempt}

            if (
                self._captcha_prompt_is_visible()
                and self._captcha_image_is_ready()
                and not self._business_captcha_loading_is_visible()
            ):
                current_challenge_id = self._current_manual_click_challenge_id(
                    capture
                )
                if (
                    old_challenge_id
                    and current_challenge_id
                    and current_challenge_id != old_challenge_id
                ):
                    return {
                        "state": "retry",
                        "capture": self._prepare_manual_click_capture(
                            reset_clicks=False
                        ),
                    }

                # 旧页面若无法提供题图身份，只在点位清空且持续稳定 5 秒后
                # 才进入重试，避免把成功加载中的短暂 DOM 状态判成失败。
                marker_count = len(self._manual_click_marker_points())
                if not old_challenge_id and marker_count < expected_count:
                    if fallback_retry_since is None:
                        fallback_retry_since = time.monotonic()
                    elif (
                        time.monotonic() - fallback_retry_since
                        >= fallback_retry_seconds
                    ):
                        return {
                            "state": "retry",
                            "capture": self._prepare_manual_click_capture(
                                reset_clicks=False
                            ),
                        }
                else:
                    fallback_retry_since = None

            self.page.wait_for_timeout(CONFIG["BUSINESS_CAPTCHA_POLL_MS"])

        if self._business_result_is_ready():
            return {"state": "passed"}
        return {"state": "timeout"}

    def _report_successful_click_captcha(
        self,
        capture,
        points,
        *,
        model_version,
        assisted,
    ):
        if not self._collection_enabled():
            return
        attempt = {
            "model_version": model_version,
            "assisted": assisted,
        }
        if not self._sample_collection_enabled() or not capture:
            self._emit_captcha_attempt(success=True, **attempt)
            return
        prompt = list(capture.get("prompt") or [])
        points = list(points or [])
        if not prompt or len(prompt) != len(points):
            self.log.emit(
                "⚠️ 点选验证码已通过，但采集点位不完整"
                f"（{len(points)}/{len(prompt)}），本次仅记录成功统计。"
            )
            self._emit_captcha_attempt(success=True, **attempt)
            return
        attempt.update(
            {
                "image_bytes": capture.get("image_bytes"),
                "answer": {
                    "prompt": prompt,
                    "points": points,
                },
            }
        )
        self._emit_captcha_attempt(success=True, **attempt)

    def _complete_manual_click_captcha(self, model_version):
        capture = self._prefetched_manual_click_capture
        self._prefetched_manual_click_capture = None
        if capture is None:
            capture = self._prepare_manual_click_capture(reset_clicks=True)
        elif self._captcha_prompt_is_visible():
            capture_challenge_id = str(capture.get("challenge_id") or "")
            current_challenge_id = self._current_manual_click_challenge_id(
                capture
            )
            if (
                capture_challenge_id
                and current_challenge_id
                and capture_challenge_id != current_challenge_id
            ):
                self._emit_captcha_attempt(
                    success=False,
                    model_version=model_version,
                    assisted=True,
                )
                self._clear_manual_click_events(capture)
                capture = self._prepare_manual_click_capture(
                    reset_clicks=False
                )

        while self._running:
            self._check_stopped()
            prompt = list((capture or {}).get("prompt") or [])
            if prompt:
                expected_count = len(prompt)
            else:
                try:
                    prompt_text = self.page.locator(
                        ".verify-msg"
                    ).first.inner_text().strip()
                    expected_count = len(
                        self._parse_manual_click_prompt(prompt_text)
                    )
                except Exception:
                    expected_count = 3
            if expected_count <= 0:
                expected_count = 3

            if self.auto_continue:
                self.log.emit("⏳ 等待用户点完验证码...")
                while self._running:
                    self._check_stopped()
                    buffered_points = self._manual_click_points(capture)
                    if (
                        len(buffered_points) >= expected_count
                        or not self._captcha_prompt_is_visible()
                    ):
                        self.log.emit("✅ 用户已点完验证码")
                        break
                    time.sleep(0.2)
                if not self._running:
                    return False
            else:
                self.log.emit("⏸️ 请完成验证码后点击【继续执行】")
                self._paused = True
                self.pause_signal.emit()
                while self._paused and self._running:
                    time.sleep(0.2)
                self._check_stopped()

            points = self._manual_click_points(capture)
            rapid_attempt = self._manual_click_attempt_from_events(
                exclude_challenge_id=str(
                    (capture or {}).get("event_challenge_id")
                    or (capture or {}).get("challenge_id")
                    or ""
                )
            )
            # 点位立即冻结到 Python；之后网页即使刷新、隐藏或重建 DOM，
            # 本轮原图与答案也不会丢失。清空的只是下一轮事件缓冲。
            self._clear_manual_click_events(capture)
            time.sleep(0.5)
            if rapid_attempt is None:
                rapid_attempt = self._manual_click_attempt_from_events(
                    exclude_challenge_id=str(
                        (capture or {}).get("event_challenge_id")
                        or (capture or {}).get("challenge_id")
                        or ""
                    )
                )
            if self._business_result_is_ready():
                if rapid_attempt is not None:
                    self._emit_captcha_attempt(
                        success=False,
                        model_version=model_version,
                        assisted=True,
                    )
                    self._report_successful_click_captcha(
                        rapid_attempt["capture"],
                        rapid_attempt["points"],
                        model_version=model_version,
                        assisted=True,
                    )
                    self._clear_manual_click_events(
                        rapid_attempt["capture"]
                    )
                else:
                    self._report_successful_click_captcha(
                        capture,
                        points,
                        model_version=model_version,
                        assisted=True,
                    )
                self.log.emit("✅ 验证码通过，开始获取信息")
                return True

            # 保留原有的快速反馈顺序。这条日志只是网页尚未给出成功结果的
            # 初步状态；真正失败必须等到题图身份发生变化后才能确认。
            self.log.emit("❌ 验证码点击错误，请重新点击验证码...")
            outcome = self._wait_for_manual_click_outcome(capture, expected_count)
            state = outcome.get("state")
            if state == "passed":
                self._report_successful_click_captcha(
                    capture,
                    points,
                    model_version=model_version,
                    assisted=True,
                )
                self.log.emit("✅ 验证码通过，开始获取信息")
                return True
            if state == "passed_after_retry":
                # 新题已被用户迅速点完并通过，旧题必须明确记为失败，不能
                # 把旧图/旧坐标误归为成功样本。
                self._emit_captcha_attempt(
                    success=False,
                    model_version=model_version,
                    assisted=True,
                )
                self._report_successful_click_captcha(
                    outcome.get("capture"),
                    outcome.get("points") or [],
                    model_version=model_version,
                    assisted=True,
                )
                self._clear_manual_click_events(outcome.get("capture"))
                self.log.emit("✅ 验证码通过，开始获取信息")
                return True
            if state == "retry":
                self._emit_captcha_attempt(
                    success=False,
                    model_version=model_version,
                    assisted=True,
                )
                capture = outcome.get("capture")
                if capture is None and self._captcha_prompt_is_visible():
                    capture = self._prepare_manual_click_capture(
                        reset_clicks=False
                    )
                continue

            self.log.emit(
                "⚠️ 等待验证码结果超时，本次结果未确定，不保存错误样本。"
            )
            return False
        return False

    # ======================
    # 封装：你的专属验证码识别逻辑（完全未修改，原样封装）
    # ======================
    def solve_captcha(self):
        if DdddOcr is None:
            custom_model = self._active_model("click")
            if custom_model is not None:
                self._emit_captcha_attempt(
                    success=False,
                    model_version=custom_model.version,
                    assisted=False,
                )
            self.log.emit(f"❌ ddddocr 不可用，无法自动识别验证码：{DDDDOCR_IMPORT_ERROR}")
            return False
        try:
            self.log.emit("🤖 开始处理中文点选验证码")
            success = False
            det_ocr = DdddOcr(det=True, ocr=False, show_ad=False)
            rec_ocr = DdddOcr(det=False, ocr=True, show_ad=False)

            # 读取界面设置的重试次数
            captcha_max_retry = self.captcha_retry
            self.log.emit(f"🔢 验证码最大重试次数：{captcha_max_retry}次")
            for i in range(captcha_max_retry):
                self._check_stopped()
                attempt_model_version = "ddddocr-builtin"
                try:
                    if i > 0:
                        response_state = self._wait_for_business_response()
                        if response_state == "result":
                            self.log.emit("✅ 验证码已通过，营运信息已返回")
                            return True
                    prompt_el = self.page.locator(".verify-msg")
                    if not prompt_el.count():
                        self.log.emit("✅ 未检测到验证码，直接跳过")
                        return True

                    prompt = prompt_el.inner_text().strip()

                    match = re.search(r"【([^】]+)】", prompt)
                    if not match:
                        raise Exception("未获取验证码文字")
                    target_chars = [c.strip() for c in match.group(1).split(',')]
                    self.log.emit(f"🎯 系统需要依次点击的文字：{target_chars}")

                    img_el = self.page.locator(".back-img").first
                    # Preserve the page capture for learning. Detection and
                    # recognition use a sharpened copy below, while a
                    # successful sample must remain untouched.
                    original_img_bytes = img_el.screenshot()

                    # 整图锐化
                    pil_img_original = Image.open(
                        io.BytesIO(original_img_bytes)
                    ).convert("RGB")
                    pil_img_sharpen = pil_img_original.filter(
                        ImageFilter.UnsharpMask(radius=1, percent=150, threshold=3))
                    img_byte_sharpen = io.BytesIO()
                    pil_img_sharpen.save(img_byte_sharpen, format='PNG')
                    img_bytes_final = img_byte_sharpen.getvalue()

                    # 检测位置+从左到右排序
                    bboxes = det_ocr.detection(img_bytes_final)
                    if not bboxes:
                        raise Exception("未检测到任何文字位置")
                    bboxes.sort(key=lambda box: box[0])
                    self.log.emit(f"📍 第一步：锐化图检测完成，共 {len(bboxes)} 个区域")

                    # 逐区域识别
                    draw = ImageDraw.Draw(pil_img_sharpen)
                    char_position_map = {}
                    self.log.emit("📝 第二步：开始逐区域识别文字")

                    def get_font():
                        """跨平台字体获取"""
                        if sys.platform == "win32":
                            font_paths = [
                                "simhei.ttf",
                                "C:/Windows/Fonts/simhei.ttf",
                                "C:/Windows/Fonts/msyh.ttc",
                            ]
                        else:
                            # Linux/统信UOS 常见中文字体路径
                            font_paths = [
                                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",  # 文泉驿正黑
                                "/usr/share/fonts/truetype/arphic/ukai.ttc",  # 文鼎楷体
                                "/usr/share/fonts/truetype/arphic/uming.ttc",  # 文鼎明体
                                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                            ]

                        for path in font_paths:
                            try:
                                return ImageFont.truetype(path, 14)
                            except:
                                continue

                        return ImageFont.load_default()

                    # 使用
                    font = get_font()

                    for box_idx, bbox in enumerate(bboxes):
                        x1, y1, x2, y2 = bbox
                        padding = 3
                        x1 = max(0, x1 - padding)
                        y1 = max(0, y1 - padding)
                        x2 = min(pil_img_sharpen.width, x2 + padding)
                        y2 = min(pil_img_sharpen.height, y2 + padding)

                        crop_color = pil_img_sharpen.crop((x1, y1, x2, y2))
                        rotate_angle = 5

                        # 8路图像增强
                        crop_color_sharp = crop_color.filter(
                            ImageFilter.UnsharpMask(radius=0.5, percent=80, threshold=2))
                        crop_gray = crop_color.convert('L')
                        crop_gray_sharp = crop_gray.filter(ImageFilter.UnsharpMask(radius=0.5, percent=80, threshold=2))
                        crop_color_rot = crop_color.rotate(rotate_angle, expand=True)
                        crop_color_rot_sharp = crop_color_rot.filter(
                            ImageFilter.UnsharpMask(radius=0.5, percent=80, threshold=2))
                        crop_gray_rot = crop_gray.rotate(rotate_angle, expand=True)
                        crop_gray_rot_sharp = crop_gray_rot.filter(
                            ImageFilter.UnsharpMask(radius=0.5, percent=80, threshold=2))

                        enhancer = ImageEnhance.Contrast(crop_color)
                        crop_contrast = enhancer.enhance(1.5)
                        crop_contrast_sharp = crop_contrast.filter(
                            ImageFilter.UnsharpMask(radius=0.5, percent=80, threshold=2))
                        crop_contrast_rot = crop_contrast.rotate(rotate_angle, expand=True)
                        crop_contrast_rot_sharp = crop_contrast_rot.filter(
                            ImageFilter.UnsharpMask(radius=0.5, percent=80, threshold=2))

                        crop_binary = crop_gray.point(lambda p: 255 if p > 170 else 0)
                        crop_binary_rot = crop_binary.rotate(rotate_angle, expand=True)

                        streams = [
                            io.BytesIO(), io.BytesIO(), io.BytesIO(), io.BytesIO(),
                            io.BytesIO(), io.BytesIO(), io.BytesIO(), io.BytesIO()
                        ]
                        crop_color_sharp.save(streams[0], format='PNG')
                        crop_gray_sharp.save(streams[1], format='PNG')
                        crop_color_rot_sharp.save(streams[2], format='PNG')
                        crop_gray_rot_sharp.save(streams[3], format='PNG')
                        crop_contrast_sharp.save(streams[4], format='PNG')
                        crop_contrast_rot_sharp.save(streams[5], format='PNG')
                        crop_binary.save(streams[6], format='PNG')
                        crop_binary_rot.save(streams[7], format='PNG')

                        char_list = []
                        for s in streams:
                            res = rec_ocr.classification(s.getvalue()).strip()
                            if len(res) == 1 and '\u4e00' <= res <= '\u9fff':
                                char_list.append(res)

                        target_pool = [c for c in char_list if c in target_chars]
                        char = max(set(target_pool), key=target_pool.count) if target_pool else (
                            max(set(char_list), key=char_list.count) if char_list else "")
                        if not char:
                            continue
                        char_position_map[char] = ((x1 + x2) // 2, (y1 + y2) // 2)

                    # 画框+正常中文
                    #     draw.rectangle([x1, y1, x2, y2], outline="red", width=2)
                    #     draw.text((x1, y1 - 18), char, fill="red", font=font)
                    #
                    # pil_img_sharpen.save("captcha_debug.png")

                    self.log.emit(f"✅ 文字识别完成：{char_position_map}")

                    custom_prediction = self._predict_with_active_click_model(
                        original_img_bytes,
                        bboxes,
                        len(target_chars),
                    )
                    if custom_prediction is not None:
                        char_position_map, attempt_model_version = (
                            custom_prediction
                        )

                    # 全局匹配+点击
                    self.log.emit("🖱️ 第三步：全局两两对比相似度，开始最优匹配")
                    recog_chars = list(char_position_map.keys())
                    target_list = target_chars.copy()
                    final_match = {}
                    used_recog = set()
                    match_pool = []
                    for t_char in target_list:
                        for r_char in recog_chars:
                            if r_char not in used_recog:
                                py_target = get_pinyin(t_char)
                                py_recog = get_pinyin(r_char)
                                py_score = difflib.SequenceMatcher(None, py_target, py_recog).ratio()
                                char_score = difflib.SequenceMatcher(None, t_char, r_char).ratio()
                                score = py_score * 0.6 + char_score * 0.4
                                match_pool.append((-score, t_char, r_char))
                    self.log.emit("=" * 50)

                    match_pool.sort()
                    for score_neg, t_char, r_char in match_pool:
                        if t_char not in final_match and r_char not in used_recog:
                            final_match[t_char] = r_char
                            used_recog.add(r_char)
                        if len(final_match) == len(target_list):
                            break

                    click_list = []
                    for t in target_chars:
                        matched_char = final_match[t]
                        x, y = char_position_map[matched_char]
                        click_list.append((x, y, t, matched_char))

                    # 必须严格按照题目给出的文字顺序点击，不能按页面横坐标重排。
                    for x, y, target_char, matched_char in click_list:
                        self._check_stopped()
                        self.log.emit(f"✅ 按题目顺序点击：{target_char}→{matched_char}，坐标({x},{y})")

                        try:
                            img_el.click(position={"x": x, "y": y}, timeout=2000)
                        except:
                            self.log.emit("⚠️ 点击验证码超时，跳过本次")
                            break

                        self._check_stopped()
                        time.sleep(0.4)

                    self.log.emit("⏳ 等待页面响应，校验验证码结果...")
                    self._check_stopped()
                    self.page.wait_for_timeout(2000)

                    if not prompt_el.is_visible():
                        normalized_points = [
                            {
                                "x": round(x / pil_img_sharpen.width, 6),
                                "y": round(y / pil_img_sharpen.height, 6),
                            }
                            for x, y, _target, _matched in click_list
                        ]
                        self._report_successful_click_captcha(
                            {
                                "image_bytes": original_img_bytes,
                                "prompt": target_chars,
                            },
                            normalized_points,
                            model_version=attempt_model_version,
                            assisted=False,
                        )
                        self.log.emit("🎉 验证码验证通过！")
                        return True
                    else:
                        self._emit_captcha_attempt(
                            success=False,
                            model_version=attempt_model_version,
                            assisted=False,
                        )
                        self.log.emit("⚠️ 验证码未消失，点击错误，自动重试...")
                        self.page.locator(".verify-refresh").click()
                        time.sleep(2)

                except Exception as e:
                    self._emit_captcha_attempt(
                        success=False,
                        model_version=attempt_model_version,
                        assisted=False,
                    )
                    self.retry_signal.emit(
                        "business_captcha",
                        f"营运查询验证码第 {i + 1} 次失败：{str(e)[:80]}",
                    )
                    self.log.emit(f"❌ 第{i + 1}次验证失败：{str(e)[:80]}")
                    try:
                        self.page.locator(".verify-refresh").click()
                        time.sleep(2)
                    except:
                        self.log.emit("⚠️ 验证码刷新失败")
                    continue
            return False
        except Exception as e:
            self.log.emit(f"💥 验证码识别异常：{str(e)[:80]}")
            return False

    def run(self):
        workbook_writer = None
        final_result = "结束"
        try:
            try:
                df_original = pd.read_excel(
                    self.original_file,
                    engine='openpyxl',
                    dtype={"车辆所有人/企业": str, "回填状态": str, "运输证号_纯数字": str}
                )
            except PermissionError:
                self.log.emit("❌ 无法读取原始表，请先关闭 Excel/WPS 表格后再运行程序！")
                final_result = "失败"
                return

            # 校验原始表是否有"运输证号_纯数字"列
            if "运输证号_纯数字" not in df_original.columns:
                self.log.emit("❌ 原始表缺少[运输证号_纯数字]列，请先运行查询运输证号功能！")
                final_result = "失败"
                return

            if "车辆所有人/企业" not in df_original.columns:
                df_original["车辆所有人/企业"] = ""
            if "回填状态" not in df_original.columns:
                df_original["回填状态"] = ""

            workbook_writer = TargetedWorkbookWriter(
                self.original_file,
                ("车辆所有人/企业", "回填状态"),
            )

            def save_results(row_index):
                workbook_writer.write_row(
                    row_index,
                    {
                        "车辆所有人/企业": df_original.at[
                            row_index,
                            "车辆所有人/企业",
                        ],
                        "回填状态": df_original.at[row_index, "回填状态"],
                    },
                )
                workbook_writer.save()

            # 直接从原始表提取"车辆标识→运输证号"映射
            plate_to_cert = dict(zip(
                df_original["车辆标识"].astype(str).str.strip(),
                df_original["运输证号_纯数字"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
            ))

            total = len(df_original)
            self.progress.emit(0)
            self.log.emit(f"📌 原始表共 {total} 条，开始回填营运信息")

            if not self._create_browser_with_retries(
                "business_browser_start",
                "营运查询浏览器启动失败",
            ):
                self.log.emit("❌ 浏览器启动失败，程序退出")
                final_result = "失败"
                return

            idx = 0
            browser_restart_counts = {}
            while idx < total and self._running:
                row = df_original.iloc[idx]
                company = ""
                need_retry = False
                self.input_result = ""
                try:
                    self._check_stopped()
                except:
                    self.log.emit("🛑 回填已停止")
                    break

                car_full = str(row["车辆标识"]).strip()
                cert_no = plate_to_cert.get(car_full, "")

                if has_assisted_payment(row):
                    self.log.emit(f"✅ 已补缴，跳过：{car_full}")
                    df_original.at[idx, "车辆所有人/企业"] = "已补缴"
                    df_original.at[idx, "回填状态"] = "已补缴"
                    save_results(idx)
                    idx += 1
                    progress = int((idx / total) * 100)
                    self.progress.emit(progress)
                    continue

                # 然后再判断 车辆所有人/企业
                if "车辆所有人/企业" in df_original.columns:
                    val = str(df_original.at[idx, "车辆所有人/企业"]).strip()
                    stale_no_transport = (
                        val == "无运输证号"
                        and bool(cert_no)
                        and cert_no.lower() != "nan"
                    )
                    if val not in ["", "nan", "NaN", "None"] and not stale_no_transport:
                        self.log.emit(f"✅ 已有公司信息，跳过：{car_full}")
                        idx += 1
                        progress = int((idx / total) * 100)
                        self.progress.emit(progress)
                        continue
                    if stale_no_transport:
                        self.log.emit(
                            f"↻ 已取得运输证号，重新回填原“无运输证号”记录：{car_full}"
                        )

                if not cert_no or cert_no.lower() == "nan":
                    self.log.emit(f"ℹ️ 无运输证号：{car_full} → 无运输证号")
                    df_original.at[idx, "车辆所有人/企业"] = "无运输证号"
                    df_original.at[idx, "回填状态"] = "查询成功"
                    save_results(idx)
                    idx += 1
                    progress = int((idx / total) * 100)
                    self.progress.emit(progress)
                    continue

                try:
                    plate = car_full.split("_")[0]
                except:
                    plate = car_full

                self.log.emit(f"[{idx + 1}/{total}] 查询：{plate} | {cert_no}")
                need_manual_input = False

                try:
                    if not self.browser or not self.page:
                        if not self._create_browser_with_retries(
                            "business_browser",
                            "营运查询浏览器恢复失败",
                        ):
                            raise BrowserRecoveryError("营运查询浏览器无法重新启动")

                    self._load_business_query_page()

                    self.page.fill('input[placeholder="请输入车辆号牌"]', plate)
                    self.page.fill('input[placeholder="请输入道路运输证号"]', cert_no)
                    self.page.click("button[lay-filter='formVehicle']")
                    response_state = self._wait_for_business_response()
                    has_captcha = response_state == "captcha"

                    is_captcha_fail = False
                    captcha_passed = response_state == "result"

                    if self.auto_mode:
                        # 调用封装的验证码方法
                        success = not has_captcha or self.solve_captcha()
                        if not success:
                            self.log.emit("❌ 验证码识别全部失败！")
                            df_original.at[idx, "车辆所有人/企业"] = ""
                            df_original.at[idx, "回填状态"] = "验证码识别失败"
                            save_results(idx)
                            idx += 1
                            progress = int((idx / total) * 100)
                            self.progress.emit(progress)
                            continue
                        else:
                            is_captcha_fail = False
                            captcha_passed = True
                    else:
                        if not self.manual_at_captcha:
                            success = not has_captcha or self.solve_captcha()
                            if not success:
                                self.log.emit(f"❌ 自动识别{self.captcha_retry}次失败，转为人工接管")
                                captcha_passed = self._complete_manual_click_captcha(
                                    "human-fallback"
                                )
                            else:
                                captcha_passed = True
                        else:
                            if not has_captcha:
                                captcha_passed = True
                            else:
                                captcha_passed = self._complete_manual_click_captcha(
                                    "human-manual"
                                )

                    self._check_stopped()
                    if is_captcha_fail:
                        self.log.emit("ℹ️ 因验证码识别失败，跳过营运信息解析")
                    else:
                        # 等待结果
                        if captcha_passed:
                            self.log.emit("⏳ 验证码已通过，继续等待营运信息")
                        result_ready = self._wait_for_business_result()
                        if not result_ready:
                            self.log.emit(
                                f"⚠️ 网页加载超过 {self.web_timeout // 1000} 秒，"
                                "仍未获取到营运信息"
                            )
                        html = self.page.content().lower()
                        tip_loc = self.page.locator(".layui-layer-content")
                        has_no_data_tip = False
                        for tip_index in range(tip_loc.count()):
                            tip = tip_loc.nth(tip_index)
                            if not tip.is_visible():
                                continue
                            tip_text = tip.inner_text().strip()
                            if any(
                                key in tip_text
                                for key in ["系统无该营运车辆信息", "未查询到"]
                            ):
                                has_no_data_tip = True
                                break

                        if has_no_data_tip:
                            company = "无营运信息"
                            self.log.emit(f"ℹ️ 查询结果：无营运信息")
                        elif "个体" in html:
                            company = "个体经营"
                            self.log.emit(f"ℹ️ 查询结果：个体经营")
                        else:
                            try:
                                company = self._business_owner_name()
                                if not company:
                                    company = ""
                                else:
                                    # 清洗公司名称：去掉尾部的括号及其内容
                                    company = clean_company_name(company)
                            except:
                                company = ""

                        # ====================== 优化2：只有【人工接管模式】才弹输入框 ======================
                        need_manual_input = False
                        # 人工模式 + 页面已明确返回但公司为空 → 才弹框；技术超时不弹框。
                        if (
                            not self.auto_mode
                            and result_ready
                            and company.strip() == ""
                        ):
                            need_manual_input = True

                except Exception as e:
                    err_msg = str(e)
                    if is_recoverable_browser_error(e):
                        attempt = browser_restart_counts.get(idx, 0) + 1
                        browser_restart_counts[idx] = attempt
                        self.retry_signal.emit(
                            "business_browser",
                            f"营运查询浏览器异常重启：{err_msg[:80]}",
                        )
                        self.log.emit(
                            "⚠️ 营运查询页或浏览器失效，正在重新启动并重试"
                            f"当前记录（第 {attempt} 次，无次数上限）"
                        )
                        if self._create_browser_with_retries(
                            "business_browser",
                            "营运查询浏览器恢复失败",
                        ):
                            need_retry = True
                            company = ""
                        else:
                            self._check_stopped()
                            raise BrowserRecoveryError(
                                "营运查询浏览器重新启动失败"
                            ) from e
                    else:
                        company = ""

                if need_manual_input and company.strip() == "":
                    try:
                        self._check_stopped()
                        self.log.emit("⏸️ 未识别到公司名称，请手动输入")
                        self.input_signal.emit(car_full)
                        self._paused = True
                        self.pause_signal.emit()
                        while self._paused and self._running:
                            time.sleep(0.2)
                        self._check_stopped()
                        if self.input_result.strip():
                            company = self.input_result.strip()
                            # 清洗公司名称：去掉尾部的括号及其内容
                            company = clean_company_name(company)
                            self.log.emit(f"✅ 人工录入：{company}")
                    except:
                        pass

                if not need_retry:
                    if company:
                        df_original.at[idx, "车辆所有人/企业"] = company
                        df_original.at[idx, "回填状态"] = "查询成功"
                        self.log.emit(f"✅ 已获取信息：{company}")
                    else:
                        df_original.at[idx, "车辆所有人/企业"] = ""
                        if is_captcha_fail:
                            df_original.at[idx, "回填状态"] = "验证码识别失败"
                            self.log.emit(f"❌ 验证码识别失败，已标记状态")
                        else:
                            df_original.at[idx, "回填状态"] = "查询失败"
                            self.log.emit(f"❌ 查询失败，已标记状态")

                    if company:
                        self.stat_event.emit("business_backfill_completed", 1)

                    save_results(idx)
                    idx += 1
                    progress = int((idx / total) * 100)
                    self.progress.emit(progress)

            workbook_writer.save()
            self.log.emit("✅ 所有数据回填完成！")

        except Exception as e:
            if "手动停止" in str(e):
                self.log.emit("🛑 已手动停止回填")
            elif isinstance(e, PermissionError):
                self.log.emit("❌ 无法读写文件，请先关闭 Excel/WPS 表格后再运行程序！")
                final_result = "失败"
                return
            else:
                self.log.emit(f"❌ 回填异常：{str(e)[:200]}")
                final_result = "失败"
        finally:
            self._close_browser()
            if workbook_writer is not None:
                workbook_writer.close()
            self._running = False
            self.finished.emit(final_result)

# ====================== 主界面 ======================

def is_excel_file_open(file_path):
    """
    跨平台终极检测：
    - Windows: 尝试打开（锁检测）
    - Linux/统信UOS: 调用 lsof 查是否被进程打开
    完全不修改、不写入、不破坏文件
    """
    if not file_path or not os.path.isfile(file_path):
        return False

    # Windows 逻辑（最准）
    if sys.platform.startswith("win"):
        try:
            with open(file_path, "r+"):
                pass
            return False
        except PermissionError:
            return True

    # Linux / 统信 UOS 逻辑（lsof 最准）
    else:
        try:
            result = subprocess.run(
                ["lsof", file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            return len(result.stdout.strip()) > 0
        except:
            return False
class MainWindow(QMainWindow):
    def __init__(self, stats_recorder=None):
        super().__init__()
        self._stats_recorder = stats_recorder
        self.setWindowTitle("运输证查询回填工具")
        self.setMinimumSize(800, 600)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(15, 15, 15, 15)

        # ====================== 新增：实时 Excel 状态提示条 ======================
        self.excel_status_label = QLabel("Excel表格状态正常")
        self.excel_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter if USING_PYQT6 else Qt.AlignCenter)
        self.excel_status_label.setStyleSheet("""
            QLabel {
                font-size: 14px;
                font-weight: bold;
                padding: 6px;
                color: #00AA00;
                background-color: #f0f9f0;
                border-radius: 6px;
            }
        """)
        main_layout.addWidget(self.excel_status_label)

        # ====================== 新增：定时器 实时检测 ======================
        self.excel_check_timer = QTimer()
        self.excel_check_timer.timeout.connect(self.check_excel_status)
        self.excel_check_timer.start(800)  # 每800ms检测一次
        # ==================================================================

        font_layout = QHBoxLayout()
        font_layout.addWidget(QLabel("界面字体："))
        self.font_size_combo = QComboBox()
        self.font_size_combo.addItems(["小号", "中号", "大号"])
        self.font_size_combo.setCurrentText("中号")
        self.font_size_combo.currentTextChanged.connect(self.change_font_size)
        font_layout.addWidget(self.font_size_combo)
        font_layout.addStretch()
        main_layout.addLayout(font_layout)

        file_group = QGroupBox("文件选择")
        file_layout = QHBoxLayout(file_group)
        self.src_edit = QLineEdit()
        self.src_edit.setPlaceholderText("原始表路径")
        file_layout.addWidget(QLabel("原始表："))
        file_layout.addWidget(self.src_edit)
        self.src_btn = QPushButton("选择原始表", clicked=self.choose_source_file)
        file_layout.addWidget(self.src_btn)
        main_layout.addWidget(file_group)

        browser_group = QGroupBox("浏览器")
        browser_layout = QHBoxLayout(browser_group)
        builtin_label = QLabel("🔧 正在使用自动适配的 Chromium 浏览器")
        builtin_label.setStyleSheet("font-weight: bold; color: #2d7d46;")
        browser_layout.addWidget(builtin_label)
        browser_layout.addStretch()
        main_layout.addWidget(browser_group)

        retry_group = QGroupBox("次数设置")
        retry_layout = QHBoxLayout(retry_group)
        retry_layout.addWidget(QLabel("列表查询重试次数："))
        self.page_retry_edit = QLineEdit("5")
        self.page_retry_edit.setFixedWidth(60)
        retry_layout.addWidget(self.page_retry_edit)
        retry_layout.addWidget(QLabel("  验证码重试次数："))
        self.captcha_retry_edit = QLineEdit("10")
        self.captcha_retry_edit.setFixedWidth(60)
        retry_layout.addWidget(self.captcha_retry_edit)
        self.infinite_captcha = QCheckBox("无限次重试")
        self.infinite_captcha.stateChanged.connect(self.toggle_mode_widgets)
        retry_layout.addWidget(self.infinite_captcha)
        retry_layout.addStretch()
        main_layout.addWidget(retry_group)

        mode_group = QGroupBox("运输证查询模式")
        mode_layout = QHBoxLayout(mode_group)
        self.auto_rdo = QRadioButton("全自动模式")
        self.manual_rdo = QRadioButton("人工接管模式")
        self.manual_at_captcha = QCheckBox("验证码人工输入")
        self.auto_continue_check = QCheckBox("自动继续（无需点继续执行）")
        self.auto_continue_check.stateChanged.connect(self.toggle_mode_widgets)
        self.only_yellow_card_check = QCheckBox("只查询黄牌")
        self.manual_rdo.setChecked(True)
        self.manual_at_captcha.setChecked(True)
        self.auto_continue_check.setChecked(True)
        self.only_yellow_card_check.setChecked(True)
        mode_layout.addWidget(self.auto_rdo)
        mode_layout.addWidget(self.manual_rdo)
        mode_layout.addWidget(self.manual_at_captcha)
        mode_layout.addWidget(self.auto_continue_check)
        mode_layout.addWidget(self.only_yellow_card_check)
        mode_layout.addStretch()
        main_layout.addWidget(mode_group)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("查询运输证号",
                                     styleSheet="background-color:#2ecc71; color:white; padding:8px; font-size:14px")
        self.backfill_btn = QPushButton("回填营运信息",
                                        styleSheet="background-color:#f39c12; color:white; padding:8px; font-size:14px")
        self.pause_btn = QPushButton("暂停",
                                    styleSheet="background-color:#9b59b6; color:white; padding:8px; font-size:14px")
        self.stop_btn = QPushButton("停止",
                                    styleSheet="background-color:#e74c3c; color:white; padding:8px; font-size:14px")
        self.resume_btn = QPushButton("继续执行",
                                      styleSheet="background-color:#3498db; color:white; padding:8px; font-size:14px")
        self.resume_btn.setEnabled(False)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.backfill_btn)
        btn_layout.addWidget(self.pause_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.resume_btn)
        main_layout.addLayout(btn_layout)

        self.progress_bar = QProgressBar()
        if USING_PYQT6:
            self.progress_bar.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        else:
            self.progress_bar.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #cccccc;
                border-radius: 6px;
                text-align: center;
                height: 18px;
            }
            QProgressBar::chunk {
                background-color: #2ecc71;
                border-radius: 5px;
            }
        """)
        main_layout.addWidget(self.progress_bar)

        log_group = QGroupBox("日志输出")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        main_layout.addWidget(log_group, stretch=1)

        self.start_btn.clicked.connect(self.start_task)
        self.backfill_btn.clicked.connect(self.start_backfill)
        self.pause_btn.clicked.connect(self.pause_task)
        self.stop_btn.clicked.connect(self.stop_task)
        self.resume_btn.clicked.connect(self.resume_task)
        self.auto_rdo.clicked.connect(self.toggle_mode_widgets)
        self.manual_rdo.clicked.connect(self.toggle_mode_widgets)
        self.manual_at_captcha.stateChanged.connect(self.toggle_mode_widgets)

        self.worker = None
        self.backfill_worker = None
        self.change_font_size("中号")
        self.toggle_mode_widgets()

    # ====================== 新增：实时检测Excel状态（核心） ======================
    def check_excel_status(self):
        src_path = self.src_edit.text().strip()
        src_open = is_excel_file_open(src_path)

        if src_open:
            self.excel_status_label.setText("当前导入Excel表格正在被打开，开始查询请先关闭Excel表格")
            self.excel_status_label.setStyleSheet("""
                QLabel {
                    font-size: 14px;
                    font-weight: bold;
                    padding: 6px;
                    color: #FF0000;
                    background-color: #fff0f0;
                    border-radius: 6px;
                }
            """)
        else:
            self.excel_status_label.setText("Excel表格状态正常")
            self.excel_status_label.setStyleSheet("""
                QLabel {
                    font-size: 14px;
                    font-weight: bold;
                    padding: 6px;
                    color: #00AA00;
                    background-color: #f0f9f0;
                    border-radius: 6px;
                }
            """)
    # ==========================================================================

    def toggle_mode_widgets(self):
        if self.auto_rdo.isChecked():
            self.manual_at_captcha.setEnabled(False)
            self.auto_continue_check.setEnabled(False)
            self.infinite_captcha.setEnabled(True)
            self.captcha_retry_edit.setEnabled(not self.infinite_captcha.isChecked())
        else:
            self.manual_at_captcha.setEnabled(True)
            self.infinite_captcha.setEnabled(False)
            self.infinite_captcha.setChecked(False)
            self.auto_continue_check.setEnabled(True)  # 始终启用自动继续
            if self.manual_at_captcha.isChecked():
                self.captcha_retry_edit.setEnabled(False)
            else:
                self.captcha_retry_edit.setEnabled(not self.infinite_captcha.isChecked())

    def change_font_size(self, size_text):
        font_map = {"小号": 9, "中号": 11, "大号": 13}
        font_size = font_map.get(size_text, 11)
        font = QFont()
        font.setPointSize(font_size)
        self.setFont(font)

    def log(self, msg):
        self.log_text.append(msg)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def choose_source_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择原始业务表", "", "Excel (*.xlsx)")
        if f:
            self.src_edit.setText(f)

    def start_task(self):
        src = self.src_edit.text().strip()
        if not src:
            QMessageBox.warning(self, "提示", "请选择原始表路径")
            return
        if not os.path.exists(src):
            QMessageBox.warning(self, "提示", "原始表不存在")
            return

        try:
            page_retry = int(self.page_retry_edit.text())
            if self.infinite_captcha.isChecked():
                captcha_retry = 9999
            else:
                captcha_retry = int(self.captcha_retry_edit.text())
        except:
            QMessageBox.warning(self, "错误", "次数、秒数必须是数字！")
            return

        auto_cont = self.auto_continue_check.isChecked()

        self._set_widgets_enabled(False)

        self.worker = Worker(
            src,
            self.auto_rdo.isChecked(),
            self.manual_at_captcha.isChecked(),
            page_retry,
            auto_cont,
            captcha_retry,
            self.only_yellow_card_check.isChecked(),
        )
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.pause_signal.connect(lambda: self.resume_btn.setEnabled(True))
        self.worker.input_signal.connect(self.show_input_dialog)
        self.worker.stat_event.connect(self._record_stat)
        self.worker.finished.connect(self._restore_widgets_state)
        self.worker.start()
        self.log("🚀 开始查询运输证号")

    def start_backfill(self):
        src = self.src_edit.text().strip()
        if not src:
            QMessageBox.warning(self, "提示", "请先选择原始表")
            return
        if not os.path.exists(src):
            QMessageBox.warning(self, "提示", "原始表文件不存在")
            return

        try:
            if self.infinite_captcha.isChecked():
                captcha_retry = 9999
            else:
                captcha_retry = int(self.captcha_retry_edit.text()) if self.captcha_retry_edit.text().isdigit() else 3
        except:
            QMessageBox.warning(self, "提示", "次数必须为数字")
            return

        auto_cont = self.auto_continue_check.isChecked()
        self._set_widgets_enabled(False)

        self.backfill_worker = BusinessBackfillWorker(
            src, auto_cont, self.auto_rdo.isChecked(), captcha_retry,
            self.manual_at_captcha.isChecked()
        )
        self.backfill_worker.log.connect(self.log)
        self.backfill_worker.progress.connect(self.progress_bar.setValue)
        self.backfill_worker.pause_signal.connect(lambda: self.resume_btn.setEnabled(True))
        self.backfill_worker.input_signal.connect(self.show_company_input_dialog)
        self.backfill_worker.stat_event.connect(self._record_stat)
        self.backfill_worker.finished.connect(self._restore_widgets_state)
        self.backfill_worker.start()
        self.log("🚀 开始从原始表读取运输证号，回填营运信息")

    def pause_task(self):
        if self.worker:
            self.worker.global_pause()
        if self.backfill_worker:
            self.backfill_worker.global_pause()
        self.log("⏸️ 已全局暂停，点击继续执行恢复")
        self.resume_btn.setEnabled(True)

    def _set_widgets_enabled(self, enabled):
        """线程运行时禁用/恢复所有控件"""
        widgets = [
            self.src_edit, self.src_btn,
            self.page_retry_edit, self.captcha_retry_edit,
            self.infinite_captcha,
            self.auto_rdo, self.manual_rdo, self.manual_at_captcha,
            self.only_yellow_card_check, self.auto_continue_check,
        ]
        for w in widgets:
            w.setEnabled(enabled)
        self.start_btn.setEnabled(enabled)
        self.backfill_btn.setEnabled(enabled)
        if enabled:
            self.resume_btn.setEnabled(False)

    def _restore_widgets_state(self, *_):
        """恢复控件状态：根据当前模式重新设置"""
        self._set_widgets_enabled(True)
        self.toggle_mode_widgets()

    def _record_stat(self, metric_key, amount):
        if self._stats_recorder:
            self._stats_recorder(metric_key, amount, "transport_tool")

    def stop_task(self):
        if self.worker:
            self.worker.stop()
        if self.backfill_worker:
            self.backfill_worker.stop()
        self.log("🛑 停止指令已发送，任务即将结束")

    def resume_task(self):
        if self.worker and self.worker.isRunning():
            self.worker.resume()
        if self.backfill_worker and self.backfill_worker.isRunning():
            self.backfill_worker.resume()
        self.resume_btn.setEnabled(False)
        self.log("▶️ 恢复执行")

    def show_input_dialog(self, plate):
        txt, ok = QInputDialog.getText(self, "人工录入", f"车牌：{plate}\n请输入运输证号：")
        if ok and txt:
            self.worker.input_result = txt
        self.worker.resume()

    def show_company_input_dialog(self, plate):
        txt, ok = QInputDialog.getText(self, "人工录入公司", f"车牌：{plate}\n请输入公司/所有人名称：")
        if ok and txt:
            self.backfill_worker.input_result = txt
        self.backfill_worker.resume()

    def shutdown(self, timeout_ms=5000):
        """停止仍在运行的工作线程，供统一客户端退出/注销时调用。"""
        all_stopped = True
        for worker in (self.worker, self.backfill_worker):
            if worker and worker.isRunning():
                worker.stop()
                if not worker.wait(timeout_ms):
                    all_stopped = False
        return all_stopped

    def closeEvent(self, event):
        self.shutdown()
        event.accept()


if __name__ == "__main__":
    # 关闭启动图
    try:
        import pyi_splash
        pyi_splash.close()
    except:
        pass

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
