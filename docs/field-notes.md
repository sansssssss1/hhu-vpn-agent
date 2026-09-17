# 实测笔记（河海大学校园网 / WebVPN）

这里记录的是**抓包与实测**得到的协议细节，不是猜的。所有结论都标了验证方式，
方便学校升级系统后快速定位哪一层变了。

实测时间：2026-09（江宁校区无线网，已认证在线状态）。

---

## 1. 校园网 ePortal（锐捷）

沿用上游 hhu-autologin 的协议：

- 门卫重定向链：`http://1.2.3.4/` → `10.96.0.155` → `eportal.hhu.edu.cn/eportal/redirectortosuccess.jsp`
- 服务列表：`POST http://eportal.hhu.edu.cn/eportal/InterFace.do?method=getServices`（body 为空）
- 登录：`POST .../InterFace.do?method=login`，所有参数做**两次** `encodeURIComponent`
- 在线判定：`http://connect.rom.miui.com/generate_204` 返回 204

### 新发现：已在线时门户会 302 到一个不可达占位地址

```
hop0 302 http://eportal.hhu.edu.cn/                         -> .../eportal/redirectortosuccess.jsp
hop1 302 http://eportal.hhu.edu.cn/eportal/redirectortosuccess.jsp -> http://123.123.123.123
hop2 ERR http://123.123.123.123   TimeoutError: timed out (4.01s)
```

**影响**：如果不先判在线就去跟门户重定向链，任何 HTTP 客户端都会卡到超时
（上游靠「先 probe 204 再决定要不要登录」避开了这个坑）。
本插件在 `campus.find_login_page()` 里显式规定：重定向目标跳出校园域就立即停止，
并把它解释为「门户已放行」。

### 1.1 代理软件会顶掉校园网请求（上游 v1.4.1 阻断级修复，本项目同步移植）

现象：开着 Clash 等系统代理时，发往 `eportal.hhu.edu.cn`（内网 10.96.0.155）的请求
被 urllib 默认送进代理，全部 `ConnectionRefused 10061` —— 表现为"不在校园网"误报、
守护整条登录链路瘫痪；掉线探针还可能被代理"代答"出 204，造成**假在线**。

修法（与上游一致，见 `hhuvpn/http.py` 的两处 `build_opener`）：

```python
urllib.request.build_opener(urllib.request.ProxyHandler({}), ...)
```

- 显式传一个**空映射**的 `ProxyHandler`，让 `build_opener` 跳过默认的 `ProxyHandler`；
- 空映射不会生成任何 `<scheme>_open` 钩子，请求即直连；
- 会话 opener 与 204 探针 opener **两处都要改**，少一处就还留着"假在线"的口子。

回归测试：`tests/test_http_direct.py`（先证明默认 opener 确实会挂系统代理，
再断言本插件的 opener 不带非空代理映射），并额外检查探针源码保留了空 `ProxyHandler`。

> 实测（2026-09-16）：把 `http_proxy/https_proxy` 指向死端口 `127.0.0.1:9` 后，
> `probe_http(generate_204)` 仍返回 204、`eportal` 仍返回 302 —— 直连生效。

---

## 2. WebVPN：网瑞达 wEngine

| 项 | 值 |
| --- | --- |
| 入口 | `https://webvpn.hhu.edu.cn`（HTTP 307 → HTTPS） |
| 会话 cookie | `wengine_vpn_ticketwebvpn_hhu_edu_cn` |
| 认证 | 金智教育统一身份认证 `authserver`（CAS），与校园网同一套学号/密码 |
| 前端脚本 | `/wengine-vpn/js/main.js`、CAS 侧 `.../cusThemenew/static/...` |

### 2.1 网址签名（关键）

访问任何校外资源都要把目标网址塞进 WebVPN 路径：

```
https://webvpn.hhu.edu.cn/{http|https}/<sig(host)>/<path>?<query>
```

`sig(host)` 的算法（**已用真实跳转地址反推验证**）：

```python
sig = hex("wrdvpnisthebest!") + hex(AES-128-CFB(key="wrdvpnisthebest!", iv="wrdvpnisthebest!", host))
```

