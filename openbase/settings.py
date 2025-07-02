import os
from pathlib import Path

# Parse comma-separated list of app directories
apps_dirs_str = os.environ.get("DJANGO_PROJECT_APPS_DIR", "")
if not apps_dirs_str:
    raise ValueError("DJANGO_PROJECT_APPS_DIR environment variable is required")

# Split by comma, strip whitespace, and convert to Path objects
DJANGO_PROJECT_APPS_DIRS = [
    Path(dir_path.strip()).resolve()
    for dir_path in apps_dirs_str.split(",")
    if dir_path.strip()
]

# Keep backward compatibility - first directory is the primary one
DJANGO_PROJECT_APPS_DIR = (
    DJANGO_PROJECT_APPS_DIRS[0] if DJANGO_PROJECT_APPS_DIRS else None
)

DJANGO_PROJECT_DIR = Path(os.environ["DJANGO_PROJECT_DIR"]).resolve()
