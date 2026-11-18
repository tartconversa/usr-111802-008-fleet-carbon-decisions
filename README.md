# 配送车队运营数据服务

服务登记多来源事件、业务主体及现有摘要，保存发布报告的原始内容，并在原始事件之上提供车队线路的排放强度分析与采购方案决策记录。示例数据为虚构记录。

需要 Python 3.10 或更新版本；仅依赖标准库。

运行测试：python -m unittest discover -s tests -v
编译检查：python -m compileall -q analytics tests
本地演练：python -m analytics --database demo.sqlite3 --fixture fixtures/sample.json

analytics/store.py 保存来源事件和修订；identity.py 管理外部身份映射；metrics.py 汇总已映射主体的事件计数与通用金额；decisions.py 保存已发布报告与批准记录；domain.py 保存车辆到线路、动力类型的分配历史（含生效时间与登记时间）；emissions.py 按实际服务量计算各线路柴油侧与电动侧的排放强度，给出改造前后可比性与原因；plans.py 登记采购方案、按预计减排量排序，并支持批准快照的复现与用当前数据重估推荐；service.py 协调这些接口。

通用摘要只表示原始观察；排放强度、改造效果与方案排序属于业务口径结论，其假设（柴油因子、分时段电力因子）来自 config/domain.json，并在批准记录中快照保存，详见 docs/domain.md。
