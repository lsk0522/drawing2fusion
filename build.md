# build.md — 개발 환경 구성 및 빌드·실행 가이드

> Phase 0~1 완료, Phase 3(DXF) 1차 완료 (2026-10-24). 실제로 쓰는 명령은 README 참고.
> 아래 2장 구조 중 `report/`(HTML), `mcp_server/`는 아직 없음. 외부 패키지는 표준 라이브러리만 쓰고,
> DXF를 처음 읽을 때만 `ezdxf`를 자동 설치(`pip install --user ezdxf`).

## 0. Fusion 실측으로 확인한 사항 (Fusion 2704.1.53, Windows)

| 항목 | 내용 | 코드 반영 |
|---|---|---|
| 파라미터 이름 | `A C E F H J K L N R S T V W c f g l m r s` 같은 한 글자는 단위 기호라 User Parameter 이름으로 거부됨 (`param name is not valid`) | `expr.FUSION_RESERVED` → Fusion에는 `L_`, `W_`처럼 자동 개명, 스펙 이름은 그대로 |
| 사각형 구속 | `addTwoPointRectangle`이 수평/수직 구속을 **추가하지 않음** | 빌더가 직접 수평/수직 구속 추가 → 완전 구속 확인 |
| 같은 좌표 점 | 원점 기준 치수를 두 점에 같은 x(또는 y) 값으로 걸면 두 번째가 `VCS_SKETCH_OVER_CONSTRAINTS`로 실패 | 같은 좌표를 가진 기존 점과 수직/수평 정렬 구속으로 대체 |
| 다각형 마지막 변 | 닫힌 다각형의 마지막 치수는 중복이라 거부됨 | 거부된 치수는 건너뜀 (로그만) |
| CustomEvent | 백그라운드 스레드 → 메인 스레드 전달 정상. `fireCustomEvent`는 성공해도 `False`를 반환하므로 반환값을 믿지 말 것 | 반환값 무시 |
| 스크립트 vs 애드인 | 일반 스크립트로 등록한 이벤트 핸들러는 스크립트가 끝나면 사라짐 → 브리지는 반드시 **애드인**으로 실행 | manifest `type: addin` |
| 기본 단위 | 새 문서의 거리 단위를 mm로 바꾸면 식 안의 숫자(`L_-10`)가 mm로 해석됨 | `prepare_design()` |
| 단위 이름 (여러 글자) | `pcd` 같은 이름도 거부됨 — 목록으로 다 막을 수 없음 | 거부되면 그 자리에서 `pcd_`로 바꿔 다시 등록 |
| XZ / YZ 평면 | XZ 스케치 (x, y) → 월드 (x, 0, **−y**), YZ 스케치 (x, y) → 월드 (0, y, **−x**) | 스펙은 월드 좌표로 쓰고 `modelToSketchSpace`로 변환, YZ는 가로/세로 치수 방향을 바꿈 |
| 여러 원 관통 컷 | 스케치 평면이 몸체 안쪽 면(예: 허브 바닥)에 걸치면 원 여러 개를 한 번에 "관통" 컷할 때 경고 상태로 아무것도 안 잘림 | 경고가 나면 지우고 몸체 끝까지의 거리 컷으로 다시 만듦 (`build-log.json`에 기록) |
| 패턴 | 직선/원형 패턴이 카운터싱크 챔퍼까지 함께 복제됨 | 구멍의 `_cb`, `_cs` 피처를 패턴 입력에 같이 넣음 |
| 코드 갱신 | 실행 중인 애드인 모듈을 `importlib.reload`로 제자리 갱신해도 브리지·이벤트가 그대로 동작 | `/reload` + 코드 해시 비교로 자동 갱신 |

검증 결과: 스펙 6종 + DXF 도면 7장 모두 실제 Fusion에서 확인 ([docs/verification-phase1-3.md](docs/verification-phase1-3.md)).

---

## 1. 사전 준비

