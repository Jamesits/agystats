"""Realtime interactive terminal UI (Rich Live + keyboard navigation) for agystats."""

from __future__ import annotations

from dataclasses import dataclass
import os
import select
import shutil
import sys
import time
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .engine import ALL_CONVERSATIONS_ID, MetricsEngine, VALID_WINDOWS
from .formatters import (
    fmt_compact,
    fmt_credits,
    fmt_usd,
    resolve_model_entry,
    short_model_name,
)
from .report import (
    build_by_model_table,
    build_context_panel,
    build_kpi_grid,
    build_roofline_panel,
    build_steps_table,
    build_subagents_table,
    build_valuation_panel,
)

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - non-POSIX platforms
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]


@dataclass
class TUIState:
    view_mode: str = "conversation"  # "conversation" | "all"
    time_window: str = "all"  # "5h" | "24h" | "7d" | "30d" | "all"
    val_pool: str = "all"  # "all" | "gemini" | "partner"
    follow_active: bool = True
    selected_conv_id: str | None = None
    model_filter: str | None = None
    show_all_steps: bool = False
    show_cold_prefills: bool = False
    step_scroll: int = 0
    status_message: str = ""


def requested_conv_id(state: TUIState) -> str | None:
    if state.view_mode == "all":
        return ALL_CONVERSATIONS_ID
    return None if state.follow_active else state.selected_conv_id


def _cycle_option(options: tuple[str, ...], current: str) -> str:
    try:
        idx = options.index(current)
        return options[(idx + 1) % len(options)]
    except ValueError:
        return options[0]


def apply_tui_key(key: str, state: TUIState, snapshot: dict[str, Any]) -> bool:
    """Apply one keypress to TUIState. Returns False when the user requests exit."""
    if key in ("q", "Q", "\x03", "\x04"):
        return False

    convs_payload = snapshot.get("conversations") or {}
    conv_list = convs_payload.get("conversations") or []
    active_id = convs_payload.get("active_conversation_id")
    metrics = snapshot.get("metrics") or {}
    by_model = metrics.get("by_model") or []
    steps = metrics.get("steps") or []

    if key in ("\t", "a", "A"):
        state.view_mode = "all" if state.view_mode == "conversation" else "conversation"
        state.step_scroll = 0
        state.status_message = f"View: {'All Conversations' if state.view_mode == 'all' else 'Conversation'}"
        return True

    if key in ("[", "]"):
        if not conv_list:
            state.status_message = "No conversations available"
            return True
        ids = [str(c["conversation_id"]) for c in conv_list]
        cur_id = (
            state.selected_conv_id
            if (not state.follow_active and state.selected_conv_id in ids)
            else (active_id if active_id in ids else ids[0])
        )
        cur_idx = ids.index(cur_id)
        step_dir = -1 if key == "[" else 1
        next_id = ids[(cur_idx + step_dir) % len(ids)]
        state.view_mode = "conversation"
        state.follow_active = False
        state.selected_conv_id = next_id
        state.step_scroll = 0
        next_title = str(
            next((c.get("title") for c in conv_list if c["conversation_id"] == next_id), next_id[:8])
        )
        state.status_message = f"Selected: {next_title[:36]} ({next_id[:8]})"
        return True

    if key in ("f", "F"):
        state.follow_active = not state.follow_active
        if state.follow_active:
            state.selected_conv_id = None
            state.status_message = "Follow active: ON"
        else:
            state.selected_conv_id = active_id or (
                str(conv_list[0]["conversation_id"]) if conv_list else None
            )
            state.status_message = "Follow active: OFF"
        state.view_mode = "conversation"
        state.step_scroll = 0
        return True

    if key in ("w", "W"):
        if state.view_mode != "all":
            state.view_mode = "all"
        else:
            state.time_window = _cycle_option(VALID_WINDOWS, state.time_window)
        state.step_scroll = 0
        state.status_message = f"Window: {state.time_window}"
        return True

    if key in ("m", "M"):
        model_names: list[str | None] = [None] + [
            str(bm.get("model_name") or "") for bm in by_model if bm.get("model_name")
        ]
        if len(model_names) <= 1:
            state.model_filter = None
            state.status_message = "No models to filter"
        else:
            try:
                cur_idx = model_names.index(state.model_filter)
                state.model_filter = model_names[(cur_idx + 1) % len(model_names)]
            except ValueError:
                state.model_filter = model_names[1]
            state.step_scroll = 0
            state.status_message = (
                f"Model filter: {short_model_name(state.model_filter)}"
                if state.model_filter
                else "Model filter: All"
            )
        return True

    if key in ("c", "C"):
        state.show_cold_prefills = not state.show_cold_prefills
        state.status_message = (
            "Scatter: showing cold prefills"
            if state.show_cold_prefills
            else "Scatter: warm steps only"
        )
        return True

    if key in ("g", "G"):
        state.val_pool = _cycle_option(("all", "gemini", "partner"), state.val_pool)
        pool_lbl = {"all": "All", "gemini": "Google", "partner": "3rd-Party"}[state.val_pool]
        state.status_message = f"Quota pool: {pool_lbl}"
        return True

    if key in ("s", "S"):
        state.show_all_steps = not state.show_all_steps
        state.step_scroll = 0
        state.status_message = "Steps: expanded" if state.show_all_steps else "Steps: recent 5"
        return True

    if key in ("j", "J", "\x1b[B"):
        filtered_len = len(
            [s for s in steps if not state.model_filter or s.get("model_name") == state.model_filter]
        )
        max_scroll = max(0, filtered_len - 1)
        state.step_scroll = min(max_scroll, state.step_scroll + 1)
        return True

    if key in ("k", "K", "\x1b[A"):
        state.step_scroll = max(0, state.step_scroll - 1)
        return True

    if key in ("r", "R"):
        state.status_message = "Refreshed"
        return True

    return True


