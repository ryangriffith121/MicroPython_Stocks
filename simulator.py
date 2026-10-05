#!/usr/bin/env python3
"""
mpy-stocks OLED simulator
=========================
Runs your *unmodified* MicroPython `stocks.py` (plus your real `ssd1306.py`) on the
desktop and shows the 128x64 SSD1306 OLED in a window.

How it works
  * Stand-ins for the MicroPython-only modules (machine, micropython, framebuf, network,
    urequests, ujson, ntptime, and time.ticks_ms()/sleep_ms()) are injected before the
    firmware is imported.
  * `framebuf` is a pure-Python port of MicroPython's MONO_VLSB framebuffer (same 8x8 font,
    same Bresenham line, same clipping), so your ssd1306.py driver runs as-is.
  * The fake I2C bus feeds a small SSD1306 controller model (command/data stream -> GDDRAM)
    and the window just draws whatever is in that display RAM.

Usage
  python simulator.py                  # live Yahoo data (falls back to made-up data if unreachable)
  python simulator.py --offline        # always use made-up data
  python simulator.py --speed 10       # 10x clock: loading, rotation and title scroll run 10x faster
  python simulator.py --dir path/to/MicroPython_Stocks

Needs Python 3.12+ (stocks.py nests same-type quotes inside f-strings, which CPython only
accepts from 3.12; MicroPython is fine with it) and tkinter (bundled with python.org installers).
Keys: Esc or q closes the window.
"""

import argparse
import gc
import json
import math
import os
import random
import runpy
import sys
import threading
import time
import traceback
import types
import urllib.parse
import urllib.request

OLED_W, OLED_H = 128, 64
I2C_ADDRS = (0x3C, 0x3D)

# 8x8 font used by MicroPython's framebuf.text() (extmod/font_petme128_8x8.h,
# MIT licence, (c) Damien P. George). ASCII 32..127, 8 bytes per glyph,
# one byte per pixel column, LSB = top pixel.
FONT_8X8 = bytes.fromhex("""
00000000000000000000004f4f0000000007070000070700147f7f14147f7f14
00242e6b6b3a1200006333180c66630000327f4d4d7772500000000406030100
00001c3e63410000000041633e1c0000082a3e1c1c3e2a080008083e3e080800
000080e0600000000008080808080800000000606000000000406030180c0602
003e7f49457f3e000040447f7f40400000627351494f460000226349497f3600
00181814167f7f1000276745457d3900003e7f49497b3200000303797d070300
00367f49497f360000266f49497f3e000000002424000000000080e464000000
00081c3663414100001414141414140000414163361c080000020351590f0600
003e7f414d4f2e00007c7e0b0b7e7c00007f7f49497f3600003e7f4141632200
007f7f41633e1c00007f7f4949414100007f7f0909010100003e7f41497b3a00
007f7f08087f7f000000417f7f410000002060417f3f0100007f7f1c36634100
007f7f4040404000007f7f060c067f7f007f7f0e1c7f7f00003e7f41417f3e00
007f7f09090f0600001e3f21617f5e00007f7f19396f460000266f49497b3200
0001017f7f010100003f7f40407f3f00001f3f60603f1f00007f7f3018307f7f
0063771c1c77630000070f78780f0700006171594d47430000007f7f41410000
0002060c18306040000041417f7f000000080c06060c0800c0c0c0c0c0c0c0c0
000001030604000000207454547c7800007f7f44447c380000387c44446c2800
00387c44447f7f0000387c54545c580000087e7f090302000098bca4a4fc7c00
007f7f04047c78000000007d7d0000000040c08080fd7d00007f7f30386c4400
0000417f7f400000007c7c1830187c7c007c7c04047c780000387c44447c3800
00fcfc24243c180000183c2424fcfc00007c7c04040c080000485c5454742000
04043f7f44642000003c7c40407c3c00001c3c60603c1c00001c7c3018307c1c
00446c38386c4400009cbca0a0fc7c00004464745c4c44000008083e77414100
000000ffff000000004141773e0808000002030103020301aa55aa55aa55aa55
""")
assert len(FONT_8X8) == 96 * 8

MONO_VLSB = 0


def log(msg):
    print("[sim] " + msg)


