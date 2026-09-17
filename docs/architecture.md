# 架构

## 分层：核心独立，联动可选

```
┌───────────────────────────── 独立运行（主体，无外部依赖） ─────────────────────────────┐
│  standalone/*.cmd        双击即用：open-databases / install-guard / uninstall-guard    │
│  hhuvpn <子命令>          CLI（人读 + --json）                                          │
│  hhuvpn guard            常驻或计划任务：校园网掉线重登 + WebVPN 保活                    │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  hhuvpn/                 核心包（纯标准库）                                             │
│    campus · webvpn · databases · verify · guard · connector · cli · http · aes · state  │
└────────────────────────────────────────────────────────────────────────────────────────┘
                                   ▲ 只单向依赖（核心永远不认识上层）
┌────────────────── 可选附属（删掉不影响上面任何一项） ──────────────────┐
│  integrations/skill(SKILL.md)   agent 技能：让 agent 知道何时该先登录   │
│  integrations/mcp_server.py     MCP stdio 服务器（也可 python 直接跑）  │
└────────────────────────────────────────────────────────────────────────┘
```

依赖方向是硬约束：`hhuvpn/*.py` 在模块级**不得** import `integrations`；
`hhuvpn mcp` 是按文件路径惰性加载的，`integrations/` 不存在时只提示"可选组件不存在"。
`tests/test_independence.py` 会临时把 `integrations/` 改名藏起来再跑一遍 CLI，把这条约束钉死。

```
                     ┌──────────────────────────────────────────────┐
   人 / agent  ────► │  CLI (hhuvpn ...)   MCP (hhuvpn mcp)  Skill  │
                     └───────────────────┬──────────────────────────┘
                                         │ 统一信封 {schema, command, ok, steps, error}
                                 ┌───────▼────────┐
                                 │  Connector     │  编排：幂等 ensure / 状态 / 校准
                                 └───┬────────┬───┘
                     ┌───────────────┘        └────────────────┐
             ┌───────▼────────┐                      ┌─────────▼────────┐
             │ CampusClient   │                      │ WebVPNClient     │
             │ ePortal 认证    │                      │ CAS 登录 + 签名   │
             └───────┬────────┘                      └─────────┬────────┘
                     │                                          │
                     └──────────────┬───────────────────────────┘
                            ┌───────▼────────┐        ┌──────────────────┐
                            │ HttpSession    │        │ aes.py           │
                            │ cookie/重定向链 │        │ AES-CBC / CFB    │
                            └────────────────┘        └──────────────────┘
                                     │
                    ┌────────────────┼─────────────────┐
              ┌─────▼─────┐   ┌──────▼──────┐   ┌──────▼───────┐
              │ databases │   │ verify      │   │ state        │
              │ 目录/别名  │   │ 判定/校准    │   │ 会话/指纹/日志│
              └───────────┘   └─────────────┘   └──────────────┘
```

## 模块职责

| 模块 | 职责 | 关键设计 |
| --- | --- | --- |
| `aes.py` | 纯标准库 AES-128：CBC（CAS 密码字段）、CFB128（WebVPN 主机签名） | 用 FIPS-197 / NIST SP 800-38A 官方向量 + 真实签名向量锁死正确性 |
| `http.py` | cookie 会话、**手动重定向链**、编码嗅探、TLS 策略 | 重定向链留痕是排查认证问题的关键；`tls_verify=auto` 降级必告警 |
| `campus.py` | ePortal 认证（协议沿用上游） | 先判在线再谈登录；重定向跳出校园域立即停止（见 field-notes §1） |
| `webvpn.py` | WebVPN 登录、网址签名/反解、认证通道 | 登录页结构解析 + 失败签名提取 + 验证码状态识别 |
| `databases.py` | 文献库目录（内置 27 个 + 用户自建） | 别名同义词归一（"知网"→cnki）；用户层覆盖内置层 |
| `verify.py` | "是否已登上"的判定与指纹校准 | 三档结论：true / false / "unknown"，永不假装成功 |
| `state.py` | 会话 cookie、指纹、状态快照、日志 | 原子写 + 0600 权限；状态目录可配置 |
| `connector.py` | 编排与错误码 | 每步结构化留痕 + `next_actions` 建议 |
| `cli.py` | 命令行与 JSON 信封 | 所有命令 `--json`；退出码区分"要人工"与"缺凭据" |
| `guard.py` | 守护：掉线重登、WebVPN 保活、断路保护 | 独立功能，不需要 agent；断路标志防止撞出验证码 |
| `integrations/mcp_server.py` | 【可选】MCP stdio（JSON-RPC 2.0） | 与 CLI 同源，共用 Connector；删掉不影响核心 |
| `secret.py` | DPAPI 凭据保护（ctypes） | 让密码可以不明文落盘 |

