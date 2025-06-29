import os
from pathlib import Path

DJANGO_PROJECT_APPS_DIR = Path(os.environ["DJANGO_PROJECT_APPS_DIR"]).resolve()
DJANGO_PROJECT_DIR = Path(os.environ["DJANGO_PROJECT_DIR"]).resolve()
