"""
X (구 트위터) 새 포스트, 답글(Reply), 리트윗(Retweet) 감지 모듈입니다.
방송인별 통합 관리(치지직/카페 방송인에 X 추가 연동 또는 X 단독 방송인 등록)를 지원하며,
X 부계정 인증 쿠키(auth_token, ct0) 기반 GraphQL API 호출 및 봇 탐지 방지용 최소 폴링 주기(3분)를 적용합니다.
"""

import html
import json
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import unquote
import requests

import config
from notifiers.discord_webhook import (
    COLOR_TWITTER_X,
    DEFAULT_PNG_ICON,
    send_discord_notification,
)
from utils.settings_manager import load_settings, log_event

# X 웹 클라이언트 공용 Bearer 토큰 (공식 웹앱 번들 표준값)
_X_PUBLIC_BEARER_TOKEN = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL 기본 Query ID 목록 (X 웹 번들 기준 + 폴백 목록)
_USER_BY_SCREEN_NAME_QUERY_IDS = [
    "KybxDj9RrADIITXlGG8kpw",
    "xmU6X_CKVnQ5lSrCbAmJsg",
    "NimuplG1OB7Fd2btCLdBOw",
    "sLVLhk0bGj3MVFEKTdax1w",
    "G3KGOASz96M-Qu0nwmGXNg",
]

_USER_TWEETS_QUERY_IDS = [
    "E3opETHurmVJflFsUBVuUQ",
    "V7H0Ap3_Hh2FyS75OCDO3Q",
    "QWF3SzpHmykQHsQMixG0cg",
    "HuTx74BxAnezK1gWvYY7zg",
]

_USER_TWEETS_AND_REPLIES_QUERY_IDS = [
    "bt4TKuFz4T7Ckk-VvQVSow",
    "Q6aAvPw7cXZbq2qznbfr6A",
    "_P1zJA2kS9W1PLHKdThsrg",
]

_USER_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

_TWEET_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
}

_RESERVED_X_PATHS = {
    "home",
    "explore",
    "notifications",
    "messages",
    "i",
    "settings",
    "search",
    "login",
    "logout",
    "compose",
    "intent",
    "tos",
    "privacy",
    "about",
}

# 캐시 및 쿨다운 관리
_cache_lock = threading.Lock()
_MAX_CACHE_ENTRIES = 500
_FORCE_CACHE_TTL_SECONDS = 15.0
# key: f"{user_id}:{include_replies}" -> (timestamp, tweets_list)
_tweets_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
# key: screen_name.lower() -> (timestamp, user_info_dict)
_user_info_cache: dict[str, tuple[float, dict[str, str]]] = {}

# X 쿠키 만료 알림 쿨다운 (웹후크당 24시간 1회)
_x_cookie_alert_timestamps: dict[str, float] = {}
_COOKIE_ALERT_COOLDOWN = 86400.0

# 동적 발견된 queryId 캐시
_discovered_query_ids: dict[str, str] = {}
_last_query_id_discovery_ts: float = 0.0


class XCookieExpiredError(Exception):
    """X(트위터) 로그인 쿠키(auth_token / ct0)가 만료되었거나 유효하지 않을 때 발생하는 예외입니다."""
    pass


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


def _handle_x_cookie_expired_alert(webhook_url: Optional[str], streamer_name: str) -> None:
    """X 로그인 쿠키 만료 또는 미설정 시 관리자/서버 웹후크로 경고 카드를 발송합니다 (24시간당 1회)."""
    if not webhook_url:
        return
    now = time.time()
    last_sent = _x_cookie_alert_timestamps.get(webhook_url, 0.0)
    if now - last_sent < _COOKIE_ALERT_COOLDOWN:
        return

    _x_cookie_alert_timestamps[webhook_url] = now
    log_event(
        f"[X 알림 경고] '{streamer_name}' X(트위터) 수집 중 쿠키 만료/오류 감지 -> 디스코드 경고 전송"
    )
    send_discord_notification(
        content="⚠️ **[X(구 트위터) 로그인 쿠키 만료 경고]** X 알림 수집을 위한 인증 쿠키가 만료되었거나 유효하지 않습니다.",
        embed_title="🔑 X(트위터) 부계정 쿠키 갱신 필요",
        embed_url="https://x.com/login",
        embed_description=(
            f"**{streamer_name}** 님의 X(구 트위터) 새 포스트를 확인하던 중 인증 세션(`auth_token`, `ct0`)이 만료되었거나 유효하지 않음이 확인되었습니다.\n\n"
            "• X 알림 수집을 계속하려면 부계정으로 X에 로그인한 뒤 최신 쿠키를 등록해 주세요.\n"
            "• 웹 대시보드 상단 설정 또는 디스코드 `/x쿠키설정` 명령어를 통해 `auth_token`과 `ct0` 값을 갱신할 수 있습니다."
        ),
        color=0xF59E0B,
        footer_text="X(구 트위터) 쿠키 상태 경고",
        webhook_username="X 상태 알리미",
        webhook_url=webhook_url,
    )


