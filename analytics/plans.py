"""采购方案的登记、排序、批准快照与事后重估。

方案本身不可变：同一编号不允许登记不同内容。批准时把当时的 as_of 时点、
排放假设、候选方案集合和排序结果整体存入 DecisionBook；之后无论新证据
如何到达，都能用同一时点和同一假设复现批准时的结论，并单独用当前数据
重估推荐是否改变。
"""
import json
from .store import timestamp

class PlanBook:
    def __init__(self, connection, emissions, decisions):
        self.connection = connection
        self.emissions = emissions
        self.decisions = decisions
        connection.execute("""CREATE TABLE IF NOT EXISTS plans (
            plan_id TEXT PRIMARY KEY, routes TEXT NOT NULL, note TEXT NOT NULL)""")

    def register(self, plan_id, routes, note=""):
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValueError("方案编号不能为空")
        if not isinstance(routes, (list, tuple)) or not routes:
            raise ValueError("方案线路不能为空")
        if not all(isinstance(r, str) and r.strip() for r in routes):
            raise ValueError("方案线路不能为空")
        if len(set(routes)) != len(routes):
            raise ValueError("方案线路重复")
        if not isinstance(note, str):
            raise ValueError("方案备注必须为字符串")
        routes = sorted(routes)
        prior = self.connection.execute(
            "SELECT routes,note FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if prior is not None:
            if json.loads(prior["routes"]) == routes and prior["note"] == note:
                return False
            raise ValueError("方案编号已存在且内容不同")
        self.connection.execute(
            "INSERT INTO plans VALUES(?,?,?)", (plan_id, json.dumps(routes, ensure_ascii=False), note))
        return True

    def list(self):
        rows = self.connection.execute("SELECT plan_id,routes FROM plans ORDER BY plan_id").fetchall()
        return {row["plan_id"]: json.loads(row["routes"]) for row in rows}

    def rank(self, as_of=None, factors=None):
        return self.emissions.rank_plans(self.list(), as_of=as_of, factors=factors)

    def approve(self, approval_id, plan_id, approved_at, as_of=None):
        plans = self.list()
        if plan_id not in plans:
            raise ValueError("未知采购方案")
        approved_at = timestamp(approved_at)
        as_of = timestamp(as_of) if as_of is not None else approved_at
        ranking = self.emissions.rank_plans(plans, as_of=as_of)
        plan_result = next(e for e in ranking["ranking"] if e["plan_id"] == plan_id)
        body = {"kind": "plan_approval", "plan_id": plan_id, "approved_at": approved_at,
                "as_of": as_of, "assumptions": ranking["assumptions"], "plans": plans,
                "ranking": ranking["ranking"],
                "recommended_plan_id": ranking["recommended_plan_id"],
                "plan_result": plan_result}
        return self.decisions.publish(approval_id, approved_at, f"采购方案批准:{plan_id}", body)

    @staticmethod
    def _approval(decisions, approval_id):
        record = decisions.get(approval_id)
        if record is None or not isinstance(record["body"], dict) \
                or record["body"].get("kind") != "plan_approval":
            raise ValueError("未知采购方案批准记录")
        return record

    def reproduce(self, approval_id):
        """按批准时的时点和假设重算，验证结论可以复现。"""
        record = self._approval(self.decisions, approval_id)
        body = record["body"]
        recomputed = self.emissions.rank_plans(
            body["plans"], as_of=body["as_of"], factors=body["assumptions"])
        matches = recomputed["ranking"] == body["ranking"] \
            and recomputed["recommended_plan_id"] == body["recommended_plan_id"]
        return {"approval_id": approval_id, "matches": matches,
                "as_of": body["as_of"], "assumptions": body["assumptions"],
                "stored": {"ranking": body["ranking"],
                           "recommended_plan_id": body["recommended_plan_id"]},
                "recomputed": recomputed}

    def reassess(self, approval_id):
        """用批准时的假设和当前全部数据重估，显示新证据是否改变推荐。"""
        record = self._approval(self.decisions, approval_id)
        body = record["body"]
        current = self.emissions.rank_plans(
            body["plans"], as_of=None, factors=body["assumptions"])
        now_entry = next(e for e in current["ranking"] if e["plan_id"] == body["plan_id"])
        then_entry = body["plan_result"]
        changed = current["recommended_plan_id"] != body["recommended_plan_id"] \
            or now_entry["expected_reduction_kg"] != then_entry["expected_reduction_kg"]
        return {"approval_id": approval_id, "changed": changed,
                "recommendation": {"approved": body["recommended_plan_id"],
                                   "current": current["recommended_plan_id"]},
                "approved_plan": {"plan_id": body["plan_id"],
                                  "rank_then": then_entry["rank"],
                                  "rank_now": now_entry["rank"],
                                  "expected_reduction_then": then_entry["expected_reduction_kg"],
                                  "expected_reduction_now": now_entry["expected_reduction_kg"]},
                "current_ranking": current["ranking"]}
