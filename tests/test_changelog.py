"""Regression tests for merge-request-scoped version derivation."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "maint-scripts" / "changelog.py"
)
BASE_PYPROJECT = (
    '[project]\n'
    'name = "buzz"\n'
    'version = "2.3.0"\n'
    'description = "test"\n'
    'requires-python = ">=3.14"\n'
)
MR_ENV = {
    "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feature",
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
}


def load_script_module():
    """Load maint-scripts/changelog.py as a module."""
    spec = importlib.util.spec_from_file_location("changelog", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load changelog.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ChangelogScriptTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script_module()

    def _git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def _main(self, args: list[str]) -> int:
        """Run the CLI without inheriting GitLab's merge-request context."""
        with patch.dict(
            os.environ,
            {
                "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "",
                "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "",
            },
        ):
            return self.module.main(args)

    def _build_repo(
        self,
        root: Path,
        target_subject: str,
        source_subjects: tuple[str, ...] = (
            "feat: per-ingest category override",
            "fix: validate config override saves",
        ),
    ):
        """Create a repository with a main baseline and a feature branch.

        main gets a baseline ``pyproject.toml`` (2.3.0) plus one target-only
        commit; the feature branch adds the given non-merge commits.
        """
        root.mkdir()
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.name", "Test")
        self._git(root, "config", "user.email", "test@example.com")
        (root / "pyproject.toml").write_text(BASE_PYPROJECT, encoding="utf-8")
        self._git(root, "add", "pyproject.toml")
        self._git(root, "commit", "-m", "chore: initialize project")
        self._git(root, "commit", "-m", target_subject, "--allow-empty")
        self._git(root, "checkout", "-b", "feature")
        for subject in source_subjects:
            self._git(root, "commit", "-m", subject, "--allow-empty")

    def _derive(self, repo: Path, source: str = "HEAD") -> tuple[str, str]:
        """Derive (version, baseline_version) for main..source."""
        derivation = self.module.derive(repo, "main", source)
        return derivation.version, derivation.baseline_version

    def test_feature_plus_fix_bumps_minor_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(repo, "feat: target only commit")

            version, baseline_version = self._derive(repo)

            self.assertEqual(baseline_version, "2.3.0")
            self.assertEqual(version, "2.4.0")

    def test_fix_only_bumps_patch_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(
                repo,
                "chore: target only commit",
                source_subjects=(
                    "fix: first patch",
                    "chore: routine change",
                    "fix: second patch",
                ),
            )

            version, baseline_version = self._derive(repo)

            self.assertEqual(baseline_version, "2.3.0")
            self.assertEqual(version, "2.3.1")

    def test_no_conventional_subject_keeps_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(
                repo,
                "chore: target only commit",
                source_subjects=("docs: refresh docs",),
            )

            version, baseline_version = self._derive(repo)

            self.assertEqual(baseline_version, "2.3.0")
            self.assertEqual(version, "2.3.0")

    def test_changelog_keeps_every_subject(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(
                repo,
                "chore: target only commit",
                source_subjects=(
                    "feat: per-ingest category override",
                    "docs: refresh docs",
                    "fix: validate config override saves",
                ),
            )

            derivation = self.module.derive(repo, "main", "HEAD")

            self.assertEqual(derivation.version, "2.4.0")
            self.assertEqual(
                [entry.subject for entry in derivation.entries],
                [
                    "feat: per-ingest category override",
                    "docs: refresh docs",
                    "fix: validate config override saves",
                ],
            )

    def test_local_invocation_defaults_to_main_head(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(repo, "chore: target only commit")

            revisions = self.module.resolve_revisions({}, "--release-a")

            self.assertEqual(revisions.target, "--release-a")
            self.assertEqual(revisions.source, "HEAD")

    def test_ci_variables_select_origin_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(repo, "chore: target only commit")

            revisions = self.module.resolve_revisions(MR_ENV, None)

            self.assertEqual(revisions.target, "origin/main")
            self.assertEqual(revisions.source, "origin/feature")

    def test_ci_target_branch_override_keeps_ci_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(repo, "chore: target only commit")

            revisions = self.module.resolve_revisions(MR_ENV, "release-a")

            self.assertEqual(revisions.target, "origin/release-a")
            self.assertEqual(revisions.source, "origin/feature")

    def test_check_rejects_stale_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(repo, "chore: target only commit")
            self._git(
                repo,
                "remote",
                "add",
                "origin",
                "git@gitlab.com:example/proj.git",
            )

            # Missing metadata is stale.
            self.assertEqual(
                self._main(
                    ["--repo", str(repo), "--target-branch", "main", "--check"]
                ),
                1,
            )

            derivation = self.module.derive(repo, "main", "HEAD")
            version = derivation.version
            baseline_version = derivation.baseline_version
            short_sha = derivation.baseline_sha
            baseline_date = derivation.baseline_date
            self.assertEqual(len(short_sha), 7)
            entries = derivation.entries
            baseline_url = derivation.baseline_url
            self.assertEqual(version, "2.4.0")
            (repo / "pyproject.toml").write_text(
                BASE_PYPROJECT.replace('"2.3.0"', f'"{version}"'),
                encoding="utf-8",
            )
            (repo / "CHANGELOG.md").write_text(
                self.module.format_changelog(
                    version,
                    baseline_version,
                    short_sha,
                    baseline_date,
                    entries,
                    baseline_url,
                ),
                encoding="utf-8",
            )

            # Matching metadata passes the check.
            self.assertEqual(
                self._main(
                    ["--repo", str(repo), "--target-branch", "main", "--check"]
                ),
                0,
            )

            # Simulate the authenticated HTTPS origin used by GitLab CI.
            self._git(
                repo,
                "remote",
                "set-url",
                "origin",
                "https://gitlab-ci-token:fixture-token"
                "@gitlab.com/example/proj.git",
            )
            self.assertEqual(
                self.module.baseline_permalink(repo, short_sha),
                "https://gitlab.com/example/proj/-/blob/"
                f"{short_sha}/CHANGELOG.md",
            )
            self.assertEqual(
                self._main(
                    ["--repo", str(repo), "--target-branch", "main", "--check"]
                ),
                0,
            )

            # A later content mismatch reports sanitized derivation details.
            stale = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
            (repo / "CHANGELOG.md").write_text(
                stale + "extra\n", encoding="utf-8"
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = self._main(
                    [
                        "--repo",
                        str(repo),
                        "--target-branch",
                        "main",
                        "--check",
                    ]
                )
            self.assertEqual(result, 1)
            diagnostic = stderr.getvalue()
            self.assertIn("target=main@", diagnostic)
            self.assertIn("source=HEAD@", diagnostic)
            self.assertIn("derived version 2.4.0", diagnostic)
            self.assertIn(
                "origin remote: https://gitlab.com/example/proj.git",
                diagnostic,
            )
            self.assertIn(
                "baseline link https://gitlab.com/example/proj/-/blob/",
                diagnostic,
            )
            self.assertIn("found 'extra'", diagnostic)
            self.assertNotIn("fixture-token", diagnostic)

    def test_missing_target_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(repo, "chore: target only commit")

            self.assertEqual(
                self._main(
                    [
                        "--repo",
                        str(repo),
                        "--target-branch",
                        "nope",
                        "--check",
                    ]
                ),
                1,
            )

    def test_permalink_only_for_gitlab_remotes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(repo, "chore: target only commit")

            # No origin remote: no permalink.
            self.assertIsNone(self.module.baseline_permalink(repo, "abc"))

            self._git(repo, "remote", "add", "origin",
                      "git@gitlab.com:example/proj.git")
            self.assertEqual(
                self.module.baseline_permalink(repo, "abc"),
                "https://gitlab.com/example/proj/-/blob/abc/CHANGELOG.md",
            )

            self._git(
                repo,
                "remote",
                "set-url",
                "origin",
                "https://gitlab-ci-token:fixture-token"
                "@gitlab.com/example/proj.git",
            )
            summary = self.module.origin_remote_summary(repo)

            self.assertEqual(
                summary, "https://gitlab.com/example/proj.git"
            )
            self.assertNotIn("fixture-token", summary)
            self.assertEqual(
                self.module.baseline_permalink(repo, "abc"),
                "https://gitlab.com/example/proj/-/blob/abc/CHANGELOG.md",
            )

            # A non-GitLab origin yields no permalink.
            self._git(repo, "remote", "set-url", "origin",
                      "git@github.com:example/proj.git")
            self.assertIsNone(self.module.baseline_permalink(repo, "abc"))

    def test_write_mode_updates_pyproject_and_changelog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            self._build_repo(repo, "chore: target only commit")

            self.assertEqual(
                self._main(
                    ["--repo", str(repo), "--target-branch", "main"]
                ),
                0,
            )

            pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
            self.assertIn('version = "2.4.0"', pyproject)
            changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
            self.assertIn("## 2.4.0", changelog)
            self.assertIn("Previous baseline: 2.3.0", changelog)


class ChangelogCliEnvironmentTests(unittest.TestCase):
    """CLI end-to-end selection of source and target revisions."""

    def setUp(self):
        self.module = load_script_module()

    def _git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def _build_ci_repo(self, root: Path) -> Path:
        """Build a repo with origin/main and origin/feature remote refs.

        main pins version 2.3.0; feature adds one ``feat:`` and one
        ``fix:`` on top of it.
        """
        root.mkdir()
        remote = root / "remote.git"
        self._git(root, "init", "--bare", str(remote))
        repo = root / "work"
        repo.mkdir()
        self._git(repo, "init", "-b", "main")
        self._git(repo, "remote", "add", "origin", str(remote))
        self._git(repo, "config", "user.name", "Test")
        self._git(repo, "config", "user.email", "test@example.com")
        (repo / "pyproject.toml").write_text(BASE_PYPROJECT, encoding="utf-8")
        self._git(repo, "add", "pyproject.toml")
        self._git(repo, "commit", "-m", "chore: initialize project")
        self._git(repo, "push", "origin", "main")
        self._git(repo, "checkout", "-b", "feature")
        self._git(
            repo,
            "commit",
            "-m",
            "feat: per-ingest category override",
            "--allow-empty",
        )
        self._git(
            repo,
            "commit",
            "-m",
            "fix: validate config override saves",
            "--allow-empty",
        )
        self._git(repo, "push", "origin", "feature")
        return repo

    def _cli_env(self, extra: dict[str, str]) -> dict[str, str]:
        env = dict(os.environ)
        if not extra:
            env.pop("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", None)
            env.pop("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", None)
        env.update(extra)
        return env

    def test_local_cli_prints_squashed_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._build_ci_repo(Path(tmpdir) / "repo")
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--repo", str(repo)],
                capture_output=True,
                text=True,
                check=True,
                env=self._cli_env({}),
            )
            self.assertEqual(result.stdout.strip(), "2.4.0")

    def test_ci_cli_uses_gitlab_branch_variables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._build_ci_repo(Path(tmpdir) / "repo")
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--repo", str(repo)],
                capture_output=True,
                text=True,
                check=True,
                env=self._cli_env(MR_ENV),
            )
            self.assertEqual(result.stdout.strip(), "2.4.0")


if __name__ == "__main__":
    unittest.main()
