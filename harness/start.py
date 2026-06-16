"""Convenience wrapper: `python -m harness.start` -> supervisor start (background)."""
import sys
from harness import supervisor

if __name__ == "__main__":
    sys.exit(supervisor.main(["start"] + sys.argv[1:]))
