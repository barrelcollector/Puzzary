"""Run the reproducible, isolated portions of Puzzary's core validation suite."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
from bootstrap_official_plugins import install as install_official_plugins

CHECK_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "portable": (
        ("-m", "compileall", "-q", "app", "migrations", "scripts"),
        ("scripts/verify_documentation.py",),
        ("scripts/verify_schema_migrations.py",),
        ("scripts/verify_group_guest_migration.py",),
        ("scripts/verify_release_metadata.py",),
        ("scripts/verify_module_ownership.py",),
        ("scripts/verify_branding.py",),
        ("scripts/verify_responsive_navigation.py",),
        ("scripts/verify_minesweeper.py",),
        ("scripts/verify_nonogram.py",),
        ("scripts/verify_takuzu.py",),
        ("scripts/verify_external_plugins.py",),
    ),
    "security": (
        ("scripts/verify_first_run_admin.py",),
        ("scripts/verify_solution_privacy.py",),
        ("scripts/verify_oidc.py",),
        ("scripts/verify_transport_security.py",),
        ("scripts/verify_pwa.py",),
        ("scripts/verify_offline_puzzles.py",),
        ("scripts/verify_word_search_grid.py",),
    ),
    "plugins": (
        ("scripts/verify_plugin_management.py",),
    ),
    "postgres": (
        ("scripts/verify_permissions.py",),
    ),
}


def run_group(group: str) -> None:
    checks = CHECK_GROUPS[group]
    with tempfile.TemporaryDirectory(prefix=f"puzzary-ci-{group}-") as temporary:
        group_root = Path(temporary)
        plugin_root = group_root / "official-plugins"
        archive_directory = os.getenv("PUZZARY_OFFICIAL_PLUGIN_ARCHIVE_DIR", "").strip()
        install_official_plugins(plugin_root, Path(archive_directory) if archive_directory else None)
        for index, arguments in enumerate(checks, start=1):
            check_root = group_root / f"{index:02d}"
            check_root.mkdir()
            environment = os.environ.copy()
            environment.update({
                "PYTHONPATH": str(ROOT),
                "PUZZARY_QA_ROOT": str(check_root),
                "PUZZARY_PLUGIN_DIR": str(plugin_root),
                "PYTHONPYCACHEPREFIX": str(check_root / "pycache"),
                "TEMP": str(check_root),
                "TMP": str(check_root),
                "TMPDIR": str(check_root),
            })
            scripts_directory = str(Path(sys.executable).resolve().parent)
            environment["PATH"] = scripts_directory + os.pathsep + environment.get("PATH", "")
            if group == "postgres":
                environment.setdefault(
                    "PERMISSIONS_QA_DATABASE",
                    f"sqlite:///{(check_root / 'permissions.sqlite').as_posix()}",
                )
            label = " ".join(arguments)
            print(f"\n==> [{group}] {label}", flush=True)
            subprocess.run(
                [sys.executable, *arguments],
                cwd=ROOT,
                env=environment,
                check=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=sorted(CHECK_GROUPS))
    args = parser.parse_args()
    run_group(args.group)


if __name__ == "__main__":
    main()
