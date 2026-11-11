"""
알림 프로그램의 전반적인 설정을 관리하는 파일입니다.
민감한 정보(웹후크 주소, 쿠키 등)는 .env 파일에서 불러오고,
조회 주기(초)나 알림 켜기/끄기 스위치는 이 파일에서 쉽게 변경할 수 있습니다.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 프로젝트 루트 경로 및 .env 파일 로드
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ------------------------------------------------------------------------------
# 1. 민감 정보 및 대상 식별자 (.env 파일에서 읽어옴)
# ------------------------------------------------------------------------------
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "").strip()

STREAMER_NAME: str = os.getenv("STREAMER_NAME", "방송인").strip()
CHZZK_CHANNEL_ID: str = os.getenv("CHZZK_CHANNEL_ID", "").strip()

NAVER_CAFE_CLUB_ID: str = os.getenv("NAVER_CAFE_CLUB_ID", "").strip()
NAVER_CAFE_MEMBER_KEY: str = os.getenv("NAVER_CAFE_MEMBER_KEY", "").strip()
NAVER_NID_AUT: str = os.getenv("NAVER_NID_AUT", "").strip()
NAVER_NID_SES: str = os.getenv("NAVER_NID_SES", "").strip()

# X(구 트위터) 조회용 부계정 로그인 쿠키 (선택)
X_AUTH_TOKEN: str = os.getenv("X_AUTH_TOKEN", "").strip()
X_CT0: str = os.getenv("X_CT0", "").strip()

# ------------------------------------------------------------------------------
# 2. 조회 주기 설정 (단위: 초)
# 너무 짧게 설정하면 사이트에서 차단될 수 있으므로 기본값(30~60초) 유지를 권장합니다.
# ------------------------------------------------------------------------------
POLL_INTERVAL_SECONDS: int = 60  # 기본 순찰 주기 (60초 = 1분마다 확인)

# ------------------------------------------------------------------------------
# 3. 세부 알림 켜기(True) / 끄기(False) 스위치
# 원하지 않는 알림은 False 로 바꾸면 디스코드로 보내지 않습니다.
# ------------------------------------------------------------------------------
# [치지직 알림 옵션]
NOTIFY_CHZZK_OPEN: bool = True           # 방송 시작(뱅온) 알림 받기
NOTIFY_CHZZK_CLOSE: bool = False         # 방송 종료(뱅종) 알림 받기 (원하면 True로 변경)
NOTIFY_CHZZK_TITLE_CHANGE: bool = True   # 방송 제목(방제) 변경 알림 받기
NOTIFY_CHZZK_CATEGORY_CHANGE: bool = True  # 방송 카테고리(게임) 변경 알림 받기
NOTIFY_CHZZK_COMMUNITY: bool = True      # 치지직 채널 커뮤니티 새 글 알림 받기

# [네이버 카페 알림 옵션]
NOTIFY_CAFE_ARTICLE: bool = True         # 방송인이 작성한 새 카페 게시글 알림 받기
NOTIFY_CAFE_COMMENT: bool = True         # 방송인이 작성한 새 카페 댓글 알림 받기

# [X(구 트위터) 알림 옵션] - X 계정이 연동된 방송인에게만 적용
NOTIFY_X_POST: bool = True               # 새 포스트(일반 글·인용) 알림 받기
NOTIFY_X_REPLY: bool = False             # 답글 알림 받기
NOTIFY_X_RETWEET: bool = False           # 재게시(리트윗) 알림 받기

# X는 봇 탐지 방지를 위해 치지직/카페보다 긴 계정별 최소 조회 간격(초)을 적용합니다.
X_POLL_MIN_INTERVAL_SECONDS: int = 180

# ------------------------------------------------------------------------------
# 4. 내부 상태 및 로그 파일 경로
# ------------------------------------------------------------------------------
STATE_FILE_PATH: Path = BASE_DIR / "state.json"
LOGS_DIR: Path = BASE_DIR / "logs"
LOG_FILE_PATH: Path = LOGS_DIR / "taemtaem.log"

# 쿠키 대칭키 암호화 시드 (지정하지 않으면 settings.json의 고유 secret_key로 자동 파생)
COOKIE_ENCRYPTION_KEY: str = os.getenv("COOKIE_ENCRYPTION_KEY", "").strip()

# 공통 HTTP 요청 헤더 (일반 브라우저처럼 보이도록 설정)
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
