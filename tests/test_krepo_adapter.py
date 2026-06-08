from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations.krepo_adapter import KRepoAdapter
from integrations.krepo_config import resolve_krepo_config


class KRepoAdapterTest(unittest.TestCase):
    def test_default_provider_uses_builtin_without_external_root(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            config = resolve_krepo_config()

        self.assertEqual(config.provider, "builtin")
        self.assertTrue(str(config.query_script).endswith("knowledge_base/src/cpp_meta_query.py"))

    def test_environment_krepo_root_selects_external_provider_when_usable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "src" / "cpp_meta_query.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('ok')\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"KREPO_ROOT": str(root)}, clear=True):
                config = resolve_krepo_config()

        self.assertEqual(config.provider, "external")
        self.assertEqual(config.query_script, script.resolve())
        self.assertTrue(config.fallback_to_builtin)

    def test_invalid_explicit_krepo_root_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "query script"):
                resolve_krepo_config(tmp)

    def test_automatic_external_provider_falls_back_to_builtin_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "src" / "cpp_meta_query.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('external')\n", encoding="utf-8")
            out = root / "artifacts"

            with mock.patch.dict(os.environ, {"KREPO_ROOT": str(root)}, clear=True):
                adapter = KRepoAdapter()

            calls = {"count": 0}

            def fake_run(command, **_kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    return subprocess.CompletedProcess(command, 2, "", "Function not found")
                return subprocess.CompletedProcess(command, 0, "params\n", "")

            with mock.patch("integrations.krepo_adapter.subprocess.run", side_effect=fake_run):
                result = adapter.params(
                    function="can_send",
                    repo="/linux-7.0",
                    file="net/can/af_can.c",
                    artifact_dir=out,
                )

            self.assertTrue(result.ok)
            self.assertEqual(result.provider, "builtin")
            self.assertEqual((out / "krepo" / "params.txt").read_text(encoding="utf-8"), "params\n")

    def test_explicit_external_provider_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "src" / "cpp_meta_query.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('external')\n", encoding="utf-8")
            adapter = KRepoAdapter(resolve_krepo_config(root, provider="external"))

            with mock.patch(
                "integrations.krepo_adapter.subprocess.run",
                return_value=subprocess.CompletedProcess(["cmd"], 2, "", "Function not found"),
            ):
                result = adapter.params(
                    function="can_send",
                    repo="/linux-7.0",
                    file="net/can/af_can.c",
                    artifact_dir=Path(tmp) / "artifacts",
                )

        self.assertFalse(result.ok)
        self.assertEqual(result.provider, "external")

    def test_report_writes_json_and_markdown_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "artifacts"
            config = resolve_krepo_config(provider="builtin")
            adapter = KRepoAdapter(config)

            def fake_run(command, **_kwargs):
                text = '{"function":"can_send"}\n' if "--format" in command else "# report\n"
                return subprocess.CompletedProcess(command, 0, text, "")

            with mock.patch("integrations.krepo_adapter.subprocess.run", side_effect=fake_run) as run:
                result = adapter.report(
                    function="can_send",
                    repo="/linux-7.0",
                    file="net/can/af_can.c",
                    artifact_dir=out,
                )

            self.assertTrue(result.ok)
            self.assertEqual(result.provider, "builtin")
            self.assertEqual((out / "krepo" / "report.json").read_text(encoding="utf-8"), '{"function":"can_send"}\n')
            report_md = (out / "krepo" / "report.md").read_text(encoding="utf-8")
            self.assertIn("# kRepo Report: can_send", report_md)
            commands = [" ".join(call.args[0]) for call in run.call_args_list]
            self.assertEqual(len(commands), 1)
            self.assertIn("report can_send --repo /linux-7.0", commands[0])
            self.assertIn("--format json", commands[0])

    def test_subsource_declares_axf_artifact_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "artifacts"
            adapter = KRepoAdapter(resolve_krepo_config(provider="builtin"))

            with mock.patch(
                "integrations.krepo_adapter.subprocess.run",
                return_value=subprocess.CompletedProcess(["cmd"], 0, "wrote\n", ""),
            ) as run:
                result = adapter.subsource(
                    function="can_send",
                    repo="/linux-7.0",
                    file="net/can/af_can.c",
                    artifact_dir=out,
                    max_depth=1,
                    max_functions=30,
                )

        self.assertEqual(result.outputs()["subsource_c"], str(out / "krepo" / "subsource.c"))
        command = " ".join(run.call_args.args[0])
        self.assertIn("subsource can_send", command)
        self.assertIn("--output", command)
        self.assertIn("--max-depth 1", command)
        self.assertIn("--max-functions 30", command)


if __name__ == "__main__":
    unittest.main()
