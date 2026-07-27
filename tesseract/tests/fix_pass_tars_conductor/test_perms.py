# tesseract/tests/fix_pass_tars_conductor/test_perms.py
import yaml
from pathlib import Path

PERMS = Path(__file__).resolve().parents[2] / "config" / "permissions.yaml"

LANE_TOOLS = [
    "lane_open", "lane_send", "lane_read", "lane_status", "lane_attach",
    "lane_close", "lane_list", "lane_named_ensure", "lane_named_get",
    "lane_named_list",
]

def test_all_lane_tools_auto():
    tools = (yaml.safe_load(PERMS.read_text(encoding="utf-8")) or {}).get("tools", {})
    missing = [t for t in LANE_TOOLS if tools.get(t) != "auto"]
    assert not missing, f"lane tools not AUTO in permissions.yaml: {missing}"
