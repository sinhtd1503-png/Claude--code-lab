"""30-day series planning and daily script writing (Claude), with one fixed cast for the whole month.

The "series bible" (characters, house, era, visual style) is created once with the monthly plan and
reused, word for word, in every clip prompt — that is what keeps characters and setting consistent
across clips and across days.
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from .content import _parse

log = logging.getLogger(__name__)


# ---------- schemas ----------

class Character(BaseModel):
    name: str = Field(description="Tên gọi tiếng Việt trong truyện, vd 'Bé Na', 'Bố', 'Bà nội'")
    role: str = Field(description="Vai trò và tuổi, tiếng Việt, vd 'con gái út, 8 tuổi'")
    look_en: str = Field(description="TIẾNG ANH. Ngoại hình CỐ ĐỊNH rất cụ thể: tuổi, dáng, khuôn mặt, kiểu tóc, "
                                     "trang phục và màu sắc cố định (vd 'Vietnamese girl, 8 years old, round face, "
                                     "short black bob hair with straight bangs, faded red cotton shirt, dark blue shorts')")
    voice_en: str = Field(default="", description="TIẾNG ANH. Giọng nói CỐ ĐỊNH để video AI lồng tiếng nhất quán: tuổi, "
                                                  "chất giọng, nhịp, giọng miền, vd 'bright playful 8-year-old girl "
                                                  "voice, Northern Vietnamese accent'")
    voice_type: str = Field(default="female", description="Một trong: girl | boy | female | male | old_female | "
                                                          "old_male (dùng cho giọng dự phòng)")


class SeriesBible(BaseModel):
    era: str = Field(description="Thời kỳ và vùng miền, tiếng Việt, vd 'Làng quê Bắc Bộ, cuối thập niên 1990'")
    characters: list[Character] = Field(description="3–5 nhân vật xuyên suốt cả tháng (một gia đình)")
    setting_en: str = Field(description="TIẾNG ANH. Bối cảnh CỐ ĐỊNH: ngôi nhà, sân, đồ vật đặc trưng, ánh sáng")
    style_en: str = Field(description="TIẾNG ANH. Phong cách hình ảnh CỐ ĐỊNH (chất phim, màu, ống kính, ánh sáng)")


class CharacterFromImage(BaseModel):
    name: str = Field(description="Tên gọi tiếng Việt trong truyện. Lấy từ chú thích của chủ kênh nếu có; "
                                  "nếu không, đặt một tên gọi gia đình tự nhiên, vd 'Bé Na', 'Bà nội'")
    role: str = Field(description="Vai trò và tuổi ước lượng, tiếng Việt, vd 'con gái út, khoảng 8 tuổi'")
    look_en: str = Field(description="TIẾNG ANH, 40–70 từ. Ngoại hình CỐ ĐỊNH đúng như trong ảnh để AI tạo lại giống "
                                     "nhất: giới tính, tuổi, dáng người, khuôn mặt, làn da, kiểu tóc và màu tóc, trang "
                                     "phục và màu sắc, phụ kiện. Không nêu danh tính người thật")
    style_en: str = Field(description="TIẾNG ANH, 1 câu: phong cách hình ảnh của ảnh (ảnh thật, phim cũ, 3D, minh hoạ…), "
                                      "ánh sáng, tông màu")
    voice_en: str = Field(default="", description="TIẾNG ANH. Giọng nói hợp với nhân vật (tuổi, giới tính, tính cách), "
                                                  "giọng miền Bắc, vd 'warm gentle elderly woman voice, slow, Northern "
                                                  "Vietnamese accent'")
    voice_type: str = Field(default="female", description="Một trong: girl | boy | female | male | old_female | old_male")


class DayIdea(BaseModel):
    day: int = Field(description="Số thứ tự ngày, từ 1")
    title: str = Field(description="Tiêu đề ngắn, tiếng Việt")
    theme: str = Field(description="Đúng một trụ cột nội dung trong danh sách được cho")
    hook: str = Field(description="Câu mở đầu 1–2 giây, gợi nhớ mạnh (kiểu 'Ai còn nhớ…', 'Ngày ấy…')")
    story: str = Field(description="MỘT ký ức cụ thể (đồ vật/trò chơi/món ăn/khoảnh khắc) và cách các nhân vật "
                                   "trong series sống lại ký ức đó, 1–2 câu")
    lesson: str = Field(description="Cảm xúc / thông điệp đọng lại ở cuối, tích cực, nhẹ nhàng, không giáo điều")
    characters: list[str] = Field(description="Tên các nhân vật xuất hiện (lấy đúng tên trong bible)")
    why_it_works: str = Field(description="Dựa trên pattern nào của đối thủ")


class MonthPlan(BaseModel):
    patterns: list[str] = Field(description="5–8 pattern về hook, nhịp, cách kể, hình ảnh, âm thanh, caption "
                                            "đang hiệu quả — ưu tiên rút ra từ kênh mẫu")
    top_hashtags: list[str] = Field(description="Hashtag nên dùng, không có dấu #")
    bible: SeriesBible
    days: list[DayIdea] = Field(description="Đúng số ngày yêu cầu, chủ đề xen kẽ, không trùng lặp")


class DialogueLine(BaseModel):
    speaker: str = Field(description="Tên nhân vật nói (đúng tên trong bible)")
    text: str = Field(description="Câu thoại tiếng Việt, tự nhiên, đời thường, 3–10 từ")
    emotion_vi: str = Field(description="Cảm xúc khi nói, tiếng Việt ngắn, vd 'hào hứng', 'thì thầm', 'nghẹn ngào'")
    delivery_en: str = Field(description="TIẾNG ANH. Cách nói để video AI diễn: cảm xúc, âm lượng, nhịp, vd "
                                         "'excited, giggling, fast'")


class SeriesScene(BaseModel):
    visual_vi: str = Field(description="Mô tả hình ảnh của cảnh bằng tiếng Việt, 1 câu, để chủ kênh duyệt")
    characters: list[str] = Field(description="Tên nhân vật xuất hiện trong cảnh (đúng tên trong bible)")
    lines: list[DialogueLine] = Field(description="1–3 câu thoại giữa các nhân vật trong cảnh 8 giây, TỔNG tối đa "
                                                  "16 từ; có thể 0 câu nếu cảnh chỉ có hình + âm thanh")
    action_en: str = Field(description="TIẾNG ANH. MỘT hành động liền mạch, cụ thể trong 8 giây: ai làm gì, ở đâu "
                                       "trong bối cảnh, biểu cảm, ai nói với ai; không mô tả lại ngoại hình nhân vật")
    camera_en: str = Field(description="TIẾNG ANH. Góc máy và chuyển động máy, vd 'slow push-in, eye level'")
    ambient_en: str = Field(default="", description="TIẾNG ANH. Âm thanh môi trường, vd 'cicadas, distant rooster, "
                                                    "wind in bamboo'")
    visual_query: str = Field(description="TIẾNG ANH, 2–4 từ để tìm video stock dự phòng trên Pixabay")


class SeriesScript(BaseModel):
    title: str
    hook: str = Field(description="Câu thoại mở đầu gây tò mò/gợi nhớ (chính là câu thoại đầu tiên)")
    scenes: list[SeriesScene]
    music_mood: str = Field(default="", description="Kiểu nhạc nền hợp video, tiếng Việt, vd 'piano nhẹ, hoài niệm'")
    lesson: str = Field(description="Thông điệp của video, 1 câu")
    caption: str = Field(description="Caption TikTok tiếng Việt, ấm áp, có câu hỏi gợi bình luận")
    hashtags: list[str] = Field(description="5–8 hashtag, không có dấu #")


# ---------- prompts ----------

def system_prompt(niche: dict) -> str:
    pillars = niche.get("pillars") or ["Tuổi thơ 8x 9x", "Gia đình", "Làng quê", "Trường lớp", "Tết"]
    style = (niche.get("style_reference") or "").strip()
    visual = (niche.get("visual_style_en") or "").strip()
    return f"""Bạn là biên kịch và chiến lược gia nội dung cho một kênh TikTok tiếng Việt.