def _build_x_cookies(
    x_auth_token: Optional[str] = None,
    x_ct0: Optional[str] = None,
) -> dict[str, str]:
    """전달받은 쿠키 또는 settings.json / .env 의 X 로그인 쿠키(auth_token, ct0)를 반환합니다."""
    if x_auth_token and x_ct0:
        return {"auth_token": x_auth_token.strip(), "ct0": x_ct0.strip()}

    settings = load_settings()
    g_cfg = settings.get("global") or {}
    auth_token = (x_auth_token or g_cfg.get("x_auth_token") or config.X_AUTH_TOKEN or "").strip()
    ct0 = (x_ct0 or g_cfg.get("x_ct0") or config.X_CT0 or "").strip()
    if auth_token and ct0:
        return {"auth_token": auth_token, "ct0": ct0}
    return {}


def _build_x_headers(ct0: str) -> dict[str, str]:
    """X 내부 GraphQL API 호출을 위한 표준 인증 헤더를 생성합니다."""
    return {
        **config.DEFAULT_HEADERS,
        "Authorization": _X_PUBLIC_BEARER_TOKEN,
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "ko",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
        "Accept": "*/*",
    }


def parse_x_screen_name(raw_input: str) -> Optional[str]:
    """
    사용자 입력(@핸들, 핸들, https://x.com/핸들, https://twitter.com/핸들/status/...)에서
    순수 X 아이디(screen_name)를 추출합니다.
    """
    if not raw_input:
        return None
    text = unquote(raw_input.strip())
    if not text:
        return None

    # URL 형태인 경우
    m_url = re.search(
        r"(?:https?://)?(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?:[/?#]|$)",
        text,
        re.IGNORECASE,
    )
    if m_url:
        candidate = m_url.group(1)
        if candidate.lower() not in _RESERVED_X_PATHS:
            return candidate
        return None

    # @핸들 또는 순수 핸들 형태인 경우
    if text.startswith("@"):
        text = text[1:].strip()

    if re.match(r"^[A-Za-z0-9_]{1,15}$", text):
        if text.lower() not in _RESERVED_X_PATHS:
            return text

    return None


def _discover_x_query_ids() -> None:
    """X 웹 번들 스크립트에서 최신 GraphQL queryId를 동적으로 탐색하여 캐싱합니다 (폴백용)."""
    global _last_query_id_discovery_ts
    now = time.time()
    if now - _last_query_id_discovery_ts < 600:
        return
    _last_query_id_discovery_ts = now

    try:
        resp = requests.get("https://x.com", headers=config.DEFAULT_HEADERS, timeout=10)
        if resp.status_code != 200:
            return
        bundle_urls = re.findall(
            r"https://abs\.twimg\.com/responsive-web/client-web/main\.[a-zA-Z0-9]+\.js",
            resp.text,
        )
        if not bundle_urls:
            return
        js_resp = requests.get(bundle_urls[0], headers=config.DEFAULT_HEADERS, timeout=10)
        if js_resp.status_code != 200:
            return
        for op_name in ("UserByScreenName", "UserTweets", "UserTweetsAndReplies"):
            m = re.search(
                rf'queryId:"([^"]+)",operationName:"{op_name}"',
                js_resp.text,
            )
            if m:
                _discovered_query_ids[op_name] = m.group(1)
    except Exception:
        pass


