from unittest.mock import AsyncMock, MagicMock, patch


def _stream_chunks(*texts: str):
    async def gen():
        for t in texts:
            choice = MagicMock()
            choice.delta.content = t
            chunk = MagicMock()
            chunk.choices = [choice]
            yield chunk
    return gen()


async def test_stream_reply_yields_token_strings():
    from app.services.chat import stream_reply

    with patch(
        "app.services.chat.litellm.acompletion",
        AsyncMock(return_value=_stream_chunks("Hello", " ", "world")),
    ):
        out: list[str] = []
        async for token in stream_reply(
            transcript="t",
            history=[],
            user_message="hi",
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
        ):
            out.append(token)
        assert "".join(out) == "Hello world"
