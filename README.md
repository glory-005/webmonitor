# webmoniter

网页变更实时监控：定期抓取页面 → 抽取关键内容 → 归一化去噪 → 比对差异 → 飞书/钉钉通知。

技术栈：**APScheduler 3.x**（调度）+ **httpx**（抓取）+ **BeautifulSoup4**（解析）+ **aiosqlite**（存储）+ **pydantic v2**（配置校验）+ **typer**（CLI）。

## 特性

- **只报真变化**：CSS/正则/JSON 抽取目标区域 → 归一化（去空白/零宽字符/全角、正则抹掉时间戳与随机 token、折叠重复行）→ sha256 快比对 → 行级 diff → 变化量与相似度阈值双重过滤，把「渲染抖动」和「真实变更」分开。
- **选择器失效不会误报**：抽取结果为空按「抓取失败」处理，而不是报「页面内容被清空」。
- **改了选择器不产生伪告警**：目标的配置指纹变化时自动重建基线（`runtime.py`），不会拿新规则的文本去撞旧规则的 hash。
- **状态机**：首次运行建立基线不通知；连续 N 次失败才告警（失败通知发出去了才记标记，不会因为一次发送失败被永久静默）；恢复后补一条恢复通知；未变化时不存正文，库不会被整页文本撑爆。
- **通知不丢**：单条通知带指数退避重试；若渠道彻底不可用，变更通知会留到下一轮补投（只保留最近一条，超过 1 小时未送达就丢弃；进程重启也丢，但 `changes` 表与 `data/artifacts/` 里仍有记录）。静默期或渠道名写错的变更**不会**进补投队列。
- **双渠道通知**：飞书自定义机器人（交互式卡片）、钉钉自定义机器人（markdown），均支持加签、关键词、@所有人。
- **限流**：按渠道令牌桶排队，避免触发机器人频控。
- **自监控**：超过 N 小时没有任何成功检查时主动告警，防止「进程活着但什么都抓不到」的静默失效。
- **静默窗口**：`webmoniter silence <id> --hours 4` 临时压掉某个目标的告警。

## 快速开始

```powershell
uv sync                                  # 安装依赖（含 dev 组：pytest/ruff）
Copy-Item .env.example .env              # 填 webhook / secret
uv run webmoniter channels               # 确认渠道被发现
uv run webmoniter test-notify            # 发一条测试消息，验证加签
uv run webmoniter check                  # 比对一轮但不发消息（注意：会推进状态）
uv run webmoniter run                    # 正式常驻监控
```

生产环境只要运行依赖：`uv sync --no-dev`。

排查选择器：`uv run webmoniter check price_watch --show-text --show-diff`

需要 JS 动态渲染的页面：

```powershell
uv sync --extra browser
uv run playwright install chromium
# 然后在 targets.yaml 里给该目标加 fetch: browser 与 wait_for: "css 选择器"
```

## 项目结构

```
webmoniter/
├─ config/targets.yaml          # ★ 日常唯一需要改的文件（监控目标 + defaults）
├─ .env.example                 # webhook / secret / 通用设置模板
├─ data/                        # 运行时产物（gitignore）：SQLite 库 + 变更内容留存
├─ src/webmoniter/
│  ├─ cli.py                    # 命令：run / check / targets / channels / changes / test-notify / silence / doctor
│  ├─ settings.py               # pydantic-settings：环境变量（前缀 WEBMONITER_）
│  ├─ config.py                 # targets.yaml 加载、defaults 深合并、配置指纹
│  ├─ models.py                 # Target / ExtractRule / DiffOptions / NotifyOptions / NotificationMessage
│  ├─ storage.py                # aiosqlite：targets / snapshots / changes / state / notifications
│  ├─ fetch/                    # http.py（httpx + 退避重试 + 编码兜底）、browser.py（可选 Playwright）
│  ├─ extract.py                # BeautifulSoup 抽取：css / regex / json
│  ├─ normalize.py              # 归一化与噪声过滤
│  ├─ diff.py                   # hash + 行级 diff + 相似度
│  ├─ watcher.py                # ★ 单目标状态机
│  ├─ scheduler.py              # APScheduler：每目标一个 interval job + jitter
│  ├─ throttle.py               # 令牌桶限流
│  ├─ notify/                   # base.py / feishu.py / dingtalk.py / registry.py
│  ├─ render.py                 # 通知文案
│  ├─ health.py                 # 自监控（静默失效检测）
│  ├─ runtime.py                # 组件装配与生命周期
│  └─ logging_setup.py          # loguru
└─ tests/                       # 抽取 / 归一化 / 比对 / 加签 / 状态机 回归测试
```

