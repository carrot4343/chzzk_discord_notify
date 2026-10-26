"""
[초심자용 ID 자동 찾기 도우미]
복잡한 개발자 도구(F12)를 열지 않고도:
1. 방송인 이름으로 '치지직 32자리 채널 ID'를 찾아주고,
2. 방송인이 쓴 네이버 카페 게시글 주소(URL) 1개만 입력하면
   'NAVER_CAFE_CLUB_ID'와 'NAVER_CAFE_MEMBER_KEY'를 자동으로 찾아줍니다.
"""

import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote
import requests

# 상위 폴더의 config 모듈을 불러올 수 있도록 경로 추가
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config  # noqa: E402
from collectors.chzzk import search_channel_id_by_name  # noqa: E402


def resolve_club_id_from_slug(cafe_slug: str) -> Optional[str]:
    """카페 영문 주소(예: cafe.naver.com/xxxx)에서 숫자 clubid를 추출합니다."""
    # 방법 1: CafeGateInfo API 호출
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

    # 방법 2: 카페 웹페이지 HTML에서 clubid 정규식 추출
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
    """
    다양한 형태의 네이버 카페 게시글 URL에서 (club_id, article_id)를 추출합니다.
    """
    # iframe_url_utf8=%2Fca-fe%2Fcafes%2F... 형태의 인코딩된 URL을 최대 2회 디코딩
    decoded_url = unquote(unquote(article_url.strip()))

    # 패턴 1: /cafes/{club_id}/articles/{article_id}
    m1 = re.search(r"/cafes/(\d+)/articles/(\d+)", decoded_url)
    if m1:
        return m1.group(1), m1.group(2)

    # 패턴 2: clubid=1234&articleid=5678
    m_club = re.search(r"clubid=(\d+)", decoded_url, re.IGNORECASE)
    m_art = re.search(r"articleid=(\d+)", decoded_url, re.IGNORECASE)
    if m_club and m_art:
        return m_club.group(1), m_art.group(1)

    # 패턴 3: https://cafe.naver.com/{cafe_slug}/{article_id}
    m3 = re.search(
        r"cafe\.naver\.com/(?:ca-fe/web/)?([a-zA-Z0-9_-]+)/(\d+)", decoded_url
    )
    if m3 and m3.group(1) not in ("cafes", "f-e"):
        cafe_slug = m3.group(1)
        art_id = m3.group(2)
        club_id = resolve_club_id_from_slug(cafe_slug)
        return club_id, art_id

    return None, None


def find_writer_member_key(club_id: str, article_id: str) -> Optional[dict[str, str]]:
    """게시글 상세 API를 호출해 작성자의 닉네임과 고유 memberKey를 조회합니다."""
    cookies = {}
    if config.NAVER_NID_AUT and config.NAVER_NID_SES:
        cookies["NID_AUT"] = config.NAVER_NID_AUT
        cookies["NID_SES"] = config.NAVER_NID_SES

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
                nick = writer.get("nick") or "알 수 없음"
                if member_key:
                    return {"memberKey": str(member_key), "nick": str(nick)}
        except Exception:
            pass
    return None


def main() -> None:
    print("=" * 65)
    print(" 🔍 치지직 채널 ID & 네이버 카페 방송인 ID 자동 추출 도우미")
    print("=" * 65)

    # 1. 치지직 채널 ID 찾기
    streamer = input(
        f"\n[1] 치지직에서 검색할 방송인 이름을 입력하세요 (엔터 시 '{config.STREAMER_NAME}'): "
    ).strip()
    if not streamer:
        streamer = config.STREAMER_NAME

    chzzk_id = search_channel_id_by_name(streamer)
    if chzzk_id:
        print(f"  ✅ 찾은 치지직 채널 ID: {chzzk_id}")
        print(f"     (채널 주소: https://chzzk.naver.com/{chzzk_id})")
    else:
        print("  ⚠️ 치지직 채널을 찾지 못했습니다. 이름을 다시 확인해 주세요.")

    # 2. 네이버 카페 정보 찾기
    print("\n" + "-" * 65)
    print("[2] 네이버 카페에서 해당 방송인이 작성한 '아무 게시글 주소(URL)' 1개를 붙여넣으세요.")
    print("    (건너뛰려면 그냥 엔터를 누르세요)")
    cafe_url = input("👉 게시글 URL 입력: ").strip()

    if cafe_url:
        club_id, article_id = parse_cafe_article_url(cafe_url)
        if not club_id or not article_id:
            print("  ❌ URL에서 카페 번호나 게시글 번호를 인식하지 못했습니다.")
            print("     예시 형태: https://cafe.naver.com/카페주소/12345")
        else:
            print(f"  ✅ 카페 고유 ID (NAVER_CAFE_CLUB_ID): {club_id}")
            writer_info = find_writer_member_key(club_id, article_id)
            if writer_info:
                print(f"  ✅ 게시글 작성자 닉네임: {writer_info['nick']}")
                print(
                    f"  ✅ 방송인 멤버 키 (NAVER_CAFE_MEMBER_KEY): {writer_info['memberKey']}"
                )
                print("\n📋 아래 4줄을 복사해서 .env 파일에 그대로 붙여넣으시면 됩니다!")
                print("-" * 65)
                print(f"STREAMER_NAME={streamer}")
                print(f"CHZZK_CHANNEL_ID={chzzk_id or ''}")
                print(f"NAVER_CAFE_CLUB_ID={club_id}")
                print(f"NAVER_CAFE_MEMBER_KEY={writer_info['memberKey']}")
                print("-" * 65)
            else:
                print(
                    "  ⚠️ 게시글 작성자 정보를 가져오지 못했습니다. "
                    "(비공개 게시글이라면 .env에 NAVER_NID_AUT, NAVER_NID_SES 쿠키를 먼저 넣은 뒤 다시 실행해 보세요!)"
                )
    else:
        print("\n📋 아래 2줄을 복사해서 .env 파일에 붙여넣으시면 됩니다!")
        print("-" * 65)
        print(f"STREAMER_NAME={streamer}")
        print(f"CHZZK_CHANNEL_ID={chzzk_id or ''}")
        print("-" * 65)


if __name__ == "__main__":
    main()
