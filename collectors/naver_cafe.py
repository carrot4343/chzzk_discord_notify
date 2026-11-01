"""
네이버 카페(Naver Cafe)에서 특정 방송인(memberKey)이 작성한
새 게시글과 새 댓글을 감지하여 디스코드로 알려주는 모듈입니다.
다중 방송인 지원, 게시글 링크 기반 자동 ID 추출, 프로필 사진(.png) 정상 출력을 지원합니다.
"""

import html
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import unquote
import requests
import config
from notifiers.discord_webhook import (
    COLOR_NAVER_CAFE,
    COLOR_NAVER_COMMENT,
    DEFAULT_PNG_ICON,
    send_discord_notification,
)
from utils.settings_manager import load_settings, log_event

# 디스코드에서 깨지지 않는 PNG 아이콘 기본값
NAVER_CAFE_ICON_URL = DEFAULT_PNG_ICON

# 다중 사용자 중복 조회 방지용 캐시 (유효시간 15초)
_CACHE_TTL_SECONDS = 15.0
_MAX_CACHE_ENTRIES = 500
_cache_lock = threading.Lock()
_articles_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_comments_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_body_cache: dict[str, tuple[float, tuple[str, Optional[str]]]] = {}

# 쿠키 만료 알림 쿨다운 (웹후크당 24시간 1회)
_cookie_alert_timestamps: dict[str, float] = {}
_COOKIE_ALERT_COOLDOWN = 86400.0


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


class NaverCookieExpiredError(Exception):
    """네이버 로그인 쿠키(NID_AUT/NID_SES)가 만료되었거나 유효하지 않을 때 발생하는 예외입니다."""
    pass


def _handle_cookie_expired_alert(webhook_url: Optional[str], streamer_name: str) -> None:
    """네이버 로그인 쿠키 만료 시 관리자/서버 웹후크로 경고 카드를 발송합니다 (24시간당 1회)."""
    if not webhook_url:
        return
    now = time.time()
    last_sent = _cookie_alert_timestamps.get(webhook_url, 0.0)
    if now - last_sent < _COOKIE_ALERT_COOLDOWN:
        return

    _cookie_alert_timestamps[webhook_url] = now
    log_event(
        f"[네이버 카페 경고] '{streamer_name}' 카페 수집 중 쿠키 만료 감지 -> 디스코드 경고 전송"
    )
    send_discord_notification(
        content="⚠️ **[네이버 로그인 쿠키 만료 경고]** 카페 알림 수집을 위한 네이버 쿠키가 만료되었습니다.",
        embed_title="🔑 네이버 로그인 쿠키 갱신 필요",
        embed_url="https://nid.naver.com/nidlogin.login",
        embed_description=(
            f"**{streamer_name}** 님의 카페 새 글 및 댓글을 확인하던 중 네이버 로그인 세션이 만료되었거나 유효하지 않음이 확인되었습니다.\n\n"
            "• 회원 전용 카페 게시글이나 작성자 댓글 알림이 누락될 수 있습니다.\n"
            "• 웹 대시보드 또는 디스코드 `/쿠키설정` 명령어를 통해 최신 `NID_AUT` / `NID_SES` 쿠키를 다시 등록해 주세요."
        ),
        color=0xF59E0B,
        footer_text="네이버 카페 쿠키 상태 경고",
        webhook_username="네이버 카페 상태 알리미",
        webhook_url=webhook_url,
    )


def _build_cookies(
    nid_aut: Optional[str] = None, nid_ses: Optional[str] = None
) -> dict[str, str]:
    """전달받은 쿠키 또는 settings.json / .env 의 네이버 로그인 쿠키를 반환합니다."""
    if nid_aut and nid_ses:
        return {"NID_AUT": nid_aut.strip(), "NID_SES": nid_ses.strip()}

    settings = load_settings()
    g_cfg = settings.get("global") or {}
    aut = (g_cfg.get("naver_nid_aut") or config.NAVER_NID_AUT or "").strip()
    ses = (g_cfg.get("naver_nid_ses") or config.NAVER_NID_SES or "").strip()
    if aut and ses:
        return {"NID_AUT": aut, "NID_SES": ses}
    return {}


