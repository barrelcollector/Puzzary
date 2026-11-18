"""Install a trusted, versioned Puzzary plugin release archive."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.plugin_installer import install_plugin_release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Pinned release ZIP URL")
    parser.add_argument("sha256", help="Published SHA-256 digest")
    parser.add_argument("--plugin-dir", default=os.getenv("PUZZARY_PLUGIN_DIR", "/data/plugins"))
    arguments = parser.parse_args()
    installed, _manifest = install_plugin_release(
        arguments.url,
        arguments.sha256,
        Path(arguments.plugin_dir).resolve(),
    )
    print(f"Installed {installed}. Restart Puzzary to activate it.")


if __name__ == "__main__":
    main()
