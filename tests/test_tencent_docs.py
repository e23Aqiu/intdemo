import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import openpyxl

from integrated_client.preferences import ClientPreferences
from integrated_client.tencent_docs import (
    TencentDocsBackfillWorker,
    TencentDocsImportError,
    TencentDocsImportWorker,
    _clipboard_html_to_tsv,
    _TencentDocsBrowserCapture,
    _TencentDocsLoginRequired,
    build_tencent_docs_backfill_plan,
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

    def test_backfill_plan_uses_pending_company_rows_and_target_columns(self):
        plan = build_tencent_docs_backfill_plan(
            self._clipboard(
                (
                    "车辆标识",
                    "车辆所有人/企业",
                    "负责人/法人代表",
                    "地址",
                    "电话",
                    "备注",
                ),
                (
                    ("车1", "已处理公司", "张三", "旧地址", "123", "保留"),
                    ("车2", "", "", "", "", "保留2"),
                    ("车3", "", "", "", "", "保留3"),
                ),
            ),
            2,
        )

        self.assertEqual(plan.start_row, 3)
        self.assertEqual(plan.row_count, 2)
        self.assertEqual(plan.available_row_count, 2)
        self.assertEqual(
            plan.target_columns,
            (
                ("公司名称", 1),
                ("法人/负责人", 2),
                ("地址", 3),
                ("电话", 4),
            ),
        )

    def test_backfill_plan_skips_address_when_online_sheet_has_no_address(self):
        plan = build_tencent_docs_backfill_plan(
            self._clipboard(
                ("车辆标识", "公司名称", "法人", "电话", "备注"),
                (("车1", "", "", "", "保留"),),
            ),
            1,
        )

        self.assertEqual(
            plan.target_columns,
            (
                ("公司名称", 1),
                ("法人/负责人", 2),
                ("电话", 3),
            ),
        )

    def test_backfill_plan_skips_all_missing_optional_columns(self):
        plan = build_tencent_docs_backfill_plan(
            self._clipboard(
                ("车辆标识", "企业名称", "备注"),
                (("车1", "", "保留"),),
            ),
            1,
        )

        self.assertEqual(plan.target_columns, (("公司名称", 1),))

    def test_backfill_plan_still_requires_online_company_column(self):
        with self.assertRaisesRegex(
            TencentDocsImportError,
            "未找到“公司名称”列",
        ):
            build_tencent_docs_backfill_plan(
                self._clipboard(
                    ("车辆标识", "法人", "地址", "电话"),
                    (("车1", "", "", ""),),
                ),
                1,
            )

    def test_backfill_plan_rejects_row_count_mismatch(self):
        clipboard = self._clipboard(
            (
                "车辆标识",
                "公司名称",
                "法人/负责人",
                "地址",
                "电话",
            ),
            (
                ("车1", "", "", "", ""),
                ("车2", "", "", "", ""),
            ),
        )

        with self.assertRaisesRegex(
            TencentDocsImportError,
            "腾讯文档有 2 行空位，本地有 1 行数据",
        ):
            build_tencent_docs_backfill_plan(clipboard, 1)

    def test_backfill_plan_counts_explicit_blank_sheet_rows(self):
        clipboard = self._clipboard(
            (
                "车辆标识",
                "公司名称",
                "法人/负责人",
                "地址",
                "电话",
            ),
            (
                ("车1", "", "", "", ""),
                ("", "", "", "", ""),
            ),
        )

        plan = build_tencent_docs_backfill_plan(clipboard, 2)

        self.assertEqual(plan.available_row_count, 2)

    def test_backfill_plan_does_not_overwrite_target_cells(self):
        clipboard = self._clipboard(
            (
                "车辆标识",
                "公司名称",
                "法定代表人",
                "注册地址",
                "联系电话",
            ),
            (("车1", "", "已有法人", "", ""),),
        )

        plan = build_tencent_docs_backfill_plan(clipboard, 1)
        with self.assertRaisesRegex(
            TencentDocsImportError,
            "已有内容",
        ):
            _TencentDocsBrowserCapture._validate_backfill_targets(
                clipboard,
                plan,
                (("甲公司", "张三", "", "123"),),
            )

    def test_backfill_worker_reports_success_and_progress(self):
        worker = TencentDocsBackfillWorker(
            "https://docs.qq.com/sheet/example",
            "admin",
            (("甲公司", "张三", "", "123"),),
            data_directory=self.temp_dir.name,
        )
        progress = []
        results = []
        worker.progress_changed.connect(progress.append)
        worker.backfill_succeeded.connect(results.append)
        expected = object()

        with patch.object(
            _TencentDocsBrowserCapture,
            "backfill",
            return_value=expected,
        ):
            worker.run()

        self.assertEqual(progress, [0, 100])
        self.assertEqual(results, [expected])

    def test_offline_guest_backfill_uses_isolated_browser_profile(self):
        worker = TencentDocsBackfillWorker(
            "https://docs.qq.com/sheet/example",
            "guest",
            (("甲公司", "张三", "地址", "123"),),
            data_directory=self.temp_dir.name,
        )
        captured_profiles = []

        def capture_factory(_url, profile_directory, *_args):
            captured_profiles.append(profile_directory)
            capture = MagicMock()
            capture.backfill.return_value = object()
            return capture

        with patch(
            "integrated_client.tencent_docs._TencentDocsBrowserCapture",
            side_effect=capture_factory,
        ):
            worker.run()

        self.assertEqual(
            captured_profiles,
            [
                Path(self.temp_dir.name)
                / "browser-profiles"
                / "tencent-docs"
                / "guest"
            ],
        )

    def test_backfill_retries_in_background_after_visible_login(self):
        capture = _TencentDocsBrowserCapture(
            "https://docs.qq.com/sheet/example",
            Path(self.temp_dir.name) / "profile",
            lambda _message: None,
            lambda: False,
        )
        runtime = object()
        expected = object()
        with patch.object(
            capture,
            "_backfill_once",
            side_effect=[_TencentDocsLoginRequired("需要登录"), expected],
        ) as backfill_once, patch.object(capture, "_login_once") as login_once, patch(
            "integrated_client.tencent_docs.sync_playwright",
        ) as sync_runtime:
            manager = MagicMock()
            manager.__enter__.return_value = runtime
            manager.__exit__.return_value = False
            sync_runtime.return_value = manager
            result = capture.backfill((("甲公司", "张三", "", "123"),))

        self.assertIs(result, expected)
        login_once.assert_called_once_with(runtime)
        self.assertEqual(backfill_once.call_count, 2)

    def test_backfill_column_skips_an_entirely_empty_optional_column(self):
        capture = _TencentDocsBrowserCapture(
            "https://docs.qq.com/sheet/example",
            Path(self.temp_dir.name) / "profile",
            lambda _message: None,
            lambda: False,
        )
        page = MagicMock()

        pasted = capture._paste_column(
            page,
            MagicMock(),
            {"width": 800, "height": 500},
            column_index=3,
            start_row=2,
            values=("", ""),
        )

        self.assertFalse(pasted)
        page.evaluate.assert_not_called()
        page.keyboard.press.assert_not_called()

    def test_backfill_only_pastes_columns_present_in_online_sheet(self):
        capture = _TencentDocsBrowserCapture(
            "https://docs.qq.com/sheet/example",
            Path(self.temp_dir.name) / "profile",
            lambda _message: None,
            lambda: False,
        )
        runtime = MagicMock()
        context = MagicMock()
        page = MagicMock()
        surface = MagicMock()
        box = {"width": 800, "height": 500}
        context.pages = [page]
        page.url = "https://docs.qq.com/sheet/example"
        runtime.chromium.launch_persistent_context.return_value = context
        before = self._clipboard(
            ("车辆标识", "公司名称", "法人", "电话", "备注"),
            (("车1", "", "", "", "保留"),),
        )
        after = self._clipboard(
            ("车辆标识", "公司名称", "法人", "电话", "备注"),
            (("车1", "甲公司", "张三", "123", "保留"),),
        )

        with patch.object(
            capture,
            "_wait_for_sheet_surface",
            return_value=(surface, box),
        ), patch(
            "integrated_client.tencent_docs.get_builtin_chromium_path",
            return_value=r"C:\browser\chrome.exe",
        ), patch.object(
            capture,
            "_has_authenticated_session",
            return_value=True,
        ), patch.object(
            capture,
            "_copy_selection",
            side_effect=(before, after),
        ), patch.object(
            capture,
            "_largest_sheet_surface",
            return_value=None,
        ), patch.object(capture, "_paste_column") as paste_column:
            result = capture._backfill_once(
                runtime,
                (("甲公司", "张三", "不应回填的地址", "123"),),
                headless=True,
                allow_login=False,
            )

        self.assertEqual(
            result.updated_columns,
            ("公司名称", "法人/负责人", "电话"),
        )
        self.assertEqual(
            paste_column.call_args_list,
            [
                call(
                    page,
                    surface,
                    box,
                    column_index=1,
                    start_row=2,
                    values=("甲公司",),
                ),
                call(
                    page,
                    surface,
                    box,
                    column_index=2,
                    start_row=2,
                    values=("张三",),
                ),
                call(
                    page,
                    surface,
                    box,
                    column_index=3,
                    start_row=2,
                    values=("123",),
                ),
            ],
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

    def test_authenticated_session_requires_known_qq_cookie(self):
        context = MagicMock()
        context.cookies.return_value = [
            {
                "name": "uin",
                "value": "o123456",
                "domain": ".docs.qq.com",
            }
        ]
        self.assertTrue(
            _TencentDocsBrowserCapture._has_authenticated_session(context)
        )
        context.cookies.return_value = [
            {
                "name": "anonymous_id",
                "value": "123",
                "domain": ".docs.qq.com",
            }
        ]
        self.assertFalse(
            _TencentDocsBrowserCapture._has_authenticated_session(context)
        )
        context.cookies.return_value = [
            {
                "name": "DOC_SID",
                "value": "encrypted-session",
                "domain": ".docs.qq.com",
            }
        ]
        self.assertTrue(
            _TencentDocsBrowserCapture._has_authenticated_session(context)
        )

    def test_page_authentication_check_uses_runtime_user_state(self):
        frame = MagicMock()
        page = type("Page", (), {"frames": [frame]})()
        frame.evaluate.return_value = True
        self.assertTrue(
            _TencentDocsBrowserCapture._page_reports_authenticated_user(page)
        )
        frame.evaluate.return_value = False
        self.assertFalse(
            _TencentDocsBrowserCapture._page_reports_authenticated_user(page)
        )

    def test_backfill_verification_checks_all_four_target_columns(self):
        plan = build_tencent_docs_backfill_plan(
            self._clipboard(
                (
                    "车辆标识",
                    "车辆所有人/企业",
                    "负责人/法人代表",
                    "地址",
                    "电话",
                ),
                (("车1", "", "", "", ""),),
            ),
            1,
        )
        copied = self._clipboard(
            (
                "车辆标识",
                "车辆所有人/企业",
                "负责人/法人代表",
                "地址",
                "电话",
            ),
            (("车1", "甲公司", "张三", "", "123"),),
        )

        _TencentDocsBrowserCapture._verify_backfill_values(
            copied,
            plan,
            (("甲公司", "张三", "", "123"),),
        )
        with self.assertRaisesRegex(
            TencentDocsImportError,
            "电话.*未正确保存",
        ):
            _TencentDocsBrowserCapture._verify_backfill_values(
                copied,
                plan,
                (("甲公司", "张三", "", "456"),),
            )

    def test_backfill_verification_leaves_existing_address_when_source_empty(self):
        plan = build_tencent_docs_backfill_plan(
            self._clipboard(
                (
                    "车辆标识",
                    "公司名称",
                    "法人/负责人",
                    "地址",
                    "电话",
                ),
                (("车1", "", "", "", ""),),
            ),
            1,
        )
        copied = self._clipboard(
            (
                "车辆标识",
                "公司名称",
                "法人/负责人",
                "地址",
                "电话",
            ),
            (("车1", "甲公司", "张三", "保留原地址", "123"),),
        )

        _TencentDocsBrowserCapture._verify_backfill_values(
            copied,
            plan,
            (("甲公司", "张三", "", "123"),),
        )

    def test_backfill_without_address_uses_local_phone_field(self):
        plan = build_tencent_docs_backfill_plan(
            self._clipboard(
                ("车辆标识", "公司名称", "法人", "电话"),
                (("车1", "", "", ""),),
            ),
            1,
        )
        before = self._clipboard(
            ("车辆标识", "公司名称", "法人", "电话"),
            (("车1", "", "", "已有电话"),),
        )

        with self.assertRaisesRegex(TencentDocsImportError, "电话.*已有内容"):
            _TencentDocsBrowserCapture._validate_backfill_targets(
                before,
                plan,
                (("甲公司", "张三", "本地地址", "123"),),
            )

        copied = self._clipboard(
            ("车辆标识", "公司名称", "法人", "电话"),
            (("车1", "甲公司", "张三", "123"),),
        )
        _TencentDocsBrowserCapture._verify_backfill_values(
            copied,
            plan,
            (("甲公司", "张三", "本地地址", "123"),),
        )
        with self.assertRaisesRegex(
            TencentDocsImportError,
            "电话.*未正确保存",
        ):
            _TencentDocsBrowserCapture._verify_backfill_values(
                copied,
                plan,
                (("甲公司", "张三", "本地地址", "456"),),
            )

    def test_nonempty_value_runs_preserve_blank_rows(self):
        self.assertEqual(
            _TencentDocsBrowserCapture._nonempty_value_runs(
                ("甲", "", "乙", "", "丙")
            ),
            ((0, ("甲",)), (2, ("乙",)), (4, ("丙",))),
        )

    def test_backfill_target_validation_rejects_only_values_that_would_overwrite(self):
        plan = build_tencent_docs_backfill_plan(
            self._clipboard(
                ("车辆标识", "公司名称", "法人", "地址", "电话"),
                (("车1", "", "", "", ""),),
            ),
            1,
        )
        copied = self._clipboard(
            ("车辆标识", "公司名称", "法人", "地址", "电话"),
            (("车1", "", "已有法人", "保留地址", ""),),
        )
        with self.assertRaisesRegex(TencentDocsImportError, "法人/负责人"):
            _TencentDocsBrowserCapture._validate_backfill_targets(
                copied,
                plan,
                (("甲公司", "张三", "", "123"),),
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
