import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from markupsafe import escape

from app.main import get_db
from app.repos import chat as chat_repo
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services.chat import stream_reply

router = APIRouter()


def _msg_html(role: str, content: str, *, is_error: bool = False) -> str:
    cls = f"chat-msg chat-msg-{role}"
    if is_error:
        cls += " chat-msg-error"
    label = "error" if is_error else role
    return (
        f'<div class="{cls}">'
        f"<strong>{label}</strong>"
        f'<div class="chat-content">{escape(content)}</div></div>'
    )


@router.post("/v/{video_id}/chat", response_class=HTMLResponse)
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

    collected: list[str] = []
    error: str | None = None
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
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    answer = "".join(collected)
    await chat_repo.append(
        db, video_id, "assistant", answer if answer else f"[error: {error}]"
    )

    parts = [_msg_html("user", content)]
    if answer:
        parts.append(_msg_html("assistant", answer))
    if error:
        parts.append(_msg_html("assistant", error, is_error=True))
    elif not answer:
        parts.append(
            _msg_html("assistant", "(empty response from model)", is_error=True)
        )
    return HTMLResponse("".join(parts))
