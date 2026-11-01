"""
치지직(Chzzk) 방송 시작/종료, 방제/카테고리 변경, 채널 커뮤니티 글을 감지하는 모듈입니다.
다중 방송인(멀티 채널) 감시 및 채널 URL/이름 자동 분석을 지원합니다.
"""

import re
import threading
import time
from typing import Any, Optional
import requests
import config
from notifiers.discord_webhook import (
    COLOR_CHZZK_GREEN,
    COLOR_CHZZK_OFFLINE,
    send_discord_notification,
)
from utils.settings_manager import log_event

CHZZK_ICON_URL = "https://ssl.pstatic.net/static/nng/glive/icon/favicon.png"

# 동일 순찰 주기 내 여러 사용자가 같은 방송인을 등록했을 때 중복 API 호출을 막는 캐시 (유효시간 15초)
_chzzk_detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 15.0
_MAX_CACHE_ENTRIES = 500
_cache_lock = threading.Lock()


def _prune_cache(cache: dict[str, tuple[float, Any]], ttl: float, max_entries: int = 500) -> None:
    """만료되었거나 최대 크기를 초과한 오래된 캐시 엔트리를 자동 정리합니다."""
    with _cache_lock:
        now = time.time()
        expired = [k for k, (ts, _) in list(cache.items()) if now - ts > ttl]
        for k in expired:
            cache.pop(k, None)
        if len(cache) > max_entries:
            sorted_keys = sorted(list(cache.keys()), key=lambda k: cache.get(k, (0.0, None))[0])
            for k in sorted_keys[: len(cache) - max_entries]:
                cache.pop(k, None)


def _build_mention_prefix(streamer: dict[str, Any], is_stream_open: bool = False) -> str:
    """방송인 설정에 지정된 멘션 접두사(<@&RoleID>, @everyone 등)를 반환합니다 (기본값: 멘션 없음)."""
    mention_role_id = (streamer.get("mention_role_id") or "").strip()
    mention_text = (streamer.get("mention_text") or "").strip()
    if not mention_role_id and not mention_text:
        return ""

    open_only = bool(streamer.get("mention_on_open_only", True))
    if open_only and not is_stream_open:
        return ""  # 뱅온 전용 설정 시 방송 시작이 아니면 멘션하지 않음

    if mention_role_id:
        return f"<@&{mention_role_id}> "
    if mention_text:
        if mention_text.isdigit():
            return f"<@&{mention_text}> "
        return f"{mention_text} "
    return ""


