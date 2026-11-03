# 三条路由怎么配

同一台机器上可以同时存在三条互不干扰的路由。它们共享一个 Claude Code 二进制，区别只在环境变量。

## 1. 官方订阅池

claude-accounts 把每个订阅拆成三样东西：一份 config 目录、一个 OAuth token、一个 slug。启动某个账号时只需要两个环境变量：

```
CLAUDE_CONFIG_DIR=<configs>/<slug>
CLAUDE_CODE_OAUTH_TOKEN=<该账号的 token>
```

上游命令：

```powershell
claude-accounts add                 # 跑一次 OAuth，新增一个订阅
claude-accounts list                # 列出所有账号和用量
claude-accounts launch 00           # 用 00 号启动 Claude Code
claude-accounts doctor              # 检查 token、隔离和同账号冲突
claude-accounts refresh 00          # 换掉 00 号的 token
```

每次 `add` 之后会多出一个可执行入口 `claude-<slug>`，等价于 `launch <slug>`。

如果你要自己包一层启动脚本，核心就是上面两个变量，加一条：不要继承 `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK` 之类的变量，否则订阅会被 API key 顶掉。参考 [examples/claude-account.cmd](../examples/claude-account.cmd)。

账号之间必须用不同的 `CLAUDE_CONFIG_DIR`。共用同一个目录会让两个订阅轮流覆盖 `~/.claude.json` 和凭据，表现是登录态莫名其妙丢失。

## 2. Claude Code + DeepSeek

DeepSeek 提供 Anthropic 兼容端点，Claude Code 原样可用，只改两个变量：

```powershell
$env:CLAUDE_CONFIG_DIR  = "$HOME\.config\claude-route\deepseek"
$env:ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
$env:ANTHROPIC_API_KEY  = "sk-..."
claude
```

单独给一份 `CLAUDE_CONFIG_DIR`，别和订阅账号的目录混用：两边的登录态、statusline、历史都不该共享。

模型名会被映射，不用改 Claude Code 的 `--model`：

| 你传的 | 实际跑的 | 计费 |
|---|---|---|
| `claude-opus*` | `deepseek-v4-pro` | V4 Pro 价格 |
| `claude-sonnet*` / `claude-haiku*` | `deepseek-flash` | V4 Flash 价格 |
| 其他不以 `claude` 开头的名字 | 返回 400 | — |

已知差异（以上游文档为准）：

- `anthropic-version` 被忽略，认证用 `x-api-key` 或 `Authorization: Bearer` 都行。
- 思考模式下工具调用带限制：`tool_choice` 为 `any` 会被当成 `auto`，指定具名工具在思考模式下返回 400。
- 图片只有 `deepseek-flash` 能吃，`deepseek-v4-pro` 会把图替换成 `[Unsupported Image]`。
- 这条路由不消耗任何 Claude 订阅额度，走的是 DeepSeek 的按量计费。

## 3. Hermes + Codex

这一条换的不是模型端点，而是 agent 运行时本身。Hermes 有自己的 provider 配置（`hermes model`），Codex 读自己的 `CODEX_HOME`，两者都不经过 `claude-accounts`。

它和另外两条路由的关系是互补而不是替换：

- 需要 Claude 的工具链和插件生态时用前两条；
- 需要跨模型交叉验证、或者某个模型的 5 小时额度已经打满时，切到这条；
- 三条都装在同一台机器上，互不共享 config 目录。

`claude-route` 在这种情况下依然有用：只要那台机器上还有走订阅的会话在跑，它们的 statusline 会继续更新 `usage/*.json`，看板照常显示。

## 可选：Baton 编排

单会话最容易撞到 5 小时额度。把任务拆给多个 executor、每个 executor 用一个不同的订阅，是唯一能真正吃满两个号的办法。

Baton 的模型是 conductor 决策、monitor 监督、executor 执行。接入 claude-route 只需要一步：给每个 executor 分配不同的 `CLAUDE_CONFIG_DIR`，然后用看板做调度输入。

```powershell
claude-route --json
```

输出结构：

```json
{
  "generated_at": "2026-11-02T23:23:26-08:00",
  "accounts": [
    {
      "slug": "00",
      "name": "claude-00",
      "five": 53.0,
      "five_resets_at": "2026-11-03T02:20:00-08:00",
      "five_reset_passed": false,
      "seven": 8.0,
      "seven_resets_at": "2026-11-05T01:00:00-08:00",
      "seven_reset_passed": false,
      "age_seconds": 0,
      "state": "READY",
      "live": true,
      "source": "cache"
    }
  ],
  "pick": "00"
}
```

调度规则可以很简单：`pick` 是当前最该用的账号；`state` 为 `COOL` 的跳过；`age_seconds` 超过 600 说明拿不到新数据，别把任务压在它上面。

Baton 不在本仓库内，也不是使用 `claude-route` 的前提。没有编排器的话，人工看 `pick` 那一行就够了。