## 数据流：一次 guard（独立守护，不需要 agent）

```
guard --once（计划任务每分钟一次）
 ├─ 校园网 probe 204 ── 在线 → 什么都不做（零副作用）
 │                     离线 → 登录 → 成功则弹通知；凭据被拒则写 auth_failed.flag 断路
 ├─ WebVPN 会话（默认每 15 分钟才检查一次，别把学校服务器当靶子）
 │        └─ 失效 → 用同一份凭据重登；被拒同样断路
 ├─ 可选：--db sd,cnki 时按 --db-check-minutes 抽查库是否仍可访问
 └─ 写 state/guard.json（供 guard --status 与外部监控查看）
```

## 数据流：一次 ensure

```
ensure(targets=[sd,cnki])
 ├─ 读配置（--config > 环境变量路径 > 项目 config.ini > ~/.hhu-vpn/config.ini）
 ├─ 解析凭据（env > config > DPAPI > 交互）
 ├─ campus: probe 204 ── 在线 → already_online（不碰门户）
 │                      离线 → 门户链路 → getServices → login → 复查 204
 ├─ webvpn: 载入 state/session.json cookie
 │            └─ 线上校验（访问门户首页是否被弹 CAS）
 │                 ├─ 有效 → already_logged_in
 │                 └─ 无效 → CAS 登录（salt/execution/service → AES 密码 → POST）
 │                            ├─ 302 + ticket → 保存 cookie
 │                            ├─ 401 → auth_rejected（提取服务端原文）
 │                            └─ isNeed → captcha_required（图形码存图 / 滑块码给指引）
 └─ 每个目标库: 签名 URL → 取首页 → 通道层 + 内容层 + 指纹层判定
      └─ 写 state/status.json（供 status 快速回看）
```

## 错误码

| code | 层级 | 含义 |
| --- | --- | --- |
| `credentials_missing` | 前置 | 没有可用账号密码 |
| `campus_disabled` / `webvpn_disabled` | 配置 | 用户在配置里关掉了自动登录 |
| `wifi_gate` | 校园网 | 当前 WiFi 不在白名单 |
| `portal_unreachable` | 校园网 | 门户链路不可达（不在校园网） |
| `online_not_restored` | 校园网 | 接口说成功但探针仍失败 |
| `auth_rejected` | 认证 | 服务器拒绝凭据（含服务端原文） |
| `captcha_required` | 认证 | 需要图形/滑块验证码 |
| `webvpn_unreachable` | WebVPN | 入口不可达 |
| `ticket_not_accepted` | WebVPN | 拿到 ticket 但门户仍要求认证 |
| `login_page_unexpected` | WebVPN | 登录页结构变了（未找到 salt） |
| `db_unreachable` | 数据库 | 目标库取不到 |
| `db_not_authenticated` | 数据库 | 可达但身份未确认 |
| `unknown_database` | 数据库 | 别名不在目录里 |

## 为什么这么设计

1. **只做一件事**：认证链路是"最多坑、最容易过期、最不值得每个 agent 重复实现"的部分；
   检索/下载则千变万化，交给上层。边界清晰，插件才活得久。
2. **JSON 优先**：agent 消费的是字段，不是中文句子。人读模式只是附赠。
3. **诚实判定**：`unknown` 是一个合法结果。把"不确定"说成"成功"会让后续所有结论失真。
4. **零依赖**：上游的取舍值得继承——用户 clone 下来就能跑，不用 pip 装任何东西。
5. **可校准**：学校页面随时会改版，与其写死规则，不如让"成功登录时的样子"成为规则。