def search_channel_id_by_name(streamer_name: str) -> Optional[str]:
    """방송인 이름으로 치지직 채널 ID(32자리)를 검색합니다."""
    if not streamer_name:
        return None

    url = "https://api.chzzk.naver.com/service/v1/search/channels"
    params = {"keyword": streamer_name, "offset": 0, "size": 5}
    try:
        resp = requests.get(
            url, params=params, headers=config.DEFAULT_HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        items = (data.get("content") or {}).get("data") or []
        if not items:
            return None

        for item in items:
            channel = item.get("channel") or {}
            if channel.get("channelName") == streamer_name:
                return channel.get("channelId")
        first_channel = items[0].get("channel") or {}
        return first_channel.get("channelId")
    except Exception as e:
        log_event(f"[치지직 오류] '{streamer_name}' 채널 검색 실패: {e}")
        return None


def extract_chzzk_channel_info(raw_input: str) -> Optional[dict[str, str]]:
    """
    사용자가 입력한 치지직 채널 주소(URL), 32자리 채널 코드, 또는 방송인 이름에서
    채널 ID, 방송인 이름, 프로필 사진 URL을 자동으로 추출합니다.
    """
    text = (raw_input or "").strip()
    if not text:
        return None

    channel_id: Optional[str] = None

    # 1. URL 또는 텍스트 안에 32자리 16진수 채널 ID가 포함된 경우 추출
    m = re.search(r"([a-fA-F0-9]{32})", text)
    if m:
        channel_id = m.group(1).lower()
    else:
        # 2. 방송인 이름으로 검색
        channel_id = search_channel_id_by_name(text)

    if not channel_id:
        return None

    # 3. 채널 기본 정보 조회 (이름, 프로필 이미지)
    try:
        url = f"https://api.chzzk.naver.com/service/v1/channels/{channel_id}"
        resp = requests.get(url, headers=config.DEFAULT_HEADERS, timeout=10)
        if resp.status_code == 200:
            ch = resp.json().get("content") or {}
            if ch.get("channelId"):
                return {
                    "channel_id": ch.get("channelId"),
                    "name": ch.get("channelName") or text,
                    "profile_image_url": ch.get("channelImageUrl") or CHZZK_ICON_URL,
                }
    except Exception as e:
        log_event(f"[치지직 오류] 채널 상세 조회 실패 ({channel_id}): {e}")

    return {
        "channel_id": channel_id,
        "name": text,
        "profile_image_url": CHZZK_ICON_URL,
    }


def fetch_chzzk_live_detail(channel_id: str) -> Optional[dict[str, Any]]:
    """치지직 채널의 현재 라이브 상세 정보를 가져옵니다 (다중 사용자 중복 호출 방지 캐시 적용)."""
    now = time.time()
    cached = _chzzk_detail_cache.get(channel_id)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    for version in ("v3", "v2"):
        url = f"https://api.chzzk.naver.com/service/{version}/channels/{channel_id}/live-detail"
        try:
            resp = requests.get(url, headers=config.DEFAULT_HEADERS, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content")
                if content is not None:
                    _prune_cache(_chzzk_detail_cache, _CACHE_TTL_SECONDS, _MAX_CACHE_ENTRIES)
                    _chzzk_detail_cache[channel_id] = (now, content)
                    return content
        except Exception as e:
            log_event(f"[치지직 경고] live-detail ({version}) 조회 중 오류: {e}")

    try:
        url = f"https://api.chzzk.naver.com/service/v1/channels/{channel_id}"
        resp = requests.get(url, headers=config.DEFAULT_HEADERS, timeout=10)
        resp.raise_for_status()
        ch = resp.json().get("content") or {}
        fallback = {
            "status": "OPEN" if ch.get("openLive") else "CLOSE",
            "liveId": None,
            "liveTitle": "",
            "liveCategoryValue": "",
            "liveImageUrl": None,
            "channel": ch,
        }
        _prune_cache(_chzzk_detail_cache, _CACHE_TTL_SECONDS, _MAX_CACHE_ENTRIES)
        _chzzk_detail_cache[channel_id] = (now, fallback)
        return fallback
    except Exception as e:
        log_event(f"[치지직 오류] 채널 정보 조회 실패: {e}")
        return None


def check_chzzk_for_streamer(
    streamer: dict[str, Any],
    streamer_state: dict[str, Any],
    global_cfg: dict[str, Any],
) -> bool:
    """
    개별 방송인의 치지직 방송 상태를 확인하고 변화가 있으면 디스코드로 알림을 발송합니다.
    """
    channel_id = (streamer.get("chzzk_channel_id") or "").strip()
    if not channel_id:
        return False

    detail = fetch_chzzk_live_detail(channel_id)
    if not detail:
        return False

    chzzk_state = streamer_state.setdefault("chzzk", {})
    channel_info = detail.get("channel") or {}
    channel_name = (
        channel_info.get("channelName")
        or streamer.get("name")
        or "방송인"
    )
    channel_image = (
        channel_info.get("channelImageUrl")
        or streamer.get("profile_image_url")
        or CHZZK_ICON_URL
    )
    # 방송인 설정에 최신 프로필 사진이 비어 있으면 자동 보완
    if channel_info.get("channelImageUrl"):
        streamer["profile_image_url"] = channel_info["channelImageUrl"]

    webhook_url = streamer.get("custom_webhook_url") or global_cfg.get(
        "discord_webhook_url"
    )

    curr_status = detail.get("status") or "CLOSE"
    curr_live_id = detail.get("liveId")
    curr_title = (detail.get("liveTitle") or "").strip()
    curr_category = (detail.get("liveCategoryValue") or "카테고리 없음").strip()

    raw_thumb = detail.get("liveImageUrl") or ""
    thumbnail_url = None
    if raw_thumb:
        thumbnail_url = raw_thumb.replace("{type}", "1080") + f"?t={int(time.time())}"

    live_url = f"https://chzzk.naver.com/live/{channel_id}"

    # 1. 최초 실행 시 현재 상태만 조용히 기록
    if not chzzk_state.get("initialized", False):
        chzzk_state["initialized"] = True
        chzzk_state["status"] = curr_status
        chzzk_state["live_id"] = curr_live_id
        chzzk_state["title"] = curr_title
        chzzk_state["category"] = curr_category
        log_event(
            f"[치지직 초기화] {channel_name} | 상태: {curr_status} | 방제: '{curr_title}'"
        )
        # 커뮤니티 최신 글 번호도 초기 기록
        if streamer.get("notify_chzzk_community", True):
            _check_chzzk_community(
                chzzk_state,
                channel_id,
                channel_name,
                channel_image,
                webhook_url,
                global_cfg,
                init_only=True,
                streamer=streamer,
            )
        return True

    prev_status = chzzk_state.get("status", "CLOSE")
    prev_live_id = chzzk_state.get("live_id")
    prev_title = chzzk_state.get("title", "")
    prev_category = chzzk_state.get("category", "")

    state_changed = False

    # 2. 방송 시작 감지
    is_new_stream = (prev_status != "OPEN" and curr_status == "OPEN") or (
        curr_status == "OPEN"
        and curr_live_id is not None
        and prev_live_id is not None
        and curr_live_id != prev_live_id
    )

    if is_new_stream:
        log_event(f"[치지직 감지] 🔴 {channel_name} 방송 시작! ({curr_title})")
        if streamer.get("notify_chzzk_open", True):
            mention_prefix = _build_mention_prefix(streamer, is_stream_open=True)
            send_discord_notification(
                content=f"{mention_prefix}🔴 **{channel_name}** 치지직 방송이 시작되었습니다!",
                embed_title=curr_title or f"{channel_name} 라이브 방송",
                embed_url=live_url,
                embed_description=f"[👉 여기를 눌러 방송 보러가기]({live_url})",
                color=COLOR_CHZZK_GREEN,
                author_name=f"{channel_name} (치지직)",
                author_icon_url=channel_image,
                image_url=thumbnail_url,
                fields=[
                    {"name": "카테고리", "value": curr_category or "없음", "inline": True},
                    {"name": "상태", "value": "🔴 LIVE", "inline": True},
                ],
                footer_text="치지직 방송 알림",
                webhook_username=f"{channel_name} 치지직 알림",
                webhook_avatar_url=channel_image,
                webhook_url=webhook_url,
            )
        state_changed = True

    # 3. 방송 중 방제 또는 카테고리 변경 감지
    elif curr_status == "OPEN":
        if curr_title and curr_title != prev_title:
            log_event(f"[치지직 감지] 📝 {channel_name} 방제 변경: '{prev_title}' -> '{curr_title}'")
            if streamer.get("notify_chzzk_title", True):
                mention_prefix = _build_mention_prefix(streamer, is_stream_open=False)
                send_discord_notification(
                    content=f"{mention_prefix}📝 **{channel_name}** 방송 제목이 변경되었습니다!",
                    embed_title=curr_title,
                    embed_url=live_url,
                    color=COLOR_CHZZK_GREEN,
                    author_name=f"{channel_name} (치지직)",
                    author_icon_url=channel_image,
                    thumbnail_url=channel_image,
                    fields=[
                        {"name": "이전 방제", "value": prev_title or "없음", "inline": False},
                        {"name": "새 방제", "value": curr_title, "inline": False},
                        {"name": "카테고리", "value": curr_category or "없음", "inline": True},
                    ],
                    footer_text="치지직 방제 변경 알림",
                    webhook_username=f"{channel_name} 치지직 알림",
                    webhook_avatar_url=channel_image,
                    webhook_url=webhook_url,
                )
            state_changed = True

        if curr_category and curr_category != prev_category:
            log_event(
                f"[치지직 감지] 🎮 {channel_name} 카테고리 변경: '{prev_category}' -> '{curr_category}'"
            )
            if streamer.get("notify_chzzk_category", True):
                mention_prefix = _build_mention_prefix(streamer, is_stream_open=False)
                send_discord_notification(
                    content=f"{mention_prefix}🎮 **{channel_name}** 방송 카테고리가 변경되었습니다!",
                    embed_title=curr_title or f"{channel_name} 라이브 방송",
                    embed_url=live_url,
                    color=COLOR_CHZZK_GREEN,
                    author_name=f"{channel_name} (치지직)",
                    author_icon_url=channel_image,
                    thumbnail_url=channel_image,
                    fields=[
                        {"name": "이전 카테고리", "value": prev_category or "없음", "inline": True},
                        {"name": "새 카테고리", "value": curr_category, "inline": True},
                    ],
                    footer_text="치지직 카테고리 변경 알림",
                    webhook_username=f"{channel_name} 치지직 알림",
                    webhook_avatar_url=channel_image,
                    webhook_url=webhook_url,
                )
            state_changed = True

    # 4. 방송 종료 감지
    elif prev_status == "OPEN" and curr_status == "CLOSE":
        log_event(f"[치지직 감지] ⚫ {channel_name} 방송 종료")
        if streamer.get("notify_chzzk_close", False):
            mention_prefix = _build_mention_prefix(streamer, is_stream_open=False)
            send_discord_notification(
                content=f"{mention_prefix}⚫ **{channel_name}** 치지직 방송이 종료되었습니다.",
                embed_title=prev_title or f"{channel_name} 방송 종료",
                embed_url=f"https://chzzk.naver.com/{channel_id}",
                color=COLOR_CHZZK_OFFLINE,
                author_name=f"{channel_name} (치지직)",
                author_icon_url=channel_image,
                thumbnail_url=channel_image,
                footer_text="치지직 방송 종료 알림",
                webhook_username=f"{channel_name} 치지직 알림",
                webhook_avatar_url=channel_image,
                webhook_url=webhook_url,
            )
        state_changed = True

    if (
        chzzk_state.get("status") != curr_status
        or chzzk_state.get("live_id") != curr_live_id
        or chzzk_state.get("title") != curr_title
        or chzzk_state.get("category") != curr_category
    ):
        chzzk_state["status"] = curr_status
        chzzk_state["live_id"] = curr_live_id
        chzzk_state["title"] = curr_title
        chzzk_state["category"] = curr_category
        state_changed = True

    # 5. 치지직 채널 커뮤니티 공지글 감지
    if streamer.get("notify_chzzk_community", True):
        if _check_chzzk_community(
            chzzk_state,
            channel_id,
            channel_name,
            channel_image,
            webhook_url,
            global_cfg,
            init_only=False,
            streamer=streamer,
        ):
            state_changed = True

    return state_changed


def _check_chzzk_community(
    chzzk_state: dict[str, Any],
    channel_id: str,
    channel_name: str,
    channel_image: str,
    webhook_url: Optional[str],
    global_cfg: dict[str, Any],
    init_only: bool = False,
    streamer: Optional[dict[str, Any]] = None,
) -> bool:
    """치지직 채널 커뮤니티에 방송인이 올린 새 게시글이 있는지 확인합니다."""
    url = (
        f"https://apis.naver.com/nng_main/nng_comment_api/v1/type/CHANNEL_POST/"
        f"id/{channel_id}/comments"
    )
    params = {
        "limit": 5,
        "offset": 0,
        "orderType": "DESC",
        "pagingType": "PAGE",
    }
    headers = {
        **config.DEFAULT_HEADERS,
        "Referer": f"https://chzzk.naver.com/{channel_id}/community",
    }
    cookies = {}
    nid_aut = (global_cfg.get("naver_nid_aut") or "").strip()
    nid_ses = (global_cfg.get("naver_nid_ses") or "").strip()
    if nid_aut and nid_ses:
        cookies["NID_AUT"] = nid_aut
        cookies["NID_SES"] = nid_ses

    try:
        resp = requests.get(
            url, params=params, headers=headers, cookies=cookies, timeout=10
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        items = ((data.get("content") or {}).get("comments") or {}).get("data") or []
        if not items:
            return False

        streamer_posts = []
        for item in items:
            comment = item.get("comment") or {}
            user = item.get("user") or {}
            if (
                comment.get("commentType") == "CHANNEL_OWNER"
                or user.get("userIdHash") == channel_id
            ):
                streamer_posts.append(comment)
        if not streamer_posts:
            streamer_posts = [
                item.get("comment") or {} for item in items if item.get("comment")
            ]

        if not streamer_posts:
            return False

        max_post_id = max(int(p.get("commentId") or 0) for p in streamer_posts)
        last_comm_id = chzzk_state.get("last_community_id")

        if last_comm_id is None or init_only:
            chzzk_state["last_community_id"] = max_post_id
            return True

        new_posts = [
            p
            for p in streamer_posts
            if int(p.get("commentId") or 0) > int(last_comm_id)
        ]
        new_posts.sort(key=lambda x: int(x.get("commentId") or 0))

        changed = False
        for post in new_posts:
            pid = int(post.get("commentId") or 0)
            content_text = (
                post.get("content") or "새 커뮤니티 글이 등록되었습니다."
            ).strip()
            post_url = f"https://chzzk.naver.com/{channel_id}/community/detail/{pid}"
            log_event(f"[치지직 커뮤니티 감지] 📢 {channel_name} 새 글: {content_text[:30]}...")

            mention_prefix = _build_mention_prefix(streamer or {}, is_stream_open=False)
            send_discord_notification(
                content=f"{mention_prefix}📢 **{channel_name}** 치지직 커뮤니티에 새 글이 올라왔습니다!",
                embed_title=f"{channel_name} 치지직 커뮤니티 게시글",
                embed_url=post_url,
                embed_description=content_text,
                color=COLOR_CHZZK_GREEN,
                author_name=f"{channel_name} (치지직 커뮤니티)",
                author_icon_url=channel_image,
                thumbnail_url=channel_image,
                footer_text="치지직 커뮤니티 알림",
                webhook_username=f"{channel_name} 치지직 알림",
                webhook_avatar_url=channel_image,
                webhook_url=webhook_url,
            )
            chzzk_state["last_community_id"] = pid
            changed = True

        return changed
    except Exception:
        return False
