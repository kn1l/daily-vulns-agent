---
name: daily-vulns
description: Collect vulnerability intelligence from configured per-source scripts, merge and prioritize the results, enrich selected items with GitHub and simple search context, and produce a Markdown daily report. Use when explicitly asked to gather vulnerability updates within a time window.
---

# 收集漏洞情报

## 概览

从配置好的多个漏洞源脚本中收集数据，按时间窗口汇总候选项，补充 GitHub 和简单搜索信息后生成 Markdown 日报。每个源脚本只负责输出自己的漏洞数据，agent 负责逐个执行、合并、筛选、排序和成文。

## 读取配置

只读取项目中的运行配置：`config.yaml`。如果该文件不存在，就直接报配置缺失，不回退到样例配置。

顶层配置字段只使用：

- `script_sources`
- `source_configs`
- `ranking_rules`
- `project_filters`
- `reporting`

其中：

- `script_sources` 是核心入口
- `source_configs` 由 agent 读取，用于为脚本命令追加运行时参数；collector 脚本不直接读取 `config.yaml`
- `ranking_rules` 是受控的自然语言提权 / 降权规则，只在汇总排序阶段生效
- `project_filters` 用于按 CVSS 和项目定位做项目级过滤；已确认低于 GitHub star 阈值的 GitHub 项目直接过滤，已达标 GitHub 项目和非 GitHub 可靠项目定位条目同为一级候选
- `reporting` 控制重点项目展开阈值、持续关注阈值，以及兼容旧格式的表格上限字段

## 每源脚本约定

所有源脚本都放在 `scripts/` 目录下，例如：`scripts/nvd.py`。

每个源脚本都必须遵守同一条 CLI 契约：

- 接收 `--start`
- 接收 `--end`
- stdout 只输出 **JSON 数组**
- stderr 只输出错误信息
- 如果脚本支持认证或超时参数，由 agent 从 `source_configs.<source>` 读取后在运行时追加 CLI 参数，不要求脚本自己读取 `config.yaml`

JSON 数组中的每个元素代表一条漏洞记录。字段允许保留源特有内容，但至少应尽量包含这些公共字段：

- `cve`
- `title`
- `source`
- `url`
- `severity`
- `cvss`
- `cvss_version`
- `cvss_scores`
- `description`
- `references`
- `tags`
- `time`

`time` 表示该记录最可信的时间信息，例如公告发布时间、页面更新时间或 feed 时间戳。agent 用它判断记录是否落在 `start` ~ `end` 窗口内。

## 工作流

按以下阶段执行，不要跳过产物写入和最终校验。

### 1. 解析输入与配置

1. 读取 YAML 配置。
2. 确认本次采集的 `start` 和 `end` 时间窗口。
3. 定位本次运行输出目录：
   - 如果用户明确指定产物目录，直接把该目录作为输出目录。
   - 如果用户未指定产物目录，先生成本次运行的 `run_timestamp`，再创建默认输出目录 `reports/daily-vulns-{start}-to-{end}-{run_timestamp}/`。
   - 如果默认输出目录已存在，在目录名末尾追加短序号避免覆盖。

### 2. 初始化产物目录

1. 创建输出目录和 `process/` 子目录。
2. 本次运行最终只能产出一个 Markdown 日报：输出目录下的 `report.md`。
3. 本次运行必须产出输出目录下的 `manifest.json`。
4. 如果用户提供的是 `.md` 路径或看起来像报告文件名的路径，先要求用户改为提供产物目录，不要继续执行。

### 3. 执行采集源

1. 逐个执行 `script_sources` 中的命令。
2. 执行 GHSA 脚本源时，如果 `source_configs.ghsa.token` 非空，必须在命令末尾追加 `--token <token>`；如果 `source_configs.ghsa.timeout` 非空，必须追加 `--timeout <seconds>`。追加参数时不要把 token 写入报告、日志摘要、过程摘要或最终回复。
3. 如果某个源脚本因为超时、临时网络错误、连接重置、DNS / TLS 临时失败、HTTP 429 或 HTTP 5xx 失败，应在首次失败后最多再重试 2 次；重试间隔递增，建议约 5 秒、15 秒。参数错误、认证失败、权限不足、GitHub 速率限制已明确耗尽且给出 reset 时间、输出非 JSON 数组等确定性失败不要重试。
4. 为每个 source 保存原始 stdout / stderr，并在 `collection-summary.json` 中记录最终结果和每次尝试。
5. 如果重试后仍失败，记录该 source 失败并继续后续源。

### 4. 合并、回退与窗口过滤

