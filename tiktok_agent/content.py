"""Claude-powered trend analysis and script writing."""
from __future__ import annotations

import json
import logging

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
# Server-side fallback: if the model declines, the API reroutes to Anthropic's
# recommended fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Optional callback(usage) called after every Claude request — the worker uses it to track spend.
USAGE_HOOK = None


# ---------- Structured output schemas ----------

class ContentIdea(BaseModel):
    title: str = Field(description="Tên ý tưởng ngắn gọn")
    angle: str = Field(description="Góc kể / cảm xúc chủ đạo")
    hook: str = Field(description="Câu mở đầu 1–2 giây đầu để giữ chân người xem")
    why_it_works: str = Field(description="Dựa trên pattern nào từ dữ liệu đối thủ")


class TrendReport(BaseModel):
    patterns: list[str] = Field(description="Các pattern nội dung/hook/format đang hiệu quả")
    best_duration_sec: int
    top_hashtags: list[str]
    ideas: list[ContentIdea] = Field(description="3 ý tưởng MỚI, không trùng với đối thủ hay video cũ")


class VisualBible(BaseModel):
    characters: str = Field(description="TIẾNG ANH. Mô tả cố định ngoại hình nhân vật chính (tuổi, dáng, tóc, trang phục), "
                                        "người Việt Nam, để mọi shot AI giữ nhất quán")
    setting: str = Field(description="TIẾNG ANH. Bối cảnh cố định (vd: 1990s rural Vietnamese house, tiled roof, wooden table)")
    style: str = Field(description="TIẾNG ANH. Phong cách hình ảnh cố định (ánh sáng, màu, chất phim)")


class Scene(BaseModel):
    narration: str = Field(description="Lời đọc tiếng Việt cho cảnh này (1–3 câu)")
    shots: list[str] = Field(description="1–2 mô tả shot TIẾNG ANH cho video AI, mỗi shot là một hành động/khung hình cụ thể "
                                         "của nhân vật trong bối cảnh (không lặp lại mô tả nhân vật, không chữ trên hình)")
    visual_query: str = Field(description="Từ khoá TIẾNG ANH để tìm video stock trên Pixabay, 2–4 từ, cụ thể, có cảm xúc")


class Script(BaseModel):
    title: str
    hook: str
    visual_bible: VisualBible
    scenes: list[Scene] = Field(description="Các cảnh theo đúng số lượng yêu cầu, cảnh đầu tiên chứa hook")
    caption: str = Field(description="Caption TikTok tiếng Việt, có câu kêu gọi bình luận/chia sẻ")
    hashtags: list[str] = Field(description="5–8 hashtag, không có dấu #")


# ---------- Client helpers ----------

def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _parse(system: str, user: str, schema: type[BaseModel], effort: str = "high",
           max_tokens: int = 16000) -> BaseModel:
    # Streaming avoids HTTP timeouts on long outputs (the 30-day plan); get_final_message() returns the parsed result.
    with _client().beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": effort},
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    ) as stream:
        resp = stream.get_final_message()
    if USAGE_HOOK is not None and getattr(resp, "usage", None) is not None:
        USAGE_HOOK(resp.usage)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude output was truncated (max_tokens)")
    return resp.parsed_output


def _system(niche: dict) -> str:
    return f"""Bạn là chiến lược gia nội dung TikTok cho một kênh tiếng Việt.

Ngách: {niche['name']}
Mô tả: {niche['description']}
Khán giả: {niche['audience']}
Giọng điệu: {niche['tone']}

Nguyên tắc:
- Học từ dữ liệu đối thủ để hiểu VÌ SAO nội dung hiệu quả (chủ đề, cảm xúc, hook, nhịp),
  nhưng luôn tạo câu chuyện và lời văn hoàn toàn mới. Không chép caption hay kịch bản của họ.
- Nội dung chân thật, tích cực; không bịa số liệu, không giả danh người thật, không clickbait sai sự thật.
- Video dạng faceless: giọng đọc AI + video stock + phụ đề."""


