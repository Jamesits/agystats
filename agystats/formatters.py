"""Shared formatting, valuation card extraction, and terminal scatter helpers for agystats."""

from __future__ import annotations

import math
import re
from typing import Any

from rich.text import Text

from .models import PROJECT_SCOPE_PREFIX, UNASSIGNED_PROJECT_ID

_ORCH_SHORT_RE = re.compile(r"^Orchestrator\s*\(([^)]+)\)\s*·\s*", re.IGNORECASE)
_SUBAGENT_PREFIX_RE = re.compile(r"^Subagent\s*·\s*", re.IGNORECASE)

_BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


def fmt_int(n: Any) -> str:
    return f"{int(n or 0):,}"


def fmt_compact(n: Any) -> str:
    v = float(n or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}k"
    return str(int(v)) if v.is_integer() else f"{v:.1f}"


def fmt_usd(val: Any) -> str:
    if val is None:
        return "—"
    try:
        n = float(val)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(n):
        return "—"
    if abs(n) < 1e-12:
        return "$0.00"
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a < 0.0001:
        return f"{sign}<$0.0001"
    if a < 0.01:
        return f"{sign}${a:.4f}"
    if a < 1.0:
        return f"{sign}${a:.3f}"
    return f"{sign}${a:,.2f}"


def fmt_credits(val: Any) -> str:
    if val is None:
        return "—"
    try:
        n = float(val)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(n):
        return "—"
    if abs(n) < 1e-9:
        return "0 cr"
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a < 0.05:
        return f"{sign}<0.1 cr"
    if a < 10:
        return f"{sign}{a:.1f} cr"
    return f"{sign}{round(a):,} cr"


def fmt_rate(val: Any) -> str:
    if val is None:
        return "—"
    try:
        n = float(val)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(n):
        return "—"
    text = f"{n:.4f}".rstrip("0").rstrip(".")
    return f"${text}"


def fmt_orch_savings_pct(pct: Any) -> str:
    n = float(pct or 0)
    if n.is_integer():
        n_str = str(int(abs(n)))
    else:
        n_str = f"{abs(n):.1f}"
    return f"-{n_str}%" if n >= 0 else f"+{n_str}%"


def is_aggregate_mode(mode: str | None) -> bool:
    return mode in ("all", "project")


def strip_subagent_prefix(name: str | None) -> str:
    if not name:
        return ""
    return _SUBAGENT_PREFIX_RE.sub("", str(name)).strip()


def short_model_name(name: str | None) -> str:
    if not name:
        return "unknown"
    return _ORCH_SHORT_RE.sub(r"\1 · ", str(name))


def src_label(src: str | None) -> str:
    return {
        "hook_ms": "hook ms",
        "db_ms": "cli db",
        "watcher_ms": "live",
        "transcript_s": "log 1s",
    }.get(src or "", "—")


def fmt_nom_tps(tps: Any) -> str:
    v = float(tps or 0)
    return str(round(v)) if v >= 100 else f"{v:.1f}"


def fmt_duration(sec: Any) -> str:
    v = max(0.0, float(sec or 0))
    s10 = round(v * 10)
    if s10 < 600:
        return f"{s10 / 10:.1f}s"
    whole_s = round(v)
    if whole_s < 3600:
        m, rem_s = divmod(whole_s, 60)
        return f"{m}m {rem_s}s"
    whole_m = round(v / 60)
    if whole_m < 1440:
        h, rem_m = divmod(whole_m, 60)
        return f"{h}h {rem_m}m"
    days = v / 86400.0
    return f"{round(days)}d" if days >= 100 else f"{days:.1f}d"


def fmt_approx_duration(sec: Any) -> str:
    return "~" + fmt_duration(sec)


def fmt_exact_sec_note(sec: Any) -> str:
    v = float(sec or 0)
    return f" [{v:.1f}s]" if v >= 60 else ""


def fmt_short_delay(sec: Any) -> str:
    return f"+{float(sec or 0):.1f}s"


def fmt_approx_sec(sec: Any) -> str:
    return f"~{float(sec or 0):.1f}s"


