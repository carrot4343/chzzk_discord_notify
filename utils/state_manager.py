"""
state.json 파일을 읽고 쓰는 다중 방송인 상태 관리자 모듈입니다.
방송인별(streamer_id)로 마지막 방송 상태, 카페 게시글 번호, 댓글 번호를 기억하여
동일한 알림이 중복 발송되는 것을 방지합니다.
"""

import json
import os
from pathlib import Path
import threading
from typing import Any
import config

_state_lock = threading.RLock()


def get_default_streamer_state() -> dict[str, Any]:
    """개별 방송인의 초기 상태 구조를 반환합니다."""
    return {
        "chzzk": {
            "initialized": False,
            "status": "CLOSE",
            "live_id": None,
            "title": "",
            "category": "",
            "last_community_id": None,
        },
        "naver_cafe": {
            "articles_initialized": False,
            "last_article_id": None,
            "comments_initialized": False,
            "last_comment_id": None,
        },
        "twitter_x": {
            "initialized": False,
            "last_tweet_id": None,
            "user_id": None,
        },
    }


def get_streamer_state(state: dict[str, Any], streamer_id: str) -> dict[str, Any]:
    """특정 방송인(streamer_id)의 상태 딕셔너리를 가져오거나 초기화합니다."""
    streamers_map = state.setdefault("streamers", {})
    if streamer_id not in streamers_map or not isinstance(
        streamers_map[streamer_id], dict
    ):
        streamers_map[streamer_id] = get_default_streamer_state()
    else:
        default_st = get_default_streamer_state()
        for sec, vals in default_st.items():
            if sec not in streamers_map[streamer_id]:
                streamers_map[streamer_id][sec] = vals
            else:
                for k, v in vals.items():
                    streamers_map[streamer_id][sec].setdefault(k, v)
    return streamers_map[streamer_id]


def load_state(state_path: Path = config.STATE_FILE_PATH) -> dict[str, Any]:
    """state.json 파일을 읽어옵니다. 구버전(단일 방송인) 형식이면 자동으로 변환합니다."""
    with _state_lock:
        if not state_path.exists():
            return {"streamers": {}}

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            # 구버전 단일 방송인 state.json 마이그레이션
            if "streamers" not in loaded and ("chzzk" in loaded or "naver_cafe" in loaded):
                migrated = {
                    "streamers": {
                        "streamer_default_1": {
                            "chzzk": loaded.get(
                                "chzzk", get_default_streamer_state()["chzzk"]
                            ),
                            "naver_cafe": loaded.get(
                                "naver_cafe", get_default_streamer_state()["naver_cafe"]
                            ),
                        }
                    }
                }
                return migrated

            if "streamers" not in loaded or not isinstance(loaded["streamers"], dict):
                loaded["streamers"] = {}
            return loaded
        except Exception as e:
            print(f"[경고] state.json 로드 중 문제 발생, 새 상태로 시작합니다: {e}")
            return {"streamers": {}}


def save_state(state: dict[str, Any], state_path: Path = config.STATE_FILE_PATH) -> None:
    """변경된 상태를 임시 파일에 기록 후 원자적으로(Atomic) state.json에 교체 저장합니다."""
    with _state_lock:
        temp_path = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, state_path)
        except Exception as e:
            print(f"[오류] state.json 저장 실패: {e}")
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
