"""rs3: the rs3cache tools from a terminal. `rs3` on its own says where to start."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rs3mcp import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli.main())
