"""
치지직 & 네이버 카페 디스코드 슬래시 명령어 봇 모듈 (1차 + 2차 전체 기능).
- 서버 관리 권한(manage_guild) 보유자 전용 슬래시 명령어 제공
- 디스코드 서버(Guild) 단위로 방송인 목록 및 채널별 웹후크 자동 생성·공유
- 1차 명령어: /알림추가, /알림목록, /알림설정, /알림삭제, /미리보기, /지금점검, /도움말
- 2차 명령어: /카페연동 (모달), /쿠키설정 (보안 모달), /팔로우연동 (다중 선택 메뉴 + 알림ON 원클릭 일괄 등록)
"""

import asyncio
import threading
import time
from typing import Any, Callable, Optional

import discord
from discord import app_commands
import requests

import config
from collectors.chzzk import CHZZK_ICON_URL, extract_chzzk_channel_info
from collectors.naver_cafe import _build_cookies, extract_cafe_info_from_url
from collectors.twitter_x import (
    XCookieExpiredError,
    extract_x_user_info,
    parse_x_screen_name,
)
from utils.settings_manager import (
    generate_streamer_id,
    load_guild_settings,
    log_event,
    save_guild_settings,
)
from web_ui import send_preview_for_streamer

WEBHOOK_DEFAULT_NAME = "치지직·카페 알림봇"

_bot_check_now_cb: Optional[Callable[[], None]] = None


def set_bot_check_now_callback(cb: Callable[[], None]) -> None:
    """메인 순찰 루프의 즉시 점검 함수를 봇에 연결합니다."""
    global _bot_check_now_cb
    _bot_check_now_cb = cb


_chzzk_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _search_chzzk_sync(keyword: str) -> list[dict[str, Any]]:
    """치지직 채널 검색 API를 동기 호출하여 상위 결과를 반환합니다 (30초 캐싱 및 Rate-limit 방어)."""
    kw = (keyword or "").strip()
    if not kw:
        return []

    now = time.time()
    cached = _chzzk_search_cache.get(kw.lower())
    if cached and (now - cached[0]) < 30.0:
        return cached[1]

    try:
        resp = requests.get(
            "https://api.chzzk.naver.com/service/v1/search/channels",
            params={"keyword": kw, "offset": 0, "size": 10},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=3,
        )
        data = resp.json()
        raw_list = (data.get("content") or {}).get("data") or []
        results: list[dict[str, Any]] = []
        for item in raw_list:
            ch = item.get("channel") or {}
            cid = ch.get("channelId")
            if not cid:
                continue
            results.append(
                {
                    "channelId": cid,
                    "channelName": ch.get("channelName", "알 수 없음"),
                    "followerCount": ch.get("followerCount", 0),
                    "openLive": bool(ch.get("openLive")),
                }
            )

        # 캐시 정리 (만료된 항목 및 100개 초과 시 오래된 것 제거)
        expired = [k for k, (ts, _) in _chzzk_search_cache.items() if now - ts > 60.0]
        for k in expired:
            _chzzk_search_cache.pop(k, None)
        if len(_chzzk_search_cache) > 100:
            for k in sorted(_chzzk_search_cache.keys(), key=lambda x: _chzzk_search_cache[x][0])[:20]:
                _chzzk_search_cache.pop(k, None)

        _chzzk_search_cache[kw.lower()] = (now, results)
        return results
    except Exception:
        return []


