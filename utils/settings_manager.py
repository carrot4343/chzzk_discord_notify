"""
다중 사용자(멀티 유저) 계정 및 사용자별 독립 설정(settings.json),
초대 코드("FLOWANGLE"), 관리자(carrot4343) 권한, 로그인 시도 제한(1분 5회 실패 시 5분 잠금)을 관리하는 모듈입니다.
"""

import base64
from collections import deque
from datetime import datetime
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from typing import Any, Optional
import uuid
from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

import config

SETTINGS_FILE_PATH: Path = config.BASE_DIR / "settings.json"
_lock = threading.RLock()
_system_logs: deque[str] = deque(maxlen=200)
_user_logs: dict[str, deque[str]] = {}

# ------------------------------------------------------------------------------
# 파일 기반 회전 로그 (RotatingFileHandler - 5MB x 5개 보관)
# ------------------------------------------------------------------------------
config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
_file_logger = logging.getLogger("taemtaem_file_logger")
_file_logger.setLevel(logging.INFO)
_file_logger.propagate = False

if not _file_logger.handlers:
    try:
        _rfh = RotatingFileHandler(
            config.LOG_FILE_PATH,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        _rfh.setFormatter(logging.Formatter("%(message)s"))
        _file_logger.addHandler(_rfh)
    except Exception as _e:
        print(f"[로깅 경고] RotatingFileHandler 초기화 실패: {_e}")

# ------------------------------------------------------------------------------
# 네이버 쿠키 대칭키(Fernet AES-128-CBC + HMAC) 암호화 / 복호화 매니저
# ------------------------------------------------------------------------------
_fernet_instance: Optional[Fernet] = None


def _get_fernet_cipher() -> Fernet:
    """대칭키 암호화 인스턴스를 반환합니다 (secret_key 또는 전용 환경변수 기반)."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    raw_seed = config.COOKIE_ENCRYPTION_KEY
    if not raw_seed:
        raw_seed = "taemtaem-notify-cookie-encryption-default-seed-2026"
        try:
            if SETTINGS_FILE_PATH.exists():
                with open(SETTINGS_FILE_PATH, "r", encoding="utf-8") as f:
                    raw_seed = json.load(f).get("secret_key") or raw_seed
        except Exception:
            pass

    derived = base64.urlsafe_b64encode(hashlib.sha256(raw_seed.encode("utf-8")).digest())
    _fernet_instance = Fernet(derived)
    return _fernet_instance


def encrypt_credential(plain: Optional[str]) -> str:
    """민감한 인증 쿠키 값을 Fernet 대칭키로 암호화합니다."""
    if not plain:
        return ""
    val = plain.strip()
    if not val:
        return ""
    if val.startswith("enc:"):
        return val  # 이미 암호화됨
    try:
        cipher = _get_fernet_cipher()
        token = cipher.encrypt(val.encode("utf-8")).decode("utf-8")
        return f"enc:{token}"
    except Exception as e:
        print(f"[보안 경고] 쿠키 암호화 실패: {e}")
        return val


def decrypt_credential(encrypted_val: Optional[str]) -> str:
    """암호화된 쿠키 값을 복호화하여 평문으로 반환합니다. (기존 평문도 하위 호환 처리)"""
    if not encrypted_val:
        return ""
    val = encrypted_val.strip()
    if not val:
        return ""
    if not val.startswith("enc:"):
        return val  # 레거시 평문 값은 그대로 반환
    try:
        cipher = _get_fernet_cipher()
        token = val[4:]
        return cipher.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception as e:
        print(f"[보안 경고] 쿠키 복호화 실패: {e}")
        return ""


def _decrypt_dict_cookies(g_cfg: dict[str, Any]) -> None:
    if not isinstance(g_cfg, dict):
        return
    if "naver_nid_aut" in g_cfg:
        g_cfg["naver_nid_aut"] = decrypt_credential(g_cfg["naver_nid_aut"])
    if "naver_nid_ses" in g_cfg:
        g_cfg["naver_nid_ses"] = decrypt_credential(g_cfg["naver_nid_ses"])


def _prepare_data_for_saving(data: dict[str, Any]) -> dict[str, Any]:
    """저장 전 settings.json 복사본의 쿠키 값을 안전하게 암호화합니다."""
    copied = dict(data)
    if "legacy_seed" in copied and isinstance(copied["legacy_seed"], dict):
        seed_copy = dict(copied["legacy_seed"])
        if "global" in seed_copy and isinstance(seed_copy["global"], dict):
            g = dict(seed_copy["global"])
            g["naver_nid_aut"] = encrypt_credential(g.get("naver_nid_aut"))
            g["naver_nid_ses"] = encrypt_credential(g.get("naver_nid_ses"))
            seed_copy["global"] = g
        copied["legacy_seed"] = seed_copy

    if "users" in copied and isinstance(copied["users"], dict):
        users_copy = {}
        for uname, u_obj in copied["users"].items():
            u_copy = dict(u_obj)
            if "global" in u_copy and isinstance(u_copy["global"], dict):
                g = dict(u_copy["global"])
                g["naver_nid_aut"] = encrypt_credential(g.get("naver_nid_aut"))
                g["naver_nid_ses"] = encrypt_credential(g.get("naver_nid_ses"))
                u_copy["global"] = g
            users_copy[uname] = u_copy
        copied["users"] = users_copy

    if "guilds" in copied and isinstance(copied["guilds"], dict):
        guilds_copy = {}
        for gid, g_obj in copied["guilds"].items():
            g_copy = dict(g_obj)
            if "global" in g_copy and isinstance(g_copy["global"], dict):
                g = dict(g_copy["global"])
                g["naver_nid_aut"] = encrypt_credential(g.get("naver_nid_aut"))
                g["naver_nid_ses"] = encrypt_credential(g.get("naver_nid_ses"))
                g_copy["global"] = g
            guilds_copy[gid] = g_copy
        copied["guilds"] = guilds_copy

    return copied

# 로그인 무차별 대입 방지 (Rate Limiting) 관리 딕셔너리
# _failed_attempts: { key: [timestamp1, timestamp2, ...] }
# _locked_until: { key: timestamp }
_failed_attempts: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}

ADMIN_USERNAME = "carrot4343"
DEFAULT_INVITE_CODE = "FLOWANGLE"


def is_admin(username: Optional[str]) -> bool:
    """carrot4343 계정인지 확인합니다."""
    if not username:
        return False
    return username.strip().lower() == ADMIN_USERNAME.lower()


def check_rate_limit(key: str) -> tuple[bool, int]:
    """
    로그인 시도 가능 여부를 반환합니다.
    잠겨있다면 (False, 남은 잠금 초), 가능하면 (True, 0)을 반환합니다.
    """
    now = time.time()
    with _lock:
        unlock_time = _locked_until.get(key, 0)
        if now < unlock_time:
            return False, int(unlock_time - now)
        elif key in _locked_until:
            del _locked_until[key]
            _failed_attempts.pop(key, None)
        return True, 0


def record_failed_attempt(key: str) -> tuple[bool, int]:
    """
    로그인 실패를 기록하고, 최근 60초 내 5회 실패 시 300초(5분) 잠금을 적용합니다.
    (잠금 여부, 남은 잠금 초)를 반환합니다.
    """
    now = time.time()
    with _lock:
        attempts = _failed_attempts.setdefault(key, [])
        # 60초 지난 과거 기록 정리
        attempts = [t for t in attempts if now - t <= 60]
        attempts.append(now)
        _failed_attempts[key] = attempts

        if len(attempts) >= 5:
            lock_duration = 300  # 5분
            _locked_until[key] = now + lock_duration
            _failed_attempts.pop(key, None)
            return True, lock_duration
        return False, 0


def clear_failed_attempts(key: str) -> None:
    """로그인 성공 시 실패 기록을 초기화합니다."""
    with _lock:
        _failed_attempts.pop(key, None)
        _locked_until.pop(key, None)


def log_event(message: str, username: Optional[str] = None) -> None:
    """콘솔에 출력하고 시스템 로그 및 (지정된 경우) 특정 사용자의 로그 버퍼와 파일(RotatingFileHandler)에 기록합니다."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{now_str}] {message}"

    # 1. 파일 로그 기록 (RotatingFileHandler - 5MB x 5개 자동 회전)
    try:
        _file_logger.info(formatted)
    except Exception:
        pass

    # 2. 콘솔 출력 (유니코드 에러 방어)
    try:
        print(formatted, flush=True)
    except UnicodeEncodeError:
        try:
            enc = sys.stdout.encoding or "utf-8"
            print(formatted.encode(enc, errors="replace").decode(enc), flush=True)
        except Exception:
            pass
    except Exception:
        pass

    # 3. 인메모리 큐 저장 (웹 UI 표시용)
    with _lock:
        _system_logs.appendleft(formatted)
        if username:
            buf = _user_logs.setdefault(username, deque(maxlen=150))
            buf.appendleft(formatted)


def get_user_logs(username: str) -> list[str]:
    """특정 사용자에게만 보이는 전용 로그와 공통 순찰 요약 로그를 합쳐 반환합니다."""
    with _lock:
        u_logs = list(_user_logs.get(username, []))
        if u_logs:
            return u_logs
        return list(_system_logs)[:50]


def _build_initial_template_from_env() -> dict[str, Any]:
    """기존 .env 설정을 자동 이전하기 위한 초기 데이터를 만듭니다."""
    streamers: list[dict[str, Any]] = []
    if config.CHZZK_CHANNEL_ID or config.NAVER_CAFE_CLUB_ID:
        default_img = (
            "https://nng-phinf.pstatic.net/MjAyMzEyMTlfNzEg/"
            "MDAxNzAyOTYzMjYyNzg0.p5NGvJ8O95i4qr34yZkV7ERB3ILAufDv-_5gCUmGgyAg."
            "rHSlJvL9dDHVkuFLZcR46qtPa1QCEqDDelVJ60sLe1cg.PNG/ed86de06d353e32b.png"
            if config.CHZZK_CHANNEL_ID == "a7e175625fdea5a7d98428302b7aa57f"
            else ""
        )
        streamers.append(
            {
                "id": "streamer_default_1",
                "enabled": True,
                "name": config.STREAMER_NAME or "탬탬버린",
                "profile_image_url": default_img,
                "cafe_profile_image_url": default_img,
                "chzzk_input": config.CHZZK_CHANNEL_ID,
                "chzzk_channel_id": config.CHZZK_CHANNEL_ID,
                "cafe_article_url": "",
                "cafe_club_id": config.NAVER_CAFE_CLUB_ID,
                "cafe_member_key": config.NAVER_CAFE_MEMBER_KEY,
                "custom_webhook_url": "",
                "notify_chzzk_open": config.NOTIFY_CHZZK_OPEN,
                "notify_chzzk_close": config.NOTIFY_CHZZK_CLOSE,
                "notify_chzzk_title": config.NOTIFY_CHZZK_TITLE_CHANGE,
                "notify_chzzk_category": config.NOTIFY_CHZZK_CATEGORY_CHANGE,
                "notify_chzzk_community": config.NOTIFY_CHZZK_COMMUNITY,
                "notify_cafe_article": config.NOTIFY_CAFE_ARTICLE,
                "notify_cafe_comment": config.NOTIFY_CAFE_COMMENT,
            }
        )

    return {
        "global": {
            "discord_webhook_url": config.DISCORD_WEBHOOK_URL,
            "naver_nid_aut": config.NAVER_NID_AUT,
            "naver_nid_ses": config.NAVER_NID_SES,
        },
        "streamers": streamers,
    }


def _create_empty_user_workspace() -> dict[str, Any]:
    """신규 가입자를 위한 빈 설정 공간을 생성합니다."""
    return {
        "global": {
            "discord_webhook_url": "",
            "naver_nid_aut": "",
            "naver_nid_ses": "",
        },
        "streamers": [],
    }


def load_all_data() -> dict[str, Any]:
    """settings.json 전체 데이터를 로드합니다."""
    if not SETTINGS_FILE_PATH.exists():
        initial = {
            "secret_key": secrets.token_hex(24),
            "poll_interval_seconds": config.POLL_INTERVAL_SECONDS,
            "invite_code": DEFAULT_INVITE_CODE,
            "allow_registration": True,
            "legacy_seed": _build_initial_template_from_env(),
            "users": {},
        }
        save_all_data(initial)
        return initial

    try:
        with _lock:
            with open(SETTINGS_FILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

        data.setdefault("secret_key", secrets.token_hex(24))
        data.setdefault("poll_interval_seconds", config.POLL_INTERVAL_SECONDS)
        data.setdefault("invite_code", DEFAULT_INVITE_CODE)
        data.setdefault("allow_registration", True)
        data.setdefault("users", {})
        data.setdefault("guilds", {})

        # 메모리 로드 시 쿠키 투명 복호화
        if isinstance(data.get("legacy_seed"), dict) and "global" in data["legacy_seed"]:
            _decrypt_dict_cookies(data["legacy_seed"]["global"])
        for u_obj in (data.get("users") or {}).values():
            if isinstance(u_obj, dict) and "global" in u_obj:
                _decrypt_dict_cookies(u_obj["global"])
        for g_obj in (data.get("guilds") or {}).values():
            if isinstance(g_obj, dict) and "global" in g_obj:
                _decrypt_dict_cookies(g_obj["global"])

        return data
    except Exception as e:
        print(f"[설정 경고] settings.json 로드 오류: {e}")
        return {
            "secret_key": secrets.token_hex(24),
            "poll_interval_seconds": config.POLL_INTERVAL_SECONDS,
            "invite_code": DEFAULT_INVITE_CODE,
            "allow_registration": True,
            "legacy_seed": _build_initial_template_from_env(),
            "users": {},
        }


def load_settings() -> dict[str, Any]:
    """하위 호환성을 위해 레거시 모듈에서 참조할 global 및 기본 설정을 반환합니다."""
    data = load_all_data()
    seed = data.get("legacy_seed") or {}
    users = data.get("users") or {}
    first_user_global = {}
    if "carrot4343" in users:
        first_user_global = users["carrot4343"].get("global") or {}
    elif users:
        first_uname = next(iter(users))
        first_user_global = users[first_uname].get("global") or {}

    merged_global = {
        **(seed.get("global") or {}),
        **first_user_global,
    }
    return {
        "global": merged_global,
        "poll_interval_seconds": data.get("poll_interval_seconds", 60),
        "invite_code": data.get("invite_code", DEFAULT_INVITE_CODE),
        "allow_registration": data.get("allow_registration", True),
    }


def save_all_data(data: dict[str, Any]) -> None:
    """전체 멀티 유저 설정 데이터를 임시 파일 기록 후 원자적으로(Atomic) settings.json에 저장합니다 (쿠키 암호화)."""
    with _lock:
        to_save = _prepare_data_for_saving(data)
        temp_path = SETTINGS_FILE_PATH.with_name(f"{SETTINGS_FILE_PATH.name}.{os.getpid()}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except Exception:
                pass
            os.replace(temp_path, SETTINGS_FILE_PATH)
        except Exception as e:
            print(f"[오류] settings.json 저장 실패: {e}")
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass


def register_user(username: str, password: str, invite_code: str) -> tuple[bool, str]:
    """
    새 사용자를 등록합니다.
    초대 코드("FLOWANGLE") 일치 여부와 신규 가입 허용 여부를 검증합니다.
    """
    uname = (username or "").strip()
    if len(uname) < 2 or len(uname) > 30:
        return False, "아이디는 2자 이상 30자 이하로 입력해 주세요."
    if len(password or "") < 4:
        return False, "비밀번호는 최소 4자 이상 입력해 주세요."

    data = load_all_data()

    # 신규 가입 차단 여부 확인
    if not data.get("allow_registration", True):
        return False, "현재 관리자에 의해 신규 회원가입이 일시 중단되었습니다."

    # 초대 코드 검증
    expected_code = data.get("invite_code", DEFAULT_INVITE_CODE).strip()
    if (invite_code or "").strip() != expected_code:
        return False, "초대 코드가 일치하지 않습니다. 올바른 초대 코드를 입력해 주세요."

    users = data.setdefault("users", {})
    if uname in users:
        return False, "이미 존재하는 아이디입니다. 다른 아이디를 선택하거나 로그인해 주세요."

    # 첫 번째 가입자이거나 carrot4343 계정이면 초기 시드 데이터 상속
    if (len(users) == 0 or uname.lower() == ADMIN_USERNAME.lower()) and data.get("legacy_seed"):
        seed = data["legacy_seed"]
        workspace = {
            "global": seed.get("global") or _create_empty_user_workspace()["global"],
            "streamers": seed.get("streamers") or [],
        }
    else:
        workspace = _create_empty_user_workspace()

    users[uname] = {
        "password_hash": generate_password_hash(password),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "global": workspace["global"],
        "streamers": workspace["streamers"],
    }
    save_all_data(data)
    log_event(f"[회원가입] '{uname}' 계정이 생성되었습니다.", username=uname)
    return True, f"환영합니다, {uname}님! 회원가입 및 로그인이 완료되었습니다."


def authenticate_user(username: str, password: str, client_ip: str = "default") -> tuple[bool, str]:
    """
    아이디와 비밀번호를 확인합니다.
    1분 내 5회 이상 실패 시 5분 동안 잠금을 적용합니다.
    """
    uname = (username or "").strip()
    rate_key = f"{client_ip}:{uname}"

    # 잠금 상태 확인
    allowed, remaining = check_rate_limit(rate_key)
    if not allowed:
        mins = remaining // 60
        secs = remaining % 60
        time_str = f"{mins}분 {secs}초" if mins > 0 else f"{secs}초"
        return False, f"⚠️ 연속 5회 로그인 실패로 5분간 계정이 잠겼습니다. (남은 시간: {time_str})"

    data = load_all_data()
    users = data.get("users") or {}
    user_obj = users.get(uname)

    if not user_obj or not check_password_hash(user_obj.get("password_hash", ""), password or ""):
        locked, lock_secs = record_failed_attempt(rate_key)
        if locked:
            return False, "⚠️ 1분 내 로그인 5회 실패로 계정이 5분 동안 잠겼습니다."
        return False, "아이디 또는 비밀번호가 일치하지 않습니다."

    # 성공 시 실패 카운트 초기화
    clear_failed_attempts(rate_key)
    log_event(f"[로그인] '{uname}'님이 로그인했습니다.", username=uname)
    return True, "로그인 성공!"


def load_user_settings(username: str) -> dict[str, Any]:
    """특정 사용자의 개인 설정(global, streamers)을 반환합니다."""
    data = load_all_data()
    users = data.get("users") or {}
    user_obj = users.get(username)
    if not user_obj:
        empty = _create_empty_user_workspace()
        return {
            "username": username,
            "is_admin": is_admin(username),
            "poll_interval_seconds": data.get("poll_interval_seconds", 60),
            **empty,
        }

    return {
        "username": username,
        "is_admin": is_admin(username),
        "poll_interval_seconds": data.get("poll_interval_seconds", 60),
        "global": user_obj.get("global") or _create_empty_user_workspace()["global"],
        "streamers": user_obj.get("streamers") or [],
    }


def save_user_settings(
    username: str,
    user_global: Optional[dict[str, Any]] = None,
    user_streamers: Optional[list[dict[str, Any]]] = None,
    poll_interval_seconds: Optional[int] = None,
) -> None:
    """특정 사용자의 설정 변경사항을 저장합니다."""
    with _lock:
        data = load_all_data()
        users = data.setdefault("users", {})
        if username not in users:
            return

        if user_global is not None:
            users[username]["global"] = user_global
        if user_streamers is not None:
            users[username]["streamers"] = user_streamers
        if poll_interval_seconds is not None:
            data["poll_interval_seconds"] = max(15, min(600, int(poll_interval_seconds)))

        save_all_data(data)


# ------------------------------------------------------------------------------
# 관리자 전용 기능 (carrot4343 전용)
# ------------------------------------------------------------------------------
def get_admin_summary() -> dict[str, Any]:
    """관리자 페이지용 전체 통계 및 사용자 목록을 반환합니다."""
    data = load_all_data()
    users = data.get("users") or {}

    user_list = []
    for uname, u_info in users.items():
        user_list.append(
            {
                "username": uname,
                "is_admin": is_admin(uname),
                "created_at": u_info.get("created_at", "-"),
                "streamer_count": len(u_info.get("streamers") or []),
                "has_webhook": bool((u_info.get("global") or {}).get("discord_webhook_url")),
            }
        )

    return {
        "invite_code": data.get("invite_code", DEFAULT_INVITE_CODE),
        "allow_registration": data.get("allow_registration", True),
        "total_users": len(users),
        "users": user_list,
    }


def update_admin_config(invite_code: str, allow_registration: bool) -> tuple[bool, str]:
    """관리자가 초대 코드 및 신규 가입 차단 여부를 수정합니다."""
    code = (invite_code or "").strip()
    if not code:
        return False, "초대 코드는 공백일 수 없습니다."

    data = load_all_data()
    data["invite_code"] = code
    data["allow_registration"] = bool(allow_registration)
    save_all_data(data)
    log_event(f"[관리자] 초대 코드가 '{code}'(으)로 변경되었으며 가입허용 상태는 {allow_registration}입니다.")
    return True, "관리자 설정이 저장되었습니다."


def delete_user_by_admin(target_username: str) -> tuple[bool, str]:
    """관리자가 특정 사용자를 강제 탈퇴(삭제)합니다."""
    if is_admin(target_username):
        return False, "관리자 계정은 삭제할 수 없습니다."

    data = load_all_data()
    users = data.get("users") or {}
    if target_username not in users:
        return False, "존재하지 않는 회원입니다."

    del users[target_username]
    save_all_data(data)
    log_event(f"[관리자] '{target_username}' 회원이 관리자에 의해 강제 탈퇴되었습니다.")
    return True, f"'{target_username}' 회원이 삭제되었습니다."


def generate_streamer_id() -> str:
    """새 방송인 등록 시 고유 ID를 생성합니다."""
    return f"st_{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------------------
# 디스코드 서버(Guild) 단위 설정 관리
# ------------------------------------------------------------------------------
def load_guild_settings(guild_id: str, guild_name: str = "") -> dict[str, Any]:
    """특정 디스코드 서버(Guild)의 공유 설정(global, streamers, channel_webhooks)을 반환합니다."""
    gid = str(guild_id).strip()
    with _lock:
        data = load_all_data()
        guilds = data.setdefault("guilds", {})
        g_obj = guilds.get(gid)
        if not g_obj:
            empty = _create_empty_user_workspace()
            g_obj = {
                "guild_id": gid,
                "guild_name": guild_name or gid,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "global": empty["global"],
                "channel_webhooks": {},
                "streamers": [],
            }
            guilds[gid] = g_obj
            save_all_data(data)
        elif guild_name and g_obj.get("guild_name") != guild_name:
            g_obj["guild_name"] = guild_name
            save_all_data(data)

        return {
            "guild_id": gid,
            "guild_name": g_obj.get("guild_name") or guild_name or gid,
            "global": g_obj.get("global") or _create_empty_user_workspace()["global"],
            "channel_webhooks": g_obj.get("channel_webhooks") or {},
            "streamers": g_obj.get("streamers") or [],
        }


def save_guild_settings(
    guild_id: str,
    guild_name: Optional[str] = None,
    guild_global: Optional[dict[str, Any]] = None,
    guild_streamers: Optional[list[dict[str, Any]]] = None,
    channel_webhooks: Optional[dict[str, str]] = None,
) -> None:
    """특정 디스코드 서버(Guild)의 설정 변경사항을 저장합니다."""
    gid = str(guild_id).strip()
    with _lock:
        data = load_all_data()
        guilds = data.setdefault("guilds", {})
        if gid not in guilds:
            empty = _create_empty_user_workspace()
            guilds[gid] = {
                "guild_id": gid,
                "guild_name": guild_name or gid,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "global": empty["global"],
                "channel_webhooks": {},
                "streamers": [],
            }

        if guild_name is not None:
            guilds[gid]["guild_name"] = guild_name
        if guild_global is not None:
            guilds[gid]["global"] = guild_global
        if guild_streamers is not None:
            guilds[gid]["streamers"] = guild_streamers
        if channel_webhooks is not None:
            guilds[gid]["channel_webhooks"] = channel_webhooks

        save_all_data(data)

