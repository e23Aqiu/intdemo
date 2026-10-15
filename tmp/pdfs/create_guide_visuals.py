from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tmp" / "pdfs" / "ui"
OUT.mkdir(parents=True, exist_ok=True)

BG = "#edf3f2"
PANEL = "#ffffff"
CARD = "#f7faf9"
BORDER = "#c7d9d7"
TEXT = "#173a3d"
MUTED = "#647c7b"
TEAL = "#1c8d83"
TEAL_DARK = "#0f5655"
TEAL_SOFT = "#e4f3f0"
ORANGE = "#ef7f36"
GREY = "#a4b5b3"


def f(size: int, bold: bool = False):
    filename = "msyhbd.ttc" if bold else "msyh.ttc"
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / filename), size)


def rr(draw, box, radius=12, *, fill=PANEL, outline=BORDER, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def label(draw, xy, value, size=24, color=TEXT, bold=False, anchor=None):
    draw.text(xy, value, font=f(size, bold), fill=color, anchor=anchor)


def checkbox(draw, x, y, value, checked=True, disabled=False, size=22):
    color = GREY if disabled else TEAL
    rr(draw, (x, y, x + size, y + size), radius=5, fill=color if checked else PANEL,
       outline=color, width=2)
    if checked:
        draw.line((x + 5, y + 11, x + 10, y + 16, x + 18, y + 6), fill="white", width=3)
    label(draw, (x + size + 10, y - 3), value, 19, GREY if disabled else TEXT)


def button(draw, box, value, *, primary=False, disabled=False, size=19):
    if disabled:
        fill, outline, color = "#f2f5f5", "#dde6e5", "#a5b1b0"
    elif primary:
        fill, outline, color = TEAL, TEAL, "white"
    else:
        fill, outline, color = "#edf5f4", BORDER, TEXT
    rr(draw, box, radius=8, fill=fill, outline=outline, width=2)
    x1, y1, x2, y2 = box
    label(draw, ((x1 + x2) / 2, (y1 + y2) / 2 - 1), value, size, color, True,
          anchor="mm")


def field(draw, box, value, *, arrow=False, disabled=False, size=18):
    fill = "#f1f4f4" if disabled else PANEL
    color = GREY if disabled else TEXT
    rr(draw, box, radius=8, fill=fill, outline=BORDER, width=2)
    x1, y1, x2, y2 = box
    label(draw, (x1 + 16, (y1 + y2) / 2 - 1), value, size, color, anchor="lm")
    if arrow:
        cx, cy = x2 - 20, (y1 + y2) / 2
        draw.polygon([(cx - 6, cy - 3), (cx + 6, cy - 3), (cx, cy + 4)], fill=MUTED)


def callout(draw, number, box):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle((x1 - 5, y1 - 5, x2 + 5, y2 + 5), radius=10,
                           outline=ORANGE, width=5)
    radius = 20
    cx, cy = x1 - 4, y1 - 4
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                 fill=ORANGE, outline="white", width=3)
    label(draw, (cx, cy - 1), str(number), 21, "white", True, anchor="mm")


def header(draw, active_tab: str):
    label(draw, (38, 28), "一键业务处理", 34, TEXT, True)
    label(draw, (38, 75), "选择表格后，自动完成运输证查询、营运企业回填和爱企查信息补齐。",
          19, MUTED)
    tabs = [(38, 118, 205, 168, "业务处理"), (210, 118, 377, 168, "运行设置")]
    for x1, y1, x2, y2, value in tabs:
        active = value == active_tab
        rr(draw, (x1, y1, x2, y2), radius=9,
           fill=PANEL if active else "#e2ecea", outline=BORDER, width=2)
        label(draw, ((x1 + x2) / 2, (y1 + y2) / 2), value, 21,
              TEAL_DARK if active else MUTED, True, anchor="mm")