验证：抓到的 CAS 跳转地址里 `sig = 77726476706e69737468656265737421f1e2559434357a467b1ac7a490406d301894467e2b`，
反解得到 `authserver.hhu.edu.cn`；本仓库 `hhuvpn/aes.py` 正向算出的值与之逐字节一致
（`tests/test_aes.py::test_webvpn_host_signature_live_vector`）。

因为 IV 固定等于 key，签名是**确定性**的：同一个主机永远同一个 sig，所以可以反解
（`hhuvpn url unwrap`，也用于把学校资源列表变成可读清单）。

### 2.2 CAS 登录页结构

| 元素 | 说明 |
| --- | --- |
| `#pwdEncryptSalt` | 每次会话随机的 16 字符密钥 |
| `var service = [...]` | `https://webvpn.hhu.edu.cn/login?cas_login=true` |
| 表单 action | `/https/<sig>/authserver/login`（**不带** query） |
| `captchaSwitch` | 实测 `"2"`（滑块验证码）；`"1"` 为图形验证码 |
| `checkNeedCaptcha.htl?username=X` | 返回 `{"isNeed":true|false}` |
| 失败响应 | HTTP **401** + 重新渲染登录页 |

**坑 1**：form action 上没有 `service`，提交时由 JS 补上
（`utils.setUrlParam("loginFromId","?service",...)`）。
不补 `service` 的话 CAS 不会带 ticket 跳回 WebVPN。

### 2.3 密码字段的加密（为什么要随机前缀）

页面 JS：

```js
function getAesString(data, key, iv) {
  key = CryptoJS.enc.Utf8.parse(key.trim());
  iv  = CryptoJS.enc.Utf8.parse(iv);
  return CryptoJS.AES.encrypt(data, key, {iv, mode: CBC, padding: Pkcs7}).toString();
}
function encryptAES(data, key) { return key ? getAesString(randomString(64)+data, key, randomString(16)) : data; }
```

即：**明文 = 64 字节随机前缀 + 密码**，key = salt，IV 随机 16 字符且**不上传**。

服务端要能在不知道 IV 的前提下还原出密码，唯一可行的解释是：
CBC 解密时错误的 IV 只会污染**第一个分组**，密码位于末尾分组，
因此服务端丢掉前面的分组就能拿到密码——随机前缀正是用来吸收这个污染的。

本插件的实现（`hhuvpn/aes.py::encrypt_cas_password`）保持同样的「64 字节随机前缀 + 密码」结构，
IV 取固定值。**实测有效**：用不存在的账号发起真实登录，服务端返回的是凭据类错误

```
HTTP 401 / "该账号非常用账号或用户名密码有误"
```

而不是解密失败或表单错误——说明密文格式被服务端正确解析。

### 2.4 完整登录链路（实测各跳）

```
GET  https://webvpn.hhu.edu.cn/                      200 → CAS 登录页（含 salt）
GET  <prefix>/authserver/checkNeedCaptcha.htl?...    200 {"isNeed":false}
POST <prefix>/authserver/login?service=...           401（凭据错）/ 302（成功，Location 带 ticket=ST-...）
GET  https://webvpn.hhu.edu.cn/login?cas_login=true&ticket=ST-...   下发 wengine_vpn_ticket...
```

### 2.5 TLS

`webvpn.hhu.edu.cn` 的证书在本机（Python 默认 CA 库）通过严格校验。
但校园里自签证书很常见，所以 `[webvpn] tls_verify = auto` 的策略是：
先严格校验，若抛 `SSLError` 则降级一次并**明确告警**（不会静默降级）。

---

## 2.6 校内 IP 直连：不是所有库都需要 WebVPN

在校内（江宁无线网）直接抓各库首页的实测结果（2026-09-16）：

| 库 | 直连结果 | 判定依据 |
| --- | --- | --- |
| ScienceDirect | **已认证** | HTML 内嵌 JSON：`"userName":"","orgName":"Hohai University","webUserId":"1968895"` |
| lib.hhu.edu.cn | 已认证 | 站点自带校名（`河海大学图书馆`） |
| www.cnki.net | 无法确认 | 首页静态 HTML 里没有任何机构信息（内容由 JS 渲染） |
| kns.cnki.net | 无法确认 | 直连先撞上 CNKI 的「安全验证」反爬页，拿不到真实页面 |
| www.webofscience.com / 万方 | 无法确认 | 同样需要 JS 或 WebVPN 会话 |