1. 收集每个脚本最终成功返回的 JSON 数组。
2. 如果 NVD 脚本未能拿到可用漏洞详情，应尝试 NVD recent feed `https://nvd.nist.gov/feeds/json/cve/2.0/nvdcve-2.0-recent.json.gz` 作为 NVD fallback，并按每条 CVE 的 `published` 时间过滤到本次 `start` ~ `end` 窗口后再合并。
3. 触发 NVD fallback 时，应记录 NVD 脚本结果、尝试过的 NVD 补充来源、补充来源原始条数、按发布时间窗口过滤后的补充条数。
4. 按公共字段合并结果。
5. 先按 `time` 和 `start` / `end` 做窗口过滤，再做简单归并或去重，并写入 `dedup-windowed.json`。

### 5. 筛选、定位、补充与排序

1. 先给每条候选项计算基础价值分；严重度和 `CVSS` 是输入信号，但不能压过影响面、使用规模、潜在暴露面和攻击前置条件。
2. 应用 `ranking_rules`，把受控自然语言规则映射成固定提权 / 降权分值。
3. 对 `CVSS > 6.9` 的候选先执行项目身份定位：从结构化包信息、CPE、GitHub 仓库、官方公告、厂商产品页和可靠描述模式中抽取项目 / 产品身份，并为每条记录写入 `project_identity`。
4. 验证 `project-identity-summary.json` 存在且覆盖所有 `CVSS > 6.9` 候选。
5. 验证 `project-identity-evidence-gaps.json` 存在且包含 medium 置信度或证据不足但值得自检的 high-CVSS / 核心厂商候选。
6. 对值得继续评估的候选补充 GitHub 相关信息和简单搜索结果，并把有价值的链接写入 `references`。
7. 先做项目身份定位，再应用 `project_filters`：按 CVSS 阈值、GitHub stars、项目重要性、历史 CVE 噪声、CMS / WordPress 噪声和项目名质量做过滤；只有 high 置信度项目身份可进入一级候选，medium 只能进入证据不足候选 / 自检，low / none 不得进入正文。
8. 对一级候选和重点关注条目，可补充受影响项目地址、GitHub star 数、包下载量、生态位置、项目一句话介绍等上下文信息。
9. 验证 `cvss-filtered-enriched.json` 中继续保留 `project_identity`。
10. 按最终价值分排序，生成重点关注项目组、同时公开的漏洞、需要持续关注条目和自检条目。

### 6. 生成最终产物

1. 写入输出目录下的 `report.md`。
2. 写入输出目录下的 `manifest.json`，严格使用固定模板。
3. `manifest.json.md_path` 必须等于 `report.md`。
4. `manifest.json.highlights` 必须是正文中详细描述的所有漏洞标题字符串数组，只包含 `重点关注的项目/漏洞` 中按固定字段完整展开的条目，顺序与正文出现顺序一致。
5. 不要额外生成其他 Markdown 日报文件。

### 7. 最终校验与回复

1. 验证输出目录存在。
2. 验证 `report.md`、`manifest.json` 和 `process/` 存在。
3. 验证所有必须的过程文件已写入。
4. 验证过程文件、`report.md` 和 `manifest.json` 中没有未脱敏 token / API key。
5. 验证最终回复只汇报输出目录、日报路径、manifest 路径和关键统计，不贴出大段过程 JSON。

## 输出目录与可追溯性

每次生成日报时，必须创建一个本次运行输出目录。默认输出目录为 `reports/daily-vulns-{start}-to-{end}-{run_timestamp}/`，其中 `run_timestamp` 使用本地运行时间的 `YYYYMMDD-HHMMSS` 格式，例如 `reports/daily-vulns-2026-05-20-to-2026-05-21-20260623-143015/`。如果同名目录已存在，在目录名末尾追加短序号避免覆盖。

只允许用户指定产物目录，不支持单独指定最终报告文件路径。用户明确要求输出到某个产物目录时，该目录就是本次运行输出目录，最终日报和过程文件目录都必须直接写入该目录；不要再在该目录下额外创建带时间戳的子目录。

输出目录必须包含：

- `report.md`：最终 Markdown 漏洞日报。
- `manifest.json`：产物清单和推送摘要。
- `process/`：本次运行的过程文件目录。

如果用户给出的路径看起来像 Markdown 文件路径，应要求用户改为提供产物目录，而不是把它解释成报告文件名。

`manifest.json` 必须是 JSON 对象，至少包含：

