#!/usr/bin/env python3
"""
VISCORA Remote Control Agent
============================
Runs on the computer of the person who SHARES their screen (User A).
The VISCORA web page forwards mouse/#!/usr/bin/env python3
"""
VISCORA Remote Control Agent
============================
Runs on the computer of the person who SHARES their screen (User A).
The VISCORA web page forwards mouse/keyboard events of the viewer (User B) to this
agent -- but only after User A clicked "Allow Control" in the meeting.

Install:   pip install pyautogui websockets
Run:       python viscora_agent.py
Then paste the printed TOKEN into the permission popup in VISCORA (first time only).

SAFETY
  * Listens on 127.0.0.1 only (never reachable from the network) and needs the token.
  * Does nothing until the page sends "start" (i.e. you pressed Allow Control).
  * EMERGENCY STOP: slam the physical mouse into the TOP-LEFT corner of the screen,
    or press the "Stop Control" button in VISCORA, or close this window.
  * All pressed keys / mouse buttons are released automatically when control stops.

Notes: share your ENTIRE SCREEN (not a window/tab). Controls the primary monitor unless
you pass --region X,Y,W,H. macOS: grant Accessibility permission to your terminal.
Linux: works on X11 (not Wayland).
"""
import argparse
import asyncio
import json
import platform
import secrets
import time

import pyautogui
import websockets

pyautogui.FAILSAFE = False   # we implement our own corner check below
pyautogui.PAUSE = 0
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# Browser KeyboardEvent.code  ->  pyautogui key name (physical key positions)
CODE_MAP = {
    "Space": "space", "Enter": "enter", "NumpadEnter": "enter", "Backspace": "backspace", "Tab": "tab",
    "Escape": "esc", "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
    "PageUp": "pageup", "PageDown": "pagedown", "CapsLock": "capslock", "ContextMenu": "apps",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "ShiftLeft": "shiftleft", "ShiftRight": "shiftright", "ControlLeft": "ctrlleft", "ControlRight": "ctrlright",
    "AltLeft": "altleft", "AltRight": "altright",
    "MetaLeft": "command" if IS_MAC else "winleft", "MetaRight": "command" if IS_MAC else "winright",
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]", "Backslash": "\\",
    "Semicolon": ";", "Quote": "'", "Comma": ",", "Period": ".", "Slash": "/", "Backquote": "`",
}
BUTTONS = {0: "left", 1: "middle", 2: "right"}


def to_key(code: str):
    if code in CODE_MAP:
        key = CODE_MAP[code]
    elif code.startswith("Key") and len(code) == 4:
        key = code[3].lower()
    elif code.startswith("Digit") and len(code) == 6:
        key = code[5]
    elif code.startswith("Numpad") and len(code) == 7 and code[6].isdigit():
        key = code[6]
    elif code.startswith("F") and code[1:].isdigit():
        key = code.lower()
    else:
        return None
    return key if key in pyautogui.KEYBOARD_KEYS else None


class State:
    def __init__(self, region):
        self.enabled = False
        self.keys = set()
        self.buttons = set()
        self.rem = [0.0, 0.0]
        self.ox, self.oy, self.w, self.h = region

    def release_all(self):
        for k in list(self.keys):
            try:
                pyautogui.keyUp(k, _pause=False)
            except Exception:
                pass
        for b in list(self.buttons):
            try:
                pyautogui.mouseUp(button=b, _pause=False)
            except Exception:
                pass
        self.keys.clear()
        self.buttons.clear()
        self.rem = [0.0, 0.0]

    def set_enabled(self, on: bool):
        if self.enabled and not on:
            self.release_all()
        if on != self.enabled:
            print(f"[{time.strftime('%H:%M:%S')}] remote control {'STARTED' if on else 'STOPPED'}")
        self.enabled = on

    def scroll(self, dx, dy):
        for axis, val in ((0, dy), (1, dx)):
            v = self.rem[axis] + (-val if IS_WIN else -val / 40.0)
            n = int(v)
            self.rem[axis] = v - n
            if n:
                try:
                    (pyautogui.scroll if axis == 0 else pyautogui.hscroll)(n, _pause=False)
                except Exception:
                    pass

    def handle(self, m) -> bool:
        """Apply one input event. Returns False if the emergency corner stop fired."""
        if not self.enabled:
            return True
        if tuple(pyautogui.position()) == (0, 0):  # user slammed the mouse into the top-left corner
            self.set_enabled(False)
            return False
        kind = m.get("kind")
        if kind == "move":
            x = self.ox + min(max(int(m["x"] * (self.w - 1)), 1), self.w - 2)  # never reaches the stop corner
            y = self.oy + min(max(int(m["y"] * (self.h - 1)), 1), self.h - 2)
            pyautogui.moveTo(x, y, _pause=False)
        elif kind == "down":
            b = BUTTONS.get(int(m["button"]))
            if b:
                pyautogui.mouseDown(button=b, _pause=False)
                self.buttons.add(b)
        elif kind == "up":
            b = BUTTONS.get(int(m["button"]))
            if b:
                pyautogui.mouseUp(button=b, _pause=False)
                self.buttons.discard(b)
        elif kind == "wheel":
            self.scroll(float(m.get("dx", 0)), float(m.get("dy", 0)))
        elif kind == "key":
            key = to_key(str(m.get("code", "")))
            if key:
                if m.get("down"):
                    pyautogui.keyDown(key, _pause=False)
                    self.keys.add(key)
                else:
                    pyautogui.keyUp(key, _pause=False)
                    self.keys.discard(key)
        elif kind == "release":
            self.release_all()
        return True


def origin_of(ws):
    try:
        return ws.request.headers.get("Origin")
    except AttributeError:
        return ws.request_headers.get("Origin")


async def main():
    ap = argparse.ArgumentParser(description="VISCORA Remote Control Agent")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=None, help="fixed token (default: random each start)")
    ap.add_argument("--origin", action="append", help="only accept this page origin (repeatable), e.g. https://my.site")
    ap.add_argument("--region", default=None, help="X,Y,W,H of the screen area that is shared (default: primary monitor)")
    args = ap.parse_args()

    token = args.token or secrets.token_urlsafe(6)
    if args.region:
        region = tuple(int(v) for v in args.region.split(","))
    else:
        w, h = pyautogui.size()
        region = (0, 0, w, h)
    st = State(region)
    busy = {"ws": None}

    async def handler(ws, path=None):
        if args.origin and origin_of(ws) not in args.origin:
            await ws.close(1008, "origin not allowed")
            return
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except Exception:
            return
        if hello.get("t") != "hello" or not secrets.compare_digest(str(hello.get("token", "")), token):
            await ws.send(json.dumps({"t": "denied"}))
            return
        if busy["ws"] is not None:
            await ws.send(json.dumps({"t": "denied"}))
            return
        busy["ws"] = ws
        print(f"[{time.strftime('%H:%M:%S')}] VISCORA page connected")
        await ws.send(json.dumps({"t": "ready", "w": region[2], "h": region[3]}))
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                    t = m.get("t")
                    if t == "start":
                        st.set_enabled(True)
                    elif t == "stop":
                        st.set_enabled(False)
                    elif t == "input":
                        if not st.handle(m):
                            await ws.send(json.dumps({"t": "stopped"}))
                except Exception as e:  # bad message: ignore, keep running
                    print("ignored bad message:", e)
        finally:
            st.set_enabled(False)
            busy["ws"] = None
            print(f"[{time.strftime('%H:%M:%S')}] VISCORA page disconnected")

    async with websockets.serve(handler, "127.0.0.1", args.port, max_size=4096):
        print("=" * 56)
        print(" VISCORA Remote Control Agent is running")
        print(f" TOKEN : {token}")
        print(f" Screen: {region[2]}x{region[3]}  (listening on 127.0.0.1:{args.port})")
        print(" Emergency stop: move your mouse to the TOP-LEFT corner")
        print("=" * 56)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("bye")
keyboard events of the viewer (User B) to this
agent -- but only after User A clicked "Allow Control" in the meeting.

Install:   pip install pyautogui websockets
Run:       python viscora_agent.py
Then paste the printed TOKEN into the permission popup in VISCORA (first time only).

SAFETY
  * Listens on 127.0.0.1 only (never reachable from the network) and needs the token.
  * Does nothing until the page sends "start" (i.e. you pressed Allow Control).
  * EMERGENCY STOP: slam the physical mouse into the TOP-LEFT corner of the screen,
    or press the "Stop Control" button in VISCORA, or close this window.
  * All pressed keys / mouse buttons are released automatically when control stops.

Notes: share your ENTIRE SCREEN (not a window/tab). Controls the primary monitor unless
you pass --region X,Y,W,H. macOS: grant Accessibility permission to your terminal.
Linux: works on X11 (not Wayland).
"""
import argparse
import asyncio
import json
import platform
import secrets
import time