| 항목 | 버전/조건 | 비고 |
|---|---|---|
| OS | Windows 10/11 또는 macOS | Fusion은 Linux 미지원. Linux 머신은 오케스트레이터만 실행 가능 (원격 모드) |
| Autodesk Fusion | 최신 업데이트 | API 사용 가능 라이선스인지 확인 |
| Python | 3.11+ | 오케스트레이터·추출기·알림용 (Fusion 내장 Python과 별개) |
| uv (권장) 또는 pip | 최신 | 가상환경·의존성 관리 |
| Git | — | |
| VS Code | — | 애드인 디버깅 (Fusion의 Scripts and Add-Ins에서 "Debug") |
| Anthropic API 키 | — | PDF/이미지 도면 해석 (Phase 4) |

---

## 2. 저장소 구조 (목표)

```
drawing2fusion/
├─ README.md / plan.md / build.md
├─ docs/
│  ├─ architecture.md
│  ├─ spec-schema.md
│  └─ errors-and-alerts.md
├─ pyproject.toml
├─ .env.example
├─ d2f/                         # 외부 Python 패키지 (오케스트레이터)
│  ├─ cli.py                    # d2f run / build / verify
│  ├─ pipeline.py               # 단계 실행 + 상태 관리
│  ├─ extract/
│  │  ├─ dxf_parser.py          # ezdxf
│  │  ├─ pdf_vector.py          # 벡터 PDF → 엔티티
│  │  ├─ vision.py              # Claude 비전 → 스펙 초안
│  │  └─ ocr_check.py           # OCR 교차검증
│  ├─ spec/
│  │  ├─ part-spec.schema.json
│  │  ├─ validate.py            # 사전 검증 규칙
│  │  └─ units.py               # mm ↔ cm, deg ↔ rad (유일한 변환 지점)
│  ├─ bridge_client.py          # 애드인 HTTP 클라이언트
│  ├─ report/                   # verify-report.json, report.html
│  └─ notify/
│     ├─ base.py                # Alert 모델, 중복 억제
│     ├─ desktop.py
│     ├─ discord.py
│     ├─ telegram.py
│     └─ email.py
├─ addin/
│  └─ Drawing2Fusion/           # Fusion 애드인 (Fusion 내장 Python으로 실행)
│     ├─ Drawing2Fusion.py      # run()/stop() 진입점
│     ├─ Drawing2Fusion.manifest
│     ├─ bridge/                # HTTP 서버 + CustomEvent 디스패처
│     ├─ builder/               # sketch, extrude, hole, fillet, chamfer, pattern
│     ├─ verifier/              # measure, compare, snapshot
│     ├─ ui/                    # 팔레트 HTML (알림·질문 표시)
│     └─ lib/                   # 순수 Python 벤더링 의존성 (가능하면 비움)
├─ mcp_server/                  # Phase 5: MCP 노출
├─ samples/
│  ├─ specs/  drawings/  broken/
└─ tests/
   ├─ unit/                     # Fusion 없이 실행
   └─ integration/              # Fusion 실행 필요
```

**규칙:** 애드인은 Fusion 내장 Python에서 돌아가므로 `pip install`에 기대지 않는다.
무거운 의존성(ezdxf, OCR, Anthropic SDK)은 전부 외부 `d2f/` 쪽에 둔다.

---

## 3. 설치

### 3.1 외부 패키지

```bash
git clone <repo-url> drawing2fusion
cd drawing2fusion

uv venv
# Windows
.venv\Scripts\activate
# macOS
source .venv/bin/activate

uv pip install -e ".[dev]"
cp .env.example .env
```

`.env` 항목:

```ini
# 브리지
D2F_BRIDGE_HOST=127.0.0.1
D2F_BRIDGE_PORT=8765
D2F_BRIDGE_TOKEN=          # 비워두면 애드인 최초 실행 시 생성되는 토큰 파일을 읽음

# 도면 해석
ANTHROPIC_API_KEY=

# 알림 (쓰는 것만)
D2F_NOTIFY_CHANNELS=fusion,desktop,discord
DISCORD_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
SMTP_HOST=
SMTP_USER=
SMTP_PASS=
D2F_NOTIFY_TO=

# 검증
D2F_DEFAULT_TOL_MM=0.05
```

### 3.2 Fusion 애드인 설치

개발 중에는 복사 대신 **심볼릭 링크**로 연결해서 수정이 바로 반영되게 한다.