## 配置

### 1. 通知渠道（环境变量，约定式命名）

| 环境变量 | 说明 |
|---|---|
| `<KIND>_WEBHOOK_<NAME>` | 必需。渠道名 = `<kind>_<name 小写>` |
| `<KIND>_SECRET_<NAME>` | 可选。加签密钥（推荐启用加签） |
| `<KIND>_KEYWORD_<NAME>` | 可选。机器人安全设置为「关键词」时填，会自动拼进标题 |

`KIND` 取 `FEISHU` 或 `DINGTALK`。例如：

```dotenv
FEISHU_WEBHOOK_OPS=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
FEISHU_SECRET_OPS=xxxxxxxx
DINGTALK_WEBHOOK_OPS=https://oapi.dingtalk.com/robot/send?access_token=xxxx
DINGTALK_SECRET_OPS=SECxxxx
```

对应 `targets.yaml` 里 `channels: [feishu_ops, dingtalk_ops]`。

> 加签算法两个平台**故意不同**，本项目的实现与测试已钉住：
> - 飞书：`key = "{秒级时间戳}\n{secret}"`，消息体为空，`sign` 放请求体；`timestamp` 是**秒**。
> - 钉钉：`key = secret`，消息体为 `"{毫秒级时间戳}\n{secret}"`，base64 后需 urlencode，`timestamp`/`sign` 放 **URL 查询参数**；`timestamp` 是**毫秒**。
>
> 服务器时间必须 NTP 同步（两端都会校验时间偏差），否则报「sign match fail」。

### 2. 监控目标（config/targets.yaml）

```yaml
defaults:
  interval: 300
  jitter: 30
  diff:
    strip_selectors: [script, style, noscript, ".ad"]
    ignore_regex:
      - '\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}'
      - 'csrf[_-]?token["'']?\s*[:=]\s*["''][^"'']+'
    min_changed_chars: 5
    similarity_threshold: 0.995
  notify:
    channels: [feishu_ops]
    on: [changed, recovered]
    error_after_consecutive: 3
    mention_all_keywords: []

targets:
  - id: gov_notice
    name: 某公告列表
    url: https://example.gov.cn/notice/list.html
    interval: 120
    extract:
      - type: css
        selector: "div.list ul"
        mode: text
```

抽取规则：

| type | 必填 | 说明 |
|---|---|---|
| `css` | `selector`，可选 `mode`（text/html/attr）、`attr` | BeautifulSoup/soupsieve 选择器 |
| `regex` | `pattern`，可选 `group`、`flags`(i/m/s/x) | 对原始 HTML 做正则 |
| `json` | `path`（`a.b[0].c`） | 接口类页面 |

`target` 里写的字段会覆盖 `defaults`，嵌套的 `diff`/`notify` 逐层合并，只写要改的字段即可。

环境变量（`WEBMONITER_` 前缀）：`CONFIG_PATH`、`DB_PATH`、`ARTIFACTS_DIR`、`LOG_DIR`、`LOG_LEVEL`、`TZ`、`RATE_PER_MINUTE`、`CONCURRENCY`、`DEFAULT_TIMEOUT`、`MAX_NOTIFY_LINES`、`NOTIFY_RETRY_ATTEMPTS`、`HEALTH_INTERVAL`、`HEALTH_SILENCE_HOURS`、`HEALTH_CHANNELS`。

> `.env` 在导入 `webmoniter.settings` 时会被注入 `os.environ`（不覆盖真实环境变量）。
> 这一步是必需的：pydantic-settings 只把 `.env` 当配置源，不会写回环境，而渠道扫描
> （`FEISHU_WEBHOOK_*`）和 `cookies_env` 都是直接读环境的。

## 命令

| 命令 | 用途 |
|---|---|
| `webmoniter run` | 常驻监控；`--once` 只跑一轮（可交给 Windows 计划任务）；`--no-notify` 只观察 |
| `webmoniter check [id]` | 手动检查一次；默认**不发消息**，但会推进状态（这次变更之后不会再告警）；`--notify` 真发；`--show-diff` / `--show-text` 排查 |
| `webmoniter targets` | 列出目标与抽取规则 |
| `webmoniter channels` | 列出已发现的渠道（不打印完整 webhook） |
| `webmoniter changes -n 20` | 最近的变更记录 |
| `webmoniter test-notify [channel]` | 发测试通知，验证加签/关键词 |
| `webmoniter silence <id> --hours 4` | 临时静默某目标（`--hours 0` 取消） |
| `webmoniter doctor` | 自检：配置、数据库、渠道、Playwright 依赖 |

