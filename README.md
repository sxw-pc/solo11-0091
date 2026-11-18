# 择约回归台（selregress）

「择约回归台」是一套完全离线、可复现的采集配方契约回归工具。它消费本地 HTML 快照与声明式 YAML 配方，批量核验 CSS 选择器是否仍能命中、字段类型是否仍可转换、跨快照版本是否发生漂移，并与 JSON 基线比较，给出 `pass / warn / fail` 结论、稳定退出码、SQLite 台账和多格式报告。

项目用于解决采集链路的典型静默故障：站点改版后选择器失效、字段采空、类型转换失败，但任务本身没有崩溃，问题直到下游才暴露。

## 是 / 不是

**它是：**

- 本地 HTML 快照集的批量解析与字段抽取回归工具；
- 声明式采集配方的静态校验器；
- 选择器命中质量与跨版本漂移诊断器；
- 基线对比、回归判定与稳定退出码提供者；
- SQLite 运行台账、JSON / Markdown / 自包含 HTML 报告与只读复核台。

**它不是：**

- 网络爬虫或 HTTP 抓取器，不发起任何外部网络请求；
- 浏览器渲染或视觉回归工具；
- 自动改写配方、自动生成生产选择器的工具；
- 分布式任务调度、站点地图生成、归档或内容变更订阅系统；
- OCR、全文检索、向量检索、机器学习平台；
- 带登录、权限、多租户或审批流的业务系统。

## 合规边界

本项目仅用于分析**本人拥有或已获授权的本地快照材料**。运行期不发起任何 DNS、HTTP、CDN、字体、图标或远程 API 请求；复核台仅监听 `127.0.0.1`，页面资源全部本地化。仓库内站点使用保留域名（如 `site-a.invalid`）与虚构机构名称，不包含真实凭据、真实公司名、真实域名或真实目标地址。详见 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)。

## 快速上手

环境：Python 3.10+，依赖见 `requirements.txt`。

```bash
# 1. 一条命令跑完整演示：加载 → 解析 → 诊断 → 基线 → 台账 → 报告
make demo

# 2. 启动只读复核台（仅 127.0.0.1:5000）
make serve

# 3. 跑全部测试
make test
```

等价的直接命令：

```bash
python -m selregress demo
python -m selregress run --config configs/demo.yaml
python -m selregress serve --config configs/demo.yaml --port 5000
python -m selregress ledger --latest
pytest -q
```

`demo` 使用仓库自带 fixture，固定时间 `2024-03-01T00:00:00Z`，因此报告内容可复现。演示数据刻意包含选择器失效、多匹配、类型失败、跨版本漂移与基线变化，因此运行结论为 `warn`，CLI 退出码为 **2**；`make demo` 接受这一预期告警并返回成功。要验证失败分支，可修改临时配置或将 `--fail-on-warn` 打开。

表格合同（仅处理本地有界 HTML）：

```bash
# 只核对快照与全局字段命中，明确不产出记录（records=null）
python -m selregress table-evidence --manifest fixtures/table_contract/manifest.json
# 按外层表真实行结构关联批次/项目/金额，并按清单声明的合同决定行归属
python -m selregress table-records --manifest fixtures/table_contract/manifest.json
```

`table-records` 不按全局字段数组位置拼记录：嵌套注释表与表头不会发布；`carry_rowspan` 接受源表 rowspan 声明的批次继承，`explicit_only` 只发布本行自带批次单元格的记录，缺失时保留行级来源证据并拒发。两份合同在同一快照上独立计算，HTML、合同或选择器变更会随证据重算，行数与清单不一致则直接拒发。


安装依赖（离线环境使用本地 wheel 源）：

```bash
python -m pip install -r requirements.txt
```

## 配方 YAML 示例

```yaml
schema_version: 1
name: site_a_detail
site: site-a.invalid
page_type: detail                 # list / detail
baseline: configs/baselines/site_a_detail.baseline.json
description: 示例详情页字段契约
fields:
  - name: price
    selector: .price              # 或 selectors: [".price", "[data-price]"]
    type: decimal                 # string/integer/decimal/date/url/boolean
    required: true
    extract: text                 # text/attr/href/src/html
    normalize:
      - trim
      - strip_thousands
    multi: false
    on_change: warn               # ignore/warn/fail
```

