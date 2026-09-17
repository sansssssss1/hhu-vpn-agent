#!/usr/bin/env python3
"""【可选附属】MCP stdio 服务器：把 hhuvpn 暴露成 MCP 工具给 agent 用。

核心（hhuvpn/）完全不依赖本文件——删掉它，命令行照常工作。
本文件只做一件事：把 Connector 的既有能力翻译成 MCP 协议。

直接运行：
    python integrations/mcp_server.py        # 当作 MCP stdio 服务器
    hhuvpn mcp                                # 等价（CLI 转发到本文件）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 允许直接以脚本方式运行（不依赖安装成包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hhuvpn import __version__, databases as db_catalog  # noqa: E402
from hhuvpn.config import Config, load_config  # noqa: E402
from hhuvpn.connector import Connector  # noqa: E402
from hhuvpn.state import State  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
MAX_TEXT = 6000


def _tool(name: str, description: str, properties: dict, required: list | None = None) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required or []}}


TOOLS = [
    _tool("hhu_status", "查看校园网是否在线、学校 WebVPN 会话是否有效、指定文献数据库是否已可访问。",
          {"databases": {"type": "array", "items": {"type": "string"},
                         "description": "库别名，如 ['sd','cnki']；留空则只看链路状态"}}),
    _tool("hhu_ensure",
          "幂等操作：需要时自动登录校园网与学校 WebVPN，然后确认目标文献数据库已可用。"
          "这是文献相关任务开始前应该调用的工具；成功后即可用 hhu_fetch/浏览器继续访问数据库。"
          "注意：本工具只负责『登上数据库』，不负责检索或下载。",
          {"databases": {"type": "array", "items": {"type": "string"},
                         "description": "目标库别名，如 ['sd','cnki']"},
           "verify": {"type": "boolean", "description": "是否逐个校验库可达与身份，默认 true"}},
          []),
    _tool("hhu_login", "显式登录校园网 ePortal 或学校 WebVPN（一般用 hhu_ensure 即可）。",
          {"target": {"type": "string", "enum": ["all", "campus", "webvpn"],
                      "description": "登录目标，默认 all"}}),
    _tool("hhu_db_list", "列出内置的全部文献数据库别名与入口地址。", {}),
    _tool("hhu_db_url", "把库别名转换成 WebVPN 入口地址（可直接交给浏览器或 curl）。",
          {"database": {"type": "string", "description": "库别名，如 sd"}}, ["database"]),
    _tool("hhu_fetch", "用已登录的 WebVPN 会话抓取页面，确认认证通道可用。返回状态码、最终地址与正文片段。",
          {"target": {"type": "string", "description": "库别名或完整网址"},
           "path": {"type": "string", "description": "可选，追加到库首页后的路径"},
           "max_bytes": {"type": "integer", "description": "最多读取字节数，默认 200000"}},
          ["target"]),
    _tool("hhu_session", "查看当前 WebVPN 会话（cookie 数量、有效期），可导出 Netscape cookie 文件。",
          {"cookie_file": {"type": "string", "description": "可选，导出 cookie 文件路径"}}),
]


class Server:
    def __init__(self, cfg: Config, state_dir: Path | None = None):
        self.cfg = cfg
        self.state = State(state_dir or cfg.state_dir)
        self.conn = Connector(cfg, self.state, verbose=False)

    # ---------------------------------------------------------------- 工具实现
    def call_tool(self, name: str, args: dict) -> dict:
        args = args or {}
        if name == "hhu_status":
            self.conn.load_session()
            payload = {"campus": self.conn.campus_status(),
                       "webvpn": self.conn.webvpn_status()}
            aliases = db_catalog.split_aliases(args.get("databases"))
            if aliases:
                payload["databases"] = [self.conn.database_status(a, save=False) for a in aliases]
            return payload

        if name == "hhu_ensure":
            return self.conn.ensure(targets=db_catalog.split_aliases(args.get("databases")),
                                    verify_dbs=args.get("verify", True))

        if name == "hhu_login":
            from hhuvpn.config import resolve_credentials
            creds = resolve_credentials(self.cfg, allow_prompt=False)
            target = args.get("target", "all")
            out = {}
            if target in ("all", "campus"):
                out["campus"] = self.conn.ensure_campus(creds, force=True)
            if target in ("all", "webvpn"):
                out["webvpn"] = self.conn.ensure_webvpn(creds, force=True)
            return out

        if name == "hhu_db_list":
            return {"databases": [d.to_dict() for d in db_catalog.list_all(self.state.dir)]}

        if name == "hhu_db_url":
            return self.conn.db_url(args.get("database", ""))

        if name == "hhu_fetch":
            self.conn.load_session()
            db = db_catalog.get(args.get("target", ""), self.state.dir)
            target = db.url if db else args.get("target", "")
            resp, vpn = self.conn.webvpn.fetch(target, path=args.get("path"),
                                               max_bytes=int(args.get("max_bytes") or 200000))
            text = resp.text
            return {"status": resp.status, "final_url": resp.url, "vpn_url": vpn,
                    "bytes": len(resp.body), "content_type": resp.content_type,
                    "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT}

        if name == "hhu_session":
            info = self.conn.load_session()
            payload = {"state_dir": str(self.state.dir), "loaded": info,
                       "cookies": self.conn.session.cookies(),
                       "webvpn_authenticated": self.conn.webvpn.is_authenticated()[0]}
            if args.get("cookie_file"):
                payload["cookie_file"] = args["cookie_file"]
                payload["cookie_count"] = self.conn.session.export_netscape(
                    args["cookie_file"], domain_filter="webvpn")
            return payload

        raise ValueError(f"未知工具: {name}")

    # ---------------------------------------------------------------- JSON-RPC
    def handle(self, msg: dict):
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            params = msg.get("params") or {}
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "hhuvpn", "version": __version__},
                "instructions": ("河海大学校园网/WebVPN 连接器：只用它完成「登上文献数据库」"
                                 "（ScienceDirect、CNKI 等），不要用它做检索或下载。"
                                 "文献相关任务开始前先调用 hhu_ensure。")}}
        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = msg.get("params") or {}
            try:
                result = self.call_tool(params.get("name"), params.get("arguments") or {})
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                    "isError": False}}
            except Exception as exc:  # noqa: BLE001
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps({"error": repr(exc)}, ensure_ascii=False)}],
                    "isError": True}}
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}


def serve_stdio(cfg: Config | None = None, state_dir: Path | None = None) -> int:
    server = Server(cfg or load_config(), state_dir)
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": f"parse error: {exc}"}}),
                  file=out, flush=True)
            continue
        try:
            resp = server.handle(msg) if isinstance(msg, dict) else None
        except Exception as exc:  # noqa: BLE001
            print(f"[hhuvpn-mcp] handler error: {exc!r}", file=sys.stderr, flush=True)
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": repr(exc)}}
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), file=out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(serve_stdio())
