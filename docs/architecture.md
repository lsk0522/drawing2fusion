# architecture.md — 시스템 구조

## 1. 전체 그림

```
┌──────────────────────────── 외부 프로세스 (Python 3.11+, d2f) ─────────────────────────────┐
│                                                                                            │
│  도면 파일 ──▶ [Extractor] ──▶ part-spec.json ──▶ [Spec Validator] ──▶ validation.json      │
│   DXF/PDF/IMG   ├ dxf_parser        (초안)             │ 사전 검증                            │
│                 ├ pdf_vector                           │ BLOCK이면 ▶ [Ask User] ─┐            │
│                 ├ vision (Claude)                      ▼                        │            │
│                 └ ocr_check                     [Pipeline / Orchestrator] ◀──────┘            │
│                                                        │  ▲                                  │
│                                     HTTP + token       │  │  build-log / measurements        │
└────────────────────────────────────────────────────────┼──┼──────────────────────────────────┘
                                                         ▼  │
┌──────────────────────────── Fusion 프로세스 (내장 Python, 애드인) ──────────────────────────┐
│                                                                                            │
│  [Bridge HTTP 서버] (백그라운드 스레드)                                                     │
│        │ fireCustomEvent(json)                                                             │
│        ▼                                                                                   │
│  [Dispatcher] (메인 스레드) ──▶ [Builder] ──▶ Fusion API (sketch, extrude, hole ...)        │
│                              ──▶ [Verifier] ──▶ boundingBox, MeasureManager, volume        │
│                              ──▶ [Snapshot] ──▶ Viewport.saveAsImage                       │
│                              ──▶ [UI Palette] ──▶ 알림 표시 / 질문 받기                     │
└────────────────────────────────────────────────────────────────────────────────────────────┘
                                                         │
                                                         ▼
                    [Comparator] ──▶ verify-report.json ──▶ [Report] report.html
                                                         └──▶ [Notifier] Fusion / Desktop / Discord / Telegram / Email
```

## 2. 컴포넌트

### 2.1 Extractor (외부)

| 입력 | 방법 | 출력 |
|---|---|---|
| DXF | `ezdxf`로 LINE/ARC/CIRCLE/LWPOLYLINE/DIMENSION 추출 → 뷰 클러스터링 → 피처 추론 | 스펙 초안 (confidence 높음) |
| 벡터 PDF | PDF 경로 추출(예: PyMuPDF) → DXF와 같은 엔티티 모델로 변환 | 스펙 초안 |
| 래스터 PDF/이미지 | Claude 비전에 도면 + 스키마를 주고 스펙 JSON 생성, OCR로 숫자 교차검증 | 스펙 초안 (치수별 confidence) |

모든 경로는 같은 `part-spec.json` 스키마로 수렴한다. 치수마다 **도면 어디서 읽었는지**(`source`)를 남겨서
나중에 알림에서 "정면도 우측 상단 Ø12"처럼 위치를 짚을 수 있게 한다.

### 2.2 Spec Validator (외부, 모델링 전)

순수 Python이라 Fusion 없이 테스트 가능. 예시 규칙:

- 필수 치수 누락 (구멍 깊이 없음, 관통 여부 없음)
- 체인 치수 합 ≠ 전체 치수
- 정면도 폭 ≠ 평면도 폭 (뷰 간 투영 불일치)
- 필렛 반경 ≥ 인접 변 길이의 절반 (빌드 실패 예상)
- 구멍이 바디 밖에 위치
- confidence < 임계값인 치수

규칙과 코드는 [errors-and-alerts.md](errors-and-alerts.md) 참고.

### 2.3 Bridge (애드인)

- `run()`에서 `app.registerCustomEvent('d2f.command')` 등록 후 백그라운드 스레드로 HTTP 서버 시작
- 요청 수신 → `request_id` 부여 → `app.fireCustomEvent(id, json)` → 결과 큐에서 대기 (타임아웃 있음)
- 메인 스레드의 핸들러가 Dispatcher를 호출하고 결과를 큐에 넣음
- 타임아웃된 요청은 메인 스레드에서 건너뜀 (stale 요청 처리)
- `127.0.0.1` 바인딩 + Bearer 토큰 (`~/.d2f/token`), `stop()`에서 서버·이벤트 정리

