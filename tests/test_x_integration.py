r"""
X(구 트위터) 디스코드 알림 연동 통합 테스트 스위트 (총 15개 핵심 시나리오 검증)
실행 방법: .\venv\Scripts\python.exe -m unittest -v tests/test_x_integration.py
"""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

import config
from bot import discord_bot
from collectors import twitter_x as tx
from utils import settings_manager
from utils.state_manager import get_default_streamer_state
import web_ui


def _make_mock_graphql_timeline_response() -> dict:
    """고정 트윗, 일반 포스트(사진/링크 포함), 답글, 리트윗, 타인 트윗이 섞인 실제 구조의 X GraphQL 응답을 생성합니다."""
    return {
        "data": {
            "user": {
                "result": {
                    "timeline_v2": {
                        "timeline": {
                            "instructions": [
                                {
                                    "type": "TimelinePinEntry",
                                    "entry": {
                                        "entryId": "tweet-9999999999",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "9999999999",
                                                        "legacy": {
                                                            "user_id_str": "555",
                                                            "full_text": "고정된 옛날 공지 트윗 (무시되어야 함)",
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                    },
                                },
                                {
                                    "type": "TimelineAddEntries",
                                    "entries": [
                                        {
                                            "entryId": "promoted-tweet-8888",
                                            "content": {},
                                        },
                                        {
                                            "entryId": "tweet-2003",
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": {
                                                            "__typename": "Tweet",
                                                            "rest_id": "2003",
                                                            "core": {
                                                                "user_results": {
                                                                    "result": {
                                                                        "rest_id": "555",
                                                                        "legacy": {
                                                                            "screen_name": "2chamcham2",
                                                                            "name": "탬탬버린",
                                                                            "profile_image_url_https": "https://pbs.twimg.com/profile_images/1/avatar_normal.jpg",
                                                                        },
                                                                    }
                                                                }
                                                            },
                                                            "legacy": {
                                                                "user_id_str": "555",
                                                                "full_text": "오늘 저녁 8시 치지직 뱅온! https://t.co/liveurl https://t.co/mediaurl",
                                                                "entities": {
                                                                    "urls": [
                                                                        {
                                                                            "url": "https://t.co/liveurl",
                                                                            "expanded_url": "https://chzzk.naver.com/live/a7e17562",
                                                                        }
                                                                    ],
                                                                    "media": [
                                                                        {
                                                                            "url": "https://t.co/mediaurl",
                                                                            "media_url_https": "https://pbs.twimg.com/media/banner123.jpg",
                                                                        }
                                                                    ],
                                                                },
                                                            },
                                                        }
                                                    }
                                                }
                                            },
                                        },
                                        {
                                            "entryId": "profile-conversation-2002",
                                            "content": {
                                                "items": [
                                                    {
                                                        "item": {
                                                            "itemContent": {
                                                                "tweet_results": {
                                                                    "result": {
                                                                        "__typename": "Tweet",
                                                                        "rest_id": "1999",
                                                                        "core": {
                                                                            "user_results": {
                                                                                "result": {
                                                                                    "rest_id": "777",
                                                                                    "legacy": {
                                                                                        "screen_name": "other_fan",
                                                                                        "name": "팬계정",
                                                                                    },
                                                                                }
                                                                            }
                                                                        },
                                                                        "legacy": {
                                                                            "user_id_str": "777",
                                                                            "full_text": "타인이 작성한 질문 트윗 (필터링되어야 함)",
                                                                        },
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    },
                                                    {
                                                        "item": {
                                                            "itemContent": {
                                                                "tweet_results": {
                                                                    "result": {
                                                                        "__typename": "Tweet",
                                                                        "rest_id": "2002",
                                                                        "core": {
                                                                            "user_results": {
                                                                                "result": {
                                                                                    "rest_id": "555",
                                                                                    "legacy": {
                                                                                        "screen_name": "2chamcham2",
                                                                                        "name": "탬탬버린",
                                                                                    },
                                                                                }
                                                                            }
                                                                        },
                                                                        "legacy": {
                                                                            "user_id_str": "555",
                                                                            "full_text": "@other_fan 네 오늘 꼭 오세요!",
                                                                            "in_reply_to_status_id_str": "1999",
                                                                            "in_reply_to_screen_name": "other_fan",
                                                                        },
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    },
                                                ]
                                            },
                                        },
                                        {
                                            "entryId": "tweet-2001",
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": {
                                                            "__typename": "Tweet",
                                                            "rest_id": "2001",
                                                            "core": {
                                                                "user_results": {
                                                                    "result": {
                                                                        "rest_id": "555",
                                                                        "legacy": {
                                                                            "screen_name": "2chamcham2",
                                                                            "name": "탬탬버린",
                                                                        },
                                                                    }
                                                                }
                                                            },
                                                            "legacy": {
                                                                "user_id_str": "555",
                                                                "full_text": "RT @official_chzzk: 이벤트 공지",
                                                                "retweeted_status_result": {
                                                                    "result": {
                                                                        "__typename": "Tweet",
                                                                        "rest_id": "1500",
                                                                        "core": {
                                                                            "user_results": {
                                                                                "result": {
                                                                                    "rest_id": "888",
                                                                                    "legacy": {
                                                                                        "screen_name": "official_chzzk",
                                                                                        "name": "치지직 공식",
                                                                                    },
                                                                                }
                                                                            }
                                                                        },
                                                                        "legacy": {
                                                                            "full_text": "치지직 공식 이벤트 안내입니다!",
                                                                        },
                                                                    }
                                                                },
                                                            },
                                                        }
                                                    }
                                                }
                                            },
                                        },
                                    ],
                                },
                            ]
                        }
                    }
                }
            }
        }
    }


class TestXIntegrationSuite(unittest.TestCase):
    """X(트위터) 디스코드 알림 연동 전체 단위/통합 테스트 케이스"""

    def setUp(self) -> None:
        tx._tweets_cache.clear()
        tx._user_info_cache.clear()
        tx._x_cookie_alert_timestamps.clear()

    def test_01_parse_x_screen_name(self) -> None:
        """[TC-01] 다양한 형태의 X 핸들 및 URL에서 아이디(screen_name) 추출 및 예약어 차단 검증"""
        self.assertEqual(tx.parse_x_screen_name("@2chamcham2"), "2chamcham2")
        self.assertEqual(tx.parse_x_screen_name("2chamcham2"), "2chamcham2")
        self.assertEqual(tx.parse_x_screen_name("https://x.com/2chamcham2"), "2chamcham2")
        self.assertEqual(
            tx.parse_x_screen_name("https://twitter.com/2chamcham2/status/18500000000"),
            "2chamcham2",
        )
        self.assertEqual(
            tx.parse_x_screen_name("https://mobile.x.com/user_name_123/with_replies?s=20"),
            "user_name_123",
        )
        # 예약 경로 및 잘못된 입력 차단
        self.assertIsNone(tx.parse_x_screen_name("https://x.com/home"))
        self.assertIsNone(tx.parse_x_screen_name("https://x.com/explore"))
        self.assertIsNone(tx.parse_x_screen_name("https://x.com/i/flow/login"))
        self.assertIsNone(tx.parse_x_screen_name("too_long_screen_name_over_15_chars"))
        self.assertIsNone(tx.parse_x_screen_name("공백 포함 아이디"))
        self.assertIsNone(tx.parse_x_screen_name(""))

    def test_02_cookie_encryption_and_decryption(self) -> None:
        """[TC-02] settings_manager에서 x_auth_token 및 x_ct0 쿠키의 Fernet 암호화/복호화 검증"""
        raw_data = {
            "legacy_seed": {
                "global": {"x_auth_token": "seed_token", "x_ct0": "seed_ct0"}
            },
            "users": {
                "carrot4343": {
                    "global": {
                        "naver_nid_aut": "naver_aut",
                        "naver_nid_ses": "naver_ses",
                        "x_auth_token": "user_x_auth_999",
                        "x_ct0": "user_x_ct0_888",
                    }
                }
            },
            "guilds": {
                "123456": {
                    "global": {
                        "x_auth_token": "guild_x_auth_777",
                        "x_ct0": "guild_x_ct0_666",
                    }
                }
            },
        }
        encrypted = settings_manager._prepare_data_for_saving(raw_data)

        # 저장용 데이터는 모두 enc: 로 암호화되어야 함
        self.assertTrue(
            encrypted["legacy_seed"]["global"]["x_auth_token"].startswith("enc:")
        )
        self.assertTrue(
            encrypted["users"]["carrot4343"]["global"]["x_auth_token"].startswith("enc:")
        )
        self.assertTrue(
            encrypted["users"]["carrot4343"]["global"]["x_ct0"].startswith("enc:")
        )
        self.assertTrue(
            encrypted["guilds"]["123456"]["global"]["x_auth_token"].startswith("enc:")
        )

        # 복호화 호출 시 평문으로 정확히 복원되어야 함
        user_g = encrypted["users"]["carrot4343"]["global"]
        settings_manager._decrypt_dict_cookies(user_g)
        self.assertEqual(user_g["x_auth_token"], "user_x_auth_999")
        self.assertEqual(user_g["x_ct0"], "user_x_ct0_888")

    def test_03_extract_x_user_info_and_cookie_error(self) -> None:
        """[TC-03] X GraphQL UserByScreenName 계정 조회 및 쿠키 만료 예외(XCookieExpiredError) 검증"""
        mock_ok_resp = MagicMock()
        mock_ok_resp.status_code = 200
        mock_ok_resp.json.return_value = {
            "data": {
                "user": {
                    "result": {
                        "rest_id": "555",
                        "legacy": {
                            "screen_name": "2chamcham2",
                            "name": "탬탬버린",
                            "profile_image_url_https": "https://pbs.twimg.com/profile_images/1/pic_normal.png",
                        },
                    }
                }
            }
        }

        with patch("collectors.twitter_x.requests.get", return_value=mock_ok_resp):
            info = tx.extract_x_user_info("https://x.com/2chamcham2", "tok1", "ct0_1")
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info["user_id"], "555")
            self.assertEqual(info["screen_name"], "2chamcham2")
            self.assertEqual(info["name"], "탬탬버린")
            self.assertTrue(info["profile_image_url"].endswith("_400x400.png"))

        # 캐시 비운 뒤 401 응답 시 XCookieExpiredError 발생 확인
        tx._user_info_cache.clear()
        mock_unauth_resp = MagicMock()
        mock_unauth_resp.status_code = 401
        with patch("collectors.twitter_x.requests.get", return_value=mock_unauth_resp):
            with self.assertRaises(tx.XCookieExpiredError):
                tx.extract_x_user_info("@2chamcham2", "expired_tok", "expired_ct0")

    def test_04_parse_timeline_instructions(self) -> None:
        """[TC-04] 타임라인 응답에서 고정 트윗·광고·타인 트윗 제외 및 포스트/답글/리트윗·미디어·URL 복원 검증"""
        raw_resp = _make_mock_graphql_timeline_response()
        tweets = tx._parse_timeline_instructions(raw_resp, "555", "2chamcham2")

        # 고정 트윗(9999999999), 광고(promoted), 타인 트윗(1999)은 제외되고 2003, 2002, 2001 3개만 추출되어야 함
        self.assertEqual([t["id"] for t in tweets], [2003, 2002, 2001])

        # 2003: 일반 포스트 + URL 확장 + 첨부 미디어
        self.assertEqual(tweets[0]["type"], "post")
        self.assertIn("https://chzzk.naver.com/live/a7e17562", tweets[0]["text"])
        self.assertNotIn("https://t.co/mediaurl", tweets[0]["text"])
        self.assertEqual(
            tweets[0]["media_url"], "https://pbs.twimg.com/media/banner123.jpg"
        )

        # 2002: 답글(reply)
        self.assertEqual(tweets[1]["type"], "reply")
        self.assertEqual(tweets[1]["in_reply_to_screen_name"], "other_fan")

        # 2001: 리트윗(retweet)
        self.assertEqual(tweets[2]["type"], "retweet")
        self.assertIn("@official_chzzk", tweets[2]["text"])
        self.assertIn("치지직 공식 이벤트 안내입니다!", tweets[2]["text"])

    def test_05_rate_limit_3min_cooldown_cache(self) -> None:
        """[TC-05] 동일 X 계정에 대해 3분(180초) 이내 중복 API 호출을 차단하는 보호 캐시 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_mock_graphql_timeline_response()

        with patch("collectors.twitter_x.requests.get", return_value=mock_resp) as mock_get:
            first = tx.fetch_x_user_tweets(
                "555", "2chamcham2", {"auth_token": "a", "ct0": "c"}
            )
            second = tx.fetch_x_user_tweets(
                "555", "2chamcham2", {"auth_token": "a", "ct0": "c"}
            )
            self.assertEqual(len(first), 3)
            self.assertEqual(len(second), 3)
            # 두 번 연속 호출해도 실제 HTTP 요청은 딱 1번만 발생해야 함
            self.assertEqual(mock_get.call_count, 1)

    def test_06_check_twitter_x_lifecycle_and_filters(self) -> None:
        """[TC-06] 최초 실행 시 알림 폭탄 방지 초기화 -> 이후 새 포스트/답글/리트윗 필터별 알림 발송 검증"""
        streamer = {
            "id": "st_test_1",
            "enabled": True,
            "name": "탬탬버린",
            "x_screen_name": "2chamcham2",
            "x_user_id": "555",
            "custom_webhook_url": "https://discord.com/api/webhooks/111/aaa",
            "notify_x_post": True,
            "notify_x_reply": False,     # 답글 알림 OFF
            "notify_x_retweet": True,    # 리트윗 알림 ON
        }
        st_state = get_default_streamer_state()
        g_cfg = {"x_auth_token": "tok", "x_ct0": "ct0"}

        initial_tweets = [
            {
                "id": 2000,
                "id_str": "2000",
                "type": "post",
                "text": "과거 포스트",
                "media_url": "",
                "screen_name": "2chamcham2",
                "author_name": "탬탬버린",
                "profile_image_url": "https://pbs.twimg.com/profile_images/1/a.jpg",
                "in_reply_to_screen_name": "",
                "url": "https://x.com/2chamcham2/status/2000",
            }
        ]

        sent_Payloads: list[dict] = []

        def _capture_send(**kwargs):
            sent_Payloads.append(kwargs)
            return True

        # 1) 첫 순찰: 초기화만 수행되고 디스코드 알림은 0건이어야 함
        with patch.object(tx, "fetch_x_user_tweets", return_value=initial_tweets), \
             patch.object(tx, "send_discord_notification", side_effect=_capture_send):
            changed = tx.check_twitter_x_for_streamer(streamer, st_state, g_cfg)
            self.assertTrue(changed)
            self.assertTrue(st_state["twitter_x"]["initialized"])
            self.assertEqual(st_state["twitter_x"]["last_tweet_id"], 2000)
            self.assertEqual(len(sent_Payloads), 0)

        # 2) 두 번째 순찰: 2001(리트윗), 2002(답글-꺼짐), 2003(새 포스트) 감지 시
        #    답글(2002)은 건너뛰고 리트윗(2001)과 새 포스트(2003) 2건만 순서대로 발송되어야 함
        parsed_new = tx._parse_timeline_instructions(
            _make_mock_graphql_timeline_response(), "555", "2chamcham2"
        )
        with patch.object(tx, "fetch_x_user_tweets", return_value=parsed_new), \
             patch.object(tx, "send_discord_notification", side_effect=_capture_send):
            changed = tx.check_twitter_x_for_streamer(streamer, st_state, g_cfg)
            self.assertTrue(changed)
            self.assertEqual(st_state["twitter_x"]["last_tweet_id"], 2003)
            self.assertEqual(len(sent_Payloads), 2)
            self.assertIn("리트윗", sent_Payloads[0]["content"])
            self.assertIn("새 포스트", sent_Payloads[1]["content"])
            self.assertEqual(
                sent_Payloads[1]["image_url"],
                "https://pbs.twimg.com/media/banner123.jpg",
            )

    def test_07_role_mention_policy(self) -> None:
        """[TC-07] 치지직 연동 방송인 vs X 단독 방송인의 역할 멘션 정책 검증"""
        # 1) 치지직이 연동된 방송인 + 뱅온 전용 멘션(기본값 True) -> X 포스트에서는 멘션 안 함
        st_combo = {
            "chzzk_channel_id": "a7e175625fdea5a7d98428302b7aa57f",
            "x_screen_name": "2chamcham2",
            "mention_role_id": "123456789",
            "mention_on_open_only": True,
        }
        self.assertEqual(tx._build_x_mention_prefix(st_combo, is_post=True), "")

        # 2) X 단독 방송인 + 뱅온 전용 멘션(기본값 True) -> 새 포스트에서만 멘션, 답글/리트윗은 멘션 안 함
        st_x_only = {
            "chzzk_channel_id": "",
            "cafe_club_id": "",
            "cafe_member_key": "",
            "x_screen_name": "solo_x_user",
            "mention_role_id": "123456789",
            "mention_on_open_only": True,
        }
        self.assertEqual(
            tx._build_x_mention_prefix(st_x_only, is_post=True), "<@&123456789> "
        )
        self.assertEqual(tx._build_x_mention_prefix(st_x_only, is_post=False), "")

        # 3) 모든 알림에서 멘션(mention_on_open_only=False) -> 치지직 연동 여부와 무관하게 멘션
        st_all = {
            **st_combo,
            "mention_on_open_only": False,
        }
        self.assertEqual(
            tx._build_x_mention_prefix(st_all, is_post=False), "<@&123456789> "
        )

    def test_08_cookie_expired_alert_24h_cooldown(self) -> None:
        """[TC-08] X 쿠키 만료 시 디스코드 경고 발송 및 24시간 쿨다운 중복 방지 검증"""
        alerts: list[dict] = []

        def _capture_alert(**kwargs):
            alerts.append(kwargs)
            return True

        webhook = "https://discord.com/api/webhooks/999/alert_test"
        with patch.object(tx, "send_discord_notification", side_effect=_capture_alert):
            tx._handle_x_cookie_expired_alert(webhook, "탬탬버린")
            tx._handle_x_cookie_expired_alert(webhook, "탬탬버린")  # 연속 호출 시 쿨다운으로 차단되어야 함
            self.assertEqual(len(alerts), 1)
            self.assertIn("쿠키 만료 경고", alerts[0]["content"])

    def test_09_web_ui_api_x_only_and_combo_streamer(self) -> None:
        """[TC-09] 웹 대시보드 API에서 X 단독 방송인 저장 허용, 빈 입력 거부, 미리보기 발송 검증"""
        fake_user_settings = {
            "username": "carrot4343",
            "is_admin": True,
            "poll_interval_seconds": 60,
            "global": {
                "discord_webhook_url": "https://discord.com/api/webhooks/123456/valid_token",
                "naver_nid_aut": "",
                "naver_nid_ses": "",
                "x_auth_token": "tok_123",
                "x_ct0": "ct0_123",
            },
            "streamers": [],
        }

        saved_streamers_holder: list[list[dict]] = []

        def _fake_save_user(username, user_global=None, user_streamers=None, poll_interval_seconds=None):
            if user_streamers is not None:
                fake_user_settings["streamers"] = user_streamers
                saved_streamers_holder.append(user_streamers)

        client = web_ui.app.test_client()
        with client.session_transaction() as sess:
            sess["username"] = "carrot4343"

        mock_x_info = {
            "user_id": "555",
            "screen_name": "2chamcham2",
            "name": "탬탬버린",
            "profile_image_url": "https://pbs.twimg.com/profile_images/1/pic_400x400.png",
        }

        with patch("web_ui.load_user_settings", return_value=fake_user_settings), \
             patch("web_ui.save_user_settings", side_effect=_fake_save_user), \
             patch("web_ui.extract_x_user_info", return_value=mock_x_info):

            # 1) 치지직·카페·X 모두 비어있을 때 -> 저장 거부
            res_empty = client.post("/api/streamers", json={"name": "빈방송인"})
            self.assertFalse(res_empty.get_json()["ok"])

            # 2) 치지직·카페 없이 X 계정만 입력한 'X 단독 방송인' -> 정상 저장
            res_x_only = client.post(
                "/api/streamers",
                json={
                    "x_input": "@2chamcham2",
                    "notify_x_post": True,
                    "notify_x_reply": True,
                    "notify_x_retweet": False,
                },
            )
            data_x = res_x_only.get_json()
            self.assertTrue(data_x["ok"], msg=data_x)
            self.assertEqual(len(fake_user_settings["streamers"]), 1)
            saved_st = fake_user_settings["streamers"][0]
            self.assertEqual(saved_st["x_screen_name"], "2chamcham2")
            self.assertEqual(saved_st["x_user_id"], "555")
            self.assertEqual(saved_st["name"], "탬탬버린")
            self.assertTrue(saved_st["notify_x_post"])
            self.assertTrue(saved_st["notify_x_reply"])

    def test_10_discord_bot_slash_commands_and_config_view(self) -> None:
        """[TC-10] 디스코드 슬래시 명령어(/x연동, /트위터연동, /x쿠키설정, /트위터쿠키설정) 및 StreamerConfigView 5줄 버튼 배치·격리 검증"""
        cmd_names = {c.name for c in discord_bot.bot_client.tree.get_commands()}
        self.assertIn("x연동", cmd_names)
        self.assertIn("트위터연동", cmd_names)
        self.assertIn("x쿠키설정", cmd_names)
        self.assertIn("트위터쿠키설정", cmd_names)

        sample_st = {
            "id": "st_1",
            "enabled": True,
            "name": "탬탬버린",
            "chzzk_channel_id": "a7e175625fdea5a7d98428302b7aa57f",
            "cafe_club_id": "30688884",
            "cafe_member_key": "member_key_1",
            "x_screen_name": "2chamcham2",
            "notify_x_post": True,
            "notify_x_reply": False,
            "notify_x_retweet": True,
        }
        status_text = discord_bot._format_streamer_status_lines(sample_st)
        self.assertIn("@2chamcham2 (포스트, 리트윗)", status_text)

        embed = discord_bot._build_streamer_config_embed(sample_st, "테스트서버")
        field_names = [f.name for f in embed.fields]
        self.assertIn("🐦 X(구 트위터) 알림 항목", field_names)

        async def _verify_view() -> None:
            with patch(
                "bot.discord_bot.load_guild_settings",
                return_value={"streamers": [sample_st]},
            ):
                view = discord_bot.StreamerConfigView("111", "테스트서버", "st_1")
                btn_labels = {
                    child.custom_id: child.label
                    for child in view.children
                    if hasattr(child, "custom_id")
                }
                btn_rows = {
                    child.custom_id: child.row
                    for child in view.children
                    if hasattr(child, "custom_id")
                }
                self.assertEqual(btn_labels.get("toggle_open"), "🟢 📺뱅온")
                self.assertEqual(btn_labels.get("toggle_cafe_art"), "🟢 ☕카페 게시글")
                self.assertEqual(btn_labels.get("toggle_x_post"), "🟢 🐦X 포스트")
                self.assertEqual(btn_labels.get("toggle_x_reply"), "⚫ 🐦X 답글")
                self.assertEqual(btn_labels.get("toggle_x_retweet"), "🟢 🐦X 리트윗")
                self.assertEqual(btn_labels.get("check_chzzk_status"), "🔍 치지직 상태 확인")
                self.assertEqual(btn_labels.get("check_cafe_latest"), "🔍 카페 최신글 확인")
                self.assertEqual(btn_labels.get("check_x_latest"), "🔍 X 최신포스트 확인")
                self.assertEqual(btn_labels.get("toggle_enabled"), "🟢 전체 알림 감시 중 (ON)")

                # Row 3(확인 버튼)과 Row 4(전체 알림 감시 마스터 스위치)가 서로 다른 줄에 완전히 분리되어 있는지 검증
                self.assertEqual(btn_rows.get("check_chzzk_status"), 3)
                self.assertEqual(btn_rows.get("check_cafe_latest"), 3)
                self.assertEqual(btn_rows.get("check_x_latest"), 3)
                self.assertEqual(btn_rows.get("toggle_enabled"), 4)

                # 전체 알림 감시 버튼(toggle_enabled) 클릭 시 send_preview_for_streamer가 절대 호출되지 않는지 검증
                fake_inter = MagicMock()
                fake_inter.user.guild_permissions.manage_guild = True
                fake_inter.response.edit_message = unittest.mock.AsyncMock()
                with patch("bot.discord_bot.save_guild_settings"), \
                     patch("bot.discord_bot.send_preview_for_streamer") as mock_preview:
                    await view.btn_enabled.callback(fake_inter)
                    mock_preview.assert_not_called()
                    self.assertFalse(sample_st["enabled"])

            # X 미연동 방송인의 경우 notify_x_post가 True로 남아있더라도 버튼은 꺼짐(⚫)으로 표시되어야 함
            unlinked_st = {
                "id": "st_2",
                "enabled": True,
                "name": "시즈키 토아",
                "x_screen_name": "",
                "notify_x_post": True,
            }
            with patch(
                "bot.discord_bot.load_guild_settings",
                return_value={"streamers": [unlinked_st]},
            ):
                view_unlinked = discord_bot.StreamerConfigView("111", "테스트서버", "st_2")
                unlinked_labels = {
                    child.custom_id: child.label
                    for child in view_unlinked.children
                    if hasattr(child, "custom_id")
                }
                self.assertEqual(unlinked_labels.get("toggle_x_post"), "⚫ 🐦X 포스트")

        asyncio.run(_verify_view())

    def test_11_web_ui_preview_sends_x_embed(self) -> None:
        """[TC-11] send_preview_for_streamer 실행 시 최신 X 포스트 미리보기 카드 전송 검증"""
        streamer = {
            "id": "st_preview_x",
            "name": "탬탬버린",
            "x_screen_name": "2chamcham2",
            "x_user_id": "555",
            "custom_webhook_url": "https://discord.com/api/webhooks/123456/valid_token",
        }
        g_cfg = {"x_auth_token": "tok_123", "x_ct0": "ct0_123"}
        mock_tweets = tx._parse_timeline_instructions(
            _make_mock_graphql_timeline_response(), "555", "2chamcham2"
        )
        sent_previews: list[dict] = []

        def _capture_preview(**kwargs):
            sent_previews.append(kwargs)
            return True

        with patch("web_ui.fetch_x_user_tweets", return_value=mock_tweets), \
             patch("web_ui.send_discord_notification", side_effect=_capture_preview):
            ok, msg = web_ui.send_preview_for_streamer(streamer, g_cfg, username="carrot4343")
            self.assertTrue(ok)
            self.assertIn("1건", msg)
            self.assertEqual(len(sent_previews), 1)
            self.assertIn("[실전 미리보기] 탬탬버린", sent_previews[0]["content"])
            self.assertEqual(
                sent_previews[0]["image_url"],
                "https://pbs.twimg.com/media/banner123.jpg",
            )

    def test_12_xlink_modal_without_cookies_and_public_fallback(self) -> None:
        """[TC-12] X 쿠키 미설정 상태에서도 공개 API 폴백으로 /x연동(XLinkModal) 방송인 추가가 정상 작동하는지 검증"""
        mock_fx_resp = MagicMock()
        mock_fx_resp.status_code = 200
        mock_fx_resp.json.return_value = {
            "code": 200,
            "user": {
                "id": "777888999",
                "screen_name": "shizukitoa",
                "name": "시즈키 토아",
                "avatar_url": "https://pbs.twimg.com/profile_images/99/toa_normal.jpg",
            },
        }

        with patch("collectors.twitter_x._build_x_cookies", return_value={}), \
             patch("collectors.twitter_x.requests.get", return_value=mock_fx_resp):
            info = tx.extract_x_user_info("@shizukitoa", None, None, True)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info["user_id"], "777888999")
            self.assertEqual(info["screen_name"], "shizukitoa")
            self.assertEqual(info["name"], "시즈키 토아")
            self.assertTrue(info["profile_image_url"].endswith("_400x400.jpg"))

        # XLinkModal.on_submit 검증 (쿠키 미설정 시에도 차단되지 않고 저장되어야 함)
        saved_guild_streamers: list[dict] = []

        def _fake_save_guild(guild_id, guild_name=None, guild_global=None, guild_streamers=None, channel_webhooks=None):
            if guild_streamers is not None:
                saved_guild_streamers.extend(guild_streamers)

        async def _run_modal_submit() -> None:
            fake_ch = MagicMock()
            fake_ch.id = 987654321
            fake_ch.name = "방송카페알림"
            fake_ch.mention = "#방송카페알림"

            existing_st = {
                "id": "st_157d6aea",
                "enabled": True,
                "name": "시즈키 토아",
                "x_screen_name": "",
                "custom_webhook_url": "https://discord.com/api/webhooks/111/222",
            }
            modal = discord_bot.XLinkModal(
                guild_id="1310839027594694696",
                guild_name="개인용",
                target_channel=fake_ch,
                existing_streamer=existing_st,
            )
            modal.x_handle_input._value = "@shizukitoa"
            modal.custom_name_input._value = "시즈키 토아"

            fake_interaction = MagicMock()
            fake_interaction.response.defer = unittest.mock.AsyncMock()
            fake_interaction.followup.send = unittest.mock.AsyncMock()

            with patch(
                "bot.discord_bot.load_guild_settings",
                return_value={"global": {}, "streamers": [existing_st]},
            ), patch(
                "bot.discord_bot._build_x_cookies", return_value={}
            ), patch(
                "bot.discord_bot.extract_x_user_info",
                return_value={
                    "user_id": "777888999",
                    "screen_name": "shizukitoa",
                    "name": "시즈키 토아",
                    "profile_image_url": "https://pbs.twimg.com/profile_images/99/toa_400x400.jpg",
                },
            ), patch(
                "bot.discord_bot._ensure_channel_webhook",
                new=unittest.mock.AsyncMock(
                    return_value="https://discord.com/api/webhooks/111/222"
                ),
            ), patch(
                "bot.discord_bot.save_guild_settings", side_effect=_fake_save_guild
            ):
                await modal.on_submit(fake_interaction)
                self.assertEqual(len(saved_guild_streamers), 1)
                self.assertEqual(saved_guild_streamers[0]["x_screen_name"], "shizukitoa")
                self.assertEqual(saved_guild_streamers[0]["x_user_id"], "777888999")
                self.assertTrue(saved_guild_streamers[0]["notify_x_post"])
                fake_interaction.followup.send.assert_called_once()

        asyncio.run(_run_modal_submit())

    def test_13_config_view_platform_check_buttons_and_permissions(self) -> None:
        """[TC-13] Row 3 확인 버튼 3종(치지직/카페/X) 클릭 시 해당 플랫폼만 검증하고 비관리자는 차단하는지 검증"""
        sample_st = {
            "id": "st_check_1",
            "enabled": True,
            "name": "탬탬버린",
            "chzzk_channel_id": "a7e175625fdea5a7d98428302b7aa57f",
            "cafe_club_id": "30688884",
            "cafe_member_key": "member_key_1",
            "x_screen_name": "2chamcham2",
        }
        g_cfg = {"x_auth_token": "tok", "x_ct0": "ct0"}

        async def _run() -> None:
            with patch(
                "bot.discord_bot.load_guild_settings",
                return_value={"global": g_cfg, "streamers": [sample_st]},
            ):
                view = discord_bot.StreamerConfigView("111", "테스트서버", "st_check_1")

                for btn, expected_platform in [
                    (view.btn_check_chzzk, "chzzk"),
                    (view.btn_check_cafe, "cafe"),
                    (view.btn_check_x, "x"),
                ]:
                    inter = MagicMock()
                    inter.user.guild_permissions.manage_guild = True
                    inter.response.defer = unittest.mock.AsyncMock()
                    inter.followup.send = unittest.mock.AsyncMock()

                    with patch(
                        "bot.discord_bot.send_preview_for_streamer",
                        return_value=(True, f"✅ {expected_platform} 확인 완료"),
                    ) as mock_preview:
                        await btn.callback(inter)
                        inter.response.defer.assert_awaited_once_with(ephemeral=True)
                        mock_preview.assert_called_once_with(
                            sample_st, g_cfg, "서버:테스트서버", expected_platform
                        )
                        inter.followup.send.assert_awaited_once_with(
                            f"✅ {expected_platform} 확인 완료", ephemeral=True
                        )

                # 서버 관리하기 권한이 없는 일반 유저는 실행 차단
                non_admin = MagicMock()
                non_admin.user.guild_permissions.manage_guild = False
                non_admin.response.send_message = unittest.mock.AsyncMock()
                with patch("bot.discord_bot.send_preview_for_streamer") as mock_preview:
                    await view.btn_check_x.callback(non_admin)
                    mock_preview.assert_not_called()
                    non_admin.response.send_message.assert_awaited_once()
                    self.assertIn(
                        "서버 관리하기",
                        non_admin.response.send_message.call_args.args[0],
                    )

        asyncio.run(_run())

    def test_14_unlinked_cafe_and_x_buttons_auto_open_modals(self) -> None:
        """[TC-14] 카페 또는 X 미연동 상태에서 알림 토글 버튼 클릭 시 각 연동 모달이 자동 실행되는지 검증"""
        unlinked_st = {
            "id": "st_unlinked_modal",
            "enabled": True,
            "name": "시즈키 토아",
            "cafe_club_id": "",
            "cafe_member_key": "",
            "x_screen_name": "",
        }

        async def _run() -> None:
            with patch(
                "bot.discord_bot.load_guild_settings",
                return_value={"global": {}, "streamers": [unlinked_st]},
            ):
                view = discord_bot.StreamerConfigView("111", "테스트서버", "st_unlinked_modal")

                cafe_inter = MagicMock()
                cafe_inter.user.guild_permissions.manage_guild = True
                cafe_inter.channel = MagicMock(spec=discord_bot.discord.TextChannel)
                cafe_inter.response.send_modal = unittest.mock.AsyncMock()
                await view.btn_cafe_art.callback(cafe_inter)
                cafe_inter.response.send_modal.assert_awaited_once()
                sent_cafe_modal = cafe_inter.response.send_modal.call_args.args[0]
                self.assertIsInstance(sent_cafe_modal, discord_bot.CafeLinkModal)
                self.assertEqual(sent_cafe_modal.existing_streamer["id"], "st_unlinked_modal")

                x_inter = MagicMock()
                x_inter.user.guild_permissions.manage_guild = True
                x_inter.channel = MagicMock(spec=discord_bot.discord.TextChannel)
                x_inter.response.send_modal = unittest.mock.AsyncMock()
                await view.btn_x_post.callback(x_inter)
                x_inter.response.send_modal.assert_awaited_once()
                sent_x_modal = x_inter.response.send_modal.call_args.args[0]
                self.assertIsInstance(sent_x_modal, discord_bot.XLinkModal)
                self.assertEqual(sent_x_modal.existing_streamer["id"], "st_unlinked_modal")

        asyncio.run(_run())

    def test_15_twitter_alias_slash_commands_open_same_modals(self) -> None:
        """[TC-15] /x연동·/트위터연동 및 /x쿠키설정·/트위터쿠키설정 명령어가 동일한 모달을 여는지 검증"""
        async def _run() -> None:
            mock_g_data = {
                "global": {"x_auth_token": "tok_saved", "x_ct0": "ct0_saved"},
                "streamers": [{"id": "st_1", "name": "탬탬버린"}],
            }

            def _make_admin_interaction():
                inter = MagicMock()
                inter.guild = MagicMock()
                inter.guild.name = "테스트서버"
                inter.guild_id = 111
                inter.user.guild_permissions.manage_guild = True
                inter.channel = MagicMock(spec=discord_bot.discord.TextChannel)
                inter.response.send_modal = unittest.mock.AsyncMock()
                return inter

            with patch("bot.discord_bot.load_guild_settings", return_value=mock_g_data):
                # 1) /x연동 vs /트위터연동
                inter_x = _make_admin_interaction()
                inter_tw = _make_admin_interaction()
                await discord_bot.cmd_link_x.callback(inter_x, 이름="탬탬버린")
                await discord_bot.cmd_link_twitter.callback(inter_tw, 이름="탬탬버린")

                modal_x = inter_x.response.send_modal.call_args.args[0]
                modal_tw = inter_tw.response.send_modal.call_args.args[0]
                self.assertIsInstance(modal_x, discord_bot.XLinkModal)
                self.assertIsInstance(modal_tw, discord_bot.XLinkModal)
                self.assertEqual(modal_x.existing_streamer["id"], "st_1")
                self.assertEqual(modal_tw.existing_streamer["id"], "st_1")

                # 2) /x쿠키설정 vs /트위터쿠키설정
                inter_xc = _make_admin_interaction()
                inter_twc = _make_admin_interaction()
                await discord_bot.cmd_set_x_cookies.callback(inter_xc)
                await discord_bot.cmd_set_twitter_cookies.callback(inter_twc)

                modal_xc = inter_xc.response.send_modal.call_args.args[0]
                modal_twc = inter_twc.response.send_modal.call_args.args[0]
                self.assertIsInstance(modal_xc, discord_bot.XCookieModal)
                self.assertIsInstance(modal_twc, discord_bot.XCookieModal)
                self.assertEqual(modal_xc.x_auth_token_input.default, "tok_saved")
                self.assertEqual(modal_twc.x_auth_token_input.default, "tok_saved")
                self.assertEqual(modal_xc.x_ct0_input.default, "ct0_saved")
                self.assertEqual(modal_twc.x_ct0_input.default, "ct0_saved")

        asyncio.run(_run())

    def test_16_alarm_inspection_and_webhook_self_healing(self) -> None:
        """[TC-16] /알림점검·/알림테스트·/지금점검 명령어, 웹후크 자동 복구, 기준점 확인 및 시험 알람 제어 검증"""
        from notifiers import discord_webhook

        cmd_names = {c.name for c in discord_bot.bot_client.tree.get_commands()}
        self.assertIn("알림점검", cmd_names)
        self.assertIn("알림테스트", cmd_names)
        self.assertIn("지금점검", cmd_names)

        # 1) check_discord_webhook_alive 검증
        self.assertFalse(discord_webhook.check_discord_webhook_alive("http://127.0.0.1/hook")[0])
        ok_resp = MagicMock(status_code=200)
        ok_resp.json.return_value = {"id": "999"}
        dead_resp = MagicMock(status_code=404)
        with patch("notifiers.discord_webhook.requests.get", return_value=ok_resp):
            self.assertEqual(
                discord_webhook.check_discord_webhook_alive(
                    "https://discord.com/api/webhooks/111/good"
                ),
                (True, "정상 연결됨"),
            )
        with patch("notifiers.discord_webhook.requests.get", return_value=dead_resp):
            alive, reason = discord_webhook.check_discord_webhook_alive(
                "https://discord.com/api/webhooks/111/dead"
            )
            self.assertFalse(alive)
            self.assertIn("404", reason)

        # 2) 순찰 하트비트 기록 확인
        discord_bot.record_monitor_heartbeat(3, 60)
        hb = discord_bot.get_monitor_heartbeat()
        self.assertEqual(hb["last_checked_count"], 3)
        self.assertEqual(hb["poll_interval_seconds"], 60)
        self.assertGreater(hb["last_cycle_ts"], 0.0)

        sample_st = {
            "id": "st_inspect_1",
            "enabled": True,
            "name": "탬탬버린",
            "chzzk_channel_id": "a7e175625fdea5a7d98428302b7aa57f",
            "cafe_club_id": "30688884",
            "cafe_member_key": "member_key_1",
            "x_screen_name": "2chamcham2",
            "x_user_id": "555",
            "discord_channel_id": "555666",
            "custom_webhook_url": "https://discord.com/api/webhooks/111/dead_hook",
            "notify_chzzk_open": True,
            "notify_cafe_article": True,
            "notify_x_post": True,
        }
        g_cfg = {
            "naver_nid_aut": "aut_1",
            "naver_nid_ses": "ses_1",
            "x_auth_token": "tok_1",
            "x_ct0": "ct0_1",
        }
        fake_state = {
            "streamers": {
                "guild:111:st_inspect_1": {
                    "chzzk": {"initialized": True, "status": "CLOSE"},
                    "naver_cafe": {
                        "articles_initialized": True,
                        "last_article_id": 100,
                        "comments_initialized": True,
                        "last_comment_id": 500,
                    },
                    "twitter_x": {
                        "initialized": True,
                        "last_tweet_id": 2000,
                        "user_id": "555",
                    },
                }
            }
        }

        async def _run() -> None:
            fake_ch = MagicMock(spec=discord_bot.discord.TextChannel)
            fake_guild = MagicMock()
            fake_guild.name = "테스트서버"
            fake_guild.get_channel.return_value = fake_ch

            def _make_inter(is_admin: bool = True):
                inter = MagicMock()
                inter.guild = fake_guild
                inter.guild_id = 111
                inter.user.guild_permissions.manage_guild = is_admin
                inter.response.defer = unittest.mock.AsyncMock()
                inter.response.send_message = unittest.mock.AsyncMock()
                inter.followup.send = unittest.mock.AsyncMock()
                return inter

            saved_holder: list[list[dict]] = []

            def _fake_save(gid, guild_name=None, guild_global=None, guild_streamers=None, channel_webhooks=None):
                if guild_streamers is not None:
                    saved_holder.append(guild_streamers)

            mock_cb = MagicMock()
            prev_cb = discord_bot._bot_check_now_cb
            discord_bot.set_bot_check_now_callback(mock_cb)
            try:
                # A) 웹후크 사망 감지 -> 자동 복구(Self-Healing) + 시험 알람 발송
                inter_a = _make_inter(True)
                with patch(
                    "bot.discord_bot.load_guild_settings",
                    return_value={"global": g_cfg, "streamers": [sample_st]},
                ), patch("bot.discord_bot.load_state", return_value=fake_state), patch(
                    "bot.discord_bot.check_discord_webhook_alive",
                    return_value=(False, "웹후크가 삭제되었거나 만료됨 (HTTP 404)"),
                ), patch(
                    "bot.discord_bot._ensure_channel_webhook",
                    new=unittest.mock.AsyncMock(
                        return_value="https://discord.com/api/webhooks/111/repaired_hook"
                    ),
                ) as mock_repair, patch(
                    "bot.discord_bot.save_guild_settings", side_effect=_fake_save
                ), patch(
                    "bot.discord_bot._fetch_chzzk_followings_sync",
                    return_value=(True, "성공", [{"channelId": "c1"}]),
                ), patch(
                    "bot.discord_bot.extract_x_user_info",
                    return_value={"user_id": "555", "screen_name": "2chamcham2"},
                ), patch(
                    "bot.discord_bot.fetch_chzzk_live_detail",
                    return_value={"status": "CLOSE", "liveTitle": "방제"},
                ), patch(
                    "bot.discord_bot.fetch_member_articles",
                    return_value=[{"articleId": 100}],
                ), patch(
                    "bot.discord_bot.fetch_member_comments",
                    return_value=[{"commentId": 500}],
                ), patch(
                    "bot.discord_bot.fetch_x_user_tweets",
                    return_value=[{"id": 2000}],
                ), patch(
                    "bot.discord_bot.send_preview_for_streamer",
                    return_value=(True, "✅ 탬탬버린 미리보기 카드 3건을 디스코드로 전송했습니다!"),
                ) as mock_preview:
                    await discord_bot.cmd_inspect_alarm.callback(
                        inter_a, 이름=None, 시험알람발송=True
                    )
                    mock_cb.assert_called_once()
                    mock_repair.assert_awaited_once_with(fake_ch, "111", "테스트서버")
                    self.assertEqual(
                        sample_st["custom_webhook_url"],
                        "https://discord.com/api/webhooks/111/repaired_hook",
                    )
                    self.assertEqual(len(saved_holder), 1)
                    mock_preview.assert_called_once()

                    sent_embed = inter_a.followup.send.call_args.kwargs["embed"]
                    self.assertIn("알림 시스템 종합 점검 리포트", sent_embed.title)
                    self.assertIn("백그라운드 순찰 엔진", sent_embed.description)
                    self.assertIn("네이버 쿠키", sent_embed.description)
                    self.assertIn("X(트위터) 쿠키", sent_embed.description)
                    field_val = sent_embed.fields[0].value
                    self.assertIn("자동 복구 완료", field_val)
                    self.assertIn("#100", field_val)
                    self.assertIn("#500", field_val)
                    self.assertIn("#2000", field_val)
                    self.assertIn("시험 알람", field_val)

                # B) /지금점검(시험알람발송=False) 시 시험 알람 미발송 검증
                inter_b = _make_inter(True)
                with patch(
                    "bot.discord_bot.load_guild_settings",
                    return_value={"global": g_cfg, "streamers": [sample_st]},
                ), patch("bot.discord_bot.load_state", return_value=fake_state), patch(
                    "bot.discord_bot.check_discord_webhook_alive",
                    return_value=(True, "정상 연결됨"),
                ), patch(
                    "bot.discord_bot._ensure_channel_webhook",
                    new=unittest.mock.AsyncMock(),
                ) as mock_repair_b, patch(
                    "bot.discord_bot._fetch_chzzk_followings_sync",
                    return_value=(True, "성공", []),
                ), patch(
                    "bot.discord_bot.extract_x_user_info",
                    return_value={"user_id": "555", "screen_name": "2chamcham2"},
                ), patch(
                    "bot.discord_bot.fetch_chzzk_live_detail",
                    return_value={"status": "OPEN", "liveTitle": "방송중"},
                ), patch(
                    "bot.discord_bot.fetch_member_articles", return_value=[]
                ), patch(
                    "bot.discord_bot.fetch_member_comments", return_value=[]
                ), patch(
                    "bot.discord_bot.fetch_x_user_tweets", return_value=[]
                ), patch(
                    "bot.discord_bot.send_preview_for_streamer"
                ) as mock_preview_b:
                    await discord_bot.cmd_check_now.callback(
                        inter_b, 이름="탬탬버린", 시험알람발송=False
                    )
                    mock_repair_b.assert_not_called()
                    mock_preview_b.assert_not_called()
                    sent_embed_b = inter_b.followup.send.call_args.kwargs["embed"]
                    self.assertIn("시험 알람 발송: OFF", sent_embed_b.footer.text)

                # C) 비관리자 실행 차단 검증
                non_admin = _make_inter(False)
                await discord_bot.cmd_test_alarm.callback(non_admin)
                non_admin.response.send_message.assert_awaited_once()
                self.assertIn(
                    "서버 관리하기",
                    non_admin.response.send_message.call_args.args[0],
                )
            finally:
                discord_bot._bot_check_now_cb = prev_cb

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
