"""CLI entry point.

  python -m tiktok_agent.main bot            # chạy bot Telegram + lịch hằng ngày (chế độ chính)
  python -m tiktok_agent.main run-once       # chạy pipeline 1 lần, không cần Telegram (để test)
  python -m tiktok_agent.main research       # chỉ quét + phân tích đối thủ, in báo cáo
  python -m tiktok_agent.main tiktok-auth    # kết nối tài khoản TikTok (OAuth)
"""
from __future__ import annotations

import argparse
import json
import logging
import urllib.parse

from .config import load_settings
from .pipeline import Pipeline


def main() -> None:
    ap = argparse.ArgumentParser(prog="tiktok_agent")
    ap.add_argument("command", choices=["bot", "run-once", "research", "tiktok-auth"])
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = Pipeline(load_settings(args.config))

    if args.command == "bot":
        from .telegram_bot import ReviewBot
        ReviewBot(p).run()
    elif args.command == "run-once":
        for cid in p.daily():
            item = p.db.get_content(cid)
            print(f"#{cid} {item['script']['title']}\n  video: {item['video_path']}\n  caption: {item['caption']}\n")
    elif args.command == "research":
        print(json.dumps(p.research(), ensure_ascii=False, indent=2))
    elif args.command == "tiktok-auth":
        if not p.publisher:
            raise SystemExit("Thiếu TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET trong .env")
        print("1) Mở link sau, đăng nhập và cho phép app:\n\n" + p.publisher.authorize_url() + "\n")
        url = input("2) Dán URL trình duyệt chuyển tới (chứa ?code=...): ").strip()
        code = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("code", [url])[0]
        tok = p.publisher.exchange_code(code)
        print(f"✅ Đã kết nối TikTok (open_id={tok.get('open_id')}, scope={tok.get('scope')})")


if __name__ == "__main__":
    main()