def _fetch_chzzk_followings_sync(
    nid_aut: str, nid_ses: str, existing_cids: set[str]
) -> tuple[bool, str, list[dict[str, Any]]]:
    """네이버 쿠키로 치지직 팔로우 채널 목록(최대 250개)을 동기 조회합니다."""
    cookies = _build_cookies(nid_aut, nid_ses)
    if not cookies.get("NID_AUT") or not cookies.get("NID_SES"):
        return (
            False,
            "네이버 로그인 쿠키(`NID_AUT`, `NID_SES`)가 설정되어 있지 않습니다. 먼저 `/쿠키설정` 명령어로 쿠키를 등록해 주세요!",
            [],
        )

    headers = {"User-Agent": "Mozilla/5.0"}
    all_channels: list[dict[str, Any]] = []
    try:
        for page in range(5):
            url = f"https://api.chzzk.naver.com/service/v1/channels/followings?page={page}&size=50"
            resp = requests.get(url, headers=headers, cookies=cookies, timeout=6)
            if resp.status_code == 401:
                return (
                    False,
                    "네이버 로그인 쿠키가 만료되었거나 올바르지 않습니다. `/쿠키설정`으로 최신 쿠키를 다시 입력해 주세요.",
                    [],
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
                pdata = (ch.get("personalData") or {}).get("following") or {}
                all_channels.append(
                    {
                        "channelId": cid,
                        "channelName": ch.get("channelName", "알 수 없음"),
                        "channelImageUrl": ch.get("channelImageUrl") or CHZZK_ICON_URL,
                        "notification": bool(pdata.get("notification")),
                        "openLive": bool((it.get("streamer") or {}).get("openLive")),
                        "already_added": cid in existing_cids,
                    }
                )
        # 알림 ON 채널이 먼저 오도록 정렬
        all_channels.sort(key=lambda x: (x["already_added"], not x["notification"]))
        return True, "성공", all_channels
    except Exception as e:
        return False, f"팔로우 목록 조회 중 오류가 발생했습니다: {e}", []


def _format_streamer_status_lines(st: dict[str, Any]) -> str:
    """방송인 1명의 알림 설정 요약을 포맷팅합니다."""
    enabled = st.get("enabled", True)
    state_badge = "🟢 감시 중" if enabled else "⏸️ 일시정지"
    ch_id = st.get("discord_channel_id")
    ch_mention = f"<#{ch_id}>" if ch_id else "기본 웹후크"

    chzzk_flags = []
    if st.get("chzzk_channel_id"):
        if st.get("notify_chzzk_open", True):
            chzzk_flags.append("뱅온")
        if st.get("notify_chzzk_close", False):
            chzzk_flags.append("뱅종")
        if st.get("notify_chzzk_title", True):
            chzzk_flags.append("방제")
        if st.get("notify_chzzk_category", True):
            chzzk_flags.append("카테고리")
        if st.get("notify_chzzk_community", True):
            chzzk_flags.append("커뮤니티")
        chzzk_str = ", ".join(chzzk_flags) if chzzk_flags else "꺼짐"
    else:
        chzzk_str = "미연동"

    cafe_flags = []
    if st.get("cafe_club_id") and st.get("cafe_member_key"):
        if st.get("notify_cafe_article", True):
            cafe_flags.append("게시글")
        if st.get("notify_cafe_comment", True):
            cafe_flags.append("댓글")
        cafe_str = ", ".join(cafe_flags) if cafe_flags else "꺼짐"
    else:
        cafe_str = "미연동"

    x_flags = []
    if st.get("x_screen_name"):
        if st.get("notify_x_post", True):
            x_flags.append("포스트")
        if st.get("notify_x_reply", False):
            x_flags.append("답글")
        if st.get("notify_x_retweet", False):
            x_flags.append("리트윗")
        x_str = f"@{st['x_screen_name']} ({', '.join(x_flags) if x_flags else '꺼짐'})"
    else:
        x_str = "미연동"

    mention_str = ""
    if st.get("mention_role_id"):
        mention_str = f" | 🔔 <@&{st['mention_role_id']}>"
    elif st.get("mention_text"):
        mention_str = f" | 🔔 `{st['mention_text']}`"

    return (
        f"**{state_badge}** | 알림 채널: {ch_mention}{mention_str}\n"
        f"• 치지직: `{chzzk_str}` | 네이버 카페: `{cafe_str}` | X: `{x_str}`"
    )


def _build_streamer_config_embed(st: dict[str, Any], guild_name: str) -> discord.Embed:
    """방송인 알림 설정 상태를 보여주는 Embed를 생성합니다."""
    enabled = st.get("enabled", True)
    color = 0x00FFA3 if enabled else 0x71717A
    name = st.get("name", "방송인")
    cid = st.get("chzzk_channel_id", "")
    x_sname = (st.get("x_screen_name") or "").strip()
    if cid:
        url = f"https://chzzk.naver.com/{cid}"
    elif x_sname:
        url = f"https://x.com/{x_sname}"
    else:
        url = None

    embed = discord.Embed(
        title=f"⚙️ {name} 알림 상세 설정",
        url=url,
        description=f"**{guild_name}** 서버의 알림 설정입니다. 아래 버튼을 눌러 실시간으로 켜고 끌 수 있습니다.",
        color=color,
    )
    img = (
        st.get("profile_image_url")
        or st.get("cafe_profile_image_url")
        or st.get("x_profile_image_url")
        or CHZZK_ICON_URL
    )
    if img and img.startswith("http"):
        embed.set_thumbnail(url=img)

    ch_id = st.get("discord_channel_id")
    embed.add_field(
        name="📢 알림 수신 채널",
        value=f"<#{ch_id}>" if ch_id else "서버 기본 웹후크",
        inline=True,
    )
    embed.add_field(
        name="📡 전체 감시 상태",
        value="🟢 작동 중 (ON)" if enabled else "⏸️ 일시정지 (OFF)",
        inline=True,
    )

    mention_role_id = st.get("mention_role_id")
    mention_text = st.get("mention_text")
    if mention_role_id:
        mode_str = " (뱅온 전용)" if st.get("mention_on_open_only", True) else " (모든 알림)"
        mention_desc = f"<@&{mention_role_id}>{mode_str}"
    elif mention_text:
        mode_str = " (뱅온 전용)" if st.get("mention_on_open_only", True) else " (모든 알림)"
        mention_desc = f"`{mention_text}`{mode_str}"
    else:
        mention_desc = "없음 (기본값: 멘션 없음)"

    embed.add_field(
        name="🔔 멘션 호출 역할",
        value=f"{mention_desc}\n*(설정/해제: `/멘션설정`)*",
        inline=True,
    )

    def _onoff(val: bool) -> str:
        return "🟢 켜짐" if val else "⚫ 꺼짐"

    if cid:
        chzzk_desc = (
            f"• 방송 시작(뱅온): **{_onoff(st.get('notify_chzzk_open', True))}**\n"
            f"• 방송 종료(뱅종): **{_onoff(st.get('notify_chzzk_close', False))}**\n"
            f"• 방제 변경: **{_onoff(st.get('notify_chzzk_title', True))}**\n"
            f"• 카테고리 변경: **{_onoff(st.get('notify_chzzk_category', True))}**\n"
            f"• 커뮤니티 새 글: **{_onoff(st.get('notify_chzzk_community', True))}**"
        )
    else:
        chzzk_desc = "미연동 (외부 플랫폼 단독 알림)"
    embed.add_field(name="📺 치지직 알림 항목", value=chzzk_desc, inline=False)

    has_cafe = bool(st.get("cafe_club_id") and st.get("cafe_member_key"))
    if has_cafe:
        cafe_desc = (
            f"• 카페 새 게시글: **{_onoff(st.get('notify_cafe_article', True))}**\n"
            f"• 카페 새 댓글: **{_onoff(st.get('notify_cafe_comment', True))}**\n"
            f"• 연동 정보: `club_id={st.get('cafe_club_id')}`"
        )
    else:
        cafe_desc = "미연동 (`/카페연동` 명령어로 게시글 주소 1개만 넣으면 즉시 연동됩니다)"
    embed.add_field(name="☕ 네이버 카페 알림 항목", value=cafe_desc, inline=False)

    if x_sname:
        x_desc = (
            f"• X 새 포스트: **{_onoff(st.get('notify_x_post', True))}**\n"
            f"• X 새 답글(Reply): **{_onoff(st.get('notify_x_reply', False))}**\n"
            f"• X 리트윗(RT): **{_onoff(st.get('notify_x_retweet', False))}**\n"
            f"• 연동 계정: [@{x_sname}](https://x.com/{x_sname})"
        )
    else:
        x_desc = "미연동 (`/x연동` 명령어로 @아이디 또는 주소를 넣으면 즉시 연동됩니다)"
    embed.add_field(name="🐦 X(구 트위터) 알림 항목", value=x_desc, inline=False)

    embed.set_footer(
        text=f"고유 ID: {st.get('id')} | 치지직 ID: {cid or '없음'} | X: @{x_sname if x_sname else '없음'}"
    )
    return embed


class StreamerConfigView(discord.ui.View):
    """버튼 클릭으로 특정 방송인의 치지직·카페·X 알림 옵션을 즉시 토글하는 인터랙티브 뷰입니다."""

    def __init__(self, guild_id: str, guild_name: str, streamer_id: str) -> None:
        super().__init__(timeout=600)
        self.guild_id = str(guild_id)
        self.guild_name = guild_name
        self.streamer_id = streamer_id
        self._sync_button_styles()

    def _get_streamer(self) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        g_data = load_guild_settings(self.guild_id, self.guild_name)
        for st in g_data.get("streamers", []):
            if st.get("id") == self.streamer_id:
                return g_data, st
        return g_data, None

    def _sync_button_styles(self) -> None:
        _, st = self._get_streamer()
        if not st:
            return

        mapping = [
            ("toggle_open", "notify_chzzk_open", True, "방송 시작"),
            ("toggle_close", "notify_chzzk_close", False, "방송 종료"),
            ("toggle_title", "notify_chzzk_title", True, "방제 변경"),
            ("toggle_category", "notify_chzzk_category", True, "카테고리"),
            ("toggle_community", "notify_chzzk_community", True, "커뮤니티"),
            ("toggle_cafe_art", "notify_cafe_article", True, "카페 게시글"),
            ("toggle_cafe_cmt", "notify_cafe_comment", True, "카페 댓글"),
            ("toggle_enabled", "enabled", True, "전체 감시"),
            ("toggle_x_post", "notify_x_post", True, "X 포스트"),
            ("toggle_x_reply", "notify_x_reply", False, "X 답글"),
            ("toggle_x_retweet", "notify_x_retweet", False, "X 리트윗"),
        ]
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            for cid, key, default_val, label_base in mapping:
                if child.custom_id == cid:
                    is_on = bool(st.get(key, default_val))
                    child.style = (
                        discord.ButtonStyle.success if is_on else discord.ButtonStyle.secondary
                    )
                    prefix = "🟢" if is_on else "⚫"
                    child.label = f"{prefix} {label_base}"
                    break

    async def _handle_toggle(
        self, interaction: discord.Interaction, key: str, default_val: bool
    ) -> None:
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "⚠️ `서버 관리하기` 권한이 있는 관리자만 설정을 변경할 수 있습니다.",
                ephemeral=True,
            )
            return

        g_data, st = self._get_streamer()
        if not st:
            await interaction.response.send_message(
                "⚠️ 해당 방송인 정보를 찾을 수 없습니다 (이미 삭제되었을 수 있습니다).",
                ephemeral=True,
            )
            return

        st[key] = not bool(st.get(key, default_val))
        save_guild_settings(
            self.guild_id,
            guild_name=self.guild_name,
            guild_streamers=g_data["streamers"],
        )
        self._sync_button_styles()
        embed = _build_streamer_config_embed(st, self.guild_name)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="방송 시작", custom_id="toggle_open", row=0)
    async def btn_open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_toggle(interaction, "notify_chzzk_open", True)

    @discord.ui.button(label="방송 종료", custom_id="toggle_close", row=0)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_toggle(interaction, "notify_chzzk_close", False)

    @discord.ui.button(label="방제 변경", custom_id="toggle_title", row=0)
    async def btn_title(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_toggle(interaction, "notify_chzzk_title", True)

    @discord.ui.button(label="카테고리", custom_id="toggle_category", row=1)
    async def btn_category(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_toggle(interaction, "notify_chzzk_category", True)

    @discord.ui.button(label="커뮤니티", custom_id="toggle_community", row=1)
    async def btn_community(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_toggle(interaction, "notify_chzzk_community", True)

    @discord.ui.button(label="카페 게시글", custom_id="toggle_cafe_art", row=2)
    async def btn_cafe_art(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_toggle(interaction, "notify_cafe_article", True)

    @discord.ui.button(label="카페 댓글", custom_id="toggle_cafe_cmt", row=2)
    async def btn_cafe_cmt(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_toggle(interaction, "notify_cafe_comment", True)

    @discord.ui.button(label="전체 감시", custom_id="toggle_enabled", row=2)
    async def btn_enabled(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_toggle(interaction, "enabled", True)

    @discord.ui.button(label="X 포스트", custom_id="toggle_x_post", row=3)
    async def btn_x_post(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_toggle(interaction, "notify_x_post", True)

    @discord.ui.button(label="X 답글", custom_id="toggle_x_reply", row=3)
    async def btn_x_reply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_toggle(interaction, "notify_x_reply", False)

    @discord.ui.button(label="X 리트윗", custom_id="toggle_x_retweet", row=3)
    async def btn_x_retweet(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_toggle(interaction, "notify_x_retweet", False)


# ------------------------------------------------------------------------------
# 2차 기능 모달 & 뷰: /쿠키설정, /카페연동, /팔로우연동
# ------------------------------------------------------------------------------
class NaverCookieModal(discord.ui.Modal, title="🔐 네이버 로그인 쿠키 보안 설정"):
    """채팅방에 노출되지 않는 보안 모달 창으로 NID_AUT, NID_SES를 입력받습니다."""

    nid_aut_input = discord.ui.TextInput(
        label="네이버 쿠키: NID_AUT",
        style=discord.TextStyle.short,
        placeholder="크롬 F12 -> Application -> Cookies 에서 복사한 NID_AUT 값",
        required=True,
        max_length=500,
    )
    nid_ses_input = discord.ui.TextInput(
        label="네이버 쿠키: NID_SES",
        style=discord.TextStyle.paragraph,
        placeholder="크롬 F12 -> Application -> Cookies 에서 복사한 NID_SES 값",
        required=True,
        max_length=2000,
    )

    def __init__(self, guild_id: str, guild_name: str, default_aut: str = "", default_ses: str = "") -> None:
        super().__init__()
        self.guild_id = str(guild_id)
        self.guild_name = guild_name
        if default_aut:
            self.nid_aut_input.default = default_aut
        if default_ses:
            self.nid_ses_input.default = default_ses

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        aut = self.nid_aut_input.value.strip()
        ses = self.nid_ses_input.value.strip()

        g_data = load_guild_settings(self.guild_id, self.guild_name)
        g_cfg = g_data.get("global") or {}
        g_cfg["naver_nid_aut"] = aut
        g_cfg["naver_nid_ses"] = ses
        save_guild_settings(self.guild_id, guild_name=self.guild_name, guild_global=g_cfg)

        ok, msg, channels = await asyncio.to_thread(
            _fetch_chzzk_followings_sync, aut, ses, set()
        )
        log_event(f"[디스코드 봇:{self.guild_name}] 네이버 로그인 쿠키 업데이트 완료 (인증확인: {ok})")

        if ok:
            await interaction.followup.send(
                f"✅ **네이버 로그인 쿠키가 안전하게 저장되었습니다!** (이 메시지는 관리자 본인에게만 보입니다)\n"
                f"• 치지직 계정 연동 확인 완료: 현재 팔로우 중인 채널 **{len(channels)}개**가 감지되었습니다.\n"
                f"• 이제 `/팔로우연동` 또는 `/카페연동` 명령어를 바로 사용하실 수 있습니다!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ 쿠키는 저장되었으나 치지직 팔로우 조회 테스트에서 경고가 발생했습니다:\n> {msg}\n"
                f"쿠키 값을 정확히 복사했는지 확인해 주세요.",
                ephemeral=True,
            )


class CafeLinkModal(discord.ui.Modal, title="☕ 네이버 카페 알림 연동"):
    """방송인이 작성한 카페 게시글 URL 1개를 입력받아 카페 ID와 멤버키를 자동 추출·연동합니다."""

    article_url_input = discord.ui.TextInput(
        label="방송인이 작성한 네이버 카페 게시글 링크 1개",
        style=discord.TextStyle.short,
        placeholder="예: https://cafe.naver.com/2chamcham2/59153",
        required=True,
        max_length=500,
    )
    custom_name_input = discord.ui.TextInput(
        label="방송인 표시 이름 (선택 - 비워두면 자동 설정)",
        style=discord.TextStyle.short,
        placeholder="비워두면 기존 이름 또는 카페 작성자 닉네임이 사용됩니다",
        required=False,
        max_length=100,
    )

    def __init__(
        self,
        guild_id: str,
        guild_name: str,
        target_channel: discord.TextChannel,
        existing_streamer: Optional[dict[str, Any]] = None,
        initial_name: str = "",
    ) -> None:
        super().__init__()
        self.guild_id = str(guild_id)
        self.guild_name = guild_name
        self.target_channel = target_channel
        self.existing_streamer = existing_streamer
        if existing_streamer:
            self.custom_name_input.default = existing_streamer.get("name", "")
            if existing_streamer.get("cafe_article_url"):
                self.article_url_input.default = existing_streamer.get("cafe_article_url", "")
        elif initial_name:
            self.custom_name_input.default = initial_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)

        article_url = self.article_url_input.value.strip()
        custom_name = self.custom_name_input.value.strip()

        g_data = load_guild_settings(self.guild_id, self.guild_name)
        g_cfg = g_data.get("global") or {}
        nid_aut = (g_cfg.get("naver_nid_aut") or "").strip() or None
        nid_ses = (g_cfg.get("naver_nid_ses") or "").strip() or None

        info = await asyncio.to_thread(
            extract_cafe_info_from_url, article_url, nid_aut, nid_ses
        )
        if not info:
            await interaction.followup.send(
                "❌ **카페 게시글에서 작성자 정보를 추출하지 못했습니다.**\n"
                "1. 게시글 주소가 정확한지 확인해 주세요 (예: `https://cafe.naver.com/카페주소/글번호`).\n"
                "2. 회원 공개 게시글이라면 먼저 `/쿠키설정` 명령어로 네이버 로그인 쿠키(`NID_AUT`, `NID_SES`)를 등록해 주세요!"
            )
            return

        try:
            webhook_url = await _ensure_channel_webhook(
                self.target_channel, self.guild_id, self.guild_name
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ 봇에게 {self.target_channel.mention} 채널의 **웹후크 관리하기(Manage Webhooks)** 권한이 없습니다."
            )
            return
        except Exception as e:
            await interaction.followup.send(f"❌ 채널 웹후크 생성 중 오류가 발생했습니다: `{e}`")
            return

        streamers: list[dict[str, Any]] = g_data.get("streamers") or []
        club_id = info["club_id"]
        member_key = info["member_key"]
        writer_nick = info.get("nick") or "방송인"
        writer_img = info.get("profile_image_url") or CHZZK_ICON_URL

        target_st = None
        if self.existing_streamer:
            target_st = next(
                (s for s in streamers if s.get("id") == self.existing_streamer.get("id")),
                None,
            )

        if target_st:
            if custom_name:
                target_st["name"] = custom_name
            target_st["cafe_article_url"] = article_url
            target_st["cafe_club_id"] = club_id
            target_st["cafe_member_key"] = member_key
            if writer_img:
                target_st["cafe_profile_image_url"] = writer_img
                if not target_st.get("profile_image_url"):
                    target_st["profile_image_url"] = writer_img
            target_st["notify_cafe_article"] = True
            target_st["notify_cafe_comment"] = True
            if not target_st.get("custom_webhook_url"):
                target_st["custom_webhook_url"] = webhook_url
                target_st["discord_channel_id"] = str(self.target_channel.id)
                target_st["discord_channel_name"] = f"#{self.target_channel.name}"
            action_msg = (
                f"☕ **{target_st['name']}** 방송인에 네이버 카페 알림(작성자: `{writer_nick}`)이 연동되었습니다!"
            )
        else:
            st_name = custom_name or writer_nick
            target_st = {
                "id": generate_streamer_id(),
                "enabled": True,
                "name": st_name,
                "profile_image_url": writer_img,
                "cafe_profile_image_url": writer_img,
                "chzzk_input": "",
                "chzzk_channel_id": "",
                "cafe_article_url": article_url,
                "cafe_club_id": club_id,
                "cafe_member_key": member_key,
                "x_input": "",
                "x_screen_name": "",
                "x_user_id": "",
                "x_profile_image_url": "",
                "custom_webhook_url": webhook_url,
                "discord_channel_id": str(self.target_channel.id),
                "discord_channel_name": f"#{self.target_channel.name}",
                "notify_chzzk_open": False,
                "notify_chzzk_close": False,
                "notify_chzzk_title": False,
                "notify_chzzk_category": False,
                "notify_chzzk_community": False,
                "notify_cafe_article": True,
                "notify_cafe_comment": True,
                "notify_x_post": False,
                "notify_x_reply": False,
                "notify_x_retweet": False,
                "mention_role_id": "",
                "mention_role_name": "",
                "mention_on_open_only": True,
            }
            streamers.append(target_st)
            action_msg = (
                f"🎉 **{st_name}** 네이버 카페 단독 알림(작성자: `{writer_nick}`)이 "
                f"{self.target_channel.mention} 채널에 등록되었습니다!"
            )

        save_guild_settings(self.guild_id, guild_name=self.guild_name, guild_streamers=streamers)
        log_event(
            f"[디스코드 봇:{self.guild_name}] '{target_st['name']}' 카페 연동 완료 (club_id={club_id})"
        )

        embed = _build_streamer_config_embed(target_st, self.guild_name)
        view = StreamerConfigView(self.guild_id, self.guild_name, target_st["id"])
        await interaction.followup.send(content=action_msg, embed=embed, view=view)


class XCookieModal(discord.ui.Modal, title="🔐 X(구 트위터) 부계정 쿠키 보안 설정"):
    """채팅방에 노출되지 않는 보안 모달 창으로 X의 auth_token, ct0 쿠키를 입력받습니다."""

    x_auth_token_input = discord.ui.TextInput(
        label="X(트위터) 쿠키: auth_token",
        style=discord.TextStyle.short,
        placeholder="x.com 로그인 후 F12 -> Application -> Cookies 에서 복사한 auth_token",
        required=True,
        max_length=500,
    )
    x_ct0_input = discord.ui.TextInput(
        label="X(트위터) 쿠키: ct0",
        style=discord.TextStyle.paragraph,
        placeholder="x.com 로그인 후 F12 -> Application -> Cookies 에서 복사한 ct0",
        required=True,
        max_length=1000,
    )

    def __init__(
        self,
        guild_id: str,
        guild_name: str,
        default_auth_token: str = "",
        default_ct0: str = "",
    ) -> None:
        super().__init__()
        self.guild_id = str(guild_id)
        self.guild_name = guild_name
        if default_auth_token:
            self.x_auth_token_input.default = default_auth_token
        if default_ct0:
            self.x_ct0_input.default = default_ct0

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        auth_token = self.x_auth_token_input.value.strip()
        ct0 = self.x_ct0_input.value.strip()

        g_data = load_guild_settings(self.guild_id, self.guild_name)
        g_cfg = g_data.get("global") or {}
        g_cfg["x_auth_token"] = auth_token
        g_cfg["x_ct0"] = ct0
        save_guild_settings(self.guild_id, guild_name=self.guild_name, guild_global=g_cfg)

        verified = False
        err_detail = ""
        try:
            test_info = await asyncio.to_thread(
                extract_x_user_info, "X", auth_token, ct0
            )
            verified = bool(test_info and test_info.get("user_id"))
        except XCookieExpiredError as e:
            err_detail = f"인증 거부됨 ({e})"
        except Exception as e:
            err_detail = str(e)

        log_event(
            f"[디스코드 봇:{self.guild_name}] X(트위터) 로그인 쿠키 업데이트 완료 (인증확인: {verified})"
        )

        if verified:
            await interaction.followup.send(
                "✅ **X(구 트위터) 부계정 쿠키가 안전하게 암호화 저장되었습니다!** (이 메시지는 관리자 본인에게만 보입니다)\n"
                "• X GraphQL API 인증 연동이 정상 확인되었습니다.\n"
                "• 이제 `/x연동` 명령어로 기존 방송인에 X를 연결하거나 X 단독 알림을 등록하실 수 있습니다!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ 쿠키는 저장되었으나 X 계정 조회 테스트에서 확인되지 않았습니다.{f' (`{err_detail}`)' if err_detail else ''}\n"
                "• `auth_token`과 `ct0` 값을 정확히 복사했는지 확인해 주세요.",
                ephemeral=True,
            )


class XLinkModal(discord.ui.Modal, title="🐦 X(구 트위터) 알림 연동"):
    """방송인의 X 아이디(@핸들) 또는 프로필/포스트 주소를 입력받아 자동 연동합니다."""

    x_handle_input = discord.ui.TextInput(
        label="X(트위터) @아이디 또는 프로필/포스트 URL",
        style=discord.TextStyle.short,
        placeholder="예: @2chamcham2 또는 https://x.com/2chamcham2",
        required=True,
        max_length=300,
    )
    custom_name_input = discord.ui.TextInput(
        label="방송인 표시 이름 (선택 - 비워두면 자동 설정)",
        style=discord.TextStyle.short,
        placeholder="비워두면 기존 이름 또는 X 프로필 닉네임이 사용됩니다",
        required=False,
        max_length=100,
    )

    def __init__(
        self,
        guild_id: str,
        guild_name: str,
        target_channel: discord.TextChannel,
        existing_streamer: Optional[dict[str, Any]] = None,
        initial_name: str = "",
    ) -> None:
        super().__init__()
        self.guild_id = str(guild_id)
        self.guild_name = guild_name
        self.target_channel = target_channel
        self.existing_streamer = existing_streamer
        if existing_streamer:
            self.custom_name_input.default = existing_streamer.get("name", "")
            if existing_streamer.get("x_screen_name"):
                self.x_handle_input.default = f"@{existing_streamer.get('x_screen_name')}"
        elif initial_name:
            self.custom_name_input.default = initial_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)

        raw_x = self.x_handle_input.value.strip()
        custom_name = self.custom_name_input.value.strip()

        parsed_handle = parse_x_screen_name(raw_x)
        if not parsed_handle:
            await interaction.followup.send(
                "❌ **유효한 X(트위터) 아이디 또는 주소 형식이 아닙니다.**\n"
                "예: `@아이디`, `아이디`, 또는 `https://x.com/아이디` 형태로 입력해 주세요."
            )
            return

        g_data = load_guild_settings(self.guild_id, self.guild_name)
        g_cfg = g_data.get("global") or {}
        x_auth_token = (g_cfg.get("x_auth_token") or config.X_AUTH_TOKEN or "").strip() or None
        x_ct0 = (g_cfg.get("x_ct0") or config.X_CT0 or "").strip() or None

        if not x_auth_token or not x_ct0:
            await interaction.followup.send(
                "⚠️ **X(트위터) 부계정 로그인 쿠키(`auth_token`, `ct0`)가 아직 설정되지 않았습니다.**\n"
                "먼저 `/x쿠키설정` 명령어(또는 웹 대시보드 상단 설정)에서 X 부계정 쿠키를 등록한 뒤 다시 시도해 주세요!"
            )
            return

        try:
            info = await asyncio.to_thread(
                extract_x_user_info, raw_x, x_auth_token, x_ct0
            )
        except XCookieExpiredError:
            await interaction.followup.send(
                "❌ **X(트위터) 로그인 쿠키가 만료되었거나 유효하지 않습니다.**\n"
                "`/x쿠키설정` 명령어로 최신 `auth_token`과 `ct0` 쿠키를 다시 등록해 주세요."
            )
            return

        if not info:
            await interaction.followup.send(
                f"❌ **X에서 `@{parsed_handle}` 계정을 찾지 못했습니다.**\n"
                "아이디 철자가 정확한지, 또는 정지/비공개 계정인지 확인해 주세요."
            )
            return

        try:
            webhook_url = await _ensure_channel_webhook(
                self.target_channel, self.guild_id, self.guild_name
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ 봇에게 {self.target_channel.mention} 채널의 **웹후크 관리하기(Manage Webhooks)** 권한이 없습니다."
            )
            return
        except Exception as e:
            await interaction.followup.send(f"❌ 채널 웹후크 생성 중 오류가 발생했습니다: `{e}`")
            return

        streamers: list[dict[str, Any]] = g_data.get("streamers") or []
        x_uid = info["user_id"]
        x_sname = info["screen_name"]
        x_nick = info.get("name") or x_sname
        x_img = info.get("profile_image_url") or CHZZK_ICON_URL

        target_st = None
        if self.existing_streamer:
            target_st = next(
                (s for s in streamers if s.get("id") == self.existing_streamer.get("id")),
                None,
            )

        if target_st:
            if custom_name:
                target_st["name"] = custom_name
            target_st["x_input"] = raw_x
            target_st["x_screen_name"] = x_sname
            target_st["x_user_id"] = x_uid
            if x_img:
                target_st["x_profile_image_url"] = x_img
                if not target_st.get("profile_image_url"):
                    target_st["profile_image_url"] = x_img
            target_st["notify_x_post"] = True
            target_st.setdefault("notify_x_reply", False)
            target_st.setdefault("notify_x_retweet", False)
            if not target_st.get("custom_webhook_url"):
                target_st["custom_webhook_url"] = webhook_url
                target_st["discord_channel_id"] = str(self.target_channel.id)
                target_st["discord_channel_name"] = f"#{self.target_channel.name}"
            action_msg = (
                f"🐦 **{target_st['name']}** 방송인에 X(트위터) 알림(`@{x_sname}`)이 연동되었습니다!"
            )
        else:
            st_name = custom_name or x_nick
            target_st = {
                "id": generate_streamer_id(),
                "enabled": True,
                "name": st_name,
                "profile_image_url": x_img,
                "cafe_profile_image_url": "",
                "chzzk_input": "",
                "chzzk_channel_id": "",
                "cafe_article_url": "",
                "cafe_club_id": "",
                "cafe_member_key": "",
                "x_input": raw_x,
                "x_screen_name": x_sname,
                "x_user_id": x_uid,
                "x_profile_image_url": x_img,
                "custom_webhook_url": webhook_url,
                "discord_channel_id": str(self.target_channel.id),
                "discord_channel_name": f"#{self.target_channel.name}",
                "notify_chzzk_open": False,
                "notify_chzzk_close": False,
                "notify_chzzk_title": False,
                "notify_chzzk_category": False,
                "notify_chzzk_community": False,
                "notify_cafe_article": False,
                "notify_cafe_comment": False,
                "notify_x_post": True,
                "notify_x_reply": False,
                "notify_x_retweet": False,
                "mention_role_id": "",
                "mention_role_name": "",
                "mention_on_open_only": True,
            }
            streamers.append(target_st)
            action_msg = (
                f"🎉 **{st_name}** (`@{x_sname}`) X(트위터) 단독 알림이 "
                f"{self.target_channel.mention} 채널에 등록되었습니다!"
            )

        save_guild_settings(self.guild_id, guild_name=self.guild_name, guild_streamers=streamers)
        log_event(
            f"[디스코드 봇:{self.guild_name}] '{target_st['name']}' X 연동 완료 (@{x_sname}, id={x_uid})"
        )

        embed = _build_streamer_config_embed(target_st, self.guild_name)
        view = StreamerConfigView(self.guild_id, self.guild_name, target_st["id"])
        await interaction.followup.send(content=action_msg, embed=embed, view=view)


class FollowImportView(discord.ui.View):
    """
    치지직 팔로우 목록에서:
    1) '🔔 알림 ON 채널 전체 일괄 등록' 원클릭 버튼
    2) 페이지별 최대 25개 다중 선택 드롭다운(Select)
    을 제공하는 인터랙티브 뷰입니다.
    """

    PAGE_SIZE = 25

    def __init__(
        self,
        guild_id: str,
        guild_name: str,
        target_channel: discord.TextChannel,
        webhook_url: str,
        channels: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=600)
        self.guild_id = str(guild_id)
        self.guild_name = guild_name
        self.target_channel = target_channel
        self.webhook_url = webhook_url
        self.all_channels = channels
        self.page = 0
        self._rebuild_components()

    def _get_unadded_channels(self) -> list[dict[str, Any]]:
        return [c for c in self.all_channels if not c.get("already_added")]

    def _get_notif_on_unadded(self) -> list[dict[str, Any]]:
        return [
            c
            for c in self.all_channels
            if not c.get("already_added") and c.get("notification")
        ]

    def build_embed(self) -> discord.Embed:
        unadded = self._get_unadded_channels()
        notif_on = self._get_notif_on_unadded()
        already_cnt = len(self.all_channels) - len(unadded)
        total_pages = max(1, (len(unadded) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

        embed = discord.Embed(
            title="📺 치지직 팔로우 채널 일괄 연동",
            description=(
                f"알림을 받을 대상 채널: {self.target_channel.mention}\n"
                f"• 전체 팔로우: **{len(self.all_channels)}명** (이미 등록됨: **{already_cnt}명**)\n"
                f"• 🔔 치지직 알림 켜둔 미등록 채널: **{len(notif_on)}명**\n\n"
                "👉 아래 **[🔔 알림 ON 채널 일괄 등록]** 버튼을 누르거나, 드롭다운 메뉴에서 원하는 방송인을 여러 명 골라 한 번에 추가하세요!"
            ),
            color=0x00FFA3,
        )
        embed.set_footer(
            text=f"페이지 {self.page + 1} / {total_pages} (페이지당 최대 25명 표시)"
        )
        return embed

    def _rebuild_components(self) -> None:
        self.clear_items()
        unadded = self._get_unadded_channels()
        notif_on = self._get_notif_on_unadded()
        total_pages = max(1, (len(unadded) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self.page >= total_pages:
            self.page = max(0, total_pages - 1)

        start = self.page * self.PAGE_SIZE
        page_items = unadded[start : start + self.PAGE_SIZE]

        if page_items:
            options: list[discord.SelectOption] = []
            for ch in page_items:
                cid = ch["channelId"]
                name = ch["channelName"]
                notif_icon = "🔔" if ch.get("notification") else "🔕"
                live_str = "🔴LIVE | " if ch.get("openLive") else ""
                desc = f"{live_str}{'치지직 알림 ON' if ch.get('notification') else '치지직 알림 OFF'}"
                options.append(
                    discord.SelectOption(
                        label=f"{notif_icon} {name}"[:100],
                        value=cid,
                        description=desc[:100],
                    )
                )

            select = discord.ui.Select(
                placeholder=f"여기서 추가할 방송인을 선택하세요 ({start + 1}~{start + len(page_items)}번째)",
                min_values=1,
                max_values=len(options),
                options=options,
                row=0,
            )
            select.callback = self._on_select_channels  # type: ignore[method-assign]
            self.add_item(select)

        btn_notif_all = discord.ui.Button(
            label=f"🔔 알림 ON 채널 일괄 등록 ({len(notif_on)}명)",
            style=discord.ButtonStyle.success,
            disabled=(len(notif_on) == 0),
            row=1,
        )
        btn_notif_all.callback = self._on_click_notif_all  # type: ignore[method-assign]
        self.add_item(btn_notif_all)

        if total_pages > 1:
            btn_prev = discord.ui.Button(
                label="◀ 이전 페이지",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page <= 0),
                row=1,
            )
            btn_prev.callback = self._on_prev_page  # type: ignore[method-assign]
            self.add_item(btn_prev)

            btn_next = discord.ui.Button(
                label="다음 페이지 ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page >= total_pages - 1),
                row=1,
            )
            btn_next.callback = self._on_next_page  # type: ignore[method-assign]
            self.add_item(btn_next)

    async def _register_batch(
        self, interaction: discord.Interaction, target_cids: set[str]
    ) -> None:
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "⚠️ `서버 관리하기` 권한이 있는 관리자만 실행할 수 있습니다.",
                ephemeral=True,
            )
            return

        g_data = load_guild_settings(self.guild_id, self.guild_name)
        streamers: list[dict[str, Any]] = g_data.get("streamers") or []
        existing_cids = {
            s.get("chzzk_channel_id") for s in streamers if s.get("chzzk_channel_id")
        }

        added_names: list[str] = []
        for ch in self.all_channels:
            cid = ch["channelId"]
            if cid not in target_cids or cid in existing_cids:
                continue
            st_name = ch["channelName"]
            st_img = ch.get("channelImageUrl") or CHZZK_ICON_URL
            streamers.append(
                {
                    "id": generate_streamer_id(),
                    "enabled": True,
                    "name": st_name,
                    "profile_image_url": st_img,
                    "cafe_profile_image_url": st_img,
                    "chzzk_input": cid,
                    "chzzk_channel_id": cid,
                    "cafe_article_url": "",
                    "cafe_club_id": "",
                    "cafe_member_key": "",
                    "x_input": "",
                    "x_screen_name": "",
                    "x_user_id": "",
                    "x_profile_image_url": "",
                    "custom_webhook_url": self.webhook_url,
                    "discord_channel_id": str(self.target_channel.id),
                    "discord_channel_name": f"#{self.target_channel.name}",
                    "notify_chzzk_open": True,
                    "notify_chzzk_close": False,
                    "notify_chzzk_title": True,
                    "notify_chzzk_category": True,
                    "notify_chzzk_community": True,
                    "notify_cafe_article": False,
                    "notify_cafe_comment": False,
                    "notify_x_post": False,
                    "notify_x_reply": False,
                    "notify_x_retweet": False,
                    "mention_role_id": "",
                    "mention_role_name": "",
                    "mention_on_open_only": True,
                }
            )
            existing_cids.add(cid)
            ch["already_added"] = True
            added_names.append(st_name)

        if added_names:
            save_guild_settings(
                self.guild_id, guild_name=self.guild_name, guild_streamers=streamers
            )
            log_event(
                f"[디스코드 봇:{self.guild_name}] 팔로우 채널 {len(added_names)}명 일괄 등록 완료"
            )

        self._rebuild_components()
        embed = self.build_embed()
        names_preview = ", ".join(added_names[:15])
        if len(added_names) > 15:
            names_preview += f" 외 {len(added_names) - 15}명"

        await interaction.response.edit_message(
            content=(
                f"🎉 **{len(added_names)}명**의 방송인이 {self.target_channel.mention} 채널에 일괄 등록되었습니다!\n"
                f"• 등록된 방송인: `{names_preview}`"
                if added_names
                else "ℹ️ 선택하신 방송인은 이미 등록되어 있습니다."
            ),
            embed=embed,
            view=self,
        )

    async def _on_select_channels(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []  # type: ignore[union-attr]
        await self._register_batch(interaction, set(values))

    async def _on_click_notif_all(self, interaction: discord.Interaction) -> None:
        cids = {c["channelId"] for c in self._get_notif_on_unadded()}
        await self._register_batch(interaction, cids)

    async def _on_prev_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_next_page(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class NotifierDiscordBot(discord.Client):
    """슬래시 명령어를 처리하는 디스코드 봇 클라이언트입니다."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        try:
            synced = await self.tree.sync()
            log_event(f"🤖 [디스코드 봇] 슬래시 명령어 {len(synced)}개 전역 동기화 완료")
        except Exception as e:
            log_event(f"[디스코드 봇 경고] 슬래시 명령어 동기화 실패: {e}")

    async def on_ready(self) -> None:
        if self.user:
            if not self.user.avatar:
                try:
                    img_bytes = await asyncio.to_thread(
                        lambda: requests.get(CHZZK_ICON_URL, timeout=5).content
                    )
                    await self.user.edit(avatar=img_bytes)
                except Exception:
                    pass
            invite_url = (
                f"https://discord.com/oauth2/authorize?client_id={self.user.id}"
                f"&permissions=536890368&scope=bot+applications.commands"
            )
            log_event(
                f"🤖 [디스코드 봇 가동 완료] 로그인 계정: {self.user} (참여 서버: {len(self.guilds)}개)"
            )
            log_event(f"🔗 [봇 초대 링크] {invite_url}")


bot_client = NotifierDiscordBot()


def _verify_admin_interaction(interaction: discord.Interaction) -> Optional[str]:
    """서버 내 실행 여부 및 '서버 관리하기(manage_guild)' 권한을 검증합니다."""
    if not interaction.guild or not interaction.guild_id:
        return "⚠️ 이 명령어는 디스코드 서버 채널 안에서만 사용할 수 있습니다."
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not perms.manage_guild:
        return "⚠️ 이 명령어는 **서버 관리하기(Manage Server)** 권한이 있는 관리자만 사용할 수 있습니다."
    return None


async def _ensure_channel_webhook(
    channel: discord.TextChannel, guild_id: str, guild_name: str
) -> str:
    """
    대상 텍스트 채널에 알림 전송용 웹후크가 있으면 재사용하고,
    없으면 봇이 자동으로 새 웹후크를 생성하여 URL을 반환합니다.
    """
    g_data = load_guild_settings(guild_id, guild_name)
    ch_webhooks: dict[str, str] = g_data.get("channel_webhooks") or {}
    ch_key = str(channel.id)

    webhooks = await channel.webhooks()
    saved_url = ch_webhooks.get(ch_key)
    if saved_url:
        for wh in webhooks:
            if wh.url == saved_url:
                return saved_url

    for wh in webhooks:
        if (bot_client.user and wh.user and wh.user.id == bot_client.user.id and wh.url) or (
            wh.name == WEBHOOK_DEFAULT_NAME and wh.url
        ):
            ch_webhooks[ch_key] = wh.url
            save_guild_settings(guild_id, guild_name=guild_name, channel_webhooks=ch_webhooks)
            return wh.url

    avatar_bytes: Optional[bytes] = None
    try:
        avatar_bytes = await asyncio.to_thread(
            lambda: requests.get(CHZZK_ICON_URL, timeout=5).content
        )
    except Exception:
        avatar_bytes = None

    new_wh = await channel.create_webhook(
        name=WEBHOOK_DEFAULT_NAME,
        avatar=avatar_bytes,
        reason="치지직·네이버 카페 디스코드 알림봇 자동 연동",
    )
    ch_webhooks[ch_key] = new_wh.url
    save_guild_settings(guild_id, guild_name=guild_name, channel_webhooks=ch_webhooks)
    return new_wh.url


# ------------------------------------------------------------------------------
# 자동완성 (Autocomplete) 핸들러
# ------------------------------------------------------------------------------
async def _autocomplete_chzzk_search(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    kw = (current or "").strip()
    if not kw:
        return []
    results = await asyncio.to_thread(_search_chzzk_sync, kw)
    choices: list[app_commands.Choice[str]] = []
    for ch in results[:25]:
        name = ch["channelName"]
        cid = ch["channelId"]
        followers = ch["followerCount"]
        f_str = f"{followers / 10000:.1f}만명" if followers >= 10000 else f"{followers}명"
        live_tag = "🔴LIVE " if ch["openLive"] else ""
        label = f"{live_tag}{name} (팔로워 {f_str})"
        choices.append(app_commands.Choice(name=label[:100], value=cid))
    return choices


async def _autocomplete_registered_streamers(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if not interaction.guild_id:
        return []
    g_data = load_guild_settings(
        str(interaction.guild_id), interaction.guild.name if interaction.guild else ""
    )
    streamers = g_data.get("streamers") or []
    kw = (current or "").strip().lower()
    choices: list[app_commands.Choice[str]] = []
    for st in streamers:
        name = st.get("name", "방송인")
        st_id = st.get("id", "")
        if not st_id:
            continue
        if kw and kw not in name.lower():
            continue
        ch_id = st.get("discord_channel_id")
        ch_name = st.get("discord_channel_name") or (f"#{ch_id}" if ch_id else "기본채널")
        state = "🟢" if st.get("enabled", True) else "⏸️"
        label = f"{state} {name} ({ch_name})"
        choices.append(app_commands.Choice(name=label[:100], value=st_id))
    return choices[:25]


# ------------------------------------------------------------------------------
# 슬래시 명령어 정의 (1차 + 2차 전체 기능)
# ------------------------------------------------------------------------------
@bot_client.tree.command(
    name="알림추가",
    description="치지직 방송인을 검색하여 현재 채널(또는 지정한 채널)에 알림을 등록합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    이름="방송인 이름을 입력하면 자동완성 목록이 뜹니다 (또는 32자리 치지직 채널 ID 입력)",
    채널="알림을 받을 디스코드 채널 (비워두면 현재 채널에 등록됩니다)",
    멘션역할="알림 시 멘션할 디스코드 역할 (선택, 비워두면 멘션 없음)",
)
@app_commands.autocomplete(이름=_autocomplete_chzzk_search)
async def cmd_add_streamer(
    interaction: discord.Interaction,
    이름: str,
    채널: Optional[discord.TextChannel] = None,
    멘션역할: Optional[discord.Role] = None,
) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    target_channel = 채널 or (
        interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
    )
    if not target_channel:
        await interaction.response.send_message(
            "⚠️ 일반 텍스트 채널에서 실행하거나 `채널` 옵션을 지정해 주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=False)

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id

    c_info = await asyncio.to_thread(extract_chzzk_channel_info, 이름.strip())
    if not c_info:
        await interaction.followup.send(
            f"❌ `{이름}`에 해당하는 치지직 채널을 찾지 못했습니다. 정확한 이름이나 채널 코드를 입력해 주세요."
        )
        return

    try:
        webhook_url = await _ensure_channel_webhook(target_channel, guild_id, guild_name)
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ 봇에게 {target_channel.mention} 채널의 **웹후크 관리하기(Manage Webhooks)** 권한이 없습니다.\n"
            "서버 설정 또는 채널 권한에서 봇에게 `웹후크 관리` 권한을 부여한 뒤 다시 시도해 주세요!"
        )
        return
    except Exception as e:
        await interaction.followup.send(f"❌ 채널 웹후크 생성 중 오류가 발생했습니다: `{e}`")
        return

    g_data = load_guild_settings(guild_id, guild_name)
    streamers: list[dict[str, Any]] = g_data.get("streamers") or []
    cid = c_info["channel_id"]
    st_name = c_info["name"]
    st_img = c_info.get("profile_image_url") or CHZZK_ICON_URL

    existing = next((s for s in streamers if s.get("chzzk_channel_id") == cid), None)
    if existing:
        existing["name"] = st_name
        existing["profile_image_url"] = st_img
        existing["custom_webhook_url"] = webhook_url
        existing["discord_channel_id"] = str(target_channel.id)
        existing["discord_channel_name"] = f"#{target_channel.name}"
        existing["enabled"] = True
        if 멘션역할:
            existing["mention_role_id"] = str(멘션역할.id)
            existing["mention_role_name"] = f"@{멘션역할.name}"
            existing["mention_on_open_only"] = True
        target_st = existing
        action_title = f"✅ **{st_name}** 알림 채널이 {target_channel.mention}(으)로 업데이트되었습니다!"
    else:
        target_st = {
            "id": generate_streamer_id(),
            "enabled": True,
            "name": st_name,
            "profile_image_url": st_img,
            "cafe_profile_image_url": st_img,
            "chzzk_input": cid,
            "chzzk_channel_id": cid,
            "cafe_article_url": "",
            "cafe_club_id": "",
            "cafe_member_key": "",
            "x_input": "",
            "x_screen_name": "",
            "x_user_id": "",
            "x_profile_image_url": "",
            "custom_webhook_url": webhook_url,
            "discord_channel_id": str(target_channel.id),
            "discord_channel_name": f"#{target_channel.name}",
            "notify_chzzk_open": True,
            "notify_chzzk_close": False,
            "notify_chzzk_title": True,
            "notify_chzzk_category": True,
            "notify_chzzk_community": True,
            "notify_cafe_article": False,
            "notify_cafe_comment": False,
            "notify_x_post": False,
            "notify_x_reply": False,
            "notify_x_retweet": False,
            "mention_role_id": str(멘션역할.id) if 멘션역할 else "",
            "mention_role_name": f"@{멘션역할.name}" if 멘션역할 else "",
            "mention_on_open_only": True,
        }
        streamers.append(target_st)
        action_title = f"🎉 **{st_name}** 치지직 알림이 {target_channel.mention} 채널에 등록되었습니다!"

    save_guild_settings(guild_id, guild_name=guild_name, guild_streamers=streamers)
    log_event(
        f"[디스코드 봇:{guild_name}] '{st_name}' 알림 등록/수정 완료 (#{target_channel.name})"
    )

    embed = _build_streamer_config_embed(target_st, guild_name)
    view = StreamerConfigView(guild_id, guild_name, target_st["id"])
    await interaction.followup.send(content=action_title, embed=embed, view=view)


@bot_client.tree.command(
    name="카페연동",
    description="네이버 카페 게시글 링크 1개로 방송인의 카페 새 글·댓글 알림을 연동하거나 카페 단독 알림을 추가합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    이름="기존 등록된 방송인에 연동하려면 선택하세요 (비워두면 카페 단독 알림으로 새로 추가됩니다)",
    채널="알림을 받을 디스코드 채널 (비워두면 현재 채널에 등록됩니다)",
)
@app_commands.autocomplete(이름=_autocomplete_registered_streamers)
async def cmd_link_cafe(
    interaction: discord.Interaction,
    이름: Optional[str] = None,
    채널: Optional[discord.TextChannel] = None,
) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    target_channel = 채널 or (
        interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
    )
    if not target_channel:
        await interaction.response.send_message(
            "⚠️ 일반 텍스트 채널에서 실행하거나 `채널` 옵션을 지정해 주세요.",
            ephemeral=True,
        )
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    streamers = g_data.get("streamers") or []

    existing = None
    if 이름:
        existing = next(
            (s for s in streamers if s.get("id") == 이름 or s.get("name") == 이름.strip()),
            None,
        )

    modal = CafeLinkModal(
        guild_id=guild_id,
        guild_name=guild_name,
        target_channel=target_channel,
        existing_streamer=existing,
        initial_name=(이름.strip() if 이름 and not existing else ""),
    )
    await interaction.response.send_modal(modal)


@bot_client.tree.command(
    name="x연동",
    description="X(구 트위터) @아이디 또는 주소를 입력해 방송인에 X 알림을 연동하거나 X 단독 알림을 추가합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    이름="기존 등록된 방송인에 연동하려면 선택하세요 (비워두면 X 단독 알림으로 새로 추가됩니다)",
    채널="알림을 받을 디스코드 채널 (비워두면 현재 채널에 등록됩니다)",
)
@app_commands.autocomplete(이름=_autocomplete_registered_streamers)
async def cmd_link_x(
    interaction: discord.Interaction,
    이름: Optional[str] = None,
    채널: Optional[discord.TextChannel] = None,
) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    target_channel = 채널 or (
        interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
    )
    if not target_channel:
        await interaction.response.send_message(
            "⚠️ 일반 텍스트 채널에서 실행하거나 `채널` 옵션을 지정해 주세요.",
            ephemeral=True,
        )
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    streamers = g_data.get("streamers") or []

    existing = None
    if 이름:
        existing = next(
            (s for s in streamers if s.get("id") == 이름 or s.get("name") == 이름.strip()),
            None,
        )

    modal = XLinkModal(
        guild_id=guild_id,
        guild_name=guild_name,
        target_channel=target_channel,
        existing_streamer=existing,
        initial_name=(이름.strip() if 이름 and not existing else ""),
    )
    await interaction.response.send_modal(modal)


@bot_client.tree.command(
    name="x쿠키설정",
    description="X(구 트위터) 알림 수집을 위한 부계정 로그인 쿠키(auth_token, ct0)를 보안 창으로 등록합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_set_x_cookies(interaction: discord.Interaction) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    g_cfg = g_data.get("global") or {}

    modal = XCookieModal(
        guild_id=guild_id,
        guild_name=guild_name,
        default_auth_token=g_cfg.get("x_auth_token", ""),
        default_ct0=g_cfg.get("x_ct0", ""),
    )
    await interaction.response.send_modal(modal)


@bot_client.tree.command(
    name="쿠키설정",
    description="카페 회원 전용 글 조회 및 치지직 팔로우 연동을 위한 네이버 쿠키(NID_AUT, NID_SES)를 보안 창으로 등록합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_set_cookies(interaction: discord.Interaction) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    g_cfg = g_data.get("global") or {}

    modal = NaverCookieModal(
        guild_id=guild_id,
        guild_name=guild_name,
        default_aut=g_cfg.get("naver_nid_aut", ""),
        default_ses=g_cfg.get("naver_nid_ses", ""),
    )
    await interaction.response.send_modal(modal)


@bot_client.tree.command(
    name="팔로우연동",
    description="네이버 계정으로 팔로우 중인 치지직 채널 목록을 불러와 알림 ON 채널 일괄 등록 또는 선택 등록을 진행합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(채널="알림을 받을 디스코드 채널 (비워두면 현재 채널에 등록됩니다)")
async def cmd_follow_import(
    interaction: discord.Interaction,
    채널: Optional[discord.TextChannel] = None,
) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    target_channel = 채널 or (
        interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
    )
    if not target_channel:
        await interaction.response.send_message(
            "⚠️ 일반 텍스트 채널에서 실행하거나 `채널` 옵션을 지정해 주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=False)

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    g_cfg = g_data.get("global") or {}
    streamers = g_data.get("streamers") or []

    existing_cids = {
        s.get("chzzk_channel_id") for s in streamers if s.get("chzzk_channel_id")
    }

    ok, msg, channels = await asyncio.to_thread(
        _fetch_chzzk_followings_sync,
        g_cfg.get("naver_nid_aut", ""),
        g_cfg.get("naver_nid_ses", ""),
        existing_cids,
    )
    if not ok:
        await interaction.followup.send(f"⚠️ {msg}")
        return

    if not channels:
        await interaction.followup.send("📭 해당 네이버 계정으로 팔로우 중인 치지직 채널이 없습니다.")
        return

    try:
        webhook_url = await _ensure_channel_webhook(target_channel, guild_id, guild_name)
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ 봇에게 {target_channel.mention} 채널의 **웹후크 관리하기(Manage Webhooks)** 권한이 없습니다."
        )
        return
    except Exception as e:
        await interaction.followup.send(f"❌ 채널 웹후크 생성 중 오류가 발생했습니다: `{e}`")
        return

    view = FollowImportView(
        guild_id=guild_id,
        guild_name=guild_name,
        target_channel=target_channel,
        webhook_url=webhook_url,
        channels=channels,
    )
    await interaction.followup.send(embed=view.build_embed(), view=view)


@bot_client.tree.command(
    name="알림목록",
    description="이 디스코드 서버에 등록된 모든 방송인 알림 목록을 확인합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_list_streamers(interaction: discord.Interaction) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    streamers = g_data.get("streamers") or []

    if not streamers:
        await interaction.response.send_message(
            "📭 아직 이 서버에 등록된 알림 방송인이 없습니다.\n"
            "`/알림추가`, `/팔로우연동`, 또는 `/카페연동` 명령어로 방송인을 등록해 보세요!",
            ephemeral=False,
        )
        return

    embed = discord.Embed(
        title=f"📺 {guild_name} — 등록된 알림 방송인 목록 ({len(streamers)}명)",
        description="세부 항목을 켜고 끄려면 `/알림설정`, 삭제하려면 `/알림삭제`를 입력해 주세요.",
        color=0x00FFA3,
    )
    for st in streamers[:25]:
        name = st.get("name", "방송인")
        embed.add_field(
            name=f"🔔 {name}",
            value=_format_streamer_status_lines(st),
            inline=False,
        )
    if len(streamers) > 25:
        embed.set_footer(text=f"외 {len(streamers) - 25}명 더 등록되어 있습니다.")

    await interaction.response.send_message(embed=embed)


@bot_client.tree.command(
    name="알림설정",
    description="등록된 방송인의 세부 알림 항목(뱅온, 뱅종, 방제, 카테고리, 커뮤니티, 카페 등)을 버튼으로 켜고 끕니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(이름="설정을 변경할 방송인을 선택하세요")
@app_commands.autocomplete(이름=_autocomplete_registered_streamers)
async def cmd_config_streamer(interaction: discord.Interaction, 이름: str) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    streamers = g_data.get("streamers") or []

    target = next(
        (s for s in streamers if s.get("id") == 이름 or s.get("name") == 이름.strip()),
        None,
    )
    if not target:
        await interaction.response.send_message(
            f"❌ `{이름}` 방송인을 목록에서 찾을 수 없습니다. `/알림목록`을 확인해 주세요.",
            ephemeral=True,
        )
        return

    embed = _build_streamer_config_embed(target, guild_name)
    view = StreamerConfigView(guild_id, guild_name, target["id"])
    await interaction.response.send_message(embed=embed, view=view)


@bot_client.tree.command(
    name="멘션설정",
    description="방송인 알림 발송 시 호출할 역할(멘션)을 설정하거나 해제합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    이름="멘션을 설정할 방송인을 선택하세요",
    역할="멘션할 디스코드 역할 (선택하지 않으면 멘션이 해제됩니다)",
    뱅온전용="방송 시작(뱅온) 시에만 멘션할지 여부 (기본값: True)",
)
@app_commands.autocomplete(이름=_autocomplete_registered_streamers)
async def cmd_set_mention(
    interaction: discord.Interaction,
    이름: str,
    역할: Optional[discord.Role] = None,
    뱅온전용: bool = True,
) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    streamers = g_data.get("streamers") or []

    target = next(
        (s for s in streamers if s.get("id") == 이름 or s.get("name") == 이름.strip()),
        None,
    )
    if not target:
        await interaction.response.send_message(
            f"❌ `{이름}` 방송인을 목록에서 찾을 수 없습니다. `/알림목록`을 확인해 주세요.",
            ephemeral=True,
        )
        return

    st_name = target.get("name", "방송인")
    if 역할:
        target["mention_role_id"] = str(역할.id)
        target["mention_role_name"] = f"@{역할.name}"
        target["mention_text"] = ""
        target["mention_on_open_only"] = bool(뱅온전용)
        mode_str = "방송 시작(뱅온) 시에만" if 뱅온전용 else "모든 알림에"
        msg = f"🔔 **{st_name}** 알림 멘션이 **@{역할.name}**({mode_str})으로 설정되었습니다!"
    else:
        target["mention_role_id"] = ""
        target["mention_role_name"] = ""
        target["mention_text"] = ""
        msg = f"🔕 **{st_name}** 알림 멘션이 **해제(멘션 없음)**되었습니다."

    save_guild_settings(guild_id, guild_name=guild_name, guild_streamers=streamers)
    log_event(
        f"[디스코드 봇:{guild_name}] '{st_name}' 멘션 설정 변경: {target.get('mention_role_name') or '없음'}"
    )

    embed = _build_streamer_config_embed(target, guild_name)
    view = StreamerConfigView(guild_id, guild_name, target["id"])
    await interaction.response.send_message(content=msg, embed=embed, view=view)


@bot_client.tree.command(
    name="알림삭제",
    description="이 서버에 등록된 방송인 알림을 삭제합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(이름="삭제할 방송인을 선택하세요")
@app_commands.autocomplete(이름=_autocomplete_registered_streamers)
async def cmd_delete_streamer(interaction: discord.Interaction, 이름: str) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    streamers = g_data.get("streamers") or []

    target = next(
        (s for s in streamers if s.get("id") == 이름 or s.get("name") == 이름.strip()),
        None,
    )
    if not target:
        await interaction.response.send_message(
            f"❌ `{이름}` 방송인을 목록에서 찾을 수 없습니다.",
            ephemeral=True,
        )
        return

    removed_name = target.get("name", 이름)
    new_streamers = [s for s in streamers if s.get("id") != target.get("id")]
    save_guild_settings(guild_id, guild_name=guild_name, guild_streamers=new_streamers)
    log_event(f"[디스코드 봇:{guild_name}] '{removed_name}' 알림 삭제 완료")

    await interaction.response.send_message(
        f"🗑️ **{removed_name}** 알림이 이 서버 목록에서 삭제되었습니다."
    )


@bot_client.tree.command(
    name="미리보기",
    description="등록된 방송인의 현재 치지직 방송 상태 및 카페 글 미리보기 카드를 즉시 전송합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(이름="미리보기를 전송할 방송인을 선택하세요")
@app_commands.autocomplete(이름=_autocomplete_registered_streamers)
async def cmd_preview_streamer(interaction: discord.Interaction, 이름: str) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    guild_name = interaction.guild.name if interaction.guild else guild_id
    g_data = load_guild_settings(guild_id, guild_name)
    streamers = g_data.get("streamers") or []

    target = next(
        (s for s in streamers if s.get("id") == 이름 or s.get("name") == 이름.strip()),
        None,
    )
    if not target:
        await interaction.response.send_message(
            f"❌ `{이름}` 방송인을 목록에서 찾을 수 없습니다.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=False)
    ok, msg = await asyncio.to_thread(
        send_preview_for_streamer,
        target,
        g_data.get("global") or {},
        f"서버:{guild_name}",
    )
    await interaction.followup.send(msg if ok else f"⚠️ {msg}")


@bot_client.tree.command(
    name="지금점검",
    description="순찰 주기를 기다리지 않고 즉시 전체 방송인의 최신 상태를 점검합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_check_now(interaction: discord.Interaction) -> None:
    err = _verify_admin_interaction(interaction)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    if not _bot_check_now_cb:
        await interaction.response.send_message(
            "⚠️ 순찰 루프가 아직 준비되지 않았습니다.", ephemeral=True
        )
        return

    threading.Thread(target=_bot_check_now_cb, daemon=True).start()
    await interaction.response.send_message(
        "🔄 **즉시 순찰을 시작했습니다!** 새로운 방송 시작·변경 사항이 있으면 등록된 채널로 즉시 알림이 전송됩니다."
    )


@bot_client.tree.command(
    name="도움말",
    description="치지직·네이버 카페 알림봇의 전체 슬래시 명령어 사용법을 안내합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_help(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🤖 치지직 & 네이버 카페 디스코드 알림봇 도움말 (전체 기능)",
        description=(
            "웹 대시보드에 접속할 필요 없이 디스코드 안에서 모든 알림을 제어할 수 있습니다.\n"
            "*(모든 명령어는 **서버 관리하기** 권한이 있는 관리자만 사용할 수 있습니다)*"
        ),
        color=0x00FFA3,
    )
    embed.add_field(
        name="➕ `/알림추가 이름: [방송인] 채널: [#채널(선택)] 멘션역할: [@역할(선택)]`",
        value="방송인 이름을 검색(자동완성)하여 지정한 채널에 치지직 알림을 바로 등록합니다. (멘션 역할 지정 가능, 기본값: 멘션 없음)",
        inline=False,
    )
    embed.add_field(
        name="🔔 `/멘션설정 이름: [방송인] 역할: [@역할(선택)] 뱅온전용: [True/False]`",
        value="방송 알림 시 호출할 역할을 지정하거나 해제합니다. 비워두면 멘션 없음으로 설정됩니다.",
        inline=False,
    )
    embed.add_field(
        name="📺 `/팔로우연동 채널: [#채널(선택)]`",
        value=(
            "네이버 계정으로 팔로우 중인 치지직 채널들을 불러와 **🔔 알림 ON 채널 원클릭 일괄 등록** "
            "또는 드롭다운 다중 선택으로 한 번에 추가합니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="☕ `/카페연동 이름: [방송인(선택)] 채널: [#채널(선택)]`",
        value=(
            "모달 팝업창에 방송인이 작성한 카페 게시글 주소 1개를 붙여넣으면 카페 ID와 멤버키를 자동 추출합니다.\n"
            "기존 방송인에 카페를 추가하거나 **네이버 카페 단독 알림**으로 새로 등록할 수 있습니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="🐦 `/x연동 이름: [방송인(선택)] 채널: [#채널(선택)]`",
        value=(
            "모달 팝업창에 방송인의 X(트위터) `@아이디` 또는 프로필 주소를 입력해 포스트·답글·리트윗 알림을 연동합니다.\n"
            "기존 방송인에 X를 추가하거나 **X 단독 알림**으로 새로 등록할 수 있습니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔐 `/쿠키설정`  |  `/x쿠키설정`",
        value=(
            "채팅방에 노출되지 않는 보안 팝업창(모달)으로 네이버 쿠키(`NID_AUT`, `NID_SES`) 또는 X 부계정 쿠키(`auth_token`, `ct0`)를 등록합니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 `/알림목록`  |  ⚙️ `/알림설정`  |  🗑️ `/알림삭제`",
        value="등록된 방송인 목록을 보거나, 버튼으로 세부 항목(뱅온·방제·커뮤니티·카페·X 포스트/답글/리트윗)을 켜고 끄거나, 삭제합니다.",
        inline=False,
    )
    embed.add_field(
        name="📨 `/미리보기`  |  🔄 `/지금점검`",
        value="현재 방송/카페 상태 카드를 즉시 테스트 발송하거나 즉시 순찰을 실행합니다.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


def start_discord_bot_background(check_now_cb: Optional[Callable[[], None]] = None) -> None:
    """
    .env에 DISCORD_BOT_TOKEN이 설정되어 있으면 별도 데몬 스레드에서 디스코드 봇을 구동합니다.
    """
    if check_now_cb:
        set_bot_check_now_callback(check_now_cb)

    token = config.DISCORD_BOT_TOKEN
    if not token:
        log_event("ℹ️ [디스코드 봇] DISCORD_BOT_TOKEN이 설정되지 않아 슬래시 봇 없이 실행합니다.")
        return

    def _runner() -> None:
        try:
            bot_client.run(token, log_handler=None)
        except Exception as e:
            log_event(f"❌ [디스코드 봇 오류] 봇 실행 중 예외 발생: {e}")

    t = threading.Thread(target=_runner, daemon=True, name="DiscordBotThread")
    t.start()