def make_settings():
    image = Image.new("RGB", (1400, 690), BG)
    d = ImageDraw.Draw(image)
    header(d, "运行设置")

    rr(d, (38, 183, 1362, 260), fill=PANEL)
    label(d, (64, 203), "内置浏览器状态", 21, TEXT, True)
    label(d, (64, 233), "Chromium（自动适配内置/系统）", 17, MUTED)
    label(d, (1010, 218), "● 运行正常", 18, TEAL, True)
    button(d, (1170, 199, 1280, 244), "立即检测", size=17)

    rr(d, (38, 278, 1362, 662), fill=PANEL)
    label(d, (64, 298), "处理参数", 23, TEXT, True)
    label(d, (64, 333), "根据业务场景配置自动化程度、验证码处理方式和查询范围。", 17, MUTED)

    cards = [(62, 370, 470, 636), (492, 370, 900, 636), (922, 370, 1338, 636)]
    for box in cards:
        rr(d, box, fill=CARD)

    label(d, (84, 391), "运行模式", 22, TEXT, True)
    label(d, (84, 426), "选择自动化程度，运行中仍可暂停或停止。", 16, MUTED)
    label(d, (84, 464), "自动化模式", 17, TEXT, True)
    mode_box = (84, 492, 448, 539)
    field(d, mode_box, "全自动模式", arrow=True, size=20)
    rr(d, (84, 551, 448, 595), radius=7, fill=TEAL_SOFT, outline="#c3e3dd")
    label(d, (98, 562), "全自动运行：优先自动识别验证码", 16, TEAL_DARK, True)
    checkbox(d, 84, 606, "爱企查兼容模式", checked=True, size=20)

    label(d, (514, 391), "验证码策略", 22, TEXT, True)
    label(d, (514, 426), "自动识别失败时按设置重试。", 16, MUTED)
    checkbox(d, 514, 462, "验证码人工处理", checked=True, disabled=True, size=20)
    checkbox(d, 514, 496, "人工处理后自动继续", checked=True, disabled=True, size=20)
    label(d, (514, 541), "识别重试", 17, TEXT, True)
    retry_box = (742, 531, 875, 571)
    field(d, retry_box, "10 次", size=18)
    infinite_box = (514, 586, 880, 618)
    checkbox(d, 514, 590, "验证码无限重试", checked=False, size=20)

    label(d, (944, 391), "查询策略", 22, TEXT, True)
    label(d, (944, 426), "控制运输证查询范围及页面重试。", 16, MUTED)
    checkbox(d, 944, 462, "运输证仅查询黄牌", checked=True, size=20)
    label(d, (944, 512), "列表重试", 17, TEXT, True)
    field(d, (1190, 502, 1314, 542), "5 次", size=18)
    label(d, (944, 562), "关闭黄牌限制后，将按表格中的实际", 15, MUTED)
    label(d, (944, 586), "车牌颜色查询。", 15, MUTED)

    callout(d, 1, (210, 118, 377, 168))
    callout(d, 2, mode_box)
    callout(d, 3, retry_box)
    callout(d, 4, infinite_box)
    image.save(OUT / "01-full-auto-settings.png")


def make_business():
    image = Image.new("RGB", (1400, 560), BG)
    d = ImageDraw.Draw(image)
    header(d, "业务处理")

    rr(d, (38, 183, 1362, 270), fill=PANEL)
    label(d, (58, 196), "业务表格", 17, TEXT, True)
    file_box = (58, 224, 800, 258)
    field(d, file_box, "腾讯文档导入后，生成的 Excel 会自动显示在这里", size=15)
    button(d, (814, 220, 928, 262), "选择表格", size=16)
    import_box = (940, 220, 1088, 262)
    button(d, import_box, "导入腾讯文档", primary=True, size=16)
    button(d, (1100, 220, 1244, 262), "打开导入文件夹", size=15)
    button(d, (1256, 220, 1342, 262), "刷新预览", size=15)

    steps = [
        (38, 288, 458, 382, "步骤 1", "查询运输证号"),
        (490, 288, 910, 382, "步骤 2", "回填营运企业"),
        (942, 288, 1362, 382, "步骤 3", "爱企查补齐信息"),
    ]
    for x1, y1, x2, y2, step, name in steps:
        rr(d, (x1, y1, x2, y2), fill=PANEL)
        label(d, (x1 + 20, y1 + 14), step, 16, MUTED, True)
        label(d, (x1 + 20, y1 + 42), name, 20, TEXT, True)
        label(d, (x1 + 20, y1 + 69), "等待执行", 15, MUTED)

    start_box = (38, 404, 1040, 458)
    button(d, start_box, "一键执行三个步骤", primary=True, size=21)
    button(d, (1052, 404, 1140, 458), "暂停", disabled=True, size=17)
    button(d, (1152, 404, 1264, 458), "继续执行", disabled=True, size=17)
    button(d, (1276, 404, 1362, 458), "停止", disabled=True, size=17)
    rr(d, (38, 476, 1362, 510), radius=8, fill=PANEL)
    rr(d, (40, 478, 180, 508), radius=7, fill=TEAL, outline=TEAL)
    label(d, (700, 530), "数据预览（导入完成后自动刷新）", 16, MUTED, True, anchor="mm")

    callout(d, 1, import_box)
    callout(d, 2, file_box)
    callout(d, 3, start_box)
    image.save(OUT / "02-business-page.png")


