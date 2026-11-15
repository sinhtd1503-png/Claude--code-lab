"""Keep video prompts inside Google's generative-AI policies (Flow / Veo / Gemini).

Most refusals for a nostalgic family channel come from a few patterns: exact ages of children, children in
unsafe or intimate situations (bathing, swimming, climbing, fire, punishment), alcohol / tobacco, weapons
(even toys), injuries, real people, brands, named studios or famous cartoon characters, and "photorealistic"
children. `sanitize()` removes or softens those words deterministically before a prompt is shown or sent;
the script writer is also told the rules (RULES_VI) so the story avoids them in the first place.
"""
from __future__ import annotations

import re

RULES_VI = """Quy tắc an toàn cho video AI (Google Flow/Veo/Gemini sẽ TỪ CHỐI nếu vi phạm) — bắt buộc tuân thủ:
- Trẻ em: luôn mặc quần áo đầy đủ, ở nơi an toàn, có người lớn bên cạnh, hoạt động lành mạnh (chơi, học, ăn, giúp
  việc nhà nhẹ). KHÔNG ghi số tuổi cụ thể của trẻ trong mô tả tiếng Anh (dùng 'little girl', 'young boy').
  KHÔNG có cảnh tắm, bơi, ngủ trên giường, thay đồ, cởi trần, chân trần cận cảnh, ôm hôn ngoài ôm/nắm tay gia đình.
- KHÔNG bạo lực hay trừng phạt (đánh đòn, roi vọt, tát, cãi nhau to), không bị thương, không máu, không khóc vì đau.
- KHÔNG hành động nguy hiểm: trèo cây cao, tắm sông, đốt lửa, pháo, điện, dao kéo, leo trèo mái nhà, xe cộ đông.
- KHÔNG rượu bia, thuốc lá, điếu cày, cờ bạc; KHÔNG súng/dao/ná cao su, kể cả đồ chơi.
- KHÔNG người thật, người nổi tiếng, thương hiệu, logo, chữ trên hình, nhân vật hoạt hình có bản quyền, tên hãng
  phim/hoạ sĩ; KHÔNG chính trị, tôn giáo, cờ, khẩu hiệu, quân phục; KHÔNG y tế, bệnh tật.
- Muốn gợi ký ức "bị mắng, bị phạt" thì kể bằng thoại nhẹ nhàng, hài hước, sau đó làm lành — không thể hiện bằng hình."""

_AGE = re.compile(r"\b(\d{1,2})\s*[- ]?\s*(?:years?|yrs?)\s*[- ]?\s*old\b", re.I)
_AGE_VI = re.compile(r"\b(\d{1,2})\s*tuổi\b", re.I)

# (pattern, replacement) — applied case-insensitively on word boundaries
SWAPS: list[tuple[str, str]] = [
    (r"photo-?realistic|hyper-?realistic|live-?action|realistic photo(?:graph)?", "animated"),
    (r"studio ghibli|ghibli|makoto shinkai|hayao miyazaki|miyazaki|disney|pixar|dreamworks", "hand-drawn anime film"),
    (r"doraemon|nobita|pok[eé]mon|pikachu|sailor moon|dragon ?ball|conan|tom and jerry|mickey", "a cute generic cartoon"),
    (r"coca-?cola|pepsi|honda|yamaha|vinamilk|nokia|sony|samsung", "an unbranded"),
    (r"shirtless|topless|bare-?chested", "wearing a simple white tank top"),
    (r"barefoot(?:ed)?|bare feet", "wearing simple sandals"),
    (r"naked|nude|undress(?:ed|ing)?", "fully clothed"),
    (r"bathing|bathes|bath ?tub|taking a bath|shower(?:ing)?", "washing hands at a basin"),
    (r"swimming|swims|bathing in the river|diving", "sitting by the riverbank"),
    (r"kiss(?:es|ing)?", "hug"),
    (r"spank(?:s|ed|ing)?|slap(?:s|ped|ping)?|whip(?:s|ped|ping)?|beat(?:s|ing)? (?:him|her|the child)|"
     r"caning|punish(?:es|ed|ing|ment)?", "gently scold "),
    (r"blood(?:y)?|bleed(?:s|ing)?|wound(?:ed)?|injur(?:y|ed|ies)|bruise(?:d|s)?", "a tiny scrape"),
    (r"cry(?:ing)? in pain|screaming", "sniffling softly"),
    (r"cigarettes?|smoking (?:a )?(?:cigarette|pipe)|tobacco|bamboo (?:water )?pipe|pipe tobacco", "a cup of tea"),
    (r"rice wine|beer|alcohol|liquor|wine|drunk(?:en)?", "green tea"),
    (r"slingshots?|toy guns?|guns?|rifles?|pistols?|knives|knife|weapons?|swords?", "a paper pinwheel"),
    (r"firecrackers?|fireworks?", "red paper lanterns"),
    (r"climbs? (?:up )?(?:a |the )?(?:tall |high )?tree|climbing (?:a |the )?(?:tall |high )?tree", "stands under a tree"),
    (r"gambl(?:e|es|ing)", "playing a board game"),
    (r"in bed|on the bed|sleeping in bed", "on a bamboo mat in the living room"),
]
_SWAPS = [(re.compile(rf"\b(?:{p})", re.I), r) for p, r in SWAPS]


def _age(m: re.Match) -> str:
    return "young" if int(m.group(1)) < 18 else m.group(0)


def sanitize(text: str) -> str:
    """Soften policy-risky wording in an English video prompt (keeps quoted dialogue untouched)."""
    if not text:
        return text
    parts = re.split(r'("[^"]*")', text)  # never rewrite what the characters say
    out = []
    for p in parts:
        if p.startswith('"') and p.endswith('"'):
            out.append(p)
            continue
        p = _AGE.sub(_age, p)
        p = _AGE_VI.sub(lambda m: "" if int(m.group(1)) < 18 else m.group(0), p)
        for rx, rep in _SWAPS:
            p = rx.sub(rep, p)
        out.append(re.sub(r"[ \t]{2,}", " ", p))
    return "".join(out)


POLICY_HINTS = ("policy", "safety", "blocked", "violat", "not allowed", "prohibited", "responsible ai",
                "inappropriate", "sensitive", "minor", "child safety", "filtered")


def policy_like(err: Exception | str) -> bool:
    t = str(err).lower()
    return any(h in t for h in POLICY_HINTS)
