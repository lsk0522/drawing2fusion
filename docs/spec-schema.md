# spec-schema.md — part-spec.json (v0.1 초안)

도면과 Fusion 모델 사이의 중간 표현. 사람이 읽고 고칠 수 있어야 한다.

## 1. 원칙

- **단위:** 길이 mm, 각도 deg (Fusion 변환은 애드인 `units`에서만)
- **좌표계:** 원점 = 도면의 기준(데이텀) 코너. X = 정면도 가로, Y = 깊이, Z = 높이
- **모든 치수는 출처를 가진다:** `source` = 도면 위치 / 사용자 입력 / 추론
- **추론값은 표시한다:** 도면에 없던 값은 `source.type = "inferred"` → 리포트에서 강조
- **feature 순서 = Fusion 타임라인 순서**

## 2. 최상위 구조

```json
{
  "schema_version": "0.1",
  "part": {
    "name": "bracket_01",
    "material": "AL6061",
    "drawing": { "file": "bracket_01.pdf", "sheet": 1, "scale": "1:1", "units": "mm" },
    "general_tolerance": "ISO 2768-m"
  },
  "parameters": { },
  "features": [ ],
  "checks": [ ],
  "notes": [ ]
}
```

## 3. parameters

Fusion User Parameters로 그대로 등록된다.

```json
"parameters": {
  "L":      { "value": 80,  "source": { "type": "drawing", "view": "front", "label": "80",  "bbox": [412, 120, 450, 140], "confidence": 0.98 } },
  "W":      { "value": 40,  "source": { "type": "drawing", "view": "top",   "label": "40",  "confidence": 0.97 } },
  "T":      { "value": 6,   "source": { "type": "drawing", "view": "right", "label": "6",   "confidence": 0.91 } },
  "hole_d": { "value": 6.6, "source": { "type": "drawing", "view": "top",   "label": "2-Ø6.6", "confidence": 0.88 }, "tolerance": [0, 0.1] },
  "fillet_r": { "value": 3, "source": { "type": "inferred", "reason": "R 표기 있으나 값 흐림" }, "confidence": 0.55 }
}
```

`confidence < 0.8` (설정값) 이거나 `inferred`면 사전 검증에서 질문 대상이 된다.

## 4. features

공통 필드: `id`(타임라인 이름), `type`, `depends_on`(선택)

### sketch

```json
{
  "id": "sk_base",
  "type": "sketch",
  "plane": "XY",
  "offset": 0,
  "entities": [
    { "id": "r1", "kind": "rect", "corner": [0, 0], "size": ["L", "W"] }
  ],
  "constraints": "auto"
}
```

엔티티 종류: `line`, `arc`, `circle`, `rect`, `slot`, `polyline`
값 자리에는 숫자 또는 파라미터 이름/식(`"L/2"`) 사용 가능.

### extrude

```json
{ "id": "ex_base", "type": "extrude", "profile": "sk_base", "distance": "T",
  "direction": "positive", "operation": "new_body" }
```

`operation`: `new_body` | `join` | `cut` | `intersect`
`distance`: 값 | `"through_all"` | `{ "to_face": "<ref>" }`

### revolve

```json
{ "id": "rv_shaft", "type": "revolve", "profile": "sk_half", "axis": "X", "angle": 360, "operation": "new_body" }
```

### hole

```json
{
  "id": "h_mount", "type": "hole",
  "face": { "plane": "XY", "offset": "T" },
  "positions": [[10, 10], ["L-10", 10]],
  "hole_type": "simple",
  "diameter": "hole_d",
  "depth": "through_all",
  "thread": null
}
```

`hole_type`: `simple` | `counterbore` | `countersink`
`thread`: `{ "spec": "M6x1.0", "modeled": false }` — 1차는 탭 표기만 기록

### fillet / chamfer

```json
{ "id": "f_corners", "type": "fillet", "edges": { "select": "vertical_outer", "of": "ex_base" }, "radius": "fillet_r" }
{ "id": "c_top",    "type": "chamfer", "edges": { "select": "top_outer", "of": "ex_base" }, "distance": 1 }
```

엣지 선택은 좌표 대신 **의미 기반 셀렉터**를 쓴다 (`vertical_outer`, `top_outer`, `hole_edges:h_mount` 등).
Fusion 엣지 ID는 빌드마다 바뀔 수 있으므로, 셀렉터 → 엣지 해석은 빌더가 기하 조건으로 수행한다.

### pattern

```json
{ "id": "p_holes", "type": "pattern_rect", "features": ["h_mount"], "x": { "count": 3, "pitch": 20 }, "y": { "count": 1 } }
{ "id": "p_bolt",  "type": "pattern_circ", "features": ["h_pcd"], "axis": "Z", "count": 6, "angle": 360 }
```

