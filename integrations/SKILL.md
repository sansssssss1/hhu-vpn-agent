---
name: hhu-database-access
description: 河海大学校园网与学校 WebVPN 自动登录，用学校身份登上 ScienceDirect、CNKI 知网、Web of Science、IEEE、万方、维普等文献数据库。当任务涉及查文献、找论文、下载知网/ ScienceDirect 全文、校外访问数据库、校园网掉线重连、需要走学校 VPN 访问图书馆电子资源时使用。只负责"登上数据库"这一步（认证 + 可达性确认 + 给出可用通道），不负责检索、筛选、下载与综述。触发词：知网、CNKI、ScienceDirect、SD、Web of Science、IEEE、万方、维普、学校 VPN、WebVPN、校外访问、文献数据库、校园网登录。
allowed-tools: Bash Read
license: MIT
compatibility: 需要 Windows/macOS/Linux 上的 Python 3.10+，仅用标准库。需要用户已配置学号与信息门户密码，并且当前能访问学校 WebVPN（校外亦可）。
metadata:
  version: "1.0"
  skill-author: "hhu-vpn-agent"
  plugin-dir: "{{HHUVPN_DIR}}"
---

# 河海大学文献数据库接入（hhuvpn）

你的任务只有一个：**在开始任何文献相关工作之前，把用户的机器变成"已登上目标数据库"的状态**，
并把可用的认证通道交给后续步骤。

不要自己写登录脚本、不要碰 ePortal/WebVPN 协议细节——这个插件已经把协议和坑都处理好了。

## 边界（必须遵守）

- 只做到"登上数据库"。**不要**在本技能里做检索、结果筛选、批量下载、写综述。
- 登上之后，把通道（WebVPN 入口地址 / `hhuvpn fetch`）交出去，由后续步骤或用户决定做什么。
- 不要向用户索要密码。缺凭据时直接告诉用户运行哪条命令自己填（见下文"缺凭据"）。

## 插件位置

插件目录：`{{HHUVPN_DIR}}`，入口是 `hhuvpn.py`（等价包装：`hhuvpn.cmd`）。

调用形式（安装脚本已把 {{HHUVPN_DIR}} 换成本机真实路径，直接照抄即可）：

```bash
python "{{HHUVPN_DIR}}/hhuvpn.py" <子命令> --json
```

如果你不确定目录在哪，找 `hhuvpn.py`：`Get-ChildItem -Path <项目根> -Recurse -Filter hhuvpn.py`。

## 标准流程

### 第 1 步：一条命令搞定（90% 的情况）

```bash
python "{{HHUVPN_DIR}}/hhuvpn.py" ensure --db sd,cnki --json
```

`ensure` 是幂等的：已在线就不动校园网；本地 WebVPN 会话还有效就不重新登录；
然后逐个确认目标库是否已可访问。**先跑它，再决定要不要做别的。**

目标库别名按用户需求给：`sd`（ScienceDirect）、`cnki`（知网）、`wos`（Web of Science）、
`ieee`、`wanfang`、`cqvip`、`springer`、`wiley`、`acs`、`asce`、`lib`（图书馆首页）…
不确定有哪些就 `db list --json`。

### 第 2 步：读结果

只看这几个字段：

| 字段 | 含义 | 你要做什么 |
| --- | --- | --- |
| `ok: true` | 链路 + 至少一个库确认可用 | 继续，把 `ready` 里的库和 `databases[].vpn_url` 交给后续步骤 |
| `steps[].action` | `already_online` / `logged_in` / `already_logged_in` / `skipped` / `failed` | 判断是哪一层出的问题 |
| `databases[].authenticated` | `true` / `false` / `"unknown"` | `true` 才算真的登上；`"unknown"` 是**无法确认**，不是失败 |
| `error.code` + `error.hint` | 失败原因与建议 | 按下面的对照表处理 |

### 关于 WebVPN：它是"按需开启"的

用户设定的策略是：**校园网常驻自动登录；WebVPN 只在使用者要用的时候开，开了之后由守护保活，
关机后到下次开机前保持关闭**。所以你（agent）要遵守：

- 要访问数据库时，正常跑 `ensure` / `open` / `fetch` 即可 ——
  这属于"使用者说明要用"，会正常开启 WebVPN；`vpn on` 是更直白的写法。