- `title`：日报标题，用于 HTML 页面和钉钉标题。
- `generated_at`：生成时间，ISO 8601 格式。
- `md_path`：Markdown 文件相对路径，第一版固定为 `report.md`。
- `highlights`：正文中详细描述的所有漏洞标题数组；“详细描述”指在 `重点关注的项目/漏洞` 中按固定字段完整展开的条目，不包含 `需要持续关注`、`自检` 或 `同时公开的漏洞` 的简短列表项；每个元素只保留漏洞标题字符串，顺序与正文出现顺序一致。

`manifest.json` 固定使用以下结构，不要增加未约定字段：

```json
{
  "title": "今日漏洞情报：{start} ~ {end}",
  "generated_at": "{ISO_8601_datetime}",
  "md_path": "report.md",
  "highlights": [
    "漏洞标题 1",
    "漏洞标题 2"
  ]
}
```

`process/` 目录至少保存：

- `collection-summary.json`：每个 source 的最终执行结果、返回条数、失败原因和 `attempts` 尝试记录；每次尝试至少记录尝试序号、是否成功、返回码、stderr 摘要、是否触发重试和重试原因；命令中的 token / API key 必须脱敏
- `{source}.stdout.json`：每个脚本源的原始 stdout JSON
- `{source}.stderr.txt`：每个脚本源的 stderr
- `nvd-fallback-summary.json`：仅当触发 NVD fallback 时保存；记录 NVD 脚本结果、尝试过的 NVD 补充来源、补充来源原始条数、按发布时间窗口过滤后的补充条数
- `nvd-recent-feed-windowed.json`：仅当触发 NVD recent feed fallback 时保存，用于追溯按发布时间窗口过滤后的 NVD feed 记录
- `dedup-windowed.json`：窗口过滤和去重后的记录
- `cvss-filtered-enriched.json`：CVSS 过滤、项目身份定位和补充 GitHub 信息后的候选；应保留 `project_identity` 字段
- `project-identity-summary.json`：所有 `CVSS > 6.9` 候选的项目身份定位结果；每条至少包含 `id`、`project_identity.name`、`project_identity.stable_key`、`project_identity.confidence`、`project_identity.evidence`、`project_identity.source_kind`、`project_identity.reject_reason` 和最终定位状态
- `project-identity-evidence-gaps.json`：项目身份或重要性证据不足的自检候选；只收录 medium 置信度，以及少量 low / none 但具备高 CVSS、核心厂商 / 基金会、基础设施、医疗 / 工控 / 边界设备或供应链信号的条目
- `first-class-summary.json`：一级候选统计、低 star 过滤统计、入选候选摘要
- `filter-summary.json`：过滤流水线统计和高分候选摘要

过程文件可以包含公开漏洞数据，但不能写入未脱敏 token、API key 或其他 secret。最终回复只汇报输出目录、日报路径、manifest 路径和关键统计，不要贴出大段过程 JSON。如果用户追问某个 CVE 为什么没进日报，应优先查这些过程文件，而不是重新采集。

## 归并与排序

默认目标是找出最值得关注的漏洞情报，而不是只按严重度机械罗列。

### 基础价值信号

先基于这些证据化信号得到基础价值分：

- 使用人数和生态位置：GitHub stars、包下载量、包注册中心流行度、可靠厂商 / 基金会 / 官方项目定位、主流框架 / 基础库 / 浏览器 / Web server / Kubernetes / 云原生核心组件地位、CI/CD 核心链路、下游依赖规模
- 影响面：边界入口、默认暴露服务、管理面、认证系统、网关、防火墙、VPN、CI/CD、供应链路径、云原生控制面、开发工具链、主流基础设施
- 潜在暴露面：是否默认公网暴露，或常部署在管理后台、API gateway、VPN、防火墙、文档转换服务、自动化平台等入口位置
- 技术后果：远程代码执行、认证绕过、权限提升、沙箱逃逸、敏感凭据泄露、SQL 注入、任意文件读写删除
- 攻击前置条件：未认证问题优先于低权限认证问题，低权限认证问题优先于管理员或本地条件问题
- 资产相关性：命中用户资产关键字或可靠项目定位时提权
- 信息可信度：官方厂商公告、GitHub Advisory、NVD 等可靠来源增强可信度；时间不明确、严重度缺失、影响面无法确认或信息明显不完整时降权

最高权重信号是使用规模、生态位置、广泛下游依赖、可靠厂商 / 基金会 / 官方项目定位，以及核心基础设施、广泛部署的服务端基础设施、边界入口、默认暴露、管理面、认证系统、网关、CI/CD、云原生控制面等关键位置。GitHub stars 只是使用量证据之一，不是项目重要性的唯一证据。技术后果严重是重要信号，但不能绕过 CVSS 阈值或已确认低 star GitHub 项目的硬过滤。核心基础设施或广泛部署服务端基础设施只要来源可信、项目定位可靠，且 `CVSS > 6.9` 或严重度为 high / critical，应优先进入重点关注候选。

