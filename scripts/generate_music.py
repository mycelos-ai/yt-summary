#!/usr/bin/env python3
"""Generate music via the sunoapi.org wrapper for Suno.

Usage examples:

    # Fully argumented run (silent, scriptable)
    python scripts/generate_music.py \\
        --style "ambient electronic, calm, atmospheric" \\
        --title "yt-summary intro" \\
        --output promos/public/music/intro.mp3

    # No args → guided wizard prompts for everything
    python scripts/generate_music.py

API key resolution order:

    1. SUNO_API_KEY environment variable
    2. ~/.config/suno/api_key (if file exists)
    3. Interactive prompt (and offer to save the key for next time)

Suno usually returns two variant tracks per job. Both are saved with
_1 / _2 suffixes — pick whichever you like better.

Independent of yt-summary's app code: lives under scripts/ as a
self-contained CLI utility, only depends on httpx (already in the
project's dev deps).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import httpx

API_BASE = "https://api.sunoapi.org"
KEY_FILE = Path.home() / ".config" / "suno" / "api_key"
POLL_INTERVAL_S = 6
POLL_MAX_ATTEMPTS = 60  # ~6 minutes total


# ── Key handling ─────────────────────────────────────────────────────


def _read_key_file() -> str | None:
    if not KEY_FILE.exists():
        return None
    try:
        value = KEY_FILE.read_text().strip()
        return value or None
    except OSError:
        return None


def _save_key_file(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key + "\n")
    KEY_FILE.chmod(0o600)


def resolve_api_key() -> str:
    """Pick up the key from env / file / prompt. Errors out if no
    interactive TTY and the key isn't already configured."""
    env_value = os.environ.get("SUNO_API_KEY", "").strip()
    if env_value:
        return env_value

    file_value = _read_key_file()
    if file_value:
        return file_value

    if not sys.stdin.isatty():
        sys.exit(
            "✗ No SUNO_API_KEY in env and no ~/.config/suno/api_key, "
            "and stdin is not a TTY — cannot prompt for the key.\n"
            "  Set SUNO_API_KEY=... or write the key to "
            f"{KEY_FILE} (chmod 600)."
        )

    print(
        "No Suno API key found.\n"
        "Get one at https://sunoapi.org/ — it starts with `sk-`.\n"
    )
    key = input("Paste your Suno API key: ").strip()
    if not key:
        sys.exit("✗ No key provided.")

    save = input(
        f"Save the key to {KEY_FILE} for next time? [Y/n] "
    ).strip().lower()
    if save in ("", "y", "yes"):
        _save_key_file(key)
        print(f"  ✓ saved to {KEY_FILE} (mode 600).")

    return key


# ── HTTP helpers ─────────────────────────────────────────────────────


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
        # Don't let a corporate HTTP_PROXY env hijack a direct call to
        # the explicitly-configured Suno endpoint.
        trust_env=False,
    )


def get_credits(client: httpx.Client) -> int | None:
    resp = client.get("/api/v1/generate/credit")
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200:
        print(f"  ! credits check failed: {body}")
        return None
    return body.get("data")


def submit_job(
    client: httpx.Client,
    *,
    style: str,
    title: str,
    instrumental: bool,
    model: str,
    style_weight: float,
    weirdness: float,
) -> str:
    """Kick off a generation. Returns the taskId."""
    payload = {
        "customMode": True,
        "instrumental": instrumental,
        "model": model,
        # Required field even though we poll for results — sunoapi.org's
        # job model assumes someone wants webhook callbacks. We don't,
        # so we point at example.com and rely on polling.
        "callBackUrl": "https://example.com/callback",
        "style": style,
        "title": title,
        "styleWeight": style_weight,
        "weirdnessConstraint": weirdness,
    }
    resp = client.post("/api/v1/generate", json=payload)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200:
        sys.exit(f"✗ Submit failed: {body.get('msg')}\n  body: {body}")
    task_id = body.get("data", {}).get("taskId")
    if not task_id:
        sys.exit(f"✗ Submit returned no taskId: {body}")
    return task_id


