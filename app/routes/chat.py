import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from markupsafe import escape

from app.main import get_current_user_id, get_db
from app.repos import chat as chat_repo
from app.repos import llm_models as llm_models_repo
from app.repos import videos as videos_repo
from app.services.chat import stream_reply
from app.services.markdown import render_markdown

router = APIRouter()


def _msg_html(role: str, content: str, *, is_error: bool = False) -> str:
    """Render one chat message.

    User messages are HTML-escaped — they're untrusted input and
    should never be parsed as markdown (XSS hardening, no surprises).
    Assistant messages run through render_markdown so tables, bold,
    bullets, code blocks, and inline timestamp links all render
    properly. Errors stay escaped (they may include error strings
    that look like markup).
    """
    if role == "user":
        body = str(escape(content))
        return f'<div class="chat-bubble-user">{body}</div>'
    if is_error:
        body = str(escape(content))
        return (
            f'<div class="chat-answer chat-msg-error">'
            f'<div class="chat-answer-content">{body}</div></div>'
        )
    body = render_markdown(content)
    return (
        f'<div class="chat-answer">'
        f'<div class="chat-answer-content">{body}</div></div>'
    )


@router.post("/v/{video_id}/chat", response_class=HTMLResponse)
async def post_chat(
    video_id: str,
    content: str = Form(...),
    llm_model_id: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.transcript is None:
        raise HTTPException(404, "Video or transcript not found")
    if video.user_id != current_user_id:
        raise HTTPException(404, "Video or transcript not found")

    chosen_id: int | None = None
    if llm_model_id.strip():
        try:
            chosen_id = int(llm_model_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid llm_model_id: {e}") from None
    model_row = (
        await llm_models_repo.get(db, chosen_id)
        if chosen_id is not None
        else await llm_models_repo.get_default(db)
    )
    if model_row is None:
        raise HTTPException(400, "LLM not configured")
    model = model_row.model
    api_key = model_row.api_key or ""
    base_url = model_row.base_url or None

    history = await chat_repo.history(db, video_id)
    await chat_repo.append(
        db, video_id, "user", content, user_id=current_user_id
    )

    collected: list[str] = []
    error: str | None = None
    try:
        async for token in stream_reply(
            transcript=video.transcript or "",
            history=history,
            user_message=content,
            model=model,
            api_key=api_key,
            base_url=base_url,
        ):
            collected.append(token)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    answer = "".join(collected)
    await chat_repo.append(
        db, video_id, "assistant",
        answer if answer else f"[error: {error}]",
        user_id=current_user_id,
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
