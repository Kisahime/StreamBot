from __future__ import annotations

import re
from typing import Any

import sounddevice as sd

_CABLE = re.compile(r"cable|vb-audio|voicemeeter|blackhole|virtual audio|vb cable", re.I)


def list_devices() -> dict[str, list[dict[str, Any]]]:
    devices = sd.query_devices()
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    for idx, dev in enumerate(devices):
        item = {
            "index": idx,
            "name": dev["name"],
            "hostapi": sd.query_hostapis(dev["hostapi"])["name"],
            "in": int(dev["max_input_channels"]),
            "out": int(dev["max_output_channels"]),
            "rate": float(dev["default_samplerate"]),
            "cable": bool(_CABLE.search(str(dev["name"]))),
        }
        if item["in"] > 0:
            inputs.append(item)
        if item["out"] > 0:
            outputs.append(item)
    default_in, default_out = sd.default.device
    return {
        "inputs": inputs,
        "outputs": outputs,
        "default_input": default_in,
        "default_output": default_out,
    }


def match_device(devices: list[dict[str, Any]], index, name: str | None):
    """Prefer a saved device name when the index has moved."""
    wanted = (name or "").strip()
    if wanted:
        for dev in devices:
            if str(dev.get("name") or "") == wanted:
                return dev.get("index")
    return index
