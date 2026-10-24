"""Register the Drawing2Fusion MCP server in Claude Desktop (Windows / macOS).

    py -3 tools\\install_mcp.py          (mcp_install.bat does this)

Adds "drawing2fusion" to claude_desktop_config.json -> mcpServers, keeping everything else and
writing a .bak copy first. Restart Claude Desktop afterwards.
"""
import json
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def config_path():
    if sys.platform == "win32":
        return os.path.join(os.environ["APPDATA"], "Claude", "claude_desktop_config.json")
    return os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")


def main():
    path = config_path()
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        shutil.copyfile(path, path + ".bak")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg.setdefault("mcpServers", {})["drawing2fusion"] = {
        "command": sys.executable,
        "args": [os.path.join(ROOT, "d2f", "mcp_server.py")],
        "env": {"PYTHONIOENCODING": "utf-8"},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("✅ Claude Desktop 설정에 drawing2fusion MCP 서버를 등록했습니다:\n   %s" % path)
    print("   Claude Desktop을 완전히 종료했다가 다시 켜면 '도면 → Fusion' 도구가 보입니다.")


if __name__ == "__main__":
    main()
