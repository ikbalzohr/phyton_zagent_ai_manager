"""Static Zagent ArcMap packaging tool-surface sync. Does not import ArcMap or start services.

Maintains manifest factories and snapshots for the source-declared 19 native/Smart Corner tools.
Requires Python 3.11. Fails closed on duplicates and pre-existing unaccounted names.
"""
import ast
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agents" / "arcmap"
MODULES = (
    "native_layer_properties.py",
    "native_grids.py",
    "smart_corner.py",
)
SNAPSHOT = AGENT / "runtime-tools.snapshot.json"
COMPONENT = AGENT / "arcmap.component.json"
TOP_COMPONENT = ROOT / "component-manifest.json"
FACTORY = ROOT / "src" / "Zagent.Core" / "Agents" / "ArcMap" / "ArcMapAgentManifestFactory.cs"


def names(module_path):
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("arcmap_"):
            continue
        if any(isinstance(d, ast.Call)
               and isinstance(d.func, ast.Attribute)
               and d.func.attr == "tool"
               for d in node.decorator_list):
            result.append(node.name)
    return result


extra = []
for module in MODULES:
    extra.extend(names(AGENT / "engine" / "mcp_server" / "tools" / module))

if len(extra) != 19 or len(set(extra)) != 19:
    raise RuntimeError(f"Expected 19 distinct Native/Grid/Smart Corner tools; got {extra}")

snap = json.loads(SNAPSHOT.read_text(encoding="utf-8-sig"))
existing = list(snap["tools"])
if len(existing) != len(set(existing)) or len(existing) != snap["count"]:
    raise RuntimeError("Existing ArcMap tool snapshot is inconsistent.")
added = sorted(set(extra) - set(existing))
if set(extra) & set(existing):
    if set(extra).issubset(existing):
        added = []
    else:
        raise RuntimeError("Source tools partially overlap snapshot; reconcile first.")
if len(existing) != 132 and not (len(existing) == 151 and not added):
    raise RuntimeError(f"Unexpected baseline ArcMap snapshot count: {len(existing)}")
tools = sorted(set(existing) | set(extra))
if len(tools) != 151:
    raise RuntimeError(f"Unexpected resultant tool count: {len(tools)}")

snap["tools"] = tools
snap["count"] = len(tools)
SNAPSHOT.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

component = json.loads(COMPONENT.read_text(encoding="utf-8-sig"))
component["public_tools"]["count"] = len(tools)
component["public_tools"]["registry_capabilities"] = len(tools)
component["baseline"]["canonical_reason"] = (
    "Frozen Stage 3 baseline 124 tools; Orchestrator +8 to 132; "
    "Native Layer Properties +6, Existing Grid/Graticule +6, Smart Corner +7 to 151."
)
component.setdefault("candidate_features", {})["smart_corner_digitizer"] = {
    "status": "DEV_UNIT_PASS_NOT_LIVE_ACCEPTED",
    "tools": 7,
    "new_total": 151
}
COMPONENT.write_text(json.dumps(component, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

top = json.loads(TOP_COMPONENT.read_text(encoding="utf-8-sig"))
top["components"]["arcmap"]["public_tools"] = len(tools)
top["components"]["arcmap"]["capability_registry_entries"] = len(tools)
old_count = int(top["runtime_snapshot"]["arcmap_tools"])
top["runtime_snapshot"]["arcmap_tools"] = len(tools)
# Preserve historical routed-tool contract and add only the exact ArcMap delta.
top["runtime_snapshot"]["connected_total_tools"] += len(tools) - old_count
top["path_contract"]["production_install"] = "<user-selected base folder>\\Zagent Master or Zagent User"
TOP_COMPONENT.write_text(json.dumps(top, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

factory = FACTORY.read_text(encoding="utf-8")
match = re.search(
    r'(public static IReadOnlyList<string> KnownToolNames \{ get; \} =\s*\[)(.*?)(\n\s*\];)',
    factory,
    re.S,
)
if not match:
    raise RuntimeError("Unable to locate ArcMapAgentManifestFactory.KnownToolNames.")
old_block = match.group(2)
current = re.findall(r'"(arcmap_[^"]+)"', old_block)
if len(current) != len(set(current)):
    raise RuntimeError("Core ArcMap capability names duplicated.")
missing = sorted(set(tools) - set(current))
unexpected = set(current) - set(tools)
if unexpected:
    raise RuntimeError(f"Core manifest has unknown tool names: {sorted(unexpected)}")
new_block = "\n" + "\n".join(f'        "{name}",' for name in tools)
factory = factory[:match.start()] + match.group(1) + new_block + match.group(3) + factory[match.end():]
# Accurate access control metadata for new read-only / destructive tool types.
factory = factory.replace(
    'suffix is "agent_safety_status" or "ping" or',
    'suffix is "agent_safety_status" or "ping" or\n'
    '            "smart_corner_preflight" or "smart_corner_preview" or\n'
    '            "smart_corner_review" or "smart_corner_validate" or\n'
    '            "smart_corner_status" or'
)
factory = factory.replace(
    'suffix is "save_current_map" or',
    'suffix is "save_current_map" or\n'
    '            "smart_corner_commit" or "smart_corner_rollback" or\n'
    '            "remove_native_grid" or'
)
FACTORY.write_text(factory, encoding="utf-8")

print(f"ARCMAP_TOOL_CONTRACT_SYNC_PASS baseline=132 new=19 total={len(tools)}")
print(f"ARC_TOOL_SURFACE={len(tools)}")
