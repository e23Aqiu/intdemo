from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont
from PyQt5.QtCore import QDate, QPoint, Qt
from PyQt5.QtWidgets import QApplication

from integrated_client.ui.tencent_docs_dialog import (
    TencentDocsLinkDialog,
    TencentDocsProgressDialog,
)
from integrated_client.ui.theme import APP_STYLESHEET
from integrated_client.ui.workflow_page import WorkflowPage


OUT = ROOT / "tmp" / "pdfs" / "ui"
OUT.mkdir(parents=True, exist_ok=True)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / name), size)


def rect_for(widget, root):
    top_left = widget.mapTo(root, QPoint(0, 0))
    return (
        top_left.x(),
        top_left.y(),
        top_left.x() + widget.width(),
        top_left.y() + widget.height(),
    )


def annotate(path: Path, boxes, *, crop=None, labels=()):
    image = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(image)
    accent = "#ef7f36"
    fill = "#fff4e8"
    for number, box in boxes:
        x1, y1, x2, y2 = box
        pad = 5
        draw.rounded_rectangle(
            (x1 - pad, y1 - pad, x2 + pad, y2 + pad),
            radius=8,
            outline=accent,
            width=4,
        )
        radius = 16
        cx = max(radius + 3, x1 - 4)
        cy = max(radius + 3, y1 - 4)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=accent,
            outline="white",
            width=2,
        )
        value = str(number)
        bbox = draw.textbbox((0, 0), value, font=font(18, True))
        draw.text(
            (cx - (bbox[2] - bbox[0]) / 2, cy - (bbox[3] - bbox[1]) / 2 - 2),
            value,
            fill="white",
            font=font(18, True),
        )
    for text, position in labels:
        bbox = draw.textbbox((0, 0), text, font=font(17, True))
        x, y = position
        draw.rounded_rectangle(
            (x - 9, y - 6, x + bbox[2] + 9, y + bbox[3] + 8),
            radius=7,
            fill=fill,
            outline=accent,
            width=2,
        )
        draw.text((x, y), text, fill="#773a13", font=font(17, True))
    if crop:
        image = image.crop(crop)
    image.save(path, quality=95)


def grab(widget, filename: str) -> Path:
    widget.show()
    QApplication.processEvents()
    target = OUT / filename
    widget.grab().save(str(target), "PNG")
    return target


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)

    page = WorkflowPage(account_key="guide", account_name="示例账号")
    page._settings_browser_check_requested = True
    page.resize(1400, 860)

    # 运行设置：主动切换为全自动模式，展示真实 1.4.2 控件状态。
    page.page_tabs.setCurrentWidget(page.settings_tab)
    page.mode_combo.setCurrentIndex(page.mode_combo.findData(True))
    settings_path = grab(page, "01-full-auto-settings.png")
    tab_bar = page.page_tabs.tabBar()
    settings_tab_box = tab_bar.tabRect(page.page_tabs.indexOf(page.settings_tab))
    tab_origin = tab_bar.mapTo(page, QPoint(0, 0))
    settings_tab_rect = (
        tab_origin.x() + settings_tab_box.x(),
        tab_origin.y() + settings_tab_box.y(),
        tab_origin.x() + settings_tab_box.x() + settings_tab_box.width(),
        tab_origin.y() + settings_tab_box.y() + settings_tab_box.height(),
    )
    annotate(
        settings_path,
        [
            (1, settings_tab_rect),
            (2, rect_for(page.mode_combo, page)),
            (3, rect_for(page.captcha_retry, page)),
            (4, rect_for(page.infinite_captcha, page)),
        ],
        crop=(0, 0, 1400, 690),
    )

    # 业务处理：突出腾讯文档入口、已选表格区域和启动按钮。
    page.page_tabs.setCurrentWidget(page.workflow_tab)
    page.file_edit.setText("腾讯文档导入后，生成的 Excel 会自动显示在这里")
    business_path = grab(page, "02-business-page.png")
    annotate(
        business_path,
        [
            (1, rect_for(page.tencent_docs_btn, page)),
            (2, rect_for(page.file_edit, page)),
            (3, rect_for(page.start_btn, page)),
        ],
        crop=(0, 0, 1400, 560),
    )

    # 腾讯文档筛选弹窗。
    dialog = TencentDocsLinkDialog(
        "https://docs.qq.com/sheet/粘贴您的腾讯文档链接",
        current_station="当前账号中心站",
        available_stations=("其他中心站",),
        only_unfilled=True,
    )
    today = QDate(2026, 10, 12)
    dialog.date_start_edit.setDate(today)
    dialog.date_end_edit.setDate(today)
    dialog_path = grab(dialog, "03-tencent-import-dialog.png")
    confirm_button = None
    for button in dialog.findChildren(type(page.start_btn)):
        if button.text() == "确认并打开":
            confirm_button = button
            break
    boxes = [
        (1, rect_for(dialog.url_edit, dialog)),
        (2, rect_for(dialog.station_combo, dialog)),
        (3, rect_for(dialog.date_start_edit, dialog)),
        (4, rect_for(dialog.date_end_edit, dialog)),
        (5, rect_for(dialog.only_unfilled_check, dialog)),
    ]
    if confirm_button is not None:
        boxes.append((6, rect_for(confirm_button, dialog)))
    annotate(dialog_path, boxes)

    # 导入进度窗口。
    progress = TencentDocsProgressDialog()
    progress.set_status("正在读取表格并按日期、中心站和未填写行筛选…")
    progress.set_progress(62)
    progress_path = grab(progress, "04-tencent-progress.png")
    annotate(
        progress_path,
        [(1, rect_for(progress.cancel_button, progress))],
    )

    progress.close()
    dialog.close()
    page.close()
    app.processEvents()


if __name__ == "__main__":
    main()