def _get_query_id_candidates(operation_name: str, defaults: list[str]) -> list[str]:
    """동적 탐색된 queryId를 최우선으로 포함한 후보 리스트를 반환합니다."""
    discovered = _discovered_query_ids.get(operation_name)
    if discovered:
        return [discovered] + [q for q in defaults if q != discovered]
    return list(defaults)


def _upgrade_profile_image_url(img_url: str) -> str:
    """X 프로필 이미지 URL의 _normal 접미사를 고화질(_400x400)로 변환합니다."""
    if not img_url:
        return ""
    return re.sub(r"_normal(\.[a-zA-Z0-9]+)$", r"_400x400\1", img_url)


def _extract_user_fields_from_graphql_result(user_res: dict[str, Any]) -> Optional[dict[str, str]]:
    """GraphQL User 결과 객체에서 user_id, screen_name, name, profile_image_url을 추출합니다."""
    if not isinstance(user_res, dict):
        return None
    rest_id = str(user_res.get("rest_id") or "").strip()
    if not rest_id:
        return None

    legacy = user_res.get("legacy") or {}
    core = user_res.get("core") or {}
    avatar = user_res.get("avatar") or {}

    screen_name = str(
        legacy.get("screen_name") or core.get("screen_name") or ""
    ).strip()
    name = str(
        legacy.get("name") or core.get("name") or screen_name
    ).strip()
    img_url = str(
        legacy.get("profile_image_url_https") or avatar.get("image_url") or ""
    ).strip()
    img_url = _upgrade_profile_image_url(img_url)

    if not screen_name:
        return None

    return {
        "user_id": rest_id,
        "screen_name": screen_name,
        "name": name or screen_name,
        "profile_image_url": img_url,
    }