import pyautogui
import websockets

pyautogui.FAILSAFE = False   # we implement our own corner check below
pyautogui.PAUSE = 0
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# Browser KeyboardEvent.code  ->  pyautogui key name (physical key positions)
CODE_MAP = {
    "Space": "space", "Enter": "enter", "NumpadEnter": "enter", "Backspace": "backspace", "Tab": "tab",
    "Escape": "esc", "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
    "PageUp": "pageup", "PageDown": "pagedown", "CapsLock": "capslock", "ContextMenu": "apps",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "ShiftLeft": "shiftleft", "ShiftRight": "shiftright", "ControlLeft": "ctrlleft", "ControlRight": "ctrlright",
    "AltLeft": "altleft", "AltRight": "altright",
    "MetaLeft": "command" if IS_MAC else "winleft", "MetaRight": "command" if IS_MAC else "winright",
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]", "Backslash": "\\",
    "Semicolon": ";", "Quote": "'", "Comma": ",", "Period": ".", "Slash": "/", "Backquote": "`",
}
BUTTONS = {0: "left", 1: "middle", 2: "right"}


def to_key(code: str):
    if code in CODE_MAP:
        key = CODE_MAP[code]
    elif code.startswith("Key") and len(code) == 4:
        key = code[3].lower()
    elif code.startswith("Digit") and len(code) == 6:
        key = code[5]
    elif code.startswith("Numpad") and len(code) == 7 and code[6].isdigit():
        key = code[6]
    elif code.startswith("F") and code[1:].isdigit():
        key = code.lower()
    else:
        return None
    return key if key in pyautogui.KEYBOARD_KEYS else None


