from __future__ import annotations

import csv
import io
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from PyQt5.QtCore import QThread, pyqtSignal

from .browser import get_builtin_chromium_path
from .config import get_data_dir

COMPANY_HEADER_ALIASES = (
    "公司名称",
    "企业名称",
    "车辆所有人/企业",
    "公司/所有人名称",
)
TENCENT_DOCS_HOST = "docs.qq.com"
TENCENT_DOCS_PUBLIC_WAIT_SECONDS = 60
TENCENT_DOCS_LOGIN_WAIT_SECONDS = 10 * 60
TENCENT_DOCS_COPY_WAIT_SECONDS = 12
_LOGIN_PROMPT_MARKERS = (
    "微信扫码登录",
    "qq扫码登录",
    "请使用微信扫码登录",
    "手机扫码登录",
    "账号密码登录",
)


class TencentDocsImportError(RuntimeError):
    pass


class TencentDocsImportCancelled(TencentDocsImportError):
    pass


class _TencentDocsLoginRequired(TencentDocsImportError):
    pass


@dataclass(frozen=True)
class TencentSheetSelection:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    company_header: str
    source_start_row: int
    source_row_count: int


@dataclass(frozen=True)
class TencentDocsImportResult:
    path: Path
    copied_rows: int
    source_start_row: int
    source_row_count: int
    company_header: str


def validate_tencent_docs_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or (
            hostname != TENCENT_DOCS_HOST
            and not hostname.endswith(f".{TENCENT_DOCS_HOST}")
        )
        or parsed.username
        or parsed.password
    ):
        raise ValueError("请输入 https://docs.qq.com 开头的腾讯文档链接")
    return url


def _normalized_header(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).casefold()


def _clean_cell(value) -> str:
    text = "" if value is None else str(value)
    return ILLEGAL_CHARACTERS_RE.sub("", text).strip("\ufeff")


class _ClipboardTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._table_depth = 0
        self._found_table = False
        self._row = None
        self._cell_parts = None
        self._cell_colspan = 1

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attributes = dict(attrs)
        if tag == "table":
            if not self._found_table:
                self._found_table = True
                self._table_depth = 1
            elif self._table_depth:
                self._table_depth += 1
            return
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            try:
                self._cell_colspan = max(
                    1,
                    int(attributes.get("colspan") or 1),
                )
            except (TypeError, ValueError):
                self._cell_colspan = 1
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append("\n")

    def handle_data(self, data):
        if self._table_depth and self._cell_parts is not None:
            self._cell_parts.append(str(data or ""))

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if not self._table_depth:
            return
        if (
            self._table_depth == 1
            and tag in {"td", "th"}
            and self._row is not None
            and self._cell_parts is not None
        ):
            self._row.append(_clean_cell("".join(self._cell_parts)))
            self._row.extend([""] * (self._cell_colspan - 1))
            self._cell_parts = None
            self._cell_colspan = 1
        elif self._table_depth == 1 and tag == "tr":
            if self._row is not None:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._table_depth -= 1


