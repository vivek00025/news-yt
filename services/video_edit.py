"""ffmpeg post-production: fit the Manus video to the client's frame size,
overlay the footer image along the bottom, then append the outro."""
import json
import subprocess
from pathlib import Path

import config

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
FPS = 30
FOOTER_MAX_HEIGHT_RATIO = 0.22
OUTRO_IMAGE_SECONDS = 3


class VideoError(RuntimeError):
    pass


def _run(cmd, what, timeout=1800):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"{what} timed out after {timeout}s.") from exc
    if proc.returncode != 0:
        raise VideoError(f"{what} failed: {proc.stderr.strip()[-600:]}")
    return proc.stdout


def probe(path) -> dict:
    out = _run([config.FFPROBE_BIN, "-v", "error", "-print_format", "json", "-show_streams",
                "-show_format", str(path)], "ffprobe")
    info = json.loads(out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise VideoError(f"{Path(path).name} has no video stream.")
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    duration = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    return {"width": int(v["width"]), "height": int(v["height"]), "duration": duration, "has_audio": has_audio}


def check_ffmpeg():
    try:
        _run([config.FFMPEG_BIN, "-version"], "ffmpeg")
    except FileNotFoundError:
        raise VideoError("ffmpeg is not installed or FFMPEG_BIN is wrong.")


def _normalize(src: Path, dst: Path, W: int, H: int, footer: Path | None = None, image_seconds=0):
    """Re-encode `src` to W x H / 30fps / AAC stereo, optionally overlaying `footer` at the bottom.
    Guarantees an audio track so segments can be concatenated."""
    is_image = src.suffix.lower() in IMAGE_EXT
    cmd = [config.FFMPEG_BIN, "-y"]
    if is_image:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", str(image_seconds or OUTRO_IMAGE_SECONDS)]
        duration, has_audio = image_seconds or OUTRO_IMAGE_SECONDS, False
    else:
        info = probe(src)
        duration, has_audio = info["duration"], info["has_audio"]
    cmd += ["-i", str(src)]
    idx = 1

    fit = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
           f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={FPS},format=yuv420p")
    graph = f"[0:v]{fit}[base]"
    vmap = "[base]"

    if footer:
        fi = probe(footer)
        scale = min(W / fi["width"], (H * FOOTER_MAX_HEIGHT_RATIO) / fi["height"])
        fw, fh = max(2, int(fi["width"] * scale) // 2 * 2), max(2, int(fi["height"] * scale) // 2 * 2)
        cmd += ["-i", str(footer)]
        graph += (f";[{idx}:v]scale={fw}:{fh},format=rgba[ft];"
                  f"[base][ft]overlay={(W - fw) // 2}:{H - fh}:format=auto,format=yuv420p[vout]")
        vmap = "[vout]"
        idx += 1

    if has_audio:
        amap = "0:a:0"
        afilter = ["-af", "aresample=44100"]
    else:
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        amap, afilter = f"{idx}:a:0", []

    cmd += ["-filter_complex", graph, "-map", vmap, "-map", amap, *afilter,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", str(dst)]
    _run(cmd, f"Encoding {src.name}")
    return dst


def compose(main: Path, out: Path, aspect: str, footer: Path | None, outro: Path | None, workdir: Path) -> Path:
    """main video (+ footer overlay) followed by outro -> out (MP4)."""
    check_ffmpeg()
    W, H = config.FRAME_SIZES[aspect]
    workdir.mkdir(parents=True, exist_ok=True)

    seg1 = _normalize(main, workdir / "seg_main.mp4", W, H, footer=footer)
    if not outro:
        _run([config.FFMPEG_BIN, "-y", "-i", str(seg1), "-c", "copy", "-movflags", "+faststart", str(out)],
             "Finalizing")
        return out

    seg2 = _normalize(outro, workdir / "seg_outro.mp4", W, H)
    _run([config.FFMPEG_BIN, "-y", "-i", str(seg1), "-i", str(seg2),
          "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
          "-map", "[v]", "-map", "[a]",
          "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], "Joining outro")
    return out


# ---------------------------------------------------------------- thumbnails
THUMB_W, THUMB_H = 1280, 720
THUMB_MAX_BYTES = 1_900_000   # YouTube's limit is 2 MB
_COVER = f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=increase,crop={THUMB_W}:{THUMB_H}"


def _jpeg_under_limit(cmd_prefix, cmd_suffix, dst: Path):
    for q in (2, 5, 8, 12, 18):
        _run(cmd_prefix + cmd_suffix + ["-q:v", str(q), str(dst)], "Thumbnail")
        if dst.stat().st_size <= THUMB_MAX_BYTES:
            return dst
    raise VideoError("Thumbnail could not be compressed under 2 MB.")


def make_thumbnail(src: Path, dst: Path) -> Path:
    """Any image from Manus -> 1280x720 JPEG under 2 MB."""
    return _jpeg_under_limit([config.FFMPEG_BIN, "-y", "-i", str(src), "-frames:v", "1", "-vf", _COVER],
                             [], dst)


def frame_thumbnail(video: Path, dst: Path) -> Path:
    """Fallback: a frame from the finished video."""
    at = max(0.5, probe(video)["duration"] * 0.3)
    return _jpeg_under_limit([config.FFMPEG_BIN, "-y", "-ss", f"{at:.2f}", "-i", str(video),
                              "-frames:v", "1", "-vf", _COVER], [], dst)
