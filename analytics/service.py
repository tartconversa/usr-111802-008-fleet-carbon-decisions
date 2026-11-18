"""协调来源事件、主体目录、现有摘要、排放分析与采购方案决策。"""
import json
import sqlite3
from pathlib import Path
from .store import EventStore
from .identity import EntityDirectory
from .metrics import Metrics
from .decisions import DecisionBook
from .domain import FleetRoster
from .emissions import EmissionFactors, EmissionAnalytics
from .plans import PlanBook

class AnalyticsService:
    def __init__(self, database, config_path=None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[1] / "config" / "domain.json"
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.store = EventStore(self.connection, self.config["event_kinds"])
        self.entities = EntityDirectory(self.connection)
        self.metrics = Metrics(self.store, self.entities)
        self.decisions = DecisionBook(self.connection)
        self.domain = FleetRoster(self.connection)
        self.factors = EmissionFactors(self.config["emissions"])
        self.emissions = EmissionAnalytics(self.store, self.entities, self.domain, self.factors)
        self.plans = PlanBook(self.connection, self.emissions, self.decisions)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def register(self, entity_id, label, group_id):
        with self.connection:
            return self.entities.register(entity_id,label,group_id)

    def link(self, source, external_id, entity_id):
        with self.connection:
            return self.entities.link(source,external_id,entity_id)

    def ingest(self, event):
        with self.connection:
            return self.store.ingest(event)

    def summary(self, as_of=None):
        return self.metrics.summary(as_of)

    def publish(self, report_id, created_at, title, as_of=None):
        body = {"as_of": as_of, "summary": self.summary(as_of)}
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)

    def assign_vehicle(self, vehicle, power, route, effective_from, recorded_at=None):
        with self.connection:
            return self.domain.assign(vehicle, power, route, effective_from, recorded_at)

    def route_comparison(self, as_of=None):
        return self.emissions.route_comparison(as_of)

    def register_plan(self, plan_id, routes, note=""):
        with self.connection:
            return self.plans.register(plan_id, routes, note)

    def rank_plans(self, as_of=None):
        return self.plans.rank(as_of)

    def approve_plan(self, approval_id, plan_id, approved_at, as_of=None):
        with self.connection:
            return self.plans.approve(approval_id, plan_id, approved_at, as_of)

    def reproduce_approval(self, approval_id):
        return self.plans.reproduce(approval_id)

    def reassess_approval(self, approval_id):
        return self.plans.reassess(approval_id)
