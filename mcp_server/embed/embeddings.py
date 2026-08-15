"""文本嵌入服务（Strategy 模式）。

两种策略，按配置切换，对外统一接口 `embed(texts) -> List[List[float]]`：
- 本地 fastembed（默认，轻量、无需联网/API Key，基于 ONNX Runtime，不依赖 torch）
- OpenAI 兼容（若配置了 OPENAI_API_KEY 且 backend=openai；base_url 可指向硅基流动等第三方）

第三方库均懒加载：缺依赖时本模块仍可 import，仅实例化时抛错，由业务层捕获降级。
"""

from mcp_server.db.base import DbUnavailable
from mcp_server.db.config import get_config


class _LocalFastEmbed:
    def __init__(self, model: str) -> None:
        try:
            import fastembed
        except ImportError as e:
            raise DbUnavailable("fastembed 未安装：pip install fastembed") from e
        self._model = fastembed.TextEmbedding(model)

    def embed(self, texts: list) -> list:
        return [list(map(float, v)) for v in self._model.embed(texts)]


class _OpenAIEmbed:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1", dim: int = 384) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise DbUnavailable("openai 未安装：pip install openai") from e
        if not api_key:
            raise DbUnavailable("未配置 OPENAI_API_KEY")
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._dim = dim
        # 截断预算：bge-m3 等模型上下文约 8192 token，代码 token 较密，
        # 8000 字符足以覆盖绝大多数代码块且不会越界触发 400。
        self._max_chars = 8000

    def embed(self, texts: list) -> list:
        # 部分 OpenAI 兼容服务（如硅基流动）对单请求输入条数/总 token 有限制，
        # 且拒绝空串（报 400 参数无效）。这里：空串占位 + 超长截断 + 分批 + 失败兜底，
        # 全程保持与输入 1:1 对齐。
        batch_size = 32
        cleaned = [
            (t if isinstance(t, str) and t.strip() else " ")[: self._max_chars]
            for t in texts
        ]
        out = []
        for i in range(0, len(cleaned), batch_size):
            batch = cleaned[i : i + batch_size]
            try:
                resp = self._client.embeddings.create(model=self._model, input=batch)
                out.extend([list(map(float, d.embedding)) for d in resp.data])
            except Exception:
                # 单批失败兜底：逐条二次截断后重试，仍失败则零向量占位，保证索引不中断。
                for t in batch:
                    try:
                        r = self._client.embeddings.create(model=self._model, input=[t[:2000]])
                        out.append(list(map(float, r.data[0].embedding)))
                    except Exception:
                        out.append([0.0] * self._dim)
        return out


class EmbeddingService:
    """嵌入策略门面（Facade）。"""

    def __init__(self) -> None:
        cfg = get_config()
        if cfg["embed_backend"] == "openai" and cfg["openai_api_key"]:
            self._impl = _OpenAIEmbed(
                cfg["openai_api_key"], cfg["embed_model"], cfg["openai_base_url"], cfg["embed_dim"]
            )
        else:
            self._impl = _LocalFastEmbed(cfg["embed_model"])
        self.dim = cfg["embed_dim"]

    def embed(self, texts: list) -> list:
        if isinstance(texts, str):
            texts = [texts]
        return self._impl.embed(texts)
