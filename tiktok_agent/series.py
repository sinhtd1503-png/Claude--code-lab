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
from .policy import RULES_VI, sanitize

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
    speech_vi: str = Field(default="", description="Cách xưng hô và nói chuyện cố định, tiếng Việt")


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
    start_en: str = Field(default="", description="TIẾNG ANH. KHUNG HÌNH ĐẦU của cảnh: ai đứng/ngồi ở đâu, tư thế, "
                                                  "đang cầm gì, góc máy. Từ cảnh 2 trở đi PHẢI trùng với end_en của "
                                                  "cảnh trước (cùng vị trí, tư thế, đồ vật, ánh sáng)")
    end_en: str = Field(default="", description="TIẾNG ANH. KHUNG HÌNH CUỐI của cảnh: vị trí, tư thế, biểu cảm, đồ vật "
                                                "trong tay, góc máy — để cảnh sau bắt đầu đúng từ đây")
    ambient_en: str = Field(default="", description="TIẾNG ANH. Âm thanh môi trường, vd 'cicadas, distant rooster, "
                                                    "wind in bamboo'")
    visual_query: str = Field(description="TIẾNG ANH, 2–4 từ để tìm video stock dự phòng trên Pixabay")


class SeriesScript(BaseModel):
    title: str
    hook: str = Field(description="Câu thoại mở đầu gây tò mò/gợi nhớ (chính là câu thoại đầu tiên)")
    scenes: list[SeriesScene]
    music_mood: str = Field(default="", description="Kiểu nhạc nền hợp video, tiếng Việt, vd 'piano nhẹ, hoài niệm'")
    continuity_en: str = Field(default="", description="TIẾNG ANH, 1–2 câu. Điều KHÔNG ĐỔI suốt video: đúng một chỗ cụ "
                                                       "thể trong bối cảnh, thời điểm trong ngày, thời tiết, ánh sáng, "
                                                       "đồ vật chính, ai có mặt — vd 'Late afternoon in the brick yard "
                                                       "under the guava tree, golden low sun from the left, a woven "
                                                       "bamboo tray of rice cakes on a low wooden table'")
    lesson: str = Field(description="Thông điệp của video, 1 câu")
    thumb_text: str = Field(default="", description="Chữ to trên ẢNH BÌA (thumbnail), tiếng Việt, 3–7 từ, nêu bật thông "
                                                    "điệp/ký ức để người lướt dừng lại, vd 'Chiếc diều của ông', 'Mùa "
                                                    "hè không điện thoại'. Không dấu chấm, không emoji")
    closing_line: str = Field(default="", description="CÂU KẾT hiện trên màn hình ở clip cuối để người xem ghi nhớ, tiếng "
                                                      "Việt, 6–14 từ, ấm áp, đọng lại, không giáo điều, vd 'Có những "
                                                      "mùa hè chỉ còn trong ký ức — và trong tim'")
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

{RULES_VI}
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
        parts.append("GIA ĐÌNH CỐ ĐỊNH CỦA KÊNH (chủ kênh đã cấu hình) — series bible PHẢI gồm ĐÚNG và CHỈ các nhân vật "
                     "này, giữ nguyên văn name, role, look_en, voice_en, voice_type, speech_vi; KHÔNG thêm nhân vật "
                     "khác. Mỗi ngày chỉ dùng 2–4 người trong số họ:\n"
                     f"<cast>\n{json.dumps(cast_brief(cast), ensure_ascii=False)}\n</cast>")
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


def cast_brief(chars: list[dict]) -> list[dict]:
    return [{k: c.get(k, "") for k in ("name", "role", "look_en", "voice_en", "voice_type", "speech_vi")} for c in chars]