Ngách: {niche['name']}
Mô tả: {niche['description']}
Khán giả: {niche['audience']}
Giọng điệu: {niche['tone']}
Trụ cột nội dung: {' | '.join(pillars)}

Phong cách trình bày phải theo sát kênh mẫu (hình ảnh, nhịp, cách kể, âm thanh, chữ trên màn hình):
<style_reference>
{style or 'Video hoài niệm ngắn, clip AI chuyển động, nhân vật thoại với nhau + nhạc nền nhẹ, phụ đề theo lời thoại.'}
</style_reference>

Hình thức video (bắt buộc):
- KHÔNG có người dẫn chuyện, KHÔNG có giọng đọc thuyết minh. Câu chuyện kể hoàn toàn bằng THOẠI tự nhiên, có cảm xúc
  giữa các nhân vật (trêu đùa, hỏi han, thì thầm, reo lên, nghẹn ngào…), kèm âm thanh môi trường và nhạc nền nhẹ.
- Phong cách hình ảnh cố định: {visual or 'phim hoạt hình anime điện ảnh, nền vẽ tay chi tiết, ánh sáng ấm'}.

Giá trị cốt lõi của kênh:
- Mỗi video đánh thức MỘT ký ức tuổi thơ 8x 9x thật cụ thể, bằng chi tiết giác quan (âm thanh, mùi vị, cảm giác
  trên tay, ánh nắng, tiếng gọi) để người xem thốt lên "đúng rồi, ngày xưa mình cũng vậy".
