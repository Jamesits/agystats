"""Tests for Antigravity CLI paths, SQLite protobuf timing, summaries DB, and transcript parsing."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agystats.engine import ALL_CONVERSATIONS_ID, MetricsEngine
from agystats.parser import decode_cli_step_metadata_timestamps
from agystats.paths import get_app_data_dir, get_brain_dir, get_state_dir


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


if __name__ == "__main__":
    unittest.main()