主流前端框架、路由框架、SSR / SSG 框架、React Server Components（RSC）相关运行时、server runtime、framework mode、构建 / hydration / manifest / redirect 处理链路，若具备高 GitHub stars、包注册表高下载量、广泛下游依赖、官方 / 基金会 / 大厂维护，或影响服务端渲染、服务端请求处理、重定向、鉴权边界、资源消耗、远程代码执行路径，应视为主流生态和潜在服务端入口证据。React Router、Remix、Next.js、Nuxt、Angular、Vue / Vue Router、SvelteKit、Astro 等项目满足这些证据时，不应因为“前端库”标签被降级。

CVSS 展示和排序优先使用 v4；没有 v4 时用 v3/v3.1，最后才使用源默认 `cvss`。格式保持紧凑，例如 `CVSSv4:7.7（v3.1:9.8）`、`CVSSv3.1:9.8`。

不要把 PoC、EXP、在野利用、active exploitation 或 KEV 状态作为排序提权信号；这些信息只能作为背景证据写入 References。没有 `CVE` 的记录也可以保留；可临时用 `title + source + url` 作为归并标识。

### `ranking_rules`

`ranking_rules` 允许用自然语言维护提权 / 降权规则，但执行时必须映射成固定效果值，而不是让 agent 自由发挥。

每条规则包含：

- `effect`
- `rule`

`effect` 只允许使用这些固定枚举：

- `strong_boost` = `+30`
- `boost` = `+15`
- `slight_boost` = `+5`
- `demote` = `-15`
- `strong_demote` = `-30`

规则命中时只能依据已有证据判断，例如 `title`、`description`、`tags`、`references`、GitHub / 简单搜索补充结果。不要使用“感觉更重要”“看起来影响很大”这类无法稳定判断的主观规则。自然语言规则应映射到上面的基础价值信号，例如项目重要性、技术后果、使用量、非生产 demo/lab/sample/test 降权、信息不完整降权。

若同一条记录同时命中提权和降权规则，可以叠加，但总规则分应限制在 `-40 ~ +40` 范围内。

### 执行顺序

排序阶段按这个顺序执行：

1. 基于基础价值信号计算基础价值分
2. 应用 `ranking_rules`
3. 应用 `project_filters`
4. 结合补充证据得到最终排序

### 项目身份定位

项目身份定位发生在 `CVSS > 6.9` 之后、`project_filters` 之前。目标是让 agent 主动尝试确认真实受影响项目 / 产品，而不是因为标题质量差就直接丢弃；但定位必须保守，不能猜测项目名、主页、仓库、stars 或下载量。

每条 `CVSS > 6.9` 候选都应写入 `project_identity`：

- `name`：当前确认的项目 / 产品名；无法确认时为空
- `stable_key`：用于归并的稳定标识，优先级为 GitHub 仓库、包生态 + 包名、CPE vendor/product、官方产品页 / 公告产品名，最后才是 high 置信度的规范化产品名
- `confidence`：只允许 `high` / `medium` / `low` / `none`
- `evidence`：支持该身份的证据数组，每项说明证据来源和值，例如 `ghsa_affected_package:npm/vite`、`github_repo:vitejs/vite`、`nvd_cpe:vendor/product`、`official_advisory_product:Cisco Unified Communications Manager`、`description_pattern:This issue affects ABB T-MAC Plus`
- `source_kind`：主要证据类型，例如 `ghsa_package`、`github_repo`、`nvd_cpe`、`official_advisory`、`description_pattern`、`title_pattern`
- `reject_reason`：无法 high 置信定位或不能进入正文时的原因

证据优先级固定：

1. GHSA `affected_packages`、`source_code_location`、repository advisory URL
2. GitHub repository URL，并在需要 stars 时通过 GitHub API 验证
3. NVD CPE vendor/product
4. 官方厂商公告、产品页、包注册表、Apache / Linux / 厂商邮件列表、oss-security 等可靠 reference
5. 描述或标题中的结构化模式：`This issue affects X`、`vulnerability in X`、`X before version Y`、`X prior to version Y`
6. 标题提取；仅当标题不是 CWE / 漏洞类型模板句、截断句或占位描述时可使用

置信度规则：

