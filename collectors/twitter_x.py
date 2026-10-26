"""
[추후 확장용] X (구 트위터) 새 포스트 및 답글 감지 모듈입니다.
1차 구현에서는 치지직과 네이버 카페를 먼저 완성하고,
이후 X 부계정 쿠키(twikit) 또는 RSSHub 연동 시 이 파일에 로직을 채워 넣습니다.
"""

from typing import Any


def check_twitter_x(state: dict[str, Any]) -> bool:
    """
    추후 X(트위터) 연동 시 활성화될 함수입니다.
    현재는 아무 동작도 하지 않고 False를 반환합니다.
    """
    _ = state
    return False
