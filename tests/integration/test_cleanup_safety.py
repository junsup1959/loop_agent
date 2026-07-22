from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import build_agent_team_bundle as bundle


ROOT = Path(__file__).resolve().parents[2]


class CleanupSafetyIntegrationTests(unittest.TestCase):
    def test_only_manifest_owned_stale_file_is_selected_and_removed(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="bundle-cleanup-safety-")
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name).resolve() / "bundle"
        destination.mkdir()
        prior = {
            "bundle_id": bundle.BUNDLE_ID,
            "format_version": bundle.BUNDLE_FORMAT_VERSION,
            "generated_paths": ["kept.txt", "obsolete/stale-owned.txt"],
            "inventory": [],
        }
        (destination / bundle.GENERATED_MANIFEST).write_text(
            json.dumps(prior, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        files = {
            "kept.txt": b"managed and current\n",
            "obsolete/stale-owned.txt": b"managed but stale\n",
            "unknown.txt": b"unknown operator file\n",
            "untracked.keep": b"untracked operator file\n",
            ".agent-team/state/agent-team.db": b"runtime database\n",
            ".agent-team/state/agent-team.db-wal": b"runtime wal\n",
            ".agent-team/state/agent-team.db-shm": b"runtime shm\n",
            ".agent-team/artifacts/evidence.json": b"durable evidence\n",
            "custom/non-manifest.txt": b"non-manifest customization\n",
        }
        for relative, content in files.items():
            path = destination.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        desired = {
            "kept.txt": bundle._BundleFile(
                path="kept.txt",
                content=files["kept.txt"],
                mode=0o644,
            )
        }
        before = {
            path.relative_to(destination).as_posix(): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        }
        with mock.patch.object(bundle, "_desired_files", return_value=desired):
            dry_run = bundle.build_bundle(ROOT, destination, check=True)
            after_check = {
                path.relative_to(destination).as_posix(): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after_check)
            self.assertEqual(
                ("obsolete/stale-owned.txt",),
                dry_run.removed_generated,
            )
            preserved = set(dry_run.preserved_unknown)
            self.assertTrue(
                {
                    "unknown.txt",
                    "untracked.keep",
                    ".agent-team/state/agent-team.db",
                    ".agent-team/state/agent-team.db-wal",
                    ".agent-team/state/agent-team.db-shm",
                    ".agent-team/artifacts/evidence.json",
                    "custom/non-manifest.txt",
                }
                <= preserved
            )
            applied = bundle.build_bundle(ROOT, destination, check=False)

        self.assertEqual(dry_run.removed_generated, applied.removed_generated)
        self.assertFalse((destination / "obsolete" / "stale-owned.txt").exists())
        self.assertEqual(files["kept.txt"], (destination / "kept.txt").read_bytes())
        for relative in preserved:
            self.assertEqual(
                files[relative],
                destination.joinpath(*relative.split("/")).read_bytes(),
            )
        self.assertNotEqual(ROOT.resolve(), destination.resolve())
        self.assertFalse(destination.is_relative_to(ROOT.resolve()))


if __name__ == "__main__":
    unittest.main()
