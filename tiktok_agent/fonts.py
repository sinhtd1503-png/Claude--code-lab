"""Fonts for the three text parts of a video — the cover text (first seconds), the subtitles and the closing line
(ending) — and the preview image the owner approves on Telegram.

The font files live in assets/fonts (Google Fonts, OFL, all with full Vietnamese support). Drawtext uses the file
directly; libass (subtitles) finds the family through `fontsdir`. The preview is rendered with the very same
filters as the final video, so what is approved is what is published.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
ROLES = ("cover", "subtitle", "ending")
ROLE_LABEL = {"cover": "ĐẦU – ảnh bìa", "subtitle": "PHỤ ĐỀ", "ending": "KẾT – câu kết"}

# key → label (Vietnamese), file in FONT_DIR (None = system font by family), family (for libass), bold flag,
# size factor (script fonts look smaller at the same point size)
CATALOG: dict[str, dict] = {
    "bvp_xb": {"label": "Be Vietnam Pro đậm – hiện đại, nổi bật", "file": "BeVietnamPro-ExtraBold.ttf",
               "family": "Be Vietnam Pro ExtraBold", "bold": False, "size": 1.0},
    "bvp": {"label": "Be Vietnam Pro – hiện đại, dễ đọc", "file": "BeVietnamPro-Bold.ttf",
            "family": "Be Vietnam Pro", "bold": True, "size": 1.0},
    "mali": {"label": "Mali – tròn, thân thiện", "file": "Mali-Bold.ttf", "family": "Mali", "bold": True, "size": 1.05},
    "itim": {"label": "Itim – hồn nhiên, vui", "file": "Itim-Regular.ttf", "family": "Itim", "bold": False, "size": 1.1},
    "pacifico": {"label": "Pacifico – viết tay mềm mại", "file": "Pacifico-Regular.ttf", "family": "Pacifico",
                 "bold": False, "size": 0.95},
    "lobster": {"label": "Lobster – cổ điển, hoài niệm", "file": "Lobster-Regular.ttf", "family": "Lobster",
                "bold": False, "size": 1.1},
    "prata": {"label": "Prata – có chân, trang nhã", "file": "Prata-Regular.ttf", "family": "Prata",
              "bold": False, "size": 1.0},
    "taviraj": {"label": "Taviraj – có chân, ấm áp", "file": "Taviraj-SemiBold.ttf", "family": "Taviraj",
                "bold": False, "size": 1.05},
    "dejavu": {"label": "DejaVu Sans – kiểu cũ", "file": None, "family": "DejaVu Sans", "bold": True, "size": 1.0},
}
DEFAULTS = {"cover": "bvp_xb", "subtitle": "bvp", "ending": "prata"}
SAMPLE = {"cover": "Chiếc diều của ông", "subtitle": "Bà ơi, ngày xưa bà cũng thả diều ạ?",
          "ending": "Có những mùa hè chỉ còn trong ký ức — và trong tim"}


def available() -> list[str]:
    """Catalog keys whose font file is present (all of them in the Docker image)."""
    return [k for k, v in CATALOG.items() if not v["file"] or (FONT_DIR / v["file"]).is_file()]


def choice(vid: dict, role: str) -> dict:
    key = ((vid or {}).get("fonts") or {}).get(role) or DEFAULTS[role]
    if key not in CATALOG or key not in available():
        key = DEFAULTS[role] if DEFAULTS[role] in available() else "dejavu"
    return {"key": key, **CATALOG[key]}


def drawtext_font(vid: dict, role: str) -> str:
    """drawtext option selecting the font of `role` (an absolute file path: no fontconfig pattern escaping)."""
    c = choice(vid, role)
    if c["file"]:
        return f"fontfile='{FONT_DIR / c['file']}'"
    return f"font='{c['family']}'"


def size(vid: dict, role: str, base: float) -> int:
    return int(round(base * choice(vid, role)["size"]))


# Subtitle looks (Lời răn dạy can pick one, or the owner describes one): colour (RGB hex), size factor, translucent
# box behind the text, position (bottom / lower third / middle of the frame)
SUB_PRESETS = [
    {"label": "Trắng viền đen, phía dưới", "color": "FFFFFF", "size": 1.0, "box": False, "position": "bottom"},
    {"label": "Chữ lớn, 1/3 dưới", "color": "FFFFFF", "size": 1.3, "box": False, "position": "lower"},
    {"label": "Nền mờ, 1/3 dưới", "color": "FFFFFF", "size": 1.15, "box": True, "position": "lower"},
    {"label": "Vàng ấm, 1/3 dưới", "color": "FFE8A3", "size": 1.25, "box": False, "position": "lower"},
    {"label": "Chữ lớn giữa màn hình", "color": "FFFFFF", "size": 1.4, "box": False, "position": "middle"},
]
# libass lays SRT subtitles out on a 288-unit-high canvas: MarginV 70 ≈ a quarter of the height above the bottom
POSITIONS = {"bottom": (2, 70), "lower": (2, 100), "middle": (10, 0)}  # force_style uses SSA numbering: 10 = centre


def sub_label(sub: dict | None) -> str:
    sub = sub or {}
    hit = next((p["label"] for p in SUB_PRESETS if all(sub.get(k) == p[k] for k in ("color", "size", "box", "position"))),
               None)
    return hit or (f"màu #{sub.get('color', 'FFFFFF')}, cỡ ×{float(sub.get('size', 1)):.2f}"
                   + (", nền mờ" if sub.get("box") else "") + f", vị trí {sub.get('position', 'bottom')}")


def _ass_colour(rgb: str) -> str:
    h = (str(rgb or "FFFFFF").lstrip("#").upper() + "FFFFFF")[:6]
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}"


def subtitle_style(vid: dict) -> str:
    c = choice(vid, "subtitle")
    sub = (vid or {}).get("sub_style") or {}
    align, margin = POSITIONS.get(sub.get("position") or "bottom", POSITIONS["bottom"])
    fs = size(vid, "subtitle", 13 * max(0.6, min(2.0, float(sub.get("size") or 1.0))))
    border = "BorderStyle=3,Outline=1,BackColour=&H80000000" if sub.get("box") else "BorderStyle=1,Outline=2"
    return (f"FontName={c['family']},FontSize={fs},PrimaryColour={_ass_colour(sub.get('color'))},"
            f"OutlineColour=&H80000000,{border},Shadow=0,Bold={1 if c['bold'] else 0},Alignment={align},"
            f"MarginV={margin}")


def subtitles_filter(vid: dict, srt: str = "subs.srt") -> str:
    return f"subtitles={srt}:fontsdir='{FONT_DIR}':force_style='{subtitle_style(vid)}'"


def next_key(vid: dict, role: str) -> str:
    keys = available()
    cur = choice(vid, role)["key"]
    return keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else keys[0]


def preview(vid: dict, texts: dict, bg: Path | None, workdir: Path, out: Path) -> Path:
    """One image with three panels — cover text, a subtitle line, the closing line — drawn with the production
    filters on a frame-like background (a character photo if there is one)."""
    from .video import ending_drawtext, thumb_filter  # same filters as the real video
    workdir.mkdir(parents=True, exist_ok=True)
    W, H = 1080, 1920
    (workdir / "subs.srt").write_text(f"1\n00:00:00,000 --> 00:00:09,000\n{texts.get('subtitle') or SAMPLE['subtitle']}\n",
                                      encoding="utf-8")
    pv = {**vid, "thumbnail_text": True, "thumbnail_sec": 99}
    src = (["-loop", "1", "-i", str(bg)] if bg and Path(bg).is_file()
           else ["-f", "lavfi", "-i", f"gradients=s={W}x{H}:c0=0x8a6a4a:c1=0x3b2a20:x0=0:y0=0:x1={W}:y1={H}"])
    label = lambda t: (f"drawtext=font='DejaVu Sans':text='{t}':fontsize=34:fontcolor=white:box=1:boxcolor=black@0.55:"
                       "boxborderw=12:x=(w-text_w)/2:y=20")
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,format=yuv420p"
    g = (f"[0:v]{base},split=3[a][b][c];"
         f"[a]null{thumb_filter(pv, workdir, texts.get('cover') or SAMPLE['cover'])},scale=360:640,"
         f"{label(ROLE_LABEL['cover'])}[pa];"
         f"[b]{subtitles_filter(vid)},scale=360:640,{label(ROLE_LABEL['subtitle'])}[pb];"
         f"[c]boxblur=20:2,eq=brightness=-0.08,{ending_drawtext(vid, workdir, texts.get('ending') or SAMPLE['ending'])},"
         f"scale=360:640,{label(ROLE_LABEL['ending'])}[pc];"
         "[pa][pb][pc]hstack=inputs=3[v]")
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", *src, "-filter_complex", g, "-map", "[v]", "-frames:v", "1",
                           "-q:v", "3", str(out)], cwd=workdir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Không tạo được ảnh minh hoạ font: {proc.stderr[-500:]}")
    return out
