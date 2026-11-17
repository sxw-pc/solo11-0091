# 采集配方规范（RECIPE_SPEC）

## 1. 文件与范围

配方是 UTF-8 YAML 文件。一份配方绑定一个 `site` 与一个 `page_type`，只处理同站点、同页面类型的本地快照。所有路径相对项目根目录解析，并使用 `Path.resolve()` 校验；任何越界路径都会被拒绝。

## 2. 顶层结构

| 键 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | 是 | 整数 | 当前仅支持 `1` |
| `name` | 是 | 非空字符串 | 配方名，运行内唯一 |
| `site` | 是 | 非空字符串 | 快照站点目录名，例如 `site-a.invalid` |
| `page_type` | 是 | `list` / `detail` | 页面类型 |
| `description` | 否 | 字符串 | 说明 |
| `baseline` | 否 | 路径字符串 | 字段级 JSON 基线，必须位于项目根目录内且存在 |
| `fields` | 是 | 非空列表 | 字段定义 |

未知顶层键会报 `R002_UNKNOWN_KEY`。

## 3. 字段结构

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `name` | 是 | — | 字段名；同一配方内不可重复 |
| `selector` | 二选一 | — | 单个 CSS 选择器 |
| `selectors` | 二选一 | — | 候选 CSS 选择器列表，按顺序取首个有命中的选择器 |
| `type` | 否 | `string` | `string` / `integer` / `decimal` / `date` / `url` / `boolean` |
| `required` | 否 | `true` | 缺失、空值、类型失败时是否升级为致命 |
| `extract` | 否 | `text` | `text` / `attr` / `href` / `src` / `html` |
| `attribute` | 条件 | — | `extract: attr` 时必须提供属性名 |
| `normalize` | 否 | `[]` | 归一化规则列表，见第 5 节 |
| `multi` | 否 | `false` | 多值字段 |
| `separator` | 否 | — | 仅 `multi: true` 可用；声明后用该分隔符连接归一化值 |
| `on_change` | 否 | `warn` | 与基线比较后的值变化级别：`ignore` / `warn` / `fail` |
| `notes` | 否 | — | 人工备注，仅允许键，不参与运行 |

### 3.1 候选选择器

`selectors` 提供候选回退能力。运行按列表顺序选取第一个有命中的选择器；若两个或以上候选都有命中但归一化前的节点取值不一致，则产生 `ambiguous_selector` 警告。该标记不阻断其他字段。

### 3.2 多值字段

`multi: true` 时收集所有命中节点的值，并按声明顺序输出。若提供 `separator`，`normalized_value` 为分隔符连接字符串；否则为归一化值列表。空值节点被忽略；全部为空时转换状态为 `empty`。

## 4. 取值方式

| `extract` | 行为 |
| --- | --- |
| `text` | `get_text(" ", strip=True)` |
| `attr` | 读取 `attribute` 指定的属性 |
| `href` | 读取 `href` |
| `src` | 读取 `src` |
| `html` | 读取节点内部 HTML 片段，仍按文本规则受控归一化 |

## 5. 归一化规则

`normalize` 为有序列表，按顺序执行：

| 规则 | 行为 |
| --- | --- |
| `trim` | 去除首尾空白 |
| `collapse_ws` | 合并连续空白为单个空格 |
| `lower` | 转小写 |
| `upper` | 转大写 |
| `strip_thousands` | 去掉英文逗号与全角逗号 |
| `nfkc` | Unicode NFKC 归一 |
| `iso_date` | 尝试将常见日期格式转为 ISO `YYYY-MM-DD`；无法识别时保持原值 |

正则替换使用精确结构的映射：

```yaml
normalize:
  - type: regex_replace
    pattern: "\\s+"
    replacement: " "
```

未知规则、格式错误、非字符串 pattern/replacement 报 `R011_INVALID_NORMALIZE`。

## 6. 字段类型转换

| 类型 | 接受形式 | 归一化输出 |
| --- | --- | --- |
| `string` | 任意文本 | 原文本（先执行 normalization） |
| `integer` | 可带正负号的整数；允许千分位逗号与常见货币前后缀 | 十进制整数字符串 |
| `decimal` | 十进制数；允许千分位逗号、`¥` / `￥` / `$` / `CNY` / `RMB` / `人民币`、尾部“元” | 无指数、去尾零的十进制字符串 |
| `date` | `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYY.MM.DD`、`YYYY年M月D日`、`YYYYMMDD`、带时分秒形式 | ISO 日期 `YYYY-MM-DD` |
| `url` | `http/https` 绝对 URL，或 `/`、`./`、`../`、`#` 开头 | 去除首尾空白后的原值 |
| `boolean` | `true/false`、`1/0`、`yes/no`、`on/off`、`是/否`、`有/无`、`真/假` | `"true"` / `"false"` |

