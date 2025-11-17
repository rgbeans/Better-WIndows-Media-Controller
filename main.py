import asyncio
import sys
import time
import io
import threading
import os
from typing import Optional, Tuple, List, Any

import pygame

# --- Imports -------------------------------------------------------------
try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SMTCManager,
    )
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSession as SMTCSess,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as SessionPlaybackStatus,
    )
    from winsdk.windows.storage.streams import DataReader, Buffer, InputStreamOptions
    from winsdk.windows.graphics.imaging import (
        BitmapDecoder,
        BitmapPixelFormat,
        BitmapAlphaMode,
        BitmapTransform,
        ExifOrientationMode,
        ColorManagementMode,
    )
except Exception as e:
    SMTCManager = None
    SMTCSess = None
    SessionPlaybackStatus = None
    DataReader = None
    Buffer = None
    InputStreamOptions = None
    BitmapDecoder = None
    BitmapPixelFormat = None
    BitmapAlphaMode = None
    BitmapTransform = None
    ExifOrientationMode = None
    ColorManagementMode = None
    _SMTC_IMPORT_ERROR = e
else:
    _SMTC_IMPORT_ERROR = None

try:
    from PIL import Image
except Exception:
    Image = None


# -----------------------------
# Async runner (background event loop)
# -----------------------------

