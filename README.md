# Daily Vulns Agent

Daily Vulns Agent 复用项目内的漏洞日报生成 skill，提供从情报生成、HTML 发布、钉钉推送到 Web 定时调度的完整流程。

## 功能

- CLI 全链路：`generate` → `publish` → `notify`
- Markdown 报告发布为静态 HTML
- FastAPI 直接托管 `/reports/` 报告和 `/assets/` 静态资源
- 钉钉 Markdown 机器人推送
- FastAPI Web 管理后台：登录、调度、站点、钉钉、Agent 配置、文件系统容量监控
- APScheduler 定时执行日报生成
- 推荐部署：宿主机用 tmux 运行一个 FastAPI Web 进程

## 目录结构

```text
config.example.yaml      # 示例配置
config.yaml              # 主配置，本地运行前由示例复制生成
prompts/                 # 日报生成 prompt
skills/                  # 项目内日报生成 skill
runs/                    # 每次运行的内部产物
public/                  # FastAPI 对外托管的静态报告和资源
scripts/                 # 部署辅助脚本
src/daily_vulns_agent/   # Python 源码
```

一次运行的主要产物：

```text
runs/2026-06-25_090000/
  skills -> <project>/skills
  manifest.json
  report.md
  report.html
  publish.json
  notify.json
  run.log

public/reports/2026-06-25_090000/
  index.html
  meta.json
```

`runs/`、`public/`、`state/` 是运行产物目录，可以保留在 `.gitignore` 中；CLI 和 Web 启动时会按需创建子目录。

## 快速开始

```bash
uv venv
uv pip install -r requirements.txt
cp config.example.yaml config.yaml
cp skills/daily-vulns/config.example.yaml skills/daily-vulns/config.yaml
```

如果要执行 `generate` 或完整 `run`，还需要确保宿主机已经安装并登录对应模型 CLI：

```bash
claude --version
# 或
codex --version
```

只测试 `publish` / `notify` 时不需要 Claude/Codex CLI。

## CLI 使用

默认读取当前目录的 `config.yaml`：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli <command>
```

也可以显式指定配置文件。`--config` 放在子命令前后都支持，推荐放在子命令前：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli --config path/to/config.yaml <command>
PYTHONPATH=src uv run python -m daily_vulns_agent.cli <command> --config path/to/config.yaml
```

完整链路：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli run
```

分步执行：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli generate
PYTHONPATH=src uv run python -m daily_vulns_agent.cli publish --run-dir runs/2026-06-25_090000
PYTHONPATH=src uv run python -m daily_vulns_agent.cli notify --run-dir runs/2026-06-25_090000
```

重复推送：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli notify --run-dir runs/2026-06-25_090000 --force-notify
```

## Web 部署

推荐用 tmux 在宿主机运行 FastAPI Web。Web 进程同时负责：

- `/admin/`：管理后台
- `/reports/`：静态报告列表和单篇报告
- `/assets/`：静态资源
- 定时调度：到点执行配置里的 `web.run_command`

启动 Web：

```bash
./scripts/start-web.sh
```

脚本默认创建 `daily-vulns-web` tmux 会话，并监听 `0.0.0.0:8000`。如果要改端口：

```bash
DAILY_VULNS_PORT=8080 ./scripts/start-web.sh
```

如果要监听 80 端口：

```bash
DAILY_VULNS_PORT=80 ./scripts/start-web.sh
```

80 是特权端口，普通用户可能没有权限直接监听。推荐优先使用 8000/8080；如果必须使用 80，可以用系统能力授权、端口转发，或用反代把 80 转到应用端口。

如果前面还有反代，想只监听本机回环地址：

```bash
DAILY_VULNS_HOST=127.0.0.1 DAILY_VULNS_PORT=8000 ./scripts/start-web.sh
```

重新进入 tmux 会话：

```bash
tmux attach -t daily-vulns-web
```

如果服务器重启后需要自动恢复，可以加入当前用户的 crontab：

```bash
crontab -e
```

添加一行，路径改成实际项目路径：

```cron
@reboot /path/to/daily-vulns-agent/scripts/start-web.sh >> /path/to/daily-vulns-agent/state/web-start.log 2>&1
```

如果要指定监听地址或端口：

```cron
@reboot DAILY_VULNS_HOST=0.0.0.0 DAILY_VULNS_PORT=8000 /path/to/daily-vulns-agent/scripts/start-web.sh >> /path/to/daily-vulns-agent/state/web-start.log 2>&1
```

默认访问：

```text
http://localhost:8000/reports/
http://localhost:8000/admin/
```

如果监听 80 端口：

```text
http://localhost/reports/
http://localhost/admin/
```

默认账号密码来自 `config.yaml`：

```yaml
web:
  admin:
    username: admin
    password: change-me
