"""Tests for Report Mode text/JSON output, Realtime TUI rendering/keys, and CLI dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from rich.console import Console

from agystats.cli import main as cli_main
from agystats.engine import ALL_CONVERSATIONS_ID, MetricsEngine
from agystats.formatters import build_braille_scatter
from agystats.report import render_conversation_list, render_report
from agystats.tui import TUIState, apply_tui_key, build_tui_renderable, run_tui


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _seed_workspace(root: Path) -> tuple[Path, Path, str, str]:
    brain = root / "brain"
    state = root / "agystats"
    cid = "aaaaaaaa-1111-2222-3333-444444444444"
    sub_id = "bbbbbbbb-1111-2222-3333-444444444444"

    t0 = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    iso = lambda ep: datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tr = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": iso(t0),
            "content": "<USER_REQUEST>\nBuild TUI dashboard\n</USER_REQUEST>",
        }
    ]
    hk = []
    cur = t0 + 1.0
    idx = 1
    for out_tok in (120, 420, 900, 1600):
        dur_s = 1.2 + out_tok / 120.0
        tr.append(
            {
                "step_index": idx,
                "type": "PLANNER_RESPONSE",
                "created_at": iso(cur),
                "model": "gemini-pro-orchestrator-argon",
                "input_tokens": 1800,
                "cache_read_tokens": 8000,
                "output_tokens": out_tok,
                "thinking": "Reasoning...",
                "content": "Working on it.",
                "tool_calls": (
                    [{"name": "invoke_subagent", "args": {"Role": '"Researcher"', "Model": '"flash"'}}]
                    if idx == 4
                    else []
                ),
            }
        )
        hk.append({"ts_ms": int(cur * 1000), "event": "PreInvocation", "conversation_id": cid})
        hk.append(
            {
                "ts_ms": int((cur + dur_s) * 1000),
                "event": "PostInvocation",
                "conversation_id": cid,
            }
        )
        idx += 1
        if idx == 5:
            tr.append(
                {
                    "step_index": idx,
                    "type": "GENERIC",
                    "created_at": iso(cur + dur_s + 0.2),
                    "content": f"Created At: {iso(cur + dur_s)}\nCompleted At: {iso(cur + dur_s + 1.0)}\nSpawned {sub_id}",
                }
            )
            idx += 1
        cur += dur_s + 2.0

    _write_jsonl(brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl", tr)
    _write_jsonl(state / "conversations" / f"{cid}.hooks.jsonl", hk)
    _write_jsonl(
        brain / sub_id / ".system_generated" / "logs" / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": iso(cur),
                "content": "Check sub-task",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": iso(cur + 1.0),
                "model": "gemini-flash-worker-agy",
                "input_tokens": 1000,
                "cache_read_tokens": 2000,
                "output_tokens": 150,
                "content": "Done.",
                "tool_calls": [],
            },
        ],
    )
    return brain, state, cid, sub_id


class TestReportAndTui(unittest.TestCase):
    def test_report_modes_no_color_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain, state, cid, _ = _seed_workspace(Path(tmp))
            eng = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                convs = eng.list_conversations()
                list_txt = render_conversation_list(convs, no_color=True)
                self.assertIn("Build TUI dashboard", list_txt)
                self.assertIn(cid[:8], list_txt)

                conv_txt = render_report(
                    eng,
                    conversation_id=cid,
                    model_filter="argon",
                    no_color=True,
                )
                self.assertIn("AGYSTATS", conv_txt)
                self.assertIn("Active Context Window", conv_txt)
                self.assertIn("Per-Model Token, Timing & Roofline", conv_txt)
                self.assertIn("Output vs. Time Roofline", conv_txt)
                self.assertIn("Paygo & AI Credits", conv_txt)
                self.assertIn("Subagent Rollup", conv_txt)
                self.assertIn("Per-Step Telemetry", conv_txt)
                self.assertNotIn("\x1b[", conv_txt)

                all_txt = render_report(
                    eng,
                    conversation_id=ALL_CONVERSATIONS_ID,
                    window="all",
                    no_color=True,
                )
                self.assertIn("All Conversations (all)", all_txt)
                self.assertNotIn("Active Context Window", all_txt)

                json_txt = render_report(eng, conversation_id=cid, as_json=True)
                parsed = json.loads(json_txt)
                self.assertEqual(parsed["metrics"]["conversation_id"], cid)
                self.assertEqual(parsed["metrics"]["subagents"]["count"], 1)
            finally:
                eng.close()

    def test_tui_renderable_keybindings_and_run_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain, state, cid, sub_id = _seed_workspace(Path(tmp))
            eng = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                snap = eng.get_snapshot(cid)
                tui_state = TUIState()

                # Render frame to string buffer
                out_buf = io.StringIO()
                con = Console(file=out_buf, width=110, height=40, no_color=True, color_system=None)
                con.print(build_tui_renderable(snap, tui_state, width=110, height=40))
                frame1 = out_buf.getvalue()
                self.assertIn("AGYSTATS LIVE", frame1)
                self.assertIn("Build TUI dashboard", frame1)

                # Test keyboard navigation
                self.assertTrue(apply_tui_key("a", tui_state, snap))
                self.assertEqual(tui_state.view_mode, "all")
                self.assertTrue(apply_tui_key("w", tui_state, snap))
                self.assertEqual(tui_state.time_window, "5h")
                self.assertTrue(apply_tui_key("g", tui_state, snap))
                self.assertEqual(tui_state.val_pool, "gemini")
                self.assertTrue(apply_tui_key("m", tui_state, snap))
                self.assertIsNotNone(tui_state.model_filter)
                self.assertTrue(apply_tui_key("]", tui_state, snap))
                self.assertEqual(tui_state.view_mode, "conversation")
                self.assertFalse(tui_state.follow_active)
                self.assertIn(tui_state.selected_conv_id, (cid, sub_id))
                self.assertTrue(apply_tui_key("f", tui_state, snap))
                self.assertTrue(tui_state.follow_active)
                self.assertTrue(apply_tui_key("s", tui_state, snap))
                self.assertTrue(tui_state.show_all_steps)
                self.assertFalse(apply_tui_key("q", tui_state, snap))

                # Test scatter helper directly
                scatter_plot = build_braille_scatter(
                    snap["metrics"]["steps"],
                    snap["metrics"]["by_model"][0]["latency_fit"],
                    width=40,
                    height=8,
                ).plain
                self.assertIn("0s", scatter_plot)

                # Test deterministic run_tui execution with max_ticks
                tui_buf = io.StringIO()
                tui_con = Console(file=tui_buf, width=100, height=36, no_color=True, color_system=None)
                rc = run_tui(eng, TUIState(), max_ticks=2, console=tui_con)
                self.assertEqual(rc, 0)
                self.assertIn("AGYSTATS LIVE", tui_buf.getvalue())
            finally:
                eng.close()

    def test_cli_dispatch_and_conversation_prefix_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain, state, cid, _ = _seed_workspace(Path(tmp))
            stdout_buf = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = stdout_buf
                rc = cli_main(
                    [
                        "report",
                        "--brain-dir",
                        str(brain),
                        "--state-dir",
                        str(state),
                        "-c",
                        cid[:8],
                        "--no-color",
                    ]
                )
            finally:
                sys.stdout = old_stdout
            self.assertEqual(rc, 0)
            self.assertIn(cid[:8], stdout_buf.getvalue())

            # Unknown conversation exits cleanly with code 2
            err_buf = io.StringIO()
            old_stderr = sys.stderr
            try:
                sys.stderr = err_buf
                rc_err = cli_main(
                    [
                        "report",
                        "--brain-dir",
                        str(brain),
                        "--state-dir",
                        str(state),
                        "-c",
                        "does-not-exist",
                    ]
                )
            finally:
                sys.stderr = old_stderr
            self.assertEqual(rc_err, 2)

            # Verify standalone agystats.py runs cleanly via subprocess
            repo_dir = Path(__file__).resolve().parents[1]
            proc = subprocess.run(
                [
                    sys.executable,
                    str(repo_dir / "agystats.py"),
                    "list",
                    "--brain-dir",
                    str(brain),
                    "--state-dir",
                    str(state),
                    "--json",
                ],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                check=True,
            )
            listed = json.loads(proc.stdout)
            self.assertEqual(listed["active_conversation_id"], cid)


if __name__ == "__main__":
    unittest.main()
