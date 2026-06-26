"""ffmpeg 결정론적 조립/인코딩 래퍼 (CLAUDE.md §4.8-4.9, §3 — 결정론적 조립 우선).

컷 순서 결합, 전환(xfade), 길이 맞춤, 플랫폼 프리셋 인코딩.
오프라인 베이스라인: 정지 이미지 → Ken Burns 클립 (Seedance 키 없을 때).
ffmpeg 미설치 시 graceful 안내.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

PLATFORM_PRESETS = {
    "instagram_reels": {"w": 1080, "h": 1920, "fps": 30},
    "tiktok": {"w": 1080, "h": 1920, "fps": 30},
    "youtube_shorts": {"w": 1080, "h": 1920, "fps": 30},
    "feed": {"w": 1080, "h": 1080, "fps": 30},
}

ASPECT_WH = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: List[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed: " + proc.stderr.decode("utf-8", "ignore")[-800:]
        )


def wh_for(aspect_ratio: str) -> Tuple[int, int]:
    return ASPECT_WH.get(aspect_ratio, (1080, 1920))


def still_to_kenburns(
    still: Path,
    out: Path,
    duration: float,
    aspect_ratio: str = "9:16",
    fps: int = 30,
    motion: str = "in",
) -> Path:
    """정지 이미지 → Ken Burns(줌/팬) 클립 (오프라인 image-to-video 베이스라인)."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not installed — cannot generate video")
    w, h = wh_for(aspect_ratio)
    frames = max(1, int(round(duration * fps)))
    # 업스케일 후 zoompan (떨림 방지 위해 큰 캔버스에서 보간)
    sw, sh = w * 2, h * 2
    if motion == "out":
        z = "if(eq(on,0),1.20,max(1.001,zoom-0.0010))"
    else:  # in
        z = "min(zoom+0.0010,1.20)"
    vf = (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
        f"crop={sw}:{sh},"
        f"zoompan=z='{z}':d={frames}:s={w}x{h}:fps={fps}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
        f"format=yuv420p"
    )
    _run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(still),
        "-t", f"{duration:.3f}", "-vf", vf, "-r", str(fps),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
        str(out),
    ])
    return out


def _fontfile() -> Optional[str]:
    from app.core import fonts

    return fonts.find_font()


def burn_text(
    clip_in: Path,
    clip_out: Path,
    text: str,
    position: str = "bottom",
    aspect_ratio: str = "9:16",
) -> Path:
    """클립에 한글 on-screen 텍스트를 drawtext 로 번인 (CLAUDE.md §4.8).

    한글 깨짐/특수문자 이스케이프 회피 위해 textfile 사용.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not installed")
    font = _fontfile()
    if not text.strip() or font is None:
        shutil.copy(clip_in, clip_out)
        return clip_out
    w, h = wh_for(aspect_ratio)
    fontsize = max(40, w // 18)
    ypos = {"top": "h*0.08", "center": "(h-text_h)/2", "bottom": "h*0.78"}.get(
        position, "h*0.78"
    )
    # ffmpeg drawtext does NOT wrap — pre-wrap to the frame width (≈90%) so long on-screen
    # text doesn't run off both edges. drawtext renders embedded newlines from the textfile.
    import textwrap as _tw
    max_chars = max(10, int((w * 0.9) / (fontsize * 0.55)))
    wrapped = "\n".join(_tw.fill(line, width=max_chars) for line in text.split("\n"))
    txt_file = clip_out.parent / (clip_out.stem + "_text.txt")
    txt_file.write_text(wrapped, encoding="utf-8")
    draw = (
        f"drawtext=fontfile='{font}':textfile='{txt_file}':"
        f"fontcolor=white:fontsize={fontsize}:"
        f"x=(w-text_w)/2:y={ypos}:"
        f"box=1:boxcolor=black@0.45:boxborderw=24:"
        f"borderw=3:bordercolor=black@0.9"
    )
    _run([
        "ffmpeg", "-y", "-i", str(clip_in), "-vf", draw,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(clip_out),
    ])
    txt_file.unlink(missing_ok=True)
    return clip_out


def concat_clips(clips: List[Path], out_path: Path) -> Path:
    """동일 파라미터 클립을 순서대로 결합 (cut 전환)."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not installed — cannot assemble video")
    list_file = out_path.parent / "_concat.txt"
    list_file.write_text(
        "\n".join(f"file '{c.resolve()}'" for c in clips), encoding="utf-8"
    )
    _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
    ])
    return out_path


# storyboard transition → (xfade transition type, overlap seconds).
# cut ≈ near-instant; crossfade = dissolve; whip_pan = slide.
_XFADE = {
    "cut": ("fade", 0.08),
    "crossfade": ("fade", 0.4),
    "dissolve": ("fade", 0.4),
    "whip_pan": ("slideleft", 0.3),
    "whip": ("slideleft", 0.3),
    "wipe": ("wipeleft", 0.3),
}