结论与设计影响：

1. **校内能直连就别绕 VPN**：`ensure`/`db check` 默认 `via=auto`——先直连、失败再 WebVPN，
   结果里 `channel` 标明通道。这样 WebVPN 因验证码/没配账号登不上时，校内可用的库照样能被确认。
2. **找不到标记就说"无法确认"**，不因为 HTTP 200 就宣称成功——CNKI 就是典型的"页面正常但看不出来"。
3. **`orgName` 是最可靠的标记**（ScienceDirect）：它是给前端用的结构化字段，
   不是页面文案，不会因为新闻/页脚出现校名而误判。

### 2.6.1 一个被真实数据打脸的假阳性

早先给 CNKI 写的规则里有 `机构用户` 这一项，本意是"页面显示机构用户信息=已认证"。
实际抓下来才发现：**CNKI 首页上明晃晃写着"机构用户"四个字，跟认证与否毫无关系**，
于是 `db check cnki` 一度报告"已登上"——这正是本模块声称要避免的假阳性。

修法（`hhuvpn/verify.py`）：

- 机构名必须落在**像认证信息的上下文**里才算数：
  `(?:机构用户|所属机构|欢迎|您好|登录用户|当前IP|institution|orgName|entitled)[^<>]{0,40}(?:河海大学|Hohai University)`
  或 `(?:河海大学|…)[^<>]{0,20}(?:图书馆|读者|用户|机构)`；模板可用 `defaults.verify.institution_patterns` 调；
- 每次命中都在结果的 `evidence` 里给出**原文片段**，人和 agent 都能自己核对；
- 写错的正则不再静默退化成子串匹配，而是记进 `rules_invalid` 暴露出来
  （目录里原有两条 `河海大学|(?i)hohai` 就是非法正则——`(?i)` 不在开头）。

回归测试：`tests/test_verify.py` 里
`test_cnki_bare_机构用户_is_not_evidence`、`test_institution_in_unrelated_context_is_not_evidence`、
`test_real_orgname_marker_is_evidence_with_snippet`、`test_invalid_rule_is_surfaced_not_silently_skipped`。

---

## 2.7 真机下载验证（2026-09-16）

用真实账号走了一遍"开 VPN → 下论文 → 删掉 → 关 VPN"：

| 项 | 结果 |
| --- | --- |
| WebVPN 登录 | 成功（CAS 302 + ticket；会话 6 个 cookie） |
| ScienceDirect 认证 | `ensure --db sd` 通过 WebVPN 确认到机构身份 |
| 论文下载 | **Springer 2 篇成功**：11 MB / 约 10 页、1.8 MB / 约 26 页（走 WebVPN 出口 IP，机构授权生效） |
| ScienceDirect PDF 端点 | 该轮未取到 PDF（先被我连爬 10 个文章页触发 429 限流，冷却后 `/pdfft` 仍返回 HTML 查看器页） |

两条经验：

1. **不要连爬文章页**。SD 对短时间多次请求会回 429（实测 10 页即触发）；
   取 PDF 优先用确定性地址 + 公开元数据 API 拿 DOI，别去逐页解析。
2. **429 必须单独识别**：它既不是"未授权"也不是"页面改版"，报成"未命中机构标记"
   会让人以为学校没订这个库。现在 `verify` 会给出 `rate_limited: true` 与明确说明。

## 3. 探测脚本

`tools/live-probe/` 里保留了当时用来摸清链路的脚本（只读、不改任何状态）：

- `probe_webvpn.py`：打印 CAS 登录页结构、salt、captchaSwitch、cookie
- `cas_probe.py`：用**假账号**走一遍完整 POST，验证表单构造（会产生一次失败登录计数，
  只对不存在的账号，不影响任何真实账号）
- `grab.py`：抓取并保存任意 URL

## 4. 环境备注

- 本机 `curl.exe`（schannel）在当前沙箱里 TLS 握手失败
  （`AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`），
  但 Python 的 TLS 正常；本插件的所有实现都走 Python，不受影响。
- 受限沙箱里 `tempfile.mkdtemp` 新建的**随机名目录**会被拒绝写入，
  固定名目录正常——测试代码因此用 `tests/.tmp/work`。