def _build_top_bar(snapshot: dict[str, Any], state: TUIState) -> Panel:
    m = snapshot.get("metrics") or {}
    convs = (snapshot.get("conversations") or {}).get("conversations") or []
    active_id = (snapshot.get("conversations") or {}).get("active_conversation_id")
    is_all = m.get("mode") == "all"

    line1 = Text()
    line1.append("AGYSTATS LIVE  ", style="bold bright_cyan")
    if not is_all:
        line1.append("[Conversation]", style="bold reverse cyan")
        line1.append(" ")
        line1.append(" All ", style="dim")
    else:
        line1.append(" Conversation ", style="dim")
        line1.append(" ")
        line1.append("[All]", style="bold reverse cyan")

    line1.append("   ")
    status = str(m.get("status") or "idle")
    gen_since = m.get("generating_since_ms")
    if status == "generating":
        elapsed = (
            max(0.0, (time.time() * 1000.0 - float(gen_since)) / 1000.0)
            if gen_since
            else 0.0
        )
        line1.append(f"● GENERATING {elapsed:.1f}s", style="bold green")
    else:
        line1.append("○ IDLE", style="dim")

    if m.get("model_name"):
        line1.append("   Model: ", style="dim")
        line1.append(short_model_name(str(m["model_name"])), style="bold cyan")

    line2 = Text()
    if is_all:
        line2.append("Window: ", style="dim")
        for w in VALID_WINDOWS:
            if w == state.time_window:
                line2.append(f"[{w}]", style="bold reverse green")
            else:
                line2.append(f" {w} ", style="dim")
            line2.append(" ")
        n_conv = int(m.get("conversations_count") or 0)
        n_sub = int(m.get("subagents_count") or 0)
        line2.append(
            f"  · {n_conv} conv{'s' if n_conv != 1 else ''}"
            + (f", {n_sub} subagent{'s' if n_sub != 1 else ''}" if n_sub else ""),
            style="dim",
        )
    else:
        cid = str(m.get("conversation_id") or "")
        title = str(m.get("title") or "No active conversation")
        is_star = cid and cid == active_id
        line2.append("Conv: ", style="dim")
        line2.append(f"{'★ ' if is_star else ''}{title}", style="bold")
        if cid:
            line2.append(f" ({cid[:8]})", style="dim")
        line2.append(
            f"   Follow: {'ON' if state.follow_active else 'OFF'}",
            style="green" if state.follow_active else "yellow",
        )
        if convs:
            line2.append(f"   [{len(convs)} total]", style="dim")

    return Panel(Group(line1, line2), box=box.ROUNDED, border_style="cyan")


