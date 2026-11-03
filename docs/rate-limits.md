# 用量接口的限流实测

`GET https://api.anthropic.com/api/oauth/usage` 是官方百分比的唯一入口，也是这套看板唯一会发出去的请求。它有两个属性：不消耗推理额度，但限流很紧。

## 实测

同一台机器、两个账号、走本地代理，连续两次探测：

| 账号 | 结果 | `Retry-After` | 耗时 |
|---|---|---|---|
| 00 | 429 `rate_limit_error` | 3600 秒 | 3.7 s |
| 01 | 429 `rate_limit_error` | 3600 秒 | 4.9 s |
| 01（约 22 分钟后） | 429 `rate_limit_error` | 1014 秒 | 1.8 s |

三点结论：

1. 冷却时间不是固定 3600，会随剩余惩罚滑动，所以必须按响应头走，不能写死。
2. 两个账号在同一时刻都被限，说明限流至少部分是出口 IP 级别的，不只是 token 级别的。加账号不会增加探测预算。
3. 第一次探测就已经是 429，说明 Claude Code 会话自己的 statusline 也在打这个接口，我们只是排在后面。

## 因此的取数顺序

```
usage/<slug>.json 存在且新鲜？
  ├─ 是 → 直接用，不发任何请求
  └─ 否 → 这个账号的冷却时间过了吗？
            ├─ 没过 → 等，界面照常显示旧值和缓存年龄
            └─ 过了 → 发一次，最多 45 分钟一次，全局错峰 90 秒
```

`usage/<slug>.json` 由 claude-accounts 的 statusline 钩子写入。Claude Code 每次调用 statusline 都会把 `rate_limits` 放在 stdin 里，钩子只是转存，不产生请求。把 `refreshInterval` 调到 15 秒，就等于免费拿到 15 秒粒度的官方数字。

## 预算参数

| 常量 | 值 | 位置 |
|---|---|---|
| `PROBE_INTERVAL` | 45 分钟 | `core.py` |
| `PROBE_GAP` | 90 秒 | `core.py` |
| `PROBE_STALE` | 10 分钟 | `core.py` |
| `PROBE_STALE_RESET` | 2 分钟 | `core.py` |

`PROBE_STALE_RESET` 是窗口刚过重置时的例外：这时旧数字已经作废，早一点拿到新数字的价值高于省一次请求。

## 手动重置

探测状态存在 Windows 的 `%LOCALAPPDATA%\claude-route\probe_state.json`，或 macOS/Linux 的 `${XDG_STATE_HOME:-~/.local/state}/claude-route/probe_state.json`。如果新目录不存在而旧的 `claude-pool-monitor` 目录存在，程序会继续使用旧目录。删掉这个文件等于清空所有冷却记录，下一轮就会重新探测——在明明还被限流的时候这么做只会再吃一次 429，没有别的效果。

想彻底不发请求，用 `--offline`。
