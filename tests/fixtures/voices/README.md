# Voice fixtures

Small Piper voice used by `tests/test_services_tts_render.py` to drive the
real Piper -> ffmpeg pipeline end-to-end.

## Provenance

- Model: `en_US-amy-low` (~60 MB, low-quality English US female voice)
- Source: https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/amy/low
- Direct URLs:
  - https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx
  - https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx.json

## Refresh

```bash
cd tests/fixtures/voices
curl -L -O https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx
curl -L -O https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx.json
shasum -a 256 en_US-amy-low.onnx en_US-amy-low.onnx.json
```

## SHA256 of current binaries

```
a5a91abb7de0f104358a25aded480ddacf1ff0762886325886ec406a2e86aab3  en_US-amy-low.onnx
2250a9a605b8dc35a116717fadc5056695dd809e34a15d02f72a0f52d53d3ebb  en_US-amy-low.onnx.json
```

If a future `curl` fetches different hashes, upstream has rotated the
artifact — update this file and re-run the tts_render tests.