- `high`：至少一个强结构化证据，或两个独立中等证据一致；可继续进入一级候选筛选
- `medium`：能抽出合理项目 / 产品名，但只有单一弱证据或半结构化证据；不得进入重点正文，只能进入 `project-identity-evidence-gaps.json` 或自检
- `low`：抽取结果像漏洞类型、泛组件、截断标题、厂商泛称、CVE 句子或不稳定身份；不得进入正文
- `none`：没有可用项目 / 产品身份；不得进入正文

若证据冲突，优先相信结构化包名、CPE、仓库和官方产品名；无法选择稳定身份时降级为 `medium` 或 `low`。不要只因 NVD、GHSA、Patchstack、VulnCheck 等漏洞库来源可信就把项目身份升为 high。

定位质量自检可使用历史过程文件中的代表样本：`Cpanel::JSON::XS` 应能从 `X before version Y` 句式定位；`FreeIPMI` 应能从 NVD 描述和产品上下文定位；`ABB T-MAC Plus` 应能从 `This issue affects X` 定位；`SWUpdate` 应能从 `X before version Y` 定位；CWE 模板标题且无稳定产品证据的 GHSA 条目应保持 low / none；历史 GHSA 元数据同步条目不得仅因高 CVSS 进入正文。

### `project_filters`

`project_filters` 用于项目级硬过滤，顺序固定：

1. 先应用 CVSS 阈值：只有 `CVSS > 6.9` 的条目进入项目过滤。CVSS 优先使用 v4，其次 v3/v3.1，最后源默认 `cvss`。CVSS 阈值固定，不从配置读取。
2. 只有 `project_identity.confidence == high` 的条目才能进入项目重要性筛选；medium 置信度条目只能进入 `project-identity-evidence-gaps.json` 或自检，low / none 直接排除正文。
3. 对已确认 GitHub 项目地址且拿到 star 数据的条目应用 `min_github_stars`：低于阈值直接排除；达到阈值后仍需确认项目重要性证据。若仅满足 `min_github_stars`，但缺少企业部署、核心基础设施、主流框架 / 基础库、广泛下游依赖、边界入口、认证系统、网关、CI/CD、云原生控制面、可靠厂商 / 基金会 / 官方项目定位等证据，则直接排除，不进入一级候选。
4. 对没有 GitHub 仓库或无法确认 GitHub 仓库 / star 数的条目，不能只因为 NVD、GHSA、Patchstack、VulnCheck 或类似漏洞库来源可信就进入一级候选；还必须有厂商公告、官网产品页、包注册表高下载量、主流生态位置、企业部署、核心基础设施、边界入口、认证系统、网关、CI/CD、云原生控制面、可靠厂商 / 基金会 / 官方项目定位等独立项目重要性证据。
5. WordPress 插件、主题、CMS 扩展、单站点管理插件默认不进入一级候选；只有同时具备明确高安装量 / 高下载量、官方或知名厂商维护、企业广泛部署、主流安全 / 缓存 / 身份认证 / 支付 / 备份插件定位，或用户资产关键字命中时，才可进入一级候选。仅有 CVSS 9.x、RCE、SQL 注入、认证绕过、Patchstack / GHSA / NVD 来源不足以入选。
6. 若项目身份定位结果为 `low` 或 `none`，或项目名仍呈现为漏洞类型句、截断标题、厂商泛称、泛组件名或占位描述（例如以 `Improper...`、`Incorrect...`、`The ... is vulnerable...`、`Allocation of Resources...` 开头），该条目不得写入日报正文；只能保留在过程文件中。若 CVSS 很高或命中核心厂商 / 基金会 / 基础设施 / 医疗 / 工控 / 边界设备信号，应写入 `project-identity-evidence-gaps.json` 或自检，并说明缺少哪些身份定位证据。
7. 对历史 CVE（CVE 年份早于本次统计年份）的 GHSA / NVD 更新时间命中项，默认不进入重点关注或持续关注；只有存在本次窗口内的新厂商公告、新补丁版本、新受影响版本、新主流项目关联、或用户资产关键字命中时，才可写入正文。仅因 GitHub Advisory 重新同步、元数据更新、引用更新或旧漏洞库记录更新时间变化，不应写入正文。
8. AI / ML / LLM / MCP 工具、模型加载库、研究框架、个人效率工具或开发辅助工具，若 GitHub stars 很高，可将高 stars 作为足够的项目重要性证据，并在漏洞本身高价值、项目身份 high 置信且无其他硬过滤原因时进入重点关注或持续关注。若 stars 仅达到最低阈值或无法确认 stars，则仍需额外具备企业生产服务端部署、模型 serving / inference 服务、云端托管入口、供应链广泛下游依赖、主流框架 / 基础库地位、官方 / 基金会 / 大厂维护，或用户资产关键字命中等证据。不能仅凭 RCE / 反序列化、HuggingFace / pickle / torch.load 关键词进入重点关注。
9. 主流前端 / 全栈框架和路由框架的 SSR、RSC、server runtime、framework mode、manifest、redirect、loader/action、hydration 或服务端请求处理漏洞，若 GitHub stars 达标且具备广泛生态使用证据，应通过项目重要性筛选。若同一项目在窗口内有多条 `CVSS > 6.9` 记录，必须按稳定仓库或包名聚合成项目组，并至少进入重点关注、需要持续关注或自检；只有在利用前提极窄、仅影响实验性 API、仅导致客户端局部影响且无服务端 / 认证 / 资源耗尽风险时，才可正文层省略，并必须记录省略原因。

