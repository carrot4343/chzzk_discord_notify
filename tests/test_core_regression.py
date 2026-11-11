r"""
치지직(Chzzk), 네이버 카페(Naver Cafe), 웹후크 보안(SSRF 방어), 메인 병렬 순찰 루프의
핵심 동작을 패치마다 자동 검증하는 회귀 테스트 스위트입니다.
실행 방법: .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
"""

import unittest
from unittest.mock import patch

from collectors import chzzk, naver_cafe
import main
from notifiers.discord_webhook import validate_discord_webhook_url
from utils.state_manager import get_default_streamer_state


class TestCoreRegressionSuite(unittest.TestCase):
    """치지직·네이버 카페·보안·순찰 엔진 통합 회귀 테스트"""

    def setUp(self) -> None:
        chzzk._chzzk_detail_cache.clear()
        naver_cafe._articles_cache.clear()
        naver_cafe._comments_cache.clear()
        naver_cafe._body_cache.clear()

    def test_01_chzzk_lifecycle_open_title_category_close(self) -> None:
        """[Core-01] 치지직 최초 초기화 -> 뱅온(멘션 포함) -> 방제/카테고리 변경 -> 뱅종 감지 검증"""
        streamer = {
            "id": "st_chzzk_1",
            "enabled": True,
            "name": "탬탬버린",
            "chzzk_channel_id": "a7e175625fdea5a7d98428302b7aa57f",
            "custom_webhook_url": "https://discord.com/api/webhooks/111/chzzk",
            "notify_chzzk_open": True,
            "notify_chzzk_close": True,
            "notify_chzzk_title": True,
            "notify_chzzk_category": True,
            "notify_chzzk_community": False,
            "mention_role_id": "777888999",
            "mention_on_open_only": True,
        }
        st_state = get_default_streamer_state()
        g_cfg: dict = {}

        sent_payloads: list[dict] = []

        def _capture_send(**kwargs):
            sent_payloads.append(kwargs)
            return True

        detail_close = {
            "status": "CLOSE",
            "liveId": 100,
            "liveTitle": "어제 방송",
            "liveCategoryValue": "Just Chatting",
            "liveImageUrl": "",
            "channel": {
                "channelName": "탬탬버린",
                "channelImageUrl": "https://nng-phinf.pstatic.net/profile.png",
            },
        }

        # 1) 최초 순찰(OFFLINE): 상태만 기록하고 알림은 보내지 않음
        with patch.object(chzzk, "fetch_chzzk_live_detail", return_value=detail_close), \
             patch.object(chzzk, "send_discord_notification", side_effect=_capture_send):
            changed = chzzk.check_chzzk_for_streamer(streamer, st_state, g_cfg)
            self.assertTrue(changed)
            self.assertTrue(st_state["chzzk"]["initialized"])
            self.assertEqual(len(sent_payloads), 0)

        # 2) 방송 시작(CLOSE -> OPEN): 뱅온 알림 + 역할 멘션(<@&777888999>) 포함 검증
        detail_open = {
            **detail_close,
            "status": "OPEN",
            "liveId": 101,
            "liveTitle": "오늘의 본방송 시작!",
            "liveCategoryValue": "마인크래프트",
        }
        with patch.object(chzzk, "fetch_chzzk_live_detail", return_value=detail_open), \
             patch.object(chzzk, "send_discord_notification", side_effect=_capture_send):
            changed = chzzk.check_chzzk_for_streamer(streamer, st_state, g_cfg)
            self.assertTrue(changed)
            self.assertEqual(len(sent_payloads), 1)
            self.assertIn("<@&777888999>", sent_payloads[0]["content"])
            self.assertIn("방송이 시작되었습니다", sent_payloads[0]["content"])

        # 3) 방송 중 방제 및 카테고리 변경: 뱅온 전용 멘션이므로 멘션 없이 변경 알림 발송
        detail_changed = {
            **detail_open,
            "liveTitle": "2부 공포게임 달립니다",
            "liveCategoryValue": "공포게임",
        }
        with patch.object(chzzk, "fetch_chzzk_live_detail", return_value=detail_changed), \
             patch.object(chzzk, "send_discord_notification", side_effect=_capture_send):
            changed = chzzk.check_chzzk_for_streamer(streamer, st_state, g_cfg)
            self.assertTrue(changed)
            self.assertEqual(len(sent_payloads), 3)  # 방제 변경 + 카테고리 변경 2건 추가
            self.assertNotIn("<@&777888999>", sent_payloads[1]["content"])
            self.assertNotIn("<@&777888999>", sent_payloads[2]["content"])

        # 4) 방송 종료(OPEN -> CLOSE): 뱅종 알림 발송
        detail_ended = {**detail_changed, "status": "CLOSE"}
        with patch.object(chzzk, "fetch_chzzk_live_detail", return_value=detail_ended), \
             patch.object(chzzk, "send_discord_notification", side_effect=_capture_send):
            changed = chzzk.check_chzzk_for_streamer(streamer, st_state, g_cfg)
            self.assertTrue(changed)
            self.assertEqual(len(sent_payloads), 4)
            self.assertIn("종료되었습니다", sent_payloads[3]["content"])

    def test_02_naver_cafe_url_parsing_and_fe_link_generation(self) -> None:
        """[Core-02] 네이버 카페 게시글 URL 파싱 및 PC/모바일 호환 /f-e/cafes/ 링크 생성 검증"""
        c1, a1 = naver_cafe.parse_cafe_article_url(
            "https://cafe.naver.com/f-e/cafes/28993571/articles/59153"
        )
        self.assertEqual((c1, a1), ("28993571", "59153"))

        c2, a2 = naver_cafe.parse_cafe_article_url(
            "https://cafe.naver.com/ArticleRead.nhn?clubid=28993571&articleid=59153"
        )
        self.assertEqual((c2, a2), ("28993571", "59153"))

        streamer = {
            "id": "st_cafe_1",
            "enabled": True,
            "name": "탬탬버린",
            "cafe_club_id": "28993571",
            "cafe_member_key": "mkey_123",
            "custom_webhook_url": "https://discord.com/api/webhooks/111/cafe",
            "notify_cafe_article": True,
            "notify_cafe_comment": True,
        }
        st_state = get_default_streamer_state()
        g_cfg = {"naver_nid_aut": "aut", "naver_nid_ses": "ses"}

        sent_payloads: list[dict] = []

        def _capture_send(**kwargs):
            sent_payloads.append(kwargs)
            return True

        init_articles = [{"articleId": 100, "subject": "기존 공지"}]
        init_comments = [{"commentId": 500, "articleId": 100, "content": "기존 댓글"}]

        # 첫 실행: 초기화만 수행
        with patch.object(naver_cafe, "fetch_member_articles", return_value=init_articles), \
             patch.object(naver_cafe, "fetch_member_comments", return_value=init_comments), \
             patch.object(naver_cafe, "send_discord_notification", side_effect=_capture_send):
            changed = naver_cafe.check_naver_cafe_for_streamer(streamer, st_state, g_cfg)
            self.assertTrue(changed)
            self.assertEqual(len(sent_payloads), 0)

        # 두 번째 실행: 새 게시글(101) 및 새 댓글(501) 감지 + /f-e/cafes/ 주소 확인
        next_articles = [
            {
                "articleId": 101,
                "subject": "이번 주 방송 일정표",
                "writernickname": "탬탬버린",
                "clubMenu": {"menuname": "공지사항"},
            },
            *init_articles,
        ]
        next_comments = [
            {
                "commentId": 501,
                "articleId": 101,
                "content": "확인 부탁드려요!",
                "article": {"subject": "이번 주 방송 일정표"},
            },
            *init_comments,
        ]
        with patch.object(naver_cafe, "fetch_member_articles", return_value=next_articles), \
             patch.object(naver_cafe, "fetch_member_comments", return_value=next_comments), \
             patch.object(naver_cafe, "fetch_article_body_summary", return_value=("일정 본문입니다.", None)), \
             patch.object(naver_cafe, "send_discord_notification", side_effect=_capture_send):
            changed = naver_cafe.check_naver_cafe_for_streamer(streamer, st_state, g_cfg)
            self.assertTrue(changed)
            self.assertEqual(len(sent_payloads), 2)
            # 흰 화면 버그가 수정된 /f-e/cafes/ 경로인지 검증
            self.assertEqual(
                sent_payloads[0]["embed_url"],
                "https://cafe.naver.com/f-e/cafes/28993571/articles/101",
            )
            self.assertEqual(
                sent_payloads[1]["embed_url"],
                "https://cafe.naver.com/f-e/cafes/28993571/articles/101",
            )

    def test_03_webhook_ssrf_validation(self) -> None:
        """[Core-03] 디스코드 공식 웹후크 URL 검증 및 내부망 SSRF 주소 차단 검증"""
        self.assertTrue(
            validate_discord_webhook_url(
                "https://discord.com/api/webhooks/1234567890/abcdefg_HIJKLMN-123"
            )
        )
        self.assertTrue(
            validate_discord_webhook_url(
                "https://discordapp.com/api/webhooks/1234567890/abcdefg_HIJKLMN-123"
            )
        )
        # 비공식 도메인, HTTP, 내부 IP 차단 확인
        self.assertFalse(validate_discord_webhook_url("http://127.0.0.1:5000/api/settings"))
        self.assertFalse(validate_discord_webhook_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(validate_discord_webhook_url("https://evil.example.com/api/webhooks/1/a"))

    def test_04_unified_main_check_streamer_task(self) -> None:
        """[Core-04] main._check_streamer_task에서 치지직·카페·X 3대 수집기가 통합 순찰되는지 검증"""
        streamer = {
            "id": "st_unified_1",
            "name": "통합방송인",
            "chzzk_channel_id": "a7e175625fdea5a7d98428302b7aa57f",
            "cafe_club_id": "28993571",
            "cafe_member_key": "mkey_123",
            "x_screen_name": "2chamcham2",
            "x_user_id": "555",
        }
        st_state = get_default_streamer_state()
        task = {
            "streamer": streamer,
            "st_state": st_state,
            "g_cfg": {},
            "name": "통합방송인",
        }

        with patch("main.check_chzzk_for_streamer", return_value=True) as m_chzzk, \
             patch("main.check_naver_cafe_for_streamer", return_value=True) as m_cafe, \
             patch("main.check_twitter_x_for_streamer", return_value=True) as m_x:
            st_chg, dt_chg, err = main._check_streamer_task(task)
            self.assertIsNone(err)
            self.assertTrue(st_chg)
            self.assertFalse(dt_chg)
            m_chzzk.assert_called_once()
            m_cafe.assert_called_once()
            m_x.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
