# 可选附属：和 harness / agent 联动

**这里的所有东西都是可选的。** 核心功能（校园网自动登录、WebVPN 登数据库、守护任务）
全部在 `hhuvpn/`、`standalone/` 与 CLI 里，不依赖本目录；把整个 `integrations/` 删掉，
`hhuvpn` 的一切命令照常工作（`hhuvpn mcp` 会提示"可选组件不存在"，其它不受影响）。

## 两种接入方式

### 1. Skill（推荐给支持 skills 的 harness：DSH / Codex / Claude Code）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-skill.ps1
```

把 `integrations/SKILL.md` 安装到 skills 目录（默认 `%USERPROFILE%\.codex\skills\hhu-database-access`），
安装时会把 `{{HHUVPN_DIR}}` 替换成本机真实路径。

带来的效果：agent 在遇到「查文献 / 知网 / ScienceDirect / 校外访问数据库」这类任务时，
会自己先调用 `hhuvpn ensure` 把数据库"登上"，然后才继续做后面的事。

### 2. MCP（推荐给支持 MCP 的客户端）

```json
{
  "mcpServers": {
    "hhuvpn": {
      "command": "python",
      "args": ["E:/path/to/hhu-vpn-agent/integrations/mcp_server.py"]
    }
  }
}
```

或直接经 CLI 转发：`hhuvpn mcp`（等价）。

暴露的工具：`hhu_status` `hhu_ensure` `hhu_login` `hhu_db_list` `hhu_db_url` `hhu_fetch` `hhu_session`。

### 3. 什么都不装（最省事）

任何能执行命令的 agent 都能直接用 CLI，约定是 `--json`：

```bash
hhuvpn ensure --db sd,cnki --json
```

## 边界

无论是 Skill、MCP 还是 CLI，对外能力都只有一件事：**登上数据库**
（校园网认证 → WebVPN 登录 → 目标库可达性与机构身份确认 → 交出可用通道）。
检索、筛选、下载、综述不在范围内——那些交给科研类 skill。

## 文件

| 文件 | 说明 |
| --- | --- |
| `SKILL.md` | 技能模板（含 `{{HHUVPN_DIR}}` 占位符，由安装脚本替换） |
| `mcp_server.py` | MCP stdio 服务器（也能直接 `python integrations/mcp_server.py` 跑） |
| `mcp.example.json` | MCP 配置示例 |
