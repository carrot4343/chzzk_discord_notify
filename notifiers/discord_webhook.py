"""
디스코드 웹후크(Webhook)로 알림 메시지(Embed 카드)를 전송하는 모듈입니다.
방송인별 개별 웹후크 URL 또는 공통 웹후크 URL을 지원하며,
디스코드에서 깨지는 .ico 포맷 아이콘을 자동으로 방지합니다.
"""

from datetime import datetime, timezone
import re
from typing import Any, Optional
from urllib.parse import urlparse
import requests
import config
from utils.settings_manager import load_settings, log_event

# 플랫폼별 대표 색상 (16진수 색상 코드)
COLOR_CHZZK_GREEN = 0x00FFA3      # 치지직 네온 그린
COLOR_CHZZK_OFFLINE = 0x808080    # 방송 종료 회색
COLOR_NAVER_CAFE = 0x03C75A       # 네이버 초록색
COLOR_NAVER_COMMENT = 0x2DB400    # 네이버 댓글 연초록색
COLOR_TWITTER_X = 0x1DA1F2        # 트위터 파란색

DEFAULT_PNG_ICON = "https://ssl.pstatic.net/static/nng/glive/icon/favicon.png"

# 허용된 디스코드 공식 웹후크 호스트
ALLOWED_DISCORD_HOSTS = {
    "discord.com",
    "discordapp.com",
    "canary.discord.com",
    "ptb.discord.com",
}


def validate_discord_webhook_url(url: Optional[str]) -> bool:
    """
    디스코드 공식 웹후크 URL인지 엄격하게 검증합니다.
    (내부 IP 호출, 악성 사이트 주소 입력 등 SSRF 공격 원천 차단)
    """
    if not url:
        return False
    u = url.strip()
    try:
        parsed = urlparse(u)
        if parsed.scheme.lower() != "https":
            return False
        hostname = (parsed.hostname or "").lower()
        if hostname not in ALLOWED_DISCORD_HOSTS:
            return False
        if not parsed.path.startswith("/api/webhooks/"):
            return False
        return True
    except Exception:
        return False


def _sanitize_icon_url(url: Optional[str]) -> Optional[str]:
    """
    디스코드 Embed 및 웹후크 아바타는 .ico 확장자 이미지를 표시하지 못하고 깨지므로,
    .ico URL이 들어오면 정상 표시되는 .png 아이콘으로 대체합니다.
    """
    if not url:
        return None
    clean = url.strip()
    if not clean:
        return None
    if clean.lower().split("?")[0].endswith(".ico"):
        return DEFAULT_PNG_ICON
    return clean


def resolve_webhook_url(custom_webhook_url: Optional[str] = None) -> str:
    """개별 웹후크 URL이 있으면 우선 사용하고, 없으면 공통 설정의 웹후크 URL을 반환합니다."""
    if custom_webhook_url and custom_webhook_url.strip().startswith("https://"):
        return custom_webhook_url.strip()
    settings = load_settings()
    global_url = (settings.get("global") or {}).get("discord_webhook_url", "").strip()
    if global_url:
        return global_url
    return config.DISCORD_WEBHOOK_URL


def send_discord_notification(
    content: str,
    embed_title: str,
    embed_url: str,
    embed_description: str = "",
    color: int = COLOR_CHZZK_GREEN,
    author_name: Optional[str] = None,
    author_icon_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    image_url: Optional[str] = None,
    fields: Optional[list[dict[str, Any]]] = None,
    footer_text: str = "알림 봇",
    webhook_username: Optional[str] = None,
    webhook_avatar_url: Optional[str] = None,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    디스코드 웹후크 URL로 보기 좋은 카드(Embed) 형태의 알림을 전송합니다.
    성공 시 True, 실패 시 False를 반환합니다.
    """
    target_webhook = resolve_webhook_url(webhook_url)
    if not target_webhook or "여기에_웹후크" in target_webhook:
        log_event("[오류] 디스코드 웹후크 URL이 설정되지 않았습니다!")
        return False

    if not validate_discord_webhook_url(target_webhook):
        log_event(f"[보안 차단] 유효하지 않은 디스코드 웹후크 주소입니다: {target_webhook}")
        return False

    safe_author_icon = _sanitize_icon_url(author_icon_url)
    safe_avatar_icon = _sanitize_icon_url(webhook_avatar_url) or safe_author_icon
    safe_thumb = _sanitize_icon_url(thumbnail_url)

    embed: dict[str, Any] = {
        "title": embed_title,
        "url": embed_url,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": footer_text},
    }

    if embed_description:
        embed["description"] = (
            embed_description[:1900] + "..."
            if len(embed_description) > 1900
            else embed_description
        )

    if author_name:
        author_obj: dict[str, str] = {"name": author_name}
        if safe_author_icon:
            author_obj["icon_url"] = safe_author_icon
        embed["author"] = author_obj

    if safe_thumb:
        embed["thumbnail"] = {"url": safe_thumb}

    if image_url:
        embed["image"] = {"url": image_url}

    if fields:
        embed["fields"] = fields

    payload: dict[str, Any] = {
        "content": content,
        "embeds": [embed],
    }

    if webhook_username:
        payload["username"] = webhook_username
    if safe_avatar_icon:
        payload["avatar_url"] = safe_avatar_icon

    try:
        response = requests.post(target_webhook, json=payload, timeout=10)
        if response.status_code in (200, 204):
            return True
        log_event(
            f"[오류] 디스코드 전송 실패 (HTTP {response.status_code}): {response.text}"
        )
        return False
    except Exception as e:
        log_event(f"[오류] 디스코드 웹후크 요청 중 예외 발생: {e}")
        return False
