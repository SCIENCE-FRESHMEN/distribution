import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_inbound_rate_10_aisles import describe_rate, parse_log


SAMPLE_LOG = """[DAY 1] 入库任务数: 10
[DAY 1] 日内结束，当前时间 2.00 小时
第 1 天汇总
------------------------------------------------------------
第 1 个入库任务 IN_1 完成 (巷道 1)，起止 0.0s~100.0s，耗时 100.0s
第 2 个入库任务 IN_2 完成 (巷道 2)，起止 10.0s~110.0s，耗时 100.0s
第 1 个出库任务 OUT_1 完成 (巷道 1)，起止 120.0s~200.0s，耗时 80.0s
移库数量: 3
[DAY 1 结束] 货位配对率: 8/10 = 80.00%; 梁配对率(不含solo): 16/24 = 66.67%; 梁配对率(含solo): 20/24 = 83.33%
[DAY 2] 入库任务数: 0
[DAY 2] 日内结束，当前时间 0.00 小时
第 2 天汇总
------------------------------------------------------------
移库数量: 0
[DAY 2 结束] 货位配对率: 8/10 = 80.00%; 梁配对率(不含solo): 16/24 = 66.67%; 梁配对率(含solo): 20/24 = 83.33%
"""


class InboundRateEvalTests(unittest.TestCase):
    def test_describe_rate(self):
        profile = describe_rate(1 / 100.0)
        self.assertAlmostEqual(profile.avg_interval_s, 100.0)
        self.assertAlmostEqual(profile.avg_tasks_per_hour, 36.0)
        self.assertAlmostEqual(profile.avg_tasks_per_day, 864.0)

    def test_parse_log_daily_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "sample.txt"
            log_path.write_text(SAMPLE_LOG, encoding="utf-8")
            metrics = parse_log(log_path, [1, 2])

        self.assertEqual(metrics[1].inbound_planned, 10)
        self.assertEqual(metrics[1].inbound_completed, 2)
        self.assertEqual(metrics[1].outbound_completed, 1)
        self.assertEqual(metrics[1].relocations, 3)
        self.assertAlmostEqual(metrics[1].last_outbound_completion_s, 200.0)
        self.assertAlmostEqual(metrics[1].avg_aisle_utilization_pct, (280.0 / 2.0) / 7200.0 * 100.0)
        self.assertAlmostEqual(metrics[1].end_beam_pairing_pct, 83.33)

        self.assertEqual(metrics[2].inbound_planned, 0)
        self.assertEqual(metrics[2].inbound_completed, 0)
        self.assertEqual(metrics[2].avg_aisle_utilization_pct, 0.0)


if __name__ == "__main__":
    unittest.main()