def _build_footer(state: TUIState) -> Text:
    foot = Text()
    bindings = [
        ("q", "Quit"),
        ("Tab/a", "Mode"),
        ("[/]", "Conv"),
        ("f", "Follow"),
        ("w", "Window"),
        ("m", "Model"),
        ("g", "Pool"),
        ("s", "Steps"),
        ("j/k", "Scroll"),
        ("r", "Refresh"),
    ]
    for idx, (k, desc) in enumerate(bindings):
        if idx > 0:
            foot.append("  ", style="dim")
        foot.append(k, style="bold cyan")
        foot.append(f":{desc}", style="dim")
    if state.status_message:
        foot.append("   | ", style="dim")
        foot.append(state.status_message, style="yellow")
    return foot


def build_tui_renderable(
    snapshot: dict[str, Any],
    state: TUIState,
    *,
    width: int = 110,
    height: int = 38,
) -> Group:
    """Build one complete Rich renderable frame for the realtime TUI."""
    m = snapshot.get("metrics") or {}
    is_all = m.get("mode") == "all"
    by_model = m.get("by_model") or []
    selected_model_entry = resolve_model_entry(by_model, state.model_filter)
    active_model_filter = (
        selected_model_entry["model_name"] if selected_model_entry else None
    )

    sections: list[Any] = [_build_top_bar(snapshot, state)]

    if not is_all:
        sections.append(build_context_panel(m, bar_width=max(24, min(64, width - 28))))

    sections.append(build_kpi_grid(m, selected_model_entry))
    sections.append(
        build_by_model_table(
            by_model, m.get("totals") or {}, model_filter=active_model_filter
        )
    )

    if selected_model_entry is not None:
        sections.append(
            build_roofline_panel(
                selected_model_entry,
                m.get("steps") or [],
                width=max(28, min(64, width - 18)),
                show_cold=state.show_cold_prefills,
            )
        )

    sections.append(build_valuation_panel(m, selected_model_entry, val_pool=state.val_pool))

    if not is_all:
        subs = m.get("subagents") or {}
        sub_count = int(subs.get("count") or 0)
        if sub_count > 0:
            if height >= 42:
                sections.append(build_subagents_table(m))
            else:
                sub_tot = (subs.get("totals") or {}).get("total_tokens") or 0
                sub_val = subs.get("valuation") or {}
                sub_cost = (
                    f" · {fmt_usd(sub_val.get('cost_usd'))} ({fmt_credits(sub_val.get('credits'))})"
                    if (sub_val.get("coverage") or {}).get("priced_steps", 0) > 0
                    else ""
                )
                sub_line = Text()
                sub_line.append(
                    f"Subagents ({sub_count}): {fmt_compact(sub_tot)} tok{sub_cost} · Grand Total: {fmt_compact((m.get('grand_totals') or {}).get('total_tokens'))} tok",
                    style="cyan",
                )
                sections.append(Panel(sub_line, box=box.SIMPLE))

        # Fit step rows to remaining vertical space
        approx_used = 24 + len(by_model) + (12 if selected_model_entry else 0)
        avail_rows = max(3, height - approx_used)
        step_limit = avail_rows if state.show_all_steps else min(5, avail_rows)
        sections.append(
            build_steps_table(
                m.get("steps") or [],
                model_filter=active_model_filter,
                limit=step_limit,
                offset=state.step_scroll,
            )
        )

    sections.append(_build_footer(state))
    return Group(*sections)