class AsyncRunner:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: float = 0.6) -> Any:
        """Run coroutine on the background loop, wait up to timeout seconds.
        Returns the result or raises TimeoutError on timeout.
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=0.5)


runner = AsyncRunner()


# -----------------------------
# SMTC helpers
# -----------------------------

DEBUG_DUMP_THUMB = True  # writes last thumbnail bytes to smtc_thumb_dump.bin when decode fails

# -----------------------------

async def _get_manager():
    return await SMTCManager.request_async()


async def _pick_best_session(manager: "SMTCSess") -> Optional["SMTCSess"]:
    current = manager.get_current_session()
    if current is not None:
        return current
    sessions = list(manager.get_sessions())
    if not sessions:
        return None
    for s in sessions:
        try:
            status = s.get_playback_info().playback_status
            if status == SessionPlaybackStatus.PLAYING:
                return s
        except Exception:
            pass
    return sessions[0]


async def _ensure_session(manager_cache: dict) -> Optional["SMTCSess"]:
    if "manager" not in manager_cache or manager_cache.get("manager") is None:
        manager_cache["manager"] = await _get_manager()
    try:
        return await _pick_best_session(manager_cache["manager"])
    except Exception:
        manager_cache["manager"] = await _get_manager()
        return await _pick_best_session(manager_cache["manager"])


async def smtc_action(manager_cache: dict, action: str) -> Tuple[bool, str]:
    try:
        if action == "refresh":
            manager_cache["manager"] = await _get_manager()
            return True, "Refreshed sessions"

        session = await _ensure_session(manager_cache)
        if session is None:
            return False, "No media sessions detected"

        pbi = session.get_playback_info()
        controls = pbi.controls

        if action == "play_pause":
            toggle = getattr(session, "try_toggle_play_pause_async", None)
            if callable(toggle):
                ok = await toggle()
                return bool(ok), ("Toggled Play/Pause" if ok else "Toggle Play/Pause failed")
            if not (getattr(controls, "is_play_enabled", True) or getattr(controls, "is_pause_enabled", True)):
                return False, "Play/Pause not supported by this app/content"
            status = pbi.playback_status
            if status == SessionPlaybackStatus.PLAYING and getattr(controls, "is_pause_enabled", True):
                ok = await session.try_pause_async()
                return bool(ok), ("Pause" if ok else "Pause failed")
            elif getattr(controls, "is_play_enabled", True):
                ok = await session.try_play_async()
                return bool(ok), ("Play" if ok else "Play failed")
            else:
                return False, "Play/Pause currently disabled"

        elif action == "play":
            ok = await session.try_play_async()
            return bool(ok), ("Play" if ok else "Play failed")

        elif action == "pause":
            ok = await session.try_pause_async()
            return bool(ok), ("Pause" if ok else "Pause failed")

        elif action == "next":
            if not getattr(controls, "is_next_enabled", True):
                return False, "Next not supported for this content"
            ok = await session.try_skip_next_async()
            return bool(ok), ("Next" if ok else "Skip next failed")

        elif action == "prev":
            if not getattr(controls, "is_previous_enabled", True):
                return False, "Previous not supported for this content"
            ok = await session.try_skip_previous_async()
            return bool(ok), ("Previous" if ok else "Skip previous failed")

        else:
            return False, f"Unknown action: {action}"

    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def get_cover_surface(session: "SMTCSess") -> Optional[pygame.Surface]:
    """Fetch the SMTC thumbnail and return a pygame Surface.

    Fix: use DataReader.read_bytes(<bytearray>) to pull bytes from the WinRT Buffer.
    Then try pygame decode, and fall back to Pillow (handles WebP, etc.).
    """
    try:
        props = await session.try_get_media_properties_async()
        thumb_ref = getattr(props, "thumbnail", None)
        if not thumb_ref:
            return None

        # Read the thumbnail stream into a WinRT Buffer
        buf = Buffer(5_000_000)
        stream = await thumb_ref.open_read_async()
        await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
        if buf.length == 0:
            return None

        # IMPORTANT: read_bytes expects a mutable buffer (bytearray), not an int length
        reader = DataReader.from_buffer(buf)
        arr = bytearray(buf.length)
        reader.read_bytes(arr)
        raw = bytes(arr)

        # Try pygame first
        try:
            surf = pygame.image.load(io.BytesIO(raw))
            return surf.convert_alpha()
        except Exception:
            pass

        # Pillow fallback for formats pygame lacks (e.g., WebP)
        try:
            from PIL import Image  # imported lazily to avoid hard dependency
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
            data = im.tobytes()
            w, h = im.size
            surf = pygame.image.frombuffer(data, (w, h), "RGBA")
            return surf.convert_alpha()
        except Exception:
            return None

    except Exception:
        return None

        buf = Buffer(5_000_000)
        stream = await thumb_ref.open_read_async()
        await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
        if buf.length == 0:
            return None

        reader = DataReader.from_buffer(buf)
        raw = bytes(bytearray(reader.read_bytes(buf.length)))

        # First try pygame's built-in decoders
        try:
            surf = pygame.image.load(io.BytesIO(raw))
            return surf.convert_alpha()
        except Exception:
            pass

        # Then try Pillow, if available (handles WebP, etc.)
        if Image is not None:
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGBA")
                data = im.tobytes()
                w, h = im.size
                surf = pygame.image.frombuffer(data, (w, h), "RGBA")
                return surf.convert_alpha()
            except Exception:
                pass

        # If we reach here, decoding failed; optionally dump bytes for debugging
        if DEBUG_DUMP_THUMB:
            try:
                with open("smtc_thumb_dump.bin", "wb") as f:
                    f.write(raw)
            except Exception:
                pass

        return None

    except Exception:
        return None


async def query_now_playing(manager_cache: dict):
    """Return now-playing info and a decoded cover surface (if any).

    Returns: (title, artist, source_app, caps, is_playing, cover_surface, has_thumbnail)
    """
    session = await _ensure_session(manager_cache)
    if session is None:
        return (
            "Nothing playing",
            "",
            "",
            {"play_pause": False, "next": False, "prev": False},
            False,
            None,
            False,
            None,
        )

    # Basic metadata
    try:
        props = await session.try_get_media_properties_async()
        title = props.title or "Unknown Title"
        artist = props.artist or props.album_artist or ""
        has_thumb = bool(getattr(props, "thumbnail", None))
    except Exception:
        title, artist, has_thumb = "Unknown Title", "", False

    # Source application ID (e.g. "Spotify.exe", browser tab origin, etc.)
    try:
        disp = session.source_app_user_model_id
    except Exception:
        disp = ""

    # Playback status + capability flags
    try:
        pbi = session.get_playback_info()
        c = pbi.controls
        is_playing = (pbi.playback_status == SessionPlaybackStatus.PLAYING)
        caps = {
            "play_pause": bool(
                getattr(c, "is_play_pause_toggle_enabled", False)
                or getattr(c, "is_play_enabled", False)
                or getattr(c, "is_pause_enabled", False)
            ),
            "next": bool(getattr(c, "is_next_enabled", False)),
            "prev": bool(getattr(c, "is_previous_enabled", False)),
        }
    except Exception:
        caps = {"play_pause": True, "next": True, "prev": True}
        is_playing = False

    cover = await get_cover_surface(session)
    return title, artist, disp, caps, is_playing, cover, has_thumb


# -----------------------------
# UI helpers (layout, colors, drawing)


# -----------------------------

# Base design size (used for responsive scaling)
BASE_W, BASE_H = 640, 256
ASPECT = BASE_W / BASE_H  # 2.5

# ---- Minimum window size (aspect-locked) ----
MIN_H = 200
MIN_W = int(MIN_H * ASPECT)  # -> 500 for 2.5 aspect

# ---- Initial actual window size ----
WIDTH, HEIGHT = MIN_W, MIN_H  # open at the minimum scale

BG = (18, 18, 18)
FG = (230, 230, 230)
MUTED = (150, 150, 150)
BTN_BG = (40, 40, 40)
BTN_BG_HOVER = (64, 64, 64)
BTN_BG_DISABLED = (30, 30, 30)
ACCENT = (100, 180, 255)
ERROR = (255, 120, 120)
OK = (120, 220, 160)

# Base metrics (scaled per window size in layout())
BASE_PADDING = 20
BASE_BTN_W, BASE_BTN_H = 170, 54
BASE_GAP = 16
BASE_COVER_SIZE = 96
SHOW_CONSOLE = False  # toggle with 'C'

# --- Native window move helpers (Windows) ---
WINDOW_DRAG_ENABLED = True
try:
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.windll.user32

    # Explicitly declare argtypes/restype so HWND (pointer-sized) is passed correctly on 64‑bit
    _SetWindowPos = _user32.SetWindowPos
    _SetWindowPos.argtypes = [
        wintypes.HWND,  # hWnd
        wintypes.HWND,  # hWndInsertAfter
        ctypes.c_int,   # X
        ctypes.c_int,   # Y
        ctypes.c_int,   # cx
        ctypes.c_int,   # cy
        ctypes.c_uint,  # uFlags
    ]
    _SetWindowPos.restype = wintypes.BOOL

    _GetWindowRect = _user32.GetWindowRect
    _GetCursorPos = _user32.GetCursorPos

    _SWP_NOSIZE = 0x0001
    _SWP_NOMOVE = 0x0002
    _SWP_NOZORDER = 0x0004
    _SWP_NOACTIVATE = 0x0010
    _SWP_SHOWWINDOW = 0x0040

    _HWND_TOPMOST = -1
    _HWND_NOTOPMOST = -2

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
except Exception:
    WINDOW_DRAG_ENABLED = False

os.environ.setdefault("SDL_MOUSE_FOCUS_CLICKTHROUGH", "1")
pygame.init()
pygame.display.set_caption("Better Windows Media Controller")
screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.RESIZABLE | pygame.NOFRAME)
clock = pygame.time.Clock()
font_big = pygame.font.SysFont("segoeui", 28)
font_mid = pygame.font.SysFont("segoeui", 18)
font_small = pygame.font.SysFont("segoeui", 14)


class Button:
    def __init__(self, action: str):
        # initial size; layout() will rescale
        self.rect = pygame.Rect(0, 0, BASE_BTN_W, BASE_BTN_H)
        self.action = action
        self.enabled = True

    def draw(self, surf: pygame.Surface, hover: bool = False, is_playing: bool = False):
        bg = BTN_BG_DISABLED if not self.enabled else (BTN_BG_HOVER if hover else BTN_BG)
        pygame.draw.rect(surf, bg, self.rect, border_radius=12)
        cx, cy = self.rect.center
        w, h = self.rect.size
        scale = min(w, h) * 0.28
        color = (100, 100, 100) if not self.enabled else FG
        if self.action == "prev":
            bar = pygame.Rect(0,0, max(2, int(scale*0.22)), int(scale*1.4))
            bar.center = (cx - int(scale*0.9), cy)
            pygame.draw.rect(surf, color, bar, border_radius=2)
            tri1 = [ (cx - int(scale*0.25), cy), (cx - int(scale*0.25)+int(scale*0.9), cy-int(scale*0.8)), (cx - int(scale*0.25)+int(scale*0.9), cy+int(scale*0.8)) ]
            tri2 = [ (cx + int(scale*0.45), cy), (cx + int(scale*0.45)+int(scale*0.9), cy-int(scale*0.8)), (cx + int(scale*0.45)+int(scale*0.9), cy+int(scale*0.8)) ]
            pygame.draw.polygon(surf, color, tri1)
            pygame.draw.polygon(surf, color, tri2)
        elif self.action == "next":
            bar = pygame.Rect(0,0, max(2, int(scale*0.22)), int(scale*1.4))
            bar.center = (cx + int(scale*1.0), cy)
            pygame.draw.rect(surf, color, bar, border_radius=2)
            tri1 = [ (cx - int(scale*0.9), cy-int(scale*0.8)), (cx - int(scale*0.9), cy+int(scale*0.8)), (cx - int(scale*0.05), cy) ]
            tri2 = [ (cx + int(scale*0.05), cy-int(scale*0.8)), (cx + int(scale*0.05), cy+int(scale*0.8)), (cx + int(scale*0.9), cy) ]
            pygame.draw.polygon(surf, color, tri1)
            pygame.draw.polygon(surf, color, tri2)
        elif self.action == "play_pause":
            if is_playing:
                wbar = max(2, int(scale*0.28))
                hbar = int(scale*1.5)
                gap = int(scale*0.35)
                r1 = pygame.Rect(cx - gap - wbar, cy - hbar//2, wbar, hbar)
                r2 = pygame.Rect(cx + gap,          cy - hbar//2, wbar, hbar)
                pygame.draw.rect(surf, color, r1, border_radius=2)
                pygame.draw.rect(surf, color, r2, border_radius=2)
            else:
                tri = [ (cx - int(scale*0.6), cy-int(scale*0.9)), (cx - int(scale*0.6), cy+int(scale*0.9)), (cx + int(scale*0.9), cy) ]
                pygame.draw.polygon(surf, color, tri)
        elif self.action == "refresh":
            # simple circular refresh icon
            r = int(scale*1.2)
            pygame.draw.circle(surf, color, (cx, cy), r, width=max(2,int(scale*0.18)))
            tip = (cx + r - int(scale*0.1), cy)
            wing1 = (tip[0] - int(scale*0.7), cy - int(scale*0.5))
            wing2 = (tip[0] - int(scale*0.7), cy + int(scale*0.5))
            pygame.draw.polygon(surf, color, [tip, wing1, wing2])
        elif self.action == "mc":
            # Auto-fit "Minecraft mode" to the button rect (longer text: slightly smaller ratio)
            label = render_fit_text("Minecraft mode", self.rect, FG, family="segoeui", bold=False,
                                    max_h_ratio=0.46, hpad=int(self.rect.height * 0.32))
            surf.blit(label, label.get_rect(center=self.rect.center))
        elif self.action == "min":
            # draw a simple '-' icon
            bar_w = int(w * 0.5)
            bar_h = max(2, int(h * 0.1))
            rect = pygame.Rect(0, 0, bar_w, bar_h)
            rect.center = (cx, cy + int(h * 0.15))
            pygame.draw.rect(surf, color, rect, border_radius=2)
        elif self.action == "aot":
            # Auto-fit "AOT" label to the button rect
            label = render_fit_text("AOT", self.rect, color, family="segoeui", bold=False,
                                    max_h_ratio=0.58, hpad=int(self.rect.height * 0.24))
            surf.blit(label, label.get_rect(center=self.rect.center))
        elif self.action == "close":
            # draw an 'X'
            l = int(min(w, h) * 0.55)
            dx = l // 2
            pygame.draw.line(surf, color, (cx - dx, cy - dx), (cx + dx, cy + dx), max(2, int(l * 0.12)))
            pygame.draw.line(surf, color, (cx - dx, cy + dx), (cx + dx, cy - dx), max(2, int(l * 0.12)))

    def contains(self, pos) -> bool:
        return self.rect.collidepoint(pos)


class Console:
    def __init__(self, max_lines: int = 6):
        self.lines: List[str] = []
        self.max_lines = max_lines

    def log(self, text: str):
        for part in wrap_text(text, 100):
            self.lines.append(part)
        self.lines = self.lines[-(self.max_lines):]

    def draw(self, surf: pygame.Surface, area: pygame.Rect):
        pygame.draw.rect(surf, (24, 24, 24), area, border_radius=8)
        y = area.top + 6
        for line in self.lines:
            color = ERROR if line.startswith("✗") or "Error" in line or "Exception" in line else ACCENT
            surf.blit(font_small.render(line, True, color), (area.left + 8, y))
            y += 18


def wrap_text(s: str, width: int) -> List[str]:
    words = s.split()
    out, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = (w if not cur else cur + " " + w)
    if cur:
        out.append(cur)
    return out

_font_cache = {}

def _get_font_cached(family: str, size: int, bold: bool = False):
    key = (family, size, bold)
    f = _font_cache.get(key)
    if f is None:
        f = pygame.font.SysFont(family, size, bold=bold)
        _font_cache[key] = f
    return f

def render_fit_text(text: str,
                    rect: pygame.Rect,
                    color: tuple,
                    *,
                    family: str = "segoeui",
                    bold: bool = False,
                    max_h_ratio: float = 0.52,
                    hpad: int | None = None) -> pygame.Surface:
    """
    Returns a rendered surface of `text` sized to fit inside `rect`.
    - Starts from rect.height * max_h_ratio and shrinks until it fits width/height.
    - hpad controls left/right padding; defaults to ~0.28 * height for rounded chips.
    """
    if hpad is None:
        hpad = max(6, int(rect.height * 0.28))

    # Start at a size proportional to the button height.
    size = max(8, int(rect.height * max_h_ratio))
    font = _get_font_cached(family, size, bold)
    surf = font.render(text, True, color)

    # Shrink until it fits within width - 2*hpad and height - small margin
    max_w = max(8, rect.width - 2 * hpad)
    max_h = max(8, rect.height - 2)

    while (surf.get_width() > max_w or surf.get_height() > max_h) and size > 8:
        size -= 1
        font = _get_font_cached(family, size, bold)
        surf = font.render(text, True, color)

    return surf


buttons = [Button("prev"), Button("play_pause"), Button("next")]
min_btn = Button("min")
aot_btn = Button("aot")
close_btn = Button("close")
mc_btn = Button("mc")  # toggles Minecraft Mode


def layout(rect: pygame.Rect, show_console: bool):
    w, h = rect.size
    # scale factor based on the smaller axis relative to base design
    s = min(w / BASE_W, h / BASE_H)
    P = max(10, int(BASE_PADDING * s))
    BTN_W = max(90, int(BASE_BTN_W * s))
    BTN_H = max(38, int(BASE_BTN_H * s))
    GAP = max(8, int(BASE_GAP * s))
    COVER_SIZE = max(56, int(BASE_COVER_SIZE * s))

    # console height scales lightly; clamp
    console_h = (84 if show_console else 0)
    console_h = int(console_h * s) if show_console else 0

    # position buttons centered
    x0 = (w - (BTN_W * 3 + GAP * 2)) // 2
    y0 = h - BTN_H - P - (console_h + (10 if show_console else 0))
    for i in range(3):
        buttons[i].rect.size = (BTN_W, BTN_H)
    buttons[0].rect.topleft = (x0, y0)
    buttons[1].rect.topleft = (x0 + BTN_W + GAP, y0)
    buttons[2].rect.topleft = (x0 + 2 * (BTN_W + GAP), y0)

    # top-right cluster: [refresh] [min] [aot] [close]
    icon_w, icon_h = max(26, int(36 * s)), max(22, int(30 * s))
    spacing = max(8, int(8 * s))
    close_btn.rect.size = (icon_w, icon_h)
    aot_btn.rect.size = (icon_w, icon_h)
    min_btn.rect.size = (icon_w, icon_h)
    close_btn.rect.topright = (w - P, P)
    aot_btn.rect.topright = (close_btn.rect.left - spacing, P)
    min_btn.rect.topright = (aot_btn.rect.left - spacing, P)

    header_area = pygame.Rect(P + COVER_SIZE + 14, P + 8, w - 2 * P - 40 - COVER_SIZE - 14, max(100, int(140 * s)))
    cover_rect = pygame.Rect(P, P + 8, COVER_SIZE, COVER_SIZE)

    # Minecraft mode button near header
    mc_btn.rect.size = (max(90, int(130 * s)), max(34, int(42 * s)))
    mc_btn.rect.topright = (w - P, header_area.top + int(52 * s))

    console_area = pygame.Rect(P, h - console_h - P, w - 2 * P, console_h)
    # return scale + padding so draw code can place progress bar consistently
    return header_area, cover_rect, console_area

    console_area = pygame.Rect(P, h - console_h - P, w - 2 * P, console_h)
    # return scale + padding so draw code can place progress bar consistently
    return header_area, cover_rect, console_area


console = Console(max_lines=6)


def draw_header(area: pygame.Rect, title: str, artist: str, source: str, is_playing: bool, mc_enabled: bool, aot_enabled: bool):
    title_surf = font_big.render(title, True, FG)
    screen.blit(title_surf, (area.left, area.top))
    meta = " — ".join([t for t in [artist, source] if t]).strip()
    if meta:
        screen.blit(font_mid.render(meta, True, MUTED), (area.left, area.top + 34))
    chip = pygame.Rect(area.left, area.top + 60, 86, 22)
    pygame.draw.rect(screen, (28, 28, 28), chip, border_radius=12)
    dot_color = OK if is_playing else MUTED
    pygame.draw.circle(screen, dot_color, (chip.left + 12, chip.centery), 5)
    text = "Playing" if is_playing else "Paused"
    screen.blit(font_small.render(text, True, FG), (chip.left + 24, chip.top + 3))
    # AOT chip (drawn next to status chip)
    chip_right = chip.right
    if aot_enabled:
        aot_chip = pygame.Rect(chip_right + 8, chip.top, 44, 22)
        pygame.draw.rect(screen, (28, 28, 28), aot_chip, border_radius=12)
        screen.blit(font_small.render("AOT", True, FG), (aot_chip.left + 10, aot_chip.top + 3))
        chip_right = aot_chip.right
    # Optional Minecraft mode chip (placed after AOT chip if present)
    if mc_enabled:
        chip2 = pygame.Rect(chip_right + 8, chip.top, 110, 22)
        pygame.draw.rect(screen, (28, 28, 28), chip2, border_radius=12)
        screen.blit(font_small.render("Minecraft mode", True, FG), (chip2.left + 8, chip2.top + 3))


def draw_cover(cover_surf: Optional[pygame.Surface], rect: pygame.Rect):
    # Semi-transparent card so transparent PNG corners blend nicely
    card = pygame.Surface(rect.size, pygame.SRCALPHA)
    pygame.draw.rect(card, (255, 255, 255, 18), card.get_rect(), border_radius=12)
    if cover_surf:
        surf = pygame.transform.smoothscale(cover_surf, rect.size)
        card.blit(surf, (0, 0))  # alpha preserved
    screen.blit(card, rect.topleft)


def main():
    """Main UI loop for the Better Windows Media Controller.

    Features:
    - Borderless resizable window (bottom-right grip, aspect locked)
    - Window drag by clicking any background area
    - SMTC play/pause/next/prev + metadata/cover
    - Minecraft mode (delay between tracks)
    - Optional always-on-top toggle with chip indicator
    """

    global screen

    if sys.platform != "win32":
        msg = "This app requires Windows 10/11 (SMTC)."
        print(msg)
        screen.fill(BG)
        warn = font_mid.render(msg, True, FG)
        screen.blit(warn, warn.get_rect(center=(WIDTH // 2, HEIGHT // 2)))
        pygame.display.flip()
        time.sleep(4)
        return

    if SMTCManager is None:
        screen.fill(BG)
        lines = [
            "winsdk not available.",
            "Install dependencies:",
            "    pip install winsdk pygame pillow",
            "",
            f"Error: {_SMTC_IMPORT_ERROR}",
        ]
        y = BASE_PADDING
        for line in lines:
            screen.blit(font_mid.render(line, True, FG), (BASE_PADDING, y))
            y += 24
        pygame.display.flip()
        waiting = True
        while waiting:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    waiting = False
        return

    manager_cache = {"manager": None}
    REFRESH_EVENT = pygame.USEREVENT + 1
    pygame.time.set_timer(REFRESH_EVENT, 900)

    title, artist, source = "Loading…", "", ""
    caps = {"play_pause": True, "next": True, "prev": True}
    is_playing = False
    cover_surf = None

    header_area, cover_rect, console_area = layout(screen.get_rect(), SHOW_CONSOLE)

    # Always-on-top state
    ALWAYS_ON_TOP = False

    # --- Minecraft Mode state ---
    MINECRAFT_MODE = False
    MC_DELAY = 300.0  # seconds between tracks
    last_track_key: Optional[str] = None
    mc_resume_at: Optional[float] = None  # epoch seconds to resume play
    mc_ignore_next_change: bool = False   # don't trigger MC logic on user-initiated skips

    # --- Borderless resize state ---
    RESIZING = False
    resize_start_mouse = (0, 0)
    resize_start_size = screen.get_size()
    resize_grip_rect = pygame.Rect(0, 0, 0, 0)

    # Dragging (move window)
    dragging = False
    drag_offset = (0, 0)

    hwnd = None
    if WINDOW_DRAG_ENABLED:
        try:
            hwnd = pygame.display.get_wm_info().get("window")
        except Exception:
            hwnd = None

    def apply_always_on_top(on: bool):
        nonlocal ALWAYS_ON_TOP
        ALWAYS_ON_TOP = on
        if not (WINDOW_DRAG_ENABLED and hwnd):
            return
        try:
            insert_after = _HWND_TOPMOST if on else _HWND_NOTOPMOST
            _SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(insert_after),
                0,
                0,
                0,
                0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            )
        except Exception as e:
            if SHOW_CONSOLE:
                console.log(f"✗ AOT error: {e}")

    # Initial SMTC query
    try:
        t, a, s, c, playing, cov, has_thumb = runner.run(query_now_playing(manager_cache), timeout=1.0)
        title, artist, source, caps, is_playing, cover_surf = t, a, s, c, playing, cov
        last_track_key = f"{title}|{artist}|{source}"
    except Exception as e:
        if SHOW_CONSOLE:
            console.log(f"✗ Initial query: {e}")

    running = True
    clock.tick(60)

    while running:
        mouse_pos = pygame.mouse.get_pos()

        # Update resize grip rect for this frame (bottom-right corner)
        ww, hh = screen.get_size()
        s_for_grip = min(ww / BASE_W, hh / BASE_H)
        grip_size = max(14, int(18 * s_for_grip))
        resize_grip_rect = pygame.Rect(ww - grip_size - 6, hh - grip_size - 6, grip_size, grip_size)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == REFRESH_EVENT:
                # Periodic SMTC refresh
                try:
                    t, a, s, c, playing, cov, has_thumb = runner.run(
                        query_now_playing(manager_cache), timeout=0.9
                    )
                    new_key = f"{t}|{a}|{s}"

                    if last_track_key is not None and new_key != last_track_key:
                        # Track changed
                        if MINECRAFT_MODE and not mc_ignore_next_change:
                            # Pause immediately and start delay
                            try:
                                ok, msg = runner.run(
                                    smtc_action(manager_cache, "pause"), timeout=0.9
                                )
                                if SHOW_CONSOLE:
                                    console.log(("○ " if ok else "✗ ") + msg)
                            except Exception as e2:
                                if SHOW_CONSOLE:
                                    console.log(f"✗ Pause error: {e2}")
                            mc_resume_at = time.time() + MC_DELAY
                        mc_ignore_next_change = False
                        last_track_key = new_key

                    title, artist, source, caps, is_playing, cover_surf = t, a, s, c, playing, cov
                except Exception as e:
                    if SHOW_CONSOLE:
                        console.log(f"✗ Refresh error: {e}")

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                # Resize grip click?
                if resize_grip_rect.collidepoint(event.pos):
                    RESIZING = True
                    resize_start_mouse = event.pos
                    resize_start_size = screen.get_size()
                    continue

                # Action buttons (playback / window controls / MC mode)
                clicked_ui = False
                all_buttons = buttons + [min_btn, aot_btn, close_btn, mc_btn]
                for btn in all_buttons:
                    if btn.contains(event.pos):
                        clicked_ui = True
                        action = btn.action

                        if action in ("prev", "next", "play_pause") and not btn.enabled:
                            break

                        try:
                            if action == "prev":
                                had_timer = (mc_resume_at is not None)
                                if MINECRAFT_MODE:
                                    # Don't let the track-change hook pause the new track
                                    mc_ignore_next_change = True
                                    # User wants to override the timer
                                    mc_resume_at = None
                                ok, msg = runner.run(smtc_action(manager_cache, "prev"), timeout=0.9)
                                if SHOW_CONSOLE:
                                    console.log(("○ " if ok else "✗ ") + msg)
                                # If we were in a Minecraft delay, just play right away
                                if MINECRAFT_MODE and had_timer:
                                    try:
                                        ok2, msg2 = runner.run(smtc_action(manager_cache, "play"), timeout=0.9)
                                        if SHOW_CONSOLE:
                                            console.log(("○ " if ok2 else "✗ ") + msg2)
                                    except Exception as e:
                                        if SHOW_CONSOLE:
                                            console.log(f"✗ Play-after-prev error: {e}")

                            elif action == "next":
                                had_timer = (mc_resume_at is not None)
                                if MINECRAFT_MODE:
                                    mc_ignore_next_change = True
                                    mc_resume_at = None
                                ok, msg = runner.run(smtc_action(manager_cache, "next"), timeout=0.9)
                                if SHOW_CONSOLE:
                                    console.log(("○ " if ok else "✗ ") + msg)
                                if MINECRAFT_MODE and had_timer:
                                    try:
                                        ok2, msg2 = runner.run(smtc_action(manager_cache, "play"), timeout=0.9)
                                        if SHOW_CONSOLE:
                                            console.log(("○ " if ok2 else "✗ ") + msg2)
                                    except Exception as e:
                                        if SHOW_CONSOLE:
                                            console.log(f"✗ Play-after-next error: {e}")

                            elif action == "play_pause":
                                # If a MC timer is running and we are paused, cancel timer and resume immediately
                                if MINECRAFT_MODE and mc_resume_at is not None and not is_playing:
                                    mc_resume_at = None
                                ok, msg = runner.run(smtc_action(manager_cache, "play_pause"), timeout=0.9)
                                if SHOW_CONSOLE:
                                    console.log(("○ " if ok else "✗ ") + msg)

                            elif action == "mc":
                                MINECRAFT_MODE = not MINECRAFT_MODE
                                mc_resume_at = None
                                mc_ignore_next_change = False

                            elif action == "min":
                                pygame.display.iconify()

                            elif action == "aot":
                                apply_always_on_top(not ALWAYS_ON_TOP)

                            elif action == "close":
                                running = False

                        except Exception as e:
                            if SHOW_CONSOLE:
                                console.log(f"✗ Action error: {e}")

                        break

                # Start window drag if click wasn't on any UI and dragging is supported
                if (not clicked_ui) and WINDOW_DRAG_ENABLED and hwnd and not RESIZING:
                    try:
                        rect = _RECT()
                        pt = _POINT()
                        _GetWindowRect(hwnd, ctypes.byref(rect))
                        _GetCursorPos(ctypes.byref(pt))
                        drag_offset = (pt.x - rect.left, pt.y - rect.top)
                        dragging = True
                    except Exception:
                        dragging = False

            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                dragging = False
                RESIZING = False

            elif event.type == pygame.MOUSEMOTION:
                if RESIZING:
                    # Aspect-locked resize from grip
                    start_w, start_h = resize_start_size
                    dx = event.pos[0] - resize_start_mouse[0]

                    new_w = max(MIN_W, start_w + dx)
                    new_h = int(new_w / ASPECT)
                    if new_h < MIN_H:
                        new_h = MIN_H
                        new_w = int(new_h * ASPECT)

                    screen = pygame.display.set_mode((new_w, new_h), pygame.RESIZABLE | pygame.NOFRAME)
                    header_area, cover_rect, console_area = layout(screen.get_rect(), SHOW_CONSOLE)

                    # Reassert AOT after resize
                    if ALWAYS_ON_TOP:
                        apply_always_on_top(True)

                elif dragging and WINDOW_DRAG_ENABLED and hwnd:
                    try:
                        pt = _POINT()
                        _GetCursorPos(ctypes.byref(pt))
                        new_x = pt.x - drag_offset[0]
                        new_y = pt.y - drag_offset[1]
                        _SetWindowPos(
                            wintypes.HWND(hwnd),
                            wintypes.HWND(0),
                            new_x,
                            new_y,
                            0,
                            0,
                            _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
                        )
                    except Exception:
                        dragging = False

        # --- Minecraft mode timer check ---
        timer_remaining: Optional[float] = None
        if MINECRAFT_MODE and mc_resume_at is not None:
            now = time.time()
            timer_remaining = max(0.0, mc_resume_at - now)
            if timer_remaining <= 0:
                # Time to resume playback
                try:
                    ok, msg = runner.run(smtc_action(manager_cache, "play"), timeout=0.9)
                    if SHOW_CONSOLE:
                        console.log(("○ " if ok else "✗ ") + msg)
                except Exception as e:
                    if SHOW_CONSOLE:
                        console.log(f"✗ Resume error: {e}")
                mc_resume_at = None
                timer_remaining = None

        # --- Drawing ---
        screen.fill(BG)

        # Cover art
        draw_cover(cover_surf, cover_rect)

        # Header (title / artist / status / chips)
        draw_header(header_area, title, artist, source, is_playing, MINECRAFT_MODE, ALWAYS_ON_TOP)

        # Enable/disable main buttons based on SMTC caps
        buttons[0].enabled = caps.get("prev", True)
        buttons[1].enabled = caps.get("play_pause", True)
        buttons[2].enabled = caps.get("next", True)

        # Draw main transport buttons
        for btn in buttons:
            btn.draw(screen, hover=btn.contains(mouse_pos), is_playing=is_playing)

        # Top-right window control buttons + Minecraft mode toggle button
        for top_btn in (min_btn, aot_btn, close_btn, mc_btn):
            top_btn.draw(screen, hover=top_btn.contains(mouse_pos))

        # Minecraft-mode progress bar & timer text
        if MINECRAFT_MODE:
            w, h = screen.get_size()
            s = min(w / BASE_W, h / BASE_H)
            P = max(10, int(BASE_PADDING * s))
            bar_height = max(4, int(5 * s))
            bar_y = buttons[0].rect.top - int(18 * s)
            bar_rect = pygame.Rect(P, bar_y, w - 2 * P, bar_height)
            pygame.draw.rect(screen, (40, 40, 40), bar_rect, border_radius=bar_height // 2)

            if timer_remaining is not None:
                frac = max(0.0, min(1.0, (MC_DELAY - timer_remaining) / MC_DELAY))
                fill_w = int(bar_rect.width * frac)
                if fill_w > 0:
                    fill_rect = pygame.Rect(bar_rect.left, bar_rect.top, fill_w, bar_rect.height)
                    pygame.draw.rect(screen, (210, 60, 60), fill_rect, border_radius=bar_height // 2)

                # Timer label near the right edge
                mm = int(timer_remaining // 60)
                ss = int(timer_remaining % 60)
                label = font_mid.render(f"{mm}:{ss:02d}", True, FG)
                screen.blit(label, (w - P - label.get_width(), bar_rect.top - label.get_height() - 2))

        # Console (if enabled)
        if SHOW_CONSOLE:
            console.draw(screen, console_area)

        # Draw resize grip (corner handle)
        ww, hh = screen.get_size()
        pygame.draw.line(screen, (70, 70, 70), (ww - 14, hh - 4), (ww - 4, hh - 14), 2)
        pygame.draw.line(screen, (70, 70, 70), (ww - 20, hh - 4), (ww - 4, hh - 20), 2)

        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            runner.stop()
        except Exception:
            pass
