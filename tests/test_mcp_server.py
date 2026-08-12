from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from runtime.mcp_server import _sync_review, _sync_run


class McpInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate from the developer's real ~/.mco/config.json so policy
        # assertions test the code, not whatever the host happens to configure.
        self._config_dir = tempfile.TemporaryDirectory(prefix="mco-test-config-")
        self.addCleanup(self._config_dir.cleanup)
        patcher = patch.dict(os.environ, {"MCO_CONFIG_DIR": self._config_dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_returns_operational_raw_output(self) -> None:
        expected = {
            "stage": "run",
            "task_id": "run-1",
            "status": "complete",
            "outputs": [{"status": "success", "output": "raw answer"}],
            "exit_code": 0,
            "artifact_root": None,
        }
        with tempfile.TemporaryDirectory() as repo, patch("runtime.invocation_runtime.run_invocation_workflow", return_value=expected) as workflow:
            result = _sync_run(repo, "task", "pi")

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], expected)
        self.assertEqual(workflow.call_args.kwargs["hard_timeout_seconds"], 180)

    def test_review_uses_read_only_execution_and_raw_output(self) -> None:
        expected = {
            "stage": "run",
            "task_id": "run-1",
            "status": "complete",
            "outputs": [{"status": "success", "output": "review answer"}],
            "exit_code": 0,
            "artifact_root": None,
        }
        with tempfile.TemporaryDirectory() as repo, patch("runtime.invocation_runtime.run_invocation_workflow", return_value=expected) as workflow:
            result = _sync_review(repo, "review", "pi")

        self.assertTrue(result["ok"])
        self.assertNotIn("findings", result["data"])
        self.assertEqual(workflow.call_args.kwargs["hard_timeout_seconds"], 180)


class McpPolicyTests(unittest.TestCase):
    """MCP calls must honour the same merged config the CLI reads."""

    def setUp(self) -> None:
        self._config_dir = tempfile.TemporaryDirectory(prefix="mco-test-config-")
        self.addCleanup(self._config_dir.cleanup)
        patcher = patch.dict(os.environ, {"MCO_CONFIG_DIR": self._config_dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._workflow_result = {
            "stage": "run", "task_id": "run-9", "status": "complete",
            "outputs": [], "exit_code": 0, "artifact_root": None,
        }

    def _write_global_config(self, policy: dict) -> None:
        path = os.path.join(self._config_dir.name, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"policy": policy}, handle)

    def test_review_applies_configured_policy_and_global_deadline(self) -> None:
        self._write_global_config({
            "timeout_seconds": 600,
            "stall_timeout_seconds": 300,
            "review_hard_timeout_seconds": 1500,
            "max_provider_parallelism": 2,
            "provider_timeouts": {"claude": 420},
        })
        with tempfile.TemporaryDirectory() as repo, patch(
            "runtime.invocation_runtime.run_invocation_workflow", return_value=self._workflow_result,
        ) as workflow:
            _sync_review(repo, "review", "pi")

        kwargs = workflow.call_args.kwargs
        self.assertEqual(kwargs["hard_timeout_seconds"], 600)
        self.assertEqual(kwargs["timeout_seconds"], 300)
        self.assertEqual(kwargs["global_timeout_seconds"], 1500)
        self.assertEqual(kwargs["max_provider_parallelism"], 2)
        self.assertEqual(kwargs["provider_timeouts"], {"claude": 420})

    def test_run_applies_configured_policy_too(self) -> None:
        self._write_global_config({"timeout_seconds": 480})
        with tempfile.TemporaryDirectory() as repo, patch(
            "runtime.invocation_runtime.run_invocation_workflow", return_value=self._workflow_result,
        ) as workflow:
            _sync_run(repo, "task", "pi")

        self.assertEqual(workflow.call_args.kwargs["hard_timeout_seconds"], 480)

    def test_per_call_overrides_win_over_config(self) -> None:
        self._write_global_config({"timeout_seconds": 600, "review_hard_timeout_seconds": 1500})
        with tempfile.TemporaryDirectory() as repo, patch(
            "runtime.invocation_runtime.run_invocation_workflow", return_value=self._workflow_result,
        ) as workflow:
            _sync_review(repo, "review", "pi", ".", "read_only", 90, 240)

        kwargs = workflow.call_args.kwargs
        self.assertEqual(kwargs["hard_timeout_seconds"], 90)
        self.assertEqual(kwargs["global_timeout_seconds"], 240)

    def test_invalid_configured_values_fall_back_to_defaults(self) -> None:
        self._write_global_config({"timeout_seconds": -5, "provider_timeouts": {"claude": 0}})
        with tempfile.TemporaryDirectory() as repo, patch(
            "runtime.invocation_runtime.run_invocation_workflow", return_value=self._workflow_result,
        ) as workflow:
            _sync_review(repo, "review", "pi")

        kwargs = workflow.call_args.kwargs
        self.assertEqual(kwargs["hard_timeout_seconds"], 180)
        self.assertEqual(kwargs["provider_timeouts"], {})

    def test_registered_agent_timeouts_are_merged_like_the_cli(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            with open(os.path.join(repo, ".mcorc.yaml"), "w", encoding="utf-8") as handle:
                handle.write(
                    "policy:\n"
                    "  provider_timeouts:\n"
                    "    claude: 420\n"
                    "agents:\n"
                    "  - name: claude\n"
                    "    command: claude\n"
                    "    timeout: 60\n"
                    "  - name: slowpoke\n"
                    "    command: slowpoke\n"
                    "    timeout: 900\n",
                )
            with patch(
                "runtime.invocation_runtime.run_invocation_workflow",
                return_value=self._workflow_result,
            ) as workflow:
                _sync_review(repo, "review", "pi")

        # An explicit policy entry stays authoritative; an agent-only timeout is adopted.
        self.assertEqual(
            workflow.call_args.kwargs["provider_timeouts"], {"claude": 420, "slowpoke": 900},
        )


if __name__ == "__main__":
    unittest.main()
