"""Tests for Antigravity CLI paths, SQLite protobuf timing, summaries DB, and transcript parsing."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agystats.engine import ALL_CONVERSATIONS_ID, MetricsEngine, PROJECT_SCOPE_PREFIX
from agystats.metadata import parse_pbtxt_title
from agystats.models import _ROLLUP_SCHEMA_VERSION
from agystats.parser import decode_cli_step_metadata_timestamps
from agystats.paths import get_app_data_dir, get_brain_dir, get_state_dir
from hooks import run_hook


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    v = int(value)
    while True:
        byte = v & 0x7F
        v >>= 7
        if v:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _encode_ts(epoch_s: float) -> bytes:
    secs = int(epoch_s)
    nanos = int(round((epoch_s - secs) * 1_000_000_000))
    if nanos >= 1_000_000_000:
        secs += 1
        nanos -= 1_000_000_000
    return (
        b"\x08" + _encode_varint(secs) + b"\x10" + _encode_varint(nanos)
    )


def _encode_step_metadata(
    *,
    start_s: float | None = None,
    first_stream_s: float | None = None,
    end_s: float | None = None,
    exec_end_s: float | None = None,
    exec_start_s: float | None = None,
) -> bytes:
    buf = bytearray()
    for field_num, ts in (
        (1, start_s),
        (6, first_stream_s),
        (7, end_s),
        (8, exec_end_s),
        (32, exec_start_s),
    ):
        if ts is None:
            continue
        sub = _encode_ts(ts)
        tag = (field_num << 3) | 2
        buf.extend(_encode_varint(tag))
        buf.extend(_encode_varint(len(sub)))
        buf.extend(sub)
    return bytes(buf)


def _create_cli_conv_db(
    db_path: Path,
    rows: list[tuple[int, int, int, bytes]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS steps (
                idx integer PRIMARY KEY,
                step_type integer NOT NULL DEFAULT 0,
                status integer NOT NULL DEFAULT 0,
                metadata blob
            )
            """
        )
        conn.execute("DELETE FROM steps")
        conn.executemany(
            "INSERT INTO steps (idx, step_type, status, metadata) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _create_cli_summaries_db(
    db_path: Path,
    rows: list[tuple[str, str, str, str, int]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                conversation_id text PRIMARY KEY,
                title text NOT NULL DEFAULT "",
                status text NOT NULL DEFAULT "",
                parent_conversation_id text NOT NULL DEFAULT "",
                not_fully_idle numeric NOT NULL DEFAULT false
            )
            """
        )
        conn.execute("DELETE FROM conversation_summaries")
        conn.executemany(
            """
            INSERT INTO conversation_summaries (
                conversation_id, title, status, parent_conversation_id, not_fully_idle
            ) VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class TestEngineAndCliDb(unittest.TestCase):
    def test_paths_prefer_antigravity_cli_and_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            cli_dir = fake_home / ".gemini" / "antigravity-cli"
            desktop_dir = fake_home / ".gemini" / "antigravity"
            cli_dir.mkdir(parents=True)
            desktop_dir.mkdir(parents=True)

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "AGYSTATS_APP_DATA_DIR",
                    "AGY_METRICS_APP_DATA_DIR",
                    "ANTIGRAVITY_CLI_APP_DATA_DIR",
                    "ANTIGRAVITY_APP_DATA_DIR",
                    "AGYSTATS_BRAIN_DIR",
                    "AGY_METRICS_BRAIN_DIR",
                    "AGYSTATS_STATE_DIR",
                    "AGY_METRICS_STATE_DIR",
                    "PLUGIN_DATA",
                )
            }
            with (
                patch.dict(os.environ, clean_env, clear=True),
                patch("pathlib.Path.home", return_value=fake_home),
            ):
                self.assertEqual(get_app_data_dir(), cli_dir)
                self.assertEqual(get_brain_dir(), cli_dir / "brain")
                self.assertEqual(get_state_dir(), cli_dir / "agystats")

                custom_app = Path(tmp) / "custom-cli"
                with patch.dict(
                    os.environ,
                    {"AGYSTATS_APP_DATA_DIR": str(custom_app)},
                    clear=False,
                ):
                    self.assertEqual(get_app_data_dir(), custom_app)
                    self.assertEqual(get_brain_dir(), custom_app / "brain")

    def test_protobuf_metadata_timing_and_cli_model_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "antigravity-cli"
            brain = app_dir / "brain"
            state = app_dir / "agystats"
            cid = "11111111-1111-1111-1111-111111111111"

            t0 = datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc).timestamp()
            iso = lambda ep: datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            md_test = _encode_step_metadata(
                start_s=t0 + 1.0, end_s=t0 + 4.25, exec_start_s=t0 + 4.26, exec_end_s=t0 + 5.10
            )
            decoded = decode_cli_step_metadata_timestamps(md_test)
            self.assertAlmostEqual(decoded["start_s"], t0 + 1.0, places=4)
            self.assertAlmostEqual(decoded["end_s"], t0 + 4.25, places=4)
            self.assertAlmostEqual(decoded["exec_start_s"], t0 + 4.26, places=4)
            self.assertAlmostEqual(decoded["exec_end_s"], t0 + 5.10, places=4)

            _write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": iso(t0),
                        "content": "<USER_REQUEST>\nPort CLI metrics\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": iso(t0 + 1),
                        "model": "gemini-pro-orchestrator-argon",
                        "input_tokens": 9000,
                        "cache_read_tokens": 0,
                        "output_tokens": 300,
                        "thinking": "Analyzing...",
                        "content": "",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "/tmp/a.py"}}],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": iso(t0 + 5),
                        "content": "file contents without Created At header",
                    },
                    {
                        "step_index": 3,
                        "type": "SYSTEM_MESSAGE",
                        "created_at": iso(t0 + 6),
                        "content": "<SYSTEM_MESSAGE>Plan mode is off: this turn runs on the execution model.</SYSTEM_MESSAGE>",
                    },
                    {
                        "step_index": 4,
                        "type": "PLANNER_RESPONSE",
                        "created_at": iso(t0 + 7),
                        "model": "gemini-flash-worker-agy",
                        "input_tokens": 1200,
                        "cache_read_tokens": 8500,
                        "output_tokens": 600,
                        "thinking": "",
                        "content": "Implemented.",
                        "tool_calls": [],
                    },
                ],
            )

            db_path = app_dir / "conversations" / f"{cid}.db"
            _create_cli_conv_db(
                db_path,
                [
                    (0, 14, 3, _encode_step_metadata(start_s=t0)),
                    (1, 15, 3, _encode_step_metadata(start_s=t0 + 1.0, end_s=t0 + 4.0)),
                    (2, 132, 3, _encode_step_metadata(start_s=t0 + 3.5, exec_start_s=t0 + 4.01, exec_end_s=t0 + 4.46)),
                    (3, 18, 3, _encode_step_metadata(start_s=t0 + 6.0)),
                    (4, 15, 3, _encode_step_metadata(start_s=t0 + 7.0, end_s=t0 + 9.0)),
                ],
            )
            _create_cli_summaries_db(
                app_dir / "conversation_summaries.db",
                [(cid, "Port CLI Metrics", "CASCADE_RUN_STATUS_IDLE", "", 0)],
            )

            eng = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m = eng.get_metrics(cid)
                self.assertEqual(m["model_name"], "Orchestrator (Argon) · Executor")
                self.assertEqual(len(m["steps"]), 2)

                s1, s2 = m["steps"]
                self.assertEqual(s1["model_name"], "Orchestrator (Argon) · Planner")
                self.assertEqual(s1["duration_source"], "db_ms")
                self.assertAlmostEqual(s1["duration_s"], 3.0, places=2)
                self.assertAlmostEqual(s1["tool_exec_s"], 0.45, places=2)
                self.assertAlmostEqual(s1["effective_tps"], 100.0, places=1)

                self.assertEqual(s2["model_name"], "Orchestrator (Argon) · Executor")
                self.assertEqual(s2["duration_source"], "db_ms")
                self.assertAlmostEqual(s2["duration_s"], 2.0, places=2)
                self.assertAlmostEqual(s2["effective_tps"], 300.0, places=1)

                # Verify Orchestrator counterfactual savings were attributed
                orch = m["valuation"]["orchestrator"]
                self.assertTrue(orch["present"])
                self.assertEqual(orch["executor_steps"], 1)
                self.assertGreater(orch["savings_usd"], 0.0)
            finally:
                eng.close()

    def test_cli_live_running_detection_without_hooks_and_db_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "antigravity-cli"
            brain = app_dir / "brain"
            state = app_dir / "agystats"
            cid = "22222222-2222-2222-2222-222222222222"

            now_s = time.time()
            t0 = now_s - 5.0
            iso = lambda ep: datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            tr_path = brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(
                tr_path,
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": iso(t0),
                        "content": "Live turn",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": iso(t0 + 1.0),
                        "model": "gemini-4-argon-medium",
                        "input_tokens": 5000,
                        "cache_read_tokens": 1000,
                        "output_tokens": 200,
                        "content": "",
                        "tool_calls": [{"name": "run_command", "args": {}}],
                    },
                ],
            )
            db_path = app_dir / "conversations" / f"{cid}.db"
            _create_cli_conv_db(
                db_path,
                [
                    (0, 14, 3, _encode_step_metadata(start_s=t0)),
                    (1, 15, 3, _encode_step_metadata(start_s=t0 + 1.0, end_s=t0 + 3.0)),
                    (2, 132, 2, _encode_step_metadata(start_s=t0 + 3.1)),
                ],
            )
            _create_cli_summaries_db(
                app_dir / "conversation_summaries.db",
                [(cid, "Live CLI Session", "CASCADE_RUN_STATUS_RUNNING", "", 1)],
            )

            eng = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                eng.refresh_if_changed(fast_poll=True)
                self.assertTrue(eng.is_generating())
                m1 = eng.get_metrics(cid, refresh=False)
                self.assertEqual(m1["status"], "generating")
                self.assertEqual(m1["model_name"], "Gemini 4 Argon (Medium)")

                # Complete step 2 in transcript + DB and mark summaries DB idle
                with open(tr_path, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "step_index": 2,
                                "type": "GENERIC",
                                "created_at": iso(t0 + 4.1),
                                "content": "command output",
                            }
                        )
                        + "\n"
                    )
                _create_cli_conv_db(
                    db_path,
                    [
                        (0, 14, 3, _encode_step_metadata(start_s=t0)),
                        (1, 15, 3, _encode_step_metadata(start_s=t0 + 1.0, end_s=t0 + 3.0)),
                        (2, 132, 3, _encode_step_metadata(start_s=t0 + 3.1, exec_start_s=t0 + 3.1, exec_end_s=t0 + 4.1)),
                    ],
                )
                _create_cli_summaries_db(
                    app_dir / "conversation_summaries.db",
                    [(cid, "Live CLI Session", "CASCADE_RUN_STATUS_IDLE", "", 0)],
                )

                eng.refresh_if_changed(fast_poll=True)
                self.assertFalse(eng.is_generating())
                m2 = eng.get_metrics(cid, refresh=False)
                self.assertEqual(m2["status"], "idle")
                self.assertAlmostEqual(m2["steps"][0]["tool_exec_s"], 1.0, places=2)
                self.assertEqual(eng.get_metrics(ALL_CONVERSATIONS_ID)["totals"]["steps_count"], 1)
            finally:
                eng.close()

    def test_executable_data_dir_and_transcript_path_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exec_data = root / "custom_app" / "sidecar_data" / "agystats" / "dashboard" / "data"
            exec_data.mkdir(parents=True)
            base_clean = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "AGYSTATS_STATE_DIR",
                    "AGY_METRICS_STATE_DIR",
                    "PLUGIN_DATA",
                    "AGYSTATS_APP_DATA_DIR",
                    "AGY_METRICS_APP_DATA_DIR",
                    "ANTIGRAVITY_CLI_APP_DATA_DIR",
                    "ANTIGRAVITY_APP_DATA_DIR",
                    "ANTIGRAVITY_EXECUTABLE_DATA_DIR",
                )
            }
            with patch.dict(
                os.environ,
                {**base_clean, "ANTIGRAVITY_EXECUTABLE_DATA_DIR": str(exec_data)},
                clear=True,
            ):
                self.assertEqual(get_app_data_dir(), root / "custom_app")
                self.assertEqual(get_brain_dir(), root / "custom_app" / "brain")
                self.assertEqual(get_state_dir(), root / "custom_app" / "agystats")
                self.assertEqual(
                    Path(run_hook._get_state_dir()),
                    root / "custom_app" / "agystats",
                )

            fake_home = root / "home"
            (fake_home / ".gemini" / "config" / "brain").mkdir(parents=True)
            (fake_home / ".gemini" / "antigravity_remote" / "brain").mkdir(parents=True)
            with patch.dict(
                os.environ, {**base_clean, "HOME": str(fake_home)}, clear=True
            ):
                self.assertEqual(
                    get_app_data_dir(), fake_home / ".gemini" / "antigravity_remote"
                )

            repo_root = Path(__file__).resolve().parents[1]
            cid = "ffffffff-1111-2222-3333-444444444444"
            t_path = (
                root
                / "remote_app"
                / "brain"
                / cid
                / ".system_generated"
                / "logs"
                / "transcript.jsonl"
            )
            t_path.parent.mkdir(parents=True)
            t_path.write_text("", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-S", "./hooks/run_hook.py", "PreInvocation"],
                cwd=str(repo_root),
                env={**base_clean, "HOME": str(fake_home)},
                input=json.dumps(
                    {
                        "conversationId": cid,
                        "modelName": "Carbon",
                        "hookEventName": "PreInvocation",
                        "transcriptPath": str(t_path),
                    }
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(proc.stdout), {})
            expected_hook_log = (
                root / "remote_app" / "agystats" / "conversations" / f"{cid}.hooks.jsonl"
            )
            self.assertTrue(expected_hook_log.is_file())

    def test_pbtxt_octal_utf8_and_live_rename_when_dehydrated(self) -> None:
        self.assertEqual(
            parse_pbtxt_title(
                'title:"Caf\\303\\251 \\342\\200\\224 Fix \\"Selector\\""  last_user_view_time:{seconds:1}'
            ),
            'Café — Fix "Selector"',
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_data = root / "antigravity-cli"
            brain = app_data / "brain"
            state = app_data / "agystats"
            projects_dir = root / "config" / "projects"
            projects_dir.mkdir(parents=True)

            cid = "11111111-aaaa-1111-aaaa-111111111111"
            _write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T01:00:00Z",
                        "content": "<USER_REQUEST>\nVery long initial prompt text that should be replaced\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T01:00:02Z",
                        "input_tokens": 1000,
                        "cache_read_tokens": 500,
                        "output_tokens": 120,
                        "content": "Done.",
                        "tool_calls": [],
                    },
                ],
            )
            _create_cli_summaries_db(
                app_data / "conversation_summaries.db",
                [(cid, "Old Summary Title", "CASCADE_RUN_STATUS_IDLE", "", 0)],
            )
            ann_path = app_data / "annotations" / f"{cid}.pbtxt"
            ann_path.parent.mkdir(parents=True)
            ann_path.write_text('title:"Initial Sidebar Name"\n', encoding="utf-8")
            db_path = app_data / "conversation_summaries.db"
            db_mtime_ns = db_path.stat().st_mtime_ns
            os.utime(ann_path, ns=(db_mtime_ns + 5_000_000, db_mtime_ns + 5_000_000))

            eng1 = MetricsEngine(
                brain_dir=brain,
                state_dir=state,
                app_data_dir=app_data,
                projects_dir=projects_dir,
            )
            try:
                convs1 = eng1.list_conversations()
                self.assertEqual(convs1["conversations"][0]["title"], "Initial Sidebar Name")
            finally:
                eng1.close()

            eng2 = MetricsEngine(
                brain_dir=brain,
                state_dir=state,
                app_data_dir=app_data,
                projects_dir=projects_dir,
            )
            try:
                convs_cold = eng2.list_conversations()
                self.assertEqual(convs_cold["conversations"][0]["title"], "Initial Sidebar Name")
                self.assertFalse(eng2._conversations[cid].hydrated)
                etag_before = eng2.get_state_etag(cid, refresh=False)

                ann_path.write_text('title:"Renamed By User \\342\\234\\223"\n', encoding="utf-8")
                os.utime(
                    ann_path,
                    ns=(db_mtime_ns + 20_000_000, db_mtime_ns + 20_000_000),
                )

                self.assertTrue(eng2.refresh_if_changed())
                convs_after = eng2.list_conversations(refresh=False)
                self.assertEqual(convs_after["conversations"][0]["title"], "Renamed By User ✓")
                self.assertEqual(eng2.get_metrics(cid, refresh=False)["title"], "Renamed By User ✓")
                self.assertNotEqual(eng2.get_state_etag(cid, refresh=False), etag_before)

                other_cid = "22222222-bbbb-2222-bbbb-222222222222"
                conn = sqlite3.connect(str(db_path))
                try:
                    conn.execute(
                        """
                        INSERT INTO conversation_summaries (
                            conversation_id, title, status, parent_conversation_id, not_fully_idle
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (other_cid, "Another Conv", "CASCADE_RUN_STATUS_IDLE", "", 0),
                    )
                    conn.commit()
                finally:
                    conn.close()
                os.utime(db_path, ns=(db_mtime_ns + 40_000_000, db_mtime_ns + 40_000_000))

                eng2.refresh_if_changed()
                self.assertEqual(
                    eng2.list_conversations(refresh=False)["conversations"][0]["title"],
                    "Renamed By User ✓",
                )
                self.assertEqual(
                    eng2.get_metrics(cid, refresh=False)["title"],
                    "Renamed By User ✓",
                )
            finally:
                eng2.close()

    def test_projects_hierarchy_tree_order_and_project_scoped_windows(self) -> None:
        fixed_now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        iso = lambda ep: datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_data = root / "antigravity-cli"
            brain = app_data / "brain"
            state = app_data / "agystats"
            projects_dir = root / "config" / "projects"
            projects_dir.mkdir(parents=True)

            p1_id = "p1-uuid"
            p2_id = "p2-uuid"
            p_sidecar_id = "p-sidecar"
            (projects_dir / f"{p1_id}.json").write_text(
                json.dumps(
                    {
                        "id": p1_id,
                        "name": "Project One",
                        "projectResources": {
                            "resources": [{"gitFolder": {"folderUri": "file:///work/one"}}]
                        },
                    }
                ),
                encoding="utf-8",
            )
            (projects_dir / f"{p2_id}.json").write_text(
                json.dumps(
                    {
                        "id": p2_id,
                        "name": "Project Two",
                        "projectResources": {
                            "resources": [{"folderUri": "file:///work/two"}]
                        },
                    }
                ),
                encoding="utf-8",
            )
            (projects_dir / f"{p_sidecar_id}.json").write_text(
                json.dumps(
                    {
                        "id": p_sidecar_id,
                        "name": "one/dashboard",
                        "projectResources": {},
                        "sidecar": {"sidecarId": "one/dashboard"},
                    }
                ),
                encoding="utf-8",
            )

            c1 = "11111111-0000-0000-0000-000000000001"
            c1_sub = "11111111-0000-0000-0000-000000000002"
            c2 = "22222222-0000-0000-0000-000000000001"
            c_base = "33333333-0000-0000-0000-000000000001"

            t_old = fixed_now - 48 * 3600
            t_recent = fixed_now - 1800
            t_sub = fixed_now - 900

            _write_jsonl(
                brain / c1 / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": iso(t_old - 5),
                        "content": "<USER_REQUEST>\nLong P1 Prompt\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High).\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": iso(t_old),
                        "input_tokens": 1000,
                        "cache_read_tokens": 500,
                        "output_tokens": 100,
                        "content": "",
                        "tool_calls": [{"name": "invoke_subagent", "args": {"Role": '"P1 Helper"'}}],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": iso(t_old + 2),
                        "content": f"Created At: {iso(t_old + 2)}\nCompleted At: {iso(t_old + 4)}\nSpawned {c1_sub}\n",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": iso(t_recent),
                        "input_tokens": 400,
                        "cache_read_tokens": 1600,
                        "output_tokens": 200,
                        "content": "Recent P1 step",
                        "tool_calls": [],
                    },
                ],
            )
            _write_jsonl(
                brain / c1_sub / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": iso(t_sub - 5),
                        "content": "Subagent task",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": iso(t_sub),
                        "input_tokens": 300,
                        "cache_read_tokens": 200,
                        "output_tokens": 50,
                        "content": "Sub done",
                        "tool_calls": [],
                    },
                ],
            )
            _write_jsonl(
                brain / c2 / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": iso(t_recent - 5),
                        "content": "<USER_REQUEST>\nP2 Prompt\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High).\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": iso(t_recent + 60),
                        "input_tokens": 5000,
                        "cache_read_tokens": 0,
                        "output_tokens": 900,
                        "content": "P2 big reply",
                        "tool_calls": [],
                    },
                ],
            )
            _write_jsonl(
                brain / c_base / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": iso(t_old - 10),
                        "content": "Scratch workspace prompt",
                    }
                ],
            )
            _write_jsonl(
                state / "conversations" / f"{c2}.hooks.jsonl",
                [
                    {
                        "ts_ms": int((t_recent + 60) * 1000),
                        "event": "SessionStart",
                        "conversation_id": c2,
                        "workspace_paths": ["/work/two/subdir"],
                    }
                ],
            )
            _write_jsonl(
                state / "conversations" / f"{c_base}.hooks.jsonl",
                [
                    {
                        "ts_ms": int((t_old - 10) * 1000),
                        "event": "SessionStart",
                        "conversation_id": c_base,
                        "workspace_paths": ["/tmp/unregistered-workspace"],
                    }
                ],
            )

            db_path = app_data / "conversation_summaries.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
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
                        nesting_depth INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT '',
                        not_fully_idle INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (c1, "P1 Conv", '["file:///work/one"]', p1_id, "", "", 0, "CASCADE_RUN_STATUS_IDLE", 0),
                        (c1_sub, "", '["file:///work/one"]', p1_id, "research", c1, 1, "CASCADE_RUN_STATUS_IDLE", 0),
                        (c2, "P2 Conv", '[]', "", "", "", 0, "CASCADE_RUN_STATUS_IDLE", 0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with patch("agystats.engine.time.time", return_value=fixed_now):
                eng = MetricsEngine(
                    brain_dir=brain,
                    state_dir=state,
                    app_data_dir=app_data,
                    projects_dir=projects_dir,
                )
                try:
                    convs = eng.list_conversations()
                    proj_ids = [p["project_id"] for p in convs["projects"]]
                    self.assertNotIn(p_sidecar_id, proj_ids)
                    self.assertIn(p1_id, proj_ids)
                    self.assertIn(p2_id, proj_ids)
                    self.assertIn("path:/tmp/unregistered-workspace", proj_ids)

                    proj_by_id = {p["project_id"]: p for p in convs["projects"]}
                    self.assertEqual(proj_by_id["path:/tmp/unregistered-workspace"]["name"], "unregistered-workspace")
                    self.assertEqual(proj_by_id[p1_id]["conversations_count"], 1)
                    self.assertEqual(proj_by_id[p1_id]["subagents_count"], 1)

                    items = convs["conversations"]
                    by_id = {c["conversation_id"]: c for c in items}
                    self.assertEqual(by_id[c1]["prompt_preview"], "Long P1 Prompt")
                    self.assertEqual(by_id[c1_sub]["title"], "P1 Helper")
                    self.assertEqual(by_id[c1_sub]["agent_name"], "research")
                    self.assertEqual(by_id[c1_sub]["nesting_depth"], 1)
                    self.assertEqual(by_id[c2]["project_id"], p2_id)
                    c1_idx = next(i for i, c in enumerate(items) if c["conversation_id"] == c1)
                    self.assertEqual(items[c1_idx + 1]["conversation_id"], c1_sub)

                    sub_met = eng.get_metrics(c1_sub)
                    self.assertTrue(sub_met["is_subagent"])
                    self.assertEqual(sub_met["parent_id"], c1)
                    self.assertEqual(sub_met["parent_title"], "P1 Conv")

                    p1_scope = f"{PROJECT_SCOPE_PREFIX}{p1_id}"
                    m_p1 = eng.get_metrics(p1_scope, window="all")
                    self.assertEqual(m_p1["mode"], "project")
                    self.assertEqual(m_p1["project_id"], p1_id)
                    self.assertEqual(m_p1["title"], "Project One")
                    self.assertEqual(m_p1["totals"]["steps_count"], 3)
                    self.assertEqual(m_p1["totals"]["output_tokens"], 350)

                    m_p1_5h = eng.get_metrics(p1_scope, window="5h")
                    self.assertEqual(m_p1_5h["totals"]["steps_count"], 2)
                    self.assertEqual(m_p1_5h["totals"]["output_tokens"], 250)
                    self.assertAlmostEqual(
                        m_p1_5h["valuation"]["windows"]["5h"]["cost_usd"],
                        m_p1_5h["valuation"]["windows"]["selected"]["cost_usd"],
                        places=8,
                    )
                    self.assertGreater(
                        m_p1_5h["valuation"]["windows"]["7d"]["cost_usd"],
                        m_p1_5h["valuation"]["windows"]["5h"]["cost_usd"],
                    )
                finally:
                    eng.close()

    def test_subagent_hook_model_slug_normalized_and_warm_restarted_v7(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_parent = "11111111-cccc-0000-0000-000000000001"
            cid_sub = "22222222-cccc-0000-0000-000000000002"
            t0 = int(datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

            _write_jsonl(
                brain / cid_parent / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T10:00:00Z",
                        "content": "<USER_REQUEST>\nDelegate work\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High).\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T10:00:01Z",
                        "input_tokens": 10000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100,
                        "content": "",
                        "tool_calls": [
                            {"name": "invoke_subagent", "args": {"Role": '"Helper"'}}
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-16T10:00:02Z",
                        "content": f"Spawned {cid_sub}",
                    },
                ],
            )
            _write_jsonl(
                brain / cid_sub / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T10:00:02Z",
                        "content": "Execute subtask",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T10:00:03Z",
                        "input_tokens": 5000,
                        "cache_read_tokens": 0,
                        "output_tokens": 200,
                        "content": "Subtask complete.",
                        "tool_calls": [],
                    },
                ],
            )
            _write_jsonl(
                state / "conversations" / f"{cid_sub}.hooks.jsonl",
                [
                    {
                        "ts_ms": t0 + 3000,
                        "event": "PreInvocation",
                        "conversation_id": cid_sub,
                        "model_name": "gemini-4-hydrogen-tiered-high",
                    },
                    {
                        "ts_ms": t0 + 5000,
                        "event": "PostInvocation",
                        "conversation_id": cid_sub,
                        "model_name": "gemini-4-hydrogen-tiered-high",
                    },
                ],
            )

            eng1 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                sub_m = eng1.get_metrics(cid_sub)
                self.assertEqual(sub_m["model_name"], "Subagent · Gemini 4 Hydrogen (High)")
                self.assertEqual(sub_m["steps"][0]["model_name"], "Subagent · Gemini 4 Hydrogen (High)")
                self.assertEqual(
                    [bm["model_name"] for bm in sub_m["by_model"]],
                    ["Subagent · Gemini 4 Hydrogen (High)"],
                )
                self.assertEqual(sub_m["by_model"][0]["valuation"]["status"], "priced")

                parent_m = eng1.get_metrics(cid_parent)
                self.assertEqual(
                    parent_m["subagents"]["items"][0]["model_name"],
                    "Subagent · Gemini 4 Hydrogen (High)",
                )

                all_m = eng1.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                self.assertEqual(
                    {bm["model_name"] for bm in all_m["by_model"]},
                    {"Gemini 4 Carbon (High)", "Subagent · Gemini 4 Hydrogen (High)"},
                )
            finally:
                eng1.close()

            eng2 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                all_m2 = eng2.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                self.assertEqual(eng2._transcript_open_count, 0)
                self.assertEqual(
                    {bm["model_name"] for bm in all_m2["by_model"]},
                    {"Gemini 4 Carbon (High)", "Subagent · Gemini 4 Hydrogen (High)"},
                )
            finally:
                eng2.close()

    def test_rollup_schema_migration_v3_to_v7(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "77777777-1111-2222-3333-444444444444"
            _write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T10:00:00Z",
                        "content": "Recent question",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T10:00:02Z",
                        "input_tokens": 3000,
                        "cache_read_tokens": 1000,
                        "output_tokens": 150,
                        "content": "Done",
                        "tool_calls": [],
                    },
                ],
            )
            state.mkdir(parents=True, exist_ok=True)
            db_path = state / "rollup_cache.sqlite3"
            for old_ver in (3, 4, 5, 6):
                db_path.unlink(missing_ok=True)
                old_conn = sqlite3.connect(str(db_path))
                old_conn.execute(f"PRAGMA user_version = {old_ver}")
                old_conn.execute("CREATE TABLE conv_meta (conv_id TEXT PRIMARY KEY)")
                old_conn.execute("CREATE TABLE step_lite (conv_id TEXT, ordinal INT)")
                old_conn.commit()
                old_conn.close()

                eng_mig = MetricsEngine(brain_dir=brain, state_dir=state)
                try:
                    m_all = eng_mig.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                    self.assertEqual(m_all["totals"]["output_tokens"], 150)
                    self.assertEqual(
                        eng_mig._db.execute("PRAGMA user_version").fetchone()[0],
                        _ROLLUP_SCHEMA_VERSION,
                    )
                finally:
                    eng_mig.close()


if __name__ == "__main__":
    unittest.main()
