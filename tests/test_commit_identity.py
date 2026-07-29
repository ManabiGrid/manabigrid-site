from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

import check_commit_identity


class CommitIdentityTests(unittest.TestCase):
    def create_commit(
        self,
        root: Path,
        *,
        author: str,
        committer: str,
    ) -> str:
        if not (root / ".git").is_dir():
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
                capture_output=True,
                text=True,
            )
        fixture = root / "fixture.txt"
        previous = (
            fixture.read_text(encoding="utf-8")
            if fixture.exists()
            else ""
        )
        fixture.write_text(previous + "fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(root), "add", "fixture.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        environment = {
            "GIT_AUTHOR_NAME": "Fixture Author",
            "GIT_AUTHOR_EMAIL": author,
            "GIT_COMMITTER_NAME": "Fixture Committer",
            "GIT_COMMITTER_EMAIL": committer,
        }
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "fixture"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def run_checker(
        self,
        *,
        author: str,
        committer: str,
    ) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commit = self.create_commit(
                root,
                author=author,
                committer=committer,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                result = check_commit_identity.main(
                    ["--repo", str(root), "--commit", commit]
                )
            return result, output.getvalue()

    def test_user_noreply_author_and_committer_pass(self) -> None:
        result, output = self.run_checker(
            author="12345+fixture-user@users.noreply.github.com",
            committer="12345+fixture-user@users.noreply.github.com",
        )
        self.assertEqual(result, 0, output)
        self.assertIn("PASS", output)

    def test_github_server_committer_passes(self) -> None:
        result, output = self.run_checker(
            author="fixture-user@users.noreply.github.com",
            committer="noreply@github.com",
        )
        self.assertEqual(result, 0, output)
        self.assertIn("PASS", output)

    def test_non_noreply_author_is_rejected_without_disclosure(self) -> None:
        private = "private-author@example.test"
        result, output = self.run_checker(
            author=private,
            committer="12345+fixture-user@users.noreply.github.com",
        )
        self.assertNotEqual(result, 0)
        self.assertIn("author email is not", output)
        self.assertNotIn(private, output)

    def test_non_noreply_committer_is_rejected_without_disclosure(self) -> None:
        private = "private-committer@example.test"
        result, output = self.run_checker(
            author="12345+fixture-user@users.noreply.github.com",
            committer=private,
        )
        self.assertNotEqual(result, 0)
        self.assertIn("committer email is not", output)
        self.assertNotIn(private, output)

    def test_malformed_sha_is_rejected_without_running_git(self) -> None:
        with self.assertRaises(check_commit_identity.IdentityCheckError):
            check_commit_identity.read_commit_identity(Path("."), "not-a-sha")

    def test_range_excludes_fixed_historical_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.create_commit(
                root,
                author="historical@example.test",
                committer="historical@example.test",
            )
            target = self.create_commit(
                root,
                author="12345+fixture-user@users.noreply.github.com",
                committer="noreply@github.com",
            )
            self.assertEqual(
                check_commit_identity.validate_commit_range(
                    root,
                    baseline,
                    target,
                ),
                1,
            )

    def test_range_rejects_non_noreply_intermediate_commit(self) -> None:
        private = "private-intermediate@example.test"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.create_commit(
                root,
                author="historical@example.test",
                committer="historical@example.test",
            )
            self.create_commit(
                root,
                author=private,
                committer="12345+fixture-user@users.noreply.github.com",
            )
            target = self.create_commit(
                root,
                author="12345+fixture-user@users.noreply.github.com",
                committer="noreply@github.com",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                result = check_commit_identity.main(
                    [
                        "--repo",
                        str(root),
                        "--since",
                        baseline,
                        "--commit",
                        target,
                    ]
                )
            self.assertNotEqual(result, 0)
            self.assertNotIn(private, output.getvalue())

    def test_range_rejects_empty_commit_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.create_commit(
                root,
                author="historical@example.test",
                committer="historical@example.test",
            )
            with self.assertRaises(check_commit_identity.IdentityCheckError):
                check_commit_identity.validate_commit_range(
                    root,
                    baseline,
                    baseline,
                )

    def test_range_rejects_unrelated_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            target = self.create_commit(
                Path(first),
                author="12345+fixture-user@users.noreply.github.com",
                committer="noreply@github.com",
            )
            unrelated = self.create_commit(
                Path(second),
                author="12345+fixture-user@users.noreply.github.com",
                committer="noreply@github.com",
            )
            with self.assertRaises(check_commit_identity.IdentityCheckError):
                check_commit_identity.validate_commit_range(
                    Path(first),
                    unrelated,
                    target,
                )


if __name__ == "__main__":
    unittest.main()
