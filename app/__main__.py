"""Run the app: python -m app"""

import uvicorn

from .config import settings

uvicorn.run("app.main:app", host=settings.host, port=settings.port)
