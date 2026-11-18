"""按实际服务量计算的线路排放强度与改造前后比较。

口径说明：
- 行程事件（trip）提供服务量，空驶行程记 empty:true、服务量为 0；
- 能耗事件（fuel/charge）按发生时间找到车辆当时的线路分配，归属到对应线路；
- 柴油排放 = 升数 × 柴油因子；电耗排放 = 电量 × 充电时段对应的电力因子；
- 排放强度 = 归属排放 ÷ 实际服务量，分柴油侧与电动侧；
- 两侧都有有效服务量与能耗记录的线路才可比较改造效果，否则给出原因。
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from .store import timestamp

Q3 = Decimal("0.001")
Q6 = Decimal("0.000001")
Q2 = Decimal("0.01")

def _fmt(value, quantum):
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")

def _num(value):
    if value == value.to_integral_value():
        return str(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return _fmt(value, Q3)

def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        try:
            parsed = Decimal(str(value))
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None

def _minutes(text):
    if not isinstance(text, str):
        raise ValueError("时段必须为 HH:MM 字符串")
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("时段必须为 HH:MM 字符串")
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("时段必须为 HH:MM 字符串")
    if not (0 <= hours <= 24 and 0 <= minutes < 60):
        raise ValueError("时段超出范围")
    total = hours * 60 + minutes
    if total > 1440:
        raise ValueError("时段超出范围")
    return total

class EmissionFactors:
    """柴油与分时段电力的排放因子；可从未经处理的配置或批准快照重建。"""

    def __init__(self, config):
        if not isinstance(config, dict):
            raise ValueError("排放因子配置必须为对象")
        diesel = _number(config.get("diesel_kg_per_liter"))
        if diesel is None or diesel <= 0:
            raise ValueError("柴油排放因子无效")
        self.diesel_kg_per_liter = diesel
        raw_periods = config.get("electricity_periods")
        if not isinstance(raw_periods, list) or not raw_periods:
            raise ValueError("电力分时段因子缺失")
        periods = []
        for item in raw_periods:
            if not isinstance(item, dict):
                raise ValueError("电力时段配置必须为对象")
            start, end = _minutes(item.get("start")), _minutes(item.get("end"))
            factor = _number(item.get("kg_per_kwh"))
            if factor is None or factor <= 0:
                raise ValueError("电力排放因子无效")
            if not start < end:
                raise ValueError("电力时段起止无效")
            periods.append({"start": item["start"], "end": item["end"],
                            "start_min": start, "end_min": end, "kg_per_kwh": factor})
        periods.sort(key=lambda p: p["start_min"])
        if periods[0]["start_min"] != 0 or periods[-1]["end_min"] != 1440:
            raise ValueError("电力时段必须覆盖全天")
        for earlier, later in zip(periods, periods[1:]):
            if earlier["end_min"] != later["start_min"]:
                raise ValueError("电力时段必须连续且不重叠")
        self.periods = periods

    def period_of(self, occurred_at):
        moment = datetime.fromisoformat(occurred_at)
        minute = moment.hour * 60 + moment.minute
        for period in self.periods:
            if period["start_min"] <= minute < period["end_min"]:
                return period
        raise ValueError("电力时段未覆盖充电时间")

    def snapshot(self):
        return {"diesel_kg_per_liter": _num(self.diesel_kg_per_liter),
                "electricity_periods": [
                    {"start": p["start"], "end": p["end"], "kg_per_kwh": _num(p["kg_per_kwh"])}
                    for p in self.periods]}

def _side():
    return {"trips": 0, "empty_trips": 0, "missing_service_trips": 0,
            "service": Decimal(0), "liters": Decimal(0),
            "kwh": Decimal(0), "kwh_by_period": defaultdict(lambda: Decimal(0))}

class EmissionAnalytics:
    def __init__(self, store, entities, roster, factors):
        self.store, self.entities, self.roster = store, entities, roster
        self.factors = factors

    def _factors(self, factors):
        if factors is None:
            return self.factors
        if isinstance(factors, EmissionFactors):
            return factors
        return EmissionFactors(factors)

    def _ledger(self, as_of, factors):
        ledger = defaultdict(lambda: defaultdict(_side))
        excluded = []
        for event in self.store.current(as_of):
            kind = event["kind"]
            if kind not in {"trip", "fuel", "charge"}:
                continue
            vehicle = self.entities.resolve(event["source"], event["entity"])
            if vehicle is None:
                excluded.append(self._exclude(event, "车辆未映射"))
                continue
            assignment = self.roster.assignment(vehicle, event["occurred_at"], as_of)
            if assignment is None:
                excluded.append(self._exclude(event, "该时段无线路分配"))
                continue
            side = ledger[assignment["route"]][assignment["power"]]
            payload = event["payload"]
            if kind == "trip":
                self._trip(event, payload, side, excluded)
            elif kind == "fuel":
                if assignment["power"] != "diesel":
                    excluded.append(self._exclude(event, "柴油能耗出现在电动分配时段"))
                    continue
                liters = _number(payload.get("liters"))
                if liters is None or liters <= 0:
                    excluded.append(self._exclude(event, "能耗数量无效"))
                    continue
                side["liters"] += liters
            else:
                if assignment["power"] != "electric":
                    excluded.append(self._exclude(event, "充电记录出现在柴油分配时段"))
                    continue
                kwh = _number(payload.get("kwh"))
                if kwh is None or kwh <= 0:
                    excluded.append(self._exclude(event, "能耗数量无效"))
                    continue
                period = factors.period_of(event["occurred_at"])
                side["kwh"] += kwh
                side["kwh_by_period"][f"{period['start']}-{period['end']}"] += kwh
        return ledger, excluded

    def _trip(self, event, payload, side, excluded):
        empty = payload.get("empty", False)
        if not isinstance(empty, bool):
            excluded.append(self._exclude(event, "空驶标记无效"))
            return
        if "service_units" in payload:
            service = _number(payload["service_units"])
            if service is None or service < 0:
                excluded.append(self._exclude(event, "服务量无效"))
                return
        else:
            service = Decimal(0)
        if empty and service > 0:
            excluded.append(self._exclude(event, "空驶标记与服务量矛盾"))
            return
        side["trips"] += 1
        if empty:
            side["empty_trips"] += 1
        elif "service_units" not in payload:
            side["missing_service_trips"] += 1
        side["service"] += service

    @staticmethod
    def _exclude(event, reason):
        return {"source": event["source"], "event_id": event["event_id"],
                "kind": event["kind"], "reason": reason}

    def _model(self, as_of, factors):
        ledger, excluded = self._ledger(as_of, factors)
        routes = {}
        for route, sides in ledger.items():
            model = {}
            for power in ("diesel", "electric"):
                side = sides.get(power)
                if side is None:
                    model[power] = None
                    continue
                if power == "diesel":
                    emissions = side["liters"] * factors.diesel_kg_per_liter
                else:
                    emissions = Decimal(0)
                    for label, kwh in side["kwh_by_period"].items():
                        start = label.split("-")[0]
                        period = next(p for p in factors.periods if p["start"] == start)
                        emissions += kwh * period["kg_per_kwh"]
                intensity = emissions / side["service"] if side["service"] > 0 else None
                model[power] = {"side": side, "emissions": emissions, "intensity": intensity}
            routes[route] = model
        fleet_service = sum((m["electric"]["side"]["service"] for m in routes.values()
                             if m["electric"] is not None), Decimal(0))
        fleet_emissions = sum((m["electric"]["emissions"] for m in routes.values()
                               if m["electric"] is not None), Decimal(0))
        fleet = {"service": fleet_service, "emissions": fleet_emissions,
                 "intensity": fleet_emissions / fleet_service if fleet_service > 0 else None}
        return routes, excluded, fleet

    @staticmethod
    def _side_reasons(model, power):
        label = "柴油" if power == "diesel" else "电动"
        energy_label = "柴油能耗" if power == "diesel" else "充电能耗"
        side_model = model[power]
        if side_model is None or side_model["side"]["trips"] == 0:
            return [f"无{label}行程"]
        reasons = []
        if side_model["side"]["service"] <= 0:
            reasons.append(f"{label}行程缺少有效服务量")
        energy = side_model["side"]["liters"] if power == "diesel" else side_model["side"]["kwh"]
        if energy <= 0:
            reasons.append(f"缺少{energy_label}记录")
        return reasons

    def route_comparison(self, as_of=None, factors=None):
        """各线路柴油侧与电动侧的排放强度，以及改造效果是否可比较。"""
        factors = self._factors(factors)
        as_of = timestamp(as_of) if as_of is not None else None
        routes_model, excluded, fleet = self._model(as_of, factors)
        routes = {}
        for route in sorted(routes_model):
            model = routes_model[route]
            reasons = []
            for power in ("diesel", "electric"):
                reasons.extend(self._side_reasons(model, power))
            comparable = not reasons
            entry = {"diesel": self._format_side(model["diesel"], "diesel", factors),
                     "electric": self._format_side(model["electric"], "electric", factors),
                     "comparable": comparable, "reasons": reasons,
                     "reduction_kg_per_unit": None, "reduction_pct": None}
            if comparable:
                cut = model["diesel"]["intensity"] - model["electric"]["intensity"]
                entry["reduction_kg_per_unit"] = _fmt(cut, Q6)
                entry["reduction_pct"] = _fmt(cut / model["diesel"]["intensity"] * 100, Q2)
            routes[route] = entry
        fleet_electric = None
        if fleet["intensity"] is not None:
            fleet_electric = {"service_units": _num(fleet["service"]),
                              "emissions_kg": _fmt(fleet["emissions"], Q3),
                              "intensity_kg_per_unit": _fmt(fleet["intensity"], Q6)}
        return {"as_of": as_of, "assumptions": factors.snapshot(), "routes": routes,
                "fleet_electric": fleet_electric, "excluded_events": excluded}

    @staticmethod
    def _format_side(model, power, factors):
        if model is None:
            return None
        side = model["side"]
        result = {"trips": side["trips"], "empty_trips": side["empty_trips"],
                  "missing_service_trips": side["missing_service_trips"],
                  "service_units": _num(side["service"]),
                  "emissions_kg": _fmt(model["emissions"], Q3),
                  "intensity_kg_per_unit": _fmt(model["intensity"], Q6) if model["intensity"] is not None else None}
        if power == "diesel":
            result["liters"] = _num(side["liters"])
        else:
            result["kwh"] = _num(side["kwh"])
            result["kwh_by_period"] = {label: _num(side["kwh_by_period"][label])
                                       for label in sorted(side["kwh_by_period"])}
        return result

    def rank_plans(self, plans, as_of=None, factors=None):
        """按预计减排量对方案排序；缺少基线或电动参考的线路被排除并说明。"""
        factors = self._factors(factors)
        as_of = timestamp(as_of) if as_of is not None else None
        routes_model, _, fleet = self._model(as_of, factors)
        entries = []
        for plan_id in sorted(plans):
            routes = plans[plan_id]
            if not isinstance(routes, (list, tuple)) or not routes:
                raise ValueError("方案线路不能为空")
            detail, excluded_routes = {}, []
            total = Decimal(0)
            for route in sorted(routes):
                model = routes_model.get(route, {"diesel": None, "electric": None})
                reasons = self._side_reasons(model, "diesel")
                if reasons:
                    excluded_routes.append({"route": route, "reasons": reasons})
                    continue
                electric = model["electric"]
                if electric is not None and electric["intensity"] is not None:
                    reference, source = electric["intensity"], "route"
                elif fleet["intensity"] is not None:
                    reference, source = fleet["intensity"], "fleet"
                else:
                    excluded_routes.append({"route": route, "reasons": ["缺少电动参考强度"]})
                    continue
                baseline = model["diesel"]["intensity"]
                service = model["diesel"]["side"]["service"]
                expected = (baseline - reference) * service
                total += expected
                detail[route] = {"baseline_intensity_kg_per_unit": _fmt(baseline, Q6),
                                 "reference_intensity_kg_per_unit": _fmt(reference, Q6),
                                 "reference_source": source,
                                 "service_units": _num(service),
                                 "expected_reduction_kg": _fmt(expected, Q3)}
            entries.append({"plan_id": plan_id, "expected_reduction_kg": _fmt(total, Q3),
                            "routes": detail, "excluded_routes": excluded_routes,
                            "_total": total})
        entries.sort(key=lambda e: (-e["_total"], e["plan_id"]))
        recommended = None
        for rank, entry in enumerate(entries, start=1):
            entry["rank"] = rank
            if rank == 1 and entry["_total"] > 0:
                recommended = entry["plan_id"]
            del entry["_total"]
        return {"as_of": as_of, "assumptions": factors.snapshot(),
                "ranking": entries, "recommended_plan_id": recommended}
