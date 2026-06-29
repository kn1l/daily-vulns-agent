# Daily Vulns Agent v1 Design

## 目标

构建一个漏洞日报 agent，复用已有日报生成 skill，完成从报告生成到 HTML 发布、钉钉推送、Web 定时调度的端到端流程。

第一版先以 CLI 主流程为核心，Web 只负责登录后的调度配置和状态展示。

## 总体架构

```text
已有日报 skill
  -> 生成 report.md + manifest.json

CLI
  -> generate
  -> publish
  -> notify
  -> run = generate + publish + notify

FastAPI Web
  -> 登录鉴权
  -> 调度配置
  -> APScheduler 定时 subprocess 调 CLI run
  -> 仪表盘展示调度状态
  -> /reports/ 托管 public/reports/
  -> /assets/ 托管 public/assets/
```

## 目录结构

```text
config.example.yaml
config.yaml
requirements.txt
prompts/
  daily_report.md
skills/
  daily-vulns/
    SKILL.md
    config.yaml
    scripts/
runs/
  2026-06-25_090000/
    skills -> <project>/skills
    manifest.json
    report.md
    report.html
    publish.json
    notify.json
    run.log
public/
  assets/
    style.css
  reports/
    2026-06-25_090000/
      index.html
      meta.json
    index.html
state/
  scheduler_state.json
  scheduler_runs/
    2026-06-25_090000.log
src/
  daily_vulns_agent/
    core.py
    cli.py
    web.py
    assets/
      style.css
    templates/
      admin/
```

## manifest.json 契约

由已有日报 skill 生成，写入当前工作目录。

```json
{
  "title": "每日漏洞情报日报 - 2026-06-25",
  "generated_at": "2026-06-25T09:00:00+08:00",
  "md_path": "report.md",
  "highlights": [
    "漏洞条目 1",
    "漏洞条目 2"
  ]
}
```

规则：

- `title` 必填，缺失直接失败。
- `generated_at` 必须是 ISO 8601 字符串，解析失败直接失败。
- `md_path` 必须是 run_dir 内的相对路径，禁止绝对路径和 `..` 路径穿越。
- `highlights` 必须是字符串数组；为空时 `publish` 允许，`notify` 失败。
- `report.md` 不存在或内容为空时，`publish` 直接失败。

## CLI 命令

```bash
python -m daily_vulns_agent.cli --config config.yaml run
python -m daily_vulns_agent.cli --config config.yaml generate
python -m daily_vulns_agent.cli --config config.yaml publish --run-dir runs/2026-06-25_090000
python -m daily_vulns_agent.cli --config config.yaml notify --run-dir runs/2026-06-25_090000
```

CLI 使用 `argparse`。

配置文件默认读取 `./config.yaml`，所有命令支持 `--config` 覆盖。

相对路径都相对于 `config.yaml` 所在目录解析。

## generate

职责：

- 创建新的 `runs/<timestamp>/` 目录。
- `timestamp` 精确到秒，例如 `2026-06-25_090000`。
- 如果目标 run_dir 已存在，直接失败，不自动覆盖。
- 以 run_dir 作为 Claude/Codex CLI 的工作目录。
- 在 run_dir 内创建 `skills -> <project>/skills` 软链接，并注入指向 run_dir 内 `skills/daily-vulns` 的 `CLAUDE_SKILL_DIR`，让 prompt 中的 `skills/daily-vulns/SKILL.md` 相对于当前工作目录可用。
- prompt 文件负责明确要求日报 agent 直接在当前工作目录生成 `report.md` 和 `manifest.json`，不要再创建额外输出子目录。
- 读取 prompt 文件内容作为 `{prompt}`，不额外拼接其他内容。
- 调用 `generation.agent` 选中的 `generation.agents.<agent>.command_template` argv 模板。
- `base_url`、`model` 可作为模板变量使用，并注入子进程环境变量。
- `api_key` 只注入子进程环境变量，不写入命令行。
- Codex agent 如果配置了 `base_url`、`api_key` 或 `model`，自动在 `{prompt}` 前追加 Codex 官方覆盖参数：`--ignore-user-config`、`--model <model>`，以及基于自定义 provider `daily_vulns_openai` 的 `-c model_provider="daily_vulns_openai"`、`-c model_providers.daily_vulns_openai.base_url="<base_url>"`、`-c model_providers.daily_vulns_openai.env_key="OPENAI_API_KEY"`，避免宿主机 `~/.codex/config.toml` 覆盖项目配置，也避免覆盖 Codex 内置 provider。
- 子进程 stdin 使用 `DEVNULL`，避免模型 CLI 自动更新、登录过期或交互确认时等待人工输入。
- `generation.timeout_seconds` 控制生成超时，默认 3600 秒；超时写入 `run.log` 并让命令失败。
- 捕获 Claude/Codex stdout/stderr 到 `run.log`。
- 终端只输出简洁状态。

## publish