def crossfade_clips(
    clips: List[Path],
    durations: List[float],
    out_path: Path,
    fade: float = 0.4,
    fps: int = 30,
    transitions: Optional[List[str]] = None,
) -> Path:
    """xfade 로 클립 결합 — 컷별 transition(cut/crossfade/whip_pan)을 반영 (CLAUDE.md §3.2/§4.8).

    transitions[i] 는 clip[i]→clip[i+1] 전환 종류. 없으면 균일 crossfade.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not installed")
    if len(clips) == 1:
        return concat_clips(clips, out_path)

    inputs: List[str] = []
    for c in clips:
        inputs += ["-i", str(c)]

    filters = []
    prev = "[0:v]"
    offset = 0.0
    for i in range(1, len(clips)):
        tr = transitions[i - 1] if transitions and i - 1 < len(transitions) else "crossfade"
        xtype, xdur = _XFADE.get(tr, ("fade", fade))
        offset += durations[i - 1] - xdur   # overlap by this junction's transition duration
        out_label = f"[v{i}]" if i < len(clips) - 1 else "[vout]"
        filters.append(
            f"{prev}[{i}:v]xfade=transition={xtype}:duration={xdur}:"
            f"offset={offset:.3f}{out_label}"
        )
        prev = out_label
    filter_complex = ";".join(filters)
    _run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-r", str(fps),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
    ])
    return out_path


# mood → (기본 주파수[화음 루트], 트레몰로 속도, 로우패스) — 결정론적 베드
_MOOD_PRESETS = {
    "upbeat": (293.66, 6.0, 3200),     # D4, 경쾌
    "energetic": (329.63, 7.0, 3600),  # E4
    "calm": (196.00, 3.0, 1600),       # G3, 잔잔
    "lo-fi": (174.61, 2.5, 1200),      # F3, 로파이
    "epic": (146.83, 4.0, 2400),       # D3, 웅장
}


def music_bed(out_path: Path, duration: float, mood: str = "") -> Path:
    """무드 기반 절차적 배경음악 베드 합성 (오프라인 베이스라인, CLAUDE.md §4.8).

    화음(루트+장3도+5도) sine 레이어 + 트레몰로 + 로우패스 + 페이드.
    상업용은 ElevenLabs/MiniMax 로 override (audio provider).
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not installed")
    root, trem, lp = _MOOD_PRESETS.get((mood or "").lower(), (220.0, 4.5, 2200))
    third = root * 1.25      # 장3도
    fifth = root * 1.5       # 완전5도
    d = max(1.0, duration)
    fade_out = max(0.1, d - 1.2)
    inputs = []
    for f in (root, third, fifth, root * 0.5):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={f:.2f}:duration={d:.2f}"]
    fc = (
        f"[0][1][2][3]amix=inputs=4:normalize=1,"
        f"tremolo=f={trem}:d=0.35,lowpass=f={lp},"
        f"volume=0.28,"
        f"afade=t=in:d=0.6,afade=t=out:st={fade_out:.2f}:d=1.2[a]"
    )
    _run([
        "ffmpeg", "-y", *inputs, "-filter_complex", fc,
        "-map", "[a]", "-c:a", "aac", "-b:a", "128k", str(out_path),
    ])
    return out_path


def has_audio_stream(path: Path) -> bool:
    """ffprobe 로 오디오 스트림 존재 확인 — 검증용."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True,
    )
    return b"audio" in proc.stdout


def mux_audio(video: Path, audio: Optional[Path], out_path: Path, volume: float = 1.0) -> Path:
    """비디오에 오디오 트랙 믹스 (audio=None 이면 그대로 복사). volume = 음악 베드 배율."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not installed")
    if audio is None or not audio.exists():
        shutil.copy(video, out_path)
        return out_path
    cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(audio)]
    if abs(volume - 1.0) > 0.01:
        cmd += ["-filter:a", f"volume={max(0.0, volume):.2f}"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-shortest", str(out_path)]
    _run(cmd)
    return out_path


def encode_preset(in_path: Path, out_path: Path, platform: str = "instagram_reels") -> Path:
    """플랫폼 프리셋으로 H.264/MP4 최종 인코딩 (9단계)."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not installed — cannot encode")
    p = PLATFORM_PRESETS.get(platform, PLATFORM_PRESETS["instagram_reels"])
    _run([
        "ffmpeg", "-y", "-i", str(in_path),
        "-vf", f"scale={p['w']}:{p['h']}:force_original_aspect_ratio=decrease,"
               f"pad={p['w']}:{p['h']}:(ow-iw)/2:(oh-ih)/2",
        "-r", str(p["fps"]),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out_path),
    ])
    return out_path


def resize_cover_video(in_path: Path, out_path: Path, w: int, h: int, fps: int = 30) -> Path:
    """영상 object-cover 리사이즈 — 비율 채우고 중앙 크롭 (멀티 플랫폼 export)."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not installed")
    has_a = has_audio_stream(in_path)
    cmd = [
        "ffmpeg", "-y", "-i", str(in_path),
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
        "-r", str(fps), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    cmd += (["-c:a", "aac"] if has_a else ["-an"])
    cmd.append(str(out_path))
    _run(cmd)
    return out_path


def upscale_video(in_path: Path, out_path: Path, factor: int = 2) -> Path:
    """Offline video upscale baseline — Lanczos scale (CLAUDE.md §9)."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not installed")
    has_a = has_audio_stream(in_path)
    cmd = [
        "ffmpeg", "-y", "-i", str(in_path),
        "-vf", f"scale=iw*{factor}:ih*{factor}:flags=lanczos",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    cmd += (["-c:a", "copy"] if has_a else ["-an"])
    cmd.append(str(out_path))
    _run(cmd)
    return out_path


def probe_duration(path: Path) -> float:
    """ffprobe 로 영상 길이(초) 확인 — 검증용."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
    )
    try:
        return float(proc.stdout.decode().strip())
    except Exception:
        return 0.0
