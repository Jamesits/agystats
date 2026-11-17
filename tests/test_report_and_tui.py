"""Tests for Report Mode text/JSON output, Realtime TUI rendering/keys, and CLI dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from rich.console import Console

from agystats.cli import main as cli_main
from agystats.engine import ALL_CONVERSATIONS_ID, MetricsEngine, PROJECT_SCOPE_PREFIX
from agystats.formatters import build_braille_scatter, extract_valuation_cards
from agystats.report import render_conversation_list, render_report
from agystats.tui import TUIState, apply_tui_key, build_tui_renderable, requested_conv_id, run_tui


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

    def test_project_report_steps_telemetry_future_rates_and_tui_project_parent_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_data = root / "antigravity-cli"
            brain = app_data / "brain"
            state = app_data / "agystats"
            projects_dir = root / "config" / "projects"
            projects_dir.mkdir(parents=True)

            p1_id = "proj-alpha"
            p2_id = "proj-beta"
            (projects_dir / f"{p1_id}.json").write_text(
                json.dumps(
                    {
                        "id": p1_id,
                        "name": "Alpha Repo",
                        "projectResources": {"resources": [{"folderUri": "file:///work/alpha"}]},
                    }
                ),
                encoding="utf-8",
            )
            (projects_dir / f"{p2_id}.json").write_text(
                json.dumps(
                    {
                        "id": p2_id,
                        "name": "Beta Repo",
                        "projectResources": {"resources": [{"folderUri": "file:///work/beta"}]},
                    }
                ),
                encoding="utf-8",
            )

            _, _, cid, sub_id = _seed_workspace(app_data)
            cid_beta = "cccccccc-1111-2222-3333-444444444444"
            _write_jsonl(
                brain / cid_beta / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T11:00:00Z",
                        "content": "<USER_REQUEST>\nBeta work\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T11:00:01Z",
                        "model": "gemini-4-carbon-tiered-high",
                        "input_tokens": 4000,
                        "cache_read_tokens": 0,
                        "output_tokens": 250,
                        "content": "Beta done.",
                        "tool_calls": [],
                    },
                ],
            )
            db_path = app_data / "conversation_summaries.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        workspace_uris TEXT NOT NULL DEFAULT '[]',
                        project_id TEXT NOT NULL DEFAULT '',
                        agent_name TEXT NOT NULL DEFAULT '',
                        parent_conversation_id TEXT NOT NULL DEFAULT '',
                        nesting_depth INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (cid, "Alpha Build", '["file:///work/alpha"]', p1_id, "", "", 0),
                        (sub_id, "", '["file:///work/alpha"]', p1_id, "browser", cid, 1),
                        (cid_beta, "Beta Build", '["file:///work/beta"]', p2_id, "", "", 0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            eng = MetricsEngine(
                brain_dir=brain,
                state_dir=state,
                app_data_dir=app_data,
                projects_dir=projects_dir,
            )
            try:
                # 1. Per-step telemetry upgraded to Time (~Delay) + Paygo; subagent model strips prefix
                conv_txt = render_report(eng, conversation_id=cid, no_color=True, width=140)
                self.assertIn("Time (~Delay)", conv_txt)
                self.assertIn("Paygo", conv_txt)
                self.assertIn("Researcher · browser", conv_txt)
                self.assertIn("Project: Alpha Repo", conv_txt)
                # Step table should not have the old per-step tok/s column
                step_section = conv_txt.split("Per-Step Telemetry")[1]
                self.assertNotIn("tok/s", step_section)
                sub_section = conv_txt.split("Subagent Rollup")[1].split("Per-Step Telemetry")[0]
                self.assertNotIn("Subagent ·", sub_section)

                # 2. Future rate card formatting keeps USD + % inline without credits
                m_beta = eng.get_metrics(cid_beta)
                cards, _, _ = extract_valuation_cards(m_beta, None)
                self.assertTrue(cards[0]["has_future"])
                self.assertIn("(", cards[0]["future_text"])
                self.assertNotIn(" cr", cards[0]["future_text"])

                # 3. Project-scoped report rendering
                proj_txt = render_report(
                    eng,
                    conversation_id=f"{PROJECT_SCOPE_PREFIX}{p1_id}",
                    window="all",
                    no_color=True,
                    width=140,
                )
                self.assertIn("Scope: Project · Alpha Repo (all)", proj_txt)
                self.assertIn("Project Timing", proj_txt)
                self.assertNotIn("Active Context Window", proj_txt)

                # 4. TUI project cycle (`p`), window cycle (`w`), and subagent parent jump (`u`)
                snap_parent = eng.get_snapshot(cid)
                tui_state = TUIState()
                self.assertTrue(apply_tui_key("p", tui_state, snap_parent))
                self.assertEqual(tui_state.view_mode, "project")
                self.assertIsNotNone(tui_state.selected_project_scope_id)
                first_proj_scope = tui_state.selected_project_scope_id
                self.assertEqual(requested_conv_id(tui_state), first_proj_scope)

                self.assertTrue(apply_tui_key("p", tui_state, snap_parent))
                self.assertEqual(tui_state.view_mode, "project")
                self.assertNotEqual(tui_state.selected_project_scope_id, first_proj_scope)

                self.assertTrue(apply_tui_key("w", tui_state, snap_parent))
                self.assertEqual(tui_state.view_mode, "project")
                self.assertEqual(tui_state.time_window, "5h")

                snap_proj = eng.get_snapshot(tui_state.selected_project_scope_id, window=tui_state.time_window)
                buf_proj = io.StringIO()
                Console(file=buf_proj, width=120, height=40, no_color=True, color_system=None).print(
                    build_tui_renderable(snap_proj, tui_state, width=120, height=40)
                )
                self.assertIn("[Project]", buf_proj.getvalue())

                snap_sub = eng.get_snapshot(sub_id)
                buf_sub = io.StringIO()
                Console(file=buf_sub, width=120, height=40, no_color=True, color_system=None).print(
                    build_tui_renderable(snap_sub, TUIState(follow_active=False, selected_conv_id=sub_id), width=120, height=40)
                )
                sub_frame = buf_sub.getvalue()
                self.assertIn("Parent: Alpha Build", sub_frame)
                self.assertIn("u:Parent", sub_frame)

                self.assertTrue(apply_tui_key("u", tui_state, snap_sub))
                self.assertEqual(tui_state.view_mode, "conversation")
                self.assertFalse(tui_state.follow_active)
                self.assertEqual(tui_state.selected_conv_id, cid)
            finally:
                eng.close()

            # 5. CLI --project dispatch and unknown project exit code 2
            stdout_buf = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = stdout_buf
                rc_proj = cli_main(
                    [
                        "report",
                        "--app-data-dir",
                        str(app_data),
                        "--projects-dir",
                        str(projects_dir),
                        "-P",
                        "Alpha",
                        "--no-color",
                    ]
                )
            finally:
                sys.stdout = old_stdout
            self.assertEqual(rc_proj, 0)
            self.assertIn("Project · Alpha Repo", stdout_buf.getvalue())

            err_buf = io.StringIO()
            old_stderr = sys.stderr
            try:
                sys.stderr = err_buf
                rc_miss = cli_main(
                    [
                        "report",
                        "--app-data-dir",
                        str(app_data),
                        "--projects-dir",
                        str(projects_dir),
                        "--project",
                        "no-such-project",
                    ]
                )
            finally:
                sys.stderr = old_stderr
            self.assertEqual(rc_miss, 2)


if __name__ == "__main__":
    unittest.main()