`CVSS <= 6.9` 的条目、已确认低 star GitHub 项目、仅满足 `min_github_stars` 但缺少上述项目重要性证据的 GitHub 项目、缺少独立项目重要性证据的非 GitHub 条目、小众 WordPress / CMS 插件条目，以及项目名抽取失败的条目，不应出现在重点关注、同时公开的漏洞、需要持续关注、过滤说明、脚注或附录中；日报正文也不要列举被过滤项目示例。对没有 GitHub 仓库或 star 数的核心基础设施、广泛部署服务端基础设施、边界入口、认证系统、网关、代理、解析服务、控制面等条目，只要非 GitHub 证据能确认可靠项目定位和重要性证据，就不能因为缺少 GitHub stars 降级或省略。

### `reporting`

`reporting` 控制日报分层阈值：

- `high_value_threshold`：达到该最终价值分的漏洞可进入重点项目组；同一项目的其他满足项目过滤要求的漏洞合并到该项目组下。正文不再要求展开所有一级候选，应优先保留企业基础设施、核心生态、广泛部署服务端、边界入口、认证系统、网关、CI/CD、云原生控制面、可靠厂商 / 基金会 / 官方项目，以及明确高使用量项目。
- `watch_threshold`：用于判断非重点项目是否进入“需要持续关注”；只有项目重要性证据明确但影响面、利用前提、使用量或可信度仍需确认的条目进入该栏。小众插件、项目名抽取失败、仅靠 CVSS / 严重后果 / 漏洞库来源的条目不得进入“需要持续关注”。同一重点项目下本次查询中满足项目过滤要求的漏洞可保留在该项目组的“同时公开的漏洞”无序列表中，但不要求写入所有一级候选。

日报正文按项目/产品聚合，不按单个漏洞全局排名罗列，也不要输出“排名”列或全局排序表。同一项目有多个满足过滤要求的漏洞时，正文只详写综合价值最高、影响最大的首要漏洞，其他漏洞放入该项目组的“同时公开的漏洞”无序列表；不要因为同项目漏洞价值较低就省略。

归并同一项目时，必须以稳定项目标识为准：同一 GitHub 仓库、同一包名、同一厂商产品页、同一官方公告系列，或明确同一产品 / 组件名称。不要只因为标题中出现相同框架、语言、厂商名、CVE 引用、漏洞类型或生态关键词就合并。若同一组里混入不同包名、不同仓库或不同厂商产品，应拆成独立项目组；无法确认时宁可分开，不要误归并。

### 重点项补充信息

对进入“重点关注的项目/漏洞”的项目组，必须输出 `受影响项目` 行，用于说明当前能确认的项目 / 产品定位。

`受影响项目` 行按证据类型选择格式：

- 有 GitHub 仓库时：`受影响项目：[项目名](GitHub URL)（Stars：1234，项目介绍：一句话介绍）`
- 无 GitHub 仓库但有可靠项目地址时：`受影响项目：[项目名](可靠项目地址)（定位依据：厂商公告 / 官网 / 产品文档 / 包注册表 / 邮件列表 / oss-security；项目说明：一句话说明）`
- 没有可链接项目地址时：`受影响项目：项目名（暂无可确认项目主页；定位依据：NVD 描述 / 厂商名 / 产品名 / 公告来源；仍需确认官网、包注册表或厂商页面）`

没有可靠项目地址时也不要省略 `受影响项目` 行；用一句话说明当前可确认的项目 / 产品定位和缺失的信息。不要猜测链接、star 数或下载量。
这些信息用于帮助读者理解影响面、使用规模和潜在暴露面，不应使用 PoC / EXP / 在野利用 / KEV 状态作为排序原因。

## 回退规则