def extract_x_user_info(
    raw_input: str,
    x_auth_token: Optional[str] = None,
    x_ct0: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """
    X 핸들 또는 프로필/포스트 URL에서 계정 정보를 조회하여
    {"user_id": ..., "screen_name": ..., "name": ..., "profile_image_url": ...} 를 반환합니다.
    쿠키가 만료되었거나 유효하지 않으면 XCookieExpiredError를 발생시킵니다.
    """
    screen_name = parse_x_screen_name(raw_input)
    if not screen_name:
        return None

    now = time.time()
    cache_key = screen_name.lower()
    cached = _user_info_cache.get(cache_key)
    if cached and (now - cached[0]) < 60.0:
        return cached[1]

    cookies = _build_x_cookies(x_auth_token, x_ct0)
    if not cookies:
        return None

    headers = _build_x_headers(cookies["ct0"])
    import json

    variables = json.dumps(
        {"screen_name": screen_name, "withSafetyModeUserFields": True},
        separators=(",", ":"),
    )
    features = json.dumps(_USER_FEATURES, separators=(",", ":"))
    field_toggles = json.dumps(
        {"withAuxiliaryUserLabels": False}, separators=(",", ":")
    )

    candidates = _get_query_id_candidates("UserByScreenName", _USER_BY_SCREEN_NAME_QUERY_IDS)

    for attempt in range(2):
        for qid in candidates:
            url = f"https://x.com/i/api/graphql/{qid}/UserByScreenName"
            try:
                resp = requests.get(
                    url,
                    params={
                        "variables": variables,
                        "features": features,
                        "fieldToggles": field_toggles,
                    },
                    headers=headers,
                    cookies=cookies,
                    timeout=10,
                )
                if resp.status_code in (401, 403):
                    raise XCookieExpiredError(f"HTTP {resp.status_code}")

                if resp.status_code == 200:
                    data = resp.json()
                    errors = data.get("errors") or []
                    for err in errors:
                        if err.get("code") in (32, 89, 215, 326):
                            raise XCookieExpiredError(str(err.get("message") or "Auth Error"))

                    user_res = (
                        ((data.get("data") or {}).get("user") or {}).get("result") or {}
                    )
                    info = _extract_user_fields_from_graphql_result(user_res)
                    if info:
                        _prune_cache(_user_info_cache, 300.0, _MAX_CACHE_ENTRIES)
                        _user_info_cache[cache_key] = (now, info)
                        return info
            except XCookieExpiredError:
                raise
            except Exception as e:
                log_event(f"[X 조회 경고] UserByScreenName({qid}) 호출 오류: {e}")

        if attempt == 0:
            _discover_x_query_ids()
            candidates = _get_query_id_candidates("UserByScreenName", _USER_BY_SCREEN_NAME_QUERY_IDS)

    return None


def _clean_tweet_text(raw_text: str, entities: Optional[dict[str, Any]] = None) -> str:
    """트윗 본문의 HTML 엔티티를 복원하고 t.co 단축 링크를 원본 URL로 치환하거나 미디어 링크를 정리합니다."""
    if not raw_text:
        return ""
    text = html.unescape(raw_text)

    if isinstance(entities, dict):
        # 일반 URL을 실제 주소로 치환
        for url_obj in entities.get("urls") or []:
            short_url = url_obj.get("url")
            expanded = url_obj.get("expanded_url") or url_obj.get("display_url")
            if short_url and expanded:
                text = text.replace(short_url, expanded)
        # 첨부 미디어 자체 t.co 링크는 본문 끝에서 제거
        for media_obj in entities.get("media") or []:
            media_short = media_obj.get("url")
            if media_short:
                text = text.replace(media_short, "")

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _extract_single_tweet_node(
    tweet_res: dict[str, Any],
    target_user_id: str,
    fallback_screen_name: str,
) -> Optional[dict[str, Any]]:
    """GraphQL tweet_results.result 단일 노드에서 포스트 정보를 정규화하여 추출합니다."""
    if not isinstance(tweet_res, dict):
        return None

    typename = tweet_res.get("__typename")
    if typename == "TweetWithVisibilityResults":
        tweet_res = tweet_res.get("tweet") or {}
    elif typename in ("TweetTombstone", "TweetUnavailable"):
        return None

    rest_id = str(tweet_res.get("rest_id") or "").strip()
    if not rest_id or not rest_id.isdigit():
        return None

    legacy = tweet_res.get("legacy") or {}
    if not legacy:
        return None

    # 작성자 확인 (타인의 트윗이 대화 스레드에 섞여 있을 경우 본인이 작성한 트윗만 필터링)
    core_user = (
        ((tweet_res.get("core") or {}).get("user_results") or {}).get("result") or {}
    )
    author_id = str(
        legacy.get("user_id_str") or core_user.get("rest_id") or ""
    ).strip()
    if target_user_id and author_id and author_id != str(target_user_id).strip():
        return None

    user_info = _extract_user_fields_from_graphql_result(core_user) or {}
    screen_name = user_info.get("screen_name") or fallback_screen_name
    author_name = user_info.get("name") or screen_name
    avatar_url = user_info.get("profile_image_url") or ""

    # 장문(note_tweet) 우선 확인
    note_text = (
        ((tweet_res.get("note_tweet") or {}).get("note_tweet_results") or {})
        .get("result", {})
        .get("text")
    )
    raw_full_text = note_text or legacy.get("full_text") or legacy.get("text") or ""

    # 리트윗 여부 판별
    rt_res = (legacy.get("retweeted_status_result") or {}).get("result") or {}
    if rt_res.get("__typename") == "TweetWithVisibilityResults":
        rt_res = rt_res.get("tweet") or {}

    is_retweet = bool(rt_res) or raw_full_text.startswith("RT @")
    in_reply_to_status_id = str(legacy.get("in_reply_to_status_id_str") or "").strip()
    in_reply_to_user_id = str(legacy.get("in_reply_to_user_id_str") or "").strip()
    in_reply_to_screen_name = str(legacy.get("in_reply_to_screen_name") or "").strip()
    is_reply = (not is_retweet) and bool(in_reply_to_status_id or in_reply_to_user_id)

    if is_retweet:
        tweet_type = "retweet"
    elif is_reply:
        tweet_type = "reply"
    else:
        tweet_type = "post"

    # 리트윗인 경우 원문 텍스트 및 미디어 추출 보강
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    if is_retweet and rt_res:
        rt_legacy = rt_res.get("legacy") or {}
        rt_user = (
            ((rt_res.get("core") or {}).get("user_results") or {}).get("result") or {}
        )
        rt_user_info = _extract_user_fields_from_graphql_result(rt_user) or {}
        rt_screen_name = rt_user_info.get("screen_name") or ""
        rt_note_text = (
            ((rt_res.get("note_tweet") or {}).get("note_tweet_results") or {})
            .get("result", {})
            .get("text")
        )
        rt_raw_text = rt_note_text or rt_legacy.get("full_text") or ""
        rt_entities = rt_legacy.get("extended_entities") or rt_legacy.get("entities") or entities
        cleaned_rt = _clean_tweet_text(rt_raw_text, rt_entities)
        if cleaned_rt:
            prefix = f"🔁 **@{rt_screen_name}** 님의 포스트 리트윗:\n" if rt_screen_name else "🔁 리트윗:\n"
            cleaned_text = f"{prefix}{cleaned_rt}"
        else:
            cleaned_text = _clean_tweet_text(raw_full_text, entities)
        entities = rt_entities
    else:
        cleaned_text = _clean_tweet_text(raw_full_text, entities)

    # 첫 번째 첨부 이미지 또는 영상 썸네일 추출
    media_url = ""
    media_list = entities.get("media") or []
    if not media_list and legacy.get("entities"):
        media_list = (legacy.get("entities") or {}).get("media") or []
    if media_list and isinstance(media_list[0], dict):
        media_url = str(media_list[0].get("media_url_https") or "").strip()

    return {
        "id": int(rest_id),
        "id_str": rest_id,
        "type": tweet_type,  # "post" | "reply" | "retweet"
        "text": cleaned_text or "(미디어 포스트)",
        "media_url": media_url,
        "screen_name": screen_name,
        "author_name": author_name,
        "profile_image_url": avatar_url,
        "in_reply_to_screen_name": in_reply_to_screen_name,
        "url": f"https://x.com/{screen_name}/status/{rest_id}",
    }


def _parse_timeline_instructions(
    data: dict[str, Any],
    target_user_id: str,
    fallback_screen_name: str,
) -> list[dict[str, Any]]:
    """GraphQL 타임라인 응답의 instructions 배열에서 트윗 목록을 추출합니다 (고정 트윗 및 광고 제외)."""
    user_result = ((data.get("data") or {}).get("user") or {}).get("result") or {}
    timeline_obj = (
        (user_result.get("timeline_v2") or user_result.get("timeline") or {}).get("timeline")
        or {}
    )
    instructions = timeline_obj.get("instructions") or []

    collected_by_id: dict[int, dict[str, Any]] = {}

    for inst in instructions:
        if not isinstance(inst, dict):
            continue
        # TimelinePinEntry(고정된 옛날 트윗)는 건너뛰고 TimelineAddEntries만 처리
        inst_type = inst.get("type")
        if inst_type != "TimelineAddEntries":
            continue

        entries = inst.get("entries") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("entryId") or "")
            # 광고(promoted), 추천 팔로우(who-to-follow), 커서(cursor) 제외
            if not (
                entry_id.startswith("tweet-")
                or entry_id.startswith("profile-conversation-")
            ):
                continue

            content = entry.get("content") or {}
            # 단일 트윗 엔트리
            item_content = content.get("itemContent") or {}
            if item_content:
                tweet_res = (item_content.get("tweet_results") or {}).get("result") or {}
                parsed = _extract_single_tweet_node(
                    tweet_res, target_user_id, fallback_screen_name
                )
                if parsed:
                    collected_by_id[parsed["id"]] = parsed

            # 대화 모듈 엔트리 (profile-conversation-*)
            for item_wrapper in content.get("items") or []:
                sub_item = (item_wrapper.get("item") or {}).get("itemContent") or {}
                tweet_res = (sub_item.get("tweet_results") or {}).get("result") or {}
                parsed = _extract_single_tweet_node(
                    tweet_res, target_user_id, fallback_screen_name
                )
                if parsed:
                    collected_by_id[parsed["id"]] = parsed

    result_list = list(collected_by_id.values())
    result_list.sort(key=lambda x: x["id"], reverse=True)
    return result_list