# --------------------------------------------------------------------------------------
# Clock: MicroPython-style ticks since "boot", optionally sped up
# --------------------------------------------------------------------------------------
class VirtualClock:
    def __init__(self, speed=1.0):
        self.speed = max(float(speed), 0.01)
        self._t0 = time.monotonic()

    def seconds(self):
        return (time.monotonic() - self._t0) * self.speed

    def install(self):
        clk = self
        time.ticks_ms = lambda: int(clk.seconds() * 1000)
        time.ticks_us = lambda: int(clk.seconds() * 1000000)
        time.ticks_diff = lambda a, b: a - b
        time.ticks_add = lambda a, b: a + b
        time.sleep_ms = lambda ms: time.sleep(ms / 1000.0 / clk.speed)
        time.sleep_us = lambda us: time.sleep(us / 1000000.0 / clk.speed)


# --------------------------------------------------------------------------------------
# framebuf: port of MicroPython's FrameBuffer (MONO_VLSB only)
# --------------------------------------------------------------------------------------
class FrameBuffer:
    def __init__(self, buf, width, height, format, stride=None):
        if format != MONO_VLSB:
            raise NotImplementedError("simulator framebuf only implements MONO_VLSB (what ssd1306 uses)")
        self._fb = buf
        self._fw = width
        self._fh = height
        self._stride = stride or width

    def _set(self, x, y, c):
        if 0 <= x < self._fw and 0 <= y < self._fh:
            i = (y >> 3) * self._stride + x
            bit = 1 << (y & 7)
            if c:
                self._fb[i] |= bit
            else:
                self._fb[i] &= ~bit & 0xFF

    def fill(self, c):
        self._fb[:] = (b"\xff" if c else b"\x00") * len(self._fb)

    def pixel(self, x, y, c=None):
        if c is None:
            if 0 <= x < self._fw and 0 <= y < self._fh:
                return (self._fb[(y >> 3) * self._stride + x] >> (y & 7)) & 1
            return None
        self._set(x, y, c)

    def fill_rect(self, x, y, w, h, c):
        if h < 1 or w < 1 or x + w <= 0 or y + h <= 0 or y >= self._fh or x >= self._fw:
            return
        xend = min(self._fw, x + w)
        yend = min(self._fh, y + h)
        x = max(x, 0)
        y = max(y, 0)
        for yy in range(y, yend):
            for xx in range(x, xend):
                self._set(xx, yy, c)

    def hline(self, x, y, w, c):
        self.fill_rect(x, y, w, 1, c)

    def vline(self, x, y, h, c):
        self.fill_rect(x, y, 1, h, c)

    def rect(self, x, y, w, h, c, f=False):
        if f:
            self.fill_rect(x, y, w, h, c)
            return
        self.fill_rect(x, y, w, 1, c)
        self.fill_rect(x, y + h - 1, w, 1, c)
        self.fill_rect(x, y, 1, h, c)
        self.fill_rect(x + w - 1, y, 1, h, c)

    def line(self, x1, y1, x2, y2, c):
        # Bresenham, same structure as MicroPython's framebuf.c
        dx = x2 - x1
        if dx > 0:
            sx = 1
        else:
            dx = -dx
            sx = -1
        dy = y2 - y1
        if dy > 0:
            sy = 1
        else:
            dy = -dy
            sy = -1
        steep = dy > dx
        if steep:
            x1, y1 = y1, x1
            dx, dy = dy, dx
            sx, sy = sy, sx
        e = 2 * dy - dx
        for _ in range(dx):
            if steep:
                self._set(y1, x1, c)
            else:
                self._set(x1, y1, c)
            while e >= 0:
                y1 += sy
                e -= 2 * dx
            x1 += sx
            e += 2 * dy
        self._set(x2, y2, c)

    def text(self, s, x, y, c=1):
        for ch in s:
            code = ord(ch)
            if code < 32 or code > 127:
                code = 127
            base = (code - 32) * 8
            for j in range(8):
                xx = x + j
                if 0 <= xx < self._fw:
                    bits = FONT_8X8[base + j]
                    yy = y
                    while bits:
                        if bits & 1:
                            self._set(xx, yy, c)
                        bits >>= 1
                        yy += 1
            x += 8

    def scroll(self, xstep, ystep):
        w, h = self._fw, self._fh
        old = [[self.pixel(x, y) for x in range(w)] for y in range(h)]
        for y in range(h):
            for x in range(w):
                sx, sy = x - xstep, y - ystep
                if 0 <= sx < w and 0 <= sy < h:
                    self._set(x, y, old[sy][sx])


