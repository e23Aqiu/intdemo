from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "tmp" / "pdfs" / "ui"
OUTPUT = ROOT / "output" / "pdf" / "IntDemo_1.4.2及以上_全自动模式与腾讯文档导入操作指引.pdf"

W, H = A4
MARGIN = 42

COLORS = {
    "bg": HexColor("#F2F7F6"),
    "paper": HexColor("#FFFFFF"),
    "text": HexColor("#173A3D"),
    "muted": HexColor("#607A78"),
    "teal": HexColor("#1C8D83"),
    "teal_dark": HexColor("#0F5655"),
    "teal_soft": HexColor("#E4F3F0"),
    "border": HexColor("#C7D9D7"),
    "orange": HexColor("#EF7F36"),
    "orange_soft": HexColor("#FFF2E6"),
    "warn": HexColor("#A65420"),
    "grey": HexColor("#8AA09E"),
}


def setup_fonts():
    pdfmetrics.registerFont(TTFont("YaHei", "C:/Windows/Fonts/msyh.ttc"))
    pdfmetrics.registerFont(TTFont("YaHei-Bold", "C:/Windows/Fonts/msyhbd.ttc"))


def width(text: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, font, size)


def wrap(text: str, max_width: float, font="YaHei", size=10.5):
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and width(candidate, font, size) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return lines


def draw_paragraph(c, text, x, y, max_width, *, size=10.5, leading=16,
                   color=None, font="YaHei"):
    color = color or COLORS["text"]
    c.setFillColor(color)
    c.setFont(font, size)
    for line in wrap(text, max_width, font, size):
        c.drawString(x, y, line)
        y -= leading
    return y


def round_box(c, x, y, w, h, *, fill="paper", stroke="border", radius=10,
              line_width=1):
    c.setFillColor(COLORS[fill] if isinstance(fill, str) else fill)
    c.setStrokeColor(COLORS[stroke] if isinstance(stroke, str) else stroke)
    c.setLineWidth(line_width)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)


def badge(c, text, x, y, *, fill="teal", color=white, size=9.5, pad_x=10,
          height=24):
    w = width(text, "YaHei-Bold", size) + pad_x * 2
    c.setFillColor(COLORS[fill] if isinstance(fill, str) else fill)
    c.roundRect(x, y, w, height, height / 2, fill=1, stroke=0)
    c.setFillColor(color)
    c.setFont("YaHei-Bold", size)
    c.drawCentredString(x + w / 2, y + 7, text)
    return w


def standard_header(c, title, page_no):
    c.setFillColor(COLORS["teal_dark"])
    c.roundRect(MARGIN, H - 48, 20, 20, 5, fill=1, stroke=0)
    c.setFillColor(COLORS["paper"])
    c.setFont("YaHei-Bold", 10)
    c.drawCentredString(MARGIN + 10, H - 41, "查")
    c.setFillColor(COLORS["muted"])
    c.setFont("YaHei", 8.5)
    c.drawString(MARGIN + 29, H - 41, "IntDemo 操作指引")
    c.drawRightString(W - MARGIN, H - 41, "仅适用于 v1.4.2 及以上版本")
    c.setStrokeColor(COLORS["border"])
    c.line(MARGIN, H - 58, W - MARGIN, H - 58)

    c.setFillColor(COLORS["text"])
    c.setFont("YaHei-Bold", 22)
    c.drawString(MARGIN, H - 92, title)

    c.setStrokeColor(COLORS["border"])
    c.line(MARGIN, 34, W - MARGIN, 34)
    c.setFillColor(COLORS["muted"])
    c.setFont("YaHei", 8)
    c.drawString(MARGIN, 20, "全自动模式与腾讯文档导入")
    c.drawRightString(W - MARGIN, 20, f"{page_no} / 8")


