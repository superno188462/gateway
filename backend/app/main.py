"""ASGI 应用入口。"""

from app.bootstrap import create_app

app = create_app()