- 某个源脚本在按重试规则执行后仍失败：记录失败项并继续后续源
- 某个源脚本输出不是 JSON 数组：记录失败项并继续后续源
- NVD 脚本未能拿到可用漏洞详情时，应记录 NVD 脚本结果并使用 NVD recent feed 按 `published` 时间过滤补充，不能把脚本 0 条当作真实无数据
- 某条记录缺少 `time`，或无法确认是否命中窗口：不要把它当作明确命中窗口的数据
- 某条记录缺少严重度：可降级到“需要持续关注”
- 信息不足时，优先降低结论强度，不要补造 `CVE`、`CVSS`、严重度或 PoC/EXP 结论

## 日报格式

```markdown
# 今日漏洞情报

统计范围：start ~ end

统计摘要：

- 采集与过滤：原始记录 X 条 → 窗口过滤与去重 N 条 → CVSS > 6.9 高分候选 Xcvss 条。
- 项目身份定位：high 置信度 Xid 条；medium 身份证据不足 Xmed 条；low / none Xlowid 条；低 stars GitHub 项目过滤 XstarLow 条。
- 入选来源：GitHub Stars 达标且项目重要性充分 X2 条；非 GitHub 但项目定位可靠 X3 条；项目重要性筛选后 X1 条；未入正文的证据不足候选 Kid 条。
- 正文结构：重点项目组 A 个；同项目同时公开漏洞 B 条；持续关注 C 条；正文层省略 D 条。

## 重点关注目录

- [项目名：中文转述的首要漏洞标题（CVE-2026-0000）](#vuln-1)
- [项目名：中文转述的首要漏洞标题（GHSA-xxxx）](#vuln-2)

## 重点关注的项目/漏洞

<a id="vuln-1"></a>

### 项目名：中文转述的首要漏洞标题（CVE-2026-0000）
- 严重度：
- 关注原因：
- 简述：
- 利用前提：
- 影响面/使用量：
- 受影响项目：[项目名](项目地址)（Stars：1234，项目介绍：一句话介绍；或定位依据：厂商公告 / 官网 / 产品文档；无可链接地址时说明暂无可确认项目主页和当前定位依据）
- References：
- 同时公开的漏洞：
  - CVE-2026-0001：中文转述标题；严重度 / CVSS；一句话说明影响；URL：https://github.com/advisories/...
  - GHSA-xxxx：中文转述标题；严重度；一句话说明影响；URL：https://github.com/advisories/...

## 需要持续关注
- 项目名 / 产品名：中文转述标题（CVE-2026-0000）；一句话概括；说明还缺少哪些影响面、使用量或可信度信息

## 自检：可能漏筛的高价值候选
- 项目名 / 产品名（CVE-2026-0000）：一句话风险概括；漏筛原因：说明为什么该条目可能被当前规则过滤、降级或未写入正文；我认为：给出对该漏洞是否应资产命中时提升、加入持续关注或保持过滤的判断。

## 漏洞采集情况

### 脚本执行情况
- source-name：执行成功，返回 N 条记录
- source-name：执行失败，原因：失败原因
- nvd：脚本未拿到可用漏洞详情；已使用 nvd-recent-feed 按发布时间窗口补充 M 条记录
```

日报写作约束：

