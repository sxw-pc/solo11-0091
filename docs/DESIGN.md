# 设计说明（DESIGN）

## 1. 目标与边界

「择约回归台」把采集字段契约视为可测试的工程接口：配方是契约，HTML 快照是测试输入，字段结果是断言，基线是上一轮期望，报告与台账是审计产物。系统不访问网络、不渲染页面、不修改配方，所有结论均可由本地文件复现。

## 2. 模块职责

| 模块 | 职责 |
| --- | --- |
| `cli.py` / `__main__.py` | 子命令、参数、退出码、错误输出 |
| `config.py` | YAML 配置、全局规则、路径越界拒绝 |
| `loader.py` | 配方与快照装配、覆盖检查 |
| `recipe_validator.py` | 静态 schema 校验、CSS 语法校验、稳定错误码 |
| `snapshot_reader.py` | 文件名解析、SHA-256、HTML 解析、空/畸形输入告警 |
| `matcher.py` | 选择器命中、候选回退、多候选一致性判断 |
| `extractor.py` | 文本/属性/链接/图片/HTML 取值、归一化、类型转换 |
| `table_evidence.py` | 表格合同输入的有界核对，只给全局命中证据，明确不产出记录 |
| `table_records.py` | 外层表格明细行关联、rowspan 网格、双合同行归属与拒发证据 |
| `diagnostics.py` | 字段诊断、稳定性评分、跨版本漂移 |
| `suggest.py` | 只读 DOM 归因、候选选择器建议（最多 5 条） |
| `baseline.py` | 基线加载、当前最新版本对比、差异级别 |
| `regression.py` | 字段状态归并、运行级判定、摘要 |
| `runner.py` | 全流程编排、运行 ID、报告与台账落盘 |
| `ledger_schema.py` | 表与索引 DDL |
| `ledger.py` | 单事务写入、历史保留 |
| `ledger_query.py` | 只读 URI 查询，供复核台与 ledger 命令 |
| `reporting.py` | JSON / Markdown / 自包含 HTML 渲染与指纹 |
| `server.py` / `views.py` | Flask 应用、CSP、只读路由 |
| `util.py` | 路径边界、确定性序列化、时间与哈希 |

## 3. 数据流

```text
configs/demo.yaml
   │
   ├─ load_config ── 路径约束 / 规则合并
   ├─ load_bundle
   │    ├─ load_recipe × N ── 静态校验、SHA-256
   │    └─ read_snapshot × M ── 文件名元数据、内容 SHA-256、BeautifulSoup
   │
   ├─ for recipe × matching snapshot × field
   │    ├─ match_nodes ── 候选命中 / 多候选一致性
   │    ├─ extract_field ── raw → normalize → type conversion
   │    └─ diagnose_field ── 标记 / 评分 / 发现项 / 只读建议
   │
   ├─ apply_version_drift ── 相邻版本“有值/无值”变化
   ├─ compare_baseline ── 最新快照与 JSON 基线
   ├─ summarize ── 字段状态 / 运行状态 / 计数
   │
   ├─ runs/<run_id>/report.{json,md,html}  (计算 SHA-256)
   └─ SQLite 台账事务写入
```

## 4. 字段状态与运行判定

- 诊断发现项级别：必填字段的 `no_match`、`empty_value`、`type_error` 按 `required_failure`（默认 `fail`）升级；其他标记按全局规则；字段 `on_change` 决定基线值变化级别。
- 字段状态取所有关联发现项、基线差异和漂移发现项中的最严级别：`fail` > `warn` > `info/pass`。
- 运行摘要同时记录命中率（有值字段 / 应采字段）、空值率、无命中、多匹配、类型失败、歧义与漂移字段计数。
- 运行状态：任一 `fail` → `fail`；否则任一 `warn` → `warn`；否则 `pass`。
- 未启用 `--fail-on-warn`：`pass=0`、`warn=2`、`fail=1`；启用后 `warn` 也返回 `1`。输入错误统一 `3`。

## 4a. 表格结构化记录（table-records）

`table_evidence` 只核对输入并保留 `records=null`；`table_records` 在同一套 FieldSpec/matcher/extractor 语义之上，把外层表格同一明细行的批次、项目、金额关联为一条记录：