def _clean_html_text(raw_text: str) -> str:
    """HTML 태그 및 엔티티(&quot; 등)를 깔끔한 일반 텍스트로 정리합니다."""
    if not raw_text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", raw_text, flags=re.IGNORECASE)
    no_tags = re.sub(r"<[^>]+>", "", text)
    cleaned = html.unescape(no_tags)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def resolve_club_id_from_slug(cafe_slug: str) -> Optional[str]:
    """카페 영문 주소(예: cafe.naver.com/2chamcham2)에서 숫자 clubid를 추출합니다."""
    url = "https://apis.naver.com/cafe-web/cafe2/CafeGateInfo.json"
    try:
        resp = requests.get(
            url, params={"cluburl": cafe_slug}, headers=config.DEFAULT_HEADERS, timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            cafe_info = ((data.get("message") or {}).get("result") or {}).get(
                "cafeInfoView"
            ) or {}
            cafe_id = cafe_info.get("cafeId")
            if cafe_id:
                return str(cafe_id)
    except Exception:
        pass

    try:
        page_resp = requests.get(
            f"https://cafe.naver.com/{cafe_slug}",
            headers=config.DEFAULT_HEADERS,
            timeout=10,
        )
        match = re.search(r"g_sClubId\s*=\s*[\"'](\d+)[\"']", page_resp.text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def parse_cafe_article_url(article_url: str) -> tuple[Optional[str], Optional[str]]:
    """다양한 형태의 네이버 카페 게시글 URL에서 (club_id, article_id)를 추출합니다."""
    decoded_url = unquote(unquote((article_url or "").strip()))

    m1 = re.search(r"/cafes/(\d+)/articles/(\d+)", decoded_url)
    if m1:
        return m1.group(1), m1.group(2)

    m_club = re.search(r"clubid=(\d+)", decoded_url, re.IGNORECASE)
    m_art = re.search(r"articleid=(\d+)", decoded_url, re.IGNORECASE)
    if m_club and m_art:
        return m_club.group(1), m_art.group(1)

    m3 = re.search(
        r"cafe\.naver\.com/(?:ca-fe/web/)?([a-zA-Z0-9_-]+)/(\d+)", decoded_url
    )
    if m3 and m3.group(1) not in ("cafes", "f-e"):
        cafe_slug = m3.group(1)
        art_id = m3.group(2)
        club_id = resolve_club_id_from_slug(cafe_slug)
        return club_id, art_id

    return None, None


def extract_cafe_info_from_url(
    article_url: str,
    nid_aut: Optional[str] = None,
    nid_ses: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """
    방송인이 작성한 대표 게시글 URL 1개에서
    club_id, article_id, member_key, 작성자 닉네임, 프로필 사진 URL을 자동 추출합니다.
    """
    club_id, article_id = parse_cafe_article_url(article_url)
    if not club_id or not article_id:
        return None

    cookies = _build_cookies(nid_aut, nid_ses)
    for api_ver in ("v2.1", "v2"):
        url = (
            f"https://apis.naver.com/cafe-web/cafe-articleapi/{api_ver}/"
            f"cafes/{club_id}/articles/{article_id}"
        )
        try:
            resp = requests.get(
                url,
                params={"useCafeId": "true"},
                headers={**config.DEFAULT_HEADERS, "Referer": "https://cafe.naver.com/"},
                cookies=cookies,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("result") or {}
                article = result.get("article") or {}
                writer = article.get("writer") or {}
                member_key = writer.get("memberKey")
                nick = writer.get("nick") or ""
                img_url = (writer.get("image") or {}).get("url") or ""
                if member_key:
                    return {
                        "club_id": str(club_id),
                        "article_id": str(article_id),
                        "member_key": str(member_key),
                        "nick": str(nick),
                        "profile_image_url": str(img_url),
                    }
        except Exception as e:
            log_event(f"[네이버 카페 경고] 게시글 작성자 추출 실패: {e}")

    return None


def fetch_article_body_summary(
    club_id: str,
    article_id: int,
    cookies: Optional[dict[str, str]] = None,
) -> tuple[str, Optional[str]]:
    """
    게시글 상세 API를 호출하여 (본문 요약 텍스트, 작성자 프로필 이미지 URL)을 가져옵니다.
    """
    now = time.time()
    cache_key = f"{club_id}:{article_id}"
    cached = _body_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    url = (
        f"https://apis.naver.com/cafe-web/cafe-articleapi/v2.1/"
        f"cafes/{club_id}/articles/{article_id}"
    )
    headers = {
        **config.DEFAULT_HEADERS,
        "Referer": "https://cafe.naver.com/",
    }
    try:
        resp = requests.get(
            url,
            params={"useCafeId": "true"},
            headers=headers,
            cookies=cookies if cookies is not None else _build_cookies(),
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            article = (data.get("result") or {}).get("article") or {}
            content_html = article.get("contentHtml") or ""
            writer_img = ((article.get("writer") or {}).get("image") or {}).get("url")
            body = _clean_html_text(content_html)
            if len(body) > 400:
                body = body[:400] + "..."
            res = (body, writer_img)
            _prune_cache(_body_cache, _CACHE_TTL_SECONDS, _MAX_CACHE_ENTRIES)
            _body_cache[cache_key] = (now, res)
            return res
    except Exception:
        pass
    return "", None


def fetch_member_articles(
    club_id: str,
    member_key: str,
    cookies: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """특정 카페(club_id)에서 특정 멤버(member_key)가 작성한 최신 게시글 목록을 조회합니다."""
    now = time.time()
    cache_key = f"{club_id}:{member_key}"
    cached = _articles_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    ck = cookies if cookies is not None else _build_cookies()
    url = "https://apis.naver.com/cafe-web/cafe-mobile/CafeMemberNetworkArticleListV3"
    params = {
        "search.cafeId": club_id,
        "search.memberKey": member_key,
        "search.perPage": 10,
        "search.page": 1,
        "requestFrom": "A",
    }
    headers = {
        **config.DEFAULT_HEADERS,
        "Referer": "https://m.cafe.naver.com/",
    }
    try:
        resp = requests.get(
            url,
            params=params,
            headers=headers,
            cookies=ck,
            timeout=10,
        )
        if resp.status_code in (401, 403):
            if ck and (ck.get("NID_AUT") or ck.get("NID_SES")):
                raise NaverCookieExpiredError(f"HTTP {resp.status_code}")
            return []

        resp.raise_for_status()
        data = resp.json()
        result = (data.get("message") or {}).get("result") or {}
        items = result.get("articleList") or []
        if items:
            _prune_cache(_articles_cache, _CACHE_TTL_SECONDS, _MAX_CACHE_ENTRIES)
            _articles_cache[cache_key] = (now, items)
        return items
    except NaverCookieExpiredError:
        raise
    except Exception as e:
        log_event(f"[네이버 카페 오류] 게시글 목록 조회 실패: {e}")
        return []


def fetch_member_comments(
    club_id: str,
    member_key: str,
    cookies: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """
    특정 카페(club_id)에서 특정 멤버(member_key)가 작성한 최신 댓글 목록을 조회합니다.
    작성 댓글 탭이 매니저 전용으로 막혀 있으면 '댓글단 글 목록(comment-articles)'에서 추출합니다.
    """
    now = time.time()
    cache_key = f"{club_id}:{member_key}"
    cached = _comments_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    ck = cookies if cookies is not None else _build_cookies()
    headers = {
        **config.DEFAULT_HEADERS,
        "Referer": "https://cafe.naver.com/",
    }

    # 1차 시도: 작성 댓글 목록 API
    url = "https://apis.naver.com/cafe-web/cafe-mobile/CafeMemberProfileCommentList"
    params = {
        "cafeId": club_id,
        "memberKey": member_key,
        "perPage": 10,
        "page": 1,
        "requestFrom": "A",
    }
    try:
        resp = requests.get(
            url, params=params, headers=headers, cookies=ck, timeout=10
        )
        if resp.status_code in (401, 403):
            if ck and (ck.get("NID_AUT") or ck.get("NID_SES")):
                raise NaverCookieExpiredError(f"HTTP {resp.status_code}")
        elif resp.status_code == 200:
            data = resp.json()
            result = (data.get("message") or {}).get("result") or {}
            comments = result.get("comments") or []
            if comments:
                _prune_cache(_comments_cache, _CACHE_TTL_SECONDS, _MAX_CACHE_ENTRIES)
                _comments_cache[cache_key] = (now, comments)
                return comments
    except NaverCookieExpiredError:
        raise
    except Exception:
        pass

    # 2차 시도: 댓글단 게시글(comment-articles) 상위 3개에서 방송인의 댓글 추출
    collected_comments: list[dict[str, Any]] = []
    try:
        ca_url = f"https://apis.naver.com/cafe-web/cafe-boardlist-api/v2/cafes/{club_id}/comment-articles"
        ca_resp = requests.get(
            ca_url,
            params={"count": 3, "targetMemberKey": member_key},
            headers=headers,
            cookies=ck,
            timeout=10,
        )
        if ca_resp.status_code != 200:
            return []

        article_list = (ca_resp.json().get("result") or {}).get("articleList") or []
        for art in article_list[:3]:
            art_id = art.get("articleId") or art.get("articleid")
            art_subject = art.get("subject") or f"게시글 #{art_id}"
            if not art_id:
                continue

            found_in_article = _extract_streamer_comments_from_article(
                club_id, int(art_id), art_subject, member_key, headers, ck
            )
            collected_comments.extend(found_in_article)
    except Exception as e:
        log_event(f"[네이버 카페 오류] 댓글단 글 기반 댓글 조회 실패: {e}")

    collected_comments.sort(key=_extract_comment_unique_id, reverse=True)
    if collected_comments:
        _comments_cache[cache_key] = (now, collected_comments)
    return collected_comments


def _extract_streamer_comments_from_article(
    club_id: str,
    article_id: int,
    article_subject: str,
    member_key: str,
    headers: dict[str, str],
    cookies: dict[str, str],
) -> list[dict[str, Any]]:
    """특정 게시글 안에서 방송인(member_key)이 작성한 댓글들만 골라냅니다."""
    results: dict[int, dict[str, Any]] = {}

    for order in ("desc", "asc"):
        c_url = (
            f"https://apis.naver.com/cafe-web/cafe-articleapi/v2/"
            f"cafes/{club_id}/articles/{article_id}/comments/pages/1"
        )
        try:
            r = requests.get(
                c_url,
                params={"requestFrom": "A", "orderBy": order},
                headers=headers,
                cookies=cookies,
                timeout=10,
            )
            if r.status_code == 200:
                items = (
                    (r.json().get("result") or {}).get("comments") or {}
                ).get("items") or []
                for item in items:
                    writer = item.get("writer") or {}
                    if writer.get("memberKey") == member_key:
                        cid = int(item.get("id") or 0)
                        sticker_obj = item.get("sticker") or {}
                        sticker_url = sticker_obj.get("url")
                        if sticker_url and "?" not in sticker_url:
                            sticker_url += "?type=p100_100"
                        writer_img = (writer.get("image") or {}).get("url")
                        results[cid] = {
                            "commentId": cid,
                            "articleId": article_id,
                            "content": item.get("content") or "",
                            "stickerImageUrl": sticker_url,
                            "writerImageUrl": writer_img,
                            "article": {"subject": article_subject},
                        }
            if results:
                break
        except Exception:
            pass

    return list(results.values())


def _extract_comment_unique_id(comment_item: dict[str, Any]) -> int:
    """댓글 데이터에서 순서를 비교할 수 있는 고유 숫자 ID를 추출합니다."""
    for key in ("commentId", "commentid", "replyId", "id"):
        val = comment_item.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return int(comment_item.get("articleId") or comment_item.get("articleid") or 0)


def _resolve_streamer_avatar(
    streamer: dict[str, Any], writer_img: Optional[str] = None
) -> str:
    """
    디스코드에서 절대 깨지지 않는 방송인 프로필 사진 URL(.png/.jpg)을 반환합니다.
    1순위: 방송인의 치지직 프로필 사진 (nng-phinf.pstatic.net)
    2순위: 네이버 카페 작성자 프로필 사진 (cafeptthumb-phinf.pstatic.net)
    3순위: 기본 PNG 아이콘
    """
    for candidate in (
        streamer.get("profile_image_url"),
        streamer.get("cafe_profile_image_url"),
        writer_img,
    ):
        if candidate and not candidate.lower().split("?")[0].endswith(".ico"):
            return candidate
    return DEFAULT_PNG_ICON


def _build_cafe_mention_prefix(streamer: dict[str, Any], is_article: bool = False) -> str:
    """카페 알림 멘션 접두사를 반환합니다 (기본값: 뱅온 전용이므로 빈 문자열, 단 카페 단독 등록자의 새 글은 호출)."""
    mention_role_id = (streamer.get("mention_role_id") or "").strip()
    mention_text = (streamer.get("mention_text") or "").strip()
    if not mention_role_id and not mention_text:
        return ""

    has_chzzk = bool((streamer.get("chzzk_channel_id") or "").strip())
    if streamer.get("mention_on_open_only", True):
        if has_chzzk or not is_article:
            return ""

    if mention_role_id:
        return f"<@&{mention_role_id}> "
    if mention_text:
        if mention_text.isdigit():
            return f"<@&{mention_text}> "
        return f"{mention_text} "
    return ""


def check_naver_cafe_for_streamer(
    streamer: dict[str, Any],
    streamer_state: dict[str, Any],
    global_cfg: dict[str, Any],
) -> bool:
    """
    개별 방송인의 네이버 카페 새 게시글과 새 댓글을 확인하고 디스코드 알림을 발송합니다.
    """
    club_id = (streamer.get("cafe_club_id") or "").strip()
    member_key = (streamer.get("cafe_member_key") or "").strip()

    if not club_id or not member_key:
        return False

    cookies = _build_cookies(
        global_cfg.get("naver_nid_aut"), global_cfg.get("naver_nid_ses")
    )
    webhook_url = streamer.get("custom_webhook_url") or global_cfg.get(
        "discord_webhook_url"
    )
    streamer_name = streamer.get("name") or "방송인"

    cafe_state = streamer_state.setdefault("naver_cafe", {})
    state_changed = False

    # 1. 작성 게시글 감지
    if streamer.get("notify_cafe_article", True):
        try:
            articles = fetch_member_articles(club_id, member_key, cookies)
        except NaverCookieExpiredError:
            _handle_cookie_expired_alert(webhook_url, streamer_name)
            return False

        if articles:
            max_article_id = max(
                int(a.get("articleid") or a.get("articleId") or 0) for a in articles
            )

            if not cafe_state.get("articles_initialized", False):
                cafe_state["articles_initialized"] = True
                cafe_state["last_article_id"] = max_article_id
                log_event(
                    f"[네이버 카페 초기화] {streamer_name} 최신 게시글 번호(#{max_article_id}) 기록 완료"
                )
                state_changed = True
            else:
                last_article_id = int(cafe_state.get("last_article_id") or 0)
                new_articles = [
                    a
                    for a in articles
                    if int(a.get("articleid") or a.get("articleId") or 0)
                    > last_article_id
                ]
                new_articles.sort(
                    key=lambda x: int(x.get("articleid") or x.get("articleId") or 0)
                )

                for article in new_articles:
                    art_id = int(
                        article.get("articleid") or article.get("articleId") or 0
                    )
                    subject = _clean_html_text(article.get("subject") or "새 게시글")
                    body_text, writer_img = fetch_article_body_summary(
                        club_id, art_id, cookies
                    )
                    nickname = (
                        article.get("writernickname")
                        or article.get("nickname")
                        or streamer_name
                    )
                    menu_name = (
                        (article.get("clubMenu") or {}).get("menuname") or ""
                    ).strip("\ufeff ")
                    avatar_url = _resolve_streamer_avatar(streamer, writer_img)
                    thumb = article.get("representImage") or avatar_url
                    article_url = f"https://cafe.naver.com/ca-fe/web/cafes/{club_id}/articles/{art_id}"

                    desc = (
                        body_text
                        if body_text
                        else f"[👉 게시글 보러가기]({article_url})"
                    )
                    fields = (
                        [{"name": "게시판", "value": menu_name, "inline": True}]
                        if menu_name
                        else None
                    )

                    log_event(
                        f"[네이버 카페 감지] 📗 {nickname} 새 게시글: {subject} (#{art_id})"
                    )
                    mention_prefix = _build_cafe_mention_prefix(streamer, is_article=True)
                    send_discord_notification(
                        content=f"{mention_prefix}📗 **{nickname}**님이 네이버 카페에 새 게시글을 작성했습니다!",
                        embed_title=subject,
                        embed_url=article_url,
                        embed_description=desc,
                        color=COLOR_NAVER_CAFE,
                        author_name=f"{nickname} (네이버 카페 게시글)",
                        author_icon_url=avatar_url,
                        thumbnail_url=thumb,
                        fields=fields,
                        footer_text="네이버 카페 게시글 알림",
                        webhook_username=f"{nickname} 카페 알림",
                        webhook_avatar_url=avatar_url,
                        webhook_url=webhook_url,
                    )
                    cafe_state["last_article_id"] = art_id
                    state_changed = True
        elif not cafe_state.get("articles_initialized", False):
            cafe_state["articles_initialized"] = True
            cafe_state["last_article_id"] = 0
            state_changed = True

    # 2. 작성 댓글 감지
    if streamer.get("notify_cafe_comment", True):
        try:
            comments = fetch_member_comments(club_id, member_key, cookies)
        except NaverCookieExpiredError:
            _handle_cookie_expired_alert(webhook_url, streamer_name)
            return state_changed

        if comments:
            max_comment_id = max(_extract_comment_unique_id(c) for c in comments)

            if not cafe_state.get("comments_initialized", False):
                cafe_state["comments_initialized"] = True
                cafe_state["last_comment_id"] = max_comment_id
                log_event(
                    f"[네이버 카페 초기화] {streamer_name} 최신 댓글 번호(#{max_comment_id}) 기록 완료"
                )
                state_changed = True
            else:
                last_comment_id = int(cafe_state.get("last_comment_id") or 0)
                new_comments = [
                    c
                    for c in comments
                    if _extract_comment_unique_id(c) > last_comment_id
                ]
                new_comments.sort(key=_extract_comment_unique_id)

                for comment in new_comments:
                    cid = _extract_comment_unique_id(comment)
                    art_id = int(
                        comment.get("articleId") or comment.get("articleid") or 0
                    )
                    contents = _clean_html_text(
                        comment.get("content")
                        or comment.get("contents")
                        or "(스티커/이미지 댓글)"
                    )
                    parent_article = comment.get("article") or {}
                    parent_subject = _clean_html_text(
                        parent_article.get("subject")
                        or comment.get("subject")
                        or f"게시글 #{art_id}"
                    )
                    avatar_url = _resolve_streamer_avatar(
                        streamer, comment.get("writerImageUrl")
                    )
                    sticker_url = comment.get("stickerImageUrl") or avatar_url
                    article_url = f"https://cafe.naver.com/ca-fe/web/cafes/{club_id}/articles/{art_id}"

                    log_event(
                        f"[네이버 카페 감지] 💬 {streamer_name} 새 댓글: {contents[:30]}..."
                    )
                    mention_prefix = _build_cafe_mention_prefix(streamer)
                    send_discord_notification(
                        content=f"{mention_prefix}💬 **{streamer_name}**님이 네이버 카페에 새 댓글을 남겼습니다!",
                        embed_title=f"원문: {parent_subject}",
                        embed_url=article_url,
                        embed_description=f"**댓글 내용:**\n{contents}",
                        color=COLOR_NAVER_COMMENT,
                        author_name=f"{streamer_name} (네이버 카페 댓글)",
                        author_icon_url=avatar_url,
                        thumbnail_url=sticker_url,
                        footer_text="네이버 카페 댓글 알림",
                        webhook_username=f"{streamer_name} 카페 알림",
                        webhook_avatar_url=avatar_url,
                        webhook_url=webhook_url,
                    )
                    cafe_state["last_comment_id"] = cid
                    state_changed = True
        elif not cafe_state.get("comments_initialized", False):
            cafe_state["comments_initialized"] = True
            cafe_state["last_comment_id"] = 0
            state_changed = True

    if webhook_url in _cookie_alert_timestamps:
        _cookie_alert_timestamps.pop(webhook_url, None)

    return state_changed
