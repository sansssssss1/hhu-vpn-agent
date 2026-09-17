"""文献数据库目录：别名 → 真实网址 → 访问校验规则。

目录分两层，用户层覆盖内置层：
  - 内置：hhuvpn/data/databases.json（随插件发布）
  - 用户：<state_dir>/databases.user.json（hhuvpn db add 写入，重装不丢）

注意本模块的边界：这里只回答「这个库的入口在哪、怎么确认已经登上」，
不含任何检索词、结果解析、批量下载逻辑——那些是后续科研步骤，不在本插件范围。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
BUILTIN = DATA_DIR / "databases.json"
USER_FILE = "databases.user.json"

# 常见叫法 → 内置别名（用户/agent 说什么都能对上）
ALIAS_SYNONYMS = {
    "sciencedirect": "sd", "elsevier": "sd", "爱思唯尔": "sd",
    "知网": "cnki", "中国知网": "cnki", "cnki.net": "cnki",
    "kns": "cnki-kns", "知网检索": "cnki-kns",
    "万方数据": "wanfang", "维普": "cqvip",
    "webofscience": "wos", "sci": "wos", "web of science": "wos", "woscc": "wos",
    "ieeexplore": "ieee", "ieee xplore": "ieee",
    "springerlink": "springer", "斯普林格": "springer",
    "wiley": "wiley", "acs": "acs", "rsc": "rsc",
    "taylor": "tandf", "taylor&francis": "tandf",
    "sage": "sage", "oup": "oup", "oxford": "oup",
    "cambridge": "cambridge", "science": "science", "nature": "nature",
    "aps": "aps", "aip": "aip", "asce": "asce", "jstor": "jstor",
    "ebsco": "ebsco", "emerald": "emerald", "proquest": "proquest",
    "读秀": "duxiu", "nstl": "nstl", "图书馆": "lib", "lib": "lib",
}


@dataclass
class Database:
    alias: str
    name: str
    host: str
    scheme: str = "https"
    entry: str = "/"
    tags: list = field(default_factory=list)
    verify: dict = field(default_factory=dict)
    source: str = "builtin"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}{self.entry}"

    def to_dict(self) -> dict:
        return {"alias": self.alias, "name": self.name, "host": self.host, "scheme": self.scheme,
                "entry": self.entry, "url": self.url, "tags": self.tags,
                "verify": self.verify, "source": self.source}


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def load_catalog(state_dir: Path | None = None) -> tuple[dict, dict]:
    """返回 (databases, defaults)。"""
    builtin = _load_json(BUILTIN)
    dbs: dict[str, dict] = dict(builtin.get("databases", {}))
    defaults = dict(builtin.get("defaults", {}))
    if state_dir:
        user = _load_json(Path(state_dir) / USER_FILE)
        dbs.update(user.get("databases", {}) or {})
        if user.get("defaults"):
            defaults.update(user["defaults"])
            defaults["source"] = "user"
    return dbs, defaults


def normalize_alias(name: str) -> str:
    key = (name or "").strip().lower()
    return ALIAS_SYNONYMS.get(key, key)


def get(name: str, state_dir: Path | None = None) -> Database | None:
    dbs, _ = load_catalog(state_dir)
    alias = normalize_alias(name)
    raw = dbs.get(alias)
    if raw is None:
        # 允许直接给域名或完整网址
        if name.startswith("http://") or name.startswith("https://") or "." in name:
            from urllib.parse import urlsplit
            u = urlsplit(name if "://" in name else "https://" + name)
            raw = {"name": u.hostname, "host": u.hostname, "scheme": u.scheme or "https",
                   "entry": u.path or "/", "tags": ["自定义"]}
            alias = u.hostname or name
        else:
            return None
    src = "user" if name in (_load_json(Path(state_dir) / USER_FILE).get("databases", {})
                             if state_dir else {}) else "builtin"
    return Database(alias=alias, name=raw.get("name", alias), host=raw.get("host", ""),
                    scheme=raw.get("scheme", "https"), entry=raw.get("entry", "/"),
                    tags=list(raw.get("tags", [])), verify=dict(raw.get("verify", {})), source=src)


def list_all(state_dir: Path | None = None) -> list[Database]:
    dbs, _ = load_catalog(state_dir)
    out = [get(a, state_dir) for a in dbs]
    return [d for d in out if d]


def add_user_db(state_dir: Path, alias: str, name: str, host: str, entry: str = "/",
                scheme: str = "https", tags=None, verify=None) -> Path:
    path = Path(state_dir) / USER_FILE
    data = _load_json(path)
    data.setdefault("schema", 1)
    data.setdefault("databases", {})
    data["databases"][normalize_alias(alias)] = {
        "name": name or alias, "host": host, "scheme": scheme, "entry": entry or "/",
        "tags": tags or ["自定义"], "verify": verify or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def split_aliases(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = re.split(r"[,\s;，、]+", str(raw))
    return [x for x in (i.strip() for i in items) if x]


__all__ = ["Database", "load_catalog", "get", "list_all", "add_user_db", "split_aliases",
           "normalize_alias", "ALIAS_SYNONYMS", "USER_FILE"]
