# Drawing2Fusion (d2f)

> 2D 도면(PDF / 이미지 / DXF)을 첨부하면 Autodesk Fusion에서 3D 파라메트릭 CAD 모델로 자동 생성하고,
> 생성 중 오류나 치수 불일치가 생기면 사용자에게 즉시 알려주는 도구.

현재 상태: **Phase 0·1 완료, Phase 2 대부분, Phase 3(DXF) 1차 완료, Phase 4는 채팅 판독으로 동작** —
**DXF 도면을 끌어다 놓으면** 해석 → (모르는 값은 질문) → Fusion 모델링 → 실측 검증 → 알림까지 동작.
투상도(3면도·1면도)와 **회전체 단면도**(해칭 반단면, 키홈, 기어 블랭크)를 읽고, 완성된 모델을 **도면 선과 다시 비교**(실루엣)해서 스펙 단계의 실수까지 잡습니다.
**사진·스크린샷·단면도 도면**은 Claude 채팅에 첨부하면 판독 → 스펙 → 같은 파이프라인으로 빌드·검증
([docs/image-reading.md](docs/image-reading.md)). 자동 이미지 판독(API 키 필요)은 아직 없음.

## 딸깍 실행 (Windows)

| 파일 | 하는 일 |
|---|---|
| `install.bat` | **처음 한 번.** 애드인을 Fusion에 연결하고 `.env`를 만든 다음 상태 점검 |
| `run.bat` | 메뉴 실행: 샘플 만들기 / 내 도면·스펙으로 만들기 / 샘플 도면(DXF) / 검사 / 결과 폴더 |
| `run.bat`에 **`.dxf`** 끌어다 놓기 | 도면을 읽어 바로 모델링 → 검증 → 결과 폴더 열기. 애매한 값은 창에서 물어봄 |
| `run.bat`에 `.json` 끌어다 놓기 | 그 스펙으로 바로 빌드하고 결과 폴더 열기 |
| `doctor.bat` | Python · 애드인 · Fusion 연결 상태 점검 |
| `uninstall.bat` | Fusion에서 애드인 연결 해제 (프로젝트 파일은 그대로) |

처음 한 번만: `install.bat` → Fusion에서 `Shift+S` → **추가 기능** → `Drawing2Fusion` → **실행**.
애드인은 "시작 시 실행"이 기본으로 켜져 있어서, 이후 Fusion을 켤 때마다 자동으로 시작됩니다.

Python은 `py` 런처 → PATH의 `python` → **Fusion에 들어 있는 Python** 순서로 자동으로 찾기 때문에 따로 설치하지 않아도 됩니다.

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

1. **도면 해석** — DXF는 벡터를 직접 파싱, PDF/이미지는 Claude 비전으로 뷰·치수·형상을 읽어 `part-spec.json` 생성
2. **스펙 검증** — 치수 누락, 체인 치수 합 불일치, 뷰 간 모순 등을 모델링 전에 잡아냄
3. **Fusion 모델링** — Fusion 애드인이 스펙을 받아 스케치 → 돌출/회전 → 구멍/필렛/챔퍼 순으로 결정적(deterministic)으로 생성
4. **치수 검증** — 완성된 바디를 실제로 측정(바운딩 박스, 구멍 지름, 거리, 부피)해서 도면 치수와 비교
5. **알림** — 단계마다 에러 코드와 심각도를 붙여 Fusion 화면 / 데스크톱 알림 / Discord·Telegram 등으로 전송

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
| [docs/verification-sections-regression.md](docs/verification-sections-regression.md) | **회전체 단면도 자동 읽기, 독립 실루엣 검사, 회귀·돌연변이 테스트 36/36** |

## 목표 사용 예 (구현 후)

```bash
# 도면 하나 변환
d2f run samples/drawings/bracket_01.pdf

# 이미 만든 스펙으로 다시 빌드만
d2f build out/bracket_01/part-spec.json

# 결과 검증만 다시
d2f verify out/bracket_01/
```

출력 폴더 예:

```
out/bracket_01/
├─ part-spec.json        # 도면에서 읽은 스펙 (사람이 고칠 수 있음)
├─ validation.json       # 모델링 전 스펙 검증 결과
├─ verify-report.json    # 모델링 후 치수 비교 결과
├─ report.html           # 도면 vs 모델 스냅샷 + 치수 비교표
├─ snapshots/            # front/top/right/iso 캡처
└─ bracket_01.f3d / .step
```

## 요구 사항 (요약)

- Windows 10/11 또는 macOS (Fusion은 Linux 미지원)
- Autodesk Fusion (API 사용 가능 버전)
- Python 3.11+ (외부 오케스트레이터용), Fusion 내장 Python (애드인용)
- Anthropic API 키 (PDF/이미지 도면 해석 시)

자세한 내용은 [build.md](build.md) 참고.

## 지원 범위 (1차)

- ✅ 단일 파트, 기계 가공 부품 위주 (블록, 브래킷, 플레이트, 플랜지, 축)
- ✅ 스케치 + 돌출/컷, 회전, 구멍(단순/카운터보어/카운터싱크/탭 표기), 필렛, 챔퍼, 직선·원형 패턴
- ⏳ 판금, 어셈블리, 곡면, GD&T 해석은 이후 단계
