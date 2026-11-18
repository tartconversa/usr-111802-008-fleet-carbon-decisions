"""核验排放强度口径、线路可比性、方案排序与批准复现。"""
import json
import tempfile
import unittest
from pathlib import Path
from analytics import AnalyticsService

CONFIG = {
    "name": "测试",
    "event_kinds": ["trip", "fuel", "charge"],
    "emissions": {
        "diesel_kg_per_liter": "2.5",
        "electricity_periods": [
            {"start": "00:00", "end": "08:00", "kg_per_kwh": "0.3"},
            {"start": "08:00", "end": "20:00", "kg_per_kwh": "0.6"},
            {"start": "20:00", "end": "24:00", "kg_per_kwh": "0.9"},
        ],
    },
}

def event(event_id, kind, vehicle, occurred, payload, revision=1, observed_at=None, source="telematics"):
    return {"source": source, "event_id": event_id, "revision": revision, "kind": kind,
            "entity": vehicle, "occurred_at": occurred,
            "observed_at": observed_at or occurred, "payload": payload}

class EmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3", self.config_path)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def add_vehicle(self, vehicle, power, route, effective, recorded=None):
        self.service.register(vehicle, vehicle, "fleet")
        self.service.link("telematics", vehicle, vehicle)
        self.service.link("billing", vehicle, vehicle)
        self.service.assign_vehicle(vehicle, power, route, effective, recorded)

    def test_intensity_uses_service_volume_and_charge_periods(self):
        self.add_vehicle("V-D", "diesel", "R1", "2026-10-01T00:00:00+08:00")
        self.add_vehicle("V-E", "electric", "R1", "2026-11-01T00:00:00+08:00")
        self.service.ingest(event("f1", "fuel", "V-D", "2026-10-05T10:00:00+08:00", {"liters": 10}))
        self.service.ingest(event("t1", "trip", "V-D", "2026-10-05T09:00:00+08:00", {"service_units": 100}))
        self.service.ingest(event("t2", "trip", "V-D", "2026-10-06T09:00:00+08:00", {"empty": True}))
        self.service.ingest(event("c1", "charge", "V-E", "2026-11-05T02:00:00+08:00", {"kwh": 10}, source="billing"))
        self.service.ingest(event("c2", "charge", "V-E", "2026-11-05T21:00:00+08:00", {"kwh": 10}, source="billing"))
        self.service.ingest(event("t3", "trip", "V-E", "2026-11-05T09:00:00+08:00", {"service_units": 120}))
        route = self.service.route_comparison()["routes"]["R1"]
        self.assertTrue(route["comparable"])
        self.assertEqual(route["diesel"]["emissions_kg"], "25.000")
        self.assertEqual(route["diesel"]["intensity_kg_per_unit"], "0.250000")
        self.assertEqual(route["diesel"]["empty_trips"], 1)
        self.assertEqual(route["electric"]["emissions_kg"], "12.000")
        self.assertEqual(route["electric"]["intensity_kg_per_unit"], "0.100000")
        self.assertEqual(route["electric"]["kwh_by_period"],
                         {"00:00-08:00": "10", "20:00-24:00": "10"})
        self.assertEqual(route["reduction_kg_per_unit"], "0.150000")
        self.assertEqual(route["reduction_pct"], "60.00")

    def test_late_recorded_route_switch_reattributes_current_view(self):
        self.add_vehicle("V-1", "diesel", "R1", "2026-10-01T00:00:00+08:00")
        self.service.assign_vehicle("V-1", "diesel", "R2", "2026-11-01T00:00:00+08:00",
                                    recorded_at="2026-11-10T09:00:00+08:00")
        self.service.ingest(event("f1", "fuel", "V-1", "2026-10-15T10:00:00+08:00", {"liters": 10}))
        self.service.ingest(event("t1", "trip", "V-1", "2026-10-15T09:00:00+08:00", {"service_units": 100}))
        self.service.ingest(event("f2", "fuel", "V-1", "2026-11-05T10:00:00+08:00", {"liters": 10},
                                  observed_at="2026-11-05T12:00:00+08:00"))
        self.service.ingest(event("t2", "trip", "V-1", "2026-11-05T09:00:00+08:00", {"service_units": 50},
                                  observed_at="2026-11-05T12:00:00+08:00"))
        routes = self.service.route_comparison()["routes"]
        self.assertEqual(routes["R1"]["diesel"]["intensity_kg_per_unit"], "0.250000")
        self.assertEqual(routes["R2"]["diesel"]["intensity_kg_per_unit"], "0.500000")
        before = self.service.route_comparison(as_of="2026-11-05T23:59:59+08:00")["routes"]
        self.assertNotIn("R2", before)
        self.assertEqual(before["R1"]["diesel"]["service_units"], "150")
        self.assertEqual(before["R1"]["diesel"]["intensity_kg_per_unit"], "0.333333")

    def test_backfilled_charge_revises_current_ranking_only(self):
        self.add_vehicle("V-D", "diesel", "R1", "2026-10-01T00:00:00+08:00")
        self.add_vehicle("V-E", "electric", "R2", "2026-10-01T00:00:00+08:00")
        self.service.ingest(event("f1", "fuel", "V-D", "2026-10-05T10:00:00+08:00", {"liters": 10}))
        self.service.ingest(event("t1", "trip", "V-D", "2026-10-05T09:00:00+08:00", {"service_units": 100}))
        self.service.ingest(event("t2", "trip", "V-E", "2026-10-06T09:00:00+08:00", {"service_units": 100}))
        self.service.ingest(event("c1", "charge", "V-E", "2026-10-06T02:00:00+08:00", {"kwh": 10},
                                  observed_at="2026-10-06T08:00:00+08:00", source="billing"))
        self.service.register_plan("p1", ["R1"])
        as_of = "2026-10-20T00:00:00+08:00"
        before = self.service.rank_plans(as_of=as_of)
        self.assertEqual(before["ranking"][0]["expected_reduction_kg"], "22.000")
        self.assertEqual(before["recommended_plan_id"], "p1")
        self.service.ingest(event("c1", "charge", "V-E", "2026-10-06T02:00:00+08:00", {"kwh": 90},
                                  revision=2, observed_at="2026-11-01T08:00:00+08:00", source="billing"))
        after = self.service.rank_plans()
        self.assertEqual(after["ranking"][0]["expected_reduction_kg"], "-2.000")
        self.assertIsNone(after["recommended_plan_id"])
        self.assertEqual(self.service.rank_plans(as_of=as_of), before)

    def test_routes_without_comparable_trips_explain_why(self):
        self.add_vehicle("V-D1", "diesel", "R-no-ev", "2026-10-01T00:00:00+08:00")
        self.add_vehicle("V-E1", "electric", "R-no-diesel", "2026-10-01T00:00:00+08:00")
        self.add_vehicle("V-D2", "diesel", "R-no-service", "2026-10-01T00:00:00+08:00")
        self.add_vehicle("V-D3", "diesel", "R-no-energy", "2026-10-01T00:00:00+08:00")
        self.service.ingest(event("t1", "trip", "V-D1", "2026-10-05T09:00:00+08:00", {"service_units": 100}))
        self.service.ingest(event("f1", "fuel", "V-D1", "2026-10-05T10:00:00+08:00", {"liters": 10}))
        self.service.ingest(event("t2", "trip", "V-E1", "2026-10-05T09:00:00+08:00", {"service_units": 80}))
        self.service.ingest(event("c1", "charge", "V-E1", "2026-10-05T02:00:00+08:00", {"kwh": 10}, source="billing"))
        self.service.ingest(event("t3", "trip", "V-D2", "2026-10-05T09:00:00+08:00", {"empty": True}))
        self.service.ingest(event("f2", "fuel", "V-D2", "2026-10-05T10:00:00+08:00", {"liters": 10}))
        self.service.ingest(event("t4", "trip", "V-D3", "2026-10-05T09:00:00+08:00", {"service_units": 50}))
        routes = self.service.route_comparison()["routes"]
        self.assertEqual(routes["R-no-ev"]["reasons"], ["无电动行程"])
        self.assertEqual(routes["R-no-diesel"]["reasons"], ["无柴油行程"])
        self.assertEqual(routes["R-no-service"]["reasons"], ["柴油行程缺少有效服务量", "无电动行程"])
        self.assertEqual(routes["R-no-energy"]["reasons"], ["缺少柴油能耗记录", "无电动行程"])
        self.assertTrue(all(not route["comparable"] for route in routes.values()))

    def test_approval_reproduces_and_reassess_flags_new_evidence(self):
        self.add_vehicle("V-D", "diesel", "R1", "2026-10-01T00:00:00+08:00")
        self.add_vehicle("V-E", "electric", "R2", "2026-10-01T00:00:00+08:00")
        self.service.ingest(event("f1", "fuel", "V-D", "2026-10-05T10:00:00+08:00", {"liters": 10}))
        self.service.ingest(event("t1", "trip", "V-D", "2026-10-05T09:00:00+08:00", {"service_units": 100}))
        self.service.ingest(event("t2", "trip", "V-E", "2026-10-06T09:00:00+08:00", {"service_units": 100}))
        self.service.ingest(event("c1", "charge", "V-E", "2026-10-06T02:00:00+08:00", {"kwh": 10},
                                  observed_at="2026-10-06T08:00:00+08:00", source="billing"))
        self.service.register_plan("p1", ["R1"])
        self.service.register_plan("p2", ["R1", "R3"])
        approved = self.service.approve_plan("ap-1", "p1", "2026-10-20T12:00:00+08:00")
        self.assertEqual(approved["body"]["as_of"], "2026-10-20T12:00:00+08:00")
        self.assertEqual(approved["body"]["recommended_plan_id"], "p1")
        self.assertTrue(self.service.reproduce_approval("ap-1")["matches"])
        self.service.ingest(event("c1", "charge", "V-E", "2026-10-06T02:00:00+08:00", {"kwh": 90},
                                  revision=2, observed_at="2026-11-01T08:00:00+08:00", source="billing"))
        reassess = self.service.reassess_approval("ap-1")
        self.assertTrue(reassess["changed"])
        self.assertEqual(reassess["recommendation"], {"approved": "p1", "current": None})
        self.assertEqual(reassess["approved_plan"]["expected_reduction_then"], "22.000")
        self.assertEqual(reassess["approved_plan"]["expected_reduction_now"], "-2.000")
        self.assertTrue(self.service.reproduce_approval("ap-1")["matches"])

    def test_events_without_attribution_are_listed(self):
        self.add_vehicle("V-D", "diesel", "R1", "2026-10-01T00:00:00+08:00")
        self.add_vehicle("V-E", "electric", "R2", "2026-10-01T00:00:00+08:00")
        self.service.ingest(event("t1", "trip", "V-D", "2026-10-05T09:00:00+08:00", {"service_units": 100}))
        self.service.ingest(event("f1", "fuel", "V-D", "2026-10-05T10:00:00+08:00", {"liters": 10}))
        self.service.ingest(event("t2", "trip", "V-UNKNOWN", "2026-10-05T09:00:00+08:00", {"service_units": 5}))
        self.service.ingest(event("f2", "fuel", "V-D", "2026-09-01T10:00:00+08:00", {"liters": 5}))
        self.service.ingest(event("f3", "fuel", "V-E", "2026-10-05T10:00:00+08:00", {"liters": 5}))
        self.service.ingest(event("f4", "fuel", "V-D", "2026-10-06T10:00:00+08:00", {"liters": -1}))
        self.service.ingest(event("t3", "trip", "V-D", "2026-10-06T09:00:00+08:00",
                                  {"empty": True, "service_units": 3}))
        excluded = self.service.route_comparison()["excluded_events"]
        reasons = {item["event_id"]: item["reason"] for item in excluded}
        self.assertEqual(reasons["t2"], "车辆未映射")
        self.assertEqual(reasons["f2"], "该时段无线路分配")
        self.assertEqual(reasons["f3"], "柴油能耗出现在电动分配时段")
        self.assertEqual(reasons["f4"], "能耗数量无效")
        self.assertEqual(reasons["t3"], "空驶标记与服务量矛盾")
        route = self.service.route_comparison()["routes"]["R1"]
        self.assertEqual(route["diesel"]["intensity_kg_per_unit"], "0.250000")

    def test_roster_plan_and_factor_validation(self):
        self.assertTrue(self.service.assign_vehicle("V-1", "diesel", "R1", "2026-10-01T00:00:00+08:00"))
        self.assertFalse(self.service.assign_vehicle("V-1", "diesel", "R1", "2026-10-01T00:00:00+08:00"))
        with self.assertRaises(ValueError):
            self.service.assign_vehicle("V-1", "electric", "R2", "2026-10-01T00:00:00+08:00")
        with self.assertRaises(ValueError):
            self.service.assign_vehicle("V-1", "diesel", "R2", "2026-10-02T00:00:00+08:00",
                                        recorded_at="2026-10-01T00:00:00+08:00")
        self.assertTrue(self.service.register_plan("p1", ["R1"]))
        self.assertFalse(self.service.register_plan("p1", ["R1"]))
        with self.assertRaises(ValueError):
            self.service.register_plan("p1", ["R2"])
        with self.assertRaises(ValueError):
            self.service.register_plan("p2", [])
        with self.assertRaises(ValueError):
            self.service.approve_plan("ap-x", "nope", "2026-10-20T12:00:00+08:00")
        with self.assertRaises(ValueError):
            self.service.reproduce_approval("ap-x")
        bad = dict(CONFIG, emissions={"diesel_kg_per_liter": "2.5", "electricity_periods": [
            {"start": "00:00", "end": "08:00", "kg_per_kwh": "0.3"},
            {"start": "09:00", "end": "24:00", "kg_per_kwh": "0.6"}]})
        bad_path = Path(self.tmp.name) / "bad.json"
        bad_path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            AnalyticsService(Path(self.tmp.name) / "bad.sqlite3", bad_path)

if __name__ == "__main__":
    unittest.main()
