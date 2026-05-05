from fastapi import FastAPI
from fastapi.responses import HTMLResponse


def create_app() -> FastAPI:
    app = FastAPI(title="yt-summary")

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return "<h1>yt-summary</h1>"

    return app


app = create_app()