# --------------------------------------------------------------------------------------
# SSD1306 controller model + fake I2C bus
# --------------------------------------------------------------------------------------
class SSD1306Panel:
    ONE_ARG = {0x81, 0x20, 0xA8, 0xD3, 0xDA, 0xD5, 0xD9, 0xDB, 0x8D}
    TWO_ARG = {0x21, 0x22}

    def __init__(self, width=OLED_W, height=OLED_H, fps_cap=30.0):
        self.width, self.height = width, height
        self.pages = height // 8
        self.ram = bytearray(width * self.pages)
        self.on = False
        self.invert = False
        self.contrast = 0x7F
        self.col_lo, self.col_hi = 0, width - 1
        self.pg_lo, self.pg_hi = 0, self.pages - 1
        self.col, self.pg = 0, 0
        self._cmd = None
        self._args = []
        self.frames = 0
        self.fps_cap = fps_cap
        self._next_frame = 0.0
        self._publish()

    def _publish(self):
        # the GUI thread only ever reads this one tuple (atomic assignment)
        self.frame = (bytes(self.ram), self.on, self.invert, self.contrast)

    def write_command(self, byte):
        if self._cmd is not None:
            self._args.append(byte)
            need = 2 if self._cmd in self.TWO_ARG else 1
            if len(self._args) < need:
                return
            cmd, args = self._cmd, self._args
            self._cmd, self._args = None, []
            if cmd == 0x81:
                self.contrast = args[0]
            elif cmd == 0x21 and args[0] < self.width:
                self.col_lo, self.col_hi = args[0], min(args[1], self.width - 1)
                self.col = self.col_lo
            elif cmd == 0x22 and args[0] < self.pages:
                self.pg_lo, self.pg_hi = args[0], min(args[1], self.pages - 1)
                self.pg = self.pg_lo
            # timing, mux, charge pump, ... have no visible effect in the simulator
        elif byte in self.ONE_ARG or byte in self.TWO_ARG:
            self._cmd, self._args = byte, []
            return
        elif byte == 0xAE:
            self.on = False
        elif byte == 0xAF:
            self.on = True
        elif byte == 0xA6:
            self.invert = False
        elif byte == 0xA7:
            self.invert = True
        self._publish()

    def write_data(self, data):
        ram, w = self.ram, self.width
        for b in data:
            ram[self.pg * w + self.col] = b
            self.col += 1
            if self.col > self.col_hi:
                self.col = self.col_lo
                self.pg += 1
                if self.pg > self.pg_hi:
                    self.pg = self.pg_lo
        self._publish()
        self.frames += 1
        self._throttle()

    def _throttle(self):
        # A real SoftI2C transfer of 1 KB takes tens of ms; this also keeps the
        # firmware's unthrottled `while True:` loop from pegging a CPU core.
        if not self.fps_cap:
            return
        now = time.monotonic()
        wait = self._next_frame - now
        if wait > 0:
            time.sleep(wait)
        self._next_frame = max(now, self._next_frame) + 1.0 / self.fps_cap


class Pin:
    IN, OUT, OPEN_DRAIN = 0, 1, 2
    PULL_UP, PULL_DOWN = 1, 2

    def __init__(self, id, mode=-1, pull=-1, *, value=None, **kw):
        self.id = id
        self._v = value or 0

    def init(self, *a, **kw):
        pass

    def value(self, v=None):
        if v is None:
            return self._v
        self._v = v

    __call__ = value

    def on(self):
        self._v = 1

    def off(self):
        self._v = 0


class FakeI2C:
    panel = None  # set by install_mocks()

    def __init__(self, *args, **kwargs):
        pass

    def scan(self):
        return [0x3C]

    def _send(self, addr, data):
        if addr not in I2C_ADDRS:
            raise OSError(19, "ENODEV")  # what a missing device looks like on hardware
        if not data:
            return
        ctrl, payload = data[0], data[1:]
        if ctrl & 0x40:  # D/C# = 1 -> display data
            self.panel.write_data(payload)
        else:  # D/C# = 0 -> command byte(s)
            for b in payload:
                self.panel.write_command(b)

    def writeto(self, addr, buf, stop=True):
        data = bytes(buf)
        self._send(addr, data)
        return len(data)

    def writevto(self, addr, vector, stop=True):
        data = b"".join(bytes(v) for v in vector)
        self._send(addr, data)
        return len(data)


