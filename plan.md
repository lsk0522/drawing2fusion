# plan.md — Drawing2Fusion 프로젝트 계획

## 1. 목표

**한 문장 목표:** 2D 기계 도면을 넣으면 Fusion에 치수가 맞는 파라메트릭 3D 모델이 만들어지고,
맞지 않거나 실패한 부분은 "어디가, 왜, 얼마나" 틀렸는지 사용자에게 알려준다.

### 성공 기준 (v1.0)

| 지표 | 목표 |
|---|---|
| 테스트 도면 세트(20장) 빌드 성공률 | ≥ 90% |
| 주요 치수 일치율 (허용오차 ±0.05 mm 또는 도면 공차) | ≥ 95% |
| 불일치 발생 시 알림 누락 | 0건 (모든 불일치는 반드시 리포트·알림) |
| 도면 1장 처리 시간 (단순 부품) | ≤ 3분 |
| 같은 스펙 재빌드 결과 | 100% 동일 (결정성) |

### 하지 않는 것 (v1.0 범위 밖)

- 어셈블리, 판금, 자유곡면, 용접 기호
- GD&T(기하공차) 해석 — 표기는 읽어서 리포트에만 남김
- 손스케치 수준의 비정형 그림
- 도면이 정보 부족일 때 "추측해서 그럴듯하게" 만들기 → **추측하지 않고 질문한다**

---

## 2. 핵심 설계 결정

1. **중간 표현(part-spec.json)을 둔다**
   도면 → 스펙 → 모델로 나눠서, 해석 오류와 모델링 오류를 분리한다.
   사람이 스펙을 직접 고쳐서 재빌드할 수 있다.

2. **모델링은 결정적 코드로**
   LLM이 Fusion API 코드를 매번 새로 짜게 하지 않는다. 스펙의 feature 타입별로
   검증된 빌더 함수가 있고, LLM은 스펙만 만든다. (자유 코드 실행은 디버깅용 탈출구로만)

3. **자체 애드인 + 브리지**
   Fusion API는 메인 스레드에서만 호출 가능하므로, 애드인이 백그라운드 HTTP 서버를 띄우고
   요청을 `CustomEvent`로 메인 스레드에 넘기는 구조를 쓴다. (커뮤니티 Fusion MCP들과 같은 패턴)
   Autodesk 공식 로컬 Fusion MCP 서버는 Phase 0에서 탐색·스크린샷 용도로 써보고,
   필요하면 보조 경로로 붙인다.

4. **검증은 3단계**
   - 사전 검증: 스펙 자체의 모순 (모델링 전)
   - 빌드 검증: 피처별 성공/경고/실패, 스케치 완전 구속 여부
   - 사후 검증: 실제 바디를 측정해서 도면 치수와 비교

5. **모르면 멈추고 묻는다**
   신뢰도 낮은 치수, 누락 치수, 뷰 모순은 `BLOCK` 심각도로 처리하고 사용자 답변을 기다린다.

자세한 구조는 [docs/architecture.md](docs/architecture.md).

---

## 3. 로드맵

> 기간은 1인 개발 기준 추정. 각 Phase는 "완료 기준"을 통과해야 다음으로 넘어간다.

### Phase 0 — 환경·기술 검증 (1주)

- [x] Fusion 애드인 (Text Commands 로그, `~/.d2f/logs/addin.log`) — 툴바 버튼은 팔레트 작업 때
- [x] 백그라운드 HTTP 서버 + `CustomEvent` 메인 스레드 디스패치 (실제 Fusion에서 왕복 확인)
- [x] 단위 함정 확인: Fusion 내부 길이 단위는 **cm**, 각도는 **radian** (`expr.py` 한 곳에서만 변환)
- [x] 박스 만들고 `boundingBox`, 부피 읽기
- [x] `Viewport.saveAsImageFile`로 정면/평면/측면/등각 스냅샷 저장
- [x] 공식 Fusion MCP 동작 확인 (개발 중 테스트 실행용으로 사용)
- [x] f3d / STEP / STL 내보내기 확인 (현재 라이선스에서 제한 없음, [docs/verification-step1.md](docs/verification-step1.md))
- [x] Windows `install.bat` / `run.bat` 경로로 빌드·검증·알림 확인
- [x] Fusion UI에서 애드인 "실행" 버튼으로 켠 상태 확인 (doctor 전 항목 ✅)

**완료 기준:** 외부 Python에서 `POST /build`로 JSON을 보내면 Fusion에 박스가 생기고, 측정값이 JSON으로 돌아온다.

### Phase 1 — Spec → Builder (2주)