- Luôn tích cực và tử tế: tình thân, sự hồn nhiên, lễ phép, biết ơn, sẻ chia; kết bằng một cảm xúc ấm áp giúp người
  xem trân quý gia đình và hiện tại. Không giáo điều, không chê bai thế hệ trẻ hay cuộc sống hiện đại.

Nguyên tắc:
- Học kênh mẫu và đối thủ để hiểu VÌ SAO họ hiệu quả và bắt chước CÁCH TRÌNH BÀY; luôn viết câu chuyện, lời văn,
  nhân vật hoàn toàn mới; không chép caption, kịch bản, câu chữ hay nhân vật của họ.
- Không bịa số liệu, không giả danh người thật, không clickbait sai sự thật, không chính trị, không cảnh nguy hiểm
  hay bạo lực với trẻ em, không quảng cáo sản phẩm, không nhắc tên thương hiệu thật.
- Video là phim ngắn dựng bằng AI: vài clip 8 giây, nhân vật nói thoại (video AI tự lồng tiếng, khớp khẩu hình) +
  nhạc nền + phụ đề; nhân vật, giọng nói và bối cảnh cố định suốt series để người xem nhận ra "gia đình" của kênh."""


def _compact(videos: list[dict]) -> list[dict]:
    return [{"author": v["author"], "caption": v["caption"][:300], "hashtags": v["hashtags"][:8],
             "music": (v.get("music") or "")[:80], "plays": v["plays"], "likes": v["likes"],
             "comments": v["comments"], "shares": v["shares"], "saves": v["saves"], "duration": v["duration"]}
            for v in videos]


def make_plan(niche: dict, videos: list[dict], recent_titles: list[str], days: int,
              feedback: str | None = None, previous: dict | None = None,
              reference: list[dict] | None = None, cast: list[dict] | None = None) -> MonthPlan:
    compact = _compact(videos)
    ref = _compact(reference or [])
    parts = []
    if ref:
        parts.append("KÊNH MẪU — video hiệu quả nhất gần đây (JSON). Đây là phong cách cần theo sát nhất: "
                     "phân tích kỹ hook, độ dài, cách đặt caption, hashtag, loại nhạc, chủ đề ký ức nào được "
                     f"tương tác cao:\n<reference_videos>\n{json.dumps(ref, ensure_ascii=False)}\n</reference_videos>")
    if compact:
        parts.append("Video hiệu quả nhất gần đây của các kênh tham khảo khác (JSON):\n"
                     f"<videos>\n{json.dumps(compact, ensure_ascii=False)}\n</videos>")
    if not compact and not ref:
        parts.append("Chưa có dữ liệu đối thủ lần này; hãy dựa trên phong cách kênh mẫu mô tả ở trên và hiểu biết "
                     "của bạn về các kênh hoài niệm 8x 9x.")
    parts.append("Các tiêu đề kênh đã làm (tránh trùng):\n"
                 f"<recent>\n{json.dumps(recent_titles, ensure_ascii=False)}\n</recent>")
    if cast:
        people = [{"name": c["name"], "role": c["role"], "look_en": c["look_en"]} for c in cast]
        parts.append("NHÂN VẬT DO CHỦ KÊNH CHỌN (từ ảnh minh hoạ) — series bible PHẢI dùng đúng các nhân vật này, "
                     "giữ nguyên văn name, role và look_en; chỉ được thêm tối đa 2 nhân vật phụ nếu thật cần. "
                     "style_en của bible phải hợp với phong cách các ảnh:\n"
                     f"<cast>\n{json.dumps(people, ensure_ascii=False)}\n</cast>\n"
                     f"Phong cách ảnh: {json.dumps([c.get('style_en', '') for c in cast], ensure_ascii=False)}")
    if previous:
        parts.append("Kế hoạch trước (chủ kênh muốn làm lại):\n"
                     f"<previous>\n{json.dumps(previous, ensure_ascii=False)[:20000]}\n</previous>")
    if feedback:
        parts.append(f"Góp ý của chủ kênh, bắt buộc làm theo:\n<feedback>\n{feedback}\n</feedback>")
    parts.append(
        f"Hãy: (1) rút ra pattern hiệu quả; (2) tạo series bible cho một gia đình Việt Nam cố định dùng suốt {days} ngày; "
        f"(3) lên kế hoạch ĐÚNG {days} ngày, mỗi ngày MỘT ký ức tuổi thơ cụ thể khác nhau, sống lại qua chính các "
        "nhân vật này qua những cuộc trò chuyện; xoay vòng đều các trụ cột nội dung; mỗi ngày kết bằng một cảm xúc ấm "
        "áp khác nhau. Mỗi nhân vật cần voice_en (giọng nói cố định) và voice_type. Viết ngắn gọn."
    )
    log.info("Claude: lập kế hoạch %d ngày (%d video kênh mẫu, %d video tham khảo)…", days, len(ref), len(compact))
    return _parse(system_prompt(niche), "\n\n".join(parts), MonthPlan, effort="medium", max_tokens=32000)


def write_script(niche: dict, bible: dict, idea: dict, patterns: list[str], scenes: int, clip_sec: int,
                 owner_notes: list[str], feedback: str | None = None, previous: dict | None = None) -> SeriesScript:
    parts = [
        f"Viết kịch bản video TikTok ~{scenes * clip_sec - 4}–{scenes * clip_sec} giây cho ngày sau trong series:",
        f"<idea>\n{json.dumps(idea, ensure_ascii=False)}\n</idea>",
        f"Series bible (nhân vật và bối cảnh cố định — dùng đúng tên):\n<bible>\n{json.dumps(bible, ensure_ascii=False)}\n</bible>",
        f"Kịch bản gồm ĐÚNG {scenes} cảnh. Mỗi cảnh là MỘT clip video AI {clip_sec} giây, trong đó các nhân vật NÓI "
        f"VỚI NHAU: 1–3 câu thoại ngắn, TỔNG tối đa 16 từ mỗi cảnh (để kịp nói tự nhiên trong {clip_sec} giây, có "
        "khoảng lặng). KHÔNG có người dẫn chuyện. Các cảnh nối tiếp liền mạch về thời gian và không gian (cùng buổi, "
        "cùng nơi, trang phục không đổi): cảnh 1 = câu thoại mở đầu gây tò mò/gợi nhớ, giữa = cùng nhau sống lại ký ức "
        "(chi tiết giác quan qua lời thoại và hành động), cảnh cuối = khoảnh khắc chạm lòng, câu thoại đọng lại. "
        "Hành động cuối của cảnh trước nối được với đầu cảnh sau. Chọn music_mood hợp cảm xúc.",
    ]
    if patterns:
        parts.append(f"Pattern hiệu quả từ đối thủ:\n<patterns>\n{json.dumps(patterns, ensure_ascii=False)}\n</patterns>")
    if owner_notes:
        parts.append("Những góp ý chủ kênh đã đưa ra trước đây (hãy tiếp tục tôn trọng):\n<notes>\n"
                     + "\n".join(f"- {n}" for n in owner_notes) + "\n</notes>")
    if previous and feedback:
        parts.append(f"Bản trước:\n<previous>\n{json.dumps(previous, ensure_ascii=False)}\n</previous>\n"
                     f"Chủ kênh góp ý, hãy sửa theo:\n<feedback>\n{feedback}\n</feedback>")
    elif previous:
        parts.append("Chủ kênh muốn một phiên bản KHÁC hẳn (hook và cách kể khác), vẫn cùng ý tưởng:\n"
                     f"<previous>\n{json.dumps(previous, ensure_ascii=False)}\n</previous>")
    parts.append("Thoại như người Việt nói thật ngoài đời (xưng hô đúng vai: con – mẹ, cháu – bà, anh – em…), có cảm "
                 "xúc, không văn vẻ, không giảng giải. Caption ngắn, gợi nhớ, kết bằng câu hỏi gợi bình luận "
                 "(vd 'Bạn còn nhớ không?').")
    log.info("Claude: viết kịch bản '%s'", idea.get("title"))
    return _parse(system_prompt(niche), "\n\n".join(parts), SeriesScript, effort="high")


def describe_character(image: bytes, media_type: str, caption: str, era: str = "") -> CharacterFromImage:
    system = ("Bạn mô tả nhân vật trong ảnh minh hoạ để công cụ tạo video AI dựng lại nhân vật đó nhất quán qua nhiều "
              "clip. Chỉ mô tả những gì nhìn thấy (ngoại hình, trang phục, phong cách ảnh), cụ thể, có màu sắc. Không đoán "
              "hay nêu danh tính người thật, không nhận xét ngoại hình theo hướng tiêu cực.")
    user = (f"Chú thích của chủ kênh: {caption or '(không có)'}\n"
            f"Bối cảnh series: {era or 'Việt Nam thập niên 1990'}\n"
            "Nếu ảnh có nhiều người, chỉ mô tả nhân vật chính (người nổi bật nhất, hoặc người chú thích nhắc tới).")
    return _parse(system, user, CharacterFromImage, effort="medium", max_tokens=3000, images=[(image, media_type)])


def apply_cast(bible: dict, cast: list[dict], limit: int = 6) -> dict:
    """Force the owner's characters (exact look) into a bible: replace same-name entries, else put them first."""
    chars = list(bible.get("characters") or [])
    for c in cast:
        entry = {"name": c["name"], "role": c["role"], "look_en": c["look_en"]}
        if c.get("voice_en"):
            entry["voice_en"] = c["voice_en"]
        if c.get("voice_type"):
            entry["voice_type"] = c["voice_type"]
        idx = next((i for i, x in enumerate(chars) if x["name"].strip().lower() == c["name"].strip().lower()), None)
        if idx is None:
            chars.insert(0, entry)
        else:
            chars[idx] = {**chars[idx], **entry}  # keep the voice the plan gave this character
    keep = {c["name"].strip().lower() for c in cast}
    extra = [x for x in chars if x["name"].strip().lower() not in keep]
    ordered = [x for x in chars if x["name"].strip().lower() in keep]
    return {**bible, "characters": (ordered + extra)[:max(limit, len(ordered))]}


