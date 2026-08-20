import os
import stat

from integrated_client.excel_files import (
    copy_excel_as_writable,
    is_excel_file_open,
    is_excel_file_read_only,
    make_excel_file_writable,
)


def test_read_only_workbook_is_not_reported_as_open(tmp_path):
    source = tmp_path / "只读业务表格.xlsx"
    source.write_bytes(b"workbook")
    source.chmod(stat.S_IREAD)
    try:
        assert is_excel_file_read_only(source)
        assert not is_excel_file_open(source)
    finally:
        source.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_read_only_workbook_can_be_copied_as_writable(tmp_path):
    source = tmp_path / "只读业务表格.xlsx"
    source.write_bytes(b"workbook")
    source.chmod(stat.S_IREAD)
    destination = tmp_path / "业务表格_可编辑.xlsx"
    try:
        result = copy_excel_as_writable(source, destination)
        assert result == destination.resolve()
        assert destination.read_bytes() == b"workbook"
        assert os.access(destination, os.W_OK)
        assert destination.stat().st_mode & stat.S_IWUSR
    finally:
        source.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_read_only_workbook_can_be_made_writable_in_place(tmp_path):
    source = tmp_path / "只读业务表格.xlsx"
    source.write_bytes(b"workbook")
    source.chmod(stat.S_IREAD)

    result = make_excel_file_writable(source)

    assert result == source.resolve()
    assert source.read_bytes() == b"workbook"
    assert not is_excel_file_read_only(source)
    assert os.access(source, os.W_OK)
    assert source.stat().st_mode & stat.S_IWUSR
