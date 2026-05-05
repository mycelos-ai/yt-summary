import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import StreamingResponse
from markupsafe import escape

from app.main import get_db
from app.repos import chat as chat_repo
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services.chat import stream_reply

router = APIRouter()


@router.post("/v/{video_id}/chat")
async def post_chat(
    video_id: str,
    content: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.transcript is None:
        raise HTTPException(404, "Video or transcript not found")
    settings = await settings_repo.get_all(db)
    model = settings.get("llm_model")
    if not model:
        raise HTTPException(400, "LLM not configured")
    api_key = settings.get("llm_api_key") or ""

    history = await chat_repo.history(db, video_id)
    await chat_repo.append(db, video_id, "user", content)

    async def streamer():
        yield (
            f'<div class="chat-msg chat-msg-user">'
            f'<strong>user</strong>'
            f'<div class="chat-content">{escape(content)}</div></div>'
        )
        yield (
            '<div class="chat-msg chat-msg-assistant">'
            '<strong>assistant</strong><div class="chat-content">'
        )
        collected: list[str] = []
        try:
            async for token in stream_reply(
                transcript=video.transcript or "",
                history=history,
                user_message=content,
                model=model,
                api_key=api_key,
                base_url=settings.get("llm_base_url"),
            ):
                collected.append(token)
                yield str(escape(token))
        except Exception as e:
            err = f"\n\n[error: {e}]"
            collected.append(err)
            yield str(escape(err))
        yield "</div></div>"
        await chat_repo.append(db, video_id, "assistant", "".join(collected))

    return StreamingResponse(streamer(), media_type="text/html; charset=utf-8")