class State:
    def __init__(self, region):
        self.enabled = False
        self.keys = set()
        self.buttons = set()
        self.rem = [0.0, 0.0]
        self.ox, self.oy, self.w, self.h = region

    def release_all(self):
        for k in list(self.keys):
            try:
                pyautogui.keyUp(k, _pause=False)
            except Exception:
                pass
        for b in list(self.buttons):
            try:
                pyautogui.mouseUp(button=b, _pause=False)
            except Exception:
                pass
        self.keys.clear()
        self.buttons.clear()
        self.rem = [0.0, 0.0]

    def set_enabled(self, on: bool):
        if self.enabled and not on:
            self.release_all()
        if on != self.enabled:
            print(f"[{time.strftime('%H:%M:%S')}] remote control {'STARTED' if on else 'STOPPED'}")
        self.enabled = on

    def scroll(self, dx, dy):
        for axis, val in ((0, dy), (1, dx)):
            v = self.rem[axis] + (-val if IS_WIN else -val / 40.0)
            n = int(v)
            self.rem[axis] = v - n
            if n:
                try:
                    (pyautogui.scroll if axis == 0 else pyautogui.hscroll)(n, _pause=False)
                except Exception:
                    pass

    def handle(self, m) -> bool:
        """Apply one input event. Returns False if the emergency corner stop fired."""
        if not self.enabled:
            return True
        if tuple(pyautogui.position()) == (0, 0):  # user slammed the mouse into the top-left corner
            self.set_enabled(False)
            return False
        kind = m.get("kind")
        if kind == "move":
            x = self.ox + min(max(int(m["x"] * (self.w - 1)), 1), self.w - 2)  # never reaches the stop corner
            y = self.oy + min(max(int(m["y"] * (self.h - 1)), 1), self.h - 2)
            pyautogui.moveTo(x, y, _pause=False)
        elif kind == "down":
            b = BUTTONS.get(int(m["button"]))
            if b:
                pyautogui.mouseDown(button=b, _pause=False)
                self.buttons.add(b)
        elif kind == "up":
            b = BUTTONS.get(int(m["button"]))
            if b:
                pyautogui.mouseUp(button=b, _pause=False)
                self.buttons.discard(b)
        elif kind == "wheel":
            self.scroll(float(m.get("dx", 0)), float(m.get("dy", 0)))
        elif kind == "key":
            key = to_key(str(m.get("code", "")))
            if key:
                if m.get("down"):
                    pyautogui.keyDown(key, _pause=False)
                    self.keys.add(key)
                else:
                    pyautogui.keyUp(key, _pause=False)
                    self.keys.discard(key)
        elif kind == "release":
            self.release_all()
        return True


