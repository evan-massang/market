"""Convenience wrapper: `python -m harness.status` -> supervisor status."""
import sys
from harness import supervisor

if __name__ == "__main__":
    sys.exit(supervisor.main(["status"] + sys.argv[1:]))
