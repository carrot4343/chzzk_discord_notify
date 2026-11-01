"""
다중 사용자(멀티 유저) 회원가입/로그인 및 개인별 방송인·웹후크·쿠키 설정을 지원하며,
초대 코드("FLOWANGLE"), 관리자(carrot4343) 전용 기능, 로그인 실패 시도 제한(Rate Limiting),
디스코드 웹후크 SSRF 방지 검증을 포함하는 Flask 웹 대시보드 서버입니다.
"""

from datetime import timedelta
from functools import wraps
from typing import Any, Callable, Optional
from flask import Flask, jsonify, render_template, request, session
import requests

import config
from collectors.chzzk import (
    CHZZK_ICON_URL,
    extract_chzzk_channel_info,
    fetch_chzzk_live_detail,
)
from collectors.naver_cafe import (
    _build_cookies,
    _resolve_streamer_avatar,
    extract_cafe_info_from_url,
    fetch_article_body_summary,
    fetch_member_articles,
    fetch_member_comments,
)
from notifiers.discord_webhook import (
    COLOR_CHZZK_GREEN,
    COLOR_CHZZK_OFFLINE,
    COLOR_NAVER_CAFE,
    COLOR_NAVER_COMMENT,
    send_discord_notification,
    validate_discord_webhook_url,
)
from utils.settings_manager import (
    authenticate_user,
    delete_user_by_admin,
    generate_streamer_id,
    get_admin_summary,
    get_user_logs,
    is_admin,
    load_all_data,
    load_user_settings,
    log_event,
    register_user,
    save_user_settings,
    update_admin_config,
)

app = Flask(
    __name__,
    template_folder=str(config.BASE_DIR / "templates"),
)
app.secret_key = load_all_data().get("secret_key", "taemtaem-notify-secret-key")
# 실질적 무제한/영구 로그인 유지 (10년)
app.permanent_session_lifetime = timedelta(days=3650)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

_check_now_callback: Optional[Callable[[], None]] = None


def set_check_now_callback(cb: Callable[[], None]) -> None:
    """메인 감시 루프의 즉시 점검 함수를 연결합니다."""
    global _check_now_callback
    _check_now_callback = cb


def login_required(fn: Callable[..., Any]) -> Callable[..., Any]:
    """로그인된 사용자만 API를 호출할 수 있도록 보호하는 데코레이터입니다."""
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not session.get("username"):
            return jsonify({"ok": False, "unauthorized": True, "message": "로그인이 필요합니다."}), 401
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn: Callable[..., Any]) -> Callable[..., Any]:
    """carrot4343 관리자만 호출할 수 있도록 보호하는 데코레이터입니다."""
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        uname = session.get("username")
        if not is_admin(uname):
            return jsonify({"ok": False, "forbidden": True, "message": "관리자(carrot4343)만 접근할 수 있는 메뉴입니다."}), 403
        return fn(*args, **kwargs)

    return wrapper