职责：

- 读取并校验 `manifest.json`。
- 读取 `md_path` 指向的 Markdown。
- 使用 `markdown-it-py` 渲染 Markdown。
- 页面正文只显示 Markdown 渲染结果，不额外插入 manifest 标题或时间。
- HTML `<title>` 使用 `manifest.title`。
- 先生成 `runs/<run>/report.html`。
- 再复制为 `public/reports/<run>/index.html`。
- 从项目内置 `assets/style.css` 同步到 `public/assets/style.css`。
- 写入 `runs/<run>/publish.json`，其中只持久化报告路由 `route`，不持久化完整站点 URL。
- 由 `publish.json` 转换生成 `public/reports/<run>/meta.json`。
- 重建 `public/reports/index.html`。

`publish.json`：

```json
{
  "run_dir": "runs/2026-06-25_090000",
  "public_dir": "public/reports/2026-06-25_090000",
  "route": "/reports/2026-06-25_090000/",
  "published_at": "2026-06-25T09:02:00+08:00",
  "notify_status": "pending"
}
```

`meta.json`：

```json
{
  "title": "每日漏洞情报日报 - 2026-06-25",
  "generated_at": "2026-06-25T09:00:00+08:00",
  "route": "/reports/2026-06-25_090000/",
  "notify_status": "pending"
}
```

公开列表页 `/reports/index.html`：

- 扫描 `public/reports/*/meta.json`。
- 显示标题链接、生成时间、推送状态。
- 按 `generated_at` 倒序。
- 和单篇报告共用 CSS。
- 页面和通知中的时间统一转换为 UTC+8 展示。

## notify

职责：

- 读取 `manifest.json` 和 `publish.json`。
- 如果 `notify.json` 已经是 success，默认拒绝重复推送；后续可通过 force 机制扩展。
- 钉钉消息类型使用 Markdown。
- 标题使用 `manifest.title`。
- 正文包含：标题、生成时间、总条数、前 5 条 highlights、阅读全文链接。
- 阅读全文链接由 `publish.json.route` 和当前配置的 `publish.site_base_url` 拼接生成。
- 不 @ 人。
- 支持钉钉 webhook，secret 可选。
- HTTP 请求失败自动重试 1 次。
- 失败时写 `notify.json` 为 failed，更新 public `meta.json` 的 `notify_status=failed`，重建列表页，命令返回非零。
- 成功时写 `notify.json` 为 success，更新 `publish.json` 和 public `meta.json` 的 `notify_status=success`，重建列表页。
- notify 成功后触发报告清理。

`notify.json`：

```json
{
  "run_dir": "runs/2026-06-25_090000",
  "url": "https://example.com/reports/2026-06-25_090000/",
  "payload": {},
  "status": "success",
  "response": {},
  "notified_at": "2026-06-25T09:03:00+08:00",
  "attempts": 1
}
```

## run

`run = generate + publish + notify`。

规则：

- 任一关键步骤失败，`run` 返回非零。
- 已生成的产物不删除。
- 如果 `notify` 失败，终端打印完整重试信息：run_dir、报告 URL、日志路径、可复制的 notify 重试命令。

## 清理策略

- 保留最近 100 篇 public 报告。
- 所有 public 报告都计入数量，包括 pending、success、failed。
- 按 `meta.json.generated_at` 排序。
- 清理触发时机：notify 成功后。
- 删除最旧 public 报告目录时，同步删除同名 `runs/<run>/`。

## config.yaml 结构

配置按功能分组：

```yaml
generation:
  agent: codex
  prompt_file: prompts/daily_report.md
  timeout_seconds: 3600
  agents:
    claude:
      base_url: ""
      api_key: ""
      model: ""
      command_template:
        - claude
        - --print
        - "{prompt}"
        - --dangerously-skip-permissions
    codex:
      base_url: ""
      api_key: ""
      model: ""
      command_template:
        - codex
        - exec
        - --dangerously-bypass-approvals-and-sandbox
        - "{prompt}"

publish:
  site_base_url: "http://localhost:8000"
  runs_dir: runs
  public_dir: public
  reports_path: /reports

dingtalk:
  webhook: "https://oapi.dingtalk.com/robot/send?..."
  secret: ""

web:
  admin:
    username: admin
    password: change-me
    session_secret: change-me-session-secret
  run_command:
    - python
    - -m
    - daily_vulns_agent.cli
    - --config
    - "{config_path}"
    - run

schedule:
  enabled: true
  timezone: Asia/Shanghai
  times:
    - "09:00"
    - "18:00"
```

YAML 使用 PyYAML。Web 写回配置时保留未知字段的值，但不保证保留注释、顺序和原始格式。

## Web 管理后台

技术：

- FastAPI
- Jinja2 服务端模板
- APScheduler
- Session Cookie 登录

认证：

- 账号密码明文保存在 `config.yaml`。
- Session 默认 7 天。
- Cookie 使用 HttpOnly、SameSite=Lax。
- 密码不写入日志或响应。

