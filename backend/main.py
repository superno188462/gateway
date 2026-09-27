"""本地开发启动入口。"""

import uvicorn


def main() -> None:
    """在本机启动 FastAPI 开发服务。"""
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