- 开头摘要必须使用分组条目而不是单行长句，并清楚区分四层含义：采集与 CVSS 高分候选、项目身份定位、入选来源、正文结构。不要把 CVSS 高分候选数写成最终筛选结果，也不要把 GitHub Stars 硬过滤后的中间数量呈现为“筛选完成”数量。项目身份定位必须统计 high、medium、low / none 和低 stars GitHub 过滤数量；入选来源必须统计 GitHub Stars 达标且项目重要性充分的条目、非 GitHub 但项目定位可靠的条目、项目重要性筛选后条目和未入正文的证据不足候选条目。正文结构必须统计重点项目组、同项目同时公开漏洞、需要持续关注和正文层省略；“漏洞采集情况”只保留脚本执行情况。
- `重点关注目录` 必须位于统计摘要之后、`重点关注的项目/漏洞` 之前；目录只列后续在 `重点关注的项目/漏洞` 中完整展开的条目标题，顺序与正文一致，不列 `需要持续关注`、`自检` 或 `同时公开的漏洞` 项。目录项必须写成 Markdown 链接，链接到对应重点条目前的稳定锚点，例如 `[标题](#vuln-1)`。
- 每个 `重点关注的项目/漏洞` 条目前必须放置一个稳定 HTML 锚点，按正文顺序命名为 `<a id="vuln-1"></a>`、`<a id="vuln-2"></a>`，目录链接必须与这些锚点一一对应。
- 重点关注项目组字段顺序固定：`严重度` → `关注原因` → `简述` → `利用前提` → `影响面/使用量` → `受影响项目` → `References` → `同时公开的漏洞`（可选）。正文不要输出 `综合价值`、`潜在暴露面`、全局排名或全局 `Sources:` 段。
- `受影响项目` 必填；没有项目地址时也必须说明当前定位依据和缺失信息。不要猜测仓库地址、star 数或下载量。
- 漏洞标题要用中文转述，不要直接复制 GHSA/NVD 原始标题；保留 CVE/GHSA ID，不使用“相关组件”“未知应用”这类占位词。
- 非 GitHub 项目只要项目定位可靠，并且影响面、使用量、厂商可信度、生态位置或资产相关性证据足够，就与 GitHub Stars 达标项目同级进入一级候选，也可以进入重点关注；核心基础设施或广泛部署服务端基础设施若来源可信、项目定位可靠，且 `CVSS > 6.9` 或严重度为 high / critical，应优先写入“重点关注”，不能只因篇幅、排序或缺少 GitHub stars 省略；若同时缺少可靠项目定位和重要性证据，放入“需要持续关注”并说明缺失信息。
- 主流前端 / 全栈框架和路由框架（例如 React Router、Remix、Next.js、Nuxt、Angular、Vue / Vue Router、SvelteKit、Astro）若命中 SSR、RSC、server runtime、framework mode、manifest、redirect、loader/action、hydration、服务端请求处理、资源耗尽或条件 RCE，应按主流生态项目处理。高 stars / 高下载量 / 广泛下游依赖且无硬过滤原因的同项目多漏洞，不能在正文中完全消失；至少写入重点关注、需要持续关注或自检。
- GitHub Stars 达到 `min_github_stars` 只是进入候选池的必要条件之一，不是充分条件；若 GitHub 项目仅刚达到阈值，且没有企业部署、核心基础设施、主流框架 / 基础库、广泛下游依赖、边界入口、认证系统、网关、CI/CD、云原生控制面、可靠厂商 / 基金会 / 官方项目定位等项目重要性证据，应直接过滤，不写入正文任何部分。
- 技术后果严重和 CVSS 高分只能作为风险信号，不能单独构成写入正文的理由；正文条目必须同时具备可靠项目定位和足够项目重要性证据。
- WordPress 插件、主题、CMS 扩展、单站点管理插件默认过滤；只有明确高安装量 / 高下载量、官方或知名厂商维护、企业广泛部署、主流安全 / 缓存 / 身份认证 / 支付 / 备份插件定位，或用户资产关键字命中时，才可写入正文。
- 项目名若仍是漏洞类型句、截断标题或占位描述，不得写入正文；必须先从公告、包名、产品页或仓库信息中确认真实项目 / 产品名，并用清晰中文标题表达。
- 若记录来自 Red Hat、IBM、Microsoft、GitHub Enterprise、Jenkins、OpenStack、Kubernetes、Apache、Linux 发行版等厂商 / 基金会公告，应优先从公告标题、受影响包名、产品字段、CPE、厂商安全页或 references 中提取真实产品名；不要用 CVE ID、漏洞类型、厂商名泛称或公告句子作为项目名。无法确认真实产品名时，该条目只保留在过程文件中。
- 日报正文不设置固定数量上限，也不因为篇幅省略足够高价值的候选；但一级候选不等于必须写入正文。一级候选可完整保存在过程文件中，正文只写真正具备项目重要性和影响证据、值得人工阅读的重点项目组和持续关注条目。摘要中的“未写入正文”应体现一级候选中被正文层筛掉的数量。
- `自检：可能漏筛的高价值候选` 只列未进入重点关注、同时公开漏洞或需要持续关注正文的少量候选，优先选择 CVSS 高且命中核心厂商 / 基金会 / 基础设施、医疗 / 工控 / 边界设备 / 身份认证 / 供应链组件、GitHub stars 很高但前置条件较窄、或非 GitHub 但有厂商公告 / 官方产品页的条目。每条格式固定为：`项目名 / 产品名（CVE-2026-0000）：一句话风险概括；漏筛原因：xxx；我认为：xxx。` 自检模块用于说明自动筛选可能漏掉的高价值候选和资产命中时的处理建议，不要把已确认应硬过滤的小众插件、项目名抽取失败条目或历史元数据同步噪声列入其中。
- `References` 下直接列出首要漏洞原始 URL，不写成 Markdown 链接；“同时公开的漏洞”每条只附 1 个 URL，优先 GHSA，其次 NVD、厂商公告或项目公告。搜索得到的可靠链接应合并到对应漏洞的 `References`。

