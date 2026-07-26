import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cloudflare_example_is_synthetic_and_parseable():
    path = ROOT / "cloudflare" / "telegram-gateway" / "wrangler.jsonc.example"
    text = path.read_text(encoding="utf-8")
    value = json.loads(text)

    assert "Synthetic Owner" not in text
    assert "123456789" not in text
    assert value["vars"] == {
        "GITHUB_REPOSITORY": "OWNER/REPOSITORY",
        "TELEGRAM_ACTOR_ID": "YOUR_TELEGRAM_USER_ID",
        "TELEGRAM_CHAT_ID": "YOUR_TELEGRAM_CHAT_ID",
    }


def test_real_cloudflare_config_is_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "cloudflare/telegram-gateway/wrangler.jsonc" in ignored