转换失败产生 `type_error`，原始节点值保存在 `raw_values`，归一化前值保留在 `normalized_values`。

## 7. 诊断标记

| 标记 | 条件 |
| --- | --- |
| `no_match` | 所有选择器均无命中 |
| `empty_value` | 命中节点但归一化后无值 |
| `multi_match` | `multi: false` 且命中数 > 1 |
| `type_error` | 类型转换失败 |
| `ambiguous_selector` | 多候选选择器命中结果不一致 |
| `version_drift` | 同一字段相邻版本由“有值”变“无值”或反之 |

稳定性评分从 100 起：`no_match` 必填 -40 / 非必填 -25；`empty_value` -30；`multi_match` -20；`type_error` -25；`ambiguous_selector` -15；`version_drift` -15，最终限制在 0–100。

## 8. 基线 JSON

```json
{
  "schema_version": 1,
  "site": "site-a.invalid",
  "page_type": "detail",
  "version": "2024-01",
  "generated_at": "2024-02-01T00:00:00Z",
  "fields": {
    "price": {
      "normalized_value": "1280",
      "match_count": 1,
      "conversion_status": "ok",
      "score": 100
    }
  }
}
```

对比使用当前配方的最新快照版本。差异类型包括 `added_field`、`missing_field`、`value_changed`、`match_count_changed`、`type_status_changed`、`stability_changed`。值变化级别优先使用字段 `on_change`，否则使用全局 `default_on_change`。

## 9. 稳定错误码

| 错误码 | 含义 |
| --- | --- |
| `R001_NOT_MAPPING` | 配方根节点不是映射 |
| `R002_UNKNOWN_KEY` | 顶层存在未知键 |
| `R003_MISSING_KEY` | 缺少 `name` / `site` / `fields` 等必填项 |
| `R004_FIELD_INVALID` | 字段不是映射 |
| `R004_FIELD_MISSING_KEY` | 字段缺少非空 `name` |
| `R005_UNKNOWN_FIELD_KEY` | 字段存在未知键 |
| `R006_DUPLICATE_FIELD` | 字段名重复 |
| `R007_INVALID_FIELD_TYPE` | 字段类型不在允许集合 |
| `R007_INVALID_PAGE_TYPE` | `page_type` 不是 `list` / `detail` |
| `R008_SELECTOR_CONFLICT` | 同时声明 `selector` 与 `selectors` |
| `R008_EMPTY_SELECTOR` | 选择器为空或 `selectors` 不是列表 |
| `R009_INVALID_SELECTOR` | CSS 选择器语法非法 |
| `R010_INVALID_EXTRACT` | 非法取值方式 |
| `R010_ATTRIBUTE_REQUIRED` | `extract: attr` 缺少属性名 |
| `R011_INVALID_NORMALIZE` | 归一化规则非法 |
| `R012_BASELINE_INVALID` | 基线路径不是非空字符串 |
| `R012_BASELINE_NOT_FOUND` | 基线文件不存在 |
| `R013_BASELINE_OUTSIDE_ROOT` | 基线路径越出项目根目录 |
| `R014_INVALID_MULTI` | `multi` 不是布尔值 |
| `R014_INVALID_SEPARATOR` | `separator` 不是非空字符串 |
| `R014_SEPARATOR_WITHOUT_MULTI` | 非多值字段声明了分隔符 |
| `R015_INVALID_REQUIRED` | `required` 不是布尔值 |
| `R016_SCHEMA_VERSION` | 不支持的 `schema_version` |
| `R017_INVALID_ON_CHANGE` | 非法 `on_change` |

加载器会把全部问题聚合到 `RECIPE_INVALID` 的 `details.issues` 中，同时 CLI 返回输入错误退出码 3。配方名重复、文件不可读等输入问题使用 `RECIPE_DUPLICATE_NAME` / `RECIPE_NOT_FOUND` / `RECIPE_UNREADABLE`。
