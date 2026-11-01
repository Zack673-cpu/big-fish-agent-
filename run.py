"""本地启动脚本：python run.py"""

import uvicorn

from app.config import load_settings

if __name__ == "__main__":
    config = load_settings()
    uvicorn.run(
        "app.main:app",
        host=config.onebot_host,
        port=config.onebot_port,
        log_level=config.log_level.lower(),
    )