- 行关联以**外层表真实行结构**为准，不按三个全局字段数组的位置拼接；嵌套 `table` 的行与单元格、`thead` 表头行永不作为业务明细发布。
- 含嵌套表的外层单元格在取值前深拷贝并剥离嵌套 `table`，剥离动作记录到 `source_cells.nested_table_stripped`；注释表文本（如 Z/999）不进入外层字段取值。
- `build_row_grid` 按 HTML rowspan/colspan 语义铺网格；rowspan 越过实际表尾不虚构行，只在 `grid_issues` 标注 `rowspan_past_table_end`。
- 两份消费者合同在同一快照上**独立计算**：`carry_rowspan` 只接受源表 rowspan 显式覆盖的批次继承（继承证据带 `inherited_from_row`）；`explicit_only` 要求批次单元格由本行自己书写，缺失时标 `missing_explicit_group` 并保留行级来源证据。合同之间不共享继承结论。
- 单字段 `no_match`/`empty`/`type_error`/`multi_match` 只拒绝所在行，不影响其他有效行；拒绝行与发布行都带稳定 `record_id`、原快照 SHA、外层行位置、字段来源与转换状态。
- 输入身份由快照字节 SHA-256 与清单指纹（含选择器、合同、字段配置的规范化摘要）固定；HTML、合同或选择器变更都会改变证据，行数与清单声明不一致时直接拒发，不沿用旧记录。

## 5. SQLite 表结构

- `run`：内部自增 `id`、唯一 `run_id`、状态、生成时间、固定时间标志、配置路径、计数、报告目录、三份报告 SHA-256；
- `recipe`：运行内配方名、站点、页面、路径、配方指纹、基线路径、字段数；
- `snapshot`：站点、页面类型、版本、相对路径、内容 SHA-256、字节数、采集时间占位、告警 JSON；
- `field_result`：配方/快照/字段维度、原始值、归一化值、命中数、转换状态、标记、评分、字段状态；
- `finding`：稳定代码、级别、消息、定位、证据 JSON、候选建议 JSON；
- `baseline_diff`：差异类型、级别、字段定位、新旧值、消息、快照路径。

索引覆盖运行、站点/页面/字段、状态、严重级别和快照。写入在单个事务中完成；重复运行通过 run ID 后缀保留历史，不执行覆盖或删除。台账读取使用 SQLite `mode=ro` URI。

## 6. 归因与候选建议

`no_match` 与 `multi_match` 使用当前快照的只读 DOM 结构归因：

- 报告原选择器的标签、类名、ID；
- 最佳候选节点的父级链与邻近文本；
- 每个候选选择器的命中数与样本。

候选建议基于类名相似度（`difflib.SequenceMatcher`）与唯一 DOM 路径生成。排序键固定为分数降序、选择器升序，去重后最多 5 条。每条建议携带置信度和固定风险提示。建议只存在于报告与台账，不会写回 YAML。

## 7. 确定性策略

- 所有集合显式排序，JSON 使用 `sort_keys=True`、固定缩进与尾换行；
- 快照使用原始字节 SHA-256；配方使用文件 SHA-256；
- 运行 ID 由固定时间前缀与输入指纹组成；
- 报告指纹在 JSON 之外计算并写入台账，避免自引用；
- `--fixed-time` 同时固定报告时间、ID 前缀与 golden 输出；
- 候选建议不使用随机数、集合遍历顺序或当前时间；
- SQLite 使用事务，不依赖自增 ID 作为报告排序键。

## 8. 错误处理

配方静态问题聚合为 `RECIPE_INVALID`（含稳定 `Rxxx` 子码）；空快照为 `S001_EMPTY_SNAPSHOT`；畸形但仍可解析的 HTML 记为告警并继续；字段归一化或类型转换失败转为 `type_error` 结构化结果，不中断其他字段。CLI 将输入错误输出为稳定 JSON 到 stderr 并返回 3。

## 9. 测试策略

单元测试覆盖加载、校验、快照、匹配、提取、诊断、基线、回归、台账与报告；集成测试调用完整 runner 与 Flask test client；golden 测试在固定时间、空台账、固定目录下核对三份报告的 SHA-256 与关键摘要，确保排序和序列化稳定。

## 10. 已知取舍

- 采用 Python `html.parser`，畸形 HTML 的解析结果可能因 BeautifulSoup 版本略有差异；
- 跨版本漂移只关注相邻版本“有值/无值”，不推断业务语义；
- 候选建议是启发式，不能替代人工判断；
- SQLite 单机台账面向中小规模快照回归，不作高并发分析库。