def origin_of(ws):
    try:
        return ws.request.headers.get("Origin")
    except AttributeError:
        return ws.request_headers.get("Origin")


async def main():
    ap = argparse.ArgumentParser(description="VISCORA Remote Control Agent")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=None, help="fixed token (default: random each start)")
    ap.add_argument("--origin", action="append", help="only accept this page origin (repeatable), e.g. https://my.site")
    ap.add_argument("--region", default=None, help="X,Y,W,H of the screen area that is shared (default: primary monitor)")
    args = ap.parse_args()

    token = args.token or secrets.token_urlsafe(6)
    if args.region:
        region = tuple(int(v) for v in args.region.split(","))
    else:
        w, h = pyautogui.size()
        region = (0, 0, w, h)
    st = State(region)
    busy = {"ws": None}

    async def handler(ws, path=None):
        if args.origin and origin_of(ws) not in args.origin:
            await ws.close(1008, "origin not allowed")
            return
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except Exception:
            return
        if hello.get("t") != "hello" or not secrets.compare_digest(str(hello.get("token", "")), token):
            await ws.send(json.dumps({"t": "denied"}))
            return
        if busy["ws"] is not None:
            await ws.send(json.dumps({"t": "denied"}))
            return
        busy["ws"] = ws
        print(f"[{time.strftime('%H:%M:%S')}] VISCORA page connected")
        await ws.send(json.dumps({"t": "ready", "w": region[2], "h": region[3]}))
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                    t = m.get("t")
                    if t == "start":
                        st.set_enabled(True)
                    elif t == "stop":
                        st.set_enabled(False)
                    elif t == "input":
                        if not st.handle(m):
                            await ws.send(json.dumps({"t": "stopped"}))
                except Exception as e:  # bad message: ignore, keep running
                    print("ignored bad message:", e)
        finally:
            st.set_enabled(False)
            busy["ws"] = None
            print(f"[{time.strftime('%H:%M:%S')}] VISCORA page disconnected")

    async with websockets.serve(handler, "127.0.0.1", args.port, max_size=4096):
        print("=" * 56)
        print(" VISCORA Remote Control Agent is running")
        print(f" TOKEN : {token}")
        print(f" Screen: {region[2]}x{region[3]}  (listening on 127.0.0.1:{args.port})")
        print(" Emergency stop: move your mouse to the TOP-LEFT corner")
        print("=" * 56)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("bye")
