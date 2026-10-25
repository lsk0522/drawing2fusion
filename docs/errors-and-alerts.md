# errors-and-alerts.md — 에러 코드, 검증 규칙, 알림

## 1. 심각도

| 심각도 | 의미 | 파이프라인 동작 | 기본 채널 |
|---|---|---|---|
| `INFO` | 진행 상황, 완료 | 계속 | Fusion 팔레트 |
| `WARN` | 결과는 나왔지만 확인 권장 (추론값 사용, 미반영 주기) | 계속 | 팔레트 + 리포트 |
| `ERROR` | 피처 실패 또는 치수 불일치 | 정책에 따라 계속/중단 | 팔레트 + 데스크톱 + 메신저 |
| `BLOCK` | 정보 부족·모순으로 진행 불가, 사용자 답 필요 | **일시정지**, 질문 | 팔레트(질문) + 데스크톱 + 메신저 |

## 2. 에러 코드

### E1xx — 입력·해석

| 코드 | 내용 | 심각도 |
|---|---|---|
| E101 | 파일 형식 미지원 / 손상 | ERROR |
| E102 | 뷰를 찾지 못함 (정면·평면·측면 분리 실패) | BLOCK |
| E103 | 치수 텍스트 판독 불가 / PDF에 선이 없음(스캔 PDF → 채팅 판독 안내) | BLOCK |
| E104 | LLM 판독값 ≠ OCR 판독값 | BLOCK |
| E105 | 치수 confidence 임계값 미만 | BLOCK (설정으로 WARN 가능) |
| E106 | 도면 단위/축척 불명 (PDF에서 치수로 축척을 못 구하면 1:1 가정 — WARN) | BLOCK / WARN |
| E107 | 도면 치수 문자 ≠ 그려진 형상 (치수 덮어쓰기) | BLOCK — "그려진 값 / 치수 문자 값 / 직접 입력" 중 선택. 차이가 max(0.05 mm, 0.3%) 이내면 작도 오차로 보고 치수 문자 값 사용 (INFO) |

### E2xx — 스펙 사전 검증 (모델링 전)

| 코드 | 내용 | 심각도 |
|---|---|---|
| E201 | 스키마 위반 (필수 필드 없음, 타입 오류) | ERROR |
| E202 | 필수 치수 누락 (예: 구멍 깊이, 관통 여부) | BLOCK |
| E203 | 체인 치수 합 ≠ 전체 치수 | BLOCK |
| E204 | 뷰 간 투영 불일치 (정면도 폭 ≠ 평면도 폭) | BLOCK |
| E205 | 형상 불가능 예상 (필렛 > 변 길이/2, 구멍이 바디 밖) | ERROR |
| E206 | 미정의 파라미터 참조 | ERROR |
| E207 | 추론값 사용 | WARN |
| E208 | 해석 후보가 2개 이상 (2D→3D 모호) | BLOCK |

### E3xx — Fusion 빌드

| 코드 | 내용 | 심각도 |
|---|---|---|
| E301 | 스케치 생성 실패 / 프로파일 없음 (열린 루프) | ERROR |
| E302 | 피처 실패 (`healthState` = error) | ERROR |
| E303 | 피처 경고 (`healthState` = warning) | WARN |
| E304 | 스케치 미완전 구속 | WARN |
| E305 | 엣지 셀렉터가 0개 또는 예상과 다른 개수 매칭 | ERROR |
| E306 | 바디가 여러 개로 분리됨 (의도하지 않은) | ERROR |

### E4xx — 사후 치수 검증

| 코드 | 내용 | 심각도 |
|---|---|---|
| E401 | 바운딩 박스 불일치 | ERROR |
| E402 | 구멍 지름 불일치 | ERROR |
| E403 | 구멍 개수 불일치 | ERROR |
| E404 | 위치/거리 불일치 | ERROR |
| E405 | 부피 편차 과다 (기대값 있을 때) | WARN |
| E406 | 실루엣 불일치: 모델을 뷰 방향으로 투영한 모양 vs **도면 선으로 둘러싸인 영역** IoU < 0.98 (0.95 미만이면 ERROR). 스펙과 무관한 독립 검사 | WARN / ERROR |
| E407 | 단위 의심 (실측/기대 ≈ 10 또는 0.1) | ERROR — 메시지에 "cm/mm 변환 의심" 명시 |
| E408 | 평면 위치 불일치 (`plane` 검사: 축에 수직인 면이 그 좌표에 없음 — 키홈·포켓이 반대쪽에 만들어진 경우 등) | ERROR |
| E409 | 나사 불일치 (`thread` 검사: 그 규격(M6x1 등)의 나사가 걸린 구멍 수가 다름) | ERROR |
| E410 | 단면 불일치: 모델을 단면 평면(회전체는 XZ·Y=0, 각진 부품은 절단선 위치)으로 자른 단면 vs **도면의 해칭(재료) 영역** IoU < 0.97 — 바깥 모양이 같아도 안쪽(웹·홈·키홈·포켓)이 틀린 경우 | WARN / ERROR |
| E411 | 도면 원 ↔ 모델 원통 불일치: 뷰에 그려진 원(평면도 = Z축, 정면도 = Y축, 측면도 = X축)에 같은 축·중심·지름의 원통이 없음, 또는 모델 구멍이 그 뷰에 원(실선·점선)으로 없음 — 스펙이 구멍을 **검사 항목과 함께** 빠뜨려도 잡힘 | ERROR |