## 5. checks — 사후 검증 대상

사전에 "도면이 보장하는 사실"을 적어두고, 빌드 후 실측과 비교한다.

```json
"checks": [
  { "id": "c_bbox",  "kind": "bbox",          "expected": ["L", "W", "T"] },
  { "id": "c_hole_n","kind": "hole_count",    "diameter": "hole_d", "expected": 2 },
  { "id": "c_hole_d","kind": "hole_diameter", "feature": "h_mount", "expected": "hole_d", "tol": [0, 0.1] },
  { "id": "c_pos1",  "kind": "distance",      "from": "face:x_min", "to": "axis:h_mount[0]", "expected": 10 },
  { "id": "c_vol",   "kind": "volume",        "expected": null, "note": "도면에 없으면 기록만" }
]
```

`tol` 없으면 `part.general_tolerance` → 그것도 없으면 `D2F_DEFAULT_TOL_MM`.

## 6. notes

도면의 주기(注記) 중 모델에 반영 못 한 것: 표면거칠기, GD&T, 열처리 등.
리포트에 "미반영 항목"으로 표시한다.

```json
"notes": [
  { "text": "모든 모서리 C0.5", "applied": false, "reason": "edge selector 미지원" },
  { "text": "⏥ 0.02 A", "applied": false, "reason": "GD&T 범위 밖" }
]
```

## 7. 버전 관리

- `schema_version` 올릴 때 `d2f/spec/migrate.py`에 변환 함수 추가
- JSON Schema 파일: `d2f/spec/part-spec.schema.json` (이 문서와 동기화)

## 8. 추가된 항목 (2026-10-24)

### 스케치 평면 좌표
스펙 좌표는 항상 **월드 축** 기준입니다. `XY` → (X, Y), `XZ` → (X, Z), `YZ` → (Y, Z).
Fusion의 XZ/YZ 스케치 축은 방향이 다르지만(XZ는 y가 −Z, YZ는 x가 −Z) 빌더가 변환합니다.
`offset`은 평면 법선 방향 (+Z / +Y / +X), 돌출 `direction: "positive"`도 같은 방향입니다.

### 슬롯
```json
{ "kind": "slot", "start": ["L1+13", 0], "end": ["L1+47", 0], "width": "key_w" }
```
`start`/`end`는 양 끝 반원의 중심.

### 카운터싱크 (90°만)
```json
{ "type": "hole", "hole_type": "countersink", "diameter": "hd", "cs_diameter": "csd", "cs_angle": 90, ... }
```
구멍을 뚫은 뒤 입구 모서리에 (cs_diameter − diameter)/2 챔퍼를 겁니다 (`<id>_cs`).

### 패턴
```json
{ "id": "p_cs",   "type": "pattern_rect", "features": ["h_cs"], "x": { "count": 2, "pitch": "px" }, "y": { "count": 2, "pitch": "py" } }
{ "id": "p_bolt", "type": "pattern_circ", "features": ["h_bolt"], "axis": "Z", "count": "n_bolt", "angle": 360 }
```
원형 패턴 축은 원점을 지나는 X/Y/Z 축. 구멍의 `_cb`/`_cs` 피처도 함께 복제됩니다.
개수 같은 단위 없는 파라미터는 `"unit": ""`.

### 파라미터 값으로 식 사용
`"value": "h1 + h2"`, `"value": "H - (h1)"`, `"value": "L"` — 다른 파라미터를 참조하는 식 가능 (순환 참조는 E206).

### 출처 종류
`drawing` / `derived` / `inferred` / `conflict` / `unresolved` / `user` — 뜻과 빌드 동작은 [dxf-reading.md](dxf-reading.md) 2장.
`conflict`는 `drawn`(그려진 값)과 `shown`(치수 문자 값)을 함께 가지며 E107 질문이 됩니다.

### 검사 (`checks`) 추가
- `hole_positions`에 `"axis": "X" | "Y" | "Z"` (기본 Z), `"diameter"`로 구멍 거르기, `feature` 없으면 몸체 전체 구멍
- `volume`에 `"rel_tol"` (기본 1%)
- `plane` — `{ "kind": "plane", "axis": "Z", "at": "key_t - d_bore/2" }`: 그 축에 수직인 평면이 좌표 `at`(mm)에 있어야 통과 (E408).
  부피·bbox가 같아도 **방향이 틀린 형상**(키홈이 −Y 쪽에 파임, 포켓이 반대 면에 생김)을 잡습니다. 측정값 `bodies[].planes`
