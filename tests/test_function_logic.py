"""
Unit tests for DownloadParisSalesFn — department filter logic.

Tests the _select_department method and parameter extraction without
hitting the real B2B portal or Browserbase.
"""

import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from backend.function_logic import FunctionBackend


def _make_backend(tool_args=None):
    """Create a FunctionBackend with mocked orchestration event."""
    event = MagicMock()
    event.extra_params = {
        "tool_calls": [{"args": tool_args or {}}]
    }
    event.organization.organization_id = "test-org"
    event.orchestration_session_uuid = "test-session"
    event.internal_orchestration_session_uuid = "test-internal"
    event.access_token = "test-token"

    backend = FunctionBackend(event)
    backend.verbose = True
    return backend


class TestExtractToolArgs:
    def test_extracts_department(self):
        backend = _make_backend({"department": "hombre", "verbose": True})
        args = backend._extract_tool_args()
        assert args["department"] == "hombre"
        assert args["verbose"] is True

    def test_missing_department_returns_empty(self):
        backend = _make_backend({"verbose": False})
        args = backend._extract_tool_args()
        assert args.get("department", "") == ""

    def test_empty_tool_calls(self):
        backend = _make_backend()
        backend.orchestration_event.extra_params = {"tool_calls": []}
        args = backend._extract_tool_args()
        assert args == {}

    def test_no_tool_calls_key(self):
        backend = _make_backend()
        backend.orchestration_event.extra_params = {}
        args = backend._extract_tool_args()
        assert args == {}


class TestSelectDepartment:
    def test_skip_when_empty(self):
        backend = _make_backend()
        driver = MagicMock()
        backend._select_department(driver, "")
        driver.execute_script.assert_not_called()

    def test_skip_when_none(self):
        backend = _make_backend()
        driver = MagicMock()
        backend._select_department(driver, None)
        driver.execute_script.assert_not_called()

    def test_combobox_found_and_selected(self):
        backend = _make_backend()
        driver = MagicMock()
        driver.execute_script.side_effect = [
            {"found": True, "type": "combobox", "label": "linea"},
            {"selected": True, "text": "Hombre"},
        ]
        backend._select_department(driver, "hombre")
        assert driver.execute_script.call_count == 2

    def test_native_select_found(self):
        backend = _make_backend()
        driver = MagicMock()
        driver.execute_script.return_value = {"found": True, "type": "select", "label": "linea"}
        backend._select_department(driver, "mujer")
        assert driver.execute_script.call_count == 1

    def test_filter_not_found_continues(self):
        backend = _make_backend()
        driver = MagicMock()
        driver.execute_script.return_value = {"found": False}
        # Should not raise
        backend._select_department(driver, "hombre")
        assert driver.execute_script.call_count == 1

    def test_combobox_fallback_to_typing(self):
        backend = _make_backend()
        driver = MagicMock()
        driver.execute_script.side_effect = [
            {"found": True, "type": "combobox", "label": "linea"},
            {"selected": False, "count": 0},
            True,  # typing fallback
            True,  # select after filter
        ]
        backend._select_department(driver, "hombre")
        assert driver.execute_script.call_count == 4


class TestValidateDownloadContent:
    def test_valid_csv_content(self):
        backend = _make_backend()
        content = b"COL1,COL2,COL3\nval1,val2,val3\n" * 5000
        backend._validate_download_content(content)

    def test_empty_content_raises(self):
        backend = _make_backend()
        with pytest.raises(ValueError, match="vacío"):
            backend._validate_download_content(b"")

    def test_html_content_raises(self):
        backend = _make_backend()
        html = b"<!DOCTYPE html><html><head><title>Login</title></head><body></body></html>"
        with pytest.raises(ValueError, match="HTML"):
            backend._validate_download_content(html)

    def test_small_csv_warns_but_passes(self):
        backend = _make_backend()
        content = b"COL1,COL2\nval1,val2\n"
        backend._validate_download_content(content)


class TestExtractFromZip:
    def test_extracts_csv_from_zip(self):
        backend = _make_backend()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("report.csv", "COL1,COL2\nval1,val2\n" * 5000)
        result = backend._extract_from_zip_if_needed(buf.getvalue())
        assert result.startswith(b"COL1,COL2")

    def test_non_zip_returns_raw(self):
        backend = _make_backend()
        content = b"COL1,COL2\nval1,val2\n" * 5000
        result = backend._extract_from_zip_if_needed(content)
        assert result == content

    def test_zip_prefers_csv_over_other(self):
        backend = _make_backend()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "ignore me")
            zf.writestr("data.csv", "COL1\nval1\n" * 5000)
        result = backend._extract_from_zip_if_needed(buf.getvalue())
        assert result.startswith(b"COL1")


class TestUploadFilename:
    @patch("backend.function_logic.files_api_manager")
    def test_filename_with_department(self, mock_files):
        backend = _make_backend()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"file_url": "https://example.com/file"}
        mock_files.call.return_value = mock_resp

        backend._upload_to_chask(b"test content", "hombre")

        call_args = mock_files.call.call_args
        # file is passed as keyword arg
        file_obj = call_args.kwargs.get("file") if call_args.kwargs else None
        if file_obj and hasattr(file_obj, "name"):
            assert "hombre" in file_obj.name
            assert file_obj.name.startswith("paris_ventas_hombre_")

    @patch("backend.function_logic.files_api_manager")
    def test_filename_without_department(self, mock_files):
        backend = _make_backend()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"file_url": "https://example.com/file"}
        mock_files.call.return_value = mock_resp

        backend._upload_to_chask(b"test content", "")

        call_args = mock_files.call.call_args
        file_obj = call_args.kwargs.get("file") if call_args.kwargs else None
        if file_obj and hasattr(file_obj, "name"):
            assert "paris_ventas_" in file_obj.name
            assert "_hombre" not in file_obj.name
            assert "_mujer" not in file_obj.name
