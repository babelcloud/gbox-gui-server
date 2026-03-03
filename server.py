"""
GBOX Local API Server
Implements gbox.ai UI Action, Command, and File System APIs locally using pyautogui.
Listens on 0.0.0.0:5789
"""

import base64
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import traceback

import pyautogui
import pyperclip
from flask import Flask, jsonify, make_response, request
from PIL import Image
from PIL import PngImagePlugin  # noqa: force-load PNG plugin at import time so PyInstaller bundles it eagerly

try:
    import mss
except ImportError:
    mss = None

app = Flask(__name__)
app.config["PROPAGATE_EXCEPTIONS"] = True

try:
    import _version
    GIT_COMMIT = _version.GIT_COMMIT
except Exception:
    GIT_COMMIT = "unknown"

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = False

platform_name = platform.system()  # noqa


def _json_500(msg: str, tb: str = None):
    """Return a 500 response with JSON body; always used for errors."""
    body = {"error": msg}
    if tb:
        body["traceback"] = tb
    resp = make_response(json.dumps(body, ensure_ascii=False), 500)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp


@app.errorhandler(500)
def handle_500(e):
    """Ensure 500 responses are always JSON."""
    traceback.print_exc()
    logging.exception("Unhandled error")
    try:
        msg = str(e) if e else "Internal server error"
    except Exception:
        msg = "Internal server error"
    return _json_500(msg, traceback.format_exc())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_action_id() -> str:
    return str(uuid.uuid4())


def _parse_duration_ms(s, default_ms: int = 500) -> float:
    """Parse a duration string like '500ms', '1s', '2m', '1h' into seconds."""
    if s is None:
        return default_ms / 1000.0
    s = str(s).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)", s, re.IGNORECASE)
    if not m:
        return default_ms / 1000.0
    value, unit = float(m.group(1)), m.group(2).lower()
    if unit == "ms":
        return value / 1000.0
    elif unit == "s":
        return value
    elif unit == "m":
        return value * 60
    else:
        return value * 3600


def _take_screenshot_b64(clip=None) -> str:
    """Take a screenshot and return as base64-encoded PNG data URI. (legacy wrapper)"""
    png_bytes = _take_screenshot_buf(clip)
    b64 = base64.b64encode(png_bytes).decode()
    return f"data:image/png;base64,{b64}"


def _get_screenshot_phases(options: dict) -> list:
    if not options:
        return []
    sc = options.get("screenshot")
    if sc is None or sc is False:
        return []
    if sc is True:
        return ["before", "after"]
    if isinstance(sc, dict):
        return sc.get("phases", ["before", "after"])
    return []


def _get_screenshot_delay(options: dict) -> float:
    if not options:
        return 0.5
    sc = options.get("screenshot")
    if isinstance(sc, dict):
        return _parse_duration_ms(sc.get("delay", "500ms"), 500)
    return 0.5


def _action_result(action_id: str, options=None,
                   before_uri: Optional[str] = None,
                   after_uri: Optional[str] = None) -> dict:
    result = {
        "message": "Action executed successfully",
        "actionId": action_id,
    }
    if before_uri or after_uri:
        sc = {}
        if before_uri:
            sc["before"] = {"uri": before_uri}
        if after_uri:
            sc["after"] = {"uri": after_uri}
        result["screenshot"] = sc
    return result


KEY_MAP = {
    "arrowUp": "up", "arrowDown": "down", "arrowLeft": "left", "arrowRight": "right",
    "escape": "esc", "backspace": "backspace", "delete": "delete",
    "enter": "enter", "space": "space", "tab": "tab",
    "home": "home", "end": "end", "pageUp": "pageup", "pageDown": "pagedown",
    "insert": "insert", "capsLock": "capslock", "numLock": "numlock",
    "scrollLock": "scrolllock", "pause": "pause", "printScreen": "printscreen",
    "meta": "win", "win": "win", "cmd": "win", "option": "alt",
    "control": "ctrl", "shift": "shift", "alt": "alt",
    "numpad0": "num0", "numpad1": "num1", "numpad2": "num2", "numpad3": "num3",
    "numpad4": "num4", "numpad5": "num5", "numpad6": "num6", "numpad7": "num7",
    "numpad8": "num8", "numpad9": "num9",
    "numpadAdd": "add", "numpadSubtract": "subtract",
    "numpadMultiply": "multiply", "numpadDivide": "divide",
    "numpadDecimal": "decimal", "numpadEnter": "enter", "numpadEqual": "=",
    "volumeUp": "volumeup", "volumeDown": "volumedown", "volumeMute": "volumemute",
    "mediaPlayPause": "playpause", "mediaStop": "stop",
    "mediaNextTrack": "nexttrack", "mediaPreviousTrack": "prevtrack",
}


def _map_key(k: str) -> str:
    return KEY_MAP.get(k, k)


def _file_info(path: str) -> dict:
    p = Path(path)
    stat = p.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    try:
        mode = oct(stat.st_mode)[-3:]
    except Exception:
        mode = "644"
    if p.is_dir():
        return {
            "type": "directory",
            "name": p.name,
            "path": str(p).replace("\\", "/") + "/",
            "mode": mode,
            "modified": modified,
        }
    size_bytes = stat.st_size
    if size_bytes < 1024:
        size_str = f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        size_str = f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        size_str = f"{size_bytes / (1024 * 1024):.1f}MB"
    else:
        size_str = f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"
    return {
        "type": "file",
        "name": p.name,
        "path": str(p).replace("\\", "/"),
        "size": size_str,
        "mode": mode,
        "modified": modified,
    }


