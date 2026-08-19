# src/utils/config.py
"""Configuration utilities."""

import tomllib
from pathlib import Path


def _load_pyproject() -> dict:
    # CONTRACT: Config->LoadPyproject->ReadToml
    """Load pyproject.toml data."""
    pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    if not pyproject_path.exists():
        return {}
    with open(pyproject_path, "rb") as f:
        return tomllib.load(f)


def get_project_header() -> str:
    # CONTRACT: Config->GetProjectHeader->ReadFromPyproject
    """Get project header from pyproject.toml."""
    data = _load_pyproject()
    return data.get("project", {}).get("header", "Unknown Project")


def get_project_description() -> str:
    # CONTRACT: Config->GetProjectDescription->ReadFromPyproject
    """Get project description from pyproject.toml."""
    data = _load_pyproject()
    return data.get("project", {}).get("description", "No description available")
