# claude-route

[![ci](https://github.com/Valdemar-Yu/claude-route/actions/workflows/ci.yml/badge.svg)](https://github.com/Valdemar-Yu/claude-route/actions/workflows/ci.yml)

一套 Claude Code 多账号路由方案，外加一个不烧额度的额度看板。

一套 Claude Code，可以对接多个官方订阅轮换使用，也可以把请求转给 DeepSeek 之类的兼容后端；`claude-route` 负责其中的账号隔离、切换和额度可见性。

```
┌─ claude-route ──────────────────────────────────────────────────────────────────────────────────────┐
│ 2026-11-02 23:23:26   2 accounts   live: claude-00                                                  │
│                                                                                                     │
│    #  account      5h window               resets    7d window            resets      data  state   │
│   00  claude-00    ███████░░░░░░░  53%     2h56m     █░░░░░░░░░░░   8%    2d1h          0s  READY   │
│   01  claude-01    ░░░░░░░░░░░░░░   0% ~   reset     ████████░░░░  63%    1d23h     14m57s  READY   │
│                                                                                                     │
│ 5h history, last hour                                                                               │
│ 00  ▄    --                                                                                         │
│ 01  ██████▁    --                                                                                   │
│                                                                                                     │
│ pick  claude-01      next probe  claude-00 in 9m59s      q quit · r probe · p pause · o offline      │
└─────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 它解决什么

一个 Claude 订阅有 5 小时和 7 天两层额度，打满之后要么等，要么换号。开两个号不难，难的是三件事：

1. 两个号要真正隔离，不能共用同一个 `~/.claude` 目录互相串登录态；
2. 得随时知道哪个号还能用、还剩多少、什么时候重置；
3. 查看额度这件事本身不能消耗额度。

前两条由上游 [claude-accounts](https://github.com/claude-code-tools/claude-code-account-switcher) 负责：它把每个订阅的 OAuth token 和 config 目录分开存，`claude-accounts launch 00` 就是另一个号的独立会话。

第三条是 `claude-route` 的全部工作。它读 claude-accounts 已经写好的用量缓存，1 秒重绘一次界面；只有在缓存过期时才去问一次官方接口，并且严格卡住频率。

## 三条路由

| 组合 | 前端 | 背后是什么 | 什么时候用 |
|---|---|---|---|
| 官方订阅池 | Claude Code | 多个 Claude 订阅轮换 | 主力，额度就是你的成本上限 |
| Claude Code + DeepSeek | Claude Code | `api.deepseek.com/anthropic` | 批量、长尾、机械活，按 token 付费，不占订阅额度 |
| Hermes + Codex | Hermes / Codex CLI | 另一套 agent 运行时 | 想换执行者或跨模型交叉验证时 |

DeepSeek 官方提供 Anthropic 兼容端点，`claude-opus*` 映射到 `deepseek-v4-pro`，`claude-sonnet*` / `claude-haiku*` 映射到 `deepseek-flash`，所以 Claude Code 只改两个环境变量就能切过去：

```powershell
$env:ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
$env:ANTHROPIC_API_KEY  = "sk-..."
claude
```

细节和另外两条路由的配法见 [docs/routes.md](docs/routes.md)。

## 可选：Baton 编排

如果一次要做的是十几个文件、几十轮的工具调用，单会话会顶到 5 小时额度。可行的做法是在外面套一层编排，把任务拆给多个执行者，每个执行者跑在独立的订阅上，`claude-route` 负责盯着它们别把池子抽干。

Baton 就是作者在用的那层编排：conductor 决策、monitor 监督、executor 执行，执行者按任务分发到不同后端。`claude-route` 不依赖它，也不包含它；这里只说明接入点——把每个 executor 的 `CLAUDE_CONFIG_DIR` 指向 claude-accounts 里不同的账号目录，再把 `claude-route --json` 接进编排器的调度判断，就能按额度余量分配任务。

## 安装

要求 Python 3.10+ 和 [claude-accounts](https://github.com/claude-code-tools/claude-code-account-switcher)。后者是 Go 程序，按上游 README 装好，确认 `claude-accounts list` 能列出账号。

```powershell
git clone https://github.com/Valdemar-Yu/claude-route.git
cd claude-route
python -m pip install -e .
```

装完得到两个等价命令：`claude-route` 和 `claudepool`。

不装也能跑，仓库里带了免安装入口：

```powershell
.\bin\claude-route.cmd --once
```

## 使用

```powershell
claude-route                # 进入 TUI，1 秒刷新
claude-route --once         # 打印一屏快照
claude-route --json         # 机器可读，给编排器或状态栏用
claude-route --offline      # 只读缓存，一个包都不发
claude-route --probe --once # 立刻探一次官方接口（受冷却限制）
```

| 按键 | 作用 |
|---|---|
| `q` | 退出 |
| `r` | 立刻探测一次，受 45 分钟冷却约束 |
| `p` | 暂停刷新 |
| `o` | 切换离线/在线 |

| 参数 | 说明 |
|---|---|
| `--base` | claude-accounts 的数据目录，默认 `~/.config/claude-subscriptions`，也认 `CLAUDE_SUBSCRIPTIONS_CONFIG_DIR` |
| `--state-dir` | 探测状态和历史的位置，默认 `%LOCALAPPDATA%\claude-route`（Linux 为 `~/.local/state/claude-route`） |
| `--interval` | 重绘间隔，默认 1 秒 |

## 读数说明

| 字段 | 含义 |
|---|---|
| `5h` / `7d` | 官方百分比。绿 <50%，黄 ≥80%，红 =100% |
| `resets` | 距离重置的时间。窗口已过期显示 `reset`，百分比带 `~` 表示按归零推算 |
| `data` | 缓存年龄。≤90 秒是绿色，说明该账号正在跑会话；黄色 10 分钟，红色 1 小时 |
| `state` | `READY` / `TIGHT` / `COOL`（5h 打满）/ `UNKNOWN` |
| `history` | 最近一小时的 5h 采样曲线和烧号速率，速率标注统计跨度 |
| `pick` | 5h 最低、7d 未满的账号，即下一个该用的 |

## 为什么不烧额度

`GET /api/oauth/usage` 是唯一能查到官方百分比的入口，但它按小时限流。实测两次都是 429，`Retry-After` 分别给过 3600 和 1014 秒，说明冷却时间是滑动的。

所以主数据源不是接口，而是 claude-accounts 的 statusline 钩子已经写好的文件：

```
Claude Code 会话
   └─ statusline 每 15 秒被调用一次，stdin 里带着 rate_limits
        └─ claude-accounts statusline 落盘到 usage/<slug>.json
             └─ claude-route 每 1 秒读这个文件，零请求
```

接口只在缓存超过 10 分钟没更新时才兜底，每个账号 45 分钟最多一次，全局错峰 90 秒，收到 429 就把 `Retry-After` 记进状态文件，到点前完全静默。两条路径都不产生推理请求，不消耗 5h/7d 额度。实测记录见 [docs/rate-limits.md](docs/rate-limits.md)。

## 边界

- 只读。claude-route 不写 `tokens.json`、`accounts.tsv`、`usage/` 或 `configs/`，探测结果存在自己的状态目录。
- 没有活会话的账号，数字会停在最后一次缓存上；`data` 列变红就是在提示这件事。
- 429 是账号或出口 IP 级别的，两个账号曾经同时被限。探测预算是全局算的，不是每个账号各算一份。
- Windows 优先（码页、PATH、`msvcrt` 键盘），Linux/macOS 能跑但没做交互键适配。
- 上游 claude-accounts 的二进制不在此仓库内，本仓库不打包、不修改它。

## License

MIT。上游 claude-accounts 遵循其各自的许可。

---

**English**

`claude-route` is a small companion for [claude-accounts](https://github.com/claude-code-tools/claude-code-account-switcher): a 1 Hz terminal dashboard for every subscription's 5h / 7d quota that never spends any of it. It reads the usage cache claude-accounts already writes from Claude Code's statusline, and only falls back to the official usage endpoint once every 45 minutes per account — honouring `Retry-After`. It also documents routing the same Claude Code install to DeepSeek's Anthropic-compatible endpoint, and to Hermes / Codex, for work that should not touch subscription quota at all.
