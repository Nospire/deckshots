"""ffmpeg helpers: Steam clip remux, transcoding of non-mp4 recordings, size-based splitting."""
import logging
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

log = logging.getLogger("deckshots.media")

MP4_LIKE = (".mp4", ".m4v", ".mov")


def needs_transcode(filename: str) -> bool:
    return Path(filename).suffix.lower() not in MP4_LIKE


def run(cmd):
    log.info("run: %s", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def remux_clip(tar_path: Path, workdir: Path) -> Path:
    """Steam Game Recording clip (DASH pieces: session.mpd + init/chunk m4s) -> single mp4, stream copy."""
    src = workdir / "src"
    src.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tf:
        tf.extractall(src, filter="data")
    out = workdir / "clip.mp4"
    mpd = next(src.rglob("session.mpd"), None)
    if mpd:
        r = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mpd), "-c", "copy", "-movflags", "+faststart", str(out)])
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
        log.warning("mpd remux failed, falling back to concat: %s", r.stderr[-400:])
    root = mpd.parent if mpd else src
    inits = sorted(root.rglob("init-stream*.m4s"))
    if not inits:
        raise RuntimeError("no session.mpd / init-stream*.m4s inside clip archive")
    parts = []
    for init in inits:
        idx = re.search(r"init-stream(\d+)", init.name).group(1)
        chunks = sorted(init.parent.glob(f"chunk-stream{idx}-*.m4s"))
        merged = workdir / f"stream{idx}.mp4"
        with open(merged, "wb") as w:
            for piece in [init, *chunks]:
                with open(piece, "rb") as rd:
                    shutil.copyfileobj(rd, w)
        parts.append(merged)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in parts:
        cmd += ["-i", str(p)]
    cmd += ["-c", "copy", "-movflags", "+faststart", str(out)]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg concat mux failed: {r.stderr[-400:]}")
    return out


def clip_thumbnail(workdir: Path) -> Path | None:
    return next((workdir / "src").rglob("thumbnail.jpg"), None)


def normalize_video(src: Path, workdir: Path, preset: str = "veryfast", crf: int = 23) -> Path:
    """Plain recording (Spectacle/OBS): mp4/mov go as-is, webm/mkv are transcoded to H.264 + AAC mp4."""
    if not needs_transcode(src.name):
        return src
    out = workdir / (src.stem + ".mp4")
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {r.stderr[-400:]}")
    return out


def split_if_needed(video: Path, workdir: Path, max_part: int) -> list[Path]:
    size = video.stat().st_size
    if size <= max_part:
        return [video]
    dur = probe_duration(video)
    if dur <= 0:
        raise RuntimeError("cannot probe duration for splitting")
    n = int(size // max_part) + 1
    seg = max(5, int(dur / n * 0.95))
    pattern = workdir / "part%02d.mp4"
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-c", "copy", "-map", "0",
             "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1",
             "-movflags", "+faststart", str(pattern)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg segment failed: {r.stderr[-400:]}")
    parts = sorted(workdir.glob("part*.mp4"))
    if not parts:
        raise RuntimeError("segmenting produced no parts")
    return parts
