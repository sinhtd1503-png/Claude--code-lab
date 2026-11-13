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


class SeriesBible(BaseModel):
    era: str = Field(description="Thời kỳ và vùng miền, tiếng Việt, vd 'Làng quê Bắc Bộ, cuối thập niên 1990'")
    characters: list[Character] = Field(description="3–5 nhân vật xuyên suốt cả tháng (một gia đình)")
    setting_en: str = Field(description="TIẾNG ANH. Bối cảnh CỐ ĐỊNH: ngôi nhà, sân, đồ vật đặc trưng, ánh sáng")
    style_en: str = Field(description="TIẾNG ANH. Phong cách hình ảnh CỐ ĐỊNH (chất phim, màu, ống kính, ánh sáng)")


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


class SeriesScene(BaseModel):
    narration: str = Field(description="Lời đọc tiếng Việt của cảnh, 15–19 từ (đọc ~6–7 giây), tối đa 21 từ")
    visual_vi: str = Field(description="Mô tả hình ảnh của cảnh bằng tiếng Việt, 1 câu, để chủ kênh duyệt")
    characters: list[str] = Field(description="Tên nhân vật xuất hiện trong cảnh (đúng tên trong bible)")
    action_en: str = Field(description="TIẾNG ANH. MỘT hành động liền mạch, cụ thể trong 8 giây: ai làm gì, ở đâu "
                                       "trong bối cảnh, biểu cảm; không mô tả lại ngoại hình nhân vật")
    camera_en: str = Field(description="TIẾNG ANH. Góc máy và chuyển động máy, vd 'slow push-in, eye level'")
    visual_query: str = Field(description="TIẾNG ANH, 2–4 từ để tìm video stock dự phòng trên Pixabay")


class SeriesScript(BaseModel):
    title: str
    hook: str
    scenes: list[SeriesScene]
    lesson: str = Field(description="Thông điệp của video, 1 câu")
    caption: str = Field(description="Caption TikTok tiếng Việt, ấm áp, có câu hỏi gợi bình luận")
    hashtags: list[str] = Field(description="5–8 hashtag, không có dấu #")


# ---------- prompts ----------

def system_prompt(niche: dict) -> str:
    pillars = niche.get("pillars") or ["Tuổi thơ 8x 9x", "Gia đình", "Làng quê", "Trường lớp", "Tết"]
    style = (niche.get("style_reference") or "").strip()
    return f"""Bạn là biên kịch và chiến lược gia nội dung cho một kênh TikTok tiếng Việt.

Ngách: {niche['name']}
Mô tả: {niche['description']}
Khán giả: {niche['audience']}
Giọng điệu: {niche['tone']}
Trụ cột nội dung: {' | '.join(pillars)}

Phong cách trình bày phải theo sát kênh mẫu (hình ảnh, nhịp, cách kể, âm thanh, chữ trên màn hình):
<style_reference>
{style or 'Video hoài niệm ngắn, clip AI chuyển động, giọng đọc kể chuyện + nhạc nền nhẹ, phụ đề theo lời đọc.'}
</style_reference>

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
- Video là phim ngắn dựng bằng AI: vài clip 8 giây + giọng đọc + nhạc nền + phụ đề; nhân vật và bối cảnh cố định
  trong suốt series để người xem nhận ra "gia đình" của kênh."""


def _compact(videos: list[dict]) -> list[dict]:
    return [{"author": v["author"], "caption": v["caption"][:300], "hashtags": v["hashtags"][:8],
             "music": (v.get("music") or "")[:80], "plays": v["plays"], "likes": v["likes"],
             "comments": v["comments"], "shares": v["shares"], "saves": v["saves"], "duration": v["duration"]}
            for v in videos]


def make_plan(niche: dict, videos: list[dict], recent_titles: list[str], days: int,
              feedback: str | None = None, previous: dict | None = None,
              reference: list[dict] | None = None) -> MonthPlan:
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
    if previous:
        parts.append("Kế hoạch trước (chủ kênh muốn làm lại):\n"
                     f"<previous>\n{json.dumps(previous, ensure_ascii=False)[:20000]}\n</previous>")
    if feedback:
        parts.append(f"Góp ý của chủ kênh, bắt buộc làm theo:\n<feedback>\n{feedback}\n</feedback>")
    parts.append(
        f"Hãy: (1) rút ra pattern hiệu quả; (2) tạo series bible cho một gia đình Việt Nam cố định dùng suốt {days} ngày; "
        f"(3) lên kế hoạch ĐÚNG {days} ngày, mỗi ngày MỘT ký ức tuổi thơ cụ thể khác nhau, sống lại qua chính các "
        "nhân vật này; xoay vòng đều các trụ cột nội dung; mỗi ngày kết bằng một cảm xúc ấm áp khác nhau. "
        "style_en của bible phải mô tả đúng phong cách hình ảnh của kênh mẫu. Viết ngắn gọn."
    )
    log.info("Claude: lập kế hoạch %d ngày (%d video kênh mẫu, %d video tham khảo)…", days, len(ref), len(compact))
    return _parse(system_prompt(niche), "\n\n".join(parts), MonthPlan, effort="medium", max_tokens=32000)


