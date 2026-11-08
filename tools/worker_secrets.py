"""Собирает секреты для Cloudflare Worker из сейфа (вызывается в .github/workflows/worker.yml).

Пишет JSON в файл (не в лог: логи Actions публичные): TELEGRAM_BOT_TOKEN, PERSONA, CHAT_ID.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
from run import Vault  # noqa: E402

v = Vault(os.environ["CREW_BOT_CODE"])
if not v.ok:
    sys.exit("сейф не открылся")
out = {
    "TELEGRAM_BOT_TOKEN": os.environ["TELEGRAM_BOT_TOKEN"],
    "PERSONA": v.data.get("persona") or "",
    "CHAT_ID": str((v.stats.get("bot") or {}).get("chat_id") or ""),
}
Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False))
print("secrets ready:", ", ".join(k for k, val in out.items() if val))
