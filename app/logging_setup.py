"""日志初始化。"""

import logging


def setup_logging(level: str = "INFO") -> None:
    """配置根日志器；把噪音较大的第三方库压到 WARNING。"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
