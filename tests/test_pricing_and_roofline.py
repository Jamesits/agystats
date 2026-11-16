"""Tests for pricing resolution, Orchestrator savings, and P95 roofline fitting."""

from __future__ import annotations

import unittest

from agystats.models import _normalize_cli_step_model
from agystats.pricing import PricingCatalog
from agystats.roofline import compute_roofline_fit


class TestPricingAndRoofline(unittest.TestCase):
    def test_cli_model_normalization_and_pricing_resolution(self) -> None:
        self.assertEqual(
            _normalize_cli_step_model("gemini-pro-orchestrator-argon"),
            ("Orchestrator (Argon)", True, "Planner"),
        )
        self.assertEqual(
            _normalize_cli_step_model("gemini-pro-orchestrator"),
            ("Orchestrator (Carbon)", True, "Planner"),
        )
        self.assertEqual(
            _normalize_cli_step_model("gemini-flash-worker-agy"),
            ("Gemini 4 Hydrogen", False, "Executor"),
        )
        self.assertEqual(
            _normalize_cli_step_model("gemini-4-argon-medium"),
            ("Gemini 4 Argon (Medium)", False, ""),
        )

        cat = PricingCatalog(as_of_date="2026-11-16")
        res_argon = cat.resolve_model("Orchestrator (Argon) · Planner")
        self.assertEqual(res_argon.status, "priced")
        self.assertEqual(res_argon.orchestrator_role, "planner")
        self.assertIsNotNone(res_argon.entry)
        self.assertEqual(res_argon.entry.catalog_id, "gemini-4-argon")

        res_exec = cat.resolve_model("Orchestrator (Argon) · Executor")
        self.assertEqual(res_exec.status, "priced")
        self.assertEqual(res_exec.orchestrator_role, "executor")
        self.assertIsNotNone(res_exec.entry)
        self.assertEqual(res_exec.entry.catalog_id, "gemini-4-hydrogen")
        self.assertIsNotNone(res_exec.counterfactual_planner)
        self.assertEqual(res_exec.counterfactual_planner.catalog_id, "gemini-4-argon")

        res_worker = cat.resolve_model("gemini-flash-worker-agy")
        self.assertEqual(res_worker.status, "priced")
        self.assertIsNotNone(res_worker.entry)
        self.assertEqual(res_worker.entry.catalog_id, "gemini-4-hydrogen")

    def test_roofline_fit_recovers_nominal_ceiling_and_guards(self) -> None:
        too_few = compute_roofline_fit(((100, 2.0, "db_ms"), (500, 6.0, "db_ms")))
        self.assertFalse(too_few["available"])
        self.assertEqual(too_few["reason"], "min_steps")

        low_spread = compute_roofline_fit(
            tuple((100 + i * 20, 2.0 + i * 0.2, "db_ms") for i in range(5))
        )
        self.assertFalse(low_spread["available"])
        self.assertEqual(low_spread["reason"], "min_spread")

        # True roofline: ttft = 1.5s, decode = 200 tok/s (slope = 0.005 s/tok), plus jitter/outliers
        pts = (
            (100, 2.05, "db_ms"),
            (300, 3.02, "db_ms"),
            (600, 4.55, "db_ms"),
            (1000, 6.55, "db_ms"),
            (1600, 9.52, "db_ms"),
            (400, 18.0, "db_ms"),
            (800, 12.0, "db_ms"),
        )
        fit = compute_roofline_fit(pts)
        self.assertTrue(fit["available"])
        self.assertLessEqual(abs(fit["inferred_ttft_s"] - 1.5), 0.35)
        self.assertLessEqual(abs(fit["inferred_decode_tps"] - 200.0), 15.0)
        self.assertGreater(fit["avg_delay_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
