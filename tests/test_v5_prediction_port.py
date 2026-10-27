import io
import contextlib
import unittest
from unittest.mock import patch

import hhan_pzone_core as core
import hhan_pzone_manager as manager
import test_v6 as fixtures
from test_v6 import candidate, item


class V5PredictionPortTests(unittest.TestCase):
    setUp = fixtures.V6Tests.setUp
    tearDown = fixtures.V6Tests.tearDown

    def points_snapshot(self, rescue=1000, near_cap=False):
        return {"base_day": 800, "base_cap": 1200, "rescue_day": rescue,
                "rescue_cap": 1800, "rescue_near_cap": near_cap}

    def test_forecast_is_attached_to_completed_portfolio_and_total(self):
        core.LAST_PORTFOLIO_CONTEXT = {"downloading_count": 1, "downloading_gb": 5}
        with patch.object(manager, "fetch_site_points_snapshot", return_value=self.points_snapshot()):
            result = manager.generate_strategy([], 100, [item(str(i), 10) for i in range(10)])
        stats = result[5]
        self.assertEqual(len(result[2]), 1)
        self.assertEqual(stats["predicted_rescue_after"], 900)
        self.assertEqual(stats["predicted_total_after"], 1700)

    def test_download_completion_forecast_is_separate_from_current_plan(self):
        core.LAST_PORTFOLIO_CONTEXT = {"downloading_gb": 0}
        with patch.object(manager, "fetch_site_points_snapshot", return_value=self.points_snapshot(rescue=500)):
            result = manager.generate_strategy([candidate(size=20)], 50, [item('a', 50)])
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(result[5]["predicted_rescue_after"], 500)
        self.assertGreater(result[5]["predicted_rescue_after_downloads"], 500)

    def test_report_shows_v5_prediction_without_daily_qualification_gate(self):
        core.LAST_PORTFOLIO_CONTEXT = {"downloading_count": 1, "downloading_gb": 5}
        with patch.object(manager, "fetch_site_points_snapshot", return_value=self.points_snapshot(rescue=1800, near_cap=True)):
            result = manager.generate_strategy([], 100, [item('a', 100)])
        self.assertEqual(result[5]["predicted_rescue_after"], 1800)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            manager.print_strategy_report(result[0], result[1], result[2], result[3], 100,
                                          {"eligible_count": 0, "already_have": 0}, result[5], 0, {}, True)
        self.assertIn("预计本轮后保种积分（V5算法）：1800.00 / 1800 /天", output.getvalue())
        self.assertIn("预计本轮后合计积分：2600.00 / 3000 /天", output.getvalue())

    def test_missing_settlement_reports_unavailable_and_preserves_plan(self):
        core.LAST_PORTFOLIO_CONTEXT = {"downloading_gb": 0}
        with patch.object(manager, "fetch_site_points_snapshot", return_value=self.points_snapshot(rescue=None)):
            result = manager.generate_strategy([], 50, [item('a', 50)])
        self.assertIsNone(result[5]["predicted_rescue_after"])
        self.assertFalse(result[0])
        self.assertFalse(result[2])


if __name__ == '__main__':
    unittest.main()
