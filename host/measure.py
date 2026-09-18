"""Run the M0 benchmark from the demo directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mnd.measure import main  # noqa: E402

if __name__ == "__main__":
    main()