## 部署

- Windows：`nssm install webmoniter <venv>\Scripts\webmoniter.exe run`，或计划任务定时执行 `run --once`。
- Linux：systemd service，`ExecStart=/path/.venv/bin/webmoniter run`，`Restart=always`。
- Docker：基础镜像 `python:3.12-slim`，`uv sync --no-dev` 后 `CMD ["webmoniter","run"]`（不加 browser extra 时镜像很小）。

建议配合 `webmoniter doctor` 写进健康检查。日志在 `logs/webmoniter.log`（10MB 滚动，保留 14 天）。

## 测试

```powershell
uv run pytest -q
uv run ruff check .
```

覆盖重点：加签算法（两个平台的差异）、抽取规则、归一化去噪、比对阈值、存储层行为、以及状态机全流程（基线 → 未变 → 变更 → 噪声 → 连续失败 → 恢复 → 发送失败重试补投）。

## 已知限制与注意事项

- **合规**：只监控你有权访问的页面，遵守目标站 robots.txt 与使用条款；间隔别低于几十秒，不要用它做全站爬取。
- 未变化时只存指纹与时间戳（不存正文），因此历史里能看到的正文只有「基线 + 每次变更」；这是刻意的体积取舍。
- 变更正文留存为 `data/artifacts/<target_id>/<时间戳>.txt`（按字节截断到 `MAX_ARTIFACT_BYTES`），是**抽取后**的文本，不是原始 HTML。
- 待补投的通知只放在内存里（单个槽位、最多保留 1 小时），进程重启或超时会丢（`changes` 表与 artifacts 里仍有记录）。发送失败的责任边界：退避重试 `NOTIFY_RETRY_ATTEMPTS` 次 + 下一轮补投；仍失败只记 ERROR 日志与 `notifications` 表（`ok=0`），请用 `webmoniter doctor` / `test-notify` 定期体检渠道。
- `webmoniter check` 默认不发消息，但**会推进 last_hash**：手动检查会把这次变更消费掉，之后常驻进程不会再就它告警。排查选择器时请留意这一点。
- `notifications.dedup_key` 仅用于审计：按内容 digest 做去重门禁会误杀 A→B→A→B 这类来回变动的第二次告警，所以刻意不做去重门禁。
- 未实现变更内容的可视化面板；如需 Web 面板，可另加 FastAPI 读取 SQLite 的 `changes` 表。
- APScheduler 钉在 3.x：4.0 目前仍是 pre-release 且 API 大改。
- 多实例共用同一个 SQLite 未做分布式锁；请只跑一个常驻进程（用 `run --once` 做计划任务时，避免与常驻进程时间重叠）。

## 开发

```powershell
uv sync                 # 安装依赖（含 dev 组：pytest / ruff）
uv run pytest -q        # 全绿（50+ 用例）
uv run ruff check .     # 必须零告警
uv run python tools/inspect_page.py <url>   # 给新目标挑选择器的诊断工具
```

CI（`.github/workflows/ci.yml`）在 **Ubuntu 与 Windows 双平台**跑 ruff + pytest + CLI 冒烟；
Windows 那一份是刻意的——时区数据（tzdata）和 GBK 控制台都是只在 Windows 上才暴露的坑。

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `webmoniter doctor` 里渠道显示 × | `.env` 里的 `*_WEBHOOK_*` 为空；填好再跑 `channels` / `test-notify` |
| 日志显示已发送但群里没有 | 机器人安全设置不匹配：加签模式下服务器时间偏差超过 1 小时会报 `sign match fail`；关键词模式下标题必须包含该关键词；也检查是否触发频控（约 20 条/分钟） |
| `check --show-text` 输出为空 | 选择器失效（页面改版）。用 `tools/inspect_page.py` 重新挑一个带 `#id` 的稳定选择器 |
| 变更被判为"噪声" | `min_changed_chars` / `similarity_threshold` 太严，或页面有天然抖动字段需要加进 `ignore_regex` |
| 明明变了却不告警 | 是不是用 `check`（不带 `--notify`）或 `run --no-notify` 跑过？它们会推进状态、静默消费掉变更 |
| Windows 控制台编码报错 | CLI 已避开 GBK 码表外的符号（用 `√`/`×`）并把重定向输出写成 UTF-8；若仍报错请附 `webmoniter doctor` 输出 |

## 许可

MIT License，Copyright (c) 2026 morenqing。详见 [LICENSE](LICENSE)。