### E5xx — 시스템

| 코드 | 내용 | 심각도 |
|---|---|---|
| E501 | 브리지 연결 불가 (Fusion 미실행 / 애드인 미실행) | ERROR |
| E502 | 요청 타임아웃 (메인 스레드 응답 없음) | ERROR |
| E503 | 프로토콜 버전 불일치 | ERROR |
| E504 | Claude API 오류 / 한도 초과 | ERROR |
| E505 | 알림 채널 전송 실패 (다른 채널로 폴백) | WARN |
| E506 | 문서 닫기 거부 (`--close-after`: 저장된 파일은 닫지 않음) | WARN |

## 3. Alert 데이터 모델

```json
{
  "run_id": "2026-10-23T15-30-12_bracket_01",
  "code": "E402",
  "severity": "ERROR",
  "stage": "verify",
  "title": "구멍 지름 불일치",
  "message": "h_mount: 기대 Ø6.6 (+0.1/0), 실측 Ø6.0, 오차 -0.6 mm",
  "where": { "feature": "h_mount", "check": "c_hole_d", "drawing": { "view": "top", "label": "2-Ø6.6", "bbox": [212, 330, 260, 350] } },
  "suggestion": "스펙 hole_d=6.6 인데 빌더가 6.0으로 생성됨 → 빌드 로그 확인. 또는 도면 판독값 재확인.",
  "question": null,
  "attachments": ["snapshots/top.png", "crops/c_hole_d.png"],
  "time": "2026-10-23T15:31:02+09:00"
}
```

`BLOCK`이면 `question`이 채워진다:

```json
"question": {
  "text": "정면도 Ø10 구멍의 깊이가 표기되어 있지 않습니다. 어떻게 할까요?",
  "options": ["관통", "깊이 직접 입력", "이 구멍 제외"],
  "input": { "type": "number", "unit": "mm", "for_option": "깊이 직접 입력" }
}
```

## 4. 알림 채널

| 채널 | 구현 | 용도 |
|---|---|---|
| Fusion 팔레트 | 애드인 HTML 팔레트 + `/ui/alert`, `/ui/ask` | 작업 중 즉시 확인, BLOCK 질문 응답 |
| Fusion 메시지박스 | `ui.messageBox` | ERROR 이상, 팔레트 꺼져 있을 때 폴백 |
| 데스크톱 알림 | Windows 토스트 / macOS 알림센터 | 다른 창 보고 있을 때 |
| Discord | 웹훅 (embed + 스냅샷 첨부) | 자리 비웠을 때, 기록 남기기 |
| Telegram | Bot API (인라인 버튼으로 BLOCK 응답 가능) | 모바일에서 바로 답하기 |
| 이메일 | SMTP | 완료 리포트 요약 |

설정 확인: `notify_test.bat` (또는 `python -m d2f notify-test [--channel discord|telegram]`) — `.env`의 웹훅/봇 토큰으로
테스트 메시지를 한 번 보내고 ✅ 보냄 / ⚪ 설정 안 됨 / 🔴 실패(이유)를 보여 줍니다. 토큰은 화면에 출력하지 않습니다.

라우팅 기본값 (`.env`의 `D2F_NOTIFY_CHANNELS`로 변경):

```
INFO  → fusion
WARN  → fusion, report
ERROR → fusion, desktop, discord|telegram
BLOCK → fusion(ask), desktop, discord|telegram
DONE  → fusion, discord|telegram (+ email 요약)
```

## 5. 메시지 형식 (메신저 예시)

```
🔴 [E402] 구멍 지름 불일치 — bracket_01
단계: 치수 검증
위치: h_mount (평면도 "2-Ø6.6")
기대 Ø6.6 (+0.1/0) / 실측 Ø6.0 / 오차 −0.6 mm
제안: 빌드 로그에서 hole_d 적용값 확인
[스냅샷] [리포트 열기]
```

```
🟡 [BLOCK E202] 정보가 필요합니다 — flange_02
정면도 Ø10 구멍 깊이 미표기
→ 1) 관통  2) 깊이 입력  3) 제외
(Fusion 팔레트 또는 여기서 답장)
```

```
✅ 완료 — bracket_01  (2분 14초)
치수 검증 18/18 통과 · 경고 1건 (추론값 fillet_r=3)
```

## 6. 동작 규칙

- **중복 억제:** 같은 `run_id + code + where.feature`는 60초 안에 1번만 전송, 이후 묶음 요약
- **폴백:** 채널 전송 실패 시 E505 기록 후 다음 채널로
- **조용한 실패 금지:** 검증 결과가 나오지 않은 check가 있으면 그 자체를 ERROR로 보고
- **리포트는 항상 생성:** 중간에 실패해도 그 시점까지의 `report.html`을 남김
- **알림에 포함하지 않는 것:** API 키, 전체 도면 원본(옵션으로 끌 수 있음)

## 7. 검증 판정 로직

```
delta = actual - expected
if tol is [lower, upper]:   pass = lower <= delta <= upper
elif tol is number:         pass = abs(delta) <= tol
if not pass and abs(actual/expected) in (≈10, ≈0.1):  code = E407
```
