"""`python -m tokenscope.live [options]` -> run the live server."""
import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
