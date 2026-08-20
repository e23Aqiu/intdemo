import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import openpyxl

from integrated_client.preferences import ClientPreferences
from integrated_client.tencent_docs import (
    TencentDocsImportError,
    TencentDocsImportWorker,
    _clipboard_html_to_tsv,
    _TencentDocsBrowserCapture,
    _TencentDocsLoginRequired,
    select_pending_tencent_rows,
    tencent_docs_import_directory,
    validate_tencent_docs_url,
    write_tencent_import_workbook,
)


class TencentDocsImportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _clipboard(headers, rows):
        return "\n".join(
            "\t".join(values)
            for values in (headers, *rows)
        )

    def test_rows_start_after_final_nonempty_company_cell(self):
        headers = ("车辆标识", "公司名称", "已协助补缴")
        rows = [
            (f"车{index}", company, "否")
            for index, company in enumerate(
                ("甲", "乙", "丙", "丁", "戊", "", "", "后补公司", "", ""),
                start=1,
            )
        ]

        selection = select_pending_tencent_rows(
            self._clipboard(headers, rows)
        )

        self.assertEqual(selection.headers, headers)
        self.assertEqual(selection.company_header, "公司名称")
        self.assertEqual(selection.source_header_row, 1)
        self.assertEqual(selection.source_start_row, 10)
        self.assertEqual(
            selection.rows,
            (
                ("车9", "", "否"),
                ("车10", "", "否"),
            ),
        )

    def test_all_rows_are_selected_when_company_column_is_empty(self):
        selection = select_pending_tencent_rows(
            self._clipboard(
                ("车辆标识", "车辆所有人/企业", "已协助补缴"),
                (
                    ("车1", "", "否"),
                    ("车2", "", "是"),
                ),
            )
        )

        self.assertEqual(selection.source_start_row, 2)
        self.assertEqual(len(selection.rows), 2)

    def test_second_row_header_is_detected_after_a_title_row(self):
        selection = select_pending_tencent_rows(
            self._clipboard(
                ("2026 年逃费车辆业务清单", "", ""),
                (
                    ("车辆标识", "公司名称", "备注"),
                    ("车1", "已处理公司", ""),
                    ("车2", "", "待处理"),
                ),
            )
        )

        self.assertEqual(selection.source_header_row, 2)
        self.assertEqual(selection.headers, ("车辆标识", "公司名称", "备注"))
        self.assertEqual(selection.company_header, "公司名称")
        self.assertEqual(selection.source_start_row, 4)
        self.assertEqual(selection.source_row_count, 2)
        self.assertEqual(selection.rows, (("车2", "", "待处理"),))

        destination = Path(self.temp_dir.name) / "second-row-pending.xlsx"
        write_tencent_import_workbook(selection, destination)
        workbook = openpyxl.load_workbook(destination)
        try:
            worksheet = workbook.active
            self.assertEqual(
                [cell.value for cell in worksheet[1]],
                ["车辆标识", "公司名称", "备注"],
            )
            self.assertEqual(
                [cell.value for cell in worksheet[2]],
                ["车2", None, "待处理"],
            )
        finally:
            workbook.close()

    def test_second_row_header_is_detected_after_a_blank_row(self):
        selection = select_pending_tencent_rows(
            "\t\t\n车辆标识\t车辆所有人/企业\t备注\n车1\t\t待处理"
        )

        self.assertEqual(selection.source_header_row, 2)
        self.assertEqual(selection.source_start_row, 3)
        self.assertEqual(selection.rows, (("车1", "", "待处理"),))

    def test_missing_company_header_is_reported(self):
        with self.assertRaisesRegex(
            TencentDocsImportError,
            "未找到“公司名称”列",
        ):
            select_pending_tencent_rows(
                self._clipboard(
                    ("车辆标识", "联系人"),
                    (("车1", ""),),
                )
            )

    def test_generated_workbook_keeps_header_and_pending_rows(self):
        selection = select_pending_tencent_rows(
            self._clipboard(
                ("车辆标识", "公司名称", "备注"),
                (
                    ("车1", "已处理公司", ""),
                    ("车2", "", "=保留为文本"),
                ),
            )
        )
        destination = Path(self.temp_dir.name) / "pending.xlsx"

        write_tencent_import_workbook(selection, destination)

        workbook = openpyxl.load_workbook(destination, data_only=False)
        try:
            worksheet = workbook.active
            self.assertEqual(
                [cell.value for cell in worksheet[1]],
                ["车辆标识", "公司名称", "备注"],
            )
            self.assertEqual(
                [cell.value for cell in worksheet[2]],
                ["车2", None, "=保留为文本"],
            )
            self.assertEqual(worksheet["C2"].data_type, "s")
            self.assertEqual(worksheet.freeze_panes, "A2")
        finally:
            workbook.close()

    def test_html_clipboard_preserves_multiline_header_cells(self):
        clipboard_html = """
            <html><body><table>
              <tr>
                <td>车</td>
                <td>跟进<br>中心站</td>
                <td>车辆所有人/企业</td>
              </tr>
              <tr><td>车1</td><td>萝岗</td><td>已处理公司</td></tr>
              <tr><td>车2</td><td>萝岗</td><td></td></tr>
            </table></body></html>
        """

        selection = select_pending_tencent_rows(
            _clipboard_html_to_tsv(clipboard_html)
        )

        self.assertEqual(selection.headers[1], "跟进\n中心站")
        self.assertEqual(selection.company_header, "车辆所有人/企业")
        self.assertEqual(selection.source_start_row, 3)
        self.assertEqual(
            selection.rows,
            (("车2", "萝岗", ""),),
        )

    def test_public_login_button_is_not_treated_as_login_prompt(self):
        class Frame:
            def __init__(self, text):
                self.text = text

            def locator(self, _selector):
                return self

            def inner_text(self, timeout):
                self.assert_timeout = timeout
                return self.text

        page = type(
            "Page",
            (),
            {"frames": [Frame("车 登录腾讯文档 开始 插入 数据")]},
        )()
        self.assertFalse(
            _TencentDocsBrowserCapture._login_prompt_visible(page)
        )
        page.frames[0].text = "请使用微信扫码登录"
        self.assertTrue(
            _TencentDocsBrowserCapture._login_prompt_visible(page)
        )

    def test_capture_uses_background_browser_before_visible_login_fallback(self):
        capture = _TencentDocsBrowserCapture(
            "https://docs.qq.com/sheet/example",
            Path(self.temp_dir.name) / "profile",
            lambda _message: None,
            lambda: False,
        )
        manager = MagicMock()
        runtime = object()
        manager.__enter__.return_value = runtime
        manager.__exit__.return_value = False

        with patch(
            "integrated_client.tencent_docs.sync_playwright",
            return_value=manager,
        ), patch.object(
            capture,
            "_capture_once",
            side_effect=[
                _TencentDocsLoginRequired("需要登录"),
                "copied",
            ],
        ) as capture_once:
            self.assertEqual(capture.capture(), "copied")

        self.assertEqual(
            capture_once.call_args_list,
            [
                call(runtime, headless=True, allow_login=False),
                call(runtime, headless=False, allow_login=True),
            ],
        )

    def test_import_worker_emits_determinate_progress_stages(self):
        worker = TencentDocsImportWorker(
            "https://docs.qq.com/sheet/example",
            "admin",
            data_directory=self.temp_dir.name,
        )
        progress = []
        results = []
        worker.progress_changed.connect(progress.append)
        worker.import_succeeded.connect(results.append)
        clipboard = self._clipboard(
            ("车辆标识", "公司名称", "已协助补缴"),
            (("车1", "", "否"),),
        )

        with patch.object(
            _TencentDocsBrowserCapture,
            "capture",
            return_value=clipboard,
        ), patch(
            "integrated_client.tencent_docs.write_tencent_import_workbook",
        ):
            worker.run()

        self.assertEqual(progress, [0, 84, 92, 100])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].copied_rows, 1)
        self.assertEqual(results[0].source_header_row, 1)
        self.assertEqual(
            results[0].path.parent,
            tencent_docs_import_directory("admin", self.temp_dir.name),
        )

    def test_import_directory_is_scoped_to_program_account(self):
        self.assertEqual(
            tencent_docs_import_directory(
                "Station User",
                self.temp_dir.name,
            ),
            Path(self.temp_dir.name)
            / "imports"
            / "tencent-docs"
            / "station-user",
        )

    def test_tencent_document_links_are_persisted_per_program_account(self):
        preferences = ClientPreferences(self.temp_dir.name)
        first = "https://docs.qq.com/sheet/first"
        second = "https://docs.qq.com/sheet/second"

        preferences.set_tencent_document_url("Admin", first)
        preferences.set_tencent_document_url("Station", second)

        reloaded = ClientPreferences(self.temp_dir.name)
        self.assertEqual(reloaded.tencent_document_url("admin"), first)
        self.assertEqual(reloaded.tencent_document_url("STATION"), second)

    def test_only_https_tencent_docs_links_are_accepted(self):
        valid = "https://docs.qq.com/sheet/example?tab=one"
        self.assertEqual(validate_tencent_docs_url(valid), valid)
        for invalid in (
            "http://docs.qq.com/sheet/example",
            "https://example.com/sheet/example",
            "javascript:alert(1)",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_tencent_docs_url(invalid)


if __name__ == "__main__":
    unittest.main()