def _resolve_path(path: str, working_dir: Optional[str] = None) -> str:
    if os.path.isabs(path):
        return path
    base = working_dir or os.getcwd()
    return os.path.join(base, path)


def _parse_timeout(timeout_str, default_s: float = 30.0) -> float:
    if timeout_str is None:
        return default_s
    # Plain integer string (e.g. "30000") is treated as milliseconds
    try:
        return float(timeout_str) / 1000.0
    except (ValueError, TypeError):
        pass
    return _parse_duration_ms(timeout_str, int(default_s * 1000))


# ---------------------------------------------------------------------------
# UI Action Routes  /api/v1/actions/*
# ---------------------------------------------------------------------------

def _take_screenshot_buf(clip=None) -> bytes:
    """Take a screenshot and return raw PNG bytes.

    Prefer mss when available: it writes PNG natively via its own zlib path,
    avoiding the lazy PIL plugin import that can fail in PyInstaller bundles
    with a zlib decompression error (corrupted archive entry).
    """
    if mss is not None:
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            if clip:
                region = {
                    "left": int(clip.get("x", 0)),
                    "top": int(clip.get("y", 0)),
                    "width": int(clip.get("width", monitor["width"])),
                    "height": int(clip.get("height", monitor["height"])),
                }
                shot = sct.grab(region)
            else:
                shot = sct.grab(monitor)
            return mss.tools.to_png(shot.rgb, shot.size)

    # Fallback: use pyautogui + PIL (requires PngImagePlugin to be importable)
    img = pyautogui.screenshot()
    if clip:
        x = int(clip.get("x", 0))
        y = int(clip.get("y", 0))
        w = int(clip.get("width", img.width))
        h = int(clip.get("height", img.height))
        img = img.crop((x, y, x + w, y + h))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _put_to_presigned_url(png_bytes: bytes, presigned_put_url: str) -> None:
    """Upload PNG bytes to a presigned S3 PUT URL."""
    import urllib.request
    req = urllib.request.Request(
        presigned_put_url,
        data=png_bytes,
        method="PUT",
        headers={"Content-Type": "image/png", "Content-Length": str(len(png_bytes))},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Presigned PUT failed with HTTP {resp.status}")


@app.route("/api/v1/actions/screenshot", methods=["POST"])
def action_screenshot():
    import sys; print('[screenshot] request received', flush=True); sys.stdout.flush(); sys.stderr.flush()
    try:
        data = request.get_json(silent=True) or {}

        # transferFormat: "base64" (default) or "storageKey"
        transfer_format = data.get("transferFormat", "base64")
        presigned_put_url = data.get("presignedPutUrl")
        storage_key = data.get("storageKey")

        clip = data.get("clip")
        png_bytes = _take_screenshot_buf(clip)

        if transfer_format == "storageKey" and presigned_put_url:
            # Upload directly to S3 via presigned PUT URL
            _put_to_presigned_url(png_bytes, presigned_put_url)
            return jsonify({
                "storageKey": storage_key,
                "outputFormat": "storageKey",
            })
        else:
            # Return as base64 (raw, no data URI prefix so caller can build it)
            image_b64 = base64.b64encode(png_bytes).decode()
            return jsonify({
                "uri": f"data:image/png;base64,{image_b64}",
                "outputFormat": "base64",
            })
    except Exception as e:
        traceback.print_exc()
        logging.exception("screenshot failed")
        try:
            msg = str(e)
        except Exception:
            msg = "Screenshot failed"
        return _json_500(msg, traceback.format_exc())


@app.route("/api/v1/actions/click", methods=["POST"])
def action_click():
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)

    before_uri = _take_screenshot_b64() if "before" in phases else None

    x = data.get("x")
    y = data.get("y")
    if x is None or y is None:
        return jsonify({"error": "x and y coordinates are required"}), 400

    button = data.get("button", "left")
    # Support both 'clicks' (click count, from remote service) and 'double' (legacy boolean)
    clicks_param = data.get("clicks")
    double = data.get("double", False)
    if clicks_param is not None:
        click_count = int(clicks_param)
    elif double:
        click_count = 2
    else:
        click_count = 1
    modifier_keys = data.get("modifierKeys", [])

    pyautogui_button = "left" if button == "left" else ("right" if button == "right" else "middle")
    mapped_modifiers = [_map_key(k) for k in modifier_keys]

    for mod in mapped_modifiers:
        pyautogui.keyDown(mod)
    try:
        if click_count == 2:
            pyautogui.doubleClick(x=int(x), y=int(y), button=pyautogui_button)
        elif click_count > 2:
            pyautogui.click(x=int(x), y=int(y), button=pyautogui_button, clicks=click_count)
        else:
            pyautogui.click(x=int(x), y=int(y), button=pyautogui_button)
    finally:
        for mod in reversed(mapped_modifiers):
            pyautogui.keyUp(mod)

    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    else:
        after_uri = None

    result = _action_result(action_id, options, before_uri, after_uri)
    result["actual"] = {"x": int(x), "y": int(y)}
    return jsonify(result)


@app.route("/api/v1/actions/move", methods=["POST"])
def action_move():
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    x = data.get("x")
    y = data.get("y")
    if x is None or y is None:
        return jsonify({"error": "x and y are required"}), 400
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None
    pyautogui.moveTo(int(x), int(y))
    after_uri = None
    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    return jsonify(_action_result(action_id, options, before_uri, after_uri))


