# Daily Vulns Agent

Daily Vulns Agent 复用已有日报生成 skill，提供 CLI 全链路、HTML 发布、钉钉推送和 Web 定时调度。

## 目录说明

```text
config.example.yaml      # 示例配置
config.yaml              # 主配置，本地运行前由示例复制生成
prompts/                 # 日报生成 prompt
skills/                  # 项目内日报生成 skill
runs/                    # 每次运行的内部产物
public/                  # Nginx 对外托管的静态文件
state/                   # Web 调度状态和调度日志
src/daily_vulns_agent/   # Python 源码
```

一次运行的产物：

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

## 本地安装

```bash
uv venv
uv pip install -r requirements.txt
cp config.example.yaml config.yaml
```

本地运行 CLI 时默认读取当前目录的 `config.yaml`：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli <command>
```

如果要使用其他配置文件，再显式传 `--config`：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli --config path/to/config.yaml <command>
```

## CLI 命令

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

## 模型 CLI 依赖

`generate` 和 `run` 会按 `config.yaml` 里的 `generation.agent` 选择默认模型 CLI，并使用对应的 `command_template` 调用外部模型 CLI，例如 Claude Code 或 Codex CLI。

默认示例同时写了 Claude 和 Codex，当前默认使用 Claude：

```yaml
generation:
  agent: claude
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
    codex:
      base_url: ""
      api_key: ""
      model: ""
      command_template:
        - codex
        - exec
        - --dangerously-bypass-approvals-and-sandbox
        - "{prompt}"
```

如果要切换默认 CLI，把 `generation.agent` 改成 `codex`。`generate` 会直接读取 `generation.prompt_file` 的内容作为 `{prompt}` 传给命令模板，并以每次新建的 run 目录作为当前工作目录；run 目录内会创建指向项目 `skills/` 的软链接，并注入指向 run 目录内 `skills/daily-vulns` 的 `CLAUDE_SKILL_DIR`。每个 agent 的 `base_url` 和 `model` 会作为模板变量和环境变量注入子进程；`api_key` 只注入环境变量，不会写入命令行。

Codex agent 如果配置了 `base_url`、`api_key` 或 `model`，`generate` 会自动在 `{prompt}` 前追加 Codex 官方覆盖参数：`--ignore-user-config`、`--model <model>`，以及基于自定义 provider `daily_vulns_openai` 的 `-c model_provider="daily_vulns_openai"`、`-c model_providers.daily_vulns_openai.base_url="<base_url>"`、`-c model_providers.daily_vulns_openai.env_key="OPENAI_API_KEY"`。这样项目配置可以覆盖宿主机 `~/.codex/config.toml`，且不会覆盖 Codex 内置 `openai` provider；API Key 仍通过 `OPENAI_API_KEY` 环境变量注入。
因此运行 `generate` / `run` 前，需要确保执行环境里已经安装并登录对应 CLI：

```bash
claude --version
# 或
codex --version
```

宿主机执行 CLI 时，依赖宿主机上的 `claude` / `codex`。

Docker Web 调度执行 `run` 时，依赖容器内的 `claude` / `codex`。当前 Dockerfile 只安装 Python 依赖，没有内置 Claude/Codex CLI；如果要让 Web 定时任务在容器内真实生成日报，需要额外扩展镜像或把生成步骤改为宿主机执行。

如果你先只测试 `publish` / `notify`，不需要 Claude/Codex CLI。

## Skill 配置

日报生成 skill 还有独立配置文件：

```text
skills/daily-vulns/config.yaml
```

`generate` 会在每个 run 目录里创建 `skills -> <project>/skills` 软链接，并设置 `CLAUDE_SKILL_DIR` 指向 `skills/daily-vulns`，所以 skill 内的脚本命令可以使用 `${CLAUDE_SKILL_DIR}` 定位脚本。

如果需要从示例重新生成：

```bash
cp skills/daily-vulns/config.example.yaml skills/daily-vulns/config.yaml
```

主要字段：

- `source_configs.ghsa.token`：GitHub/GHSA token，可提升 API 限额；建议部署时在服务器本地配置，不要提交真实 token。


## Docker Compose 部署

启动前准备配置：

```bash
cp config.example.yaml config.yaml
```

启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f app
docker compose logs -f nginx
```

停止：

```bash
docker compose down
```

默认访问：

```text
http://localhost:8080/reports/
http://localhost:8080/admin/
```

默认账号密码来自 `config.yaml`：

```yaml
web:
  admin:
    username: admin
    password: change-me
```

配置页拆分为站点配置、钉钉配置和 Agent 配置：站点配置修改站点 URL；钉钉配置修改 webhook/secret；Agent 配置修改默认 agent、Base URL、API Key、模型名和 Claude/Codex 命令模板。已有 API Key 和钉钉 secret 在响应中替换为等长 `*` 掩码，提交时如果保持原掩码不变则保留旧值，改成其他内容则覆盖。

## Docker 和宿主机 CLI 的共享文件

Docker Compose 把宿主机目录挂载到容器内；运行产物目录会在容器启动时自动初始化，可以保留在 `.gitignore` 中：

```yaml
./config.yaml -> /app/config.yaml
./prompts     -> /app/prompts
./skills      -> /app/skills
./runs        -> /app/runs
./public      -> /app/public
./state       -> /app/state
```

因此下面两种方式操作的是同一批文件。

宿主机 CLI：

```bash
PYTHONPATH=src uv run python -m daily_vulns_agent.cli publish --run-dir runs/test-run
```

容器内 CLI：

```bash
docker compose exec app python -m daily_vulns_agent.cli publish --run-dir /app/runs/test-run
```

Web 调度在容器内执行 `web.run_command`，默认读取 `/app/config.yaml`，写入 `/app/runs`、`/app/public`、`/app/state`。这些路径都映射到宿主机同名目录。

## 关键配置

```yaml
generation:
  agent: claude
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
  site_base_url: "http://localhost:8080"
  runs_dir: runs
  public_dir: public
  reports_path: /reports

dingtalk:
  webhook: ""
  secret: ""

schedule:
  enabled: true
  timezone: Asia/Shanghai
  times:
    - "09:00"
```

`site_base_url` 应该设置为 Nginx 对外可访问的站点根 URL。`publish.json` 保存报告路由，`notify` 会用当前配置里的 `site_base_url` 拼接阅读全文链接。

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

## 本地 publish 验收

创建：

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
