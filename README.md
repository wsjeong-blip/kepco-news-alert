# KEPCO 뉴스·공개 유출정보 모니터링 설치 안내

## 교체할 파일

1. GitHub 저장소 최상위 폴더의 `kepco_alert.py`를 새 파일로 교체합니다.
2. `.github/workflows/` 폴더의 기존 `KEPCO news alert` 워크플로 YAML 파일 **내용**을 `kepco_alert.yml`로 교체합니다. 기존 파일명이 다른 경우에도 파일명은 그대로 두어도 됩니다. 다만 동일 워크플로 파일이 두 개 생기지 않도록 주의하세요.
3. **기존 `seen.json`은 삭제하거나 초기화하지 마세요.** 이 파일은 중복 전송 방지 기록입니다.
4. GitHub의 **Actions > KEPCO news alert > Run workflow**에서 수동 실행 후 Summary를 확인합니다.

## 기존 설정 유지

- GitHub Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- 선택 Secrets: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` (없으면 네이버 건너뜀)
- 5분 크론 설정, 뉴스 수집 및 텔레그램 전송, `seen.json` 중복방지 유지

## 새로 추가된 기능

| 기능 | 설정 필요 여부 | 실제 범위 |
|---|---|---|
| RansomLook 공개 피해 주장 RSS | 필요 없음 | 관련 기관명·도메인이 포함된 게시 주장 메타데이터 |
| ThreatCluster 공개 피해 주장 RSS | 필요 없음 | 공개 유출 피해 주장 목록의 키워드 일치 탐지 |
| 공개 유출/해킹포럼 관련 보도 | 필요 없음 | Google 뉴스 공개 보도 검색, 포럼 자체 직접 수집 아님 |
| HIBP 유출 계정 확인 | 선택 | **인증된 관리 도메인만** 조회. 유출 계정 수·변화 요약만 보고 |

### HIBP 사용 시에만 추가

- HIBP 서비스에서 해당 도메인에 대한 권한 인증 및 지원하는 API 구독이 필요합니다.
- Repository **Settings > Secrets and variables > Actions > Secrets**에서 `HIBP_API_KEY` 설정
- 동일 화면의 **Variables**에 `HIBP_DOMAIN` 설정 (예: 권한을 인증한 관리 도메인)
- API를 연결하지 않아도 나머지 공개 RSS/보도 탐지는 동작합니다.
- HIBP 도메인 조회는 서비스 이용지침과 호출 제한을 고려해 하루 1회만 조회합니다.
- 계정 이름·이메일 주소·비밀번호 및 유출 원본을 GitHub 저장소나 텔레그램에 저장·발송하지 않습니다.

## 중복 전송 정책

- `seen.json`이 정상 GitHub에 커밋·푸시된 경우에는 이전 전송 항목을 다음 실행에서 건너뜁니다.
- 항목의 GUID, 제목, URL 등을 조합해 복수의 식별 키로 관리합니다.
- 새 공개 유출정보 소스는 최초 성공 실행에서 과거 수집분을 **기록만 하고 전송하지 않습니다**.
- 텔레그램 전송에 실패한 항목은 전송 기록으로 남기지 않고 다음 실행에서 재시도합니다.
- GitHub 저장소에 `seen.json` 저장이 실패하거나 기록을 삭제하면 중복이 재발할 수 있습니다. 텔레그램 전송과 GitHub 커밋을 완전히 원자적으로 처리할 수는 없습니다.

## 한계와 안전

- 이 코드는 Tor 또는 폐쇄형 다크웹 해킹포럼에 직접 접속하거나 유출된 데이터베이스를 다운로드하지 않습니다.
- 공개 피드에 없는 거래 게시물, 비공개 포럼, 이미 삭제된 게시물은 탐지할 수 없습니다.
- RSS/위협정보 출처의 '유출 피해 주장'은 공식적으로 확인된 개인정보 유출과 다릅니다.
- GitHub Actions `*/5` 설정은 5분 간격 실행을 **요청**하는 것이며 GitHub 사정상 지연 또는 누락될 수 있습니다. 새 소스의 공개 RSS 갱신 주기는 별개입니다.
- 모니터링 키워드는 `kepco_alert.py` 상단의 `RANSOMWARE_WATCH`, `PUBLIC_INTEL_QUERIES`, `COMPANY_KEYWORDS`를 수정합니다.
- 실행 환경의 외부 피드 실제 응답은 별도로 검증해야 합니다. GitHub Actions Summary에서 소스별 성공/실패를 확인하세요.