def fetch_x_user_tweets(
    user_id: str,
    screen_name: str,
    cookies: Optional[dict[str, str]] = None,
    include_replies: bool = False,
    force: bool = False,
) -> list[dict[str, Any]]:
    """
    특정 X 사용자(user_id)의 최신 포스트 목록을 조회합니다.
    봇 탐지 및 Rate-Limit 방지를 위해 동일 계정은 최소 3분(180초) 간격으로만 호출합니다 (force=True 시 15초).
    """
    uid = str(user_id or "").strip()
    sname = str(screen_name or "").strip()
    if not uid:
        return []

    now = time.time()
    cache_key = f"{uid}:{int(bool(include_replies))}"
    min_interval = (
        _FORCE_CACHE_TTL_SECONDS
        if force
        else float(getattr(config, "X_POLL_MIN_INTERVAL_SECONDS", 180))
    )

    cached = _tweets_cache.get(cache_key)
    if cached and (now - cached[0]) < min_interval:
        return cached[1]

    ck = cookies if cookies is not None else _build_x_cookies()
    if not ck:
        return []

    headers = _build_x_headers(ck["ct0"])
    import json

    operation_name = "UserTweetsAndReplies" if include_replies else "UserTweets"
    default_qids = (
        _USER_TWEETS_AND_REPLIES_QUERY_IDS
        if include_replies
        else _USER_TWEETS_QUERY_IDS
    )
    candidates = _get_query_id_candidates(operation_name, default_qids)

    variables = json.dumps(
        {
            "userId": uid,
            "count": 20,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        },
        separators=(",", ":"),
    )
    features = json.dumps(_TWEET_FEATURES, separators=(",", ":"))
    field_toggles = json.dumps({"withArticlePlainText": False}, separators=(",", ":"))

    for attempt in range(2):
        for qid in candidates:
            url = f"https://x.com/i/api/graphql/{qid}/{operation_name}"
            try:
                resp = requests.get(
                    url,
                    params={
                        "variables": variables,
                        "features": features,
                        "fieldToggles": field_toggles,
                    },
                    headers=headers,
                    cookies=ck,
                    timeout=10,
                )
                if resp.status_code in (401, 403):
                    raise XCookieExpiredError(f"HTTP {resp.status_code}")
                if resp.status_code == 429:
                    log_event(f"[X 속도 제한 경고] @{sname} 타임라인 조회 HTTP 429 감지 (다음 주기에 재시도)")
                    return cached[1] if cached else []

                if resp.status_code == 200:
                    data = resp.json()
                    errors = data.get("errors") or []
                    for err in errors:
                        if err.get("code") in (32, 89, 215, 326):
                            raise XCookieExpiredError(str(err.get("message") or "Auth Error"))

                    tweets = _parse_timeline_instructions(data, uid, sname)
                    _prune_cache(_tweets_cache, max(min_interval * 2, 600.0), _MAX_CACHE_ENTRIES)
                    _tweets_cache[cache_key] = (now, tweets)
                    return tweets
            except XCookieExpiredError:
                raise
            except Exception as e:
                log_event(f"[X 타임라인 경고] {operation_name}({qid}) 호출 오류: {e}")

        if attempt == 0:
            _discover_x_query_ids()
            candidates = _get_query_id_candidates(operation_name, default_qids)

    return cached[1] if cached else []


