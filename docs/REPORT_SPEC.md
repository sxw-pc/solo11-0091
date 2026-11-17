# 报告规范（REPORT_SPEC）

## 1. 输出位置与文件

每次运行写入 `runs/<run_id>/`：

- `report.json`：机器可读全量报告；
- `report.md`：人工复核摘要；
- `report.html`：自包含 HTML，可直接离线打开。

`run_id` 形如 `20240301T000000Z-e23831f518b0`；固定时间与固定输入下，指纹部分稳定。台账遇重复基础 ID 时追加 `-2`、`-3` 等后缀，保留历史且不覆盖旧运行。报告文件 SHA-256 同时写入 SQLite `run` 表的 `json_sha256` / `md_sha256` / `html_sha256`。

## 2. JSON 顶层结构

```json
{
  "schema_version": 1,
  "report_kind": "selregress.run",
  "run": {
    "run_id": "...",
    "status": "pass|warn|fail",
    "generated_at": "2024-03-01T00:00:00Z",
    "fixed_time": true,
    "config_path": "configs/demo.yaml",
    "fail_on_warn": false,
    "report_dir": "runs/<run_id>"
  },
  "summary": {
    "status": "warn",
    "recipe_count": 3,
    "snapshot_count": 6,
    "field_result_count": 40,
    "finding_count": 11,
    "fail_finding_count": 0,
    "warn_finding_count": 11,
    "info_finding_count": 0,
    "baseline_diff_count": 11,
    "field_pass_count": 28,
    "field_warn_count": 12,
    "field_fail_count": 0,
    "average_stability": 94,
    "min_stability": 60,
    "field_with_value_count": 37,
    "hit_rate_percent": 92,
    "empty_field_count": 0,
    "empty_rate_percent": 0,
    "no_match_field_count": 3,
    "multi_match_field_count": 1,
    "type_error_field_count": 3,
    "ambiguous_selector_count": 1,
    "version_drift_field_count": 3,
    "empty_field_names": [],
    "no_match_field_names": ["site_a_detail.stock@2024-02", "site_a_list.featured_date@2024-02", "site_b_detail.product_code@2024-02"],
    "multi_match_field_names": ["site_a_list.featured_category@2024-02"],
    "type_error_field_names": ["site_a_detail.price@2024-02", "site_a_list.featured_price@2024-02", "site_b_detail.price@2024-02"],
    "ambiguous_field_names": ["site_b_detail.title@2024-02"],
    "version_drift_field_names": ["site_a_detail.stock@2024-02", "site_a_list.featured_date@2024-02", "site_b_detail.product_code@2024-02"],
    "input_warning_count": 0
  },
  "recipes": [],
  "snapshots": [],
  "fields": [],
  "findings": [],
  "baseline_diffs": []
}
```

### 2.1 `recipes[]`

每项包含 `name`、`site`、`page_type`、`path`、`sha256`、`baseline`、`schema_version`、`field_count`、`fields`。`fields` 保存静态配方定义（选择器、类型、是否必填等）。

### 2.2 `snapshots[]`

每项包含 `site`、`page_type`、`version`、`path`、`sha256`、`size`、`captured_at`、`warnings`。`captured_at` 是由版本标签生成的确定性采集时间占位（例如 `2024-01-01T00:00:00Z`），不是文件系统时间。

### 2.3 `fields[]`

一次“配方 × 快照 × 字段”结果，主要键：

| 键 | 含义 |
| --- | --- |
| `recipe_name` / `recipe_path` | 配方 |
| `site` / `page_type` / `version` | 快照维度 |
| `snapshot_path` / `snapshot_sha256` | 快照与指纹 |
| `field_name` / `field_type` / `required` / `multi` | 字段契约 |
| `selectors` / `used_selector` / `selector_hits` | 选择器与每个候选的命中数 |
| `raw_values` | 从节点取得的原始值列表 |
| `normalized_value` / `normalized_values` | 单值或多值归一化结果 |
| `match_count` | 命中节点数 |
| `conversion_status` | `ok` / `empty` / `type_error` / `no_match` |
| `has_value` | 归一化后是否至少有一个非空值；用于命中率统计 |
| `error` | 结构化转换或归一化错误文本 |
| `ambiguous` | 多候选是否不一致 |
| `flags` | 排序去重后的诊断标记 |
| `score` / `score_reasons` | 0–100 稳定性评分与扣分解释 |
| `status` | `pass` / `warn` / `fail` / `info` |