- [x] 스펙 검증기 (`d2f/spec/validate.py`, E1xx/E2xx) — JSON Schema 파일은 아직
- [x] 빌더: `sketch`(rect/circle/line/polyline/arc), `extrude`(new/join/cut/intersect), `revolve`, `hole`(단순/카운터보어), `fillet`, `chamfer`
- [x] `slot`, 90° 카운터싱크, `pattern_rect` / `pattern_circ`, XZ/YZ 평면 스케치(좌표 변환 실측), revolve
- [x] 코드 자동 리로드: CLI가 애드인 코드 해시를 비교해 다르면 Fusion 재시작 없이 다시 불러옴
- [ ] 남은 것: 원점이 아닌 축 기준 원형 패턴, 90° 외 카운터싱크, 단면 구멍(측면 구멍), JSON Schema 파일
- [x] 스케치 치수 구속 자동 부여 + `isFullyConstrained` 체크 (샘플 전부 완전 구속)
- [x] 스펙 치수를 Fusion **User Parameters**로 등록 (단위 기호 이름은 자동 개명)
- [x] 피처마다 `healthState` / 에러 메시지 수집 → `build-log.json`
- [x] 라이브 빌드 모드 (`--live MS`)
- [x] 손으로 작성한 스펙 6종 (블록, 플레이트, L브래킷, 플랜지, 단차축, 복합 블록) — 전부 실제 Fusion에서 통과

**완료 기준:** 수작업 스펙 5종이 모두 에러 없이 빌드되고, 재실행 시 동일 결과.

### Phase 2 — Verifier + 알림 (1~2주)

- [x] 측정: 바운딩 박스, 구멍 지름·위치(X/Y/Z 축), 부피 — 면 간 거리(`MeasureManager`)는 아직
- [x] `checks` 비교 → `verify-report.json` (기대값 / 실측값 / 오차 / 판정)
- [x] 에러 코드 체계 ([docs/errors-and-alerts.md](docs/errors-and-alerts.md)), 터미널 질문 → 답하면 이어서 빌드
- [x] 알림: 터미널, Fusion 텍스트 창, Discord·Telegram(코드만, 실제 전송 미확인) — 팔레트·데스크톱 알림은 아직
- [ ] `report.html`: 스냅샷 + 치수 비교표 + 에러 목록
- [x] 일부러 틀린 스펙·도면 5종(구멍 위치, 필렛, 값 누락, 치수 불일치, 치수 덮어쓰기)

**완료 기준:** 의도적으로 넣은 오류 10종이 전부 올바른 코드로 감지·알림된다.

### Phase 3 — DXF 입력 (1~2주)

- [x] `ezdxf`로 선·호·원·폴리라인·블록·치수(DIMENSION)·문자 추출, 처음 실행 때 ezdxf 자동 설치
- [x] 뷰 분리(겹침·포함 묶기) + 테두리·표제란 제외 + 제3각/제1각 판별 + 1면도(두께 주기 "t=5")
- [x] 피처 추론: 정면도 높이 구간별로 평면도 외곽(사각형·모서리R·원·다각형) / 원 / 사각 블록 돌출, 구멍(관통·막힘·카운터보어)
- [x] 치수 문자 ↔ 그려진 길이 교차검증 → 덮어쓰기는 E107 질문, 카운터보어 깊이는 "DP5.4" 주기에서 읽음
- [x] 치수 없는 값은 식으로 연결 (`H = h1 + h2`, `h2 = H - h1`, 원형이면 `W = L`)
- [x] `d2f run 도면.dxf` / run.bat 끌어다 놓기 / 메뉴 8번
- [x] 공개 도면 3장(QCAD 예제)으로 시험: 1장 근사 빌드, 2장은 틀린 모델 대신 질문으로 멈춤 ([docs/verification-public-drawings.md](docs/verification-public-drawings.md))
- [x] 회전체 단면도: 해칭 반단면 → revolve, 해칭 안 된 이 포함, 키홈, 연쇄 치수, 경사각 식 ([docs/verification-sections-regression.md](docs/verification-sections-regression.md))
- [x] 독립 실루엣 검사(E406): 도면 선 영역 vs 모델 투영. 이전에 '통과'로 잘못 보고되던 근사 모델을 잡아냄
- [x] 회귀 테스트 `tools/regress.py`: 30개 케이스 + 돌연변이 6개, Fusion에서 36/36
- [ ] 남은 것: 정면·측면도의 원(옆 구멍), 경사·곡선 외곽, 회전체가 아닌 단면도, 나사 표기(M6 → 탭)

**완료 기준:** DXF 테스트 세트 10장 중 8장 이상 자동 빌드 + 치수 검증 통과.
**현재:** 생성 도면 12장(정상 9 / 오류 3) — 정상은 전부 통과(실루엣 0.99+), 오류는 질문으로 멈추고 답하면 통과. 공개 도면 3장: 베벨기어 자동 회전체 8/8, 플랜지는 질문, 포크는 근사라서 실루엣 검사가 실패로 보고.

### Phase 4 — PDF / 이미지 입력 (2~3주)

