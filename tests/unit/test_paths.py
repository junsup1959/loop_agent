from __future__ import annotations

import tempfile
import unittest
from pathlib import Path, PureWindowsPath

from scripts.agent_team_layout import AgentTeamLayout
from scripts.agent_team_paths import AxPathAuthority, AxPathError


class AxPathAuthorityTests(unittest.TestCase):
    def test_reuses_agent_team_layout_and_derives_bounded_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-paths-") as temporary:
            root = Path(temporary)
            source = root / "source"
            runtime = root / "runtime"
            (source / "agents").mkdir(parents=True)
            (source / "skills").mkdir(parents=True)
            (source / "agents" / "team.toml").write_text(
                "[team]\n", encoding="utf-8"
            )
            (source / "skills" / "catalog.toml").write_text(
                "[catalog]\n", encoding="utf-8"
            )
            layout = AgentTeamLayout.discover(source / "agents")

            authority = AxPathAuthority.from_layout(layout, runtime)

            self.assertEqual(source.resolve(), authority.ax_source_root)
            self.assertEqual(
                (runtime / "repositories" / "target-1.git").resolve(),
                authority.managed_repository("target-1"),
            )
            self.assertEqual(
                (
                    runtime
                    / "workspaces"
                    / "goal-1"
                    / "legacy"
                    / "work-1-r2"
                ).resolve(),
                authority.development_worktree("goal-1", "work-1", 2),
            )
            self.assertEqual(
                (runtime / "activations" / "activation-1" / "temp").resolve(),
                authority.scratch_root("activation-1"),
            )
            self.assertEqual(
                (runtime / "state" / "agent-team.db").resolve(),
                authority.state_database,
            )
            with self.assertRaises(ValueError):
                authority.target_runtime("../escape")

    def test_rejects_source_runtime_overlap_and_target_overlap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-overlap-") as temporary:
            root = Path(temporary)
            source = root / "source"
            runtime = root / "runtime"
            target = root / "target"
            for path in (source, runtime, target):
                path.mkdir()

            with self.assertRaises(AxPathError):
                AxPathAuthority(source, source / "runtime")

            authority = AxPathAuthority(source, target / "runtime")
            with self.assertRaises(AxPathError):
                authority.assert_runtime_outside_target(target)

            source_inside_target = AxPathAuthority(target / "source", runtime)
            with self.assertRaises(AxPathError):
                source_inside_target.assert_runtime_outside_target(target)

    def test_common_git_directory_produces_stable_target_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-identity-") as temporary:
            root = Path(temporary)
            source = root / "source"
            runtime = root / "runtime"
            main = root / "main"
            linked = root / "linked"
            common = main / ".git"
            worktree_git = common / "worktrees" / "linked"
            for path in (source, runtime, main, linked, common, worktree_git):
                path.mkdir(parents=True, exist_ok=True)
            (linked / ".git").write_text(
                f"gitdir: {worktree_git}\n", encoding="utf-8"
            )
            (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")

            authority = AxPathAuthority(source, runtime)

            self.assertEqual(
                common.resolve(), authority.canonical_git_common_dir(main)
            )
            self.assertEqual(
                common.resolve(), authority.canonical_git_common_dir(linked)
            )
            self.assertEqual(
                authority.canonical_target_identity(main),
                authority.canonical_target_identity(linked),
            )

    def test_windows_wsl_translation_is_deterministic_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ax-wsl-") as temporary:
            root = Path(temporary)
            authority = AxPathAuthority(root / "source", root / "runtime")

            translated = authority.to_wsl_path(
                PureWindowsPath(r"C:\Project Folder\repo")
            )
            self.assertEqual("/mnt/c/Project Folder/repo", translated)
            self.assertEqual(
                PureWindowsPath(r"C:\Project Folder\repo"),
                authority.from_wsl_path(translated),
            )

            with self.assertRaises(AxPathError):
                authority.to_wsl_path(PureWindowsPath(r"\\server\share\repo"))
            with self.assertRaises(AxPathError):
                authority.to_wsl_path(PureWindowsPath("relative/repo"))
            with self.assertRaises(AxPathError):
                authority.from_wsl_path("/home/user/repo")


if __name__ == "__main__":
    unittest.main()
