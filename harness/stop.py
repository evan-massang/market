"""Convenience wrapper: `python -m harness.stop` -> supervisor stop."""
import sys
from harness import supervisor

if __name__ == "__main__":
    sys.exit(supervisor.main(["stop"] + sys.argv[1:]))