字段键、归一化规则、候选选择器行为与稳定错误码见 [docs/RECIPE_SPEC.md](docs/RECIPE_SPEC.md)。

## 报告与退出码

每次运行写入：

- `runs/<run_id>/report.json`：机器可读全量报告；
- `runs/<run_id>/report.md`：人工复核摘要；
- `runs/<run_id>/report.html`：内嵌 CSS、无任何外部资源、可直接离线打开；
- `var/selregress.db`：SQLite 台账（默认配置），保留历史运行。

JSON 报告包含命中率、空值率、多匹配、类型失败、歧义、漂移等诊断计数；字段、发现项、漂移项、候选建议与基线差异均按稳定规则排序。使用 `--fixed-time` 可固定时间戳，便于 golden 测试。完整结构见 [docs/REPORT_SPEC.md](docs/REPORT_SPEC.md)。

退出码约定：

| 退出码 | 含义 |
| ---: | --- |
| 0 | `pass`，无失败无警告 |
| 2 | `warn`，无致命失败但有警告 |
| 1 | `fail`，存在致命失败；或启用 `--fail-on-warn` 时的 `warn` |
| 3 | 配置、路径、配方、基线或输入错误 |

```bash
python -m selregress run --config configs/demo.yaml
python -m selregress run --config configs/demo.yaml --fail-on-warn
python -m selregress run --config configs/demo.yaml --fixed-time 2024-03-01T00:00:00Z
```

## 目录结构

```text
selregress/             Python 包：CLI、加载、校验、提取、诊断、基线、台账、报告、Flask
  templates/            Jinja 模板（复核台 + 自包含 HTML 报告）
  static/               本地 CSS / 原生 JS
configs/
  recipes/              三个演示采集配方
  baselines/            字段级 JSON 基线
  demo.yaml             演示配置
fixtures/
  snapshots/            两站点、六个版本的本地 HTML 快照
  expected/             演示期望摘要
  bad/                  畸形配方、畸形/空快照测试素材
tests/                  pytest 测试与 golden 数据
docs/                   DESIGN / RECIPE_SPEC / REPORT_SPEC / COMPLIANCE
var/                    本地 SQLite 台账（运行时生成）
runs/                   报告输出（运行时生成）
```

## 复核台

```bash
python -m selregress serve --config configs/demo.yaml --port 5000
# 打开 http://127.0.0.1:5000/
# 健康检查 http://127.0.0.1:5000/healthz
```

页面包括运行列表、运行详情（字段矩阵 / 发现项 / 基线差异 / 配方与快照清单）、快照详情、发现项详情和候选选择器建议。复核台只注册 GET 路由，非 GET 请求返回 405；没有修改配方、删除运行、触发抓取或执行命令的接口。动态内容由 Jinja 自动转义，CSP 禁止外部连接。

## 测试

```bash
pytest -q
```

测试覆盖配方校验、路径边界、快照解析与指纹、字段提取与类型转换、全部诊断标记、基线差异、`pass/warn/fail` 与 `--fail-on-warn`、候选建议确定性、台账事务与历史保留、报告排序与自包含性、Flask 只读约束与 CLI demo。测试不依赖网络、真实域名或随机结果。至少三个 golden 测试校验固定时间下的完整报告指纹与关键摘要。

## 已知限制与不实现清单

- 仅解析本地静态 HTML，不执行 JavaScript，不处理由前端渲染后才出现的字段；
- 只做 DOM 与字段契约层诊断，不做页面视觉比对、OCR 或内容语义判断；
- 候选选择器建议基于类名相似度与 DOM 路径，仅供人工复核，不保证业务语义正确；
- 不支持 WARC / HAR / RSS / OAI-PMH 等归档或订阅协议；
- 不实现真实抓取、代理池、Cookie、验证码、反爬规避、分布式调度、任务队列；
- 不实现自动回写配方、自动提交选择器、链接巡检、表单填报 RPA；
- 不实现用户登录、权限系统、多租户、审计审批；
- 不依赖外部数据库、云存储、消息队列或远程 API；
- 跨版本漂移按“有值/无值”与相邻版本对判断，不做任意版本间的语义 diff；
- SQLite 台账适合单机、小规模快照回归，不是高并发分析仓库。