- [x] 사전 실험: 휴대폰 캡처 도면을 Claude가 직접 판독 → 스펙 → 빌드 13/13 통과 ([docs/verification-image-test.md](docs/verification-image-test.md))
- [ ] PDF 벡터 추출 시도 (벡터 PDF면 DXF 경로와 동일하게 처리)
- [x] **채팅 판독 절차 (API 키 없이)**: 도면 첨부 → Claude 판독·검산·질문 → 스펙 → run.bat → 비교 이미지 ([docs/image-reading.md](docs/image-reading.md), `tools/compare_image.py`)
- [x] 단면도 회전체 시험: 베벨기어 블랭크 7/7, 부피 실측 = 계산값. 판독 실수(키홈 방향)를 잡는 `plane` 검사(E408) 추가
- [ ] 래스터 도면 자동 판독(API 키 사용 시): 채팅 판독과 같은 스펙을 코드로 생성
- [ ] 치수마다 `confidence`와 도면 내 위치(`bbox`) 기록
- [ ] OCR(예: PaddleOCR) 결과와 LLM 결과 교차검증, 불일치 시 `BLOCK` + 질문
- [ ] 사용자 확인 UI: 도면 위에 읽은 치수를 겹쳐 보여주고 수정 가능하게

**완료 기준:** 이미지 도면 10장 중 7장 이상, 사용자 질문 2회 이하로 빌드 성공.

### Phase 5 — 에이전트 루프 + MCP (2주)

- [ ] 검증 실패 시 원인 분류 → 스펙 수정안 제시 → 재빌드 (최대 N회, 사용자 승인 옵션)
- [ ] 자체 MCP 서버로 노출: `extract_spec`, `build`, `verify`, `snapshot`, `ask_user`
- [ ] Claude Desktop / Claude Code에서 "이 도면 모델링해줘" 한 마디로 전체 파이프라인 실행

**완료 기준:** 테스트 세트 20장 기준 성공 기준(1장) 달성.

### Phase 6 — 확장 (이후)

- 회전체 고도화, 판금, 간단한 어셈블리
- STEP/STL 내보내기 자동화, 3D 프린팅 슬라이서 연동
- 모델 → 도면 역생성 후 원본 도면과 비교 (가장 강력한 검증)

---

## 4. 테스트 데이터 계획

| 세트 | 수량 | 내용 |
|---|---|---|
| `samples/specs/` | 10 (현재 6) | 손으로 작성한 정답 스펙 (Phase 1~2) |
| `samples/drawings/dxf/` | 10 (현재 12) | 정답(`*.expected.json`)이 있는 DXF — `tools/make_sample_dxf.py`로 생성 |
| `samples/drawings/pdf/` | 10 | 벡터 PDF + 스캔 이미지 |
| `samples/broken/` | 10+ | 일부러 틀린 도면/스펙 (알림 테스트용) |

각 샘플은 `expected.json`(정답 치수)을 함께 둔다. 직접 그린 도면이나 라이선스가 명확한 자료만 사용.

---

## 5. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| 비전 모델이 치수를 잘못 읽음 | 틀린 모델 | OCR 교차검증, confidence 낮으면 질문, 사후 측정으로 2중 확인 |
| 2D → 3D 해석이 여러 개 가능 | 엉뚱한 형상 | 뷰 간 일관성 검사, 후보가 2개 이상이면 스냅샷 보여주고 선택받기 |
| Fusion 내부 단위(cm/rad) 실수 | 치수 10배 오차 | 단위 변환을 한 모듈에서만 처리, 단위 테스트 필수 |
| 메인 스레드 블로킹 / Fusion 멈춤 | 작업 손실 | 요청 타임아웃, 피처 단위 분할 실행, 빌드 전 자동 저장 |
| Fusion API 버전 변화 | 빌더 깨짐 | 지원 버전 명시, 통합 테스트를 업데이트마다 실행 |
| 로컬 브리지에 임의 코드 실행 노출 | 보안 | localhost 바인딩 + 토큰 인증, 자유 코드 실행은 기본 비활성 |
| 라이선스 제약 (개인용 등) | 기능 제한 | Phase 0에서 확인 후 범위 조정 |

---

## 6. 결정 필요 사항 (Open Questions)

- [ ] 알림 기본 채널: Discord / Telegram / 이메일 중 무엇을 기본으로?
- [ ] 허용오차 기본값: ±0.05 mm 고정 vs 도면 일반공차(KS B ISO 2768-m 등) 적용
- [ ] 사용자 확인 UI 위치: Fusion 팔레트 안 vs 별도 웹 페이지
- [ ] 공식 Fusion MCP를 주 경로로 쓸지, 자체 브리지만 쓸지 (Phase 0 결과 보고 결정)
- [ ] 출력 포맷: `.f3d`만 / `.step`까지 / Fusion 클라우드 프로젝트 저장
