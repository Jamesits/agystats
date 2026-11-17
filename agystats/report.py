"""Report mode text/Rich/JSON renderer for agystats."""

from __future__ import annotations

import io
import json
import shutil
import time
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .engine import ALL_CONVERSATIONS_ID, MetricsEngine
from .formatters import (
    build_braille_scatter,
    extract_valuation_cards,
    fmt_approx_duration,
    fmt_approx_sec,
    fmt_cold_time_note,
    fmt_compact,
    fmt_credits,
    fmt_duration,
    fmt_int,
    fmt_nom_tps,
    fmt_short_delay,
    fmt_usd,
    fmt_wasted_summary,
    is_aggregate_mode,
    resolve_model_entry,
    short_model_name,
    src_label,
    strip_subagent_prefix,
)


def _new_console(*, no_color: bool = False, width: int | None = None) -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    eff_width = width or max(100, shutil.get_terminal_size((120, 40)).columns)
    console = Console(
        file=buf,
        no_color=no_color,
        color_system=None if no_color else "auto",
        width=eff_width,
        legacy_windows=False,
        soft_wrap=True,
    )
    return console, buf


def render_conversation_list(
    conversations_payload: dict[str, Any],
    *,
    as_json: bool = False,
    no_color: bool = False,
    width: int | None = None,
) -> str:
    """Render discovered conversations as JSON or a formatted text table."""
    if as_json:
        return json.dumps(conversations_payload, indent=2)

    console, buf = _new_console(no_color=no_color, width=width)
    active_id = conversations_payload.get("active_conversation_id")
    convs = conversations_payload.get("conversations") or []

    table = Table(
        title="Antigravity CLI Conversations",
        box=box.ROUNDED,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Project", max_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("Title", min_width=20, overflow="fold")
    table.add_column("Model", max_width=20, no_wrap=True, overflow="ellipsis")
    table.add_column("Steps", justify="right", no_wrap=True)
    table.add_column("Context", justify="right", no_wrap=True)
    table.add_column("Status", no_wrap=True)

    if not convs:
        table.add_row("—", "—", "No conversations found", "—", "0", "0", "idle")
    else:
        for c in convs:
            cid = str(c.get("conversation_id") or "")
            is_active = cid == active_id
            is_sub = bool(c.get("is_subagent"))
            depth = max(1, int(c.get("nesting_depth") or 1)) if is_sub else 0
            if is_sub:
                prefix = ("  " * max(0, depth - 1)) + ("★ ↳ " if is_active else "↳ ")
            else:
                prefix = "★ " if is_active else "  "
            title = str(c.get("title") or cid[:8])
            agent_name = str(c.get("agent_name") or "").strip()
            if is_sub and agent_name and agent_name.lower() not in title.lower():
                title = f"{title} · {agent_name}"
            project_name = str(c.get("project_name") or "No Project")
            status = str(c.get("status") or "idle")
            status_txt = Text(status.upper(), style="bold green" if status == "generating" else "dim")
            table.add_row(
                f"{prefix}{cid[:8]}",
                project_name,
                title,
                short_model_name(str(c.get("model_name") or "unknown")),
                fmt_int(c.get("planner_steps_count")),
                fmt_compact(c.get("current_tokens")),
                status_txt,
            )

    console.print(table)
    return buf.getvalue()


def build_context_panel(m: dict[str, Any], bar_width: int = 56) -> Panel:
    """Build the Active Context Window Rich panel."""
    ctx = m.get("context") or {}
    brk = ctx.get("breakdown") or {}
    cur = int(ctx.get("current_tokens") or 0)
    cache_read = int(ctx.get("cache_read_tokens") or 0)
    hit_pct = float(ctx.get("cache_hit_pct") or 0.0)

    head = Text()
    head.append(f"{fmt_compact(cur)} tok", style="bold white")
    head.append(f"  ({fmt_int(cur)})  ", style="dim")
    head.append(
        f"{hit_pct:g}% cached ({fmt_compact(cache_read)} / {fmt_compact(cur)})",
        style="cyan",
    )
    if ctx.get("cold_cache_spike"):
        lbl = "Model Switch Prefill" if ctx.get("model_switched") else "Cold Prefill"
        head.append(f"  [❄ {lbl}]", style="bold yellow")
    if ctx.get("compacted"):
        head.append("  [⇄ Compacted]", style="bold magenta")

    show_summary = int(brk.get("summary_tokens") or 0) > 0 or int(
        (m.get("totals") or {}).get("compacted_count") or 0
    ) > 0
    segs = [
        ("System & Tools", int(brk.get("system_tokens") or 0), "blue", False),
        ("Compacted Summary", int(brk.get("summary_tokens") or 0), "magenta", not show_summary),
        ("Tool Outputs", int(brk.get("tool_tokens") or 0), "yellow", False),
        ("Model", int(brk.get("model_tokens") or 0), "green", False),
        ("User", int(brk.get("user_tokens") or 0), "cyan", False),
    ]

    bar = Text()
    if cur > 0:
        used_chars = 0
        non_empty = [(lbl, val, col) for lbl, val, col, hidden in segs if not (hidden and val == 0) and val > 0]
        for idx, (_, val, col) in enumerate(non_empty):
            if idx == len(non_empty) - 1:
                chars = max(1, bar_width - used_chars)
            else:
                chars = max(1, round((val / cur) * bar_width)) if val > 0 else 0
                chars = min(chars, max(0, bar_width - used_chars))
            used_chars += chars
            if chars > 0:
                bar.append("█" * chars, style=col)
        if used_chars < bar_width:
            bar.append("░" * (bar_width - used_chars), style="dim")
    else:
        bar.append("░" * bar_width, style="dim")

    legend = Text()
    for lbl, val, col, hidden in segs:
        if hidden and val == 0:
            continue
        pct = (val / cur * 100.0) if cur > 0 else 0.0
        legend.append("■ ", style=col)
        legend.append(f"{lbl}: ", style="dim")
        legend.append(f"{fmt_compact(val)} ({pct:.1f}%)   ", style="bold")

    items: list[Any] = [head, bar, legend]

    top_tools = ctx.get("top_tools") or []
    if top_tools:
        tools_line = Text()
        tools_line.append("Top Tools: ", style="dim")
        for idx, t in enumerate(top_tools):
            if idx > 0:
                tools_line.append(" · ", style="dim")
            tools_line.append(
                f"{t['name']} {fmt_compact(t['tokens'])} ({t['pct']}%)",
                style="yellow",
            )
        items.append(tools_line)

    return Panel(
        Group(*items),
        title="Active Context Window",
        box=box.ROUNDED,
        border_style="blue",
    )


def build_kpi_grid(
    m: dict[str, Any],
    selected_model_entry: dict[str, Any] | None,
) -> Table:
    """Build the 4-column KPI summary row (Input, Output, Latest Step, Timing)."""
    mode = m.get("mode")
    is_all_mode = is_aggregate_mode(mode)
    global_totals = m.get("totals") or {}
    by_model = m.get("by_model") or []

    scoped_tot = selected_model_entry or global_totals
    scoped_last = (
        selected_model_entry.get("latest_step")
        if selected_model_entry
        else m.get("latest_step")
    )
    scoped_steps_count = (
        selected_model_entry.get("steps_count")
        if selected_model_entry
        else (m.get("planner_steps_count") or 0)
    )
    scope_tag = (
        f" · {short_model_name(selected_model_entry['model_name'])}"
        if selected_model_entry
        else ""
    )

    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in range(4):
        grid.add_column(ratio=1)

    in_lines = Text()
    in_lines.append(f"{fmt_compact(scoped_tot.get('gross_input_tokens'))}\n", style="bold bright_white")
    in_lines.append(f"Uncached:    {fmt_int(scoped_tot.get('input_tokens'))}\n", style="dim")
    in_lines.append(f"Cached Read: {fmt_int(scoped_tot.get('cache_read_tokens'))}\n", style="dim")
    in_lines.append(f"Cache Hit:   {scoped_tot.get('cache_hit_pct', 0)}%", style="cyan")

    out_lines = Text()
    out_lines.append(f"{fmt_compact(scoped_tot.get('output_tokens'))}\n", style="bold bright_white")
    out_lines.append(f"~Thinking:   {fmt_int(scoped_tot.get('est_thinking_tokens'))}\n", style="dim")
    out_lines.append(f"~Visible:    {fmt_int(scoped_tot.get('est_visible_tokens'))}", style="dim")

    step_tag = f" #{scoped_last['ordinal']}" if scoped_last else ""
    last_lines = Text()
    if not scoped_last:
        last_lines.append("—\n", style="bold")
        last_lines.append("No steps yet", style="dim")
    else:
        dur_s = scoped_last.get("duration_s")
        dur_txt = f"{dur_s}s" if dur_s is not None else "—"
        src_txt = f" ({src_label(scoped_last.get('duration_source'))})" if dur_s is not None else ""
        last_lines.append(f"{dur_txt}{src_txt}\n", style="bold bright_white")
        est_d = scoped_last.get("est_delay_s")
        retry_s = float(scoped_last.get("retry_wait_s") or 0.0)
        if est_d is not None:
            comb = float(est_d) + retry_s
            delay_txt = f"~{comb:.2f}s" + (" (↻)" if retry_s > 0 else "")
        elif retry_s > 0:
            delay_txt = f"+{fmt_duration(retry_s)} (↻)"
        elif scoped_last.get("cold_cache_spike"):
            delay_txt = "— (❄ cold)"
        else:
            delay_txt = "—"
        last_lines.append(f"~Est. Delay: {delay_txt}\n", style="dim")
        last_lines.append(f"Step Output: {fmt_int(scoped_last.get('output_tokens'))} tok", style="dim")
        if scoped_last.get("effective_tps") is not None:
            last_lines.append(f" ({scoped_last['effective_tps']} tok/s)", style="cyan")

    time_lines = Text()
    model_t = float(scoped_tot.get("model_time_s") or 0.0)
    time_lines.append(f"{fmt_duration(model_t)}\n", style="bold bright_white")
    if scoped_tot.get("est_total_delay_s") is not None:
        tot_d = float(scoped_tot["est_total_delay_s"])
        wasted_s = float(scoped_tot.get("wasted_time_s") or 0.0)
        partial = (
            int(scoped_tot.get("est_delay_steps_count") or 0)
            < int(scoped_tot.get("warm_timed_steps_count") or 0)
            or (int(scoped_tot.get("est_delay_steps_count") or 0) == 0 and wasted_s > 0)
        )
        time_lines.append(
            f"~Est. Delay: {fmt_approx_duration(tot_d)}{'*' if partial else ''}\n",
            style="dim",
        )
    else:
        time_lines.append("~Est. Delay: —\n", style="dim")
    wasted_s = float(scoped_tot.get("wasted_time_s") or 0.0)
    if wasted_s > 0:
        w_sum = fmt_wasted_summary(scoped_tot)
        time_lines.append(
            f"Retry/Fail:  {fmt_duration(wasted_s)}{f' ({w_sum})' if w_sum else ''}\n",
            style="yellow",
        )
    time_lines.append(f"Tool Exec:   {fmt_duration(scoped_tot.get('tool_time_s') or 0.0)}\n", style="dim")
    step_lbl = "Total Steps" if is_all_mode else "Planner Steps"
    time_lines.append(f"{step_lbl}: {fmt_int(scoped_steps_count)}", style="dim")

    timing_title_base = (
        "Model"
        if selected_model_entry
        else ("Project" if mode == "project" else ("Total" if is_all_mode else "Session"))
    )
    grid.add_row(
        Panel(in_lines, title=f"Input Tokens{scope_tag}", box=box.ROUNDED),
        Panel(out_lines, title=f"Output Tokens{scope_tag}", box=box.ROUNDED),
        Panel(last_lines, title=f"Latest Step{step_tag}{scope_tag}", box=box.ROUNDED),
        Panel(
            time_lines,
            title=f"{timing_title_base} Timing{scope_tag}",
            box=box.ROUNDED,
        ),
    )
    return grid


def build_by_model_table(
    by_model: list[dict[str, Any]],
    totals: dict[str, Any],
    *,
    model_filter: str | None = None,
) -> Panel:
    """Build the Per-Model Token, Timing, and Roofline panel."""
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style="bold cyan",
        expand=True,
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("Model", max_width=24, no_wrap=True, overflow="ellipsis")
    table.add_column("Stp", justify="right", no_wrap=True)
    table.add_column("Input (Hit%)", justify="right")
    table.add_column("Out (~Thk)", justify="right")
    table.add_column("Time (~Dly)", justify="right")
    table.add_column("~TPS", justify="right", no_wrap=True)
    table.add_column("~TTFT (+Dly)", justify="right")
    table.add_column("Paygo", justify="right", no_wrap=True)

    if not by_model:
        table.add_row("No planner steps yet", "0", "0", "0", "0.0s", "—", "—", "—")
        return Panel(table, title="Per-Model Token, Timing & Roofline", box=box.ROUNDED)

    for bm in by_model:
        name = str(bm.get("model_name") or "unknown")
        is_sel = model_filter == name
        star = " ★" if bm.get("is_active") else ""
        label_txt = Text(
            ("> " if is_sel else "") + short_model_name(name) + star,
            style="bold bright_cyan" if is_sel else ("bold" if bm.get("is_active") else ""),
        )

        fit = bm.get("latency_fit") or {}
        if fit.get("available"):
            nom_tps = f"~{fmt_nom_tps(fit['inferred_decode_tps'])}"
            if fit.get("avg_delay_s") is not None:
                ttft_delay = f"{fmt_approx_sec(fit['inferred_ttft_s'])} ({fmt_short_delay(fit['avg_delay_s'])})"
            else:
                ttft_delay = f"{fmt_approx_sec(fit['inferred_ttft_s'])} (—)"
        else:
            reason = fit.get("reason")
            if reason == "min_steps":
                nom_tps = f"— ({int(fit.get('warm_count') or 0)}/4)"
            elif reason == "min_spread":
                nom_tps = "— (spread)"
            else:
                nom_tps = "—"
            ttft_delay = "—"

        delay_str = ""
        if bm.get("est_total_delay_s") is not None:
            w_s = float(bm.get("wasted_time_s") or 0.0)
            partial = (
                int(bm.get("est_delay_steps_count") or 0) < int(bm.get("warm_timed_steps_count") or 0)
                or (int(bm.get("est_delay_steps_count") or 0) == 0 and w_s > 0)
            )
            delay_str = f" ({fmt_approx_duration(bm['est_total_delay_s'])}{'*' if partial else ''})"
        time_cell = f"{fmt_duration(bm.get('model_time_s') or 0.0)}{delay_str}"

        bm_val = bm.get("valuation") or {}
        paygo_cell = (
            fmt_usd(bm_val.get("cost_usd"))
            if bm_val.get("status") == "priced"
            else "—"
        )

        table.add_row(
            label_txt,
            fmt_int(bm.get("steps_count")),
            f"{fmt_compact(bm.get('gross_input_tokens'))} ({bm.get('cache_hit_pct', 0)}%)",
            f"{fmt_compact(bm.get('output_tokens'))} (~{fmt_compact(bm.get('est_thinking_tokens'))})",
            time_cell,
            nom_tps,
            ttft_delay,
            paygo_cell,
        )

    if len(by_model) > 1:
        tot_delay_str = ""
        if totals.get("est_total_delay_s") is not None:
            w_s = float(totals.get("wasted_time_s") or 0.0)
            partial = (
                int(totals.get("est_delay_steps_count") or 0) < int(totals.get("warm_timed_steps_count") or 0)
                or (int(totals.get("est_delay_steps_count") or 0) == 0 and w_s > 0)
            )
            tot_delay_str = f" ({fmt_approx_duration(totals['est_total_delay_s'])}{'*' if partial else ''})"
        tot_ttft = (
            f"— ({fmt_short_delay(totals['avg_delay_s'])})"
            if totals.get("avg_delay_s") is not None
            else "—"
        )
        table.add_section()
        table.add_row(
            Text("All Models", style="bold"),
            fmt_int(totals.get("steps_count")),
            f"{fmt_compact(totals.get('gross_input_tokens'))} ({totals.get('cache_hit_pct', 0)}%)",
            f"{fmt_compact(totals.get('output_tokens'))} (~{fmt_compact(totals.get('est_thinking_tokens'))})",
            f"{fmt_duration(totals.get('model_time_s') or 0.0)}{tot_delay_str}",
            "—",
            tot_ttft,
            "",
        )
    return Panel(table, title="Per-Model Token, Timing & Roofline", box=box.ROUNDED)


def build_valuation_panel(
    m: dict[str, Any],
    selected_model_entry: dict[str, Any] | None,
    val_pool: str = "all",
) -> Panel:
    """Build the Paygo & AI Credits panel."""
    cards, callouts, eff_pool = extract_valuation_cards(
        m, selected_model_entry, val_pool=val_pool
    )
    pool_labels = {"all": "All Pools", "gemini": "Google Pool", "partner": "3rd-Party Pool"}
    val = m.get("valuation") or {}
    cpu = float(val.get("credits_per_usd") or 100.0)
    cpu_lbl = str(int(cpu)) if cpu.is_integer() else f"{cpu:.4g}"

    title = (
        f"Paygo & AI Credits · {short_model_name(selected_model_entry['model_name'])}"
        if selected_model_entry
        else f"Paygo & AI Credits · {pool_labels.get(eff_pool, eff_pool)} ({cpu_lbl} cr/$1)"
    )

    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in cards:
        grid.add_column(ratio=1)

    card_panels: list[Panel] = []
    for c in cards:
        txt = Text()
        txt.append(f"{c['cost_text']}", style="bold bright_white")
        if c["credits_text"]:
            txt.append(f" · {c['credits_text']}", style="cyan")
        txt.append(f"  ({fmt_int(c['steps_count'])} steps)\n", style="dim")
        if c["has_future"]:
            txt.append(f"{c['future_lbl']}: {c['future_text']}\n", style="yellow")
        txt.append(f"Uncached In: {c['uncached_text']}  ", style="dim")
        txt.append(f"Cached: {c['cache_read_text']}  ", style="dim")
        txt.append(f"Output: {c['output_text']}", style="dim")
        card_panels.append(Panel(txt, title=c["label"], box=box.SIMPLE))

    grid.add_row(*card_panels)

    body_items: list[Any] = [grid]
    for kind, left, right in callouts:
        line = Text()
        if kind == "orch":
            line.append("★ ", style="bold green")
            line.append(f"{left}: ", style="green")
            line.append(right, style="bold green")
        elif kind == "warn":
            line.append("⚠ ", style="bold yellow")
            line.append(f"{left}: ", style="yellow")
            line.append(right, style="dim")
        else:
            line.append("• ", style="cyan")
            line.append(f"{left}: ", style="dim")
            line.append(right, style="bold")
        body_items.append(line)

    return Panel(Group(*body_items), title=title, box=box.ROUNDED, border_style="green")


def build_roofline_panel(
    selected_model_entry: dict[str, Any],
    all_steps: list[dict[str, Any]],
    *,
    width: int = 60,
    show_cold: bool = False,
) -> Panel:
    """Build the Latency Roofline Fit & Scatter panel for a single model."""
    m_name = str(selected_model_entry.get("model_name") or "unknown")
    fit = selected_model_entry.get("latency_fit") or {}
    scatter_steps = selected_model_entry.get("scatter_steps")
    if not isinstance(scatter_steps, list):
        scatter_steps = [
            s
            for s in all_steps
            if s.get("model_name") == m_name
            and s.get("step_kind") != "checkpoint"
            and s.get("duration_s") is not None
            and float(s["duration_s"]) > 0
        ][-100:]

    kpi_line = Text()
    if fit.get("available"):
        q_lbl = f"~P{fit['quantile_pct']}" if (fit.get("quantile_pct") or 95) < 95 else "P95"
        kpi_line.append(f"~Min TTFT ({q_lbl}): ", style="dim")
        kpi_line.append(f"~{float(fit['inferred_ttft_s']):.2f}s   ", style="bold cyan")
        kpi_line.append(f"~Nominal TPS ({q_lbl}): ", style="dim")
        kpi_line.append(f"~{float(fit['inferred_decode_tps']):.1f} tok/s   ", style="bold green")
        if fit.get("avg_delay_s") is not None:
            kpi_line.append("Avg Delay: ", style="dim")
            kpi_line.append(f"+{float(fit['avg_delay_s']):.2f}s   ", style="bold yellow")
        if fit.get("median_delay_s") is not None:
            kpi_line.append("P50 Delay: ", style="dim")
            kpi_line.append(f"+{float(fit['median_delay_s']):.2f}s   ", style="yellow")
        kpi_line.append(
            f"(n={fit.get('warm_count', 0)} warm"
            + (f", {fit['cold_excluded_count']} cold excl." if fit.get("cold_excluded_count") else "")
            + ")",
            style="dim",
        )
    else:
        reason = fit.get("reason")
        if reason == "min_steps":
            kpi_line.append(
                f"Needs ≥4 warm timed steps for roofline fit ({fit.get('warm_count', 0)}/4 recorded).",
                style="dim",
            )
        elif reason == "min_spread":
            kpi_line.append(
                f"Needs ≥200 tok output spread (current: {fmt_int(fit.get('token_spread'))} tok).",
                style="dim",
            )
        else:
            kpi_line.append("Latency roofline not yet available for this model.", style="dim")

    plot = build_braille_scatter(scatter_steps, fit, width=width, height=9, show_cold=show_cold)
    legend = Text(
        "Legend: ",
        style="dim",
    )
    legend.append("● Warm  ", style="bold cyan")
    legend.append("● Latest  ", style="bold bright_magenta")
    if fit.get("available"):
        legend.append("┈ Roofline  ", style="bright_green")
    if show_cold:
        legend.append("◆ Cold (excl.)", style="bold yellow")

    return Panel(
        Group(kpi_line, plot, legend),
        title=f"Output vs. Time Roofline · {short_model_name(m_name)}",
        box=box.ROUNDED,
        border_style="cyan",
    )


def build_subagents_table(m: dict[str, Any]) -> Panel:
    """Build the Subagent Rollup panel."""
    subs = m.get("subagents") or {}
    grand = m.get("grand_totals") or {}
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style="bold cyan",
        expand=True,
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("Role", overflow="fold")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Model", max_width=22, no_wrap=True, overflow="ellipsis")
    table.add_column("Stp", justify="right", no_wrap=True)
    table.add_column("Input", justify="right", no_wrap=True)
    table.add_column("Output", justify="right", no_wrap=True)
    table.add_column("Paygo", justify="right", no_wrap=True)

    for it in subs.get("items") or []:
        in_tok = int(it.get("input_tokens") or 0) + int(it.get("cache_read_tokens") or 0)
        cost_txt = (
            f"{fmt_usd(it['cost_usd'])} ({fmt_credits(it.get('credits'))})"
            if it.get("cost_usd") is not None
            else "—"
        )
        role_txt = str(it.get("role") or "Subagent")
        agent_name = str(it.get("agent_name") or "").strip()
        if agent_name and agent_name.lower() not in role_txt.lower():
            role_txt = f"{role_txt} · {agent_name}"
        clean_sub_model = strip_subagent_prefix(str(it.get("model_name") or "unknown")) or "unknown"
        table.add_row(
            role_txt,
            str(it.get("conversation_id") or "")[:8],
            short_model_name(clean_sub_model),
            fmt_int(it.get("planner_steps_count")),
            fmt_compact(in_tok),
            fmt_compact(it.get("output_tokens")),
            cost_txt,
        )
    return Panel(
        table,
        title=f"Subagent Rollup ({subs.get('count', 0)} subagent(s) · Grand Total: {fmt_int(grand.get('total_tokens'))} tok)",
        box=box.ROUNDED,
    )


def build_steps_table(
    steps: list[dict[str, Any]],
    *,
    model_filter: str | None = None,
    limit: int = 15,
    offset: int = 0,
) -> Panel:
    """Build the Per-Step Telemetry panel (most recent first)."""
    filtered = (
        [s for s in steps if s.get("model_name") == model_filter]
        if model_filter
        else list(steps)
    )
    ordered = list(reversed(filtered))
    total_n = len(ordered)
    if limit >= 0:
        visible = ordered[offset : offset + limit]
    else:
        visible = ordered[offset:]

    scope = f"Filtered: {short_model_name(model_filter)}" if model_filter else "All Models"
    shown_note = (
        f"showing {len(visible)} of {total_n}"
        if len(visible) < total_n
        else f"{total_n} steps"
    )
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style="bold cyan",
        expand=True,
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("#", no_wrap=True)
    table.add_column("Model", max_width=20, no_wrap=True, overflow="ellipsis")
    table.add_column("Ctx (Cache%)", justify="right")
    table.add_column("Out (~Thk)", justify="right")
    table.add_column("Time (~Delay)", justify="right")
    table.add_column("Paygo", justify="right", no_wrap=True)
    table.add_column("Tools", overflow="fold")

    if not visible:
        table.add_row("—", "No steps recorded yet", "—", "—", "—", "—", "")
        return Panel(table, title=f"Per-Step Telemetry ({scope} · {shown_note})", box=box.ROUNDED)

    for s in visible:
        is_ckpt = s.get("step_kind") == "checkpoint"
        flags = ""
        if s.get("cold_cache_spike"):
            flags += "❄"
        if is_ckpt or s.get("compacted"):
            flags += "⇄"
        if float(s.get("retry_wait_s") or 0) > 0 or int(s.get("retry_count") or 0) > 0:
            flags += "↻"
        ord_cell = f"#{s.get('ordinal')}" + (f" {flags}" if flags else "")

        switch_prefix = "↱ " if s.get("model_switched") else ""
        model_cell = switch_prefix + short_model_name(str(s.get("model_name") or "unknown"))

        ctx_cell = f"{fmt_compact(s.get('total_context'))} ({s.get('cache_hit_pct', 0)}%)"
        out_cell = f"{fmt_int(s.get('output_tokens'))} (~{fmt_int(s.get('est_thinking_tokens'))})"

        dur_s = s.get("duration_s")
        retry_s = float(s.get("retry_wait_s") or 0.0)
        if is_ckpt:
            dur_cell = "—"
        elif dur_s is None:
            dur_cell = f"— (+{fmt_duration(retry_s)})" if retry_s > 0 else "—"
        else:
            inf_s = float(dur_s)
            total_wait_s = inf_s + retry_s
            est_delay = s.get("est_delay_s")
            if est_delay is not None:
                dur_cell = f"{fmt_duration(total_wait_s)} ({fmt_approx_duration(float(est_delay) + retry_s)})"
            elif retry_s > 0:
                dur_cell = f"{fmt_duration(total_wait_s)} ({fmt_approx_duration(retry_s)}*)"
            else:
                dur_cell = fmt_duration(inf_s)

        pricing_status = s.get("pricing_status")
        cost_usd = s.get("cost_usd")
        if (pricing_status in (None, "priced")) and cost_usd is not None:
            cost_cell = fmt_usd(cost_usd)
        else:
            cost_cell = "—"

        tools = s.get("tool_names") or []
        if is_ckpt:
            tools_cell = "⇄ compaction"
        elif tools:
            tools_cell = ", ".join(str(t) for t in tools)
        else:
            tools_cell = "reply"

        table.add_row(
            ord_cell,
            model_cell,
            ctx_cell,
            out_cell,
            dur_cell,
            cost_cell,
            tools_cell,
        )

    return Panel(table, title=f"Per-Step Telemetry ({scope} · {shown_note})", box=box.ROUNDED)


def build_header_panel(m: dict[str, Any]) -> Panel:
    """Build the top header summary panel."""
    mode = m.get("mode")
    is_agg = is_aggregate_mode(mode)
    status = str(m.get("status") or "idle")
    gen_since = m.get("generating_since_ms")

    head = Text()
    head.append("AGYSTATS", style="bold bright_cyan")
    head.append(" · Antigravity CLI Telemetry   ", style="dim")

    if status == "generating":
        elapsed = (
            max(0.0, (time.time() * 1000.0 - float(gen_since)) / 1000.0)
            if gen_since
            else 0.0
        )
        head.append(f"● GENERATING {elapsed:.1f}s", style="bold green")
    else:
        head.append("○ IDLE", style="dim")

    sub = Text()
    if is_agg:
        win = str(m.get("window") or "all")
        n_conv = int(m.get("conversations_count") or 0)
        n_sub = int(m.get("subagents_count") or 0)
        if mode == "project":
            proj_title = str(m.get("title") or m.get("project_name") or "Project")
            sub.append(f"Scope: Project · {proj_title} ({win})  ·  ", style="bold")
        else:
            sub.append(f"Scope: All Conversations ({win})  ·  ", style="bold")
        sub.append(
            f"{n_conv} conv{'s' if n_conv != 1 else ''}"
            + (f" · {n_sub} subagent{'s' if n_sub != 1 else ''}" if n_sub else ""),
            style="dim",
        )
    else:
        cid = str(m.get("conversation_id") or "")
        title = str(m.get("title") or "No active conversation")
        sub.append(f"Conversation: {title}", style="bold")
        if cid:
            sub.append(f" ({cid[:8]})", style="dim")
        if m.get("project_name"):
            sub.append(f"   Project: {m['project_name']}", style="dim")
        if m.get("is_subagent") and m.get("parent_id"):
            p_title = str(m.get("parent_title") or str(m["parent_id"])[:8])
            sub.append(f"   ↑ Parent: {p_title}", style="cyan")

    if m.get("model_name"):
        sub.append("   Model: ", style="dim")
        sub.append(short_model_name(str(m["model_name"])), style="bold cyan")

    totals = m.get("totals") or {}
    cold_note = fmt_cold_time_note(totals)
    if cold_note:
        sub.append(cold_note, style="dim")

    return Panel(Group(head, sub), box=box.ROUNDED, border_style="cyan")


def render_report(
    engine: MetricsEngine,
    *,
    conversation_id: str | None = None,
    window: str = "all",
    pool: str = "all",
    model_filter: str | None = None,
    steps_limit: int = 15,
    as_json: bool = False,
    no_color: bool = False,
    width: int | None = None,
) -> str:
    """Render a full text/Rich or JSON telemetry report."""
    req_conv = ALL_CONVERSATIONS_ID if conversation_id == ALL_CONVERSATIONS_ID else conversation_id
    snap = engine.get_snapshot(req_conv, window=window)
    if as_json:
        return json.dumps(snap, indent=2)

    m = snap.get("metrics") or {}
    is_all = is_aggregate_mode(m.get("mode"))
    by_model = m.get("by_model") or []
    selected_model_entry = resolve_model_entry(by_model, model_filter)
    active_model_filter = (
        selected_model_entry["model_name"] if selected_model_entry else None
    )

    console, buf = _new_console(no_color=no_color, width=width)
    eff_width = console.width

    console.print(build_header_panel(m))
    if not is_all:
        console.print(build_context_panel(m, bar_width=max(24, min(64, eff_width - 28))))

    console.print(build_kpi_grid(m, selected_model_entry))
    console.print(
        build_by_model_table(
            by_model, m.get("totals") or {}, model_filter=active_model_filter
        )
    )

    if selected_model_entry is not None:
        console.print(
            build_roofline_panel(
                selected_model_entry,
                m.get("steps") or [],
                width=max(30, min(68, eff_width - 16)),
            )
        )

    console.print(build_valuation_panel(m, selected_model_entry, val_pool=pool))

    if not is_all:
        subs = m.get("subagents") or {}
        if int(subs.get("count") or 0) > 0:
            console.print(build_subagents_table(m))
        if steps_limit != 0:
            console.print(
                build_steps_table(
                    m.get("steps") or [],
                    model_filter=active_model_filter,
                    limit=steps_limit,
                )
            )

    return buf.getvalue()