页面范围：

- 登录页
- 仪表盘：启用状态、下次运行时间、最近成功/失败
- 调度配置：启用/停用、多个 HH:MM、时区
- 站点配置：site_base_url
- 钉钉配置：webhook、secret；已有 secret 在响应中替换为等长 `*` 掩码，提交时如果保持原掩码不变则保留旧值，改成其他内容则覆盖。
- Agent 配置：默认 agent、Base URL、API Key、模型名、Claude/Codex command_template；已有 API Key 在响应中替换为等长 `*` 掩码，提交时如果保持原掩码不变则保留旧值，改成其他内容则覆盖。

不做：

- Web 手动触发
- Web 日志页
- 后台报告列表
- 编辑 prompt_file、admin 密码

secret 显示和提交规则：

- webhook 明文回显。
- 钉钉 secret 和 Agent API Key 不在响应中回传原文，只返回等长 `*` 掩码。
- 输入框保持可编辑。
- 提交值如果等于已有值长度的全 `*` 掩码，则保留旧 secret / API Key。
- 提交值如果不同于旧掩码，则用提交值覆盖；提交空值表示清空。

## 调度

- Web 使用 APScheduler。
- 支持每天多个 HH:MM 时间点。
- 默认时区 `Asia/Shanghai`。
- 到点执行完整 `run`。
- Web 通过 subprocess 调用配置中的 `web.run_command`。
- 允许并发 run。
- run_dir 精确到秒；如果同秒冲突，后启动的 run 失败。
- Web 维护内存任务表，同时写 `state/scheduler_state.json`。
- `next_runs` 在 Web 启动、调度配置保存重载、以及调度任务触发后重新计算并写入状态文件。
- FastAPI 重启后只恢复历史状态，不恢复旧进行中任务。

`scheduler_state.json` 保存：

- enabled
- next_runs
- last_run
- last_success
- last_failure
- scheduler log 路径

Web 子进程 stdout/stderr 写到：

```text
state/scheduler_runs/<timestamp>.log
```

调度日志保留最近 30 个。

## 部署

第一版推荐部署方式为宿主机直接运行 FastAPI Web + APScheduler。

服务：

- `web`：FastAPI + APScheduler，运行 `daily_vulns_agent.web:app`，启动时自动初始化 `state/`、`state/scheduler_runs/`、`runs/`、`public/reports/` 和 `public/assets/`。

对外路径：

- `/reports/` 由 FastAPI 静态托管 `public/reports/`
- `/assets/` 由 FastAPI 静态托管 `public/assets/`
- `/admin/` 由 FastAPI 提供管理后台

宿主机 Web 读写：

- `./config.yaml`
- `./prompts`
- `./skills`
- `./runs`
- `./public`
- `./state`

`runs/`、`public/`、`state/` 是运行产物目录，可以写入 `.gitignore`；应用启动时会自动创建所需子目录。

宿主机 CLI 和 FastAPI Web 调度读写同一批宿主机目录。`config.yaml` 中的相对路径相对于项目根目录解析。Web 调度调用宿主机上的 Claude/Codex CLI，因此可以复用宿主机已安装、登录和配置好的 agent CLI。

宿主机 Web 启动：

```bash
PYTHONPATH=src uv run uvicorn daily_vulns_agent.web:app --host 127.0.0.1 --port 8000
```

如果要让 FastAPI 直接对外提供访问：

```bash
PYTHONPATH=src uv run uvicorn daily_vulns_agent.web:app --host 0.0.0.0 --port 8000
```

默认本机端口：

```text
http://localhost:8000/reports/
http://localhost:8000/admin/
```

API 可以公网开放，但必须登录鉴权。不强制校验 HTTPS。

## 本地验收

基础检查：

```bash
PYTHONPATH=src uv run python -m py_compile src/daily_vulns_agent/*.py
```

publish 验收手工创建：

```text
runs/test-run/
  manifest.json
  report.md
```

执行：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli --config config.yaml publish --run-dir runs/test-run
```

验收：

- `runs/test-run/report.html` 存在。
- `runs/test-run/publish.json` 存在，包含 `route`，不包含持久化完整站点 URL。
- `public/reports/test-run/index.html` 存在。
- `public/reports/test-run/meta.json` 存在，包含 `route` 和 pending 状态。
- `public/reports/index.html` 列出该报告和 pending 状态。
- `public/assets/style.css` 从项目内置 CSS 同步过去。

生成链路验收：

- `generate` 启动外部模型 CLI 前先创建新的 `runs/<timestamp>/`。
- run 目录内存在 `skills -> <project>/skills` 软链接。
- 子进程环境包含指向 run 目录内 `skills/daily-vulns` 的 `CLAUDE_SKILL_DIR`。
- prompt 要求直接在当前工作目录写入 `report.md` 和 `manifest.json`。