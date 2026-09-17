# 更新日志（hhuvpn 部分）

上游 hhu-autologin 的更新日志见 `upstream/CHANGELOG.md`。

## 0.5.1 — 2026-09-16（真机下载验证时抓到的四个问题）

拿真实账号做"开 VPN → 下论文 → 删掉 → 关 VPN"的端到端验证，过程中发现并修掉：

- **测试套件会偷偷开真实 VPN**（最该骂的一条）：`test_cli_mcp.py` 里几条 CLI 测试
  没有隔离，跑 `ensure` 时读到了本机真实的 config.ini / DPAPI 凭据，
  于是**真的去登录 WebVPN**——测试跑一遍，就把使用者刚关掉的 VPN 重新打开。
  现在**所有** CLI 测试统一走隔离版本（临时 config + 临时 state + 清空 `HHU_*` 环境变量），
  并实测：跑完整套测试后，真实 state 目录里没有任何 WebVPN 日志、意图仍为 off、无会话文件。
  （这条正是 0.5.1 新加的审计日志揪出来的——"谁会开 VPN"必须可追溯。）

- **竞态：关掉的 VPN 被在途的保活 tick 重新拉起来**（实机复现）。
  使用者执行 `vpn off` 的同一秒，计划任务里某个已启动的 tick 还拿着旧的开启意图，
  于是把会话又登了回来——表现为"明明关了，几分钟后又是已开启"。
  修法：tick 记下本轮的**意图版本**（`intent.since`），登录前与登录后各复核一次；
  若期间被关闭，则放弃重登、必要时**回滚**（注销 + 清会话）。
  并且明确：保活**不改写**使用者的开启意图——意图是使用者的，不是守护的。
  回归测试：`test_R5_close_during_inflight_tick_wins`、`test_R5b_rollback_if_closed_during_login`。
- **限流被当成"未授权"**：ScienceDirect 对短时间内多次请求会返回 HTTP 429。
  之前会落到"未命中机构标记"，让人误以为是订阅问题。
  现在单独识别：`rate_limited: true` + 明确说明"被限流，等几分钟再试，不是未授权"，
  且结论为 `unknown`（限流页不是真实页面，不能下结论）。
- **审计缺失**：谁会开 VPN 是这个功能的核心契约，但日志里看不出调用来源。
  现在 `ensure_webvpn(by=...)` 每次调用都记日志（ensure / cli_login / cli_vpn_on / guard_keepalive），
  并记录保活轮次的会话状态——"守护有没有偷偷开 VPN"可以直接查日志回答。

## 0.5.0 — 2026-09-16（WebVPN 改为"按需开启 + 开着就保活 + 关机即关闭"）

按用户要求把两个服务的策略拆开：**校园网常驻自动登录；WebVPN 只在使用者说明时开启，
开启后保活，关机后到下次开机前保持关闭。**

- **新增"开启意图"状态** `state/webvpn_intent.json`（on/off）：
  只有使用者显式开启（`vpn on` / `ensure` / `open` / `login webvpn`）才置 on；
  `vpn off`、注销、以及开机后的首次守护运行置 off。守护据此决定要不要保活。
- **守护不再主动开 WebVPN**：intent=off 时完全不登录、不续期，只报告状态；
  intent=on 时恢复保活（会话过期自动重登），直到 `vpn off` 或关机。
- **关机边界**：本次开机后的第一次守护运行会关掉 WebVPN（远端注销 + 清本地会话 + 复位意图），
  保证"关机后到下次开机前都是关的"。第一次启用守护时只记录开机标识、不掐掉当时开着的会话。
- **新增 `hhuvpn vpn` 子命令**：`on` / `off` / `status`（`status` 会对有效会话做"认领"，
  避免出现"状态 on / 意图 off"的自相矛盾）。
- 配置改为 `[webvpn] keepalive = true`（开启后是否保活）与 `close_on_boot = true`；
  原来的 `guard_keepalive` 语义作废。
- 库抽查只在 WebVPN 处于开启状态时进行；未开启时明确写"跳过：按需模式未开启"，不拿签名地址去撞登录页。

回归测试：`tests/test_webvpn_on_demand.py`（R1 守护不主动开 / R2 开着就保活 /
R2b 有效会话不动 / R3 开机后关闭并复位 / R3b 首次启用不误关 / R4 vpn off 复位）。

## 0.4.1 — 2026-09-16（明确"校园网与 WebVPN 是同一个账号"）

用户指出：校园网与 WebVPN 用的是同一个账号。据此把三件事落实：

- **凭据只解析一份**：`login`（不带参数）原来会在校园网与 WebVPN 两处各自解析一次，
  交互模式下可能连问两遍密码；现在解析一次、两处复用。
- **失败也不重复消费**：校园网认证因 `auth_rejected` / `captcha_required` 被拒时，
  不再拿同一份密码去撞 WebVPN——两边都撞等于把失败次数翻倍，更容易把账号推进滑块验证码。
  非凭据类问题（不在校园网、门户不可达）不受影响，WebVPN 照常尝试（校外就是这么用的）。
- **把事实写进接口**：`ensure` 报告新增 `account_scope: "campus+webvpn（同一账号）"`，
  `credentials.scope = "campus+webvpn"`；配置示例与文档都点明"只填一处、只改一处"。

回归测试：`tests/test_shared_account.py`（凭据作用域、被拒后跳过第二次登录、
校园网验证码同样跳过、非凭据故障不跳过，并断言凭据对象不会序列化出密码）。

## 0.4.0 — 2026-09-16（按用户指正的正常逻辑修正）