def make_dialog():
    image = Image.new("RGB", (1280, 810), BG)
    d = ImageDraw.Draw(image)
    rr(d, (20, 20, 1260, 790), radius=20, fill=PANEL)
    label(d, (60, 60), "导入腾讯文档", 39, TEXT, True)
    label(d, (60, 125), "请输入腾讯文档在线表格链接", 22, TEXT, True)
    url_box = (60, 170, 1220, 238)
    field(d, url_box, "https://docs.qq.com/sheet/粘贴您的腾讯文档链接", size=22)

    label(d, (60, 282), "跟进中心站", 22, TEXT)
    station_box = (330, 260, 1220, 328)
    field(d, station_box, "当前账号中心站", arrow=True, size=22)

    label(d, (60, 384), "下发日期", 22, TEXT)
    start_box = (330, 360, 660, 428)
    end_box = (890, 360, 1220, 428)
    field(d, start_box, "2026-10-12", arrow=True, size=22)
    label(d, (775, 394), "至", 22, TEXT, anchor="mm")
    field(d, end_box, "2026-10-12", arrow=True, size=22)

    check_box = (60, 470, 820, 516)
    checkbox(d, 60, 478, "仅导入“车辆所有人/企业”未填写的行", checked=True, size=27)
    label(d, (60, 548), "链接和腾讯文档登录状态会按当前程序账号保存在本机。", 19, MUTED)
    label(d, (60, 582), "新格式按“跟进中心站”筛选；旧格式自动忽略中心站。", 19, MUTED)

    button(d, (878, 672, 1030, 740), "取消", size=22)
    confirm_box = (1050, 672, 1220, 740)
    button(d, confirm_box, "确认并打开", primary=True, size=22)

    callout(d, 1, url_box)
    callout(d, 2, station_box)
    callout(d, 3, start_box)
    callout(d, 4, end_box)
    callout(d, 5, check_box)
    callout(d, 6, confirm_box)
    image.save(OUT / "03-tencent-import-dialog.png")


def make_progress():
    image = Image.new("RGB", (920, 420), BG)
    d = ImageDraw.Draw(image)
    rr(d, (20, 20, 900, 400), radius=20, fill=PANEL)
    label(d, (60, 58), "正在导入腾讯文档", 34, TEXT, True)
    label(d, (60, 128), "正在读取表格并按日期、中心站和未填写行筛选…", 20, TEXT, True)
    rr(d, (60, 194, 860, 238), radius=9, fill="#f6f9f9", outline=BORDER)
    rr(d, (62, 196, 560, 236), radius=8, fill=TEAL, outline=TEAL)
    label(d, (460, 216), "62%", 17, TEXT, anchor="mm")
    cancel_box = (60, 280, 860, 344)
    button(d, cancel_box, "取消导入", size=21)
    callout(d, 1, cancel_box)
    image.save(OUT / "04-tencent-progress.png")


def make_success():
    image = Image.new("RGB", (1000, 620), BG)
    d = ImageDraw.Draw(image)
    rr(d, (25, 25, 975, 595), radius=20, fill=PANEL)
    draw_circle = (68, 60, 128, 120)
    d.ellipse(draw_circle, fill=TEAL)
    d.line((84, 90, 96, 102, 115, 78), fill="white", width=6)
    label(d, (150, 67), "腾讯文档导入完成", 33, TEXT, True)
    label(d, (70, 150), "已识别原表第 1 行为表头，从 1459 条数据中复制 86 行。", 21, TEXT)
    label(d, (70, 203), "下发日期：2026-10-12 至 2026-10-12", 20, TEXT)
    label(d, (70, 244), "中心站：当前账号中心站", 20, TEXT)
    label(d, (70, 285), "仅筛选“车辆所有人/企业”未填写行：是", 20, TEXT)
    rr(d, (70, 350, 930, 457), radius=10, fill=TEAL_SOFT, outline="#c3e3dd")
    label(d, (90, 370), "新 Excel 已自动导入程序，本次导入不计入业务时间。", 20,
          TEAL_DARK, True)
    label(d, (90, 412), "请回到数据预览确认行数和内容，再开始执行。", 19, TEAL_DARK)
    button(d, (755, 500, 930, 554), "确定", primary=True, size=20)
    image.save(OUT / "05-tencent-success.png")


if __name__ == "__main__":
    make_settings()
    make_business()
    make_dialog()
    make_progress()
    make_success()
