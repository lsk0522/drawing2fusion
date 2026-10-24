"""Double-click entry points used by run.bat / install.bat.

  python -m d2f menu            interactive menu
  python -m d2f doctor          check Python, add-in install, Fusion connection
  python -m d2f quick SPEC      build one spec (drag & drop onto run.bat) and open the result folder
"""

import glob
import os
import subprocess
import sys

from . import cli
from .bridge_client import BridgeClient, BridgeError

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ADDIN_SRC = os.path.join(ROOT, "addin", "Drawing2Fusion")
DEFAULT_LIVE_MS = 300


def _addin_dst():
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Autodesk", "Autodesk Fusion 360", "API", "AddIns", "Drawing2Fusion")


def _open_folder(path):
    if os.environ.get("D2F_NO_OPEN"):
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - opens Explorer
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
    except Exception:
        pass


def _line(title=""):
    print("\n" + "=" * 60)
    if title:
        print("  " + title)
        print("=" * 60)


def _connected():
    try:
        return BridgeClient(timeout_s=5).ping()
    except BridgeError:
        return None


START_ADDIN_HELP = (
    "  1) Fusion을 켭니다\n"
    "  2) Shift+S → '추가 기능(Add-Ins)' 탭 → Drawing2Fusion 선택 → '실행(Run)'\n"
    "     ('시작 시 실행'에 체크해 두면 다음부터 자동으로 켜집니다)\n"
    "  3) 다시 시도하세요")


# --------------------------------------------------------------------------- doctor
def doctor():
    _line("Drawing2Fusion 상태 점검")
    ok = True

    v = sys.version_info
    py_ok = v >= (3, 9)
    print("%s Python %d.%d.%d  (%s)" % ("✅" if py_ok else "❌", v[0], v[1], v[2], sys.executable))
    ok &= py_ok

    dst = _addin_dst()
    if os.path.exists(dst):
        same = os.path.realpath(dst) == os.path.realpath(ADDIN_SRC)
        print("✅ 애드인 설치됨: %s%s" % (dst, "" if same else "  (⚠ 이 폴더가 아닌 다른 복사본)"))
    else:
        print("❌ 애드인이 설치되지 않았습니다 → install.bat 을 먼저 실행하세요")
        ok = False

    token = os.path.join(os.path.expanduser("~"), ".d2f", "token")
    print("%s 토큰 파일: %s" % ("✅" if os.path.exists(token) else "⚠️", token if os.path.exists(token)
                                  else "아직 없음 (애드인을 한 번 실행하면 생깁니다)"))

    info = _connected()
    if info:
        print("✅ Fusion 연결됨 — Fusion %s, 애드인 %s, 열린 문서 '%s'"
              % (info.get("fusion_version"), info.get("addin_version"), info.get("active_document")))
    else:
        print("❌ Fusion에 연결할 수 없습니다. 애드인을 실행해 주세요:\n" + START_ADDIN_HELP)
        ok = False

    chans = os.environ.get("D2F_NOTIFY_CHANNELS", "console,fusion")
    extra = []
    if "discord" in chans and not os.environ.get("DISCORD_WEBHOOK_URL"):
        extra.append("discord 채널이 켜져 있지만 DISCORD_WEBHOOK_URL 이 비어 있음")
    if "telegram" in chans and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        extra.append("telegram 채널이 켜져 있지만 토큰/채팅 ID가 비어 있음")
    print("ℹ️ 알림 채널: %s%s" % (chans, "".join("\n   ⚠ " + e for e in extra)))
    print("\n" + ("모든 준비 완료!" if ok else "위의 ❌ 항목을 해결해 주세요."))
    return 0 if ok else 1