# --------------------------------------------------------------------------------------
# network / urequests: simulated Wi-Fi + Yahoo chart data (live or made-up)
# --------------------------------------------------------------------------------------
class FakeWLAN:
    PM_NONE, PM_PERFORMANCE, PM_POWERSAVE = 0, 1, 2
    clock = None

    def __init__(self, interface_id=0):
        self._active = False
        self._t = None

    def active(self, flag=None):
        if flag is None:
            return self._active
        self._active = bool(flag)

    def config(self, *args, **kwargs):
        return None

    def scan(self):
        time.sleep_ms(300)
        return [
            (b"HomeNet", b"\x00" * 6, 6, -48, 3, False),
            (b"Neighbour-5G", b"\x00" * 6, 11, -71, 3, False),
            (b"CoffeeShop", b"\x00" * 6, 1, -83, 0, False),
        ]

    def connect(self, ssid=None, key=None, *args, **kwargs):
        self._t = time.ticks_ms()

    def disconnect(self):
        self._t = None

    def isconnected(self):
        return self._t is not None and time.ticks_diff(time.ticks_ms(), self._t) >= 1000

    def status(self):
        return 1010 if self.isconnected() else 1001

    def ifconfig(self, *a):
        return ("192.168.1.42", "255.255.255.0", "192.168.1.1", "8.8.8.8")


class Response:
    def __init__(self, body, status=200):
        self.text = body
        self.content = body.encode("utf-8")
        self.status_code = status

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass


# Placeholder numbers, only used when live data can't be fetched (or with --offline).
_BASE_PRICE = {"^DJI": 46000.0, "NVDA": 222.27, "TSM": 285.0, "GOOG": 245.0,
               "MSFT": 520.0, "AMZN": 235.0, "AVGO": 340.0}
# (range, interval) -> (number of bars, per-bar volatility)
_SERIES = {("1d", "5m"): (78, 0.0009), ("1mo", "1d"): (21, 0.010), ("ytd", "1wk"): (40, 0.028)}


