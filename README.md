# 🔔 치지직 & 네이버 카페 다중 사용자 디스코드 알림 시스템 (Chzzk & Naver Cafe Discord Notifier)

치지직(CHZZK) 스트리머의 생방송 상태 및 네이버 카페 새 글/댓글을 실시간으로 감시하여 디스코드(Discord) 채널로 풍부한 임베드 알림 카드를 전송하는 올인원 다중 사용자 알림 봇 시스템입니다.

---

## ✨ 주요 기능

### 1. 📺 치지직(CHZZK) 알림
- **생방송 시작 & 종료 알림**: 방송 ON/OFF 상태 실시간 감지
- **방송 정보 변경 알림**: 방제(제목) 또는 카테고리(게임) 변경 시 자동 감지 및 알림
- **치지직 커뮤니티 새 글 알림**: 스트리머 채널 라운지에 작성된 새 글 실시간 전달
- **고해상도 프로필 아이콘**: 디스코드 임베드에 최적화된 스트리머 프로필 썸네일 지원
- **치지직 채널 검색 기능**: 채널 ID 없이 스트리머 이름만으로 1초 만에 자동 완성
- **내 팔로우 채널 일괄 연동**: 치지직에서 팔로우 중인 채널 및 알림 켜둔 채널 원클릭 일괄 추가

### 2. ☕ 네이버 카페(Naver Cafe) 알림
- **스트리머 작성 새 게시글 알림**: 제목, 본문 미리보기, 사진 썸네일 포함
- **스트리머 작성 새 댓글 알림**: 게시글 제목, 작성 댓글 내용, 네이버 스티커 이미지 포함
- **원클릭 카페 분석**: 스트리머가 작성한 카페 글 주소 1개만 입력하면 카페 ID와 회원 식별키 자동 추출

### 3. 🛡️ 보안 & 다중 사용자(Multi-User) 아키텍처
- **독립된 사용자 공간**: 각 사용자마다 개별 계정(ID/PW)으로 로그인하여 본인만의 웹후크 및 방송인 목록 관리
- **스마트 중복 조회 방지 (15초 TTL 캐시)**: 동일한 스트리머를 여러 사용자가 등록하더라도 API 호출은 1회만 수행하여 서버 부하 원천 차단
- **초대 코드 가입 시스템**: 승인된 사용자만 가입할 수 있는 초대 코드 검증
- **관리자 전용 대시보드**: 등록 회원 관리, 강제 탈퇴, 초대 코드 변경, 신규 가입 차단 기능
- **로그인 무차별 대입(Brute-force) 방지**: 연속 실패 시 자동 계정/IP 잠금
- **디스코드 웹후크 SSRF 방지 검증**: 악성 URL 등록 차단
- **영구 자동 로그인 유지**: 안전한 장기 세션 지원

---

## 🚀 빠른 시작 가이드 (Getting Started)

### 1. 환경 설정 및 의존성 설치
```bash
# 가상환경 생성 및 활성화
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 필수 패키지 설치
pip install -r requirements.txt
```

### 2. 환경 변수 설정
`.env.example` 파일을 복사하여 `.env` 파일을 생성하고 기본 설정을 구성합니다:
```bash
cp .env.example .env
```

### 3. 실행하기
```bash
# 웹 UI 대시보드와 함께 실행 (기본 포트: 5000)
python main.py

# 백그라운드 콘솔 모드 (웹 UI 없이 순찰만 실행)
python main.py --no-ui
```

실행 후 웹 브라우저에서 `http://127.0.0.1:5000`에 접속하여 관리자 계정을 생성하고 알림을 설정하세요.

---

## 🛠️ 기술 스택

- **Backend**: Python 3.10+, Flask, Werkzeug
- **HTTP Client**: Requests (with session & cookie caching)
- **Frontend**: Vanilla HTML5, Modern CSS3, JavaScript (Fetch API)
- **Reverse Proxy & SSL**: Nginx, Let's Encrypt (Certbot)
- **Platform**: Cross-platform (Windows / Linux Ubuntu 데몬 지원)

---

## 📄 라이선스 (License)

This project is licensed under the MIT License.