엔드포인트:

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/ping` | 버전, 프로토콜 버전, 활성 문서 |
| POST | `/build` | 스펙 전체 빌드 → build-log |
| POST | `/build/step` | 피처 하나씩 (디버깅·재시도용) |
| POST | `/measure` | 측정 요청 목록 → 결과 |
| POST | `/snapshot` | 뷰 목록 → 이미지 경로 |
| POST | `/ui/alert` | 팔레트·메시지박스에 알림 표시 |
| POST | `/ui/ask` | 질문 표시 후 사용자 답 반환 |
| POST | `/export` | f3d/step/stl |
| POST | `/eval` | 임의 Python 실행 — **기본 비활성**, 개발 모드에서만 |

### 2.4 Builder (애드인)

피처 타입마다 함수 하나. 입력은 mm/deg, 내부에서 cm/rad로 변환 (`units` 한 곳에서만).

```
build(spec):
    doc = new_document() or active
    register_user_parameters(spec.parameters)
    for feat in spec.features (순서대로):
        try:
            obj = BUILDERS[feat.type](feat, ctx)
            log(feat.id, status=health(obj), message=obj.errorOrWarningMessage)
            if feat.type == "sketch": log(fully_constrained=sketch.isFullyConstrained)
        except Exception as e:
            log(feat.id, status="failed", error=E3xx)
            if policy == "stop_on_error": break
    return build_log
```

- 스케치 치수는 User Parameter 식(`width`, `hole_d` 등)으로 걸어서 모델이 파라메트릭하게 남음
- 피처 이름 = 스펙 `id` → 타임라인에서 바로 찾을 수 있음
- 빌드 전 문서 자동 저장 또는 새 문서 사용 → 실패해도 원본 손상 없음

### 2.5 Verifier + Comparator

| 검증 대상 | 측정 방법 |
|---|---|
| 전체 크기 | `BRepBody.boundingBox` (축 정렬) / 필요 시 방향 지정 bbox |
| 구멍 지름·개수 | 원통면(`Cylinder` geometry) 순회 → 반지름·축 위치 |
| 구멍 위치 | 원통 축과 기준면 간 거리 |
| 특정 거리·두께 | `MeasureManager.measureMinimumDistance(면A, 면B)` |
| 부피·질량 | `physicalProperties` |
| 형상 일치 (보조) | 스냅샷 실루엣 vs 도면 뷰 윤곽 IoU, 또는 Claude 비전 비교 |

Comparator는 스펙의 `checks` 각각에 대해 `expected / actual / delta / tol / pass` 를 만든다.

### 2.6 Notifier

- 모든 단계는 `Alert` 객체를 이벤트 버스에 발행 → Notifier가 심각도별 라우팅
- 같은 원인의 반복 알림은 묶어서 1건으로 (중복 억제)
- 형식과 채널은 [errors-and-alerts.md](errors-and-alerts.md)

### 2.7 Ask User (Human-in-the-loop)

`BLOCK` 알림은 질문을 동반한다. 응답 경로:

1. Fusion 팔레트에서 버튼/입력
2. CLI 프롬프트
3. (선택) Telegram 버튼 응답

응답은 `part-spec.json`의 해당 값에 `"source": {"type": "user"}`로 기록되어 다음 실행에도 재사용된다.

## 3. 데이터 흐름과 상태

```
created → extracted → validated ─(BLOCK)→ waiting_user → validated
                         │
                         ▼
                      building ─(E3xx)→ build_failed ──▶ (Phase 5) fixing → building
                         │
                         ▼
                      verifying ─(E4xx)→ mismatch ──▶ 알림 + 리포트 (+ 자동 수정 옵션)
                         │
                         ▼
                        done
```

각 실행은 `out/<name>/state.json`에 상태를 저장 → 중단 후 `d2f resume`으로 이어서 실행.

## 4. 공식 Fusion MCP와의 관계

- Autodesk가 Fusion 안에 로컬 MCP 서버(Preferences → General → API 토글, 기본 포트 27182)를 제공한다.
  Claude Desktop 같은 로컬 MCP 클라이언트에서 실행 중인 Fusion을 조작할 수 있다.
- 이 프로젝트는 **결정적 빌드와 정량 검증**이 핵심이라 자체 애드인을 주 경로로 둔다.
- 공식 MCP는 Phase 0에서 기능을 확인하고, 스냅샷·탐색 등 겹치는 기능은 재사용을 검토한다.
- Phase 5에서 자체 MCP 서버(`mcp_server/`)를 만들어 Claude가 `extract_spec → build → verify` 도구를
  호출하게 한다. 이때 클라이언트는 로컬(Claude Desktop/Claude Code)이어야 한다.

## 5. 보안

- 브리지는 localhost만, 토큰 필수
- `/eval`(임의 코드)은 `D2F_DEV=1`일 때만 활성
- API 키·웹훅 URL은 `.env`에만, 로그에 마스킹
- 외부로 나가는 데이터: Claude API(도면 이미지), 알림 채널(요약 + 스냅샷). 사내 도면이면 알림에 이미지 첨부 끄기 옵션
