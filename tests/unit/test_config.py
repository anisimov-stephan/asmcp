# tests/unit/test_config.py
"""Unit tests for config utilities."""

from unittest.mock import patch

from src.utils.config import _load_pyproject, get_project_description, get_project_header


class TestLoadPyproject:
    """Test _load_pyproject function."""

    def test_load_pyproject_success(self):
        # CONTRACT: TestLoadPyproject->Success->ReturnDict
        result = _load_pyproject()
        assert isinstance(result, dict)
        assert "project" in result

    def test_load_pyproject_file_not_found(self):
        # CONTRACT: TestLoadPyproject->FileNotFound->ReturnEmptyDict
        with patch("src.utils.config.Path") as mock_path:
            mock_instance = mock_path.return_value.parent.parent.parent.__truediv__.return_value
            mock_instance.exists.return_value = False
            mock_instance.__truediv__ = lambda s, o: mock_instance
            result = _load_pyproject()
            assert result == {}


class TestGetProjectHeader:
    """Test get_project_header function."""

    def test_get_project_header_success(self):
        # CONTRACT: TestGetProjectHeader->Success->ReturnHeader
        with patch(
            "src.utils.config._load_pyproject", return_value={"project": {"header": "MyApp"}}
        ):
            result = get_project_header()
            assert result == "MyApp"

    def test_get_project_header_default(self):
        # CONTRACT: TestGetProjectHeader->MissingKey->ReturnDefault
        with patch("src.utils.config._load_pyproject", return_value={"project": {}}):
            result = get_project_header()
            assert result == "Unknown Project"

    def test_get_project_header_empty_dict(self):
        # CONTRACT: TestGetProjectHeader->EmptyDict->ReturnDefault
        with patch("src.utils.config._load_pyproject", return_value={}):
            result = get_project_header()
            assert result == "Unknown Project"


class TestGetProjectDescription:
    """Test get_project_description function."""

    def test_get_project_description_success(self):
        # CONTRACT: TestGetProjectDescription->Success->ReturnDescription
        with patch(
            "src.utils.config._load_pyproject",
            return_value={"project": {"description": "Test description"}},
        ):
            result = get_project_description()
            assert result == "Test description"

    def test_get_project_description_default(self):
        # CONTRACT: TestGetProjectDescription->MissingKey->ReturnDefault
        with patch("src.utils.config._load_pyproject", return_value={"project": {}}):
            result = get_project_description()
            assert result == "No description available"

    def test_get_project_description_empty_dict(self):
        # CONTRACT: TestGetProjectDescription->EmptyDict->ReturnDefault
        with patch("src.utils.config._load_pyproject", return_value={}):
            result = get_project_description()
            assert result == "No description available"
