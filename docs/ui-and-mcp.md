# ui-and-mcp.md — Fusion 패널과 Claude 연결(MCP)

## 1. Fusion 패널

Fusion **ADD-INS(추가 기능)** 패널에 **Drawing2Fusion** 버튼이 생깁니다 (애드인 v0.3). 누르면 오른쪽에 패널이 열립니다.

| 영역 | 내용 |
|---|---|
| **도면 열기…** | `.dxf` / `.pdf` / part-spec `.json` 선택 → 바로 실행 |
| 상태 줄 | 실행 중 / ✅ 완료 · 치수 검증 n/m / 🔴 문제 있음 / 🟠 확인 필요 |
| 알림 목록 | 경고·오류·질문만 (코드, 제목, 내용) |
| 실행 기록 | 콘솔 출력 전체 (접어 둠) |
| 확인이 필요한 값 | 도면만으로 정할 수 없는 값이 있으면 질문이 폼으로 나옴 → **답하고 계속 만들기** → 스펙에 답을 적고 다시 빌드 |
| 다시 실행 / 결과 폴더 | 같은 도면 다시 / `out\<부품>` 열기 |

동작: 패널은 모델링을 직접 하지 않고 `python -m d2f run …`을 백그라운드로 실행합니다 — run.bat과 똑같은 파이프라인이라
결과도 같습니다. 출력은 패널로 실시간 전달되고, 파이프라인이 보내는 알림(/alert)도 패널에 표시됩니다.

## 2. Claude와 연결 (MCP)

Claude가 직접 도면을 읽히고, 빌드하고, 검증 실패를 보고 스펙을 고쳐 다시 빌드할 수 있게 하는 연결입니다.
API 키는 필요 없습니다 (Claude Desktop/Code 구독 그대로).

### Claude Desktop
1. `mcp_install.bat` 더블클릭 — `%APPDATA%\Claude\claude_desktop_config.json`에 `drawing2fusion` 서버를 추가합니다 (기존 설정은 `.bak`으로 백업).
2. Claude Desktop을 완전히 종료했다가 다시 켭니다.
3. Fusion과 애드인이 켜져 있는 상태에서 채팅: "바탕화면 drawing2fusion\samples\drawings\pdf\plate_tapped.pdf 를 모델링해줘"

### Claude Code
프로젝트 폴더의 `.mcp.json`에 등록돼 있어서, 이 폴더에서 Claude Code를 열면 바로 쓸 수 있습니다.

### 도구 목록

| 도구 | 하는 일 |
|---|---|
| `d2f_status` | Fusion 연결, 애드인 버전 |
| `d2f_extract` | 도면 → `out/<이름>/part-spec.json`, 파라미터(출처 포함)·이슈·질문 |
| `d2f_questions` / `d2f_answer` | 질문 목록 / 답 적기 (`[{id, answer, value?}]`) |
| `d2f_get_spec` / `d2f_put_spec` | 스펙 읽기 / 고치기 (검증 오류가 있으면 쓰지 않음) |
| `d2f_validate` | Fusion 없이 스펙 검사 |
| `d2f_build` | Fusion 빌드 → 실측 → 비교 (도면에서 온 스펙이면 실루엣·단면도 대조) → 실패한 검사 |
| `d2f_report` | 빌드 결과 요약 |
| `d2f_compare_image` | 도면 이미지 | 스냅샷 | 검증표 한 장 |

보통 흐름: `d2f_extract` → (질문 있으면 `d2f_answer`) → `d2f_build` → 실패한 검사를 보고 `d2f_get_spec` / `d2f_put_spec`으로 고침 → 다시 `d2f_build`.
이것이 계획서의 "자동 수정 루프"입니다 — 고치는 판단은 Claude가, 모델링과 검증은 결정적인 코드가 합니다.

서버 직접 실행(디버깅): `py -3 -m d2f.mcp_server` 후 JSON-RPC 한 줄씩 입력.
