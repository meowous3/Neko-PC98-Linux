#!/usr/bin/env python3
"""NEKO.COM port — a cat that chases the mouse cursor.

Faithfully ported from the PC-9801 TSR disassembly (NEKO.asm).
States, timing, and behavior match the original.

Behavioral flow (from decide_direction at 0x03E0):
  chase cursor → scratch/groom at cursor → fall asleep → maybe wander
"""

import atexit
import io
import json
import os
import random
import subprocess
import sys
import tempfile

# Force X11/XWayland for GTK window — reliable positioning + transparency
os.environ.setdefault("GDK_BACKEND", "x11")

import cairo
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
import gi
from PIL import Image

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from gi.repository import AppIndicator3, Gdk, GdkPixbuf, GLib, Gtk

# ---------------------------------------------------------------------------
# Constants (from the original binary)
# ---------------------------------------------------------------------------
GRID = 8              # pixels per grid cell (original: 8px character cells)
IDLE_THRESHOLD = 100  # ticks sitting before cat gets bored (0x023C)
DEADZONE = 64         # pixels — cat won't chase if cursor is within this range
DEFAULT_TICK_MS = 180 # ms between updates (~5.6 FPS, original: VSYNC/10)
DEFAULT_MOVE_SPEED = 1  # grid units per tick (original: 1)
DEFAULT_SCALE = 4     # sprite display scale (34x34 base → 136x136 at 4×)
SPRITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sprites")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neko.json")

# Animation states (anim_state values from data segment at 0x0116)
ST_UP = 0             # moving up
ST_DOWN = 1           # moving down
ST_RIGHT = 2          # moving right
ST_LEFT = 3           # moving left
ST_SCRATCH_RIGHT = 4  # grooming/scratching, facing right (paw + sparks)
ST_SCRATCH_LEFT = 5   # grooming/scratching, facing left
ST_SLEEP = 6          # sleeping/sitting still

# File names from extraction (numbers correct, text labels were misnamed)
_SPRITE_PREFIXES = [
    "move_up", "move_down", "move_right", "move_left",
    "sit_right", "sit_left", "scratch",
]

DBUS_NAME = "com.github.neko.cursor"
DBUS_PATH = "/cursor"

# KWin script: polls workspace.cursorPos at ~20Hz and pushes via callDBus
KWIN_JS = """\
var last_x = -1, last_y = -1;
var timer = new QTimer();
timer.timeout.connect(function() {{
    var p = workspace.cursorPos;
    if (p.x !== last_x || p.y !== last_y) {{
        last_x = p.x;
        last_y = p.y;
        callDBus("{svc}", "{path}", "{svc}", "update", p.x + "," + p.y);
    }}
}});
timer.start(50);
"""

# =========================================================================
# Cursor providers — abstract away compositor differences
# =========================================================================