def write_script(niche: dict, bible: dict, idea: dict, patterns: list[str], scenes: int, clip_sec: int,
                 owner_notes: list[str], feedback: str | None = None, previous: dict | None = None) -> SeriesScript:
    names = [c["name"] for c in bible.get("characters", [])]
    brief = {**{k: v for k, v in bible.items() if k != "characters"}, "characters": cast_brief(bible.get("characters", []))}
    parts = [
        f"Viết kịch bản video TikTok ~{scenes * clip_sec - 4}–{scenes * clip_sec} giây cho ngày sau trong series:",
        f"<idea>\n{json.dumps(idea, ensure_ascii=False)}\n</idea>",
        f"Series bible (nhân vật và bối cảnh cố định):\n<bible>\n{json.dumps(brief, ensure_ascii=False)}\n</bible>",
        "NHÂN VẬT: chỉ dùng các nhân vật trong bible, viết ĐÚNG tên " + json.dumps(names, ensure_ascii=False)
        + " ở speaker và characters (nếu ý tưởng nhắc tên khác, đổi sang nhân vật tương ứng trong bible). Mỗi cảnh tối "
        "đa 3 nhân vật, người nói phải có trong characters của cảnh. Thoại đúng speech_vi (xưng hô, cách nói) của "
        "từng người, nhất quán từ cảnh đầu đến cảnh cuối. Không mô tả lại ngoại hình hay đổi trang phục.",
        f"Kịch bản gồm ĐÚNG {scenes} cảnh. Mỗi cảnh là MỘT clip video AI {clip_sec} giây, trong đó các nhân vật NÓI "
        f"VỚI NHAU: 1–3 câu thoại ngắn, TỔNG tối đa 16 từ mỗi cảnh (để kịp nói tự nhiên trong {clip_sec} giây, có "
        "khoảng lặng). KHÔNG có người dẫn chuyện. " + beats(scenes) + " Chọn music_mood hợp cảm xúc.",
        "LIỀN MẠCH (rất quan trọng — mỗi cảnh được tạo thành một clip RIÊNG rồi mới ghép): cả video là MỘT khoảnh khắc "
        "liên tục, như một cảnh quay dài bị cắt thành từng đoạn. Ghi continuity_en (nơi chốn cụ thể, giờ, ánh sáng, "
        "đồ vật, ai có mặt) và giữ nguyên trong mọi cảnh: không đổi chỗ, không nhảy thời gian, không đổi trang phục, "
        "không thêm/bớt đồ vật chính. Mỗi cảnh có start_en và end_en; start_en của cảnh sau = end_en của cảnh trước "
        "(cùng vị trí, tư thế, đồ vật trong tay, hướng nhìn). Hành động trong cảnh bắt đầu từ start_en và kết thúc "
        "đúng ở end_en; câu thoại cuối của cảnh trước được đáp lại ở đầu cảnh sau.",
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
    parts.append("Viết thêm thumb_text (chữ to trên ảnh bìa, 3–7 từ, nêu bật thông điệp) và closing_line (câu kết hiện "
                 "trên màn hình ở cuối video, 6–14 từ, đọng lại cảm xúc của thông điệp; khác câu thoại cuối).")
    parts.append("Thoại như người Việt nói thật ngoài đời (xưng hô đúng vai: con – mẹ, cháu – bà, anh – em…), có cảm "
                 "xúc, không văn vẻ, không giảng giải. Caption ngắn, gợi nhớ, kết bằng câu hỏi gợi bình luận "
                 "(vd 'Bạn còn nhớ không?').")
    log.info("Claude: viết kịch bản '%s'", idea.get("title"))
    return _parse(system_prompt(niche), "\n\n".join(parts), SeriesScript, effort="high")


def beats(n: int) -> str:
    """Story beats for n scenes (works for 3, 4, 5, 6…)."""
    if n <= 1:
        return "Một cảnh duy nhất: mở bằng câu gợi nhớ, kết bằng khoảnh khắc ấm áp."
    if n == 2:
        return "Cảnh 1 = câu thoại mở đầu gây tò mò/gợi nhớ; cảnh 2 = khoảnh khắc chạm lòng, câu thoại đọng lại."
    mid = "cảnh 2" if n == 3 else f"cảnh 2–{n - 1}"
    extra = (" Với nhiều cảnh: các cảnh giữa đi từng bước (nhớ lại → cùng làm/chơi → một chi tiết bất ngờ nhỏ, vui "
             "hoặc xúc động), mỗi cảnh thêm MỘT chi tiết mới, không lặp ý.") if n >= 4 else ""
    return (f"Cấu trúc: cảnh 1 = câu thoại mở đầu gây tò mò/gợi nhớ; {mid} = cùng nhau sống lại ký ức (chi tiết giác "
            f"quan qua lời thoại và hành động); cảnh {n} = khoảnh khắc chạm lòng, câu thoại đọng lại.{extra}")


def describe_character(image: bytes, media_type: str, caption: str, era: str = "", who: str = "") -> CharacterFromImage:
    system = ("Bạn mô tả nhân vật trong ảnh minh hoạ để công cụ tạo video AI dựng lại nhân vật đó nhất quán qua nhiều "
              "clip. Chỉ mô tả những gì nhìn thấy (ngoại hình, trang phục, phong cách ảnh), cụ thể, có màu sắc. Không đoán "
              "hay nêu danh tính người thật, không nhận xét ngoại hình theo hướng tiêu cực.")
    user = ((f"Đây là ảnh mẫu cho nhân vật cố định: {who}. Giữ đúng tên này; mô tả look_en thật chi tiết, cụ thể để "
             "mọi clip vẽ lại đúng người này (khuôn mặt, kiểu tóc, trang phục, màu sắc).\n" if who else "")
            + f"Chú thích của chủ kênh: {caption or '(không có)'}\n"
            f"Bối cảnh series: {era or 'Việt Nam thập niên 1990'}\n"
            "Nếu ảnh có nhiều người, chỉ mô tả nhân vật chính (người nổi bật nhất, hoặc người chú thích nhắc tới). "
            "Với trẻ em: KHÔNG ghi số tuổi cụ thể trong look_en (dùng 'little girl', 'young boy'), chỉ mô tả "
            "khuôn mặt, tóc, trang phục; không mô tả cơ thể.")
    return _parse(system, user, CharacterFromImage, effort="medium", max_tokens=3000, images=[(image, media_type)])


def set_cast(bible: dict, cast: list[dict]) -> dict:
    """The bible's characters ARE the channel's fixed family (exact look, voice, speech) — nobody else."""
    return {**bible, "characters": [dict(c) for c in cast]}


def invent_character(name: str, role: str, era: str, style: str) -> CharacterFromImage:
    """New character from a name + role only (no photo yet): fixed look and voice to reuse in every clip."""
    system = ("Bạn thiết kế một nhân vật phụ cố định cho series phim ngắn hoạt hình về ký ức tuổi thơ Việt Nam. Mô tả "
              "cụ thể, có màu sắc, để công cụ tạo video AI vẽ lại giống hệt nhau qua nhiều clip. Không dựa trên người "
              "thật.\n\n" + RULES_VI)
    user = (f"Tên: {name}\nVai trò: {role or '(chưa rõ, hãy chọn vai hợp với tên)'}\nBối cảnh: {era}\n"
            f"Phong cách hình ảnh: {style}\nGiữ nguyên tên. Với trẻ em: không ghi số tuổi trong look_en.")
    return _parse(system, user, CharacterFromImage, effort="low", max_tokens=3000)


# ---------- clip prompts (consistency lives here) ----------

def scene_cast(bible: dict, scene: dict) -> list[dict]:
    """Characters on screen in a scene (listed ones + every speaker), in bible order."""
    chars = bible.get("characters", [])
    want = {n.strip().lower() for n in list(scene.get("characters") or [])
            + [ln.get("speaker", "") for ln in scene.get("lines") or []]}
    return [c for c in chars if c["name"].strip().lower() in want] or chars[:2]


def character_block(bible: dict, names: list[str]) -> str:
    # English look only: the Vietnamese role often contains a child's exact age, which video models refuse
    picked = scene_cast(bible, {"characters": names})
    return "; ".join(f"{c['name']}: {c['look_en']}" for c in picked)


def film_cast(bible: dict, scenes: list[dict]) -> list[dict]:
    """Everyone who appears anywhere in the video, in bible order (same block in every scene prompt)."""
    seen: list[dict] = []
    for sc in scenes:
        for c in scene_cast(bible, sc):
            if c not in seen:
                seen.append(c)
    order = {c["name"]: i for i, c in enumerate(bible.get("characters", []))}
    return sorted(seen, key=lambda c: order.get(c["name"], 99))


def clip_prompt(bible: dict, scene: dict, index: int, total: int, script: dict | None = None) -> str:
    """Prompt for one 8-second clip. With `script`, every clip of the video shares the same cast block, the same
    continuity line and starts exactly where the previous clip ended — so separately made clips join seamlessly."""
    on_screen = scene_cast(bible, scene)
    scenes = (script or {}).get("scenes") or [scene]
    cast_all = film_cast(bible, scenes) if script else on_screen
    prev = scenes[index - 1] if script and 0 < index <= len(scenes) else None
    chars = {c["name"].strip().lower(): c for c in bible.get("characters", [])}
    lines = scene.get("lines") or []
    parts = []
    if prev:  # continuation first: video models weigh the start of the prompt most
        parts += [
            f"CONTINUATION of the previous clip (clip {index} of {total}) — NO cut, NO new scene: the same place, the "
            "same moment, the same people with the same faces, hair and clothes, the same camera lens, framing and "
            "height, the same lighting and color grade. Begin exactly on the previous clip's last frame.",
            f"Previous clip (already filmed): {prev.get('action_en', '').rstrip('.')}. It ended on: "
            f"{(prev.get('end_en') or 'the characters in the same positions').rstrip('.')}. "
            f"Previous camera: {prev.get('camera_en', 'gentle movement').rstrip('.')}.",
        ]
    parts += [
        "Vertical 9:16 animated shot, 8 seconds, part of a short nostalgic film about a Vietnamese 1990s childhood "
        "memory. The same family appears in every shot of this film.",
        f"Visual style (identical in every shot): {bible.get('style_en', '').rstrip('.')}.",
        f"Era and place: {bible.get('era', '')}.",
        f"Setting (same place in every shot): {bible.get('setting_en', '').rstrip('.')}.",
        "Characters of this film — exactly the same face, hairstyle, outfit, colors and body proportions in every shot "
        "(match the character reference images, if provided): "
        + " | ".join(f"{c['name']}: {c['look_en'].rstrip('.')}" for c in cast_all) + ".",
        f"In this shot: {', '.join(c['name'] for c in on_screen)} only; no other people.",
    ]
    cont = ((script or {}).get("continuity_en") or "").strip().rstrip(".")
    if cont:
        parts.append(f"Same continuous moment in every shot (do not change): {cont}.")
    if script and total > 1:
        parts.append(f"This is shot {index + 1} of {total} of one continuous scene, cut into separate clips.")
    start = ((prev or {}).get("end_en") or scene.get("start_en") or "").strip().rstrip(".")
    if start:
        parts.append(f"Opening frame{' (exactly where the previous shot ended)' if prev else ''}: {start}.")
    elif index > 0:
        parts.append("Opening frame: continues directly from the previous shot, same positions and lighting.")
    parts.append(f"Action: {scene['action_en'].rstrip('.')}.")
    end = (scene.get("end_en") or "").strip().rstrip(".")
    if end:
        parts.append(f"Final frame (hold it still for the last half second): {end}.")
    parts.append(f"Camera: {scene.get('camera_en', 'slow gentle movement').rstrip('.')}.")
    if index > 0:
        parts.append("Continuity: same lighting, color grade, clothes and props as the previous shot; no time jump.")
    if lines:
        speakers = []
        for ln in lines:
            c = chars.get(ln["speaker"].strip().lower())
            if c and c.get("voice_en") and c not in speakers:
                speakers.append(c)
        if speakers:
            parts.append("Voices (each character always has this exact same voice in every shot): "
                         + " | ".join(f"{c['name']}: {c['voice_en'].rstrip('.')}" for c in speakers) + ".")
        spoken = [f'{ln["speaker"]} ({ln.get("delivery_en", "").rstrip(".") or "natural"}) says in Vietnamese: '
                  f'"{ln["text"]}"' for ln in lines]
        parts.append("Dialogue, in this order, natural pauses, accurate lip-sync, spoken in Vietnamese: "
                     + " Then ".join(spoken) + ".")
    else:
        parts.append("No dialogue.")
    amb = (scene.get("ambient_en") or "").rstrip(".")
    parts.append(f"Sound: only {'the dialogue and ' if lines else ''}soft natural ambient sound"
                 f"{(' (' + amb + ')') if amb else ''}. Absolutely NO music, no soundtrack, no score, no singing, "
                 "no narrator (music is added later for the whole film).")
    parts.append("Wholesome, family-friendly; everyone fully clothed and safe. No text, no captions, no subtitles, "
                 "no logos, no watermark.")
    return sanitize("\n".join(parts))


def extend_prompt(bible: dict, scene: dict, index: int, total: int, script: dict | None = None) -> str:
    """Short prompt for Google Flow's "Extend" (continue the previous clip): only what happens next."""
    chars = {c["name"].strip().lower(): c for c in bible.get("characters", [])}
    lines = scene.get("lines") or []
    parts = [f"Continue this exact shot seamlessly for 8 more seconds (clip {index + 1} of {total}): same place, same "
             "people, same clothes, same lighting, same camera lens; no cut, no new people.",
             f"Action: {scene['action_en'].rstrip('.')}.",
             f"Camera: {scene.get('camera_en', 'slow gentle movement').rstrip('.')}."]
    end = (scene.get("end_en") or "").strip().rstrip(".")
    if end:
        parts.append(f"End on: {end}.")
    if lines:
        spoken = []
        for ln in lines:
            c = chars.get(ln["speaker"].strip().lower()) or {}
            v = f"; {c['voice_en'].rstrip('.')}" if c.get("voice_en") else ""
            spoken.append(f'{ln["speaker"]} ({(ln.get("delivery_en") or "natural").rstrip(".")}{v}) says in Vietnamese: '
                          f'"{ln["text"]}"')
        parts.append("Dialogue, accurate lip-sync: " + " Then ".join(spoken) + ".")
    parts.append("Sound: only the dialogue and soft ambient sound; absolutely no music. No text, no captions.")
    return sanitize("\n".join(parts))


class SafePrompt(BaseModel):
    prompt_en: str = Field(description="Prompt tiếng Anh đã viết lại, giữ đúng cấu trúc, phong cách, nhân vật và câu "
                                       "chuyện; câu thoại tiếng Việt giữ nguyên nếu không vi phạm")
    changes_vi: str = Field(description="Đã sửa những gì, 1–2 câu tiếng Việt")


def safer_prompt(prompt: str, error: str = "") -> SafePrompt:
    """Rewrite one clip prompt that Google Flow / Gemini refused for policy reasons."""
    system = ("Bạn là chuyên gia viết prompt video AI tuân thủ chính sách nội dung của Google (Flow, Veo, Gemini). "
              "Viết lại prompt để chắc chắn được chấp nhận mà vẫn giữ câu chuyện, cảm xúc, phong cách hình ảnh, "
              "nhân vật và thoại.\n\n" + RULES_VI)
    user = (f"Prompt bị từ chối:\n<prompt>\n{prompt}\n</prompt>\n"
            + (f"Thông báo lỗi: {error}\n" if error else "")
            + "Tìm mọi chi tiết có thể vi phạm (kể cả gián tiếp: tuổi trẻ em, tiếp xúc cơ thể, nguy hiểm, đồ vật nhạy "
              "cảm, thương hiệu, phong cách hãng phim, chữ trên hình…) và thay bằng chi tiết an toàn tương đương.")
    out = _parse(system, user, SafePrompt, effort="low", max_tokens=4000)
    out.prompt_en = sanitize(out.prompt_en)
    return out


def character_sheet_prompt(bible: dict) -> str:
    """One-time prompt for a reference image of the cast (used as 'ingredients' in Flow for every clip)."""
    chars = "; ".join(f"{c['name']}: {c['look_en']}" for c in bible.get("characters", []))
    return sanitize("Character reference sheet, full body, front view, plain light background, soft even light, "
                    f"{bible.get('style_en', '').rstrip('.')}. Characters standing side by side: {chars}. "
                    "Wholesome family portrait, everyone fully clothed. No text.")


def portrait_prompt(bible: dict, c: dict) -> str:
    """One-time prompt for ONE character's reference image (Flow image mode / Gemini / Whisk)."""
    return sanitize(f"Character reference portrait of {c['name']}, full body, front view, neutral friendly pose, plain "
                    f"light background, soft even light, {bible.get('style_en', '').rstrip('.')}. "
                    f"{c['look_en'].rstrip('.')}. Wholesome, fully clothed. Only this one person. No text.")