def fmt_cold_time_note(obj: dict[str, Any] | None) -> str:
    n = int((obj or {}).get("cold_timed_steps_count") or 0)
    if n <= 0:
        return ""
    cold_s = float((obj or {}).get("cold_model_time_s") or 0)
    noun = "prefill" if n == 1 else "prefills"
    return f" (incl. {n} cold {noun}: {fmt_duration(cold_s)}{fmt_exact_sec_note(cold_s)})"


def fmt_wasted_summary(obj: dict[str, Any] | None) -> str:
    if not obj:
        return ""
    parts: list[str] = []
    retries = int(obj.get("retry_steps_count") or 0)
    failed = int(obj.get("failed_turns_count") or 0)
    if retries > 0:
        parts.append(f"{retries} retried step" + ("" if retries == 1 else "s"))
    if failed > 0:
        parts.append(f"{failed} failed turn" + ("" if failed == 1 else "s"))
    return ", ".join(parts)


def fmt_wasted_time_note(obj: dict[str, Any] | None) -> str:
    w = float((obj or {}).get("wasted_time_s") or 0)
    if w <= 0:
        return ""
    summary = fmt_wasted_summary(obj)
    suffix = f" ({summary})" if summary else ""
    return f" · incl. {fmt_duration(w)}{fmt_exact_sec_note(w)} retry/failed wait{suffix}"


def resolve_model_entry(
    by_model: list[dict[str, Any]], query: str | None
) -> dict[str, Any] | None:
    if not query:
        return None
    q = query.strip().lower()
    if not q:
        return None
    for bm in by_model:
        name = str(bm.get("model_name") or "")
        if name.lower() == q or short_model_name(name).lower() == q:
            return bm
    for bm in by_model:
        name = str(bm.get("model_name") or "")
        if q in name.lower() or q in short_model_name(name).lower():
            return bm
    return None