class KWinCursorProvider:
    """KDE Plasma: load a KWin script that pushes cursor pos via DBus."""

    STALE_THRESHOLD = 3.0  # seconds without an update before reloading

    def __init__(self, dbus_service):
        import time as _time
        self.svc = dbus_service
        self.script_name = "neko_cursor_tracker"
        self._tmpfile = None
        self._loaded = False
        self._scale = 1.0
        self._monotonic = _time.monotonic
        self._last_reload = 0.0
        self._detect_scale()
        self._load_script()
        atexit.register(self.cleanup)

    def _detect_scale(self):
        """Compute XWayland/KWin coordinate scale factor.

        KWin cursorPos uses Wayland logical coords. Our GTK window uses
        XWayland physical coords. Query the Wayland logical screen size
        in a subprocess and divide.
        """
        try:
            out = subprocess.check_output([
                sys.executable, "-c",
                "import os; os.environ['GDK_BACKEND']='wayland'; "
                "import gi; gi.require_version('Gtk','3.0'); "
                "gi.require_version('Gdk','3.0'); "
                "from gi.repository import Gdk; "
                "d=Gdk.Display.get_default(); "
                "m=d.get_primary_monitor() or d.get_monitor(0); "
                "g=m.get_geometry(); print(g.width, g.height)"
            ], text=True, timeout=3).strip()
            lw, lh = map(int, out.split())
            if lw > 0:
                display = Gdk.Display.get_default()
                mon = display.get_primary_monitor() or display.get_monitor(0)
                phys_w = mon.get_geometry().width
                self._scale = phys_w / lw
        except Exception:
            pass

    def _load_script(self):
        js = KWIN_JS.format(svc=DBUS_NAME, path=DBUS_PATH)
        self._tmpfile = tempfile.NamedTemporaryFile(
            suffix=".js", delete=False, mode="w", prefix="neko_kwin_"
        )
        self._tmpfile.write(js)
        self._tmpfile.close()

        try:
            result = subprocess.check_output([
                "gdbus", "call", "--session",
                "--dest", "org.kde.KWin",
                "--object-path", "/Scripting",
                "--method", "org.kde.kwin.Scripting.loadScript",
                self._tmpfile.name, self.script_name,
            ], text=True).strip()
            # result looks like "(0,)" — extract the script ID
            self._script_id = int(result.strip("(),"))
            subprocess.check_call([
                "gdbus", "call", "--session",
                "--dest", "org.kde.KWin",
                "--object-path", "/Scripting",
                "--method", "org.kde.kwin.Scripting.start",
            ], stdout=subprocess.DEVNULL)
            self._loaded = True
        except Exception as e:
            print(f"Warning: failed to load KWin script: {e}", file=sys.stderr)

    def _reload_script(self):
        """Unload and reload the KWin cursor tracking script."""
        now = self._monotonic()
        if now - self._last_reload < 10.0:
            return  # don't spam reloads
        self._last_reload = now
        print("neko: cursor tracking stale, reloading KWin script",
              file=sys.stderr)
        # Unload old script
        if self._loaded:
            try:
                subprocess.call([
                    "gdbus", "call", "--session",
                    "--dest", "org.kde.KWin",
                    "--object-path", "/Scripting",
                    "--method", "org.kde.kwin.Scripting.unloadScript",
                    self.script_name,
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            self._loaded = False
        self._load_script()

    def get_position(self):
        # Auto-recover if updates have stopped
        now = self._monotonic()
        if now - self.svc.last_update > self.STALE_THRESHOLD:
            self._reload_script()
        x = int(self.svc.cursor_x * self._scale)
        y = int(self.svc.cursor_y * self._scale)
        return x, y

    def cleanup(self):
        if self._loaded:
            try:
                subprocess.call([
                    "gdbus", "call", "--session",
                    "--dest", "org.kde.KWin",
                    "--object-path", "/Scripting",
                    "--method", "org.kde.kwin.Scripting.unloadScript",
                    self.script_name,
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            self._loaded = False
        if self._tmpfile:
            try:
                os.unlink(self._tmpfile.name)
            except Exception:
                pass


class HyprlandCursorProvider:
    """Hyprland: use hyprctl cursorpos."""

    def get_position(self):
        try:
            out = subprocess.check_output(
                ["hyprctl", "cursorpos", "-j"],
                timeout=0.5, text=True,
            )
            data = json.loads(out)
            return data["x"], data["y"]
        except Exception:
            return 0, 0

    def cleanup(self):
        pass


class X11CursorProvider:
    """Fallback: GDK X11 pointer query (works on real X11 sessions)."""

    def get_position(self):
        seat = Gdk.Display.get_default().get_default_seat()
        _, x, y = seat.get_pointer().get_position()
        return x, y

    def cleanup(self):
        pass


class CursorDBusService(dbus.service.Object):
    """DBus service that receives cursor position updates from KWin."""

    def __init__(self):
        import time as _time
        bus_name = dbus.service.BusName(DBUS_NAME, dbus.SessionBus())
        super().__init__(bus_name, DBUS_PATH)
        self.cursor_x = 0
        self.cursor_y = 0
        self.last_update = _time.monotonic()
        self._monotonic = _time.monotonic

    @dbus.service.method(DBUS_NAME, in_signature="s")
    def update(self, pos_str):
        parts = pos_str.split(",")
        self.cursor_x = int(parts[0])
        self.cursor_y = int(parts[1])
        self.last_update = self._monotonic()


def make_cursor_provider():
    """Auto-detect compositor and return the right cursor provider."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    session_type = os.environ.get("XDG_SESSION_TYPE", "")

    if "KDE" in desktop and session_type == "wayland":
        svc = CursorDBusService()
        provider = KWinCursorProvider(svc)
        return provider

    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return HyprlandCursorProvider()

    # X11 session or unknown — try GDK pointer directly
    return X11CursorProvider()


# =========================================================================
# Sprite processing
# =========================================================================

def _process_sprite(path):
    """Load a sprite PNG, add black outline + fill interior holes.

    On the PC-9801, sprites were drawn on black VRAM — the black
    background provided the cat's visible outline and filled interior
    regions like the eyes.

    Handles both 32x32 original and legacy 128x128 (4x) sprites.
    Returns a 34x34 PIL RGBA image (32x32 + 1px padding for outline).
    """
    img = Image.open(path).convert("RGBA")
    if img.size != (32, 32):
        img = img.resize((32, 32), Image.NEAREST)

    # Pad to 34x34 so edge-touching pixels can get their outline
    padded = Image.new("RGBA", (34, 34), (0, 0, 0, 0))
    padded.paste(img, (1, 1))
    px = padded.load()

    colored = set()
    for y in range(34):
        for x in range(34):
            if px[x, y][3] > 0:
                colored.add((x, y))

    # Add 1px black outline (4-way adjacency)
    for cx, cy in colored:
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < 34 and 0 <= ny < 34 and px[nx, ny][3] == 0:
                px[nx, ny] = (0, 0, 0, 255)

    # Fill interior transparent pixels (eyes) with solid black.
    # Flood-fill from edges to find exterior; anything still transparent
    # after that is interior and gets filled black.
    exterior = set()
    queue = []
    for x in range(34):
        for y in (0, 33):
            if px[x, y][3] == 0:
                exterior.add((x, y))
                queue.append((x, y))
    for y in range(34):
        for x in (0, 33):
            if px[x, y][3] == 0 and (x, y) not in exterior:
                exterior.add((x, y))
                queue.append((x, y))
    while queue:
        cx, cy = queue.pop()
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < 34 and 0 <= ny < 34 and (nx, ny) not in exterior and px[nx, ny][3] == 0:
                exterior.add((nx, ny))
                queue.append((nx, ny))
    for y in range(34):
        for x in range(34):
            if px[x, y][3] == 0 and (x, y) not in exterior:
                px[x, y] = (0, 0, 0, 255)

    return padded


def _pil_to_pixbuf(pil_img, scale):
    """Scale a 34x34 PIL image by `scale` and convert to GdkPixbuf."""
    size = 34 * scale
    scaled = pil_img.resize((size, size), Image.NEAREST)
    buf = io.BytesIO()
    scaled.save(buf, format="PNG")
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(buf.getvalue())
    loader.close()
    return loader.get_pixbuf()


# =========================================================================
# Config
# =========================================================================

def load_config():
    defaults = {
        "tick_ms": DEFAULT_TICK_MS,
        "move_speed": DEFAULT_MOVE_SPEED,
        "scale": DEFAULT_SCALE,
        "deadzone": DEADZONE,
    }
    try:
        with open(CONFIG_PATH) as f:
            defaults.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    defaults["tick_ms"] = max(50, min(500, int(defaults["tick_ms"])))
    defaults["move_speed"] = max(1, min(8, int(defaults["move_speed"])))
    defaults["scale"] = max(1, min(8, int(defaults["scale"])))
    defaults["deadzone"] = max(0, min(128, int(defaults["deadzone"])))
    return defaults


def save_config(tick_ms, move_speed, scale, deadzone):
    with open(CONFIG_PATH, "w") as f:
        json.dump({"tick_ms": tick_ms, "move_speed": move_speed,
                   "scale": scale, "deadzone": deadzone}, f)


# =========================================================================
# Neko — the cat
# =========================================================================

class Neko:
    def __init__(self, cursor_provider, config):
        self.cursor = cursor_provider
        self.scale = config["scale"]

        # Process sprites to 34x34 PIL images (outline + interior fill)
        self._base_sprites = {}
        for st in range(7):
            for fr in range(4):
                path = os.path.join(
                    SPRITE_DIR, f"{st}_{_SPRITE_PREFIXES[st]}_f{fr}.png"
                )
                self._base_sprites[(st, fr)] = _process_sprite(path)

        # Convert to pixbufs at current scale
        self.sprites = {}
        self._rebuild_pixbufs()

        # --- Window setup ---
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_app_paintable(True)
        self.win.set_decorated(False)
        self.win.set_keep_above(True)
        self.win.set_accept_focus(False)
        self.win.set_default_size(self.sprite_w, self.sprite_h)
        self.win.set_type_hint(Gdk.WindowTypeHint.DOCK)

        screen = self.win.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.win.set_visual(visual)

        self.win.connect("draw", self._on_draw)
        self.win.connect("destroy", Gtk.main_quit)

        # Screen size (XWayland physical coordinates)
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geom = monitor.get_geometry()
        self.screen_w = geom.width
        self.screen_h = geom.height

        # --- Cat state (mirrors data segment in NEKO.asm at 0x0116) ---
        # Original starts at (0, 0) in state 0 (moving up)
        self.cat_x = 0
        self.cat_y = 0
        self.anim_state = ST_UP
        self.anim_frame = 0
        self.idle_counter = 0
        self.has_slept = False
        self.flag_wander = False
        self.scratch_state = ST_SCRATCH_RIGHT
        self.wander_target_x = 0
        self.wander_target_y = 0

        # Effective targets from compare functions (for movement clamping)
        self._eff_target_x = 0
        self._eff_target_y = 0

        # Speed settings
        self.tick_ms = config["tick_ms"]
        self.move_speed = config["move_speed"]
        self.deadzone = config["deadzone"]
        self._timer_id = None

        self.win.show_all()

        # Click-through
        self.win.get_window().input_shape_combine_region(
            cairo.Region(), 0, 0
        )

        self._update_position()
        self._restart_timer()

    def _rebuild_pixbufs(self):
        """Rebuild all pixbufs at the current scale."""
        for key, pil_img in self._base_sprites.items():
            self.sprites[key] = _pil_to_pixbuf(pil_img, self.scale)
        sample = self.sprites[(0, 0)]
        self.sprite_w = sample.get_width()
        self.sprite_h = sample.get_height()
        self.sprite_gw_minus1 = self.sprite_w // GRID - 1

    def set_scale(self, scale):
        self.scale = scale
        self._rebuild_pixbufs()
        self.win.resize(self.sprite_w, self.sprite_h)
        self.win.queue_draw()

    def _restart_timer(self):
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
        self._timer_id = GLib.timeout_add(self.tick_ms, self._tick)

    # -------------------------------------------------------------------
    # Drawing
    # -------------------------------------------------------------------
    def _on_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        pb = self.sprites[(self.anim_state, self.anim_frame)]
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        return True

    def _update_position(self):
        self.win.move(self.cat_x * GRID, self.cat_y * GRID)
        self.win.queue_draw()

    # -------------------------------------------------------------------
    # compare_y (0x0383): compare cursor Y to cat's top edge
    # -------------------------------------------------------------------
    def _compare_y(self, cursor_gy):
        self._eff_target_y = cursor_gy
        if cursor_gy == self.cat_y:
            return True, -1
        self.idle_counter = 0
        self.has_slept = False
        return (False, ST_UP) if cursor_gy < self.cat_y else (False, ST_DOWN)

    # -------------------------------------------------------------------
    # compare_x (0x03A7): compare cursor X to cat center
    # -------------------------------------------------------------------
    def _compare_x(self, cursor_gx):
        self._eff_target_x = cursor_gx
        cat_center_gx = self.cat_x + self.sprite_gw_minus1 // 2
        if cursor_gx == cat_center_gx:
            return True, -1
        self.idle_counter = 0
        self.has_slept = False
        return (False, ST_LEFT) if cursor_gx < cat_center_gx else (False, ST_RIGHT)

    # -------------------------------------------------------------------
    # decide_direction (0x03E0)
    # -------------------------------------------------------------------
    def _decide_direction(self, cursor_gx, cursor_gy):
        # Deadzone: if cat and cursor are close enough, treat as aligned
        cat_cx = self.cat_x * GRID + self.sprite_w // 2
        cat_cy = self.cat_y * GRID + self.sprite_h // 2
        in_deadzone = abs(cursor_gx * GRID - cat_cx) <= self.deadzone and \
                      abs(cursor_gy * GRID - cat_cy) <= self.deadzone

        # Update facing direction based on cursor position relative to cat center
        if cursor_gx * GRID > cat_cx:
            self.scratch_state = ST_SCRATCH_RIGHT
        else:
            self.scratch_state = ST_SCRATCH_LEFT

        if not in_deadzone:
            if random.getrandbits(1):
                ay, dy = self._compare_y(cursor_gy)
                if not ay:
                    self.anim_state = dy
                    return
                ax, dx = self._compare_x(cursor_gx)
                if not ax:
                    self.anim_state = dx
                    return
            else:
                ax, dx = self._compare_x(cursor_gx)
                if not ax:
                    self.anim_state = dx
                    return
                ay, dy = self._compare_y(cursor_gy)
                if not ay:
                    self.anim_state = dy
                    return

        # .both_aligned (0x040D)
        if self.flag_wander:
            self.flag_wander = False
            return

        if self.idle_counter < IDLE_THRESHOLD:
            self.idle_counter += 1
            self.anim_state = self.scratch_state
            return

        # .idle_timeout (0x0436)
        if self.has_slept:
            self._start_sleep()
            return

        if random.getrandbits(3) == 0:
            self._start_wander()
        else:
            self._start_sleep()

    def _start_sleep(self):
        self.flag_wander = False
        self.anim_state = ST_SLEEP
        self.has_slept = True

    def _start_wander(self):
        self.flag_wander = True
        self.wander_target_x = random.randint(0, self.screen_w // GRID - 1)
        self.wander_target_y = random.randint(0, self.screen_h // GRID - 1)

    # -------------------------------------------------------------------
    # advance_animation (0x0490)
    # -------------------------------------------------------------------
    def _advance_animation(self):
        self.anim_frame = (self.anim_frame + 1) & 3
        if self.anim_state >= ST_SCRATCH_RIGHT:
            return
        # Clamp movement so the cat can't overshoot the target
        s = self.move_speed
        # Target for cat_x is cursor_gx offset so cat center lands on cursor
        half_w = self.sprite_gw_minus1 // 2
        tx = self._eff_target_x - half_w
        ty = self._eff_target_y
        if self.anim_state == ST_UP:
            self.cat_y = max(self.cat_y - s, ty)
        elif self.anim_state == ST_DOWN:
            self.cat_y = min(self.cat_y + s, ty)
        elif self.anim_state == ST_RIGHT:
            self.cat_x = min(self.cat_x + s, tx)
        elif self.anim_state == ST_LEFT:
            self.cat_x = max(self.cat_x - s, tx)

    # -------------------------------------------------------------------
    # update_cat (0x04C8)
    # -------------------------------------------------------------------
    def _tick(self):
        if self.flag_wander:
            cursor_gx = self.wander_target_x
            cursor_gy = self.wander_target_y
        else:
            mx, my = self.cursor.get_position()
            if mx < 0 or my < 0 or mx > self.screen_w or my > self.screen_h:
                print(f"neko: bad cursor pos ({mx}, {my}), skipping",
                      file=sys.stderr)
                return True
            cursor_gx = mx // GRID
            cursor_gy = my // GRID

        self._decide_direction(cursor_gx, cursor_gy)
        self._advance_animation()

        max_gx = (self.screen_w - self.sprite_w) // GRID
        max_gy = (self.screen_h - self.sprite_h) // GRID
        self.cat_x = max(0, min(self.cat_x, max_gx))
        self.cat_y = max(0, min(self.cat_y, max_gy))

        self._update_position()
        return True


# =========================================================================
# Settings dialog
# =========================================================================

class NekoSettings(Gtk.Window):
    """Settings window with sliders, spin buttons, reset and save."""

    def __init__(self, neko):
        super().__init__(title="Neko Settings")
        self.neko = neko
        self.set_default_size(350, -1)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.connect("delete-event", lambda w, e: w.hide() or True)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(8)

        # --- Animation Speed (ms between frames) ---
        grid.attach(Gtk.Label(label="Animation Speed (ms)", xalign=0), 0, 0, 2, 1)
        self.anim_adj = Gtk.Adjustment(
            value=neko.tick_ms, lower=50, upper=500,
            step_increment=10, page_increment=50,
        )
        anim_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.anim_adj
        )
        anim_scale.set_inverted(True)
        anim_scale.set_draw_value(False)
        anim_scale.set_hexpand(True)
        anim_spin = Gtk.SpinButton(adjustment=self.anim_adj, climb_rate=1)
        anim_spin.set_width_chars(4)
        self.anim_adj.connect("value-changed", self._on_anim_changed)
        grid.attach(anim_scale, 0, 1, 1, 1)
        grid.attach(anim_spin, 1, 1, 1, 1)

        # --- Movement Speed (grid units per tick) ---
        grid.attach(Gtk.Label(label="Movement Speed", xalign=0), 0, 2, 2, 1)
        self.move_adj = Gtk.Adjustment(
            value=neko.move_speed, lower=1, upper=8,
            step_increment=1, page_increment=1,
        )
        move_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.move_adj
        )
        move_scale.set_draw_value(False)
        move_scale.set_hexpand(True)
        move_scale.set_digits(0)
        move_spin = Gtk.SpinButton(adjustment=self.move_adj, climb_rate=1)
        move_spin.set_digits(0)
        move_spin.set_width_chars(4)
        self.move_adj.connect("value-changed", self._on_move_changed)
        grid.attach(move_scale, 0, 3, 1, 1)
        grid.attach(move_spin, 1, 3, 1, 1)

        # --- Sprite Scale ---
        grid.attach(Gtk.Label(label="Sprite Scale", xalign=0), 0, 4, 2, 1)
        self.scale_adj = Gtk.Adjustment(
            value=neko.scale, lower=1, upper=8,
            step_increment=1, page_increment=1,
        )
        scale_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.scale_adj
        )
        scale_scale.set_draw_value(False)
        scale_scale.set_digits(0)
        scale_scale.set_hexpand(True)
        scale_spin = Gtk.SpinButton(adjustment=self.scale_adj, climb_rate=1)
        scale_spin.set_digits(0)
        scale_spin.set_width_chars(4)
        self.scale_adj.connect("value-changed", self._on_scale_changed)
        grid.attach(scale_scale, 0, 5, 1, 1)
        grid.attach(scale_spin, 1, 5, 1, 1)

        # --- Deadzone ---
        grid.attach(Gtk.Label(label="Deadzone", xalign=0), 0, 6, 2, 1)
        self.dz_adj = Gtk.Adjustment(
            value=neko.deadzone, lower=0, upper=128,
            step_increment=4, page_increment=16,
        )
        dz_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.dz_adj
        )
        dz_scale.set_draw_value(False)
        dz_scale.set_digits(0)
        dz_scale.set_hexpand(True)
        dz_spin = Gtk.SpinButton(adjustment=self.dz_adj, climb_rate=1)
        dz_spin.set_digits(0)
        dz_spin.set_width_chars(4)
        self.dz_adj.connect("value-changed", self._on_dz_changed)
        grid.attach(dz_scale, 0, 7, 1, 1)
        grid.attach(dz_spin, 1, 7, 1, 1)

        vbox.pack_start(grid, False, False, 0)

        # --- Buttons ---
        btn_box = Gtk.Box(spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        reset_btn = Gtk.Button(label="Reset")
        reset_btn.connect("clicked", self._on_reset)
        save_btn = Gtk.Button(label="Save")
        save_btn.connect("clicked", self._on_save)
        btn_box.pack_start(reset_btn, False, False, 0)
        btn_box.pack_start(save_btn, False, False, 0)
        vbox.pack_start(btn_box, False, False, 0)

        self.add(vbox)

    def _on_anim_changed(self, adj):
        self.neko.tick_ms = int(adj.get_value())
        self.neko._restart_timer()

    def _on_move_changed(self, adj):
        self.neko.move_speed = int(adj.get_value())

    def _on_scale_changed(self, adj):
        self.neko.set_scale(int(adj.get_value()))

    def _on_dz_changed(self, adj):
        self.neko.deadzone = int(adj.get_value())

    def _on_reset(self, _):
        self.anim_adj.set_value(DEFAULT_TICK_MS)
        self.move_adj.set_value(DEFAULT_MOVE_SPEED)
        self.scale_adj.set_value(DEFAULT_SCALE)
        self.dz_adj.set_value(DEADZONE)

    def _on_save(self, _):
        save_config(
            int(self.anim_adj.get_value()),
            int(self.move_adj.get_value()),
            int(self.scale_adj.get_value()),
            int(self.dz_adj.get_value()),
        )


# =========================================================================
# System tray
# =========================================================================

class NekoTray:
    """System tray icon with settings and quit."""

    def __init__(self, neko):
        self.neko = neko
        self._settings_win = None
        icon_path = os.path.join(SPRITE_DIR, "4_sit_right_f0.png")
        self.indicator = AppIndicator3.Indicator.new(
            "neko-desktop-pet",
            icon_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self._build_menu())

    def _build_menu(self):
        menu = Gtk.Menu()
        settings_item = Gtk.MenuItem(label="Settings")
        settings_item.connect("activate", self._on_settings)
        menu.append(settings_item)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _: Gtk.main_quit())
        menu.append(quit_item)
        menu.show_all()
        return menu

    def _on_settings(self, _):
        if self._settings_win is None:
            self._settings_win = NekoSettings(self.neko)
        self._settings_win.show_all()
        self._settings_win.present()


if __name__ == "__main__":
    DBusGMainLoop(set_as_default=True)
    config = load_config()
    cursor = make_cursor_provider()
    try:
        neko = Neko(cursor, config)
        tray = NekoTray(neko)
        Gtk.main()
    finally:
        cursor.cleanup()
