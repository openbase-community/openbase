"""Entry point for running openbase as a module: python -m openbase"""

from __future__ import annotations

from openbase.core.cli.cli import main

if __name__ == "__main__":
    # Click commands parse sys.argv automatically when called directly
    # The type checker doesn't understand click's runtime behavior
    main()  # type: ignore