def resolve_conversation_id(
    conversations: list[dict[str, Any]], query: str | None
) -> str | None:
    """Resolve an exact ID, ID prefix, or title substring to a conversation ID."""
    if not query:
        return None
    q = query.strip().lower()
    if not q:
        return None
    for c in conversations:
        if str(c.get("conversation_id") or "").lower() == q:
            return str(c["conversation_id"])
    prefix_matches = [
        str(c["conversation_id"])
        for c in conversations
        if str(c.get("conversation_id") or "").lower().startswith(q)
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if prefix_matches:
        return prefix_matches[0]
    title_matches = [
        str(c["conversation_id"])
        for c in conversations
        if q in str(c.get("title") or "").lower()
    ]
    if title_matches:
        return title_matches[0]
    preview_matches = [
        str(c["conversation_id"])
        for c in conversations
        if q in str(c.get("prompt_preview") or "").lower()
    ]
    return preview_matches[0] if preview_matches else None


def resolve_project_scope_id(
    projects: list[dict[str, Any]], query: str | None
) -> str | None:
    """Resolve an exact project ID/name, prefix, or substring to a `project:<id>` scope."""
    if not query:
        return None
    raw = query.strip()
    if raw.lower().startswith(PROJECT_SCOPE_PREFIX):
        raw = raw[len(PROJECT_SCOPE_PREFIX) :].strip()
    q = raw.lower()
    if not q:
        return None

    for p in projects:
        pid = str(p.get("project_id") or "")
        pname = str(p.get("name") or "")
        if pid.lower() == q or pname.lower() == q:
            return f"{PROJECT_SCOPE_PREFIX}{pid}"

    if q in ("no project", "unassigned"):
        for p in projects:
            if str(p.get("project_id") or "") == UNASSIGNED_PROJECT_ID:
                return f"{PROJECT_SCOPE_PREFIX}{UNASSIGNED_PROJECT_ID}"

    for p in projects:
        pid = str(p.get("project_id") or "")
        pname = str(p.get("name") or "")
        if pid.lower().startswith(q) or pname.lower().startswith(q):
            return f"{PROJECT_SCOPE_PREFIX}{pid}"

    for p in projects:
        pid = str(p.get("project_id") or "")
        pname = str(p.get("name") or "")
        if q in pname.lower() or q in pid.lower():
            return f"{PROJECT_SCOPE_PREFIX}{pid}"
    return None


def extract_valuation_cards(
    metrics: dict[str, Any],
    selected_model_entry: dict[str, Any] | None,
    val_pool: str = "all",
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]], str]:
    """Build valuation window cards and callouts matching dashboard charts.js semantics."""
    is_all_mode = is_aggregate_mode(metrics.get("mode"))
    val = metrics.get("valuation") or {}
    windows = val.get("windows") or {}
    subs = metrics.get("subagents") or {"count": 0, "items": [], "totals": {}}
    cpu = float(val.get("credits_per_usd") or 100.0)
    pool = val_pool if val_pool in ("all", "gemini", "partner") else "all"

    encompassing = (
        (windows.get("selected") or val)
        if is_all_mode
        else (windows.get("total") or val)
    )
    by_pool = encompassing.get("by_pool") or {}

    def _has_pool_usage(p: dict[str, Any] | None) -> bool:
        return bool(p and ((p.get("total_steps") or 0) > 0 or (p.get("total_tokens") or 0) > 0))

    if not selected_model_entry:
        if (pool == "gemini" and not _has_pool_usage(by_pool.get("gemini"))) or (
            pool == "partner" and not _has_pool_usage(by_pool.get("partner"))
        ):
            pool = "all"

    def _card_spec(win_obj: dict[str, Any] | None, label: str) -> dict[str, Any]:
        if selected_model_entry is not None:
            m_name = str(selected_model_entry.get("model_name") or "")
            mv = ((win_obj or {}).get("by_model") or {}).get(m_name)
            base_mv = selected_model_entry.get("valuation") or {}
            steps_count = int((mv or {}).get("steps_count") or 0)
            tot_tok = int((mv or {}).get("total_tokens") or 0)
            has_usage = steps_count > 0 or tot_tok > 0
            if base_mv.get("status") != "priced":
                return {
                    "label": label,
                    "steps_count": steps_count,
                    "has_usage": has_usage,
                    "cost_text": "—",
                    "credits_text": "",
                    "uncached_text": "—",
                    "cache_read_text": "—",
                    "output_text": "—",
                    "has_future": False,
                    "future_lbl": "",
                    "future_text": "",
                }
            c_usd = float((mv or {}).get("cost_usd") or 0.0)
            c_cred = float((mv or {}).get("credits") or 0.0)
            u_cost = float((mv or {}).get("uncached_input_cost_usd") or 0.0)
            c_cost = float((mv or {}).get("cache_read_cost_usd") or 0.0)
            o_cost = float((mv or {}).get("output_cost_usd") or 0.0)
            has_fut = bool(
                mv
                and mv.get("has_future_change")
                and mv.get("future_effective_date")
                and steps_count > 0
            )
            fut_lbl = ""
            fut_text = ""
            if has_fut and mv is not None:
                fut_usd = float(mv.get("future_cost_usd") or 0.0)
                pct_tag = ""
                if c_usd > 0 and fut_usd != c_usd:
                    diff_pct = round(((fut_usd - c_usd) / c_usd) * 100)
                    pct_tag = f" ({'+' if diff_pct >= 0 else ''}{diff_pct}%)"
                fut_lbl = f"From {mv['future_effective_date']}"
                fut_text = f"{fmt_usd(fut_usd)}{pct_tag}"
            return {
                "label": label,
                "steps_count": steps_count,
                "has_usage": has_usage,
                "cost_text": fmt_usd(c_usd),
                "credits_text": fmt_credits(c_cred),
                "uncached_text": fmt_usd(u_cost),
                "cache_read_text": fmt_usd(c_cost),
                "output_text": fmt_usd(o_cost),
                "has_future": has_fut,
                "future_lbl": fut_lbl,
                "future_text": fut_text,
            }

        pv = (((win_obj or {}).get("by_pool") or {}).get(pool)) or win_obj or {}
        cov = pv.get("coverage") or {}
        priced_steps = int(cov.get("priced_steps") or 0)
        unpriced_steps = int(cov.get("unpriced_steps") or 0)
        unattr_steps = int(cov.get("unattributed_steps") or 0)
        p_steps = (
            int(pv["total_steps"])
            if pv.get("total_steps") is not None
            else (priced_steps + unpriced_steps + unattr_steps)
        )
        p_has_usage = (
            p_steps > 0
            or int(pv.get("total_tokens") or 0) > 0
            or float(pv.get("cost_usd") or 0) > 0
        )
        has_any_priced = priced_steps > 0
        has_gaps = (unpriced_steps + unattr_steps) > 0
        no_data = not has_any_priced and not has_gaps

        p_has_fut = bool(has_any_priced and pv.get("has_future_change"))
        p_fut_lbl = ""
        p_fut_text = ""
        if p_has_fut:
            dates = pv.get("future_effective_dates") or []
            p_fut_lbl = f"From {dates[0]}" if len(dates) == 1 else "Scheduled std."
            fut_usd = float(pv.get("future_cost_usd") or 0.0)
            cur_usd = float(pv.get("cost_usd") or 0.0)
            pct_tag = ""
            if cur_usd > 0 and fut_usd != cur_usd:
                g_diff = round(((fut_usd - cur_usd) / cur_usd) * 100)
                pct_tag = f" ({'+' if g_diff >= 0 else ''}{g_diff}%)"
            p_fut_text = f"{fmt_usd(fut_usd)}{pct_tag}"

        star = "*" if has_gaps else ""
        return {
            "label": label,
            "steps_count": p_steps,
            "has_usage": p_has_usage,
            "cost_text": (
                "$0.00"
                if no_data
                else (f"{fmt_usd(pv.get('cost_usd'))}{star}" if has_any_priced else "—")
            ),
            "credits_text": (
                "0 cr"
                if no_data
                else (f"{fmt_credits(pv.get('credits'))}{star}" if has_any_priced else "")
            ),
            "uncached_text": (
                fmt_usd(pv.get("uncached_input_cost_usd"))
                if has_any_priced
                else ("—" if has_gaps else "$0.00")
            ),
            "cache_read_text": (
                fmt_usd(pv.get("cache_read_cost_usd"))
                if has_any_priced
                else ("—" if has_gaps else "$0.00")
            ),
            "output_text": (
                fmt_usd(pv.get("output_cost_usd"))
                if has_any_priced
                else ("—" if has_gaps else "$0.00")
            ),
            "has_future": p_has_fut,
            "future_lbl": p_fut_lbl,
            "future_text": p_fut_text,
        }

    cards: list[dict[str, Any]] = []
    if is_all_mode:
        win = str(metrics.get("window") or "all")
        sel_lbl_map = {
            "5h": "Last 5h",
            "24h": "Last 24h",
            "7d": "Last 7d",
            "30d": "Last 30d",
            "all": "All Time",
        }
        if win in ("24h", "7d", "30d", "all"):
            cards.append(_card_spec(windows.get("5h"), "Last 5h"))
        if win in ("30d", "all"):
            cards.append(_card_spec(windows.get("7d"), "Last 7d"))
        cards.append(_card_spec(windows.get("selected") or val, sel_lbl_map.get(win, "All Time")))
    else:
        age_s = float(val.get("conversation_age_s") or 0.0)
        c5 = _card_spec(windows.get("5h"), "Last 5h")
        c7 = _card_spec(windows.get("7d"), "Last 7d")
        c_tot = _card_spec(windows.get("total") or val, "Total")
        if age_s >= 5 * 3600 and c5["has_usage"]:
            cards.append(c5)
        if age_s >= 7 * 86400 and c7["has_usage"]:
            cards.append(c7)
        cards.append(c_tot)

    callouts: list[tuple[str, str, str]] = []
    if selected_model_entry is not None:
        mv_meta = selected_model_entry.get("valuation") or {}
        if mv_meta.get("status") == "unpriced":
            callouts.append(
                (
                    "warn",
                    f"Unpriced model: {selected_model_entry.get('model_name')}",
                    f"{fmt_int(selected_model_entry.get('steps_count'))} step(s)",
                )
            )
        elif mv_meta.get("status") == "unattributed":
            callouts.append(
                (
                    "info",
                    f"Unattributed steps ({selected_model_entry.get('model_name')}) excluded from cost",
                    f"{fmt_int(selected_model_entry.get('steps_count'))} step(s)",
                )
            )
        elif (
            mv_meta.get("orchestrator_role") == "executor"
            and mv_meta.get("orchestrator_savings_usd") is not None
        ):
            sav_usd = float(mv_meta["orchestrator_savings_usd"])
            cf_cost = float(mv_meta.get("cost_usd") or 0.0) + sav_usd
            sav_pct = round((sav_usd / cf_cost) * 1000) / 10 if cf_cost > 0 else 0.0
            pct_str = f" · {fmt_orch_savings_pct(sav_pct)}" if sav_pct else ""
            callouts.append(
                (
                    "orch",
                    f"Orchestrator Saved (vs {mv_meta.get('counterfactual_label') or 'Planner'})",
                    f"{fmt_usd(sav_usd)} ({fmt_credits(sav_usd * cpu)}{pct_str})",
                )
            )
    else:
        active_enc = by_pool.get(pool) or encompassing
        cov = active_enc.get("coverage") or {}
        unpriced_steps = int(cov.get("unpriced_steps") or 0)
        unattr_steps = int(cov.get("unattributed_steps") or 0)
        unpriced_list = cov.get("unpriced_models") or []

        orch = active_enc.get("orchestrator") or {}
        if orch.get("present"):
            ex_steps = int(orch.get("executor_steps") or 0)
            if ex_steps > 0:
                pl_str = " / ".join(orch.get("planner_labels") or []) or "Planner"
                fut_note = ""
                if orch.get("has_future_change") and abs(
                    float(orch.get("future_savings_usd") or 0.0)
                    - float(orch.get("savings_usd") or 0.0)
                ) > 1e-9:
                    fut_note = f" · Future: {fmt_usd(orch.get('future_savings_usd'))}"
                callouts.append(
                    (
                        "orch",
                        f"Orchestrator Saved (vs {ex_steps} Executor step{'s' if ex_steps != 1 else ''} on {pl_str})",
                        f"{fmt_usd(orch.get('savings_usd'))} ({fmt_credits(orch.get('savings_credits'))} · {fmt_orch_savings_pct(orch.get('savings_pct'))}){fut_note}",
                    )
                )
            else:
                pl_steps = int(orch.get("planner_steps") or 0)
                callouts.append(
                    (
                        "orch",
                        f"Orchestrator ({pl_steps} Planner step{'s' if pl_steps != 1 else ''})",
                        "$0.00 (no Executor steps yet)",
                    )
                )

        if not is_all_mode and int(subs.get("count") or 0) > 0:
            own_v = active_enc.get("own") or {}
            sub_v = active_enc.get("subagents") or {}
            own_cov = own_v.get("coverage") or {}
            sub_cov = sub_v.get("coverage") or {}
            own_has = int(own_cov.get("priced_steps") or 0) > 0
            sub_has = int(sub_cov.get("priced_steps") or 0) > 0
            own_txt = (
                f"{fmt_usd(own_v.get('cost_usd'))} ({fmt_credits(own_v.get('credits'))})"
                if own_has
                else (
                    "—"
                    if (int(own_cov.get("unpriced_steps") or 0) + int(own_cov.get("unattributed_steps") or 0)) > 0
                    else "$0.00 (0 cr)"
                )
            )
            sub_txt = (
                f"{fmt_usd(sub_v.get('cost_usd'))} ({fmt_credits(sub_v.get('credits'))})"
                if sub_has
                else (
                    "—"
                    if (int(sub_cov.get("unpriced_steps") or 0) + int(sub_cov.get("unattributed_steps") or 0)) > 0
                    else "$0.00 (0 cr)"
                )
            )
            callouts.append(
                (
                    "info",
                    "Session Breakdown",
                    f"Main: {own_txt} · Subagents ({subs['count']}): {sub_txt}",
                )
            )

        if unpriced_steps + unattr_steps > 0:
            gap_parts: list[str] = []
            if unpriced_steps > 0:
                names = ", ".join(str(u.get("model_name")) for u in unpriced_list)
                gap_parts.append(f"{unpriced_steps} unpriced ({names})")
            if unattr_steps > 0:
                gap_parts.append(f"{unattr_steps} unattributed")
            callouts.append(
                ("warn", "Partial coverage — excluded from cost", " · ".join(gap_parts))
            )

    return cards, callouts, pool