def section_title(c, text, x, y, *, size=14):
    c.setFillColor(COLORS["teal"])
    c.roundRect(x, y - 3, 5, 20, 2.5, fill=1, stroke=0)
    c.setFillColor(COLORS["text"])
    c.setFont("YaHei-Bold", size)
    c.drawString(x + 14, y, text)


def numbered_item(c, number, title, body, x, y, max_width, *, body_size=9.5):
    c.setFillColor(COLORS["orange"])
    c.circle(x + 13, y - 1, 12, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("YaHei-Bold", 10)
    c.drawCentredString(x + 13, y - 5, str(number))
    c.setFillColor(COLORS["text"])
    c.setFont("YaHei-Bold", 10.5)
    c.drawString(x + 34, y + 1, title)
    y = draw_paragraph(c, body, x + 34, y - 15, max_width - 34,
                       size=body_size, leading=14, color=COLORS["muted"])
    return y - 8


def bullet(c, text, x, y, max_width, *, size=10, leading=15, color=None):
    color = color or COLORS["text"]
    c.setFillColor(COLORS["teal"])
    c.circle(x + 4, y + 3, 2.5, fill=1, stroke=0)
    return draw_paragraph(c, text, x + 14, y, max_width - 14, size=size,
                          leading=leading, color=color)


def draw_image(c, path, x, y, max_w, max_h, *, border=True):
    image = ImageReader(str(path))
    iw, ih = image.getSize()
    scale = min(max_w / iw, max_h / ih)
    w, h = iw * scale, ih * scale
    if border:
        round_box(c, x - 5, y - 5, w + 10, h + 10, fill="paper", stroke="border", radius=9)
    c.drawImage(image, x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
    return w, h


def draw_callout_legend(c, number, text, x, y, width_):
    c.setFillColor(COLORS["orange"])
    c.circle(x + 9, y + 4, 8, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("YaHei-Bold", 8)
    c.drawCentredString(x + 9, y + 1, str(number))
    return draw_paragraph(c, text, x + 23, y, width_ - 23, size=9.2,
                          leading=13, color=COLORS["text"])


def draw_table(c, x, y_top, col_widths, rows, *, header=True, row_heights=None,
               font_size=9.2):
    total_w = sum(col_widths)
    if row_heights is None:
        row_heights = [30] * len(rows)
    y = y_top
    for row_idx, row in enumerate(rows):
        h = row_heights[row_idx]
        y -= h
        c.setFillColor(COLORS["teal_dark"] if header and row_idx == 0 else
                       (COLORS["bg"] if row_idx % 2 == 0 else COLORS["paper"]))
        c.setStrokeColor(COLORS["border"])
        c.rect(x, y, total_w, h, fill=1, stroke=1)
        cell_x = x
        for col_idx, cell in enumerate(row):
            if col_idx:
                c.line(cell_x, y, cell_x, y + h)
            c.setFillColor(white if header and row_idx == 0 else COLORS["text"])
            font_name = "YaHei-Bold" if header and row_idx == 0 else "YaHei"
            c.setFont(font_name, font_size)
            lines = wrap(str(cell), col_widths[col_idx] - 14, font_name, font_size)
            line_y = y + h - 13
            for line in lines[:4]:
                c.drawString(cell_x + 7, line_y, line)
                line_y -= 12
            cell_x += col_widths[col_idx]
    return y


def page_cover(c):
    c.setFillColor(COLORS["bg"])
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(COLORS["teal_dark"])
    c.roundRect(0, 500, W + 20, 370, 30, fill=1, stroke=0)
    c.setFillColor(COLORS["teal"])
    c.circle(520, 760, 95, fill=1, stroke=0)
    c.setFillColor(HexColor("#F6B66B"))
    c.circle(505, 742, 34, fill=1, stroke=0)

    badge(c, "适用版本：IntDemo v1.4.2 及以上", MARGIN, 742,
          fill="orange", size=10.5, height=28)
    c.setFillColor(white)
    c.setFont("YaHei-Bold", 30)
    c.drawString(MARGIN, 675, "全自动模式与")
    c.drawString(MARGIN, 627, "腾讯文档导入操作指引")
    c.setFont("YaHei", 13)
    c.setFillColor(HexColor("#D8ECE9"))
    c.drawString(MARGIN, 583, "从在线表筛选、导入，到全自动执行三个业务步骤")
    c.drawString(MARGIN, 558, "面向日常业务人员的图文版说明")

    round_box(c, MARGIN, 344, W - 2 * MARGIN, 115, fill="paper", stroke="border", radius=14)
    c.setFillColor(COLORS["text"])
    c.setFont("YaHei-Bold", 14)
    c.drawString(MARGIN + 22, 424, "本指引包含")
    features = [
        ("01", "选择全自动模式", "设置验证码自动识别、重试方式并启动流水线"),
        ("02", "导入腾讯文档", "按日期、中心站和未填写行筛选在线表"),
    ]
    x = MARGIN + 22
    for no, title, body in features:
        c.setFillColor(COLORS["teal_soft"])
        c.circle(x + 18, 383, 18, fill=1, stroke=0)
        c.setFillColor(COLORS["teal_dark"])
        c.setFont("YaHei-Bold", 9)
        c.drawCentredString(x + 18, 379, no)
        c.setFont("YaHei-Bold", 11.5)
        c.drawString(x + 47, 391, title)
        draw_paragraph(c, body, x + 47, 373, 185, size=9, leading=13,
                       color=COLORS["muted"])
        x += 245

    round_box(c, MARGIN, 120, W - 2 * MARGIN, 158, fill="teal_soft", stroke="border", radius=14)
    c.setFillColor(COLORS["teal_dark"])
    c.setFont("YaHei-Bold", 13)
    c.drawString(MARGIN + 22, 242, "重要提醒")
    draw_paragraph(c,
        "1. 低于 1.4.2 的客户端不适用本指引，请先升级客户端。\n"
        "2. 腾讯文档导入只生成本机 Excel，不会修改在线表原始内容。\n"
        "3. 界面示意中的账号、中心站和数据条数仅用于说明，以实际界面为准。",
        MARGIN + 22, 214, W - 2 * MARGIN - 44, size=10.5, leading=22,
        color=COLORS["text"])
    c.setFillColor(COLORS["muted"])
    c.setFont("YaHei", 9)
    c.drawString(MARGIN, 67, "文档版本：1.0  |  编制日期：2026-10-12")
    c.drawRightString(W - MARGIN, 67, "IntDemo 用户操作文档")
    c.showPage()


def page_prepare(c):
    standard_header(c, "开始前确认", 2)
    y = H - 125
    round_box(c, MARGIN, y - 74, W - 2 * MARGIN, 68, fill="orange_soft", stroke="orange", radius=10)
    badge(c, "版本要求", MARGIN + 16, y - 40, fill="orange", size=9, height=22)
    c.setFillColor(COLORS["warn"])
    c.setFont("YaHei-Bold", 13)
    c.drawString(MARGIN + 110, y - 34, "仅支持 IntDemo v1.4.2 及以上版本")
    c.setFont("YaHei", 9.2)
    c.drawString(MARGIN + 110, y - 54, "可在客户端左侧栏底部或“系统设置”中核对版本号。")

    y -= 108
    section_title(c, "使用前准备", MARGIN, y)
    y -= 35
    items = [
        ("确认账号与中心站", "登录将实际执行业务的账号；中心站筛选默认使用该账号所属中心站。"),
        ("确认内置浏览器正常", "进入“运行设置”，检查“内置浏览器状态”为运行正常；异常时先点“立即检测”。"),
        ("准备腾讯文档链接", "链接必须以 https://docs.qq.com 开头，并确保当前腾讯文档账号具有查看和复制权限。"),
        ("检查表头", "表格需包含车牌、下发日期和公司字段；支持的别名见第 6 页。"),
    ]
    for idx, (title, body) in enumerate(items, 1):
        y = numbered_item(c, idx, title, body, MARGIN, y, W - 2 * MARGIN)

    y -= 2
    section_title(c, "推荐操作顺序", MARGIN, y)
    y -= 39
    flow = [
        "进入一键业务处理",
        "在运行设置中选择全自动模式",
        "返回业务处理并导入腾讯文档",
        "核对筛选结果和数据预览",
        "点击一键执行三个步骤",
    ]
    box_w = W - 2 * MARGIN
    for idx, text in enumerate(flow, 1):
        round_box(c, MARGIN, y - 34, box_w, 34, fill="paper", stroke="border", radius=8)
        c.setFillColor(COLORS["teal"])
        c.circle(MARGIN + 18, y - 17, 10, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("YaHei-Bold", 8.5)
        c.drawCentredString(MARGIN + 18, y - 20, str(idx))
        c.setFillColor(COLORS["text"])
        c.setFont("YaHei-Bold", 10)
        c.drawString(MARGIN + 38, y - 21, text)
        y -= 43

    round_box(c, MARGIN, 57, box_w, 55, fill="teal_soft", stroke="border", radius=9)
    draw_paragraph(c,
        "两项功能的关系：腾讯文档导入负责准备本次要处理的数据；全自动模式负责按当前规则连续执行后续业务步骤。",
        MARGIN + 14, 88, box_w - 28, size=9.5, leading=15, color=COLORS["teal_dark"],
        font="YaHei-Bold")
    c.showPage()


def page_auto(c):
    standard_header(c, "功能一：选择全自动模式", 3)
    y = H - 123
    y = draw_paragraph(c,
        "进入“一键业务处理”后，通过“运行设置”选择全自动模式。设置会按当前程序账号保存在本机。",
        MARGIN, y, W - 2 * MARGIN, size=10.5, leading=16, color=COLORS["muted"])
    img_y = 430
    draw_image(c, UI / "01-full-auto-settings.png", MARGIN, img_y, W - 2 * MARGIN, 265)
    c.setFillColor(COLORS["muted"])
    c.setFont("YaHei", 7.8)
    c.drawRightString(W - MARGIN, img_y - 14, "界面示意，以实际账号显示为准")

    y = 388
    legends = [
        (1, "点击“运行设置”页签。"),
        (2, "在“自动化模式”中选择“全自动模式”。"),
        (3, "设置验证码识别重试次数；默认示意为 10 次。"),
        (4, "如需持续尝试，可勾选“验证码无限重试”。"),
    ]
    for number, text in legends:
        y = draw_callout_legend(c, number, text, MARGIN, y, W - 2 * MARGIN) - 9

    round_box(c, MARGIN, 63, W - 2 * MARGIN, 102, fill="teal_soft", stroke="border", radius=10)
    c.setFillColor(COLORS["teal_dark"])
    c.setFont("YaHei-Bold", 11)
    c.drawString(MARGIN + 15, 143, "选择后会发生什么")
    bullet(c, "程序优先使用当前显示的数字验证码和点选验证码模型自动识别。",
           MARGIN + 15, 121, W - 2 * MARGIN - 30, size=9.2, leading=14, color=COLORS["teal_dark"])
    bullet(c, "“验证码人工处理”和“人工处理后自动继续”会变为不可选；运行中仍可暂停或停止。",
           MARGIN + 15, 94, W - 2 * MARGIN - 30, size=9.2, leading=14, color=COLORS["teal_dark"])
    c.showPage()


def page_start(c):
    standard_header(c, "全自动运行前的检查与启动", 4)
    draw_paragraph(c,
        "完成运行模式设置后，返回“业务处理”页。腾讯文档导入后的 Excel 会自动成为本次业务表格。",
        MARGIN, H - 123, W - 2 * MARGIN, size=10.5, leading=16, color=COLORS["muted"])
    img_y = 520
    draw_image(c, UI / "02-business-page.png", MARGIN, img_y, W - 2 * MARGIN, 215)

    y = 485
    for number, text in [
        (1, "点击“导入腾讯文档”进入筛选窗口。"),
        (2, "导入成功后，程序自动显示生成的 Excel 路径并刷新数据预览。"),
        (3, "确认预览无误后，点击“一键执行三个步骤”。"),
    ]:
        y = draw_callout_legend(c, number, text, MARGIN, y, W - 2 * MARGIN) - 10

    section_title(c, "启动前建议检查", MARGIN, y - 3)
    y -= 40
    checks = [
        "数据预览中的行数、车牌和日期是否符合本次处理范围；",
        "运行设置中“当前自动识别模型”是否为期望使用的模型；",
        "“运输证仅查询黄牌”、列表重试和验证码重试是否符合本批次要求；",
        "需要人工观察首批任务时，可先处理少量数据验证结果。",
    ]
    for text in checks:
        y = bullet(c, text, MARGIN, y, W - 2 * MARGIN, size=9.7, leading=15) - 8

    round_box(c, MARGIN, 60, W - 2 * MARGIN, 72, fill="orange_soft", stroke="orange", radius=9)
    c.setFillColor(COLORS["warn"])
    c.setFont("YaHei-Bold", 10.5)
    c.drawString(MARGIN + 15, 109, "注意")
    draw_paragraph(c,
        "全自动模式不是“忽略异常”。验证码重试耗尽后，程序仍会按当前步骤规则跳过或停止；请关注流水线日志。",
        MARGIN + 15, 88, W - 2 * MARGIN - 30, size=9.3, leading=14, color=COLORS["warn"])
    c.showPage()


def page_import(c):
    standard_header(c, "功能二：导入腾讯文档", 5)
    draw_paragraph(c,
        "在“业务处理”页点击“导入腾讯文档”，填写链接并设置筛选条件。日期范围包含开始日和结束日。",
        MARGIN, H - 123, W - 2 * MARGIN, size=10.5, leading=16, color=COLORS["muted"])
    img_y = 365
    draw_image(c, UI / "03-tencent-import-dialog.png", MARGIN + 8, img_y, W - 2 * MARGIN - 16, 320)

    c.setFillColor(COLORS["muted"])
    c.setFont("YaHei", 7.8)
    c.drawRightString(W - MARGIN, img_y - 13, "示例日期为文档编制日；实际默认显示当天")

    y_left, y_right = 325, 325
    left = [
        (1, "粘贴 docs.qq.com 在线表格链接。"),
        (2, "中心站默认当前账号中心站，可改为全部中心站。"),
        (3, "选择下发日期开始日，默认当天。"),
    ]
    right = [
        (4, "选择下发日期结束日，默认当天。"),
        (5, "按需勾选只导入公司字段未填写的行；该选项会记住。"),
        (6, "确认无误后点击“确认并打开”。"),
    ]
    col_w = (W - 2 * MARGIN - 18) / 2
    for number, text in left:
        y_left = draw_callout_legend(c, number, text, MARGIN, y_left, col_w) - 10
    for number, text in right:
        y_right = draw_callout_legend(c, number, text, MARGIN + col_w + 18, y_right, col_w) - 10

    round_box(c, MARGIN, 62, W - 2 * MARGIN, 78, fill="teal_soft", stroke="border", radius=9)
    c.setFillColor(COLORS["teal_dark"])
    c.setFont("YaHei-Bold", 10.5)
    c.drawString(MARGIN + 15, 117, "登录与保存")
    draw_paragraph(c,
        "公开表格可直接读取；需要登录时请在弹出的浏览器中完成登录。链接和登录状态按当前程序账号保存在本机。",
        MARGIN + 15, 95, W - 2 * MARGIN - 30, size=9.2, leading=14, color=COLORS["teal_dark"])
    c.showPage()


def page_rules(c):
    standard_header(c, "腾讯文档筛选规则与表格兼容", 6)
    y = H - 124
    section_title(c, "两种在线表的处理差异", MARGIN, y)
    y -= 24
    rows = [
        ["表格类型", "识别条件", "中心站规则", "共同筛选条件"],
        ["新格式", "包含“跟进中心站”列", "按选择的中心站筛选；选择“全部中心站”则不过滤", "日期范围；公司字段是否未填写"],
        ["旧格式", "不包含“跟进中心站”列", "自动忽略中心站选择", "日期范围；公司字段是否未填写"],
    ]
    y = draw_table(c, MARGIN, y, [70, 118, 158, 165], rows,
                   row_heights=[30, 58, 52], font_size=8.4)

    y -= 28
    section_title(c, "支持的表头别名", MARGIN, y)
    y -= 24
    alias_rows = [
        ["字段用途", "可识别列名"],
        ["车牌", "车牌、车辆标识"],
        ["下发日期", "下发时间、下发日期、下发日期时间、生成时间"],
        ["公司/所有人", "公司名称、企业名称、车辆所有人/企业、公司/所有人名称"],
        ["中心站", "跟进中心站（新格式可选列）"],
    ]
    y = draw_table(c, MARGIN, y, [105, 406], alias_rows,
                   row_heights=[29, 31, 43, 43, 31], font_size=8.8)

    y -= 30
    section_title(c, "筛选示例", MARGIN, y)
    y -= 32
    round_box(c, MARGIN, y - 116, W - 2 * MARGIN, 116, fill="paper", stroke="border", radius=10)
    c.setFillColor(COLORS["text"])
    c.setFont("YaHei-Bold", 10.5)
    c.drawString(MARGIN + 16, y - 24, "示例设置")
    draw_paragraph(c,
        "中心站：A 中心站；日期：2026-10-12 至 2026-10-12；勾选“仅导入未填写行”。",
        MARGIN + 16, y - 45, W - 2 * MARGIN - 32, size=9.2, leading=14)
    c.setFillColor(COLORS["teal_dark"])
    c.setFont("YaHei-Bold", 9.5)
    c.drawString(MARGIN + 16, y - 77, "新格式结果：")
    c.setFont("YaHei", 9.2)
    c.drawString(MARGIN + 88, y - 77, "只导入当天、A 中心站且公司字段为空的行。")
    c.setFont("YaHei-Bold", 9.5)
    c.drawString(MARGIN + 16, y - 100, "旧格式结果：")
    c.setFont("YaHei", 9.2)
    c.drawString(MARGIN + 88, y - 100, "忽略 A 中心站，只按当天和公司字段为空筛选。")

    round_box(c, MARGIN, 58, W - 2 * MARGIN, 52, fill="orange_soft", stroke="orange", radius=9)
    draw_paragraph(c,
        "若表头不在支持列表中，程序会提示无法识别；请先在腾讯文档中规范列名后重新导入。",
        MARGIN + 14, 87, W - 2 * MARGIN - 28, size=9.2, leading=14, color=COLORS["warn"],
        font="YaHei-Bold")
    c.showPage()


def page_progress(c):
    standard_header(c, "导入过程、取消与完成确认", 7)
    y = H - 124
    section_title(c, "导入进行中", MARGIN, y)
    draw_paragraph(c,
        "程序会打开腾讯文档、读取表格并应用筛选。页面初始化或登录时请耐心等待。",
        MARGIN, y - 24, W - 2 * MARGIN, size=9.8, leading=15, color=COLORS["muted"])
    draw_image(c, UI / "04-tencent-progress.png", MARGIN + 30, 485, W - 2 * MARGIN - 60, 185)
    draw_callout_legend(c, 1, "需要终止时点击“取消导入”；程序会关闭本次腾讯文档浏览器任务。",
                        MARGIN, 463, W - 2 * MARGIN)

    section_title(c, "导入完成", MARGIN, 424)
    draw_image(c, UI / "05-tencent-success.png", MARGIN + 55, 171, W - 2 * MARGIN - 110, 235)
    y = 143
    for text in [
        "核对复制行数、日期范围、中心站和未填写筛选结果；",
        "关闭提示后检查“数据预览”，再开始全自动处理；",
        "在线表后续发生变化时，需要重新执行一次导入。",
    ]:
        y = bullet(c, text, MARGIN, y, W - 2 * MARGIN, size=9.2, leading=14) - 5
    c.showPage()


def page_trouble(c):
    standard_header(c, "常见问题与最终检查", 8)
    y = H - 124
    rows = [
        ["现象", "处理方法"],
        ["提示链接无效", "确认链接以 https://docs.qq.com 开头，不要粘贴分享说明或其他文字。"],
        ["一直初始化或反复重试", "确认网络和文档权限；如出现登录页先完成登录。仍无响应时取消导入后重新打开。"],
        ["提示未找到下发日期列", "将列名改为“下发时间、下发日期、下发日期时间”或“生成时间”之一。"],
        ["筛选后没有数据", "放宽日期范围，检查中心站名称和未填写勾选项；旧格式不会按中心站筛选。"],
        ["全自动识别失败较多", "检查运行设置中的当前模型、验证码重试次数和浏览器状态，并查看流水线日志。"],
        ["找不到对应功能", "先确认客户端版本不低于 1.4.2；低版本请升级后再按本指引操作。"],
    ]
    y = draw_table(c, MARGIN, y, [155, 356], rows,
                   row_heights=[28, 44, 58, 48, 52, 52, 44], font_size=8.2)

    y -= 28
    section_title(c, "开始执行前的 6 项检查", MARGIN, y)
    y -= 31
    checklist = [
        "客户端版本为 1.4.2 或更高版本",
        "当前登录账号和中心站正确",
        "运行模式已选择“全自动模式”",
        "腾讯文档链接、日期和筛选项正确",
        "导入完成提示中的行数符合预期",
        "数据预览无误，内置浏览器状态正常",
    ]
    for idx, text in enumerate(checklist):
        col = idx % 2
        row = idx // 2
        x = MARGIN + col * 255
        yy = y - row * 42
        c.setStrokeColor(COLORS["teal"])
        c.setLineWidth(1.5)
        c.roundRect(x, yy - 3, 14, 14, 3, fill=0, stroke=1)
        draw_paragraph(c, text, x + 23, yy, 220, size=8.8, leading=13)

    round_box(c, MARGIN, 60, W - 2 * MARGIN, 78, fill="teal_soft", stroke="border", radius=10)
    c.setFillColor(COLORS["teal_dark"])
    c.setFont("YaHei-Bold", 11)
    c.drawString(MARGIN + 15, 115, "完成以上检查后")
    draw_paragraph(c,
        "返回“业务处理”页，点击“一键执行三个步骤”。运行期间可通过暂停、继续执行和停止按钮控制任务，并通过流水线日志查看进度。",
        MARGIN + 15, 93, W - 2 * MARGIN - 30, size=9.3, leading=14, color=COLORS["teal_dark"])
    c.showPage()


def build():
    setup_fonts()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(OUTPUT), pagesize=A4, pageCompression=1)
    c.setTitle("IntDemo 1.4.2及以上：全自动模式与腾讯文档导入操作指引")
    c.setAuthor("IntDemo")
    c.setSubject("全自动模式和腾讯文档导入功能操作指引")
    for page in (
        page_cover,
        page_prepare,
        page_auto,
        page_start,
        page_import,
        page_rules,
        page_progress,
        page_trouble,
    ):
        page(c)
    c.save()
    print(OUTPUT)


if __name__ == "__main__":
    build()
