# 独立使用（不需要任何 agent）

这一层是给**人**用的：双击即可，不涉及 MCP、Skill、harness。
把 `integrations/` 整个删掉，这里的功能照样完整（有测试兜底）。

> 文件名刻意用英文：`cmd.exe` 解析 UTF-8 批处理文件不可靠（多字节中文在 `( )` 块里会散架），
> 所以启动器一律纯 ASCII，中文提示由里面的 Python 输出来负责。

## 三个入口

| 文件 | 用途 |
| --- | --- |
| `open-databases.cmd` | 双击：**显式开启**（校园网认证 + 登 WebVPN），然后打开目标文献数据库 |
| `install-guard.cmd` | 双击：注册计划任务，每分钟检查一次 |
| `uninstall-guard.cmd` | 双击：删掉上面那个计划任务 |

### 守护到底管什么（校园网与 WebVPN 策略不同）

| | 策略 |
| --- | --- |
| **校园网** | **常驻自动登录**：掉线即重登，不用管 |
| **WebVPN** | **按需开启 + 开着就保活**：守护不主动开；你开了之后它负责续期，直到你 `vpn off` 或关机 |
| **关机后** | **保持关闭**：下次开机第一次运行时关掉 WebVPN 并复位开启意图，回到未开启状态 |

也就是说：**没让你操心校园网；WebVPN 不会在你不知情的时候挂着。**

用命令行控制 WebVPN：

```bat
python ..\hhuvpn.py vpn          :: 看状态（是否已开启 / 保活 / 开机是否自动关）
python ..\hhuvpn.py vpn on       :: 现在要用：开启
python ..\hhuvpn.py vpn off      :: 用完了：关闭并复位（守护不会自动重开）
```

想改成"守护也完全不管 WebVPN"：`config.ini` 里 `[webvpn] keepalive = false`；
想改成"开机后不要自动关"：`close_on_boot = false`。

## 典型用法

### 只想现在能上知网

双击 `open-databases.cmd`。带参数（命令行里）：

```bat
open-databases.cmd cnki
open-databases.cmd sd cnki
open-databases.cmd --portal          :: 只打开 WebVPN 门户首页
```

### 想让它一直自动（推荐）

1. 先 `python ..\hhuvpn.py config set-password` 填好账号（DPAPI 加密，不必明文落盘）；
2. 双击 `install-guard.cmd`；
3. 以后开机自动生效，掉线自动恢复，不用管。

它到底干了什么：

```bat
python ..\hhuvpn.py guard --status
type ..\state\hhuvpn.log
```

计划任务的名字是 `hhuvpn-guard`，可以用 PowerShell 查看：

```powershell
Get-ScheduledTask -TaskName hhuvpn-guard | Select TaskName,State
Get-ScheduledTaskInfo -TaskName hhuvpn-guard | Select LastRunTime,LastTaskResult
```

### 只想临时盯一会儿（不装计划任务）

```bat
python ..\hhuvpn.py guard                     :: 常驻，每分钟检查
python ..\hhuvpn.py guard --max-minutes 120   :: 盯两小时后自动退出
python ..\hhuvpn.py guard --once --db sd,cnki :: 跑一轮并顺带校验库
python ..\hhuvpn.py guard --clear-flag        :: 改过密码后解除断路
```

### 高级选项

```powershell
powershell -ExecutionPolicy Bypass -File install-guard.ps1 -IntervalMinutes 5 -Databases "sd,cnki"
powershell -ExecutionPolicy Bypass -File install-guard.ps1 -TaskName my-hhuvpn -UsePythonConsole
```

## 它和上游守护的分工

- 上游 `upstream/hhu_login.py`：只管校园网，成熟稳定；
- `hhuvpn guard`：校园网 + WebVPN 会话，两者都管，且与 CLI 共用同一份 `config.ini`。

两个都能装、互不冲突；只装一个也行。上游安装方式见 `upstream/README.md`。

## 关于浏览器的说明（重要）

脚本能拿到并维持 WebVPN 会话，但**浏览器的 cookie 是浏览器自己的**。
所以 `open-databases.cmd` 打开页面后若仍显示登录页，是因为浏览器还没有 WebVPN 会话——
在浏览器里登录一次 `https://webvpn.hhu.edu.cn/` 即可，之后浏览器会自己保持一段时间。

想让命令行程序直接取数据库页面（不需要浏览器登录），用脚本自己的会话：

```bat
python ..\hhuvpn.py fetch cnki --out cnki.html
python ..\hhuvpn.py session --cookie-file cookies.txt
```
