"""CLI argument parser and dispatcher for agystats (Report Mode & Realtime TUI)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from .engine import ALL_CONVERSATIONS_ID, MetricsEngine, VALID_WINDOWS
from .formatters import resolve_conversation_id, resolve_model_entry
from .paths import get_brain_dir, get_state_dir
from .report import render_conversation_list, render_report
from .tui import TUIState, run_tui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agystats",
        description="Local telemetry, latency roofline, and offline cost/credits dashboard for Antigravity CLI.",
    )
    parser.add_argument(
        "mode_cmd",
        nargs="?",
        choices=["report", "tui", "live", "watch", "list"],
        help="Execution mode: 'report' (text output), 'tui'/'live'/'watch' (realtime TUI), or 'list' (conversations). Defaults to TUI on TTY, else report.",
    )

    mode_flags = parser.add_mutually_exclusive_group()
    mode_flags.add_argument(
        "-r",
        "--report",
        action="store_true",
        help="Run Report Mode (print text/JSON summary and exit).",
    )
    mode_flags.add_argument(
        "-t",
        "--tui",
        "--live",
        dest="tui",
        action="store_true",
        help="Run Realtime Mode TUI.",
    )
    mode_flags.add_argument(
        "-l",
        "--list",
        dest="list_convs",
        action="store_true",
        help="List discovered conversations and exit.",
    )

    scope_flags = parser.add_mutually_exclusive_group()
    scope_flags.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Aggregate across all conversations instead of a single session.",
    )
    scope_flags.add_argument(
        "-c",
        "--conversation",
        default=None,
        help="Select conversation by exact ID, ID prefix, or title substring.",
    )

    parser.add_argument(
        "-w",
        "--window",
        choices=list(VALID_WINDOWS),
        default="all",
        help="Time window when using --all mode (default: all).",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="Filter by model name (exact or substring match) and show latency roofline.",
    )
    parser.add_argument(
        "-p",
        "--pool",
        choices=["all", "gemini", "partner"],
        default="all",
        help="Quota pool filter for Paygo & AI Credits (default: all).",
    )
    parser.add_argument(
        "-n",
        "--steps",
        type=int,
        default=15,
        help="Number of recent steps to show in Report Mode (-1 for all, 0 to hide, default: 15).",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Output raw JSON in Report Mode or List Mode.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in output.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.25,
        help="Polling interval in seconds for Realtime TUI Mode (default: 0.25).",
    )
    parser.add_argument(
        "--app-data-dir",
        type=Path,
        default=None,
        help="Override Antigravity app data directory (default: ~/.gemini/antigravity-cli).",
    )
    parser.add_argument(
        "--brain-dir",
        type=Path,
        default=None,
        help="Override brain directory containing conversation transcripts.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Override persistent cache & pricing override state directory.",
    )
    return parser


def _resolve_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    cmd = args.mode_cmd
    if cmd in ("live", "watch"):
        cmd = "tui"

    flag_mode: str | None = None
    if args.list_convs:
        flag_mode = "list"
    elif args.report:
        flag_mode = "report"
    elif args.tui:
        flag_mode = "tui"

    if cmd and flag_mode and cmd != flag_mode:
        parser.error(f"Conflicting mode arguments: {args.mode_cmd} vs --{flag_mode}")

    if cmd:
        return cmd
    if flag_mode:
        return flag_mode
    return "tui" if (sys.stdin.isatty() and sys.stdout.isatty()) else "report"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = _resolve_mode(parser, args)

    if args.interval <= 0:
        parser.error("--interval must be greater than 0")

    app_data_dir = args.app_data_dir.expanduser() if args.app_data_dir else None
    brain_dir = (
        args.brain_dir.expanduser()
        if args.brain_dir is not None
        else get_brain_dir(app_data_dir)
    )
    state_dir = (
        args.state_dir.expanduser()
        if args.state_dir is not None
        else get_state_dir(app_data_dir)
    )

    engine = MetricsEngine(brain_dir=brain_dir, state_dir=state_dir)
    try:
        convs_payload = engine.list_conversations()

        if mode == "list":
            out = render_conversation_list(
                convs_payload,
                as_json=args.as_json,
                no_color=args.no_color,
            )
            sys.stdout.write(out)
            if out and not out.endswith("\n"):
                sys.stdout.write("\n")
            return 0

        target_conv_id: str | None = None
        if args.all:
            target_conv_id = ALL_CONVERSATIONS_ID
        elif args.conversation:
            resolved_cid = resolve_conversation_id(
                convs_payload.get("conversations") or [], args.conversation
            )
            if resolved_cid is None:
                print(
                    f"[agystats] Conversation not found matching: {args.conversation!r}",
                    file=sys.stderr,
                )
                return 2
            target_conv_id = resolved_cid

        if mode == "report":
            out = render_report(
                engine,
                conversation_id=target_conv_id,
                window=args.window,
                pool=args.pool,
                model_filter=args.model,
                steps_limit=args.steps,
                as_json=args.as_json,
                no_color=args.no_color,
            )
            sys.stdout.write(out)
            if out and not out.endswith("\n"):
                sys.stdout.write("\n")
            return 0

        # Realtime TUI mode
        initial_model_filter: str | None = None
        if args.model:
            m_data = engine.get_metrics(target_conv_id, window=args.window, refresh=False)
            matched = resolve_model_entry(m_data.get("by_model") or [], args.model)
            initial_model_filter = (
                str(matched["model_name"]) if matched else args.model
            )

        tui_state = TUIState(
            view_mode="all" if args.all else "conversation",
            time_window=args.window,
            val_pool=args.pool,
            follow_active=not bool(target_conv_id and target_conv_id != ALL_CONVERSATIONS_ID),
            selected_conv_id=(
                target_conv_id
                if (target_conv_id and target_conv_id != ALL_CONVERSATIONS_ID)
                else None
            ),
            model_filter=initial_model_filter,
            show_all_steps=(args.steps < 0),
        )
        return run_tui(
            engine,
            tui_state,
            interval_s=args.interval,
            no_color=args.no_color,
        )
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
