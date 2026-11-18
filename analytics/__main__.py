"""本地演练入口：导入样本，查看业务摘要、线路改造比较、方案排序与批准复现。"""
import argparse
import json
from pathlib import Path
from .service import AnalyticsService

def main():
    parser = argparse.ArgumentParser(description="运营分析服务")
    parser.add_argument("--database", required=True)
    parser.add_argument("--fixture", default="fixtures/sample.json")
    args = parser.parse_args()
    sample = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    service = AnalyticsService(args.database)
    try:
        for subject in sample.get("entities", []):
            service.register(**subject)
        for alias in sample.get("aliases", []):
            service.link(**alias)
        for assignment in sample.get("assignments", []):
            service.assign_vehicle(**assignment)
        for event in sample.get("events", []):
            service.ingest(event)
        for plan in sample.get("plans", []):
            service.register_plan(**plan)
        report = {"summary": service.summary(),
                  "comparison": service.route_comparison(),
                  "ranking": service.rank_plans()}
        approval = sample.get("approval")
        if approval:
            service.approve_plan(**approval)
            report["approval"] = {
                "reproduce": service.reproduce_approval(approval["approval_id"]),
                "reassess": service.reassess_approval(approval["approval_id"])}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    finally:
        service.close()

if __name__ == "__main__":
    main()
