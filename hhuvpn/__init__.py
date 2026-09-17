"""hhuvpn —— 河海大学校园网 / WebVPN 自动登录与数据库访问连接器。

设计目标（见 README.md）：把「上校园网 → 登学校 WebVPN → 确认能进目标文献数据库」
这一条链路做成一件事。**核心可独立运行**（CLI / 守护 / standalone 双击脚本），
与 agent / harness 的联动（integrations/）是可选附属。

基于上游 hhu-autologin v1.4.2（见 upstream/ 与 CHANGELOG.md）。
只做到「登上数据库」为止，不包含检索、下载、综述等后续科研步骤。
"""

__version__ = "0.5.1"
__upstream_version__ = "1.4.2"
__all__ = ["__version__", "__upstream_version__"]
