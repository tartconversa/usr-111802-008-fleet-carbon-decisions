"""车队车辆与运营线路的分配历史。

车辆到线路、动力类型的分配按区间保存：effective_from 是车辆实际换线的
生效时间，recorded_at 是这次分配被登记进系统的时间。两者通常相同，但
中途换线可能事后才补登；按批准时点复现时必须只看当时已登记的分配。
"""
from .store import timestamp

class FleetRoster:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS assignments (
            vehicle TEXT NOT NULL, effective_from TEXT NOT NULL, recorded_at TEXT NOT NULL,
            power TEXT NOT NULL, route TEXT NOT NULL,
            PRIMARY KEY (vehicle, effective_from))""")

    def assign(self, vehicle, power, route, effective_from, recorded_at=None):
        if power not in {"electric", "diesel"}:
            raise ValueError("动力类型无效")
        if not all(isinstance(v, str) and v.strip() for v in (vehicle, route)):
            raise ValueError("车辆与线路不能为空")
        effective = timestamp(effective_from)
        recorded = timestamp(recorded_at) if recorded_at is not None else effective
        if recorded < effective:
            raise ValueError("登记时间不能早于生效时间")
        prior = self.connection.execute(
            "SELECT power,route,recorded_at FROM assignments WHERE vehicle=? AND effective_from=?",
            (vehicle, effective)).fetchone()
        if prior is not None:
            if tuple(prior) == (power, route, recorded):
                return False
            raise ValueError("同车辆同生效时间的分配内容冲突")
        self.connection.execute(
            "INSERT INTO assignments VALUES(?,?,?,?,?)", (vehicle, effective, recorded, power, route))
        return True

    def assignment(self, vehicle, at, as_of=None):
        """车辆在某个发生时间点的有效分配；as_of 限制只看见当时已登记的分配。"""
        where = "vehicle=? AND effective_from<=?"
        parameters = [vehicle, timestamp(at)]
        if as_of is not None:
            where += " AND recorded_at<=?"
            parameters.append(timestamp(as_of))
        row = self.connection.execute(
            f"SELECT power,route,effective_from,recorded_at FROM assignments WHERE {where} "
            "ORDER BY effective_from DESC LIMIT 1", parameters).fetchone()
        return dict(row) if row else None

    def route(self, vehicle, as_of=None):
        current = self.history(vehicle, as_of)
        return current[-1]["route"] if current else None

    def history(self, vehicle, as_of=None):
        where = "vehicle=?"
        parameters = [vehicle]
        if as_of is not None:
            where += " AND recorded_at<=?"
            parameters.append(timestamp(as_of))
        rows = self.connection.execute(
            f"SELECT power,route,effective_from,recorded_at FROM assignments WHERE {where} "
            "ORDER BY effective_from", parameters).fetchall()
        return [dict(row) for row in rows]
