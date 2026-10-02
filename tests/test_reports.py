"""日报只追加：原始版与更正版本衔接，历史不覆盖。"""

from src.deep_gas import reports
from src.deep_gas.errors import ValidationError

from domain_fixture import DomainFixture


class DailyReportTest(DomainFixture):
    def test_correction_forms_version_chain(self) -> None:
        first = reports.record_daily(self.conn, self.actors["drill"], "W1",
                                     "2026-10-05", 80.0, "初报")
        self.assertEqual(first["current"]["version_no"], 1)
        self.assertIsNone(first["current"]["correction_of"])

        corrected = reports.record_daily(self.conn, self.actors["drill"], "W1",
                                         "2026-10-05", 83.0, "计量校零")
        self.assertEqual(corrected["current"]["version_no"], 2)
        self.assertEqual(corrected["current"]["correction_of"], first["current"]["id"])
        # 历史版本原样保留
        self.assertEqual([v["actual_rate"] for v in corrected["versions"]], [80.0, 83.0])
        self.assertEqual(reports.day_actuals(self.conn, "2026-10-05")
                         [self.conn.execute("SELECT id FROM wells WHERE code='W1'").fetchone()["id"]],
                         83.0)

    def test_negative_rate_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            reports.record_daily(self.conn, self.actors["drill"], "W1",
                                 "2026-10-05", -1.0)