def synthetic_chart(symbol, range_, interval):
    n, vol = _SERIES.get((range_, interval), (60, 0.010))
    rnd = random.Random("%s|%s|%s" % (symbol, range_, interval))
    price = _BASE_PRICE.get(symbol, 100.0 + rnd.random() * 400.0)
    walk, x = [], 0.0
    for _ in range(n):
        x += rnd.gauss(0.0, vol)
        walk.append(x)
    closes = [round(price * math.exp(w - walk[-1]), 2) for w in walk]  # last bar == current price
    prev_close = closes[0] * (1.0 + rnd.uniform(-0.004, 0.004))
    closes_json = list(closes)
    closes_json[n // 3] = None  # real feeds have gaps; the firmware filters them out
    return json.dumps({"chart": {"result": [{
        "meta": {"symbol": symbol, "regularMarketPrice": closes[-1],
                 "chartPreviousClose": round(prev_close, 2)},
        "indicators": {"quote": [{"close": closes_json}]},
    }], "error": None}})


class DataFeed:
    def __init__(self, live=True):
        self.live = live
        self.n_live = 0
        self.n_sim = 0
        self._fails = 0

    def summary(self):
        if self.n_live == 0 and self.n_sim == 0:
            return "none yet"
        return "live %d / simulated %d" % (self.n_live, self.n_sim)

    def get(self, url, headers=None, **kw):
        marker = "finance.yahoo.com/v8/finance/chart/"
        if marker not in url:  # not the Yahoo chart API: just try the real network
            if not self.live:
                raise OSError(113, "EHOSTUNREACH (offline simulator)")
            return self._http(url, headers)
        parsed = urllib.parse.urlparse(url)
        symbol = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        qs = urllib.parse.parse_qs(parsed.query)
        range_ = qs.get("range", ["1d"])[0]
        interval = qs.get("interval", ["5m"])[0]
        if self.live:
            try:
                body = self._http(url, headers).text
                d = json.loads(body)
                d["chart"]["result"][0]["meta"]["regularMarketPrice"]  # sanity check
                self.n_live += 1
                self._fails = 0
                return Response(body)
            except Exception as e:
                self._fails += 1
                log("live fetch of %s %s/%s failed (%s: %s); using made-up data"
                    % (symbol, range_, interval, type(e).__name__, e))
                if self._fails >= 3:
                    log("3 live failures in a row - switching to made-up data for the rest of this run")
                    self.live = False
        self.n_sim += 1
        return Response(synthetic_chart(symbol, range_, interval))

    def _http(self, url, headers):
        req = urllib.request.Request(url.replace("^", "%5E"), headers=headers or {})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return Response(resp.read().decode("utf-8"), resp.status)


# --------------------------------------------------------------------------------------
# Install the stand-in modules
# --------------------------------------------------------------------------------------
def install_mocks(speed=1.0, offline=False, fps=30.0):
    clock = VirtualClock(speed)
    clock.install()
    panel = SSD1306Panel(fps_cap=fps)
    feed = DataFeed(live=not offline)
    FakeI2C.panel = panel
    FakeWLAN.clock = clock

    def module(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    def machine_reset():
        raise SystemExit("machine.reset() called")

    module("machine", Pin=Pin, SoftI2C=FakeI2C, I2C=FakeI2C, reset=machine_reset,
           freq=lambda *a: 240000000)
    module("micropython", const=lambda x: x)
    module("framebuf", FrameBuffer=FrameBuffer, MONO_VLSB=0, MVLSB=0, RGB565=1,
           GS4_HMSB=2, MONO_HLSB=3, MONO_HMSB=4, GS2_HMSB=5, GS8=6)
    module("network", WLAN=FakeWLAN, STA_IF=0, AP_IF=1)
    module("urequests", get=feed.get)
    module("ntptime", settime=lambda *a, **k: None)
    sys.modules["ujson"] = json
    sys.modules["utime"] = time
    if not hasattr(gc, "mem_free"):
        gc.mem_free = lambda: 100000
    return clock, panel, feed


class Firmware(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.error = None

    def run(self):
        try:
            runpy.run_path(self.path, run_name="__main__")
        except SystemExit as e:
            self.error = "firmware exited: %s" % (e,)
        except BaseException:
            self.error = traceback.format_exc()
            print(self.error, file=sys.stderr)


# --------------------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------------------
# (top colour, bottom colour, first row of the bottom colour); the common yellow/blue
# SSD1306 modules have yellow rows 0-15 and blue rows 16-63.
SCHEMES = {
    "twotone": ("#f5e94a", "#b4d9ff", 16),
    "white": ("#e9eef7", "#e9eef7", 0),
    "blue": ("#9fd0ff", "#9fd0ff", 0),
    "yellow": ("#f5e94a", "#f5e94a", 0),
    "green": ("#7dffa0", "#7dffa0", 0),
}
OFF_COLOR = "#0b0c10"


def _scale_color(hex_color, f):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


class OledWindow:
    def __init__(self, panel, firmware, feed, clock, scale=6, scheme="twotone"):
        try:
            import tkinter as tk
        except ImportError:
            sys.exit("tkinter isn't available. Windows/macOS: install Python from python.org (it bundles Tk). "
                     "Debian/Ubuntu: sudo apt install python3-tk")
        self.tk = tk
        self.panel, self.fw, self.feed, self.clock = panel, firmware, feed, clock
        self.top, self.bottom, self.split = SCHEMES[scheme]
        margin = 16
        self.root = tk.Tk()
        self.root.title("mpy-stocks - SSD1306 128x64 simulator")
        self.root.configure(bg="#111114")
        self.root.resizable(False, False)
        self.cv = tk.Canvas(self.root, width=OLED_W * scale + 2 * margin,
                            height=OLED_H * scale + 2 * margin, bg="#000000", highlightthickness=0)
        self.cv.pack(padx=10, pady=(10, 4))
        dot = max(1, scale - 1)
        self.items = []
        for y in range(OLED_H):
            for x in range(OLED_W):
                x0 = margin + x * scale
                y0 = margin + y * scale
                self.items.append(self.cv.create_rectangle(x0, y0, x0 + dot, y0 + dot,
                                                           fill=OFF_COLOR, width=0))
        self.status = tk.Label(self.root, text="", bg="#111114", fg="#8a8f98",
                               font=("Courier", 10), anchor="w", justify="left")
        self.status.pack(fill="x", padx=12, pady=(0, 8))
        self._ram = bytes(len(panel.ram))
        self._mode = None
        self._colors = (OFF_COLOR, OFF_COLOR)
        self._shown = None
        self._fps = 0.0
        self._fps_t = time.monotonic()
        self._fps_n = 0
        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.root.bind("q", lambda e: self.root.destroy())

    def _redraw(self, frame):
        ram, on, inv, contrast = frame
        mode = (on, inv, contrast)
        full = mode != self._mode
        if full:
            f = 0.30 + 0.70 * contrast / 255.0
            self._colors = (_scale_color(self.top, f), _scale_color(self.bottom, f))
            self._mode = mode
        prev = self._ram
        cfg = self.cv.itemconfigure
        for idx in range(len(ram)):
            new = ram[idx]
            old = prev[idx]
            if not full and new == old:
                continue
            page, x = divmod(idx, OLED_W)
            diff = 0xFF if full else (new ^ old)
            for bit in range(8):
                if (diff >> bit) & 1:
                    y = page * 8 + bit
                    lit = bool(on) and (bool((new >> bit) & 1) != bool(inv))
                    color = self._colors[1 if y >= self.split else 0] if lit else OFF_COLOR
                    cfg(self.items[y * OLED_W + x], fill=color)
        self._ram = ram

    def _update_status(self):
        now = time.monotonic()
        if now - self._fps_t >= 1.0:
            self._fps = (self.panel.frames - self._fps_n) / (now - self._fps_t)
            self._fps_n, self._fps_t = self.panel.frames, now
        if self.fw.error:
            first = self.fw.error.strip().splitlines()[-1]
            self.status.config(fg="#ff6b6b", text="firmware stopped: " + first + "  (see terminal)")
        else:
            self.status.config(text="uptime %4.0fs | %4.1f fps | clock x%g | data: %s"
                               % (time.ticks_ms() / 1000.0, self._fps, self.clock.speed, self.feed.summary()))

    def _poll(self):
        frame = self.panel.frame
        if frame is not self._shown:
            self._redraw(frame)
            self._shown = frame
        self._update_status()
        self.root.after(15, self._poll)

    def run(self):
        self._poll()
        self.root.mainloop()


# --------------------------------------------------------------------------------------
def find_dir(explicit, script):
    candidates = [explicit] if explicit else [os.path.dirname(os.path.abspath(__file__)), os.getcwd()]
    for d in candidates:
        if d and os.path.isfile(os.path.join(d, script)):
            return os.path.abspath(d)
    sys.exit("Couldn't find %s in: %s\nUse --dir to point at your MicroPython_Stocks folder."
             % (script, ", ".join(c for c in candidates if c)))


def check_syntax(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    try:
        compile(src, path, "exec")
    except SyntaxError as e:
        hint = ""
        if sys.version_info < (3, 12):
            hint = ("\n\n%s nests same-type quotes inside f-strings (PEP 701). MicroPython accepts that, "
                    "CPython only from 3.12 - run the simulator with Python 3.12 or newer." % os.path.basename(path))
        sys.exit("%s: %s%s" % (path, e, hint))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Show what stocks.py draws on the SSD1306 OLED, in a desktop window.")
    ap.add_argument("--dir", help="folder with stocks.py and ssd1306.py (default: next to this script, then cwd)")
    ap.add_argument("--script", default="stocks.py", help="firmware file to run (default: stocks.py)")
    ap.add_argument("--offline", action="store_true", help="never touch the network; use made-up price data")
    ap.add_argument("--speed", type=float, default=1.0, help="clock multiplier, e.g. 10 (default 1)")
    ap.add_argument("--fps", type=float, default=30.0, help="max display refresh rate (default 30)")
    ap.add_argument("--scale", type=int, default=6, help="window pixels per OLED pixel (default 6)")
    ap.add_argument("--color", choices=sorted(SCHEMES), default="twotone",
                    help="panel colour: twotone = yellow/blue module (default)")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    repo = find_dir(args.dir, args.script)
    script_path = os.path.join(repo, args.script)
    check_syntax(script_path)
    os.chdir(repo)
    sys.path.insert(0, repo)  # so the firmware's `import ssd1306` finds *your* driver

    clock, panel, feed = install_mocks(args.speed, args.offline, args.fps)
    log("running %s%s" % (script_path, " (offline data)" if args.offline else ""))
    fw = Firmware(script_path)
    fw.start()
    OledWindow(panel, fw, feed, clock, args.scale, args.color).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