- **不要**为了让库可用而反复开启/重登 WebVPN；**不要**去改用户的开关设置。
- 如果 `vpn` 状态显示 `intent: off`，说明用户没打算现在用 WebVPN：
  不要自作主张 `vpn on`，先问一句要不要开。
- 用户说"关掉 VPN"时执行 `vpn off`（会注销并复位意图，守护不会自动重开）。

### 第 3 步：按需使用通道

- 要给人打开：`open <库>`（默认开 WebVPN 入口地址）
- 要给 agent 取页面：`fetch <库> --path "/..." --out 文件`（走已登录的 WebVPN 会话）
- 要把会话交给别的工具：`session --cookie-file cookies.txt --json`

**正常流程是 校园网 → 学校 WebVPN → 数据库**，默认就是这条（`--via webvpn`）：
WebVPN 登不上（例如没配账号 `credentials_missing`）时，`ensure` 就是**失败**——
如实报告失败，别把"校内 IP 直连能用"包装成任务完成。

`channel` 字段：`webvpn` = 通过学校 WebVPN（正常流程）；`direct` = 校内 IP 直连，
**只在显式 `--via auto`（WebVPN 走不通时兜底）或 `--via direct`（只用备用通道）时才会出现**。
向用户汇报时要说清是哪条通道，以及 WebVPN 那条是否真的走通了。

### 账号只有一份（校园网 = WebVPN）

校园网 ePortal 认证与学校 WebVPN（统一身份认证）用的是**同一个账号**：同一套学号 +
同一个信息门户密码，`config.ini [account]` 里填一次两处通用，报告里的
`account_scope` / `credentials.scope` 都是 `campus+webvpn`。

因此：
- 用户说"密码不对/改了密码"时，只需改一处，别建议他配两套账号；
- 若报告里校园网那步是 `auth_rejected` / `captcha_required`，WebVPN 那步会被**主动跳过**
  （`shared_credentials: true`）——这是对的，不要劝用户"再试一次 WebVPN"；
- 反过来，"不在校园网/门户不可达"这类非凭据问题**不会**跳过 WebVPN（校外正是靠它）。

## 失败对照表

| error.code | 含义 | 正确的下一步 |
| --- | --- | --- |
| `credentials_missing` | 没配账号密码 | 告诉用户运行 `python "{{HHUVPN_DIR}}/hhuvpn.py" config set-password`（自己输密码，你不要代填） |
| `auth_rejected` | 学号/密码错，或被服务器拒绝 | 让用户核对密码；不要在短时间内反复重试（会触发验证码） |
| `captcha_required` | 需要验证码 | 图形验证码：把 `error.captcha.captcha_image` 路径指给用户，请其读出后跑 `login webvpn --captcha <码>`；滑块验证码：跑 `login webvpn --browser`，用户登录后复制 ticket，再 `login --ticket <值>` |
| `portal_unreachable` | 不在校园网 / 门卫没劫持 | 提示用户连上 `Hohai University` 无线或确认在校园网内 |
| `webvpn_unreachable` | 学校 WebVPN 连不上 | 检查网络/代理；校外同样应可访问 |
| `db_not_authenticated` | WebVPN 已登，但库没确认机构身份 | 先 `db check <库> --evidence` 看依据；可能是学校未订购该库，或需要 `db calibrate` 重新学习指纹 |
| `unknown_database` | 别名不认识 | `db list --json` 里选，或 `db add <别名> <网址>` |

退出码：`0` 成功 · `1` 失败 · `2` 用法错 · `3` 要人工介入（验证码）· `4` 缺凭据。

## 几条纪律

- **先 `ensure`，再谈别的**。没有确认"已登上"就不要开始任何数据库操作，否则会拿到登录页
  当成正常页面，得出"查不到文献"的错误结论。
- `authenticated: "unknown"` 要如实转述为"无法确认是否已登录"，不要包装成成功。
- 不要重复重试登录失败：连续失败会触发滑块验证码，反而把用户挡在门外。
- 输出里可能含会话 cookie，**不要把 cookie、ticket 原文写进回答或日志**。
- 用户的学号/密码只存在于本机配置或环境变量里；任何时候都不要要求用户把密码贴给你。

## 参考

- 用法与字段说明：`README.md`
- 协议细节与实测记录：`docs/field-notes.md`
- 真机验证步骤：`docs/verification.md`