@app.route("/api/v1/actions/type", methods=["POST"])
def action_type():
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    text = data.get("text")
    if text is None:
        return jsonify({"error": "text is required"}), 400
    mode = data.get("mode", "append")
    press_enter = data.get("pressEnter", False)
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None
    if mode == "replace":
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)
    pyautogui.write(text, interval=0.01)
    if press_enter:
        pyautogui.press("enter")
    after_uri = None
    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    return jsonify(_action_result(action_id, options, before_uri, after_uri))


@app.route("/api/v1/actions/press-key", methods=["POST"])
def action_press_key():
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    keys = data.get("keys", [])
    if not keys:
        return jsonify({"error": "keys is required"}), 400
    combination = data.get("combination", True)
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None
    mapped = [_map_key(k) for k in keys]
    if combination and len(mapped) > 1:
        pyautogui.hotkey(*mapped)
    else:
        for k in mapped:
            pyautogui.press(k)
    after_uri = None
    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    return jsonify(_action_result(action_id, options, before_uri, after_uri))


@app.route("/api/v1/actions/scroll", methods=["POST"])
def action_scroll():
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None
    screen_w, screen_h = pyautogui.size()

    if "scrollX" in data or "scrollY" in data:
        # Absolute pixel delta mode: {x, y, scrollX, scrollY}
        x = int(data.get("x", screen_w // 2))
        y = int(data.get("y", screen_h // 2))
        scroll_x = data.get("scrollX", 0)
        scroll_y = data.get("scrollY", 0)
        pyautogui.moveTo(x, y)
        if scroll_y != 0:
            clicks = int(scroll_y / 100 * 3)
            if clicks == 0:
                clicks = 1 if scroll_y > 0 else -1
            pyautogui.scroll(clicks, x=x, y=y)
        if scroll_x != 0:
            clicks = int(scroll_x / 100 * 3)
            if clicks == 0:
                clicks = 1 if scroll_x > 0 else -1
            pyautogui.hscroll(clicks, x=x, y=y)
        actual = {"x": x, "y": y, "scrollX": scroll_x, "scrollY": scroll_y}
    elif "direction" in data:
        # Direction + click-count mode: {x, y, direction, clicks}
        direction = data.get("direction", "up")
        x = int(data.get("x", screen_w // 2))
        y = int(data.get("y", screen_h // 2))
        clicks_count = int(data.get("clicks", 3))
        sx, sy = 0, 0
        if direction == "up":
            pyautogui.scroll(clicks_count, x=x, y=y)
            sy = clicks_count * 100 // 3
        elif direction == "down":
            pyautogui.scroll(-clicks_count, x=x, y=y)
            sy = -(clicks_count * 100 // 3)
        elif direction == "left":
            pyautogui.hscroll(-clicks_count, x=x, y=y)
            sx = -(clicks_count * 100 // 3)
        else:  # right
            pyautogui.hscroll(clicks_count, x=x, y=y)
            sx = clicks_count * 100 // 3
        actual = {"x": x, "y": y, "scrollX": sx, "scrollY": sy}
    else:
        # Legacy direction + distance mode
        direction = data.get("direction", "up")
        distance_raw = data.get("distance")
        x = screen_w // 2
        y = screen_h // 2
        distance_map = {"tiny": 50, "short": 150, "medium": 300, "long": 600}
        if distance_raw is None:
            pixels = screen_h // 2
        elif isinstance(distance_raw, str):
            pixels = distance_map.get(distance_raw, 300)
        else:
            pixels = int(distance_raw)
        clicks = max(1, pixels // 100 * 3)
        sx, sy = 0, 0
        if direction == "up":
            pyautogui.scroll(clicks, x=x, y=y)
            sy = pixels
        elif direction == "down":
            pyautogui.scroll(-clicks, x=x, y=y)
            sy = -pixels
        elif direction == "left":
            pyautogui.hscroll(-clicks, x=x, y=y)
            sx = -pixels
        else:
            pyautogui.hscroll(clicks, x=x, y=y)
            sx = pixels
        actual = {"x": x, "y": y, "scrollX": sx, "scrollY": sy}

    after_uri = None
    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    result = _action_result(action_id, options, before_uri, after_uri)
    result["actual"] = actual
    return jsonify(result)


@app.route("/api/v1/actions/drag", methods=["POST"])
def action_drag():
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay_sec = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None

    if "path" in data:
        path_points = data["path"]
        if not path_points:
            return jsonify({"error": "path must not be empty"}), 400
        duration_str = data.get("duration", "50ms")
        interval = _parse_duration_ms(duration_str, 50)
        start = path_points[0]
        pyautogui.mouseDown(x=int(start["x"]), y=int(start["y"]))
        for pt in path_points[1:]:
            pyautogui.moveTo(int(pt["x"]), int(pt["y"]), duration=interval)
        end = path_points[-1]
        pyautogui.mouseUp(x=int(end["x"]), y=int(end["y"]))
        actual = {
            "start": {"x": int(start["x"]), "y": int(start["y"])},
            "end": {"x": int(end["x"]), "y": int(end["y"])},
            "duration": duration_str,
        }
    elif "startX" in data or "startY" in data:
        # Flat coordinate mode: {startX, startY, endX, endY, duration?}
        sx = int(data.get("startX", 0))
        sy = int(data.get("startY", 0))
        ex = int(data.get("endX", 0))
        ey = int(data.get("endY", 0))
        duration_val = data.get("duration")
        dur = (duration_val / 1000.0) if isinstance(duration_val, (int, float)) else _parse_duration_ms(duration_val, 500)
        pyautogui.mouseDown(x=sx, y=sy)
        pyautogui.moveTo(ex, ey, duration=dur)
        pyautogui.mouseUp(x=ex, y=ey)
        actual = {
            "start": {"x": sx, "y": sy},
            "end": {"x": ex, "y": ey},
            "duration": str(duration_val or "500ms"),
        }
    else:
        start = data.get("start")
        end = data.get("end")
        if not start or not end:
            return jsonify({"error": "start and end are required"}), 400
        if not isinstance(start, dict) or not isinstance(end, dict):
            return jsonify({"error": "Natural language targets are not supported in local mode"}), 422
        duration_str = data.get("duration", "500ms")
        dur = _parse_duration_ms(duration_str, 500)
        sx, sy = int(start["x"]), int(start["y"])
        ex, ey = int(end["x"]), int(end["y"])
        pyautogui.mouseDown(x=sx, y=sy)
        pyautogui.moveTo(ex, ey, duration=dur)
        pyautogui.mouseUp(x=ex, y=ey)
        actual = {
            "start": {"x": sx, "y": sy},
            "end": {"x": ex, "y": ey},
            "duration": duration_str,
        }

    after_uri = None
    if "after" in phases:
        time.sleep(delay_sec)
        after_uri = _take_screenshot_b64()
    result = _action_result(action_id, options, before_uri, after_uri)
    result["actual"] = actual
    return jsonify(result)


@app.route("/api/v1/actions/long-press", methods=["POST"])
def action_long_press():
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    x = data.get("x")
    y = data.get("y")
    if x is None or y is None:
        return jsonify({"error": "x and y are required"}), 400
    duration_str = data.get("duration", "500ms")
    dur = _parse_duration_ms(duration_str, 500)
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None
    pyautogui.mouseDown(x=int(x), y=int(y))
    time.sleep(dur)
    pyautogui.mouseUp(x=int(x), y=int(y))
    after_uri = None
    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    result = _action_result(action_id, options, before_uri, after_uri)
    result["actual"] = {"x": int(x), "y": int(y), "duration": duration_str}
    return jsonify(result)


@app.route("/api/v1/actions/swipe", methods=["POST"])
def action_swipe():
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    start = data.get("start")
    end = data.get("end")
    if not start or not end:
        return jsonify({"error": "start and end are required"}), 400
    if not isinstance(start, dict) or not isinstance(end, dict):
        return jsonify({"error": "start and end must be coordinate objects"}), 400
    duration_str = data.get("duration", "300ms")
    dur = _parse_duration_ms(duration_str, 300)
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None
    sx, sy = int(start["x"]), int(start["y"])
    ex, ey = int(end["x"]), int(end["y"])
    pyautogui.mouseDown(x=sx, y=sy)
    pyautogui.moveTo(ex, ey, duration=dur)
    pyautogui.mouseUp(x=ex, y=ey)
    after_uri = None
    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    result = _action_result(action_id, options, before_uri, after_uri)
    result["actual"] = {"start": {"x": sx, "y": sy}, "end": {"x": ex, "y": ey}, "duration": duration_str}
    return jsonify(result)


@app.route("/api/v1/actions/touch", methods=["POST"])
def action_touch():
    """Multi-touch simulation. Each point has a start position and a list of actions."""
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    points = data.get("points", [])
    if not points:
        return jsonify({"error": "points is required"}), 400
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None

    for point in points:
        start = point.get("start", {})
        cur_x = int(start.get("x", 0))
        cur_y = int(start.get("y", 0))
        pyautogui.moveTo(cur_x, cur_y)
        for action in point.get("actions", []):
            action_type = action.get("type", "")
            if action_type == "move":
                to_x = int(action.get("x", cur_x))
                to_y = int(action.get("y", cur_y))
                pyautogui.dragTo(to_x, to_y, duration=0.1, button="left")
                cur_x, cur_y = to_x, to_y
            elif action_type == "wait":
                dur = _parse_duration_ms(action.get("duration", "100ms"), 100)
                time.sleep(dur)
            elif action_type == "click":
                pyautogui.click(x=cur_x, y=cur_y)

    after_uri = None
    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    return jsonify(_action_result(action_id, options, before_uri, after_uri))


@app.route("/api/v1/actions/press-button", methods=["POST"])
def action_press_button():
    """Press device hardware buttons (volume up/down, etc.)."""
    data = request.get_json(silent=True) or {}
    action_id = _new_action_id()
    buttons = data.get("buttons", data.get("button", []))
    if not buttons:
        return jsonify({"error": "buttons is required"}), 400
    options = data.get("options")
    phases = _get_screenshot_phases(options)
    delay = _get_screenshot_delay(options)
    before_uri = _take_screenshot_b64() if "before" in phases else None

    BUTTON_KEY_MAP = {
        "volumeUp": "volumeup",
        "volumeDown": "volumedown",
        "volumeMute": "volumemute",
        "home": "win",
        "back": "alt+left",
        "menu": "apps",
    }
    for btn in buttons:
        key = BUTTON_KEY_MAP.get(btn)
        if key is None:
            return jsonify({"error": f"Unsupported button: {btn}"}), 400
        if "+" in key:
            parts = key.split("+")
            pyautogui.hotkey(*parts)
        else:
            pyautogui.press(key)

    after_uri = None
    if "after" in phases:
        time.sleep(delay)
        after_uri = _take_screenshot_b64()
    return jsonify(_action_result(action_id, options, before_uri, after_uri))


@app.route("/api/v1/actions/screen-size", methods=["GET"])
def action_get_screen_size():
    """Return current screen resolution."""
    w, h = pyautogui.size()
    return jsonify({"width": w, "height": h})


@app.route("/api/v1/actions/screen-resolution", methods=["POST"])
def action_set_screen_resolution():
    """Set screen resolution via OS command (Windows only)."""
    data = request.get_json(silent=True) or {}
    width = data.get("width")
    height = data.get("height")
    if width is None or height is None:
        return jsonify({"error": "width and height are required"}), 400
    width, height = int(width), int(height)

    if platform_name == "Windows":
        # Use PowerShell to change display resolution
        ps_script = (
            f"Add-Type -AssemblyName System.Windows.Forms; "
            f"$mode = [System.Windows.Forms.Screen]::PrimaryScreen; "
            f"$dm = New-Object System.Management.ManagementObject('Win32_VideoController.DeviceID=\"VideoController1\"'); "
            f"& {{ "
            f"  $signature = @'`n"
            f"[DllImport(\"user32.dll\")] public static extern bool EnumDisplaySettings(string deviceName, int modeNum, ref DEVMODE devMode);`n"
            f"[DllImport(\"user32.dll\")] public static extern int ChangeDisplaySettings(ref DEVMODE devMode, int flags);`n"
            f"[StructLayout(LayoutKind.Sequential)] public struct DEVMODE {{ [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName; public short dmSpecVersion; public short dmDriverVersion; public short dmSize; public short dmDriverExtra; public int dmFields; public int dmPositionX; public int dmPositionY; public int dmDisplayOrientation; public int dmDisplayFixedOutput; public short dmColor; public short dmDuplex; public short dmYResolution; public short dmTTOption; public short dmCollate; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName; public short dmLogPixels; public int dmBitsPerPel; public int dmPelsWidth; public int dmPelsHeight; public int dmDisplayFlags; public int dmDisplayFrequency; }}`n"
            f"'@`n"
            f"  Add-Type -MemberDefinition $signature -Name NativeMethods -Namespace Win32`n"
            f"  $dm = New-Object Win32.NativeMethods+DEVMODE`n"
            f"  $dm.dmSize = [System.Runtime.InteropServices.Marshal]::SizeOf($dm)`n"
            f"  [Win32.NativeMethods]::EnumDisplaySettings($null, -1, [ref]$dm) | Out-Null`n"
            f"  $dm.dmPelsWidth = {width}`n"
            f"  $dm.dmPelsHeight = {height}`n"
            f"  $dm.dmFields = 0x180000`n"
            f"  $result = [Win32.NativeMethods]::ChangeDisplaySettings([ref]$dm, 0)`n"
            f"  exit $result`n"
            f"}}"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if platform_name == "Windows" else 0
            )
            if result.returncode != 0:
                return jsonify({"error": f"ChangeDisplaySettings returned {result.returncode}", "stderr": result.stderr}), 500
            return jsonify({"message": f"Resolution set to {width}x{height}"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        # Linux/macOS: try xrandr
        try:
            result = subprocess.run(
                ["xrandr", "--fb", f"{width}x{height}"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return jsonify({"error": result.stderr or "xrandr failed"}), 500
            return jsonify({"message": f"Resolution set to {width}x{height}"})
        except FileNotFoundError:
            return jsonify({"error": "xrandr not available"}), 501
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/v1/actions/clipboard", methods=["GET"])
def action_get_clipboard():
    try:
        text = pyperclip.paste()
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/actions/clipboard", methods=["POST"])
def action_set_clipboard():
    data = request.get_json(silent=True) or {}
    # Accept both "text" (preferred) and "content" (legacy) fields
    text = data.get("text") if data.get("text") is not None else data.get("content")
    if text is None:
        return jsonify({"error": "text is required"}), 400
    try:
        pyperclip.copy(text)
        return jsonify({"message": "Clipboard set successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Command Routes  /api/v1/commands
# ---------------------------------------------------------------------------

@app.route("/api/v1/commands", methods=["POST"])
def exec_command():
    data = request.get_json(silent=True) or {}
    command = data.get("command")
    if not command:
        return jsonify({"error": "command is required"}), 400

    envs = data.get("envs")
    working_dir = data.get("workingDir")
    timeout_str = data.get("timeout", "30s")
    timeout_sec = _parse_timeout(timeout_str, 30.0)

    env = os.environ.copy()
    if envs and isinstance(envs, dict):
        env.update(envs)

    kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        text=True,
        timeout=timeout_sec,
        env=env,
        cwd=working_dir or None,
    )
    if platform_name == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(command, **kwargs)
        return jsonify({
            "exitCode": result.returncode,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"exitCode": 124, "returncode": 124, "stdout": "", "stderr": f"Command timed out after {timeout_sec}s"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# File System Routes  /api/v1/fs/*
# ---------------------------------------------------------------------------

@app.route("/api/v1/fs/list", methods=["GET"])
def fs_list():
    path = request.args.get("path")
    working_dir = request.args.get("workingDir")
    depth = int(request.args.get("depth", 1))

    if not path:
        return jsonify({"error": "path is required"}), 400

    resolved = _resolve_path(path, working_dir)
    if not os.path.exists(resolved):
        return jsonify({"error": "Directory not found"}), 404
    if not os.path.isdir(resolved):
        return jsonify({"error": "Path is not a directory"}), 400

    def _list_dir(dir_path: str, current_depth: int) -> list:
        entries = []
        try:
            for name in sorted(os.listdir(dir_path)):
                full = os.path.join(dir_path, name)
                info = _file_info(full)
                if os.path.isdir(full) and current_depth < depth:
                    info["children"] = _list_dir(full, current_depth + 1)
                entries.append(info)
        except PermissionError:
            pass
        return entries

    return jsonify(_list_dir(resolved, 1))


@app.route("/api/v1/fs/read", methods=["GET"])
def fs_read():
    path = request.args.get("path")
    working_dir = request.args.get("workingDir")
    if not path:
        return jsonify({"error": "path is required"}), 400
    resolved = _resolve_path(path, working_dir)
    if not os.path.exists(resolved):
        return jsonify({"error": "File not found"}), 404
    if os.path.isdir(resolved):
        return jsonify({"error": "Path is a directory"}), 405
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/fs/write", methods=["POST"])
def fs_write():
    if request.content_type and "multipart/form-data" in request.content_type:
        path = request.form.get("path")
        working_dir = request.form.get("workingDir")
        file_obj = request.files.get("content")
        text_content = request.form.get("content") if file_obj is None else None
        is_binary = file_obj is not None
    else:
        data = request.get_json(silent=True) or {}
        path = data.get("path")
        working_dir = data.get("workingDir")
        text_content = data.get("content")
        is_binary = False
        file_obj = None

    if not path:
        return jsonify({"error": "path is required"}), 400
    if text_content is None and file_obj is None:
        return jsonify({"error": "content is required"}), 400

    resolved = _resolve_path(path, working_dir)
    if os.path.isdir(resolved):
        return jsonify({"error": "Path is already a directory"}), 409

    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)

    try:
        if is_binary and file_obj:
            file_obj.save(resolved)
        else:
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(text_content)
        return jsonify(_file_info(resolved))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/fs", methods=["DELETE"])
def fs_delete():
    # Accept path from query params (preferred) or JSON body
    path = request.args.get("path")
    working_dir = request.args.get("workingDir")
    if path is None:
        data = request.get_json(silent=True) or {}
        path = data.get("path")
        working_dir = data.get("workingDir") or working_dir
    if not path:
        return jsonify({"error": "path is required"}), 400
    resolved = _resolve_path(path, working_dir)
    if not os.path.exists(resolved):
        return jsonify({"error": "File/dir not found"}), 404
    try:
        if os.path.isdir(resolved):
            shutil.rmtree(resolved)
        else:
            os.remove(resolved)
        return jsonify({"message": "File/Directory deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/fs/exists", methods=["POST"])
def fs_exists():
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    working_dir = data.get("workingDir")
    if not path:
        return jsonify({"error": "path is required"}), 400
    resolved = _resolve_path(path, working_dir)
    if not os.path.exists(resolved):
        return jsonify({"exists": False})
    fs_type = "directory" if os.path.isdir(resolved) else "file"
    return jsonify({"exists": True, "type": fs_type})


@app.route("/api/v1/fs/rename", methods=["POST"])
def fs_rename():
    data = request.get_json(silent=True) or {}
    old_path = data.get("oldPath")
    new_path = data.get("newPath")
    working_dir = data.get("workingDir")
    if not old_path or not new_path:
        return jsonify({"error": "oldPath and newPath are required"}), 400
    old_resolved = _resolve_path(old_path, working_dir)
    new_resolved = _resolve_path(new_path, working_dir)
    if not os.path.exists(old_resolved):
        return jsonify({"error": "Old path not found"}), 404
    if os.path.exists(new_resolved):
        return jsonify({"error": "New path already exists"}), 409
    try:
        os.rename(old_resolved, new_resolved)
        return jsonify(_file_info(new_resolved))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/fs/info", methods=["GET"])
def fs_get_info():
    path = request.args.get("path")
    working_dir = request.args.get("workingDir")
    if not path:
        return jsonify({"error": "path is required"}), 400
    resolved = _resolve_path(path, working_dir)
    if not os.path.exists(resolved):
        return jsonify({"error": "File/dir not found"}), 404
    return jsonify(_file_info(resolved))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "platform": platform_name, "commit": GIT_COMMIT})


def _wrap_wsgi_with_error_catch(wsgi_app):
    """Wrap WSGI app to catch all exceptions, print traceback, and return JSON 500."""
    def wrapper(environ, start_response):
        import sys
        print("[wsgi] request:", environ.get("PATH_INFO", ""), flush=True)
        sys.stderr.flush()
        try:
            result = wsgi_app(environ, start_response)
            # Wrap iterator so we catch exceptions raised during iteration (e.g. in view)
            def next_():
                try:
                    for chunk in result:
                        yield chunk
                except Exception as e:
                    traceback.print_exc()
                    body = json.dumps(
                        {"error": str(e), "traceback": traceback.format_exc()},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    start_response("500 Internal Server Error", [("Content-Type", "application/json; charset=utf-8")])
                    yield body
            return next_()
        except Exception as e:
            traceback.print_exc()
            body = json.dumps(
                {"error": str(e), "traceback": traceback.format_exc()},
                ensure_ascii=False,
            ).encode("utf-8")
            start_response("500 Internal Server Error", [("Content-Type", "application/json; charset=utf-8")])
            return [body]
    return wrapper


def _run_server():
    """Start the Flask server (blocking)."""
    import sys
    # When spawned as a --child process the console is hidden; write logs to file.
    log_dir = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "GBOXGUIServer")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "child.log")
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.FileHandler(log_file, encoding="utf-8")],
        )
    except Exception:
        pass
    sys.stderr.write(f"[GBOX] Loading {__file__}\n")
    sys.stderr.write("[GBOX] Installing WSGI error wrapper\n")
    sys.stderr.flush()
    app.wsgi_app = _wrap_wsgi_with_error_catch(app.wsgi_app)
    sys.stderr.write("[GBOX] Starting on 0.0.0.0:5789\n")
    sys.stderr.flush()
    logging.info("[GBOX] Starting Flask on 0.0.0.0:5789")
    app.run(host="0.0.0.0", port=5789, debug=False)



# ---------------------------------------------------------------------------
# Windows Service wrapper (pywin32)
# ---------------------------------------------------------------------------
# Architecture:
#   - The SCM service runs in Session 0 (no desktop).
#   - On start / user logon it calls WTSQueryUserToken + CreateProcessAsUser
#     to spawn a *hidden* child process of this exe (--child flag) inside the
#     active interactive desktop session (Session 1).
#   - The child runs the Flask server there, so pyautogui has full desktop
#     access and no console window is visible to the user.
# ---------------------------------------------------------------------------

def _setup_service_logging():
    """Configure file-based logging for the service (Session 0 has no console)."""
    log_dir = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "GBOXGUIServer")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "service.log")
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    return log_file


def _get_service_exe() -> str:
    """Return the absolute path to this executable.

    sys.argv[0] is unreliable in service context (SCM may pass service name);
    sys.executable is always the correct frozen exe path in PyInstaller bundles.
    """
    import sys as _sys
    exe = getattr(_sys, "executable", None) or _sys.argv[0]
    return os.path.abspath(exe)


def _find_active_user_session() -> int:
    """Find a desktop session with a logged-in user.

    Enumerates all WTS sessions and returns the first one that is Active
    and has a valid user token.  Falls back to WTSGetActiveConsoleSessionId
    if enumeration finds nothing.  Returns 0xFFFFFFFF if no session is usable.
    """
    import win32ts

    WTSActive = 0
    try:
        sessions = win32ts.WTSEnumerateSessions(win32ts.WTS_CURRENT_SERVER_HANDLE)
    except Exception:
        sid = win32ts.WTSGetActiveConsoleSessionId()
        return sid

    for session in sessions:
        sid = session["SessionId"]
        state = session["State"]
        if state != WTSActive or sid == 0:
            continue
        try:
            token = win32ts.WTSQueryUserToken(sid)
            token.Close()
            logging.info("[GBOXService] found active user session %d", sid)
            return sid
        except Exception:
            continue

    # Fallback
    return win32ts.WTSGetActiveConsoleSessionId()


def _spawn_in_user_session(session_id: int):
    """Launch a hidden child process in the given user desktop session.

    Returns (pid, process_handle).  Caller is responsible for closing the handle.
    Requires the service to run as LocalSystem (the default).
    """
    import win32con
    import win32process
    import win32profile
    import win32security
    import win32ts

    user_token = win32ts.WTSQueryUserToken(session_id)

    # pywin32 signature (differs from C API order!):
    #   DuplicateTokenEx(ExistingToken, ImpersonationLevel, DesiredAccess,
    #                    TokenType, TokenAttributes=None)
    dup_token = win32security.DuplicateTokenEx(
        user_token,
        win32security.SecurityImpersonation,
        win32con.MAXIMUM_ALLOWED,
        win32security.TokenPrimary,
    )

    try:
        env = win32profile.CreateEnvironmentBlock(dup_token, False)
    except Exception:
        env = None

    exe = _get_service_exe()
    cmd_line = f'"{exe}" --child'
    logging.info("[GBOXService] spawning child: %s", cmd_line)

    si = win32process.STARTUPINFO()
    si.dwFlags = win32con.STARTF_USESHOWWINDOW
    si.wShowWindow = win32con.SW_HIDE
    si.lpDesktop = "winsta0\\default"

    # pywin32 signature:
    #   CreateProcessAsUser(hToken, appName, commandLine, processAttributes,
    #                       threadAttributes, bInheritHandles, dwCreationFlags,
    #                       newEnvironment, currentDirectory, startupinfo)
    proc_h, thread_h, pid, _tid = win32process.CreateProcessAsUser(
        dup_token,            # hToken
        None,                 # appName
        cmd_line,             # commandLine
        None,                 # processAttributes (None = default)
        None,                 # threadAttributes  (None = default)
        False,                # bInheritHandles
        (win32con.CREATE_NO_WINDOW
         | win32con.NORMAL_PRIORITY_CLASS
         | win32con.CREATE_UNICODE_ENVIRONMENT),
        env,                  # newEnvironment
        None,                 # currentDirectory
        si,                   # startupinfo
    )
    thread_h.Close()
    return pid, proc_h


if platform.system() == "Windows":
    try:
        import threading
        import win32con
        import win32event
        import win32process
        import win32service
        import win32serviceutil
        import win32ts

        class GBOXService(win32serviceutil.ServiceFramework):
            _svc_name_ = "GBOXGUIServer"
            _svc_display_name_ = "GBOX GUI Server"
            _svc_description_ = (
                "GBOX Local API Server – provides UI automation, command execution "
                "and file system APIs on localhost:5789."
            )
            _svc_start_type_ = win32service.SERVICE_AUTO_START
            # Receive SESSION_CHANGE notifications so we can (re)spawn on logon
            _svc_controls_accepted_ = (
                win32service.SERVICE_ACCEPT_STOP
                | win32service.SERVICE_ACCEPT_SESSIONCHANGE
            )

            def __init__(self, args):
                win32serviceutil.ServiceFramework.__init__(self, args)
                self._stop_event = win32event.CreateEvent(None, 0, 0, None)
                self._child_handle = None
                self._child_pid = None
                self._lock = threading.Lock()

            # ------------------------------------------------------------------
            def SvcStop(self):
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
                self._kill_child()
                win32event.SetEvent(self._stop_event)

            # ------------------------------------------------------------------
            def SvcOtherEx(self, control, event_type, data):
                """Handle session-change events (logon / logoff / lock / unlock)."""
                if control == win32service.SERVICE_CONTROL_SESSIONCHANGE:
                    # WTS_SESSION_LOGON = 5, WTS_SESSION_UNLOCK = 8
                    if event_type in (win32con.WTS_SESSION_LOGON,
                                      win32con.WTS_SESSION_UNLOCK):
                        session_id = data[0] if data else None
                        self._ensure_child(session_id)
                    # WTS_SESSION_LOGOFF = 6, WTS_SESSION_LOCK = 7
                    elif event_type in (win32con.WTS_SESSION_LOGOFF,
                                        win32con.WTS_SESSION_LOCK):
                        self._kill_child()

            # ------------------------------------------------------------------
            def SvcDoRun(self):
                import servicemanager
                log_file = _setup_service_logging()
                logging.info("[GBOXService] service starting; log=%s", log_file)
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_INFORMATION_TYPE,
                    servicemanager.PYS_SERVICE_STARTED,
                    (self._svc_name_, ""),
                )
                # Attempt immediate spawn in case a user is already logged in
                active = _find_active_user_session()
                logging.info("[GBOXService] initial session lookup => %s", active)
                if active != 0xFFFFFFFF:
                    self._ensure_child(active)

                # Keep the service alive; a background watchdog revives the child
                # if it dies unexpectedly (e.g. crash).
                while True:
                    rc = win32event.WaitForSingleObject(self._stop_event, 10_000)
                    if rc == win32event.WAIT_OBJECT_0:
                        break
                    self._watchdog()

            # ------------------------------------------------------------------
            def _ensure_child(self, session_id=None):
                with self._lock:
                    if self._child_handle and self._is_child_alive():
                        return
                    if session_id is None:
                        session_id = _find_active_user_session()
                    if session_id == 0xFFFFFFFF:
                        logging.debug("[GBOXService] no active user session found")
                        return
                    try:
                        pid, handle = _spawn_in_user_session(session_id)
                        self._child_pid = pid
                        self._child_handle = handle
                        logging.info("[GBOXService] spawned child pid=%d in session %d",
                                     pid, session_id)
                    except Exception:
                        logging.exception("[GBOXService] failed to spawn child in session %d",
                                          session_id)

            def _kill_child(self):
                with self._lock:
                    if self._child_handle:
                        try:
                            win32process.TerminateProcess(self._child_handle, 0)
                        except Exception:
                            pass
                        self._child_handle = None
                        self._child_pid = None

            def _is_child_alive(self) -> bool:
                if not self._child_handle:
                    return False
                try:
                    rc = win32event.WaitForSingleObject(self._child_handle, 0)
                    return rc != win32event.WAIT_OBJECT_0  # OBJECT_0 means exited
                except Exception:
                    return False

            def _watchdog(self):
                need_respawn = False
                with self._lock:
                    if self._child_handle and not self._is_child_alive():
                        logging.warning("[GBOXService] child exited unexpectedly; restarting")
                        self._child_handle = None
                        self._child_pid = None
                    need_respawn = self._child_handle is None
                if need_respawn:
                    self._ensure_child()

    except ImportError:
        GBOXService = None  # pywin32 not available
else:
    GBOXService = None


if __name__ == "__main__":
    import sys

    if platform.system() == "Windows":
        if "--child" in sys.argv:
            # Spawned by the SCM service into the user desktop session.
            # Run the Flask server directly with no console window.
            _run_server()
        elif "--console" in sys.argv or "-c" in sys.argv:
            # Explicit foreground/debug mode requested by the user.
            _run_server()
        elif len(sys.argv) == 1:
            # No arguments: Windows SCM is starting the service process.
            # Register with SCM via StartServiceCtrlDispatcher; if we are not
            # being called from SCM (e.g. user double-clicks the exe) this raises
            # an exception and we fall back to plain console mode.
            if GBOXService is not None:
                try:
                    import servicemanager
                    servicemanager.Initialize()
                    servicemanager.PrepareToHostSingle(GBOXService)
                    servicemanager.StartServiceCtrlDispatcher()
                except Exception:
                    _run_server()
            else:
                _run_server()
        else:
            # Service management sub-commands: install, remove, start, stop, …
            if GBOXService is not None:
                # Inject --startup auto before the 'install' subcommand so the
                # service is registered as Automatic.  HandleCommandLine (getopt)
                # requires options to appear BEFORE the subcommand word.
                if "install" in sys.argv and "--startup" not in sys.argv:
                    idx = sys.argv.index("install")
                    sys.argv.insert(idx, "auto")
                    sys.argv.insert(idx, "--startup")
                win32serviceutil.HandleCommandLine(GBOXService)
            else:
                print("pywin32 is not installed; service management unavailable.")
                sys.exit(1)
    else:
        # Non-Windows: plain foreground process
        _run_server()