def _clipboard_html_to_tsv(clipboard_html: str) -> str:
    html = str(clipboard_html or "")
    if "<table" not in html.casefold():
        raise TencentDocsImportError("腾讯文档剪贴板中没有表格")
    parser = _ClipboardTableParser()
    try:
        parser.feed(html)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise TencentDocsImportError(
            f"无法解析腾讯文档表格结构：{exc}"
        ) from exc
    if not parser.rows:
        raise TencentDocsImportError("腾讯文档剪贴板中的表格为空")
    output = io.StringIO(newline="")
    writer = csv.writer(
        output,
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writerows(parser.rows)
    return output.getvalue()


def _clipboard_rows(clipboard_text: str) -> list[list[str]]:
    text = str(clipboard_text or "").replace("\x00", "")
    if not text.strip():
        raise TencentDocsImportError("腾讯文档没有复制出任何表格内容")
    try:
        rows = [
            [_clean_cell(value) for value in row]
            for row in csv.reader(io.StringIO(text), delimiter="\t")
        ]
    except csv.Error as exc:
        raise TencentDocsImportError(f"无法解析腾讯文档表格内容：{exc}") from exc
    while rows and not any(value.strip() for value in rows[-1]):
        rows.pop()
    if not rows or not any(value.strip() for value in rows[0]):
        raise TencentDocsImportError("腾讯文档首行没有可识别的表头")
    return rows


def _company_column_index(headers: list[str]) -> int:
    normalized = [_normalized_header(header) for header in headers]
    aliases = [_normalized_header(alias) for alias in COMPANY_HEADER_ALIASES]
    for alias in aliases:
        if alias in normalized:
            return normalized.index(alias)
    for index, header in enumerate(normalized):
        if "公司名称" in header or "企业名称" in header:
            return index
    available = "、".join(header for header in headers if header.strip()) or "（空）"
    raise TencentDocsImportError(
        "未找到“公司名称”列。支持的列名包括："
        f"{'、'.join(COMPANY_HEADER_ALIASES)}；当前表头：{available}"
    )


def select_pending_tencent_rows(clipboard_text: str) -> TencentSheetSelection:
    """Select every row after the final non-empty company-name cell."""

    rows = _clipboard_rows(clipboard_text)
    width = max(len(row) for row in rows)
    normalized_rows = [
        row[:width] + [""] * (width - len(row))
        for row in rows
    ]
    headers = normalized_rows[0]
    company_column = _company_column_index(headers)

    last_company_row_index = 0
    for row_index, row in enumerate(normalized_rows[1:], start=1):
        if row[company_column].strip():
            last_company_row_index = row_index

    pending_rows = normalized_rows[last_company_row_index + 1 :]
    # Clipboard APIs sometimes append empty rows even though they are outside
    # the sheet's used range. Preserve internal empty rows, but not that tail.
    while pending_rows and not any(value.strip() for value in pending_rows[-1]):
        pending_rows.pop()

    return TencentSheetSelection(
        headers=tuple(headers),
        rows=tuple(tuple(row) for row in pending_rows),
        company_header=headers[company_column],
        source_start_row=last_company_row_index + 2,
        source_row_count=max(0, len(normalized_rows) - 1),
    )


def write_tencent_import_workbook(
    selection: TencentSheetSelection,
    destination: str | Path,
) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.stem}-{uuid.uuid4().hex}.tmp.xlsx"
    )
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "待处理"
    try:
        for row_index, values in enumerate(
            (selection.headers, *selection.rows),
            start=1,
        ):
            for column_index, value in enumerate(values, start=1):
                cell = worksheet.cell(row=row_index, column=column_index)
                cell.value = _clean_cell(value)
                cell.data_type = "s"
        worksheet.freeze_panes = "A2"
        workbook.save(temporary)
        os.replace(temporary, destination)
    finally:
        workbook.close()
        temporary.unlink(missing_ok=True)
    return destination


def _safe_account_directory(account_key: str) -> str:
    normalized = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        str(account_key or "").strip().lower(),
    ).strip("-.")
    return normalized[:80] or "local"


