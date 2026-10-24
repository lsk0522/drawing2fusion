# 1단계 검증 절차와 결과

> 검증일: 2026-10-24 · Fusion 2704.1.53 · Windows · Python 3.12.10 (`py` 런처로 자동 탐지)

## 절차

| # | 확인할 것 | 방법 | 통과 기준 |
|---|---|---|---|
| 1 | 설치 | `install.bat` 더블클릭 | AddIns 폴더에 연결(junction) 생성, `.env` 생성, 상태 점검에서 Python·애드인 ✅ |
| 2 | 애드인 실행 | Fusion → `Shift+S` → 추가 기능 → `Drawing2Fusion` → 실행 | `doctor.bat`에서 "Fusion 연결됨 ✅" |
| 3 | 정상 샘플 빌드 | `samples\specs\*.json`을 `run.bat`에 끌어다 놓기 | 종료 코드 0, "치수 검증 N/N 통과", 결과 폴더 생성 |
| 4 | 결과 파일 | `out\<이름>\` 확인 | `part-spec / validation / build-log / measurements / verify-report .json`, `snapshots\*.png`, `.f3d` |
| 5 | 내보내기 | `python -m d2f build ... --export f3d,step,stl` | 세 파일 모두 생성, STL 크기가 도면 치수와 같음 |
| 6 | 빌드 전 차단 | `samples\broken\` 3종 | `hole_outside`·`fillet_too_big` → E205, 종료 코드 1 / `needs_answer` → E105·E202 질문, 종료 코드 2. **Fusion에 아무것도 만들지 않음** |
| 7 | 빌드 후 불일치 감지 | `samples\broken\dim_mismatch.json` | E401(두께), E403(구멍 개수), E402(구멍 지름+위치) 모두 감지, 종료 코드 1 |
| 8 | 파라메트릭 | Fusion에서 `L_`, `hole_d` 값 변경 | 모델과 구멍 위치가 따라 바뀌고 타임라인 오류 없음 |
| 9 | 단위 테스트 | `python -m unittest discover -s tests -t .` | 전부 OK |

## 결과

| # | 결과 | 메모 |
|---|---|---|
| 1 | ✅ | `install.bat` 실행 → junction 생성, `.env` 생성 |
| 2 | ✅ | 사용자가 Fusion UI에서 실행(08:43). `doctor.bat` 전 항목 ✅ "Fusion 연결됨", 이어서 `run.bat`으로 bracket_l 빌드 성공(8.0s, 종료 코드 0). 3~8은 먼저 같은 애드인 코드를 Fusion 안에서 띄워 검증함 |
| 3 | ✅ | block_basic 4/4 (5.8s), plate_holes 10/10 (7.6s), bracket_l 8/8 (8.1s) — 모두 `run.bat` 경유 |
| 4 | ✅ | 파일 전부 생성 |
| 5 | ✅ | f3d 79KB, STEP 21KB, STL 36KB. STL 80.0×40.0×6.0 mm 일치. STEP은 단위를 cm로 선언(표준 형식이라 다른 CAD에서 자동 변환) |
| 6 | ✅ | 종료 코드 1 / 2 / 1, 빌드 시작 전에 중단 |
| 7 | ✅ | "bbox Z: 기대 6, 실측 7, 오차 +1 mm", "Ø (2개): 기대 6.6, 실측 6, 오차 -0.6 mm · 위치 (10, 20), (70, 20)" |
| 8 | ✅ | L 80→100, hole_d 6.6→8 → bbox 100×40×6, 구멍 Ø8 @ x=10/90, 타임라인 전부 정상 |
| 9 | ✅ | Windows Python 3.12에서 25개 통과 |

## 검증 중 고친 것

- 단위 기호 이름 바꾸기(L→L_) 안내가 "추론값 사용" 경고로 여러 번 뜨던 문제 → 빌드당 한 번, 정보(INFO)로만 표시
- 빌드 중단 알림의 임시 코드 `E2xx` 제거
- `D2F_NO_OPEN=1` 환경 변수: 자동 테스트할 때 결과 폴더 창이 열리지 않게
- 일부러 틀린 샘플 `samples/broken/dim_mismatch.json` 추가 (빌드 후 불일치 감지용)

## 알게 된 점

- 컴퓨터 제어로는 Fusion의 "스크립트 및 애드인" 창을 조작할 수 없음 ("식별되지 않은 앱"으로 잡힘). 이 창이 열려 있는 동안 Fusion API 호출도 멈춤.
- `runOnStartup: true`라서 Fusion을 하나 더 켜면 그쪽이 먼저 애드인을 켜서 포트 8765를 잡음 (로그 08:31 기록). Fusion은 하나만 켜서 쓸 것.
- 추가 기능 목록에 `Drawing2Fusion`이 두 줄로 보이지만, 디스크에 있는 manifest는 하나(AddIns 폴더의 junction → 바탕화면 폴더)뿐. 아래 줄을 켜서 정상 동작 확인.