def _read_key_nonblocking(fd: int, timeout_s: float) -> str | None:
    rlist, _, _ = select.select([fd], [], [], max(0.0, timeout_s))
    if not rlist:
        return None
    raw = os.read(fd, 1)
    if not raw:
        return None
    if raw == b"\x1b":
        # Check for ANSI arrow escape sequence
        r2, _, _ = select.select([fd], [], [], 0.02)
        if r2:
            seq = os.read(fd, 2)
            return ("\x1b" + seq.decode("latin-1", errors="ignore"))
    return raw.decode("latin-1", errors="ignore")


def run_tui(
    engine: MetricsEngine,
    state: TUIState | None = None,
    *,
    interval_s: float = 0.25,
    no_color: bool = False,
    max_ticks: int | None = None,
    console: Console | None = None,
) -> int:
    """Run the realtime Rich Live TUI loop."""
    tui_state = state or TUIState()
    if console is None:
        console = Console(
            no_color=no_color,
            color_system=None if no_color else "auto",
            legacy_windows=False,
        )

    use_raw_tty = (
        max_ticks is None
        and termios is not None
        and tty is not None
        and sys.stdin.isatty()
        and console.is_terminal
    )

    stdin_fd: int | None = None
    old_termios: Any = None
    if use_raw_tty:
        stdin_fd = sys.stdin.fileno()
        assert termios is not None and tty is not None
        old_termios = termios.tcgetattr(stdin_fd)
        tty.setcbreak(stdin_fd)

    try:
        engine.refresh_if_changed(target_id=requested_conv_id(tui_state), fast_poll=True)
        snap = engine.get_snapshot(
            requested_conv_id(tui_state),
            window=tui_state.time_window,
            refresh=False,
        )
        term_sz = shutil.get_terminal_size((110, 38))
        use_alt_screen = max_ticks is None and console.is_terminal

        with Live(
            build_tui_renderable(
                snap,
                tui_state,
                width=console.width or term_sz.columns,
                height=console.height or term_sz.lines,
            ),
            console=console,
            screen=use_alt_screen,
            auto_refresh=False,
            transient=use_alt_screen,
        ) as live:
            ticks = 0
            while True:
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break

                poll_wait = 0.1 if engine.is_generating() else max(0.05, float(interval_s))
                dirty = False

                if stdin_fd is not None:
                    deadline = time.monotonic() + poll_wait
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        key = _read_key_nonblocking(stdin_fd, min(0.05, remaining))
                        if key is not None:
                            if not apply_tui_key(key, tui_state, snap):
                                return 0
                            dirty = True
                            break
                else:
                    time.sleep(min(0.05, poll_wait) if max_ticks is not None else poll_wait)

                if engine.refresh_if_changed(
                    target_id=requested_conv_id(tui_state), fast_poll=True
                ):
                    dirty = True

                # Periodic re-render while generating so elapsed timer ticks smoothly
                if dirty or engine.is_generating():
                    snap = engine.get_snapshot(
                        requested_conv_id(tui_state),
                        window=tui_state.time_window,
                        refresh=False,
                    )
                    cur_sz = shutil.get_terminal_size((110, 38))
                    live.update(
                        build_tui_renderable(
                            snap,
                            tui_state,
                            width=console.width or cur_sz.columns,
                            height=console.height or cur_sz.lines,
                        ),
                        refresh=True,
                    )
    except KeyboardInterrupt:
        return 0
    finally:
        if stdin_fd is not None and old_termios is not None and termios is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_termios)
            except Exception:
                pass

    return 0
