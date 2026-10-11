# -*- coding: utf-8 -*-
"""ARC-07: edit exactly one existing ArcObjects IScaleBar on CURRENT page."""
from __future__ import division
import arcpy
from core.registry import action
from core.safety import require_live_write
from stage3 import bridge as arcobjects

UNITS = {"METERS": (9, "m", 1.0),
         "KILOMETERS": (10, "km", 1000.0)}


def _scale_bars():
    client, framework, arcmapui, carto = arcobjects._typed_arcobjects_modules()
    app = client.CreateObject("esriFramework.AppRef",
                              interface=framework.IApplication)
    arcobjects._verify_current_process(app)
    doc = app.Document.QueryInterface(arcmapui.IMxDocument)
    layout = doc.PageLayout.QueryInterface(carto.IPageLayout)
    graphics = layout.QueryInterface(carto.IGraphicsContainer)
    graphics.Reset()
    rows = []
    while True:
        element = graphics.Next()
        if element is None:
            break
        try:
            frame = element.QueryInterface(carto.IMapSurroundFrame)
            bar = frame.MapSurround.QueryInterface(carto.IScaleBar)
            name = element.QueryInterface(carto.IElementProperties).Name
            rows.append((str(name or ""), bar))
        except Exception:
            continue
    return doc, rows


def _state(name, bar):
    return {"element_name": name, "units": int(bar.Units),
            "unit_label": str(bar.UnitLabel),
            "division": float(bar.Division),
            "divisions": int(bar.Divisions),
            "subdivisions": int(bar.Subdivisions)}


@action("list_native_scale_bars")
def list_native_scale_bars(command):
    doc, rows = _scale_bars()
    return {"ok": True, "scale_bars": [_state(n, b) for n, b in rows],
            "saved": False}


@action("set_native_scale_bar")
def set_native_scale_bar(command):
    blocked = require_live_write()
    if blocked:
        return blocked
    name = str(command.get("element_name") or "").strip()
    if not name:
        raise ValueError("element_name required; no implicit first-match")
    target_unit = str(command.get("units") or "").upper()
    if target_unit not in UNITS:
        raise ValueError("units must be METERS or KILOMETERS")
    doc, rows = _scale_bars()
    selected = [(n, b) for n, b in rows if n == name]
    if len(selected) != 1:
        raise RuntimeError("Expected exactly one named scale bar")
    bar = selected[0][1]
    before = _state(name, bar)
    if before["units"] not in (9, 10):
        raise RuntimeError("Only existing meters/kilometers can be converted")
    next_code, next_label, next_factor = UNITS[target_unit]
    old_factor = 1.0 if before["units"] == 9 else 1000.0
    new_division = (before["division"] * old_factor / next_factor
                    if command.get("preserve_distance", True)
                    else before["division"])
    if new_division <= 0:
        raise ValueError("Scale bar division must be positive")
    try:
        bar.Units = next_code
        bar.UnitLabel = next_label
        bar.Division = new_division
        arcpy.RefreshActiveView()
    except Exception:
        try:
            bar.Units = before["units"]
            bar.UnitLabel = before["unit_label"]
            bar.Division = before["division"]
        except Exception:
            pass
        raise
    return {"ok": True, "before": before, "after": _state(name, bar),
            "saved": False, "verified_on_arcmap": False}