**指正**：正常逻辑应该是 **先登录校园网 → 再登录 WebVPN → 最后通过 WebVPN 进数据库**。
0.3.0 把"校内直连优先"设成默认，等于把备用通道当成了主流程，还让 `ensure` 在 WebVPN 没登上时
报成功——这是错的，已全部改回：

- `ensure` / `db check` / `open` 的默认 `--via` 改回 **webvpn**：必须有 WebVPN，
  再通过 WebVPN 进库；WebVPN 拿不到就是**失败**，如实报错并给出该跑哪条命令。
- 校内 IP 直连降为**显式备用通道**，只有两种情况会出现：
  `--via auto`（先 WebVPN，走不通才允许兜底，结果里带说明）或 `--via direct`（明确只用它）。
- 默认失败时仍会把备用通道的情况**单列**在 `fallback` / `fallback_ready` 里，
  并写明"仅作备选，不改变本次判定为失败"——信息给到，但不冒充成功。
- `open` 默认开 WebVPN 入口地址；`--via direct` 才开真实网址，说明文字也区分开。
- 退出码对齐：`open` 缺凭据返回 4、需人工（验证码）返回 3，与 `ensure` 一致。

保留 0.3.0 的其他成果（那些是对的）：SD 的 `orgName` 结构化标记、
CNKI「机构用户」假阳性的修复、`evidence` 原文片段、非法正则显式暴露。

## 0.3.0 — 2026-09-16（实际应用后按真实反馈改的）

拿真机跑了一遍，改了四件事：

- **新增「校内 IP 直连」通道**：实测 ScienceDirect 在校园网出口 IP 上**本来就已认证**
  （页面内嵌 `"orgName":"Hohai University","webUserId":"1968895"`），根本不用绕 WebVPN。
  现在 `ensure`/`db check` 默认**先校内直连、再回退 WebVPN**，结果里带
  `channel: direct|webvpn`；CLI 增加 `db check --via auto|direct|webvpn` 与 `fetch --direct`。
  连带好处：WebVPN 因验证码登不上时，校内可用的库照样能被确认，`ensure` 不再一刀切失败。
- **修掉一个假阳性（重要）**：CNKI 首页写着「机构用户」四个字，与是否认证无关，
  早先那条宽松规则把 CNKI 判成"已登上"。现在机构名必须出现在**像认证信息的上下文**里
  （`机构用户/欢迎/所属机构/orgName/entitled…` 附近，或紧跟 `图书馆/读者/用户/机构`），
  且每次命中都在 `evidence` 里给出**可核对的原文片段**。
- **非法正则不再静默降级**：以前写错的正则会被当成子串匹配（既可能误报也可能漏报，还没人发现）。
  现在记入 `rules_invalid` 显式暴露；同时修掉目录里两处 `(?i)` 位置不合法的规则。
- **`open` 按通道开对地址**：校内直连已认证时开真实网址（浏览器同样在校内 IP 上），
  走 WebVPN 时才开签名入口；说明文字按每个库的实际情况给，**不把"无法确认"说成已认证**。

实测结论（江宁校区无线网，2026-09-16）：

| 库 | 校内直连结果 | 依据 |
| --- | --- | --- |
| ScienceDirect | **已认证** | `orgName: Hohai University`（内嵌 JSON） |
| 河海图书馆 | 已认证 | 站点自带校名 |
| 中国知网 www | 无法确认 | 首页静态 HTML 里没有机构信息（JS 渲染） |
| 知网 kns 检索 | 无法确认 | 直连先撞上 CNKI "安全验证"反爬页 |
| Web of Science / 万方 | 无法确认 | 需要 JS 或 WebVPN 会话 |

## 0.2.0 — 2026-09-16

**基座升级到上游 v1.4.2**（`python tools/upstream-sync.py` 可复现这次同步）

- `upstream/` 整体替换为 v1.4.2：新增 `hhu_gui.pyw`（无黑窗入口）、
  `tools/make_portable_zip.py`（便携包）、`使用前必读！！！.txt`；
  `hhu_gui.py` 高 DPI 适配；CI 增加 Python 3.8/3.10/3.12 × Windows/Ubuntu 版本矩阵
- **移植上游 v1.4.1 的阻断级修复：所有请求强制直连**
  （`ProxyHandler({})` 顶掉系统代理）。开 Clash 时校园门户请求被塞进代理 →
  误报"不在校园网"、守护瘫痪、探针被"代答"造成假在线。本项目 `hhuvpn/http.py`
  的会话 opener 与 204 探针 opener 两处同步修复
- 新增回归测试 `tests/test_http_direct.py`：先证明默认 opener 确实跟随系统代理
  （保证测试有鉴别力），再锁定本插件直连；同时新增 Python 3.8 语法兼容核对
- 文档补充：代理软件这一坑记入 `docs/field-notes.md` §1.1；README 标明基座版本与同步方法

## 0.1.0 — 2026-09-16

首个版本：校园网 ePortal 认证（移植自上游 v1.4.0）+ WebVPN 登录 + 文献库接入与判定。

- WebVPN 网址签名算法实测反推并验证（AES-128-CFB，key=iv=`wrdvpnisthebest!`）
- 纯标准库 AES-128（CBC/CFB128），用 FIPS-197 / NIST SP 800-38A 向量锁定
- 数据库目录（27 个）+ 三层访问判定（通道 / 机构标记 / 校准指纹）+ 未知即 unknown
- CLI（`--json` 契约）、守护 `hhuvpn guard`、独立入口 `standalone/`
- 可选附属 `integrations/`（Skill + MCP），核心与其解耦并有测试兜底
