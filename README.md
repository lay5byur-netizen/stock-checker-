# Stock Checker — GitHub Actions 자동화

해외 쇼핑몰 상품 재고 + 가격을 자동 체크해 구글 시트에 기록하고, 변동 발생 시 이메일로 알림.

## 지원 사이트 (8종)

| 사이트 | 추출 방식 | 통화 |
|---|---|---|
| Nike US/JP | `__NEXT_DATA__` + JSON-LD ProductGroup | USD / JPY |
| Uniqlo JP | 공개 commerce v5 API | JPY |
| Adidas US | JSON-LD Product + DOM `unavailable` 클래스 | USD |
| Adidas JP | JSON-LD ProductGroup + DOM `unavailable` 클래스 | JPY |
| Swim2000 | Shopify `/products/<handle>.js` | USD |
| top4running.de | JSON-LD ProductGroup + `?size=` 쿼리 | EUR |
| runningwarehouse.com | schema.org/Offer microdata | USD |
| Rakuten | DOM 그리드 + 통합 OOS 정규식 (`売り切れ`, `再入荷` 등) | JPY |

## 스케줄

| 등급 | 실행 시간 (KST) | UTC cron | 워크플로 |
|---|---|---|---|
| **A** | 매일 07:00, 18:00 | `0 22 * * *` / `0 9 * * *` | `schedule-a.yml` |
| **B** | 매일 07:00 (A 10분 후) | `10 22 * * *` | `schedule-b.yml` |
| **C** | 화/토 15:00 | `0 6 * * 1,5` | `schedule-c.yml` |

## 파일 구조

```
stock-checker/
├── stock_checker.py            # 단일 통합 스크립트 (모든 사이트 로직)
├── site_config.json            # 사이트별 설정 (선택자, 패턴, 임계값)
├── requirements.txt            # Python 패키지
├── .gitignore
├── README.md
└── .github/workflows/
    ├── schedule-a.yml          # A등급 (매일 07:00, 18:00 KST)
    ├── schedule-b.yml          # B등급 (매일 07:00 KST)
    └── schedule-c.yml          # C등급 (화/토 15:00 KST)
```

## GitHub Secrets

| Secret | 필수 | 설명 |
|---|---|---|
| `GOOGLE_CREDENTIALS` | ✅ | 서비스 계정 JSON 전체 (`{` 부터 `}` 까지) |
| `GOOGLE_SHEET_ID` | ✅ | `1rGBFWaN3DVq41DtcVgc0aM5LVDTXNzpg5LmYeJGzpRc` |
| `SMTP_HOST` | 선택 | 이메일 알림용 — 예: `smtp.gmail.com` |
| `SMTP_USER` | 선택 | 발신 계정 이메일 |
| `SMTP_PASS` | 선택 | 발신 앱 비밀번호 (Gmail 은 [앱 비밀번호](https://myaccount.google.com/apppasswords)) |
| `ALERT_TO` | 선택 | 수신자 이메일 (기본: `lay5byur@gmail.com`) |

## 시트 구조

| 탭 | 컬럼 |
|---|---|
| `상품 리스트` | A=번호 / B=상품명 / C=URL / D=카테고리 / E=등급 / F=스토어 상품명 / G=판매자 상품코드 |
| `전체 현황` | A=번호 / B=상품명 / C=색상 / D=재고 변동 / E=가격 변동 / **F=auto** / G=마지막 체크 |
| `변동 알림` | timestamp / 상품 / 색상 / TYPE / 변경 내용 / 시트 링크 |
| `사용량 모니터링` | 실행 로그 |

> ⚠️ F열(스토어 상품명)은 ARRAYFORMULA 로 자동 채워지므로 **절대 손대지 않음**.

## 분류 규칙

**재고 (D 컬럼) — 간소화 표기**
- `🔴 [size]` — 신규 품절 (이전 IN_STOCK → 오늘 OOS, 긴급)
- `🟠 [size] 계속 품절` — 이전 OOS → 오늘 OOS
- `🟢 [size]` — 재입고 (이전 OOS → 오늘 IN_STOCK)
- `⚫` — 첫등록 + 전 사이즈 OOS
- `🟤 [size]` — 첫등록 + 일부 OOS
- `⚠️ [size] ([qty])` — 재고 적음 (Uniqlo LOW_STOCK 또는 qty ≤ 3)
- `✅ 정상` — 변동 없음

**가격 (E 컬럼)**
- 첫 등록 또는 ±2% 미만 → `현재가` 만 기록
- −2% 이하 → `💰 이전가 → 현재가 (-N% ⬇️)` 배경 파랑 (#CFE2F3)
- +2% 이상 → `💸 이전가 → 현재가 (+N% ⬆️)` 배경 빨강 (#F4CCCC)

## 로컬 테스트

```bash
pip install -r requirements.txt
python -m playwright install chromium

export GOOGLE_CREDENTIALS="$(cat /path/to/service_account.json)"
export GOOGLE_SHEET_ID="1rGBFWaN3DVq41DtcVgc0aM5LVDTXNzpg5LmYeJGzpRc"

# 시트 안 건드리고 결과만 출력 (dry-run)
python stock_checker.py --grades A --slot manual --dry-run

# 실제 실행
python stock_checker.py --grades A,B --slot manual
```

## 업로드 (GitHub)

```bash
cd C:\Users\User\Downloads\stock-checker-v2
git init
git add .
git commit -m "Initial: stock checker"
git branch -M main
git remote add origin https://github.com/<your-username>/stock-checker.git
git push -u origin main
```

또는 GitHub 웹 인터페이스에서 폴더 통째로 드래그 업로드.

## 수동 실행 (테스트)

저장소 → `Actions` 탭 → 워크플로 선택 → `Run workflow`.
약 5~10분 후 시트 갱신 확인.

## 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 워크플로 60일 후 멈춤 | GitHub 정책 — 가벼운 commit 한 번 해주면 재활성화 |
| `permission denied` (시트) | 서비스 계정 이메일을 시트의 편집자로 추가 |
| `JSONDecodeError` (GOOGLE_CREDENTIALS) | Secret 에 JSON 전체 (`{...}`) 그대로 붙여넣기 |
| Playwright 실패 | 로그에서 `playwright install chromium --with-deps` 단계 확인 |
| 가격 통화 이상 | `site_config.json` 의 currency 설정 확인 |
| 새 OOS 마커 못 잡음 (Rakuten) | `stock_checker.py` 의 `OOS_JP_PATTERNS` 정규식에 추가 |
