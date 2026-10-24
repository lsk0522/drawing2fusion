# Drawing2Fusion (d2f)

> 2D 도면(PDF / 이미지 / DXF)을 첨부하면 Autodesk Fusion에서 3D 파라메트릭 CAD 모델로 자동 생성하고,
> 생성 중 오류나 치수 불일치가 생기면 사용자에게 즉시 알려주는 도구.

현재 상태 (v0.3): **도면 파일을 넣으면 Fusion 모델까지 자동, 그리고 완성된 모델을 도면과 다시 대조합니다.**

| 입력 | 처리 |
|---|---|
| **DXF** | 3면도·1면도(두께 주기), 높이 층별 돌출, 위·옆 구멍, 카운터보어, **탭(M6 → 나사 피처)**, 공차(±, H7, h11, P9 …) |
| **벡터 PDF** (CAD에서 내보낸 PDF) | 선 굵기·점선 패턴으로 외형/숨은선/중심선 구분, **화살표 쌍 → 치수**, 축척(1:1, 1:2 …) 자동, 단면 해칭 영역 복원 → DXF와 같은 해석 |
| **회전체 단면도** (DXF·PDF) | 해칭 반단면 → 회전 프로파일, 키홈, 해칭 안 된 기어 이, 연쇄 치수·모따기·경사각 |
| 사진·스캔·손그림 | Claude 채팅에 올리면 판독 → 같은 파이프라인 ([docs/image-reading.md](docs/image-reading.md)) |

검증은 스펙에서 나온 값만 보지 않습니다. **도면 선에서 직접 만든 실루엣(E406)과 단면 해칭 영역(E410)**, 면 위치(E408),
나사(E409)를 모델과 대조해서 "스펙 자체가 틀린" 경우까지 잡습니다 — 실수를 일부러 심은 돌연변이 테스트 9개를 모두 잡습니다.

## 딸깍 실행 (Windows)

| 파일 | 하는 일 |
|---|---|
| `install.bat` | **처음 한 번.** 애드인을 Fusion에 연결하고 `.env`를 만든 다음 상태 점검 |
| Fusion 안 **ADD-INS → Drawing2Fusion** 버튼 | 오른쪽 패널: **도면 열기** → 진행 상황 · 알림 · 검증 결과 · 질문에 답하기 · 결과 폴더 |
| `run.bat` | 메뉴 실행: 샘플 만들기 / 내 도면·스펙으로 만들기 / 샘플 도면(DXF·PDF) / 검사 / 결과 폴더 |
| `run.bat`에 **`.dxf` / `.pdf`** 끌어다 놓기 | 도면을 읽어 바로 모델링 → 검증 → 결과 폴더 열기. 애매한 값은 창에서 물어봄 |
| `run.bat`에 `.json` 끌어다 놓기 | 그 스펙으로 바로 빌드하고 결과 폴더 열기 |
| `mcp_install.bat` | Claude Desktop에 MCP 서버 등록 → 채팅에서 "이 도면 모델링해줘"로 전체 실행·수정 ([docs/ui-and-mcp.md](docs/ui-and-mcp.md)) |
| `doctor.bat` | Python · 애드인 · Fusion 연결 상태 점검 |
| `uninstall.bat` | Fusion에서 애드인 연결 해제 (프로젝트 파일은 그대로) |

처음 한 번만: `install.bat` → Fusion에서 `Shift+S` → **추가 기능** → `Drawing2Fusion` → **실행**.
애드인은 "시작 시 실행"이 기본으로 켜져 있어서, 이후 Fusion을 켤 때마다 자동으로 시작됩니다.
(추가 기능 목록에 Drawing2Fusion이 두 개 보이면 하나만 켜 두세요 — 두 번째 것은 이제 스스로 시작하지 않습니다.)

Python은 `py` 런처 → PATH의 `python` → **Fusion에 들어 있는 Python** 순서로 자동으로 찾기 때문에 따로 설치하지 않아도 됩니다.
DXF용 ezdxf, PDF용 PyMuPDF는 처음 쓸 때 자동 설치됩니다.