def poll_job(client: httpx.Client, task_id: str) -> list[dict]:
    """Poll until status=SUCCESS (or timeout). Returns the track list."""
    started = time.monotonic()
    last_status = ""
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        elapsed = int(time.monotonic() - started)
        try:
            resp = client.get(
                "/api/v1/generate/record-info",
                params={"taskId": task_id},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            print(f"\n  ! poll error (attempt {attempt}): {e}")
            time.sleep(POLL_INTERVAL_S)
            continue
        body = resp.json()
        status = body.get("data", {}).get("status", "?")
        if status != last_status:
            # New state → newline so the previous \r-line stays visible
            if last_status:
                print()
            last_status = status

        # In-line progress so the terminal doesn't scroll for every poll
        sys.stdout.write(
            f"\r  {elapsed:>3}s · attempt {attempt:>2}/{POLL_MAX_ATTEMPTS}"
            f" · status={status}    "
        )
        sys.stdout.flush()

        if status == "SUCCESS":
            print()
            tracks = (
                body.get("data", {})
                .get("response", {})
                .get("sunoData", [])
            )
            return tracks
        if status in ("FAILED", "ERROR", "SENSITIVE_WORD_ERROR"):
            print()
            sys.exit(f"✗ Generation failed: status={status}, body={body}")

        time.sleep(POLL_INTERVAL_S)
    print()
    sys.exit(
        "✗ Timed out after "
        f"{POLL_INTERVAL_S * POLL_MAX_ATTEMPTS}s. taskId={task_id}"
    )


def download_tracks(
    tracks: list[dict],
    output_template: Path,
) -> list[Path]:
    """Save each track as <stem>_<n><ext>. Returns the saved paths."""
    saved: list[Path] = []
    if not tracks:
        sys.exit("✗ Job succeeded but returned no tracks.")
    output_template.parent.mkdir(parents=True, exist_ok=True)

    for idx, track in enumerate(tracks, start=1):
        url = track.get("audioUrl") or track.get("streamAudioUrl")
        if not url:
            print(f"  ! track {idx} has no audioUrl, skipping: {track}")
            continue
        stem = output_template.stem
        suffix = output_template.suffix or ".mp3"
        target = output_template.with_name(f"{stem}_{idx}{suffix}")
        with (
            httpx.Client(timeout=120.0, trust_env=False) as dl,
            dl.stream("GET", url) as resp,
            open(target, "wb") as f,
        ):
            resp.raise_for_status()
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                f.write(chunk)
        title = track.get("title") or "(untitled)"
        duration = track.get("duration")
        size_kb = target.stat().st_size // 1024
        print(
            f"  ✓ {target}  ({size_kb} KB"
            f"{f', {duration:.0f}s' if duration else ''}, '{title}')"
        )
        saved.append(target)
    return saved


# ── Wizard / CLI parsing ─────────────────────────────────────────────


def _prompt(label: str, default: str = "", *, required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    prompt = f"{label}{suffix}: "
    while True:
        value = input(prompt).strip()
        if not value and default:
            return default
        if value:
            return value
        if not required:
            return ""
        print("  required.")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="generate_music.py",
        description=(
            "Generate background music via Suno (sunoapi.org). "
            "Run without args for an interactive wizard."
        ),
    )
    p.add_argument("--style", help="Style description prompt for Suno")
    p.add_argument("--title", help="Track title")
    p.add_argument(
        "--output",
        type=Path,
        help="Output path template (e.g. promos/public/music/intro.mp3 "
             "→ saves intro_1.mp3 and intro_2.mp3 next to it)",
    )
    p.add_argument(
        "--instrumental",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Generate instrumental (no vocals). Default: yes.",
    )
    p.add_argument(
        "--model", default="V5",
        help="Suno model version (default V5).",
    )
    p.add_argument(
        "--style-weight", type=float, default=0.8,
        help="0.0-1.0, how strongly to enforce the style prompt.",
    )
    p.add_argument(
        "--weirdness", type=float, default=0.2,
        help="0.0-1.0, weirdnessConstraint param. Higher = more "
             "experimental.",
    )
    return p.parse_args(list(argv))


def fill_in_missing(args: argparse.Namespace) -> argparse.Namespace:
    """Prompt for any required fields the user didn't pass."""
    needs_wizard = not (args.style and args.title and args.output)
    if needs_wizard:
        print("\n── Suno music generation wizard ──\n")
    if not args.style:
        args.style = _prompt(
            "Style (e.g. 'ambient electronic, calm, atmospheric')",
            required=True,
        )
    if not args.title:
        args.title = _prompt(
            "Track title", default="Untitled", required=True,
        )
    if not args.output:
        default_out = f"./{_slug(args.title)}.mp3"
        raw = _prompt("Output path template", default=default_out)
        args.output = Path(raw)
    return args


def _slug(value: str) -> str:
    """Cheap slugify — lowercased, ascii-ish, dashes for whitespace."""
    out = []
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "track"


# ── Entrypoint ───────────────────────────────────────────────────────


def main() -> int:
    args = parse_args(sys.argv[1:])
    args = fill_in_missing(args)
    api_key = resolve_api_key()

    print(
        f"\n  style:        {args.style}\n"
        f"  title:        {args.title}\n"
        f"  output:       {args.output} (will save _1 + _2)\n"
        f"  model:        {args.model}\n"
        f"  instrumental: {args.instrumental}\n"
        f"  styleWeight:  {args.style_weight}\n"
        f"  weirdness:    {args.weirdness}\n"
    )

    with _client(api_key) as client:
        credits = get_credits(client)
        if credits is not None:
            print(f"  credits left: {credits}")
        print("\n→ submitting job …")
        task_id = submit_job(
            client,
            style=args.style,
            title=args.title,
            instrumental=args.instrumental,
            model=args.model,
            style_weight=args.style_weight,
            weirdness=args.weirdness,
        )
        print(f"  taskId: {task_id}")

        print("→ polling until ready (this usually takes 1-3 minutes) …")
        tracks = poll_job(client, task_id)
        print(f"  ✓ generated {len(tracks)} track(s)")

        print("→ downloading …")
        saved = download_tracks(tracks, args.output)

    if saved:
        print(f"\n✓ Done. {len(saved)} file(s) saved.")
        return 0
    print("\n✗ Nothing saved.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n^C — aborted by user.")
        sys.exit(130)
    except httpx.HTTPStatusError as e:
        try:
            body = e.response.json()
        except (ValueError, json.JSONDecodeError):
            body = e.response.text
        sys.exit(f"✗ HTTP {e.response.status_code}: {body}")
    except httpx.HTTPError as e:
        sys.exit(f"✗ Network error: {type(e).__name__}: {e}")
