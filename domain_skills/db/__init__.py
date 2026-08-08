"""数据访问层（Repository 模式）。

每个存储后端一个模块，对外暴露领域方法（upsert_chunk / vector_search …），
隐藏 SQL / Cypher / HTTP 细节；业务层（skills）只依赖这些方法，不直接碰连接。

所有第三方客户端均**懒加载**（在 connect() 内 import）：
- 未装依赖时模块仍可 import，仅在使用时抛 DbUnavailable，业务层据此优雅降级；
- 服务启动不依赖任何数据库在线。
"""

from .base import DbUnavailable
from .config import get_config, load_config
