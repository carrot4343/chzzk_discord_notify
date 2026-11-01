"""
치지직 & 네이버 카페 디스코드 통합 알림 프로그램 메인 실행 파일입니다.
다중 사용자(멀티 유저) 로그인 대시보드(http://127.0.0.1:5000)와
동일 방송인 중복 조회 방지(15초 TTL 캐시) 기반 백그라운드 순찰을 실행합니다.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
from typing import Any, Optional
import webbrowser

from bot.discord_bot import start_discord_bot_background
from collectors.chzzk import check_chzzk_for_streamer
from collectors.naver_cafe import check_naver_cafe_for_streamer
from utils.settings_manager import (
    load_all_data,
    log_event,
    save_all_data,
)
from utils.state_manager import get_streamer_state, load_state, save_state
from web_ui import app, send_preview_for_streamer, set_check_now_callback

_cycle_lock = threading.Lock()


def _check_streamer_task(task: dict[str, Any]) -> tuple[bool, bool, Optional[str]]:
    """개별 방송인의 치지직 및 네이버 카페 상태를 비동기 스레드 풀에서 확인합니다."""
    streamer = task["streamer"]
    st_state = task["st_state"]
    g_cfg = task["g_cfg"]
    st_name = task.get("name") or "방송인"
    prev_img = streamer.get("profile_image_url")
    state_changed = False
    data_changed = False

    try:
        if check_chzzk_for_streamer(streamer, st_state, g_cfg):
            state_changed = True
        if streamer.get("profile_image_url") != prev_img:
            data_changed = True
        if check_naver_cafe_for_streamer(streamer, st_state, g_cfg):
            state_changed = True
        return state_changed, data_changed, None
    except Exception as exc:
        return state_changed, data_changed, f"'{st_name}' 점검 중 오류: {exc}"


def run_all_streamers_cycle() -> None:
    """
    등록된 모든 사용자 및 디스코드 서버(Guild)의 활성 방송인들을 스레드 풀로 병렬 순찰합니다.
    (동일한 방송인을 여러 곳에서 등록했더라도 내부 캐시를 통해 1회만 API를 호출합니다.)
    """
    with _cycle_lock:
        data = load_all_data()
        users = data.get("users") or {}
        guilds = data.get("guilds") or {}
        state = load_state()

        tasks: list[dict[str, Any]] = []

        # 아직 웹 UI에서 가입한 계정이 없고 길드 등록도 없을 때는 초기 시드(legacy_seed)로 감시 동작 유지
        if not users and not guilds and data.get("legacy_seed"):
            seed = data["legacy_seed"]
            g_cfg = seed.get("global") or {}
            for streamer in seed.get("streamers") or []:
                if not streamer.get("enabled", True):
                    continue
                st_key = streamer.get("id") or "streamer_default_1"
                st_state = get_streamer_state(state, st_key)
                tasks.append(
                    {
                        "streamer": streamer,
                        "st_state": st_state,
                        "g_cfg": g_cfg,
                        "name": streamer.get("name") or "기본 방송인",
                    }
                )
        else:
            for uname, u_obj in users.items():
                g_cfg = u_obj.get("global") or {}
                streamers = u_obj.get("streamers") or []
                for streamer in streamers:
                    if not streamer.get("enabled", True):
                        continue
                    st_id = streamer.get("id") or "default"
                    state_key = (
                        st_id
                        if st_id == "streamer_default_1" and len(users) == 1
                        else f"{uname}:{st_id}"
                    )
                    st_state = get_streamer_state(state, state_key)
                    tasks.append(
                        {
                            "streamer": streamer,
                            "st_state": st_state,
                            "g_cfg": g_cfg,
                            "name": f"{uname}:{streamer.get('name') or st_id}",
                        }
                    )

            for gid, g_obj in guilds.items():
                g_cfg = g_obj.get("global") or {}
                streamers = g_obj.get("streamers") or []
                for streamer in streamers:
                    if not streamer.get("enabled", True):
                        continue
                    st_id = streamer.get("id") or "default"
                    state_key = f"guild:{gid}:{st_id}"
                    st_state = get_streamer_state(state, state_key)
                    tasks.append(
                        {
                            "streamer": streamer,
                            "st_state": st_state,
                            "g_cfg": g_cfg,
                            "name": f"guild({gid}):{streamer.get('name') or st_id}",
                        }
                    )

        state_changed = False
        data_changed = False
        checked_count = 0

        if tasks:
            # 병렬 실행 (최대 10개 작업자 스레드)
            max_workers = min(10, max(1, len(tasks)))
            if max_workers == 1:
                st_chg, dt_chg, err = _check_streamer_task(tasks[0])
                if err:
                    log_event(f"[순찰 경고] {err}")
                if st_chg:
                    state_changed = True
                if dt_chg:
                    data_changed = True
                checked_count = 1
            else:
                with ThreadPoolExecutor(
                    max_workers=max_workers, thread_name_prefix="poll-worker"
                ) as executor:
                    future_to_task = {
                        executor.submit(_check_streamer_task, task): task for task in tasks
                    }
                    for future in as_completed(future_to_task):
                        st_chg, dt_chg, err = future.result()
                        if err:
                            log_event(f"[순찰 경고] {err}")
                        if st_chg:
                            state_changed = True
                        if dt_chg:
                            data_changed = True
                        checked_count += 1

        if state_changed:
            save_state(state)
        if data_changed:
            updated_imgs = {
                t["streamer"].get("id"): t["streamer"].get("profile_image_url")
                for t in tasks
                if t["streamer"].get("id") and t["streamer"].get("profile_image_url")
            }
            fresh_data = load_all_data()
            for u_obj in (fresh_data.get("users") or {}).values():
                for st in u_obj.get("streamers") or []:
                    if st.get("id") in updated_imgs:
                        st["profile_image_url"] = updated_imgs[st["id"]]
            for g_obj in (fresh_data.get("guilds") or {}).values():
                for st in g_obj.get("streamers") or []:
                    if st.get("id") in updated_imgs:
                        st["profile_image_url"] = updated_imgs[st["id"]]
            save_all_data(fresh_data)

        log_event(
            f"[순찰 완료] 웹 사용자({len(users)}명) · 디스코드 서버({len(guilds)}개)의 활성 알림 총 {checked_count}건 점검 완료 (병렬 처리)"
        )


def background_monitor_loop() -> None:
    """백그라운드에서 주기적으로 모든 사용자의 방송인을 순찰하는 루프입니다."""
    while True:
        try:
            run_all_streamers_cycle()
        except Exception as e:
            log_event(f"[순찰 오류] 감시 중 예외 발생: {e}")

        data = load_all_data()
        interval = int(data.get("poll_interval_seconds") or 60)
        time.sleep(max(15, interval))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="치지직 & 네이버 카페 디스코드 통합 알림 프로그램 (다중 사용자 웹 UI 포함)"
    )
    parser.add_argument(
        "--force-latest",
        action="store_true",
        help="현재 설정된 방송인들의 최신 소식을 디스코드로 즉시 보내봅니다.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="반복하지 않고 딱 1번만 전체 방송인 상태를 확인한 뒤 종료합니다.",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="웹 UI 서버를 띄우지 않고 순찰 루프만 실행합니다.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="웹 UI 서버는 켜되, 브라우저 창을 자동으로 열지는 않습니다.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="웹 UI 바인딩 호스트 (기본값: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="웹 UI 포트 번호 (기본값: 5000)",
    )
    args = parser.parse_args()

    data = load_all_data()

    if args.force_latest:
        users = data.get("users") or {}
        if users:
            for uname, u_obj in users.items():
                g_cfg = u_obj.get("global") or {}
                for st in u_obj.get("streamers") or []:
                    send_preview_for_streamer(st, g_cfg, username=uname)
        elif data.get("legacy_seed"):
            seed = data["legacy_seed"]
            g_cfg = seed.get("global") or {}
            for st in seed.get("streamers") or []:
                send_preview_for_streamer(st, g_cfg)
        return

    if args.once:
        run_all_streamers_cycle()
        return

    if args.no_ui:
        log_event("🚀 [콘솔 전용 모드] 다중 사용자 실시간 감시를 시작합니다.")
        start_discord_bot_background(run_all_streamers_cycle)
        background_monitor_loop()
        return

    set_check_now_callback(run_all_streamers_cycle)
    start_discord_bot_background(run_all_streamers_cycle)

    monitor_thread = threading.Thread(target=background_monitor_loop, daemon=True)
    monitor_thread.start()

    url = f"http://127.0.0.1:{args.port}"
    log_event("=" * 65)
    log_event(f"🌐 다중 사용자 웹 관리자 대시보드 주소: {url}")
    log_event("   브라우저에서 위 주소로 접속하여 회원가입/로그인 후 알림을 관리하세요.")
    log_event("=" * 65)

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