# --------------------------------------------------------------------------- build helpers
def _build(spec_path, live_ms=DEFAULT_LIVE_MS, export="f3d", open_result=True):
    spec_path = spec_path.strip().strip('"').strip("'")
    if not os.path.isfile(spec_path):
        print("❌ 파일이 없습니다: %s" % spec_path)
        return 1
    ext = os.path.splitext(spec_path)[1].lower()
    if ext not in (".json", ".dxf", ".pdf"):
        print("❌ 자동으로 되는 형식은 도면 .dxf, 벡터 .pdf, part-spec .json 입니다. "
              "사진·스캔 도면은 Claude 채팅에 올려 판독하세요 (docs/image-reading.md).")
        return 1
    if _connected() is None:
        print("❌ Fusion에 연결할 수 없습니다.\n" + START_ADDIN_HELP)
        return cli.EXIT_BRIDGE
    _line("빌드: %s" % os.path.basename(spec_path))
    code = cli.main(["run", spec_path, "--out", os.path.join(ROOT, "out"),
                     "--live", str(live_ms), "--export", export])
    try:
        name = os.path.splitext(os.path.basename(spec_path))[0] if ext in (".dxf", ".pdf") else \
            ((cli.validator.load(spec_path).get("part") or {}).get("name")
             or os.path.splitext(os.path.basename(spec_path))[0])
        out_dir = os.path.join(ROOT, "out", name)
        if open_result and os.path.isdir(out_dir):
            _open_folder(out_dir)
    except Exception:
        pass
    print({0: "✅ 성공", 1: "🔴 문제가 있습니다 (위 메시지 확인)", 2: "🟠 확인이 필요한 값이 있습니다 (위 질문 확인)",
           3: "🔴 Fusion 연결 실패"}.get(code, "종료 코드 %s" % code))
    return code


def quick(spec_path):
    return _build(spec_path)


def _ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except EOFError:
        return default
    return v or default


def _choose_sample(kind="spec"):
    if kind == "dxf":
        files = sorted(glob.glob(os.path.join(ROOT, "samples", "drawings", "dxf", "*.dxf")) +
                       glob.glob(os.path.join(ROOT, "samples", "drawings", "pdf", "*.pdf")))
    else:
        files = sorted(glob.glob(os.path.join(ROOT, "samples", "specs", "*.json"))) + \
            sorted(glob.glob(os.path.join(ROOT, "samples", "broken", "*.json")))
    if not files:
        print("샘플이 없습니다.")
        return None
    for i, f in enumerate(files, 1):
        print("  %2d) %s" % (i, os.path.relpath(f, os.path.join(ROOT, "samples"))))
    sel = _ask("번호 선택 (Enter=취소): ")
    if not sel.isdigit() or not (1 <= int(sel) <= len(files)):
        return None
    return files[int(sel) - 1]


def _live_setting(current):
    v = _ask("그리는 속도 ms (지금 %d, 0=즉시, Enter=유지): " % current, str(current))
    return int(v) if v.isdigit() else current


def menu():
    live = DEFAULT_LIVE_MS
    while True:
        info = _connected()
        _line("Drawing2Fusion  ·  Fusion %s" % ("연결됨 ✅" if info else "연결 안 됨 ❌"))
        print("  1) 샘플 부품 만들기 (스펙)")
        print("  2) 내 도면/스펙 파일로 만들기 (.dxf / .pdf / .json 을 이 창에 끌어다 놓아도 됩니다)")
        print("  8) 샘플 도면(DXF·PDF)으로 만들기")
        print("  3) 스펙 검사만 하기 (Fusion 없이)")
        print("  4) 상태 점검")
        print("  5) 결과 폴더 열기")
        print("  6) 그리는 속도 바꾸기 (지금 %d ms)" % live)
        print("  7) 단위 테스트 실행")
        print("  0) 끝내기")
        sel = _ask("\n선택: ")
        if sel in ("0", "q", "Q"):
            return 0
        if sel == "1":
            f = _choose_sample()
            if f:
                _build(f, live)
        elif sel == "2":
            f = _ask("도면/스펙 파일 경로: ")
            if f:
                _build(f, live)
        elif sel == "3":
            f = _choose_sample() if _ask("샘플에서 고를까요? (y/N): ").lower() == "y" else _ask("스펙 파일 경로: ")
            if f:
                cli.main(["validate", f.strip().strip('"')])
        elif sel == "4":
            doctor()
        elif sel == "5":
            out = os.path.join(ROOT, "out")
            os.makedirs(out, exist_ok=True)
            _open_folder(out)
        elif sel == "6":
            live = _live_setting(live)
        elif sel == "8":
            f = _choose_sample("dxf")
            if f:
                _build(f, live)
        elif sel == "7":
            subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=ROOT)
        else:
            continue
        _ask("\n(Enter를 누르면 메뉴로)")