def _resolve_x_streamer_avatar(
    streamer: dict[str, Any], x_avatar_url: Optional[str] = None
) -> str:
    """방송인의 프로필 이미지 우선순위(X 프로필 -> 치지직 프로필 -> 카페 프로필 -> 기본 아이콘)를 반환합니다."""
    for candidate in (
        x_avatar_url,
        streamer.get("x_profile_image_url"),
        streamer.get("profile_image_url"),
        streamer.get("cafe_profile_image_url"),
    ):
        if candidate and not candidate.lower().split("?")[0].endswith(".ico"):
            return candidate
    return DEFAULT_PNG_ICON


def _build_x_mention_prefix(streamer: dict[str, Any], is_post: bool = False) -> str:
    """
    X 알림 멘션 접두사를 반환합니다.
    - mention_on_open_only=True(기본값)일 때: 치지직과 카페가 모두 없는 'X 단독 방송인'의 '일반 새 포스트(is_post=True)'에만 멘션을 붙입니다.
    - mention_on_open_only=False(모든 알림에서 호출)일 때: 활성화된 모든 X 알림에 멘션을 붙입니다.
    """
    mention_role_id = (streamer.get("mention_role_id") or "").strip()
    mention_text = (streamer.get("mention_text") or "").strip()
    if not mention_role_id and not mention_text:
        return ""

    has_chzzk = bool((streamer.get("chzzk_channel_id") or "").strip())
    has_cafe = bool(
        (streamer.get("cafe_club_id") or "").strip()
        and (streamer.get("cafe_member_key") or "").strip()
    )

    if streamer.get("mention_on_open_only", True):
        if has_chzzk or has_cafe or not is_post:
            return ""

    if mention_role_id:
        return f"<@&{mention_role_id}> "
    if mention_text:
        if mention_text.isdigit():
            return f"<@&{mention_text}> "
        return f"{mention_text} "
    return ""