def build_braille_scatter(
    scatter_steps: list[dict[str, Any]],
    fit: dict[str, Any] | None,
    *,
    width: int = 56,
    height: int = 10,
    show_cold: bool = False,
) -> Text:
    """Render an Output Tokens (Y) vs Duration (X) Braille scatter + P95 roofline plot."""
    pts = [
        s
        for s in scatter_steps
        if s.get("step_kind") != "checkpoint"
        and s.get("duration_s") is not None
        and float(s["duration_s"]) > 0
        and (show_cold or not s.get("cold_cache_spike"))
    ]
    if not pts:
        return Text("No warm timed steps to plot.", style="dim")

    width = max(24, width)
    height = max(6, height)
    px_w = width * 2
    px_h = height * 4

    has_fit = bool(fit and fit.get("available") and (fit.get("slope_s_per_tok") or 0) > 0)
    raw_max_x = max(float(s["duration_s"]) for s in pts)
    if has_fit and fit is not None:
        raw_max_x = max(raw_max_x, float(fit["inferred_ttft_s"]) * 1.25)
    raw_max_y = max(100, max(int(s["output_tokens"]) for s in pts))
    max_x = max(1.0, raw_max_x * 1.08)
    max_y = max(100.0, raw_max_y * 1.08)

    grid: list[list[int]] = [[0] * width for _ in range(height)]
    tags: list[list[str]] = [[""] * width for _ in range(height)]

    def _plot_px(x_val: float, y_val: float, tag: str) -> None:
        px = max(0, min(px_w - 1, round((x_val / max_x) * (px_w - 1))))
        py = max(0, min(px_h - 1, px_h - 1 - round((y_val / max_y) * (px_h - 1))))
        cx, sub_x = divmod(px, 2)
        cy, sub_y = divmod(py, 4)
        grid[cy][cx] |= _BRAILLE_BITS[sub_y][sub_x]
        if tag == "latest" or not tags[cy][cx]:
            tags[cy][cx] = tag
        elif tag == "warm" and tags[cy][cx] == "line":
            tags[cy][cx] = "warm"

    if has_fit and fit is not None:
        ttft = float(fit["inferred_ttft_s"])
        slope = float(fit["slope_s_per_tok"])
        for px in range(px_w):
            x_sec = (px / max(1, px_w - 1)) * max_x
            if x_sec >= ttft:
                y_tok = (x_sec - ttft) / slope
                if 0.0 <= y_tok <= max_y:
                    _plot_px(x_sec, y_tok, "line")

    last_pt = pts[-1]
    for s in pts:
        tag = "latest" if s is last_pt else ("cold" if s.get("cold_cache_spike") else "warm")
        _plot_px(float(s["duration_s"]), float(s["output_tokens"]), tag)

    y_top_lbl = f"{fmt_compact(round(max_y)):>6} ┤"
    y_mid_lbl = f"{fmt_compact(round(max_y / 2)):>6} ┤"
    y_bot_lbl = f"{'0':>6} └"

    out = Text()
    for row_idx in range(height):
        if row_idx == 0:
            out.append(y_top_lbl, style="dim")
        elif row_idx == height // 2:
            out.append(y_mid_lbl, style="dim")
        elif row_idx == height - 1:
            out.append(y_bot_lbl, style="dim")
        else:
            out.append("       │", style="dim")

        for col_idx in range(width):
            bits = grid[row_idx][col_idx]
            if bits == 0:
                out.append("─" if row_idx == height - 1 else " ", style="dim" if row_idx == height - 1 else "")
                continue
            ch = chr(0x2800 + bits)
            tag = tags[row_idx][col_idx]
            if tag == "latest":
                out.append(ch, style="bold bright_magenta")
            elif tag == "cold":
                out.append(ch, style="bold yellow")
            elif tag == "warm":
                out.append(ch, style="bold cyan")
            else:
                out.append(ch, style="bright_green")
        out.append("\n")

    left_lbl = "0s"
    mid_lbl = fmt_duration(max_x / 2)
    right_lbl = fmt_duration(max_x)
    axis_chars = [" "] * width
    for idx, ch in enumerate(left_lbl):
        if idx < width:
            axis_chars[idx] = ch
    mid_start = max(len(left_lbl) + 1, min(width - len(mid_lbl) - len(right_lbl) - 1, (width - len(mid_lbl)) // 2))
    for idx, ch in enumerate(mid_lbl):
        pos = mid_start + idx
        if 0 <= pos < width:
            axis_chars[pos] = ch
    right_start = max(len(left_lbl) + 1, width - len(right_lbl))
    for idx, ch in enumerate(right_lbl):
        pos = right_start + idx
        if 0 <= pos < width:
            axis_chars[pos] = ch
    out.append("        " + "".join(axis_chars), style="dim")
    return out
