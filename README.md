# hhu-vpn-agent

在 [sqxart/hhu-autologin](https://github.com/sqxart/hhu-autologin) 基础上做的扩展：
**校园网自动登录 + 走学校 WebVPN 登上文献数据库（ScienceDirect / 知网 / Web of Science …）**。

两条使用路线，彼此独立：

| 路线 | 面向 | 依赖 | 入口 |
| --- | --- | --- | --- |
| **独立运行**（主体） | 人 | 只要 Python，双击即用 | `standalone/` 里的 .cmd、`hhuvpn` 命令、计划任务守护 |
| **agent 联动**（可选附属） | AI harness | 无额外依赖 | `integrations/`（Skill / MCP） |

> 把整个 `integrations/` 目录删掉，独立运行的每一项功能照常工作——
> 这条不是口号，有测试兜底：`tests/test_independence.py` 会临时把该目录藏起来再跑一遍 CLI。

> **边界（很重要）**：本项目只做到「登上数据库」——
> 校园网认证、WebVPN 登录、目标库可达性与机构身份确认、交出可用通道。
> 它**不**提供检索、筛选、下载、综述；那些是后续科研步骤，交给别的工具。

---

## 目录结构

```
hhuvpn/                 核心（纯标准库，独立运行，不依赖任何 agent 组件）
  campus.py             ePortal 校园网认证（协议沿用上游）
  webvpn.py             WebVPN 登录 + 网址签名
  databases.py          文献库目录（内置 27 个，可自建）
  verify.py             「是否已登上」的判定与指纹校准
  guard.py              守护：掉线重登 + WebVPN 保活（独立功能）
  cli.py / connector.py 命令行与编排
standalone/             给人用的独立入口（双击即可）
integrations/           可选附属：Skill 模板 + MCP 服务器（删掉不影响核心）
upstream/               上游 hhu-autologin v1.4.2 原样保留（MIT）
tools/upstream-sync.py  同步上游：info / fetch / diff 三步
docs/                   架构、实测笔记、验证清单
tests/                  离线测试（46 项）
CHANGELOG.md            本插件更新日志（含每次跟进上游的记录）
```

---

## 路线一：独立运行（推荐先看这条）

### 第 0 步：配账号（一次）

```powershell
python hhuvpn.py config set-password     # 输入学号 + 信息门户密码，DPAPI 加密保存
```

不想用加密存储就在 `config.ini` 的 `[account]` 段填明文（与上游同一份配置）；
也可以用环境变量 `HHU_USERNAME` / `HHU_PASSWORD`（不落盘）。

> **校园网与 WebVPN 是同一个账号**：同一套学号 + 同一个信息门户密码。
> 所以只填这一处、只改这一处；插件也不会因为校园网密码错就换个密码去试 WebVPN
> （同一个账号，那样只会把失败次数翻倍、触发验证码）。

### 第 1 步：现在就能上数据库

双击 `standalone/open-databases.cmd`，或者：

```powershell
python hhuvpn.py open            # 用配置里的默认库（sd, cnki）
python hhuvpn.py open cnki sd    # 指定库
python hhuvpn.py open --portal   # 只打开 WebVPN 门户首页
```

它会：**确认/建立校园网认证 → 登学校 WebVPN → 通过 WebVPN 进数据库** → 调默认浏览器打开。
这就是正常流程，默认行为（`--via webvpn`）：WebVPN 拿不到就算失败，如实报失败，不糊弄。

> 注意 `open` / `ensure` 属于"使用者说明要用"的显式动作，**它们会开启 WebVPN**；
> 后台守护不会（见下）。

> **备用通道（可选，不是默认）**：校园网出口 IP 上有些库本来就按 IP 授权
> （实测 ScienceDirect 在校内直连时页面里就带 `orgName: Hohai University`）。
> 这条通道只在两种情况下用：
> `--via auto`（先走 WebVPN，走不通时才允许兜底）或 `--via direct`（明确只用它）。
> 结果里的 `channel` 字段标明实际走的哪条；默认判断里，备用通道的成功**不算**主流程成功。

### 第 2 步：让它一直自动（可选但推荐）

双击 `standalone/install-guard.cmd`，注册一个每分钟运行的计划任务：

```
pythonw.exe "<项目>\hhuvpn.py" guard --once --quiet
```

守护管两件事，**校园网与 WebVPN 的策略是分开的**：

| | 策略 | 说明 |
| --- | --- | --- |
| **校园网** | **常驻自动登录** | 掉线即重登，不需要人在场，也不需要 agent |
| **WebVPN** | **按需开启 + 开着就保活** | 守护**不会主动开**；你（或你让 agent 执行 `ensure`/`open`）开了之后，守护负责续期，直到 `vpn off` 或关机 |
| 关机边界 | **关机后保持关闭** | 下次开机后的第一次守护运行会关掉 WebVPN 并复位"开启意图"，恢复到未开启状态 |

```powershell
python hhuvpn.py vpn on                 # 显式开启（使用者说明要用）
python hhuvpn.py vpn                  # 看状态：是否已开启 / 保活开关 / 开机是否自动关
python hhuvpn.py vpn off                # 显式关闭，并复位开启意图

python hhuvpn.py guard                  # 前台常驻（不装计划任务也能用）
python hhuvpn.py guard --once --db sd,cnki   # 跑一轮（WebVPN 未开启时会跳过库检查并说明原因）
python hhuvpn.py guard --status         # 上一轮干了什么
python hhuvpn.py guard --clear-flag     # 改完密码后解除断路
```

开启意图记在 `state/webvpn_intent.json`（`on`/`off`）：守护只看它，不看"本地碰巧有没有 cookie"。
想反过来（守护也完全不管 WebVPN）就设 `[webvpn] keepalive = false`；
想开机后不自动关闭就设 `close_on_boot = false`。

> 浏览器说明：脚本维持的是**脚本自己的** WebVPN 会话。浏览器有自己的 cookie，
> 首次打开若仍显示登录页，在浏览器里登录一次 `https://webvpn.hhu.edu.cn/` 即可。
> 不想碰浏览器就用 `hhuvpn fetch cnki --out cnki.html`（走脚本会话）。

### 独立运行的常用命令

| 命令 | 作用 |
| --- | --- |
| `hhuvpn open [库...]` | 登录 + 浏览器打开（人用一步到位） |
| `hhuvpn guard` | 守护：掉线重登 + WebVPN 保活 |
| `hhuvpn status [--db sd,cnki]` | 看当前状态 |
| `hhuvpn db list` | 有哪些库可用 |
| `hhuvpn db check cnki --evidence` | 校验某库并给出判定依据 |
| `hhuvpn fetch cnki --out cnki.html` | 用会话取页面 |
| `hhuvpn session --cookie-file cookies.txt` | 导出 cookie 给 curl 等工具 |
| `hhuvpn doctor` | 环境与链路体检 |
| `hhuvpn config set-password` | 保存账号（DPAPI 加密） |

---

## 路线二：agent / harness 联动（可选）

三种方式，任选其一；都不装也行——agent 直接跑上面的命令即可。

### A. Skill（DSH / Codex / Claude Code 等）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-skill.ps1
```

技能装好后，agent 遇到「查文献 / 知网 / ScienceDirect / 校外访问数据库」这类任务时，
会自己先 `hhuvpn ensure` 把数据库登上，再继续后面的事。

### B. MCP

见 `integrations/mcp.example.json`，或直接 `hhuvpn mcp`。
工具：`hhu_status` `hhu_ensure` `hhu_login` `hhu_db_list` `hhu_db_url` `hhu_fetch` `hhu_session`。

### C. 直接调 CLI（最通用）

```bash
hhuvpn ensure --db sd,cnki --json     # 幂等：需要就登录，然后确认目标库可用
hhuvpn url sd --json                  # 拿 WebVPN 入口
```

约定：任何子命令都支持 `--json`，输出**一个** JSON 对象（`{schema, command, ok, ...}`）；
失败也有 JSON（`ok=false` + `error.code/message/hint`）。
退出码：`0` 成功 · `1` 失败 · `2` 用法错 · `3` 需人工（验证码）· `4` 缺凭据。

---

## 它是怎么判断"已经登上数据库"的

不打肿脸充胖子，三档回答：

1. **通道层**：通过 WebVPN 拿到 200，且没被弹回统一身份认证登录页；
2. **内容层**：页面出现机构身份标记（默认 `河海大学|Hohai University`）或该库自带规则命中；
3. **指纹层**：跑过一次 `hhuvpn db calibrate <别名>` 后，用它学到的标记比对。

三档都不成立就返回 `authenticated: "unknown"`，并把标题、字节数、命中情况一并列出——
**宁可说不知道，也不假装成功**。依据可用 `db check --evidence` 查看。

---

## 与上游的关系

| 目录 | 内容 |
| --- | --- |
| `upstream/` | sqxart/hhu-autologin 原样保留（MIT）：`hhu_login.py` 守护、`hhu_gui.py` 图形控制台、`install.bat` |
| `hhuvpn/` | 新增核心：校园网协议沿用上游改写为库、WebVPN 登录、数据库目录与校验、守护、CLI |

- 配置**共用一份** `config.ini`（`[account]` 段两边都认），不用维护两套密码。
- 上游守护只管校园网；`hhuvpn guard` 管校园网 + WebVPN。两个都能装，互不冲突。
- 协议细节（含 WebVPN 网址签名算法）见 `docs/field-notes.md`。

## 安全

- `state/` 里是**登录态 cookie**，`config.ini` 可能含明文密码——都不要提交/外传（`.gitignore` 已排除）。
- 推荐 `hhuvpn config set-password`（Windows DPAPI，仅当前用户可解）或环境变量。
- 本项目不做任何绕过认证的事：登录用的就是学校统一身份认证，密码在本地加密后提交给学校服务器。

## 测试

```bash
python tests/run_all.py
```

覆盖：AES 官方向量与真实签名向量、WebVPN 地址签名/反解、登录页解析、配置与凭据优先级、
数据库判定与指纹、CLI JSON 契约与参数位置、MCP 协议、**核心不依赖可选组件**（含临时藏起
`integrations/` 的实测）。

需要你自己账号的端到端验证步骤见 `docs/verification.md`。

## 推送到 GitHub

仓库里**不应该**出现这两类东西（`.gitignore` 已排除，但推之前请自己再确认一遍）：

| 不要提交 | 为什么 |
| --- | --- |
| `config.ini` | 里面可能有你的学号（明文密码版还有密码） |
| `state/` | 登录态 cookie、会话 ticket、DPAPI 凭据、含学号的运行日志 |

```bash
cd hhu-vpn-agent
git init -b main
git add .
git status            # 确认列表里没有 config.ini / state/
git commit -m "feat: 河海校园网 + WebVPN 自动登录与文献数据库接入"
git remote add origin git@github.com:<你的账号>/hhu-vpn-agent.git
git push -u origin main
```

推送前自查（两条都应当**没有输出**）：

```bash
# 1) 有没有混进敏感文件（学号/密码/会话都在里面）
git ls-files | grep -E "^(config\.ini|state/|.*\.dpapi$)" && echo "↑ 先移除再提交" || echo "无敏感文件"
# 2) 有没有混进 10 位数字串（学号形态）——按需替换成你自己的敏感串
git grep -nE "[0-9]{10}" -- . || echo "无 10 位数字串"
```

发布包用 `tools/make-package.ps1` 生成：它会先排除 `config.ini` / `state/` / 缓存，
再对打出来的目录做一次**隐私自检**（命中就删包报错，宁可不发也不带出去）。

## 许可证

MIT。上游版权归 [sqxart/hhu-autologin](https://github.com/sqxart/hhu-autologin) 作者所有。