# ---------- clip prompts (consistency lives here) ----------

def character_block(bible: dict, names: list[str]) -> str:
    chars = bible.get("characters", [])
    picked = [c for c in chars if c["name"] in names] or chars[:2]
    return "; ".join(f"{c['name']} ({c['role']}): {c['look_en']}" for c in picked)


def clip_prompt(bible: dict, scene: dict, index: int, total: int) -> str:
    chars = {c["name"]: c for c in bible.get("characters", [])}
    parts = [
        "Vertical 9:16 animated shot, 8 seconds, part of a short nostalgic film about a Vietnamese 1990s childhood "
        "memory.",
        f"Visual style (identical in every shot): {bible.get('style_en', '').rstrip('.')}.",
        f"Era and place: {bible.get('era', '')}.",
        f"Setting (same place in every shot): {bible.get('setting_en', '').rstrip('.')}.",
        f"Characters (exact same faces, hair, clothing and age in every shot): {character_block(bible, scene.get('characters', []))}.",
        f"Action: {scene['action_en'].rstrip('.')}.",
        f"Camera: {scene.get('camera_en', 'slow gentle movement').rstrip('.')}.",
    ]
    if index > 0:
        parts.append("Continuity: continues directly from the previous shot, same moment of the day, same lighting.")
    lines = scene.get("lines") or []
    if lines:
        spoken = []
        for ln in lines:
            voice = (chars.get(ln["speaker"]) or {}).get("voice_en", "")
            spoken.append(f'{ln["speaker"]} ({ln.get("delivery_en", "")}{"; " + voice if voice else ""}) says in '
                          f'Vietnamese: "{ln["text"]}"')
        parts.append("Dialogue, in this order, natural pauses, accurate lip-sync, spoken in Vietnamese: "
                     + " Then ".join(spoken) + ".")
    else:
        parts.append("No dialogue.")
    amb = (scene.get("ambient_en") or "").rstrip(".")
    parts.append(f"Sound: {'the dialogue and ' if lines else ''}soft ambient sound{(' (' + amb + ')') if amb else ''}; "
                 "no background music, no narrator.")
    parts.append("No text, no captions, no subtitles, no logos, no watermark.")
    return "\n".join(parts)


def character_sheet_prompt(bible: dict) -> str:
    """One-time prompt for a reference image of the cast (used as 'ingredients' in Flow for every clip)."""
    chars = "; ".join(f"{c['name']} ({c['role']}): {c['look_en']}" for c in bible.get("characters", []))
    return ("Character reference sheet, full body, front view, plain light background, soft even light, "
            f"{bible.get('style_en', '').rstrip('.')}. Characters standing side by side: {chars}. "
            "No text.")