### 2.4 `findings[]`

包含 `code`、`severity`、`message`、配方/站点/页面/版本/快照/字段定位、`evidence` 对象、`suggestions` 数组。候选建议项固定包含 `selector`、`confidence`（high/medium/low）、`score`、`reason`、`risk`。

### 2.5 `baseline_diffs[]`

包含 `change_type`、`severity`、配方与字段定位、`old_value`、`new_value`、`message`、`snapshot_path`。

## 3. Markdown 章节

`report.md` 固定包含，顺序稳定：

1. 标题与运行标识、结论、生成时间、固定时间、配置；
2. `## 总览`：配方/快照/字段计数、发现计数、基线差异、平均稳定性；
3. `## 字段矩阵`：配方、版本、字段、状态、命中、转换、稳定性、标记；
4. `## 失败字段`；
5. `## 发现项`；
6. `## 跨版本漂移`：版本对与字段路径；
7. `## 候选选择器建议`：候选、置信度、分数、理由、风险；
8. `## 基线差异`；
9. 合规尾注。

Markdown 表格中的 `|` 与换行会被转义；长值截断，避免破坏结构。

## 4. HTML 自包含要求

- 单文件内嵌全部 CSS，不引用 `<link rel="stylesheet">`、外部字体、图标、图片或脚本；
- 不使用 CDN、远程 API、`fetch`、`XMLHttpRequest`；
- 不使用 `<script>`；仅在页内使用锚点导航；
- 所有报告字段经过 Jinja 自动转义，原始 HTML 片段不会被当作可执行标记；
- 可直接通过 `file://` 离线打开。

## 5. 排序规则

- `recipes`：`name`, `path`；
- `snapshots`：`site`, `page_type`, `version`, `path`；
- `fields`：`recipe_name`, `site`, `page_type`, `version`, `snapshot_path`, `field_name`；
- `findings`：严重级别（fail→warn→info），随后 `code`, `recipe_name`, `site`, `page_type`, `version`, `snapshot_path`, `field_name`, `message`；
- `baseline_diffs`：`recipe_name`, `site`, `page_type`, `field_name`, `change_type`, `snapshot_path`；
- 候选建议：分数降序、选择器字符串升序，最多 5 条；
- JSON 对象键使用 `sort_keys=True`，缩进 2，末尾换行；不输出随机 ID 或浮点时间。

## 6. 确定性与时间

无 `--fixed-time` 时使用当前 UTC 秒级时间。无配置文件 `fixed_time` 时同样如此。固定时间会对 `generated_at`、`run_id` 前缀和所有报告时间字段生效。golden 测试使用 `--fixed-time` 或配置内的 `fixed_time`，在空台账、固定输出目录下比较完整 JSON / Markdown / HTML 指纹。

SQLite 记录报告指纹，但报告文件内不嵌入自身 SHA-256，避免自引用循环。台账通过 `run_id` 关联三份报告。

## 7. 台账与报告的对应

SQLite 表包括 `run`、`recipe`、`snapshot`、`field_result`、`finding`、`baseline_diff`。复核台读取同一台账，展示运行详情、字段矩阵、快照详情、发现项与基线差异，不重新计算报告。

## 8. 退出码

| 退出码 | 条件 |
| ---: | --- |
| 0 | `pass`；或修复预演找到可行方案 |
| 2 | `warn` 且未启用 `--fail-on-warn`；或修复预演无可行方案 |
| 1 | `fail`，或 `warn` + `--fail-on-warn` |
| 3 | 配置、路径、配方、基线或输入错误 |

## 9. 选择器修复预演计划

`plan-selector-repair` 输出 JSON，顶层包含：

- `summary`：计划 ID、范围、目标/失败字段、是否可行、替换主选择器是否可交付、
  推荐方案摘要、冲突数量、报告文件路径；
- `plan`：完整反事实预演，包括 `scope_semantics`、`base_run`、`field_actions`
  （每个字段的替换/追加/放弃动作与真实重跑结果）、`evaluations`（整计划组合
  对照）、`recommended`、`conflicts` 与 `affected_snapshots`。

计划摘要与完整计划同时写入 `runs/<base_run_id>/selector-repair-<plan_id>.json`
和 `.md`，并写入 SQLite `repair_plan` / `repair_action`。候选建议的
`score`/`confidence` 只作线索；是否可交付由真实抽取、基线、跨版本与未列目标
字段一致性共同决定。
