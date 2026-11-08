"""python -m admin：启动本机后台管理服务。"""

from __future__ import annotations

import uvicorn

from .config import AdminSettings


def main() -> None:
    settings = AdminSettings()
    uvicorn.run(
        "admin.asgi:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
