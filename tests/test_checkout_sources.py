import json
import subprocess
import unittest
from unittest.mock import patch

from checkout_sources import checkout_sources, parse_sources


class SourceConfigurationTests(unittest.TestCase):
    def test_accepts_only_expected_directories(self):
        raw = json.dumps({"service": "owner/private-library"})
        self.assertEqual(parse_sources(raw, ["service"]), {"service": "owner/private-library"})
        with self.assertRaises(ValueError):
            parse_sources(raw, ["other"])

    def test_rejects_unsafe_directory_or_repository(self):
        for folder, repo in [("../service", "owner/library"), ("service", "https://example.invalid/owner/library"), ("service", "owner/library;echo"), ("service", 123)]:
            with self.subTest(folder=folder, repo=repo), self.assertRaises(ValueError):
                parse_sources(json.dumps({folder: repo}), [folder])

    def test_rejects_empty_or_malformed_configuration(self):
        for raw in ("", "{}", "[]", "null", '{"service":'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, TypeError)):
                parse_sources(raw, ["service"])

    @patch("checkout_sources.subprocess.run")
    def test_clones_dependencies_without_shell_or_output_disclosure(self, run):
        run.return_value = subprocess.CompletedProcess([], 0)
        count = checkout_sources('{"worker_a":"owner/first","worker_b":"owner/second"}', ["worker_a", "worker_b"], "test-only")
        self.assertEqual(count, 2)
        self.assertEqual(run.call_count, 2)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], "worker_b")
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("shell", kwargs)

    @patch("checkout_sources.subprocess.run")
    def test_failure_does_not_reveal_git_stderr(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, stderr=b"private-url-and-token")
        with self.assertRaisesRegex(RuntimeError, "^Dependency checkout failed$"):
            checkout_sources('{"service":"owner/library"}', ["service"], "test-only")

    @patch("checkout_sources.subprocess.run")
    def test_missing_token_never_starts_git(self, run):
        with self.assertRaises(ValueError):
            checkout_sources('{"service":"owner/library"}', ["service"], "")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