def send_preview_for_streamer(
    streamer: dict[str, Any],
    global_cfg: dict[str, Any],
    username: Optional[str] = None,
) -> tuple[bool, str]:
    """
    특정 방송인의 현재 치지직 상태, 최신 카페 게시글, 최신 카페 댓글을
    해당 사용자의 디스코드 웹후크로 즉시 전송합니다.
    """
    webhook_url = (
        streamer.get("custom_webhook_url")
        or global_cfg.get("discord_webhook_url")
        or ""
    ).strip()
    if not webhook_url:
        return False, "디스코드 웹후크 URL이 설정되지 않았습니다. 먼저 공통 설정에 웹후크 URL을 입력해 주세요."

    if not validate_discord_webhook_url(webhook_url):
        return False, "유효한 디스코드 공식 웹후크 URL(https://discord.com/api/webhooks/...)이 아닙니다."

    sent_count = 0
    channel_id = (streamer.get("chzzk_channel_id") or "").strip()
    streamer_name = streamer.get("name") or "방송인"

    # 1. 치지직 미리보기
    if channel_id:
        detail = fetch_chzzk_live_detail(channel_id)
        if detail:
            ch = detail.get("channel") or {}
            name = ch.get("channelName") or streamer_name
            img = (
                ch.get("channelImageUrl")
                or streamer.get("profile_image_url")
                or CHZZK_ICON_URL
            )
            if ch.get("channelImageUrl"):
                streamer["profile_image_url"] = ch["channelImageUrl"]

            status = detail.get("status") or "CLOSE"
            title = detail.get("liveTitle") or "방제 없음"
            cat = detail.get("liveCategoryValue") or "카테고리 없음"
            live_url = f"https://chzzk.naver.com/live/{channel_id}"
            is_open = status == "OPEN"

            mention_prefix = ""
            if streamer.get("mention_role_id"):
                mention_prefix = f"<@&{streamer['mention_role_id']}> "
            elif streamer.get("mention_text"):
                mt = streamer["mention_text"].strip()
                mention_prefix = f"<@&{mt}> " if mt.isdigit() else f"{mt} "

            ok = send_discord_notification(
                content=(
                    f"{mention_prefix}🔴 **[실전 미리보기] {name}** 현재 방송 중입니다!"
                    if is_open
                    else f"⚫ **[실전 미리보기] {name}** 현재 오프라인 상태입니다."
                ),
                embed_title=title,
                embed_url=live_url,
                embed_description=f"[👉 치지직 채널 바로가기](https://chzzk.naver.com/{channel_id})",
                color=COLOR_CHZZK_GREEN if is_open else COLOR_CHZZK_OFFLINE,
                author_name=f"{name} (치지직)",
                author_icon_url=img,
                thumbnail_url=img,
                fields=[
                    {
                        "name": "현재 상태",
                        "value": "🔴 LIVE" if is_open else "⚫ OFFLINE",
                        "inline": True,
                    },
                    {"name": "마지막 카테고리", "value": cat, "inline": True},
                ],
                footer_text="치지직 상태 미리보기",
                webhook_username=f"{name} 치지직 알림",
                webhook_avatar_url=img,
                webhook_url=webhook_url,
            )
            if ok:
                sent_count += 1

    # 2. 네이버 카페 최신 게시글 & 댓글 미리보기
    club_id = (streamer.get("cafe_club_id") or "").strip()
    member_key = (streamer.get("cafe_member_key") or "").strip()
    if club_id and member_key:
        cookies = _build_cookies(
            global_cfg.get("naver_nid_aut"), global_cfg.get("naver_nid_ses")
        )
        articles = fetch_member_articles(club_id, member_key, cookies)
        if articles:
            latest = articles[0]
            art_id = int(latest.get("articleid") or latest.get("articleId") or 0)
            subject = latest.get("subject") or "제목 없음"
            nickname = (
                latest.get("writernickname")
                or latest.get("nickname")
                or streamer_name
            )
            body_text, writer_img = fetch_article_body_summary(
                club_id, art_id, cookies
            )
            avatar_url = _resolve_streamer_avatar(streamer, writer_img)
            menu_name = (
                (latest.get("clubMenu") or {}).get("menuname") or ""
            ).strip("\ufeff ")
            url = f"https://cafe.naver.com/ca-fe/web/cafes/{club_id}/articles/{art_id}"

            ok = send_discord_notification(
                content=f"📗 **[실전 미리보기] {nickname}**님의 최근 네이버 카페 게시글입니다!",
                embed_title=subject,
                embed_url=url,
                embed_description=body_text or f"[👉 게시글 보러가기]({url})",
                color=COLOR_NAVER_CAFE,
                author_name=f"{nickname} (네이버 카페 게시글)",
                author_icon_url=avatar_url,
                thumbnail_url=avatar_url,
                fields=(
                    [{"name": "게시판", "value": menu_name, "inline": True}]
                    if menu_name
                    else None
                ),
                footer_text="네이버 카페 게시글 미리보기",
                webhook_username=f"{nickname} 카페 알림",
                webhook_avatar_url=avatar_url,
                webhook_url=webhook_url,
            )
            if ok:
                sent_count += 1

        comments = fetch_member_comments(club_id, member_key, cookies)
        if comments:
            latest_c = comments[0]
            c_art_id = latest_c.get("articleId") or latest_c.get("articleid")
            c_text = latest_c.get("content") or "(스티커/이미지 댓글)"
            parent_subj = (
                (latest_c.get("article") or {}).get("subject")
                or f"게시글 #{c_art_id}"
            )
            avatar_url = _resolve_streamer_avatar(
                streamer, latest_c.get("writerImageUrl")
            )
            sticker_url = latest_c.get("stickerImageUrl") or avatar_url
            c_url = f"https://cafe.naver.com/ca-fe/web/cafes/{club_id}/articles/{c_art_id}"

            ok = send_discord_notification(
                content=f"💬 **[실전 미리보기] {streamer_name}**님의 최근 네이버 카페 댓글입니다!",
                embed_title=f"원문: {parent_subj}",
                embed_url=c_url,
                embed_description=f"**댓글 내용:**\n{c_text}",
                color=COLOR_NAVER_COMMENT,
                author_name=f"{streamer_name} (네이버 카페 댓글)",
                author_icon_url=avatar_url,
                thumbnail_url=sticker_url,
                footer_text="네이버 카페 댓글 미리보기",
                webhook_username=f"{streamer_name} 카페 알림",
                webhook_avatar_url=avatar_url,
                webhook_url=webhook_url,
            )
            if ok:
                sent_count += 1

    log_event(
        f"[{username or '시스템'}] {streamer_name} 미리보기 카드 {sent_count}건 디스코드 발송 완료",
        username=username,
    )
    return True, f"✅ {streamer_name} 미리보기 카드 {sent_count}건을 디스코드로 전송했습니다!"


