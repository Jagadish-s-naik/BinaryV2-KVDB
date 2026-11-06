"""CLI entrypoint for running torture_tool via `python -m torture_tool`."""

import sys
from torture_tool.runner import main

if __name__ == "__main__":
    sys.exit(main())

