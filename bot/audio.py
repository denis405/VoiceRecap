import asyncio
from pathlib import Path


class AudioError(RuntimeError):
    pass


async def to_mp3_chunks(src: Path, out_dir: Path, chunk_sec: int) -> list[Path]:
    """Extract audio track, downmix to 16 kHz mono mp3 and split into chunk_sec pieces."""
    pattern = out_dir / "chunk_%03d.mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(chunk_sec), "-reset_timestamps", "1",
        str(pattern),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg failed ({proc.returncode}): {stderr.decode(errors='replace')[-500:]}")

    chunks = sorted(out_dir.glob("chunk_*.mp3"))
    # ffmpeg may emit a near-empty tail segment; drop it
    if len(chunks) > 1 and chunks[-1].stat().st_size < 4096:
        chunks.pop()
    if not chunks or chunks[0].stat().st_size == 0:
        raise AudioError("ffmpeg produced no audio")
    return chunks