```

如果不在项目根目录启动，或想使用其他配置文件，再显式设置：

```bash
DAILY_VULNS_CONFIG=/path/to/config.yaml
```

## 关键配置

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
  webhook: ""
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
```

`publish.site_base_url` 应设置为 FastAPI 对外可访问的站点根 URL。`publish.json` 只保存报告路由，`notify` 会用当前配置里的 `site_base_url` 拼接阅读全文链接。页面和通知中的时间统一转换为 UTC+8 展示。

配置页拆分为：

- 站点配置：`site_base_url`
- 钉钉配置：`webhook`、`secret`
- Agent 配置：默认 agent、Base URL、API Key、模型名、命令模板
- 调度配置：启用状态、时区、每日运行时间

已有 API Key 和钉钉 secret 在前端响应中只返回等长 `*` 掩码，不返回原文。提交时如果保持原掩码不变则保留旧值；改成其他内容则覆盖；提交空值表示清空。

## 模型 CLI 行为

`generate` 和 `run` 会按 `generation.agent` 选择模型 CLI，并使用对应的 `command_template` 调用外部命令。

`generate` 的关键行为：

- 每次先创建新的 `runs/<timestamp>/` 目录
- 以该 run 目录作为模型 CLI 的当前工作目录
- 直接读取 `generation.prompt_file` 的内容作为 `{prompt}`
- 在 run 目录里创建 `skills -> <project>/skills` 软链接
- 注入 `CLAUDE_SKILL_DIR=skills/daily-vulns`
- 捕获模型 CLI 输出到 `runs/<timestamp>/run.log`
- `api_key` 只注入环境变量，不写入命令行

每个 agent 的 `base_url` 和 `model` 会作为模板变量和环境变量注入子进程；`api_key` 只通过环境变量注入。

Codex agent 如果配置了 `base_url`、`api_key` 或 `model`，会自动在 `{prompt}` 前追加 Codex 官方覆盖参数：

- `--ignore-user-config`
- `--model <model>`
- `-c model_provider="daily_vulns_openai"`
- `-c model_providers.daily_vulns_openai.base_url="<base_url>"`
- `-c model_providers.daily_vulns_openai.env_key="OPENAI_API_KEY"`

这样项目配置可以覆盖宿主机 `~/.codex/config.toml`，且不会覆盖 Codex 内置 provider。API Key 仍通过 `OPENAI_API_KEY` 环境变量注入。

## Skill 配置

日报生成 skill 位于：

```text
skills/daily-vulns/
```

skill 还有独立配置文件：

```text
skills/daily-vulns/config.yaml
```

如果需要从示例重新生成：

```bash
cp skills/daily-vulns/config.example.yaml skills/daily-vulns/config.yaml
```

主要字段：

- `source_configs.ghsa.token`：GitHub/GHSA token，可提升 API 限额；建议部署时在服务器本地配置，不要提交真实 token。

`generate` 会在每个 run 目录里创建 `skills -> <project>/skills` 软链接，并设置 `CLAUDE_SKILL_DIR` 指向 `skills/daily-vulns`，所以 skill 内脚本可以使用 `${CLAUDE_SKILL_DIR}` 定位自身目录。

## manifest.json 契约

日报生成 skill 必须在当前工作目录生成：

```text
manifest.json
report.md
```

`manifest.json` 示例：

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

- `title` 必填
- `generated_at` 必须是 ISO 8601 字符串
- `md_path` 必须是 run 目录内的相对路径，禁止绝对路径和 `..`
- `highlights` 必须是字符串数组；为空时 `publish` 允许，`notify` 失败
- `report.md` 不存在或内容为空时，`publish` 失败

## 本地 publish 验收

创建测试输入：

```text
runs/test-run/manifest.json
runs/test-run/report.md
```

执行：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli publish --run-dir runs/test-run
```

检查：

```text
runs/test-run/report.html
runs/test-run/publish.json        # 包含 route，不持久化完整站点 URL
public/reports/test-run/index.html
public/reports/test-run/meta.json # 包含 route 和 notify_status
public/reports/index.html
public/assets/style.css
```

## 基础检查

```bash
PYTHONPATH=src uv run python -m py_compile src/daily_vulns_agent/*.py
```
