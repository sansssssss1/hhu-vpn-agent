# 真机验证清单

分三层验证：**离线测试**（不需要账号）→ **半程验证**（不需要账号，能验证到认证接口）
→ **端到端验证**（需要你自己的账号）。另附**独立运行验证**（守护 / 计划任务 / 双击脚本）。

---

## 独立运行验证（不需要 agent，也不需要账号）

### 守护那条命令本身

```powershell
python hhuvpn.py guard --once --json      # 应输出 campus.state=online（在校园网内时）
python hhuvpn.py guard --status           # 应显示上一轮结果
```

### 计划任务链路（真实注册 → 触发 → 卸载）

```powershell
powershell -ExecutionPolicy Bypass -File standalone/install-guard.ps1 -TaskName hhuvpn-guard-selftest
Get-ScheduledTask -TaskName hhuvpn-guard-selftest | Select TaskName,State
Start-ScheduledTask -TaskName hhuvpn-guard-selftest
Start-Sleep 12
Get-Content state/guard.json              # 说明任务真的跑起来了
Get-ScheduledTaskInfo hhuvpn-guard-selftest | Select LastRunTime,LastTaskResult   # 期望 0
powershell -ExecutionPolicy Bypass -File standalone/uninstall-guard.ps1 -TaskName hhuvpn-guard-selftest
```

> 实测记录（2026-09-16）：注册 state=Ready，手动触发后 `LastTaskResult=0`，
> `state/guard.json` 被写入，卸载后查询为空 —— 这条链路是通的。

### 双击脚本

```powershell
standalone\open-databases.cmd --portal --no-browser   # 只验证流程，不开浏览器
standalone\open-databases.cmd cnki --no-browser
```

期望：先打印项目路径，再打印人类可读的结果与退出码；未配置账号时给出明确指引。

### 核心与可选组件解耦

```powershell
python tests/test_independence.py
```

它会临时把 `integrations/` 改名藏起来，再跑 `db list` / `url` / `mcp`：
前两者必须完全正常，`mcp` 必须只给"可选组件不存在"的友好提示，然后自动恢复目录。

---

## 第 0 层：离线测试（30 秒，无需账号）

```bash
python tests/run_all.py
```

期望：`ALL SUITES PASSED`（34 项）。其中包含两个"真实世界向量"：

- `test_webvpn_host_signature_live_vector`：AES 实现算出的主机签名与**线上抓到的**CAS 跳转
  地址逐字节一致——签名算法没写错；
- `test_nist_cfb128` / `test_fips197_block` / `test_nist_cbc`：NIST/FIPS 官方测试向量——
  自写的纯 Python AES 没写错。

## 第 1 层：半程验证（无需账号）

```bash
python hhuvpn.py doctor --json
```

应看到：

| 检查项 | 期望 |
| --- | --- |
| `campus_online` | 在校园网内为 true（204 探针）；校外为 false 也正常 |
| `webvpn_reachable` | true，最终地址是 `/https/<sig>/authserver/login?...` |
| `webvpn_session` | 未登录时为 false，reason=`redirected_to_cas` |
| `credentials` | 配好账号后 source=`config`/`dpapi`/`env` |

再看一次"没凭据时不装成功"：

```bash
python hhuvpn.py ensure --db sd,cnki --json
# 期望：ok=false, error.code=credentials_missing, 退出码 4
```

（可选）用**不存在的账号**验证表单构造是否正确——服务端应当返回凭据类错误，
而不是解密/表单错误：

```bash
$env:HHU_USERNAME='0000000000'; $env:HHU_PASSWORD='NotARealPassword-1'
python hhuvpn.py --json login webvpn
# 期望：error_code=auth_rejected，error_text 形如"该账号非常用账号或用户名密码有误"
```

> 注意：这会产生一次失败登录计数（针对不存在的账号，不影响真实账号），
> 只做一次即可，别反复跑。

## 第 2 层：端到端（需要你的账号）

```bash
python hhuvpn.py config set-password      # 输入学号 + 信息门户密码（DPAPI 加密保存）
python hhuvpn.py ensure --db sd,cnki --json
```

期望 JSON 形状：

```json
{
  "ok": true,
  "steps": [
    {"name": "campus",  "action": "already_online"},
    {"name": "webvpn",  "action": "logged_in", "ok": true}
  ],
  "databases": [
    {"alias": "sd",   "authenticated": true, "vpn_url": "https://webvpn.hhu.edu.cn/https/.../"},
    {"alias": "cnki", "authenticated": true, "vpn_url": "https://webvpn.hhu.edu.cn/https/.../"}
  ],
  "ready": ["sd", "cnki"],
  "state_dir": ".../state"
}
```

如果某个库是 `authenticated: "unknown"`——说明通道通了但页面里没看到机构标记，
先看依据：

```bash
python hhuvpn.py db check sd --evidence --json
```

在**确认人眼看到机构名已登录**的情况下，学习该库的真实指纹，之后判定就准了：

```bash
python hhuvpn.py db calibrate sd
python hhuvpn.py db check sd --json
```

### 验证"真的登上去了"的三个旁证

1. `hhuvpn fetch cnki --out cnki.html`，打开看页面右上角是否显示"河海大学"；
2. `hhuvpn session --cookie-file cookies.txt`，用 curl 带 cookie 直接取库首页：
   `curl -b cookies.txt "https://webvpn.hhu.edu.cn/https/<sig>/"`；
3. `hhuvpn url sd --open`，浏览器里打开应能直接看到全文入口而不是"Get Access"。

### 会话复用与失效

- 登录成功后 cookie 存在 `state/session.json`，默认 120 分钟内有效（`session_ttl_minutes`）；
- 再次 `ensure` 会先做一次线上校验，有效就跳过登录（`action: already_logged_in`）；
- 学校侧会话过期后会重新走登录，不需要人工干预；
- `hhuvpn logout` 注销并清理本地 cookie。

## 常见问题定位

| 现象 | 先看 |
| --- | --- |
| 一直卡在 `captcha_required` | 说明触发了滑块验证码（captchaSwitch=2）。用 `login webvpn --browser` 人工过一次，再 `login --ticket <值>` 导入 |
| `ensure` 说 `already_online` 但库打不开 | 库可能需要 WebVPN，而不是校园网直连；看 `databases[].note` |
| HTTP 200 但页面是登录页 | 判定层会把它标成不可达；若没标，用 `db check --evidence` 看规则 |
| 校外（家里）使用 | 校园网那步会是 `portal_unreachable`（正常），WebVPN 那步仍应成功 |
| 守护一直不动 | `guard --status` 看是否已断路（auth_failed.flag）；改密码后 `guard --clear-flag` |
| 计划任务注册失败 | Windows PowerShell 必须能访问计划任务服务；用管理员或非受限终端重试 |
| .cmd 乱码 | 启动器刻意写成纯 ASCII（cmd.exe 解析 UTF-8 批处理不可靠）；中文提示由 Python 输出 |