## 명령어로 실행

```powershell
cd $HOME\Desktop\drawing2fusion
powershell -ExecutionPolicy Bypass -File .\install.ps1    # 애드인을 Fusion AddIns 폴더에 연결
```

1. Fusion → `Shift+S` → **추가 기능(Add-Ins)** 탭 → `Drawing2Fusion` → **실행(Run)**
2. 터미널에서 (Python 3.9+, 추가 패키지 필요 없음):

```powershell
python -m d2f ping                                            # 연결 확인
python -m d2f run samples\drawings\dxf\flange.dxf             # 도면 → 모델 → 검증 (한 번에)
python -m d2f run samples\drawings\pdf\flange_1to2.pdf         # 벡터 PDF (1:2 축척 도면)
python -m d2f extract samples\drawings\dxf\bracket_l.dxf       # 도면 해석만 (out\<이름>\part-spec.json)
python -m d2f build samples\specs\plate_holes.json --live 500  # 0.5초 간격으로 그리며 빌드
python -m d2f build samples\specs\bracket_l.json --export f3d,step
python -m d2f validate samples\broken\needs_answer.json        # 질문(BLOCK)이 어떻게 나오는지
python -m unittest discover -s tests -t .                      # Fusion 없이 단위 테스트
python tools\compare_image.py 도면.png out\<이름>              # 원본 도면 | 스냅샷 | 검증표 비교 이미지
python tools\regress.py                                       # 전체 회귀 테스트 (Fusion 필요, 약 2분) → out\regression.md
```

결과는 `out\<부품이름>\` 에 저장: `part-spec.json`, `extract-report.json`(도면일 때), `build-log.json`, `measurements.json`, `verify-report.json`, `snapshots\*.png`, `<이름>.f3d`.

애드인 코드를 고쳐도 Fusion을 다시 켤 필요 없음 — 빌드할 때 코드가 바뀐 걸 감지해서 자동으로 다시 불러옵니다.

종료 코드: `0` 통과 · `1` 오류/치수 불일치 · `2` 사용자 확인 필요(BLOCK) · `3` Fusion 연결 안 됨

---

## 무엇을 하나

```
 도면 첨부 ──▶ ① 도면 해석 ──▶ ② 스펙(JSON) 검증 ──▶ ③ Fusion 모델링 ──▶ ④ 치수 검증 ──▶ 완료 리포트
                    │                 │                     │                   │
                    └─────────────────┴──── 문제 발생 시 ─────┴───────────────────┴──▶ 🔔 사용자 알림