# ---------- Public API ----------

def analyze_trends(niche: dict, videos: list[dict], recent_titles: list[str]) -> TrendReport:
    compact = [
        {
            "author": v["author"],
            "caption": v["caption"][:400],
            "hashtags": v["hashtags"][:10],
            "plays": v["plays"], "likes": v["likes"], "comments": v["comments"],
            "shares": v["shares"], "saves": v["saves"],
            "duration": v["duration"], "music": v["music"],
        }
        for v in videos
    ]
    user = (
        "Đây là các video hiệu quả nhất gần đây của các kênh đối thủ (JSON):\n"
        f"<videos>\n{json.dumps(compact, ensure_ascii=False)}\n</videos>\n\n"
        "Các tiêu đề kênh mình đã làm gần đây (tránh trùng lặp):\n"
        f"<recent>\n{json.dumps(recent_titles, ensure_ascii=False)}\n</recent>\n\n"
        "Phân tích pattern và đề xuất 3 ý tưởng video mới cho kênh của mình."
    )
    log.info("Analyzing %d competitor videos with Claude…", len(videos))
    return _parse(_system(niche), user, TrendReport)


def write_script(niche: dict, idea: dict, report: dict | None, target_sec: int,
                 feedback: str | None = None, previous: dict | None = None,
                 scene_count: int | None = None, clip_sec: int = 8) -> Script:
    parts = [
        f"Viết kịch bản video TikTok dài khoảng {target_sec} giây "
        f"(~{int(target_sec * 2.6)} từ lời đọc tổng cộng) cho ý tưởng sau:",
        f"<idea>\n{json.dumps(idea, ensure_ascii=False)}\n</idea>",
    ]
    if scene_count:
        parts.append(
            f"Kịch bản gồm ĐÚNG {scene_count} cảnh. Mỗi cảnh sẽ là MỘT clip video AI dài {clip_sec} giây, nên "
            f"lời đọc mỗi cảnh chỉ 15–19 từ (đọc khoảng {clip_sec - 2}–{clip_sec - 1} giây), tuyệt đối không quá 21 từ. "
            f"Trường shots của mỗi cảnh chỉ có 1 mô tả: một hành động liền mạch, rõ ràng diễn ra trong {clip_sec} giây, "
            "khớp với lời đọc của cảnh. Bốn cảnh nối nhau thành một câu chuyện trọn vẹn: hook → ký ức → nhận ra → thông điệp."
        )
    if report:
        parts.append(
            "Insight từ đối thủ:\n"
            f"<insights>\n{json.dumps(report, ensure_ascii=False)}\n</insights>"
        )
    if previous and feedback:
        parts.append(
            f"Bản trước:\n<previous>\n{json.dumps(previous, ensure_ascii=False)}\n</previous>\n"
            f"Chủ kênh góp ý, hãy sửa theo:\n<feedback>\n{feedback}\n</feedback>"
        )
    elif previous:
        parts.append(
            "Chủ kênh muốn một phiên bản KHÁC hẳn bản dưới (góc kể và hook khác):\n"
            f"<previous>\n{json.dumps(previous, ensure_ascii=False)}\n</previous>"
        )
    parts.append(
        "Yêu cầu: câu đầu là hook mạnh; lời văn tự nhiên như đang kể chuyện với người thân, "
        "câu ngắn dễ đọc to; mỗi cảnh có từ khoá Pixabay tiếng Anh (ngắn, phổ biến, dễ có kết quả) mô tả hình ảnh cụ thể "
        "(ví dụ 'old grandmother hands', 'kids playing rain', 'family dinner table'). "
        "visual_bible mô tả nhân vật và bối cảnh Việt Nam cố định dùng chung cho mọi shot video AI; "
        "các shot trong scenes phải khớp với lời đọc của cảnh đó."
    )
    log.info("Writing script for idea: %s", idea.get("title"))
    return _parse(_system(niche), "\n\n".join(parts), Script)