def write_script(niche: dict, bible: dict, idea: dict, patterns: list[str], scenes: int, clip_sec: int,
                 owner_notes: list[str], feedback: str | None = None, previous: dict | None = None) -> SeriesScript:
    parts = [
        f"Viết kịch bản video TikTok ~{scenes * clip_sec - 4}–{scenes * clip_sec} giây cho ngày sau trong series:",
        f"<idea>\n{json.dumps(idea, ensure_ascii=False)}\n</idea>",
        f"Series bible (nhân vật và bối cảnh cố định — dùng đúng tên):\n<bible>\n{json.dumps(bible, ensure_ascii=False)}\n</bible>",
        f"Kịch bản gồm ĐÚNG {scenes} cảnh. Mỗi cảnh là MỘT clip video AI {clip_sec} giây nên lời đọc mỗi cảnh chỉ "
        f"15–19 từ. Các cảnh nối tiếp nhau liền mạch về thời gian và không gian (cùng buổi, cùng nơi, trang phục "
        "không đổi): cảnh 1 = hook gợi nhớ (kiểu 'Ai còn nhớ…'), giữa = sống lại ký ức bằng chi tiết giác quan, "
        "cảnh cuối = cảm xúc đọng lại + câu chạm lòng. "
        "Hành động cuối của cảnh trước nối được với đầu cảnh sau.",
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
    parts.append("Lời văn theo đúng giọng kể của kênh mẫu: tự nhiên như kể chuyện với bạn cùng thế hệ, câu ngắn, "
                 "gợi hình, dễ đọc to. Caption ngắn, gợi nhớ, kết bằng câu hỏi gợi bình luận "
                 "(vd 'Bạn còn nhớ không?').")
    log.info("Claude: viết kịch bản '%s'", idea.get("title"))
    return _parse(system_prompt(niche), "\n\n".join(parts), SeriesScript, effort="high")


# ---------- clip prompts (consistency lives here) ----------

def character_block(bible: dict, names: list[str]) -> str:
    chars = bible.get("characters", [])
    picked = [c for c in chars if c["name"] in names] or chars[:2]
    return "; ".join(f"{c['name']} ({c['role']}): {c['look_en']}" for c in picked)


def clip_prompt(bible: dict, scene: dict, index: int, total: int) -> str:
    parts = [
        "Vertical 9:16 cinematic live-action shot, 8 seconds, part of a short nostalgic film about a Vietnamese "
        "1990s childhood memory.",
        f"Era and place: {bible.get('era', '')}.",
        f"Visual style (identical in every shot): {bible.get('style_en', '').rstrip('.')}.",
        f"Setting (same place in every shot): {bible.get('setting_en', '').rstrip('.')}.",
        f"Characters (exact same faces, hair, clothing and age in every shot): {character_block(bible, scene.get('characters', []))}.",
        f"Action: {scene['action_en'].rstrip('.')}.",
        f"Camera: {scene.get('camera_en', 'slow gentle movement').rstrip('.')}.",
    ]
    if index > 0:
        parts.append("Continuity: continues directly from the previous shot, same moment of the day, same lighting.")
    parts.append("No text, no captions, no subtitles, no logos, no watermark. No dialogue, no lip-sync; soft ambient sound only.")
    return "\n".join(parts)


def character_sheet_prompt(bible: dict) -> str:
    """One-time prompt for a reference image of the cast (used as 'ingredients' in Flow for every clip)."""
    chars = "; ".join(f"{c['name']} ({c['role']}): {c['look_en']}" for c in bible.get("characters", []))
    return ("Character reference sheet, full body, front view, plain light background, soft even light, "
            f"photorealistic, {bible.get('style_en', '').rstrip('.')}. Characters standing side by side: {chars}. "
            "No text.")