**Windows (관리자 PowerShell 또는 개발자 모드):**

```powershell
New-Item -ItemType SymbolicLink `
  -Path "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\Drawing2Fusion" `
  -Target "$PWD\addin\Drawing2Fusion"
```

**macOS:**

```bash
ln -s "$PWD/addin/Drawing2Fusion" \
  "$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/Drawing2Fusion"
```

> 경로는 Fusion 버전에 따라 다를 수 있다. Fusion에서 Scripts and Add-Ins 창(Shift+S)의
> 폴더 아이콘으로 실제 AddIns 경로를 확인할 것.

그다음 Fusion에서:

1. `Shift+S` → **Add-Ins** 탭 → `Drawing2Fusion` 선택 → **Run**
2. 필요하면 **Run on Startup** 체크
3. Text Commands 창에 `[d2f] bridge listening on 127.0.0.1:8765` 확인

---

## 4. 실행

### 4.1 브리지 연결 확인

```bash
d2f ping
# → {"ok": true, "fusion_version": "...", "active_document": "Untitled"}
```

### 4.2 파이프라인

```bash
# 전체: 해석 → 검증 → 빌드 → 측정 → 리포트 → 알림
d2f run samples/drawings/dxf/bracket_01.dxf

# 단계별
d2f extract samples/drawings/pdf/flange_02.pdf -o out/flange_02/
d2f validate out/flange_02/part-spec.json
d2f build    out/flange_02/part-spec.json
d2f verify   out/flange_02/

# 옵션
--new-document      # 새 Fusion 문서에서 빌드 (기본)
--keep-open         # 빌드 후 문서 열어두기
--no-ask            # BLOCK 발생 시 질문 대신 중단
--max-retries 2     # Phase 5 자동 수정 재시도 횟수
--export step,f3d
```

### 4.3 알림 테스트

```bash
d2f notify-test --channel discord
d2f run samples/broken/fillet_too_big.json   # E302 알림이 와야 함
```

---

## 5. 테스트

```bash
# 단위 테스트 — Fusion 없이 (브리지는 mock)
pytest tests/unit -q

# 통합 테스트 — Fusion 실행 + 애드인 Run 상태여야 함
pytest tests/integration -q -m fusion

# 샘플 전체 회귀 + 성공률 리포트
d2f bench samples/ --report out/bench.html
```

필수 단위 테스트:

- `units.py`: mm→cm, deg→rad 왕복 변환 (10배 오차 방지)
- `validate.py`: 각 사전 검증 규칙이 `samples/broken/`을 정확한 코드로 잡는지
- `compare.py`: 허용오차 경계값 (정확히 tol, tol+ε)
- `notify/base.py`: 중복 억제, 심각도별 라우팅

---

## 6. 디버깅 팁

- **로그:** 애드인은 `adsk.core.Application.log()`와 `~/.d2f/logs/addin.log` 양쪽에 기록
- **VS Code 디버깅:** Scripts and Add-Ins 창에서 애드인 선택 → Debug → VS Code에서 브레이크포인트
- **애드인 수정 반영:** Stop → Run (심볼릭 링크라 복사 불필요). 모듈 캐시 때문에 안 바뀌면 Fusion 재시작
- **Fusion이 멈춤:** 대부분 메인 스레드에서 오래 걸리는 작업 또는 백그라운드 스레드에서 API 직접 호출. 모든 API 호출은 `CustomEvent` 핸들러 안에서만
- **치수가 10배 차이:** 단위 변환 누락. Fusion API 길이는 cm
- **포트 충돌:** `D2F_BRIDGE_PORT` 변경 (공식 Fusion MCP는 27182를 쓰므로 피할 것)

---

## 7. 배포 (v1.0 이후)

- 외부 패키지: `uv build` → wheel, 또는 `pipx install drawing2fusion`
- 애드인: `addin/Drawing2Fusion/`을 zip으로 묶어 배포, 설치 스크립트(`install.ps1`, `install.sh`) 제공
- 버전 일치: 애드인과 외부 패키지가 같은 `protocol_version`을 쓰는지 `d2f ping`에서 확인