def check_twitter_x_for_streamer(
    streamer: dict[str, Any],
    streamer_state: dict[str, Any],
    global_cfg: dict[str, Any],
) -> bool:
    """
    개별 방송인의 X(구 트위터) 새 포스트/답글/리트윗을 확인하고 디스코드 알림을 발송합니다.
    상태(state.json)가 변경되었으면 True를 반환합니다.
    """
    screen_name = (streamer.get("x_screen_name") or "").strip()
    user_id = (streamer.get("x_user_id") or "").strip()

    if not screen_name and not user_id:
        return False

    notify_post = streamer.get("notify_x_post", True)
    notify_reply = streamer.get("notify_x_reply", False)
    notify_retweet = streamer.get("notify_x_retweet", False)

    if not (notify_post or notify_reply or notify_retweet):
        return False

    cookies = _build_x_cookies(
        global_cfg.get("x_auth_token"),
        global_cfg.get("x_ct0"),
    )
    if not cookies:
        return False

    webhook_url = streamer.get("custom_webhook_url") or global_cfg.get(
        "discord_webhook_url"
    )
    streamer_name = streamer.get("name") or screen_name or "방송인"

    x_state = streamer_state.setdefault(
        "twitter_x",
        {"initialized": False, "last_tweet_id": None, "user_id": None},
    )
    state_changed = False

    # user_id가 아직 없으면 screen_name으로 자동 조회하여 채움
    if not user_id:
        cached_uid = str(x_state.get("user_id") or "").strip()
        if cached_uid:
            user_id = cached_uid
            streamer["x_user_id"] = user_id
        else:
            try:
                u_info = extract_x_user_info(
                    screen_name,
                    global_cfg.get("x_auth_token"),
                    global_cfg.get("x_ct0"),
                )
            except XCookieExpiredError:
                _handle_x_cookie_expired_alert(webhook_url, streamer_name)
                return False

            if not u_info:
                return False
            user_id = u_info["user_id"]
            screen_name = u_info["screen_name"]
            streamer["x_user_id"] = user_id
            streamer["x_screen_name"] = screen_name
            if u_info.get("profile_image_url"):
                streamer["x_profile_image_url"] = u_info["profile_image_url"]
                if not streamer.get("profile_image_url"):
                    streamer["profile_image_url"] = u_info["profile_image_url"]
            x_state["user_id"] = user_id
            state_changed = True

    try:
        tweets = fetch_x_user_tweets(
            user_id=user_id,
            screen_name=screen_name,
            cookies=cookies,
            include_replies=bool(notify_reply),
        )
    except XCookieExpiredError:
        _handle_x_cookie_expired_alert(webhook_url, streamer_name)
        return state_changed

    if not tweets:
        if not x_state.get("initialized", False):
            x_state["initialized"] = True
            x_state["last_tweet_id"] = 0
            x_state["user_id"] = user_id
            state_changed = True
        return state_changed

    # 프로필 이미지가 최신화되었으면 반영
    latest_avatar = tweets[0].get("profile_image_url")
    if latest_avatar:
        streamer["x_profile_image_url"] = latest_avatar
        if not streamer.get("profile_image_url"):
            streamer["profile_image_url"] = latest_avatar

    max_tweet_id = max(int(t["id"]) for t in tweets)

    # 최초 실행 시 과거 포스트 폭탄 방지: 최신 트윗 ID만 기록
    if not x_state.get("initialized", False):
        x_state["initialized"] = True
        x_state["last_tweet_id"] = max_tweet_id
        x_state["user_id"] = user_id
        log_event(
            f"[X 초기화] {streamer_name} (@{screen_name}) 최신 포스트 번호(#{max_tweet_id}) 기록 완료"
        )
        return True

    last_tweet_id = int(x_state.get("last_tweet_id") or 0)
    new_tweets = [t for t in tweets if int(t["id"]) > last_tweet_id]
    new_tweets.sort(key=lambda x: int(x["id"]))

    for tw in new_tweets:
        tw_id = int(tw["id"])
        tw_type = tw.get("type", "post")

        # 사용자 알림 필터 확인
        if tw_type == "post" and not notify_post:
            x_state["last_tweet_id"] = tw_id
            state_changed = True
            continue
        if tw_type == "reply" and not notify_reply:
            x_state["last_tweet_id"] = tw_id
            state_changed = True
            continue
        if tw_type == "retweet" and not notify_retweet:
            x_state["last_tweet_id"] = tw_id
            state_changed = True
            continue

        tw_url = tw.get("url") or f"https://x.com/{screen_name}/status/{tw_id}"
        tw_text = tw.get("text") or "(내용 없음)"
        if len(tw_text) > 500:
            tw_text = tw_text[:500] + "..."
        media_url = tw.get("media_url") or None
        avatar_url = _resolve_x_streamer_avatar(streamer, tw.get("profile_image_url"))
        mention_prefix = _build_x_mention_prefix(streamer, is_post=(tw_type == "post"))

        if tw_type == "reply":
            reply_to = tw.get("in_reply_to_screen_name")
            content_msg = f"{mention_prefix}💬 **{streamer_name}**님이 X(트위터)에 새 답글을 남겼습니다!"
            embed_title = (
                f"@{reply_to} 님에게 남긴 답글"
                if reply_to
                else f"{streamer_name} (@{screen_name})님의 새 답글"
            )
            author_label = f"{streamer_name} (@{screen_name}) - X 답글"
            footer_label = "X(구 트위터) 답글 알림"
        elif tw_type == "retweet":
            content_msg = f"{mention_prefix}🔁 **{streamer_name}**님이 X(트위터) 포스트를 리트윗했습니다!"
            embed_title = f"{streamer_name} (@{screen_name})님의 리트윗"
            author_label = f"{streamer_name} (@{screen_name}) - X 리트윗"
            footer_label = "X(구 트위터) 리트윗 알림"
        else:
            content_msg = f"{mention_prefix}🐦 **{streamer_name}**님이 X(트위터)에 새 포스트를 작성했습니다!"
            embed_title = f"{streamer_name} (@{screen_name})님의 새 포스트"
            author_label = f"{streamer_name} (@{screen_name}) - X 포스트"
            footer_label = "X(구 트위터) 포스트 알림"

        log_event(
            f"[X 감지] 🐦 {streamer_name} (@{screen_name}) 새 {tw_type}: {tw_text[:35]}... (#{tw_id})"
        )
        send_discord_notification(
            content=content_msg,
            embed_title=embed_title,
            embed_url=tw_url,
            embed_description=tw_text,
            color=COLOR_TWITTER_X,
            author_name=author_label,
            author_icon_url=avatar_url,
            thumbnail_url=avatar_url,
            image_url=media_url,
            footer_text=footer_label,
            webhook_username=f"{streamer_name} X 알림",
            webhook_avatar_url=avatar_url,
            webhook_url=webhook_url,
        )
        x_state["last_tweet_id"] = tw_id
        state_changed = True

    if webhook_url in _x_cookie_alert_timestamps:
        _x_cookie_alert_timestamps.pop(webhook_url, None)

    return state_changed


def check_twitter_x(state: dict[str, Any]) -> bool:
    """레거시 하위 호환용 래퍼 함수입니다."""
    _ = state
    return False