```

1. **도면 해석** — DXF·벡터 PDF는 코드가 직접 읽음, 사진·스캔은 Claude 채팅 판독 → `part-spec.json`
2. **스펙 검증** — 치수 누락, 치수 문자 ≠ 그려진 길이, 뷰 간 모순 등을 모델링 전에 잡아 질문
3. **Fusion 모델링** — 애드인이 스펙대로 스케치 → 돌출/회전 → 구멍/나사/필렛/챔퍼/패턴을 결정적으로 생성
4. **치수 검증** — 완성 바디를 실측(크기, 구멍 지름·위치, 부피, 면 위치, 나사)하고, **도면 선과 실루엣·단면을 직접 대조**
5. **알림** — 에러 코드와 심각도를 붙여 Fusion 패널 / 콘솔 / Discord·Telegram으로 전송

핵심 원칙: **AI는 "읽기"와 "애매한 부분 질문"만 하고, 모델링 자체는 검증 가능한 코드가 한다.**
그래야 같은 스펙이면 항상 같은 모델이 나오고, 틀린 곳을 정확히 짚어 알려줄 수 있다.

## 문서 안내

| 문서 | 내용 |
|---|---|
| [plan.md](plan.md) | 목표, 범위, 단계별 로드맵, 완료 기준, 리스크 |
| [build.md](build.md) | 개발 환경 구성, 애드인 설치, 실행·테스트·디버깅 방법 |
| [docs/architecture.md](docs/architecture.md) | 전체 구조, 컴포넌트, 데이터 흐름, Fusion 연동 방식 |
| [docs/spec-schema.md](docs/spec-schema.md) | 도면 → 모델 사이의 중간 표현 `part-spec.json` 스키마 |
| [docs/errors-and-alerts.md](docs/errors-and-alerts.md) | 에러 코드 체계, 검증 규칙, 알림 채널과 메시지 형식 |
| [docs/feature-research.md](docs/feature-research.md) | 유사 서비스 조사, 꼭 필요한/배울/활용할 기능, UI·UX 정리 |
| [docs/verification-step1.md](docs/verification-step1.md) | 1단계 검증 절차와 실제 실행 결과 |
| [docs/verification-phase1-3.md](docs/verification-phase1-3.md) | 빌더 확장·DXF 해석 검증 결과 |
| [docs/dxf-reading.md](docs/dxf-reading.md) | DXF 도면을 어떻게 읽는지, 도면 작성 팁, 한계 |
| [docs/verification-image-test.md](docs/verification-image-test.md) | 휴대폰 캡처 도면(클램프 브래킷) → 모델 테스트, 13/13 통과 |
| [docs/image-reading.md](docs/image-reading.md) | **사진·PDF·단면도 도면을 채팅으로 판독해 모델링하는 절차** (API 키 불필요) |
| [docs/verification-public-drawings.md](docs/verification-public-drawings.md) | 공개 DXF 3장 테스트, 베벨기어 단면도 채팅 판독 7/7 |
| [docs/verification-sections-regression.md](docs/verification-sections-regression.md) | 회전체 단면도 자동 읽기, 독립 실루엣 검사, 회귀·돌연변이 테스트 36/36 |
| [docs/pdf-reading.md](docs/pdf-reading.md) | **벡터 PDF 도면 읽기** — 선 종류, 치수 복원, 축척, 해칭, 한계 |
| [docs/ui-and-mcp.md](docs/ui-and-mcp.md) | **Fusion 패널 사용법, Claude Desktop/Code MCP 연결** |
| [docs/verification-v0.3.md](docs/verification-v0.3.md) | **v0.3 검증: PDF·옆 구멍·탭·공차·단면 비교, 회귀 45케이스** |

## 결과 폴더

```
out/<부품>/
├─ part-spec.json        # 도면에서 읽은 스펙 (사람이 고쳐서 다시 빌드 가능)
├─ extract-report.json   # 뷰·치수·실루엣·단면 영역 (검증의 '도면 쪽' 기준)
├─ validation.json       # 모델링 전 스펙 검증 결과
├─ build-log.json / measurements.json
├─ verify-report.json    # 모델 실측 vs 도면 비교 결과
├─ snapshots/            # front/top/right/iso 캡처
├─ compare.png           # (tools/compare_image.py) 도면 | 스냅샷 | 검증표
└─ <부품>.f3d / .step / .stl
```

## 요구 사항 (요약)

- Windows 10/11 또는 macOS (Fusion은 Linux 미지원)
- Autodesk Fusion (API 사용 가능 버전)
- Python 3.9+ (없으면 Fusion에 들어 있는 Python 사용), Fusion 내장 Python (애드인용)
- API 키 필요 없음 (사진 도면은 Claude 채팅에서 판독)

자세한 내용은 [build.md](build.md) 참고.

## 지원 범위 (1차)

- ✅ 단일 파트, 기계 가공 부품 위주 (블록, 브래킷, 플레이트, 플랜지, 축)
- ✅ 스케치 + 돌출/컷, 회전, 구멍(단순/카운터보어/카운터싱크/옆 구멍/탭 나사), 필렛, 챔퍼, 직선·원형 패턴
- ⏳ 곡선 외곽 투상도(지금은 근사 후 실루엣 검사가 실패로 알림), 회전체가 아닌 단면도, 판금, 어셈블리, GD&T 해석