class _TencentDocsBrowserCapture:
    def __init__(
        self,
        url: str,
        profile_directory: Path,
        status_callback,
        cancelled,
        progress_callback=None,
    ):
        self.url = validate_tencent_docs_url(url)
        self.profile_directory = profile_directory
        self.status_callback = status_callback
        self.cancelled = cancelled
        self.progress_callback = progress_callback
        self._last_status = ""
        self._last_progress = -1

    def _status(self, message: str) -> None:
        if message != self._last_status:
            self._last_status = message
            self.status_callback(message)

    def _progress(self, value: int) -> None:
        value = max(self._last_progress, min(100, max(0, int(value))))
        if value == self._last_progress:
            return
        self._last_progress = value
        if self.progress_callback is not None:
            self.progress_callback(value)

    def _check_cancelled(self) -> None:
        if self.cancelled():
            raise TencentDocsImportCancelled("已取消腾讯文档导入")

    @staticmethod
    def _largest_sheet_surface(page):
        candidates = []
        for frame in page.frames:
            for selector in (
                "[role='grid']",
                "[class*='sheet'] canvas",
                "[class*='grid'] canvas",
                "canvas",
            ):
                try:
                    rectangles = frame.eval_on_selector_all(
                        selector,
                        """elements => elements.slice(0, 80).map(
                            (element, index) => {
                                const rect = element.getBoundingClientRect();
                                const style = getComputedStyle(element);
                                return {
                                    index,
                                    width: rect.width,
                                    height: rect.height,
                                    visible:
                                        style.display !== "none" &&
                                        style.visibility !== "hidden" &&
                                        rect.width > 0 &&
                                        rect.height > 0
                                };
                            }
                        )""",
                    )
                except PlaywrightError:
                    continue
                for box in rectangles:
                    if (
                        box["visible"]
                        and box["width"] >= 320
                        and box["height"] >= 180
                    ):
                        item = frame.locator(selector).nth(box["index"])
                        candidates.append(
                            (box["width"] * box["height"], item, box)
                        )
        if not candidates:
            return None
        _, item, box = max(candidates, key=lambda candidate: candidate[0])
        return item, box

    @staticmethod
    def _login_prompt_visible(page) -> bool:
        for frame in page.frames:
            try:
                body_text = (
                    frame.locator("body")
                    .inner_text(timeout=500)
                    .casefold()
                )
            except PlaywrightError:
                continue
            if any(
                marker in body_text
                for marker in _LOGIN_PROMPT_MARKERS
            ):
                return True
        return False

    def _wait_for_sheet_surface(self, page, *, allow_login: bool):
        wait_seconds = (
            TENCENT_DOCS_LOGIN_WAIT_SECONDS
            if allow_login
            else TENCENT_DOCS_PUBLIC_WAIT_SECONDS
        )
        deadline = time.monotonic() + wait_seconds
        stable_since = None
        while time.monotonic() < deadline:
            self._check_cancelled()
            if page.is_closed():
                raise TencentDocsImportError("腾讯文档浏览器已被关闭")
            login_pending = self._login_prompt_visible(page)
            surface = self._largest_sheet_surface(page)
            if login_pending and not allow_login:
                raise _TencentDocsLoginRequired("腾讯文档需要登录")
            if login_pending:
                surface = None
            if surface is None:
                stable_since = None
                self._status(
                    "请在浏览器中完成腾讯文档登录，登录后程序会自动继续。"
                    if login_pending
                    else "等待腾讯文档表格加载完成…"
                )
            else:
                if stable_since is None:
                    stable_since = time.monotonic()
                    self._status("已检测到在线表格，正在等待数据加载完成…")
                    self._progress(35)
                if time.monotonic() - stable_since >= 2.0:
                    self._progress(45)
                    return surface
            page.wait_for_timeout(500)
        raise TencentDocsImportError(
            "等待腾讯文档加载超时；请确认链接有效且当前账号有查看权限"
        )

    @staticmethod
    def _prepare_copy_capture(page) -> None:
        script = """() => {
            window.__INTDEMO_COPY_CAPTURE__ = {
                text: "",
                html: "",
                events: 0
            };
            const remember = (type, data) => {
                const state = window.__INTDEMO_COPY_CAPTURE__;
                if (!state) return;
                const value = String(data || "");
                if (
                    (type === "text/plain" || type === "text") &&
                    value.length > state.text.length
                ) {
                    state.text = value;
                } else if (
                    type === "text/html" &&
                    value.length > state.html.length
                ) {
                    state.html = value;
                }
            };
            if (!window.__INTDEMO_COPY_LISTENER_INSTALLED__) {
                window.__INTDEMO_COPY_LISTENER_INSTALLED__ = true;
                document.addEventListener("copy", event => {
                    const state = window.__INTDEMO_COPY_CAPTURE__;
                    if (!state) return;
                    state.events += 1;
                    const transfer = event.clipboardData;
                    if (!transfer) return;
                    const collect = () => {
                        for (const type of Array.from(transfer.types || [])) {
                            try {
                                remember(type, transfer.getData(type));
                            } catch (_) {}
                        }
                    };
                    collect();
                    queueMicrotask(collect);
                }, false);
            }
            if (
                !window.__INTDEMO_SET_DATA_CAPTURE_INSTALLED__ &&
                window.DataTransfer?.prototype?.setData
            ) {
                window.__INTDEMO_SET_DATA_CAPTURE_INSTALLED__ = true;
                const originalSetData = DataTransfer.prototype.setData;
                DataTransfer.prototype.setData = function(type, data) {
                    remember(type, data);
                    return originalSetData.call(this, type, data);
                };
            }
        }"""
        for frame in page.frames:
            try:
                frame.evaluate(script)
            except PlaywrightError:
                continue

    @staticmethod
    def _captured_copy_payload(page) -> tuple[str, str]:
        copied_text = ""
        copied_html = ""
        for frame in page.frames:
            try:
                candidate = frame.evaluate(
                    """() => ({
                        text:
                            window.__INTDEMO_COPY_CAPTURE__?.text || "",
                        html:
                            window.__INTDEMO_COPY_CAPTURE__?.html || ""
                    })"""
                )
            except PlaywrightError:
                continue
            text = str(candidate.get("text") or "")
            html = str(candidate.get("html") or "")
            if len(text) > len(copied_text):
                copied_text = text
            if len(html) > len(copied_html):
                copied_html = html
        return copied_text, copied_html

    @staticmethod
    def _clipboard_payload(page) -> tuple[str, str]:
        payload = page.evaluate(
            """async () => {
                const payload = { text: "", html: "" };
                if (!navigator.clipboard) return payload;
                try {
                    payload.text =
                        await navigator.clipboard.readText() || "";
                } catch (_) {}
                try {
                    const items = await navigator.clipboard.read();
                    for (const item of items) {
                        for (const type of item.types || []) {
                            if (
                                type !== "text/plain" &&
                                type !== "text/html"
                            ) continue;
                            const value = await (
                                await item.getType(type)
                            ).text();
                            if (
                                type === "text/plain" &&
                                value.length > payload.text.length
                            ) {
                                payload.text = value;
                            } else if (
                                type === "text/html" &&
                                value.length > payload.html.length
                            ) {
                                payload.html = value;
                            }
                        }
                    }
                } catch (_) {}
                return payload;
            }"""
        )
        return (
            str(payload.get("text") or ""),
            str(payload.get("html") or ""),
        )

    def _copy_selection(self, page, surface, box, select_twice=False) -> str:
        click_x = max(24, min(box["width"] - 24, 120))
        click_y = max(24, min(box["height"] - 24, 100))
        surface.click(
            position={"x": click_x, "y": click_y},
            force=True,
            timeout=3_000,
        )
        page.keyboard.press("Control+Home")
        page.wait_for_timeout(150)
        page.keyboard.press("Control+A")
        if select_twice:
            page.wait_for_timeout(150)
            page.keyboard.press("Control+A")
        # The canvas can be visible before Tencent Docs has finished wiring
        # its spreadsheet keyboard handlers. Give the all-cells selection a
        # moment to settle before requesting the clipboard payload.
        page.wait_for_timeout(500)
        self._prepare_copy_capture(page)
        sentinel = f"__INTDEMO_{uuid.uuid4().hex}__"
        try:
            page.evaluate(
                """async value => {
                    if (navigator.clipboard) {
                        await navigator.clipboard.writeText(value);
                    }
                }""",
                sentinel,
            )
        except PlaywrightError:
            sentinel = ""
        page.keyboard.press("Control+C")
        deadline = time.monotonic() + TENCENT_DOCS_COPY_WAIT_SECONDS
        while time.monotonic() < deadline:
            self._check_cancelled()
            copied, copied_html = self._captured_copy_payload(page)
            if copied_html:
                try:
                    return _clipboard_html_to_tsv(copied_html)
                except TencentDocsImportError:
                    pass
            if copied and copied != sentinel:
                return copied
            try:
                copied, copied_html = self._clipboard_payload(page)
            except PlaywrightError:
                copied, copied_html = "", ""
            if copied_html:
                try:
                    return _clipboard_html_to_tsv(copied_html)
                except TencentDocsImportError:
                    pass
            if copied and copied != sentinel:
                return copied
            page.wait_for_timeout(250)
        raise TencentDocsImportError(
            "腾讯文档未能复制表格内容；请确认该文档允许复制"
        )

    def _capture_once(self, runtime, *, headless: bool, allow_login: bool) -> str:
        context = None
        try:
            launch_options = {
                "executable_path": get_builtin_chromium_path(runtime),
                "headless": headless,
                "accept_downloads": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
            }
            if headless:
                launch_options["viewport"] = {"width": 1440, "height": 900}
            else:
                launch_options["no_viewport"] = True
                launch_options["args"].append("--start-maximized")
            context = runtime.chromium.launch_persistent_context(
                str(self.profile_directory),
                **launch_options,
            )
            parsed = urlparse(self.url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            try:
                context.grant_permissions(
                    ["clipboard-read", "clipboard-write"],
                    origin=origin,
                )
            except PlaywrightError:
                pass
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(3_000)
            self._status(
                "正在后台打开腾讯文档链接…"
                if headless
                else "正在打开腾讯文档登录页面…"
            )
            self._progress(15)
            try:
                page.goto(
                    self.url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except PlaywrightError:
                # Tencent Docs can keep navigation pending while its
                # spreadsheet shell is already usable.
                if page.is_closed():
                    raise
            self._progress(25)
            surface, box = self._wait_for_sheet_surface(
                page,
                allow_login=allow_login,
            )
            current = urlparse(page.url)
            if current.scheme and current.netloc:
                try:
                    context.grant_permissions(
                        ["clipboard-read", "clipboard-write"],
                        origin=f"{current.scheme}://{current.netloc}",
                    )
                except PlaywrightError:
                    pass
            last_copy_error = None
            for attempt in range(1, 5):
                self._status(
                    "表格已加载，正在后台读取表头和已使用区域…"
                    if attempt == 1
                    else f"表格仍在初始化，正在重试读取（{attempt}/4）…"
                )
                self._progress(50 + attempt * 6)
                if attempt > 1:
                    page.wait_for_timeout(1_500)
                    refreshed = self._largest_sheet_surface(page)
                    if refreshed is not None:
                        surface, box = refreshed
                try:
                    copied = self._copy_selection(
                        page,
                        surface,
                        box,
                        select_twice=True,
                    )
                    select_pending_tencent_rows(copied)
                    self._progress(78)
                    return copied
                except TencentDocsImportCancelled:
                    raise
                except TencentDocsImportError as exc:
                    last_copy_error = exc
            if last_copy_error is not None:
                raise last_copy_error
            raise TencentDocsImportError("腾讯文档未能读取表格内容")
        except TencentDocsImportCancelled:
            raise
        except PlaywrightError as exc:
            raise TencentDocsImportError(
                f"腾讯文档浏览器操作失败：{exc}"
            ) from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except PlaywrightError:
                    pass

    def capture(self) -> str:
        self.profile_directory.mkdir(parents=True, exist_ok=True)
        self._status("正在启动后台浏览器…")
        self._progress(5)
        with sync_playwright() as runtime:
            self._check_cancelled()
            try:
                return self._capture_once(
                    runtime,
                    headless=True,
                    allow_login=False,
                )
            except _TencentDocsLoginRequired:
                self._check_cancelled()
                self._status(
                    "该文档需要登录，正在打开浏览器；登录后会自动继续。"
                )
                return self._capture_once(
                    runtime,
                    headless=False,
                    allow_login=True,
                )


class TencentDocsImportWorker(QThread):
    status_changed = pyqtSignal(str)
    progress_changed = pyqtSignal(int)
    import_succeeded = pyqtSignal(object)
    import_failed = pyqtSignal(str)
    import_cancelled = pyqtSignal()

    def __init__(
        self,
        url: str,
        account_key: str,
        data_directory: str | Path | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.url = validate_tencent_docs_url(url)
        self.account_key = str(account_key or "").strip().lower()
        self.data_directory = Path(data_directory or get_data_dir())

    def stop(self) -> None:
        self.requestInterruption()

    def run(self) -> None:
        account_directory = _safe_account_directory(self.account_key)
        profile_directory = (
            self.data_directory
            / "browser-profiles"
            / "tencent-docs"
            / account_directory
        )
        destination = None
        try:
            self.progress_changed.emit(0)
            capture = _TencentDocsBrowserCapture(
                self.url,
                profile_directory,
                self.status_changed.emit,
                self.isInterruptionRequested,
                self.progress_changed.emit,
            )
            clipboard_text = capture.capture()
            self.status_changed.emit("正在筛选待处理行并生成新 Excel 表格…")
            self.progress_changed.emit(84)
            selection = select_pending_tencent_rows(clipboard_text)
            timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            destination = (
                self.data_directory
                / "imports"
                / "tencent-docs"
                / account_directory
                / f"腾讯文档待处理-{timestamp}-{uuid.uuid4().hex[:6]}.xlsx"
            )
            self._check_interruption()
            self.progress_changed.emit(92)
            write_tencent_import_workbook(selection, destination)
            if self.isInterruptionRequested():
                destination.unlink(missing_ok=True)
                raise TencentDocsImportCancelled("已取消腾讯文档导入")
            self.progress_changed.emit(100)
            self.import_succeeded.emit(
                TencentDocsImportResult(
                    path=destination,
                    copied_rows=len(selection.rows),
                    source_start_row=selection.source_start_row,
                    source_row_count=selection.source_row_count,
                    company_header=selection.company_header,
                )
            )
        except TencentDocsImportCancelled:
            self.import_cancelled.emit()
        except (OSError, ValueError, TencentDocsImportError) as exc:
            self.import_failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - protects the Qt worker boundary
            self.import_failed.emit(f"腾讯文档导入失败：{exc}")

    def _check_interruption(self) -> None:
        if self.isInterruptionRequested():
            raise TencentDocsImportCancelled("已取消腾讯文档导入")
