"""Output-root guards only: no compilation, execution or protocol writes."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments_v2 import initial_lookahead_batch as batch
from experiments_v2 import initial_lookahead_extension as extension
from experiments_v2 import initial_lookahead_runner as runner


class InitialLookaheadPathTests(unittest.TestCase):
    def allowed(self, repo):
        return (
            repo / "IEEE_conference_template/build/initial-lookahead/path-test",
            repo / "ZAC_zzx/results/initial_lookahead_v1/path-test",
        )

    def disallowed(self, repo):
        return (
            repo / "build/initial-lookahead/path-test",
            repo / "IEEE_conference_template/build/initial-lookahead-other/path-test",
            repo / "IEEE_conference_template/build/initial-lookahead/../path-test",
        )

    def test_runner_accepts_relocated_and_result_roots_before_horizon_guard(self):
        for root in self.allowed(runner.REPO):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "paired horizons"):
                runner.run(SimpleNamespace(root=root, horizons=[0, 0]))

    def test_runner_rejects_old_root_and_escape_before_any_execution(self):
        for root in self.disallowed(runner.REPO):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "output must be beneath"):
                runner.run(SimpleNamespace(root=root, horizons=[0, 0]))

    def check_cli_allowed(self, module):
        for root in self.allowed(module.REPO):
            with self.subTest(root=root), patch.object(
                    module, "protocol_payload", side_effect=RuntimeError("guard reached")) as payload:
                with self.assertRaisesRegex(RuntimeError, "guard reached"):
                    module.main(["--root", str(root), "--seal-only"])
                payload.assert_called_once()

    def check_cli_disallowed(self, module):
        for root in self.disallowed(module.REPO):
            with self.subTest(root=root), patch.object(module, "protocol_payload") as payload:
                with self.assertRaisesRegex(ValueError, "allowed result"):
                    module.main(["--root", str(root), "--seal-only"])
                payload.assert_not_called()

    def test_batch_accepts_relocated_and_result_roots(self):
        self.check_cli_allowed(batch)

    def test_batch_rejects_old_root_and_escape(self):
        self.check_cli_disallowed(batch)

    def test_extension_accepts_relocated_and_result_roots(self):
        self.check_cli_allowed(extension)

    def test_extension_rejects_old_root_and_escape(self):
        self.check_cli_disallowed(extension)


if __name__ == "__main__":
    unittest.main()
