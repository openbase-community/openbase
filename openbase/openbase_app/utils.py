import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from rest_framework.exceptions import NotFound


def get_django_apps():
    """
    Identifies Django apps by looking for directories containing an apps.py file
    across all configured app directories.
    """
    apps = []
    for apps_dir in settings.DJANGO_PROJECT_APPS_DIRS:
        if not apps_dir.exists():
            continue
        for item in apps_dir.iterdir():
            if item.is_dir():
                # Check if it's a Django app (common indicators: apps.py, models.py)
                if (item / "apps.py").exists() or (item / "models.py").exists():
                    # Store app info with directory context
                    app_info = {
                        "name": item.name,
                        "path": str(item),
                        "apps_dir": str(apps_dir),
                    }
                    # Avoid duplicates based on app name
                    if not any(app["name"] == item.name for app in apps):
                        apps.append(app_info)
    return apps


def find_app_directory(app_name: str) -> Optional[Path]:
    """
    Find the directory path for a given Django app across all configured directories.
    Returns the Path object for the app directory, or None if not found.
    """
    for apps_dir in settings.DJANGO_PROJECT_APPS_DIRS:
        if not apps_dir.exists():
            continue
        app_path = apps_dir / app_name
        if app_path.is_dir() and (
            (app_path / "apps.py").exists() or (app_path / "models.py").exists()
        ):
            return app_path
    return None


def find_app_file(
    app_name: str, file_path: str, raise_if_not_found: bool = False
) -> Optional[Path]:
    """
    Find a specific file within an app across all configured directories.
    Returns the Path object for the file, or None if not found.

    Args:
        app_name: Name of the Django app
        file_path: Relative path within the app (e.g., "models.py", "tasks/my_task.py")
        raise_if_not_found: If True, raise NotFound exception instead of returning None
    """
    app_dir = find_app_directory(app_name)
    if app_dir:
        target_file = app_dir / file_path
        if target_file.exists():
            return target_file

    if raise_if_not_found:
        raise NotFound(f"{file_path} not found for app {app_name}")
    return None


def get_source_line_col_for_ast_node(source_code: str, line: int, col: int) -> Tuple[int, int]:
    """
    Convert AST line/col numbers to actual source code positions for substring replacement.
    
    Args:
        source_code: The source code string
        line: AST line number (1-based)
        col: AST column offset (0-based)
    
    Returns:
        Tuple of (start_pos, end_pos) for substring operations
    """
    lines = source_code.split('\n')
    if line > len(lines):
        raise ValueError(f"Line {line} is beyond the source code length")
    
    # Calculate character position
    char_pos = sum(len(lines[i]) + 1 for i in range(line - 1)) + col
    return char_pos


def replace_ast_node_source(
    source_code: str, 
    start_line: int, 
    start_col: int, 
    end_line: int, 
    end_col: int, 
    replacement: str
) -> str:
    """
    Replace a section of source code based on AST line/column positions.
    
    Args:
        source_code: Original source code
        start_line: Starting line number (1-based)
        start_col: Starting column offset (0-based) 
        end_line: Ending line number (1-based)
        end_col: Ending column offset (0-based)
        replacement: New code to insert
    
    Returns:
        Modified source code
    """
    lines = source_code.split('\n')
    
    # Calculate start and end character positions
    start_pos = sum(len(lines[i]) + 1 for i in range(start_line - 1)) + start_col
    end_pos = sum(len(lines[i]) + 1 for i in range(end_line - 1)) + end_col
    
    # Perform replacement
    return source_code[:start_pos] + replacement + source_code[end_pos:]


def validate_app_exists(app_name: str) -> Path:
    """
    Validate that an app exists and return its directory path.
    Raises NotFound if the app doesn't exist.
    """
    app_dir = find_app_directory(app_name)
    if not app_dir:
        raise NotFound(f"App '{app_name}' not found")
    return app_dir


def get_file_list_from_directory(directory: Path, pattern: str = "*.py") -> List[Dict[str, str]]:
    """
    Get a list of files from a directory matching a pattern.
    
    Args:
        directory: Directory to search
        pattern: File pattern to match (default: "*.py")
    
    Returns:
        List of dicts with 'name' and 'file' keys
    """
    files = []
    if directory.exists():
        for file_path in directory.glob(pattern):
            if file_path.name == "__init__.py":
                continue
            files.append({
                "name": file_path.stem,
                "file": str(file_path)
            })
    return files