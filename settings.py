import os
from pathlib import Path

DJANGO_PROJECT_BASE_DIR = Path(os.environ["DJANGO_PROJECT_BASE_DIR"]).resolve()