@app.route("/")
def index():
    return render_template("index.html")


# ------------------------------------------------------------------------------
# 인증 (회원가입 / 로그인 / 로그아웃 / 세션 확인) API
# ------------------------------------------------------------------------------
@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    data = load_all_data()
    users = data.get("users") or {}
    uname = session.get("username")
    if uname and uname in users:
        return jsonify(
            {
                "authenticated": True,
                "username": uname,
                "is_admin": is_admin(uname),
                "has_any_user": len(users) > 0,
            }
        )
    session.pop("username", None)
    return jsonify({"authenticated": False, "is_admin": False, "has_any_user": len(users) > 0})


@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    body = request.get_json(force=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    invite_code = (body.get("invite_code") or "").strip()
    ok, msg = register_user(username, password, invite_code)
    if ok:
        session["username"] = username
        session.permanent = True
    return jsonify({"ok": ok, "message": msg, "username": username if ok else None})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    body = request.get_json(force=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    remember_me = bool(body.get("remember_me", True))
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1").split(",")[0].strip()

    ok, msg = authenticate_user(username, password, client_ip=client_ip)
    if ok:
        session["username"] = username
        session.permanent = remember_me
    return jsonify({"ok": ok, "message": msg, "username": username if ok else None})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.pop("username", None)
    return jsonify({"ok": True, "message": "로그아웃되었습니다."})


# ------------------------------------------------------------------------------
# 관리자 전용 API (carrot4343 전용)
# ------------------------------------------------------------------------------
@app.route("/api/admin/summary", methods=["GET"])
@login_required
@admin_required
def api_admin_summary():
    return jsonify(get_admin_summary())


@app.route("/api/admin/config", methods=["POST"])
@login_required
@admin_required
def api_admin_update_config():
    body = request.get_json(force=True) or {}
    invite_code = body.get("invite_code")
    allow_registration = body.get("allow_registration", True)
    ok, msg = update_admin_config(invite_code, allow_registration)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/admin/users/<target_username>", methods=["DELETE"])
@login_required
@admin_required
def api_admin_delete_user(target_username: str):
    ok, msg = delete_user_by_admin(target_username)
    return jsonify({"ok": ok, "message": msg})


# ------------------------------------------------------------------------------
# 개인별 설정 및 방송인 관리 API (로그인 필수)
# ------------------------------------------------------------------------------
@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    username = session["username"]
    return jsonify(load_user_settings(username))


@app.route("/api/settings/global", methods=["POST"])
@login_required
def api_save_global():
    username = session["username"]
    body = request.get_json(force=True) or {}

    webhook_url = (body.get("discord_webhook_url") or "").strip()
    if webhook_url and not validate_discord_webhook_url(webhook_url):
        return jsonify(
            {
                "ok": False,
                "message": "유효한 디스코드 공식 웹후크 URL(https://discord.com/api/webhooks/...)을 입력해 주세요.",
            }
        )

    user_data = load_user_settings(username)
    g = user_data.get("global") or {}
    g["discord_webhook_url"] = webhook_url
    g["naver_nid_aut"] = (body.get("naver_nid_aut") or "").strip()
    g["naver_nid_ses"] = (body.get("naver_nid_ses") or "").strip()

    try:
        interval = int(body.get("poll_interval_seconds") or 60)
    except (ValueError, TypeError):
        interval = 60

    save_user_settings(username, user_global=g, poll_interval_seconds=interval)
    log_event(f"[{username}] 개인 공통 설정(웹후크/쿠키)이 저장되었습니다.", username=username)
    return jsonify({"ok": True, "message": "✅ 내 공통 설정이 저장되었습니다!"})


@app.route("/api/lookup/chzzk", methods=["POST"])
@login_required
def api_lookup_chzzk():
    body = request.get_json(force=True) or {}
    query = (body.get("query") or "").strip()
    info = extract_chzzk_channel_info(query)
    if not info:
        return jsonify(
            {
                "ok": False,
                "message": "해당 입력으로 치지직 채널을 찾지 못했습니다. 주소나 이름을 확인해 주세요.",
            }
        )
    return jsonify({"ok": True, **info})


@app.route("/api/lookup/cafe", methods=["POST"])
@login_required
def api_lookup_cafe():
    username = session["username"]
    user_data = load_user_settings(username)
    g_cfg = user_data.get("global") or {}

    body = request.get_json(force=True) or {}
    article_url = (body.get("article_url") or "").strip()
    nid_aut = (body.get("nid_aut") or g_cfg.get("naver_nid_aut") or "").strip()
    nid_ses = (body.get("nid_ses") or g_cfg.get("naver_nid_ses") or "").strip()

    info = extract_cafe_info_from_url(article_url, nid_aut, nid_ses)
    if not info:
        return jsonify(
            {
                "ok": False,
                "message": "게시글에서 작성자 정보를 추출하지 못했습니다. (네이버 로그인 쿠키 NID_AUT, NID_SES가 입력되어 있는지 확인해 주세요)",
            }
        )
    return jsonify({"ok": True, **info})


@app.route("/api/chzzk/search", methods=["GET"])
@login_required
def api_chzzk_search():
    keyword = (request.args.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"ok": True, "channels": []})

    try:
        url = "https://api.chzzk.naver.com/service/v1/search/channels"
        resp = requests.get(
            url,
            params={"keyword": keyword, "offset": 0, "size": 10},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        data = resp.json()
        raw_list = (data.get("content") or {}).get("data") or []
        results = []
        for item in raw_list:
            ch = item.get("channel") or {}
            cid = ch.get("channelId")
            if not cid:
                continue
            results.append(
                {
                    "channelId": cid,
                    "channelName": ch.get("channelName", "알 수 없음"),
                    "channelImageUrl": ch.get("channelImageUrl") or CHZZK_ICON_URL,
                    "followerCount": ch.get("followerCount", 0),
                    "openLive": bool(ch.get("openLive")),
                    "verifiedMark": bool(ch.get("verifiedMark")),
                }
            )
        return jsonify({"ok": True, "channels": results})
    except Exception as e:
        return jsonify({"ok": False, "message": f"치지직 검색 오류: {e}", "channels": []})


@app.route("/api/chzzk/followings", methods=["GET"])
@login_required
def api_chzzk_followings():
    username = session.get("username")
    cfg = load_user_settings(username)
    g_cfg = cfg.get("global") or {}
    nid_aut = (g_cfg.get("naver_nid_aut") or "").strip()
    nid_ses = (g_cfg.get("naver_nid_ses") or "").strip()

    if not nid_aut or not nid_ses:
        return jsonify(
            {
                "ok": False,
                "need_cookies": True,
                "message": "상단 '공통 설정'에 네이버 로그인 쿠키(NID_AUT, NID_SES)를 먼저 입력하고 저장해 주세요.",
            }
        )

    existing_channel_ids = {
        st.get("chzzk_channel_id") or st.get("chzzk_input")
        for st in cfg.get("streamers", [])
        if st.get("chzzk_channel_id") or st.get("chzzk_input")
    }

    cookies = {"NID_AUT": nid_aut, "NID_SES": nid_ses}
    headers = {"User-Agent": "Mozilla/5.0"}
    all_channels = []

    try:
        page = 0
        while page < 5:  # 최대 250개까지 수집
            url = f"https://api.chzzk.naver.com/service/v1/channels/followings?page={page}&size=50"
            resp = requests.get(url, headers=headers, cookies=cookies, timeout=6)
            if resp.status_code == 401:
                return jsonify(
                    {
                        "ok": False,
                        "need_cookies": True,
                        "message": "네이버 쿠키가 만료되었거나 올바르지 않습니다. 최신 쿠키로 다시 저장해 주세요.",
                    }
                )
            data = resp.json()
            items = (data.get("content") or {}).get("followingList") or []
            if not items:
                break

            for it in items:
                ch = it.get("channel") or {}
                cid = ch.get("channelId")
                if not cid:
                    continue
                pdata = ch.get("personalData", {}).get("following", {})
                all_channels.append(
                    {
                        "channelId": cid,
                        "channelName": ch.get("channelName", "알 수 없음"),
                        "channelImageUrl": ch.get("channelImageUrl") or CHZZK_ICON_URL,
                        "notification": bool(pdata.get("notification")),
                        "openLive": bool((it.get("streamer") or {}).get("openLive")),
                        "liveTitle": (it.get("liveInfo") or {}).get("liveTitle") or "",
                        "already_added": cid in existing_channel_ids,
                    }
                )
            page += 1

        return jsonify({"ok": True, "channels": all_channels})
    except Exception as e:
        return jsonify({"ok": False, "message": f"팔로우 목록을 가져오지 못했습니다: {e}"})


@app.route("/api/streamers/batch", methods=["POST"])
@login_required
def api_streamers_batch():
    username = session.get("username")
    body = request.get_json(force=True) or {}
    items = body.get("streamers") or []
    if not items:
        return jsonify({"ok": False, "message": "추가할 방송인이 선택되지 않았습니다."})

    cfg = load_user_settings(username)
    streamers = cfg.get("streamers") or []
    existing_cids = {
        st.get("chzzk_channel_id") or st.get("chzzk_input")
        for st in streamers
        if st.get("chzzk_channel_id") or st.get("chzzk_input")
    }

    added_count = 0
    for it in items:
        cid = (it.get("channel_id") or "").strip()
        name = (it.get("name") or "").strip()
        img = (it.get("profile_image_url") or "").strip() or CHZZK_ICON_URL
        if not cid or cid in existing_cids:
            continue

        st_obj = {
            "id": generate_streamer_id(),
            "enabled": True,
            "name": name or "치지직 방송인",
            "profile_image_url": img,
            "cafe_profile_image_url": img,
            "chzzk_input": cid,
            "chzzk_channel_id": cid,
            "cafe_article_url": "",
            "cafe_club_id": "",
            "cafe_member_key": "",
            "custom_webhook_url": "",
            "mention_text": "",
            "mention_role_id": "",
            "mention_role_name": "",
            "mention_on_open_only": True,
            "notify_chzzk_open": True,
            "notify_chzzk_close": False,
            "notify_chzzk_title": True,
            "notify_chzzk_category": True,
            "notify_chzzk_community": True,
            "notify_cafe_article": False,
            "notify_cafe_comment": False,
        }
        streamers.append(st_obj)
        existing_cids.add(cid)
        added_count += 1

    if added_count > 0:
        save_user_settings(username, user_streamers=streamers)
        log_event(f"[{username}] 치지직 팔로우 방송인 {added_count}명 일괄 등록 완료", username=username)

    return jsonify({"ok": True, "added_count": added_count, "message": f"{added_count}명의 방송인이 성공적으로 추가되었습니다!"})


@app.route("/api/streamers", methods=["POST"])
@login_required
def api_save_streamer():
    username = session["username"]
    body = request.get_json(force=True) or {}
    user_data = load_user_settings(username)
    g_cfg = user_data.get("global") or {}

    chzzk_input = (body.get("chzzk_input") or "").strip()
    name = (body.get("name") or "").strip()
    profile_img = (body.get("profile_image_url") or "").strip()
    cafe_url = (body.get("cafe_article_url") or "").strip()
    cafe_club_id = (body.get("cafe_club_id") or "").strip()
    cafe_member_key = (body.get("cafe_member_key") or "").strip()
    custom_webhook = (body.get("custom_webhook_url") or "").strip()
    cafe_profile_img = ""

    if custom_webhook and not validate_discord_webhook_url(custom_webhook):
        return jsonify(
            {
                "ok": False,
                "message": "유효한 디스코드 공식 웹후크 URL(https://discord.com/api/webhooks/...)을 입력해 주세요.",
            }
        )

    chzzk_channel_id = ""
    if chzzk_input:
        c_info = extract_chzzk_channel_info(chzzk_input)
        if c_info:
            chzzk_channel_id = c_info["channel_id"]
            if not name:
                name = c_info["name"]
            if not profile_img:
                profile_img = c_info["profile_image_url"]

    if cafe_url and (not cafe_club_id or not cafe_member_key):
        cafe_info = extract_cafe_info_from_url(
            cafe_url, g_cfg.get("naver_nid_aut"), g_cfg.get("naver_nid_ses")
        )
        if cafe_info:
            cafe_club_id = cafe_info["club_id"]
            cafe_member_key = cafe_info["member_key"]
            cafe_profile_img = cafe_info.get("profile_image_url") or ""
            if not name and cafe_info.get("nick"):
                name = cafe_info["nick"]
            if not profile_img and cafe_profile_img:
                profile_img = cafe_profile_img

    if not chzzk_channel_id and not (cafe_club_id and cafe_member_key):
        return jsonify(
            {
                "ok": False,
                "message": "치지직 채널 정보 또는 네이버 카페 대표 게시글 정보 중 최소 1개는 유효해야 합니다.",
            }
        )

    streamer_id = (body.get("id") or "").strip() or generate_streamer_id()
    streamer_obj: dict[str, Any] = {
        "id": streamer_id,
        "enabled": bool(body.get("enabled", True)),
        "name": name or "방송인",
        "profile_image_url": profile_img,
        "cafe_profile_image_url": cafe_profile_img or profile_img,
        "chzzk_input": chzzk_input,
        "chzzk_channel_id": chzzk_channel_id,
        "cafe_article_url": cafe_url,
        "cafe_club_id": cafe_club_id,
        "cafe_member_key": cafe_member_key,
        "custom_webhook_url": custom_webhook,
        "mention_text": (body.get("mention_text") or "").strip(),
        "mention_role_id": (body.get("mention_role_id") or "").strip(),
        "mention_role_name": (body.get("mention_role_name") or "").strip(),
        "mention_on_open_only": bool(body.get("mention_on_open_only", True)),
        "notify_chzzk_open": bool(body.get("notify_chzzk_open", True)),
        "notify_chzzk_title": bool(body.get("notify_chzzk_title", True)),
        "notify_chzzk_category": bool(body.get("notify_chzzk_category", True)),
        "notify_chzzk_community": bool(body.get("notify_chzzk_community", True)),
        "notify_chzzk_close": bool(body.get("notify_chzzk_close", False)),
        "notify_cafe_article": bool(body.get("notify_cafe_article", True)),
        "notify_cafe_comment": bool(body.get("notify_cafe_comment", True)),
    }

    streamers = user_data.get("streamers") or []
    replaced = False
    for idx, existing in enumerate(streamers):
        if existing.get("id") == streamer_id:
            streamers[idx] = streamer_obj
            replaced = True
            break
    if not replaced:
        streamers.append(streamer_obj)

    save_user_settings(username, user_streamers=streamers)
    action_str = "수정" if replaced else "추가"
    log_event(
        f"[{username}] 방송인 '{streamer_obj['name']}' 설정이 {action_str}되었습니다.",
        username=username,
    )

    if _check_now_callback:
        _check_now_callback()

    return jsonify(
        {
            "ok": True,
            "message": f"✅ '{streamer_obj['name']}' 방송인 알림 설정이 {action_str}되었습니다!",
        }
    )


@app.route("/api/streamers/<streamer_id>", methods=["DELETE"])
@login_required
def api_delete_streamer(streamer_id: str):
    username = session["username"]
    user_data = load_user_settings(username)
    streamers = user_data.get("streamers") or []
    target_name = streamer_id
    for st in streamers:
        if st.get("id") == streamer_id:
            target_name = st.get("name") or streamer_id
            break

    updated = [s for s in streamers if s.get("id") != streamer_id]
    save_user_settings(username, user_streamers=updated)
    log_event(
        f"[{username}] 방송인 '{target_name}' 항목이 삭제되었습니다.", username=username
    )
    return jsonify({"ok": True, "message": f"🗑️ '{target_name}' 항목을 삭제했습니다."})


@app.route("/api/streamers/<streamer_id>/preview", methods=["POST"])
@login_required
def api_preview_streamer(streamer_id: str):
    username = session["username"]
    user_data = load_user_settings(username)
    g_cfg = user_data.get("global") or {}
    streamers = user_data.get("streamers") or []
    target = next((s for s in streamers if s.get("id") == streamer_id), None)
    if not target:
        return jsonify({"ok": False, "message": "해당 방송인을 찾을 수 없습니다."})

    ok, msg = send_preview_for_streamer(target, g_cfg, username=username)
    save_user_settings(username, user_streamers=streamers)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/test-webhook", methods=["POST"])
@login_required
def api_test_webhook():
    username = session["username"]
    user_data = load_user_settings(username)
    webhook_url = (user_data.get("global") or {}).get("discord_webhook_url")
    if not webhook_url:
        return jsonify({"ok": False, "message": "등록된 디스코드 웹후크 URL이 없습니다."})

    if not validate_discord_webhook_url(webhook_url):
        return jsonify({"ok": False, "message": "유효한 디스코드 공식 웹후크 URL(https://discord.com/api/webhooks/...)이 아닙니다."})

    ok = send_discord_notification(
        content=f"🎉 **{username}**님의 디스코드 웹후크 연결 테스트에 성공했습니다!",
        embed_title="디스코드 웹후크 연결 정상",
        embed_url="https://chzzk.naver.com/",
        embed_description="내가 등록한 모든 방송인의 치지직·네이버 카페 알림이 이곳으로 전송됩니다.",
        color=COLOR_CHZZK_GREEN,
        footer_text=f"{username} 알림 봇 테스트",
        webhook_username="치지직 & 카페 알림 봇",
        webhook_avatar_url=CHZZK_ICON_URL,
        webhook_url=webhook_url,
    )
    if ok:
        log_event(f"[{username}] 디스코드 웹후크 연결 테스트 성공", username=username)
        return jsonify({"ok": True, "message": "✅ 내 디스코드 채널로 테스트 알림을 전송했습니다!"})
    return jsonify({"ok": False, "message": "❌ 전송 실패! 디스코드 웹후크 URL을 다시 확인해 주세요."})


@app.route("/api/check-now", methods=["POST"])
@login_required
def api_check_now():
    if _check_now_callback:
        _check_now_callback()
    return jsonify({"ok": True, "message": "🔄 전체 방송인 상태 즉시 점검을 완료했습니다!"})


@app.route("/api/logs", methods=["GET"])
@login_required
def api_logs():
    username = session["username"]
    return jsonify({"logs": get_user_logs(username)})
