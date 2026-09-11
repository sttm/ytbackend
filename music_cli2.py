import os
import re
import sys
import shutil
import subprocess
import urllib.request
import json
from html import unescape
from html.parser import HTMLParser

from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, quote

from ytmusicapi import YTMusic
from yt_dlp import YoutubeDL


# ============================================================
# CONFIG
# ============================================================

env_vlc_dir = os.environ.get("MUSIC_CLI_VLC_DIR")

VLC_DIR = Path(
    env_vlc_dir if env_vlc_dir else Path.cwd()
).resolve()

DOWNLOAD_DIR = VLC_DIR / "Downloads"
ALBUMS_DIR = VLC_DIR / "Albums"

PLAYLIST_PATH = VLC_DIR / "all_music.m3u"
TELEGRAM_CHANNELS_PATH = VLC_DIR / "telegram_channels.json"
TELEGRAM_BOTS_PATH = VLC_DIR / ".music_cli_telegram_bots.json"

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".wav",
    ".ogg",
    ".opus",
    ".aiff",
    ".aif",
}

yt = YTMusic()


# ============================================================
# TERMINAL MODE
# ============================================================

_TTY_BASE_ATTRS = None


def _capture_tty_base():
    """Сохраняем нормальный режим терминала до включения cbreak."""
    global _TTY_BASE_ATTRS

    if not sys.stdin.isatty():
        return

    try:
        import termios
        _TTY_BASE_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        _TTY_BASE_ATTRS = None


def restore_text_terminal(flush_input=False):
    """Возвращает terminal в canonical + echo режим перед text_input().

    a-Shell иногда не полностью восстанавливает состояние после cbreak,
    поэтому сначала пробуем вернуть сохранённые атрибуты, а затем явно
    включаем ICANON/ECHO/ISIG и нормальную обработку Enter.
    """
    if not sys.stdin.isatty():
        return

    try:
        import termios
        fd = sys.stdin.fileno()

        if _TTY_BASE_ATTRS is not None:
            attrs = list(_TTY_BASE_ATTRS)
            attrs[6] = list(_TTY_BASE_ATTRS[6])
        else:
            attrs = termios.tcgetattr(fd)

        # input flags: CR -> NL
        attrs[0] |= getattr(termios, "ICRNL", 0)

        # output post-processing
        attrs[1] |= getattr(termios, "OPOST", 0)

        # local flags: canonical input + echo + signals
        attrs[3] |= (
            getattr(termios, "ICANON", 0)
            | getattr(termios, "ECHO", 0)
            | getattr(termios, "ISIG", 0)
        )

        # В canonical режиме эти значения обычно не критичны,
        # но оставляем безопасные значения.
        if hasattr(termios, "VMIN"):
            attrs[6][termios.VMIN] = 1
        if hasattr(termios, "VTIME"):
            attrs[6][termios.VTIME] = 0

        termios.tcsetattr(fd, termios.TCSANOW, attrs)

        if flush_input:
            try:
                termios.tcflush(fd, termios.TCIFLUSH)
            except Exception:
                pass

    except Exception:
        # Не ломаем fallback обычного input() на терминалах,
        # где termios частично недоступен.
        pass


def text_input(prompt="> "):
    """Текстовый ввод без Python input() на TTY.

    a-Shell нестабильно переключается из cbreak-навигации обратно в
    canonical input(). Поэтому на настоящем терминале читаем строку тем же
    низкоуровневым способом через os.read().

    Поддержка:
      - UTF-8 текст
      - Enter -> подтвердить
      - Backspace -> удалить символ
      - Esc -> отменить (возвращает пустую строку)
      - Ctrl-C -> KeyboardInterrupt
      - Ctrl-D на пустой строке -> EOF / пустая строка

    На не-TTY (pipe/IDE) остаётся обычный input().
    """
    disable_mouse_reporting()

    if not sys.stdin.isatty():
        return input(prompt)

    try:
        import termios
        import tty
        import codecs
    except Exception:
        return input(prompt)

    fd = sys.stdin.fileno()

    try:
        out_fd = sys.stdout.fileno()
    except Exception:
        out_fd = None

    try:
        old = termios.tcgetattr(fd)
    except Exception:
        return input(prompt)

    # Печатаем prompt до отключения echo.
    sys.stdout.write(prompt)
    sys.stdout.flush()

    chars = []
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    try:
        tty.setcbreak(fd)

        while True:
            try:
                data = os.read(fd, 1)
            except InterruptedError:
                continue

            if not data:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(chars)

            # Enter: a-Shell/macOS может прислать CR или LF.
            if data in (b"\r", b"\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(chars)

            # Esc = отмена текущего текстового ввода.
            if data == b"\x1b":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return ""

            # Ctrl-C.
            if data == b"\x03":
                sys.stdout.write("^C\n")
                sys.stdout.flush()
                raise KeyboardInterrupt

            # Ctrl-D: на пустой строке считаем отменой.
            if data == b"\x04" and not chars:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return ""

            # Backspace/Delete.
            if data in (b"\x7f", b"\x08"):
                if chars:
                    chars.pop()
                    # Стереть последний отображённый символ.
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue

            # UTF-8 может приходить несколькими байтами.
            text = decoder.decode(data, final=False)
            if not text:
                continue

            for ch in text:
                # Не добавляем управляющие символы.
                if ord(ch) < 32 and ch != "\t":
                    continue

                chars.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()

    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        except Exception:
            pass


_capture_tty_base()

try:
    import atexit
    atexit.register(disable_mouse_reporting)
except Exception:
    pass


# ============================================================
# HELPERS
# ============================================================

def safe_filename(value):
    if not value:
        return "Unknown"

    value = str(value).strip()

    value = re.sub(
        r'[\\/:*?"<>|]',
        "_",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value[:180]


def normalize_text(value):
    return re.sub(
        r"\s+",
        " ",
        str(value or "").strip().lower()
    )


def artist_string(track):
    artists = track.get("artists") or []

    names = []

    for artist in artists:
        if isinstance(artist, dict):
            name = artist.get("name")

            if name:
                names.append(name)

    if names:
        return ", ".join(names)

    return (
        track.get("artist")
        or track.get("uploader")
        or track.get("channel")
        or "Unknown Artist"
    )


def album_string(track):
    album = track.get("album")

    if isinstance(album, dict):
        return album.get("name") or ""

    if isinstance(album, str):
        return album

    return ""


def find_ffmpeg():
    return shutil.which("ffmpeg")


def is_audio_file(path):
    return (
        path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
    )




# ============================================================
# TERMINAL UI
# ============================================================

ANSI_CLEAR = "\033[2J\033[H"
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"

# a-Shell sends useful touch samples when these xterm modes are enabled.
ANSI_MOUSE_ON = (
    "\033[?1000h"   # button events
    "\033[?1002h"   # button motion
    "\033[?1003h"   # any motion
    "\033[?1006h"   # SGR coordinates
)
ANSI_MOUSE_OFF = (
    "\033[?1000l"
    "\033[?1002l"
    "\033[?1003l"
    "\033[?1006l"
)

# Selection style verified visually in a-Shell.
ANSI_SELECTED = "\033[47m\033[30m"

GESTURE_IDLE_TIMEOUT = 0.16
GESTURE_MAX_TIME = 1.2
GESTURE_TAP_THRESHOLD = 2
GESTURE_SWIPE_THRESHOLD = 4
DOUBLE_TAP_TIMEOUT = 1.2


def enable_mouse_reporting():
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(ANSI_MOUSE_ON)
        sys.stdout.flush()
    except Exception:
        pass


def disable_mouse_reporting():
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(ANSI_MOUSE_OFF)
        sys.stdout.flush()
    except Exception:
        pass


def clear_screen():
    print(ANSI_CLEAR, end="", flush=True)


def terminal_width():
    try:
        columns = shutil.get_terminal_size((48, 24)).columns
    except Exception:
        columns = 48
    # 48 is the minimum supported layout, not the maximum.
    return max(48, columns)


def terminal_height():
    try:
        lines = shutil.get_terminal_size((48, 24)).lines
    except Exception:
        lines = 24
    return max(12, lines)


def full_line(value, width):
    value = str(value or "")
    if len(value) > width:
        return value[:width]
    return value + " " * (width - len(value))


def fit_text(value, width):
    value = str(value or "")
    if width <= 0:
        return ""
    if len(value) <= width:
        return value + " " * (width - len(value))
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


def selected_line(value, width, selected=False):
    line = full_line(value, width)
    if selected:
        return ANSI_SELECTED + line + ANSI_RESET
    return line


def duration_text(item):
    value = item.get("duration") or ""
    if value:
        return str(value)
    seconds = item.get("duration_seconds") or item.get("durationSeconds")
    if seconds is None:
        return ""
    try:
        seconds = int(seconds)
        return f"{seconds // 60}:{seconds % 60:02d}"
    except Exception:
        return ""


def render_header(title, subtitle=None):
    width = terminal_width()
    print(ANSI_BOLD + fit_text(title, width).rstrip() + ANSI_RESET)
    if subtitle:
        print(ANSI_DIM + fit_text(subtitle, width).rstrip() + ANSI_RESET)
    print("─" * width)


def pause(message="Press any key to continue"):
    print()
    print(ANSI_DIM + message + ANSI_RESET)
    text_input("> ")


def prompt_text(title, label, subtitle=None):
    clear_screen()
    render_header(title, subtitle)
    print()
    print(label)
    print(ANSI_DIM + "Empty Enter or '..' = Back" + ANSI_RESET)
    print()
    value = text_input("> ").strip()
    if not value or value == "..":
        return None
    return value


def prompt_confirm(title, message, default=True, subtitle=None):
    clear_screen()
    render_header(title, subtitle)
    print()
    print(message)
    print()
    print(ANSI_DIM + ("[Y/n]" if default else "[y/N]") + ANSI_RESET)
    print()
    value = text_input("> ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def _read_byte(fd, timeout=None):
    import select
    if timeout is not None:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
    try:
        data = os.read(fd, 1)
    except Exception:
        return None
    return data or None


def _read_escape_sequence(fd):
    import time
    buf = bytearray(b"\x1b")
    deadline = time.monotonic() + 0.10
    while time.monotonic() < deadline:
        b = _read_byte(fd, 0.01)
        if not b:
            continue
        buf.extend(b)
        if b in b"mMABCD~":
            break
    return bytes(buf)


def _parse_mouse_sequence(seq):
    try:
        s = seq.decode("ascii", "ignore")
        if not s.startswith("\x1b[<"):
            return None

        button, x, y = [
            int(v)
            for v in s[3:-1].split(";")
        ]

        base_button = button & 0b11
        is_motion = bool(button & 32)
        is_wheel = bool(button & 64)

        return {
            "button": button,
            "base_button": base_button,
            "x": x,
            "y": y,
            "press": s.endswith("M"),
            "release": s.endswith("m"),
            "motion": is_motion,
            "wheel": is_wheel,

            # SGR mouse convention:
            # base_button == 3 together with motion means pointer move
            # with no mouse button held.
            "hover": (
                is_motion
                and base_button == 3
                and not is_wheel
            ),

            # Primary-button event or drag.
            "primary": (
                base_button == 0
                and not is_wheel
            ),
        }

    except Exception:
        return None


def _read_raw_event(fd, timeout=None):
    b = _read_byte(fd, timeout)
    if not b:
        return ("TIMEOUT", None)

    if b == b"\x03":
        return ("QUIT", None)

    if b in (b"\r", b"\n"):
        return ("ENTER", None)

    if b in (b"\x7f", b"\x08"):
        return ("BACKSPACE", None)

    if b == b"\x1b":
        seq = _read_escape_sequence(fd)

        if seq in (b"\x1b[A", b"\x1bOA"):
            return ("UP", None)
        if seq in (b"\x1b[B", b"\x1bOB"):
            return ("DOWN", None)
        if seq in (b"\x1b[C", b"\x1bOC"):
            return ("RIGHT", None)
        if seq in (b"\x1b[D", b"\x1bOD"):
            return ("LEFT", None)

        mouse = _parse_mouse_sequence(seq)
        if mouse:
            return ("MOUSE", mouse)

        if seq == b"\x1b":
            return ("ESC", None)

        return ("UNKNOWN", seq)

    try:
        return ("CHAR", b.decode("utf-8"))
    except Exception:
        return ("UNKNOWN", b)


def _collect_mouse_gesture(fd, first_mouse):
    """Collect a real click/touch gesture.

    Important:
    - ordinary mouse hover is NEVER part of a gesture;
    - a-Shell may send repeated primary press samples during a swipe;
    - a final release is useful but not required.
    """
    import time

    samples = [first_mouse]
    started = time.monotonic()
    last_sample = started

    while True:
        now = time.monotonic()

        if now - started >= GESTURE_MAX_TIME:
            break

        kind, data = _read_raw_event(
            fd,
            timeout=0.03
        )

        now = time.monotonic()

        if kind == "TIMEOUT":
            if (
                now - last_sample
                >= GESTURE_IDLE_TIMEOUT
            ):
                break
            continue

        if kind == "MOUSE" and data:
            # Plain pointer movement must never turn into
            # a TAP / double-TAP.
            if data.get("hover"):
                break

            # Only primary-button/touch samples belong
            # to the gesture.
            if not data.get("primary"):
                break

            samples.append(data)
            last_sample = now

            if data.get("release"):
                break

            continue

        break

    return samples


def _classify_mouse_gesture(samples):
    if not samples:
        return ("NONE", None)

    start = samples[0]
    end = samples[-1]
    dx = end["x"] - start["x"]
    dy = end["y"] - start["y"]

    info = {
        "x": end["x"],
        "y": end["y"],
        "start_x": start["x"],
        "start_y": start["y"],
        "end_x": end["x"],
        "end_y": end["y"],
        "dx": dx,
        "dy": dy,
        "samples": len(samples),
    }

    if (
        abs(dx) <= GESTURE_TAP_THRESHOLD
        and abs(dy) <= GESTURE_TAP_THRESHOLD
    ):
        return ("TAP", info)

    if (
        abs(dy) >= GESTURE_SWIPE_THRESHOLD
        and abs(dy) > abs(dx)
    ):
        return ("SWIPE_DOWN" if dy > 0 else "SWIPE_UP", info)

    if (
        abs(dx) >= GESTURE_SWIPE_THRESHOLD
        and abs(dx) > abs(dy)
    ):
        return ("SWIPE_RIGHT" if dx > 0 else "SWIPE_LEFT", info)

    return ("MOVE", info)


def read_menu_event():
    """Unified keyboard + touch event reader.

    Return examples:
      ("UP", None)
      ("ENTER", None)
      ("CHAR", "2")
      ("TAP", {"x":..., "y":...})
      ("SWIPE_UP", {...})
    """
    if not sys.stdin.isatty():
        return (None, None)

    try:
        import termios
        import tty
    except Exception:
        return (None, None)

    fd = sys.stdin.fileno()

    try:
        old = termios.tcgetattr(fd)
    except Exception:
        return (None, None)

    enable_mouse_reporting()

    try:
        tty.setraw(fd, termios.TCSANOW)

        kind, data = _read_raw_event(fd)

        if kind == "MOUSE" and data:
            # Normal mouse movement on macOS:
            # focus may follow the pointer, but this event
            # can NEVER activate an item.
            if data.get("hover"):
                return ("HOVER", data)

            # Ignore non-primary buttons here.
            if not data.get("primary"):
                return ("MOUSE_OTHER", data)

            # Real click/touch gesture.
            samples = _collect_mouse_gesture(
                fd,
                data
            )
            return _classify_mouse_gesture(
                samples
            )

        return (kind, data)

    finally:
        disable_mouse_reporting()
        try:
            restore_attrs = _TTY_BASE_ATTRS if _TTY_BASE_ATTRS is not None else old
            termios.tcsetattr(fd, termios.TCSANOW, restore_attrs)
        except Exception:
            pass


def _menu_entries_with_back(rows, back_label="Back"):
    return [
        {
            "_is_back": True,
            "number": "..",
            "left": f"← {back_label}",
            "middle": "",
            "right": "",
            "badge": "",
        }
    ] + [dict(row, _is_back=False) for row in rows]


def _activate_entry(entries, index):
    if entries[index].get("_is_back"):
        return None
    return index - 1


def _scrollbar_geometry(total, visible_count, scroll, max_scroll):
    if total <= visible_count or visible_count <= 0:
        return 0, visible_count

    thumb_size = max(1, round(visible_count * visible_count / total))
    track_space = max(1, visible_count - thumb_size)
    thumb_start = round((scroll / max(1, max_scroll)) * track_space)
    return thumb_start, thumb_size


def _tap_scrollbar(y, list_start_y, visible_count, max_scroll):
    relative_y = y - list_start_y
    if not (0 <= relative_y < visible_count) or max_scroll <= 0:
        return None
    ratio = relative_y / max(1, visible_count - 1)
    return max(0, min(max_scroll, round(ratio * max_scroll)))


def select_table(
    rows,
    title,
    subtitle=None,
    back_label="Back",
    initial=0,
    left_header="TITLE",
    middle_header="ALBUM / ARTIST",
    right_header="TIME",
    allow_zero_selection=False,
    require_double_tap=True,
):
    """Responsive table with a-Shell touch gestures and real hitboxes."""
    if not rows:
        return None

    import time

    entries = _menu_entries_with_back(rows, back_label)
    selected = max(0, min(initial + 1, len(entries) - 1))
    scroll = 0
    typed = ""

    last_tap_index = None
    last_tap_time = 0.0

    while True:
        clear_screen()
        width = terminal_width()
        height = terminal_height()
        render_header(title, subtitle)

        numeric_numbers = []
        for r in rows:
            try:
                numeric_numbers.append(int(r.get("number", 0)))
            except Exception:
                pass

        max_number = max(numeric_numbers or [0])
        num_w = max(4, len(str(max_number)) + 2)

        has_right = bool(right_header) or any(str(r.get("right") or "") for r in rows)
        has_middle = bool(middle_header) and any(str(r.get("middle") or "") for r in rows)

        right_w = 0
        if has_right:
            # Keep enough room for values such as 12:05
            # and leave visual breathing space before scrollbar.
            right_w = max(
                6,
                min(
                    12,
                    max(
                        [len(str(r.get("right") or "")) for r in rows]
                        + [len(right_header or "")]
                    ),
                ),
            )

        # Reserve space BEFORE calculating columns.
        #
        # The last three terminal columns are:
        #   "  " + scrollbar
        #
        # Previously left_w was calculated from full terminal width,
        # while rows were later clipped to width - 3. That pushed the
        # TIME / YEAR column beyond the visible content area.
        content_w = max(10, width - 3)

        # Wide screen: one-line rows with middle column.
        wide = width >= 72 and has_middle
        row_height = 1 if wide else (2 if has_middle else 1)

        if wide:
            middle_w = max(
                14,
                min(
                    28,
                    max(
                        [len(str(r.get("middle") or "")) for r in rows]
                        + [len(middle_header or "")]
                    ),
                ),
            )

            # num + spaces + left + middle + right must fit content_w.
            left_w = max(
                8,
                content_w
                - num_w
                - middle_w
                - right_w
                - 3
            )

        else:
            middle_w = 0

            # Narrow layout:
            # [number] + space + title + space + TIME/YEAR
            left_w = max(
                8,
                content_w
                - num_w
                - right_w
                - 2
            )

        # Header rows actually rendered:
        table_header_y = 4 if subtitle else 3
        list_start_y = table_header_y + 2

        header = fit_text("#", num_w) + " " + fit_text(left_header, left_w)
        if wide:
            header += " " + fit_text(middle_header, middle_w)
        if has_right:
            header += " " + str(right_header or "").rjust(right_w)

        print(
            ANSI_DIM
            + full_line(header, content_w)
            + "   "
            + ANSI_RESET
        )
        print("─" * width)

        footer_lines = 2
        used_before_list = list_start_y - 1
        available_physical_lines = max(1, height - used_before_list - footer_lines)
        visible_count = max(1, available_physical_lines // row_height)
        visible_count = min(visible_count, len(entries))

        if selected < scroll:
            scroll = selected
        elif selected >= scroll + visible_count:
            scroll = selected - visible_count + 1

        max_scroll = max(0, len(entries) - visible_count)
        scroll = max(0, min(scroll, max_scroll))

        view_end = min(len(entries), scroll + visible_count)

        thumb_start, thumb_size = _scrollbar_geometry(
            len(entries), visible_count, scroll, max_scroll
        )

        hitboxes = []
        current_y = list_start_y

        for visible_local, idx in enumerate(range(scroll, view_end)):
            row = entries[idx]
            is_back = row.get("_is_back", False)

            number = row.get("number", "")
            number_text = f"[{number}]"
            left = str(row.get("left") or "")
            badge = str(row.get("badge") or "")
            if badge and not is_back:
                left = f"{left} {badge}"

            right = str(row.get("right") or "")
            middle = "" if is_back else str(row.get("middle") or "")

            if wide:
                line = fit_text(number_text, num_w) + " " + fit_text(left, left_w)
                line += " " + fit_text(middle, middle_w)
                if has_right:
                    line += " " + right.rjust(right_w)

                sb = "█" if thumb_start <= visible_local < thumb_start + thumb_size else "│"
                print(selected_line(line, content_w, idx == selected) + "  " + sb)

                hitboxes.append({"index": idx, "y1": current_y, "y2": current_y})
                current_y += 1

            else:
                line1 = fit_text(number_text, num_w) + " " + fit_text(left, left_w)
                if has_right:
                    line1 += " " + right.rjust(right_w)

                sb1 = "█" if thumb_start <= visible_local < thumb_start + thumb_size else "│"
                print(selected_line(line1, content_w, idx == selected) + "  " + sb1)

                y1 = current_y
                current_y += 1

                if has_middle:
                    detail = (" " * (num_w + 1)) + fit_text(middle, max(1, content_w - num_w - 1))
                    print(selected_line(detail, content_w, idx == selected) + "  " + sb1)
                    y2 = current_y
                    current_y += 1
                else:
                    y2 = y1

                hitboxes.append({"index": idx, "y1": y1, "y2": y2})

        print("─" * width)
        typed_info = f" input:{typed}" if typed else ""
        tap_hint = "tap×2" if require_double_tap else "tap"
        hint = (
            f"↑↓ • {tap_hint} • swipe • Enter • number • .. Back"
            f"  {selected + 1}/{len(entries)}{typed_info}"
        )
        print(ANSI_DIM + full_line(fit_text(hint, width).rstrip(), width) + ANSI_RESET)

        kind, data = read_menu_event()

        if kind is None:
            print()
            print("Number or '..' for Back")
            value = text_input("> ").strip()
            if not value or value == "..":
                return None
            if value.isdigit():
                n = int(value)
                for idx, row in enumerate(rows):
                    try:
                        if int(row.get("number", idx + 1)) == n:
                            return idx
                    except Exception:
                        continue
            continue

        if kind == "UP":
            selected = max(0, selected - 1)
            typed = ""
            last_tap_index = None
            continue

        if kind == "DOWN":
            selected = min(len(entries) - 1, selected + 1)
            typed = ""
            last_tap_index = None
            continue

        if kind == "SWIPE_UP":
            amount = max(3, min(visible_count, abs(int(data.get("dy", -3)))))
            selected = min(len(entries) - 1, selected + amount)
            typed = ""
            last_tap_index = None
            continue

        if kind == "SWIPE_DOWN":
            amount = max(3, min(visible_count, abs(int(data.get("dy", 3)))))
            selected = max(0, selected - amount)
            typed = ""
            last_tap_index = None
            continue

        if kind == "SWIPE_RIGHT":
            return None

        if kind == "HOVER":
            x = int(data.get("x", 0))
            y = int(data.get("y", 0))

            # Hover changes focus only.
            # It NEVER arms or activates double-tap.
            hovered = None

            for box in hitboxes:
                if box["y1"] <= y <= box["y2"]:
                    hovered = box["index"]
                    break

            if hovered is not None:
                selected = hovered

            typed = ""
            last_tap_index = None
            continue

        if kind == "TAP":
            x = int(data.get("x", 0))
            y = int(data.get("y", 0))

            # Clickable scrollbar on the two rightmost columns.
            if x >= width - 1:
                new_scroll = _tap_scrollbar(y, list_start_y, visible_count * row_height, max_scroll)
                if new_scroll is not None:
                    scroll = new_scroll
                    selected = scroll
                    typed = ""
                    last_tap_index = None
                continue

            clicked = None
            for box in hitboxes:
                if box["y1"] <= y <= box["y2"]:
                    clicked = box["index"]
                    break

            if clicked is None:
                last_tap_index = None
                continue

            # Ordinary menus activate on one real tap/click.
            # Long result tables keep the safer two-tap model:
            # first tap selects, second tap opens.
            if not require_double_tap:
                selected = clicked
                typed = ""
                last_tap_index = None
                return _activate_entry(entries, clicked)

            now = time.monotonic()

            if (
                clicked == selected
                and last_tap_index == clicked
                and now - last_tap_time <= DOUBLE_TAP_TIMEOUT
            ):
                return _activate_entry(entries, clicked)

            selected = clicked
            last_tap_index = clicked
            last_tap_time = now
            typed = ""
            continue

        if kind in ("ESC", "QUIT"):
            return None

        if kind == "BACKSPACE":
            typed = typed[:-1]
            continue

        if kind == "ENTER":
            last_tap_index = None

            if typed:
                if typed == "..":
                    return None

                if typed.isdigit():
                    n = int(typed)
                    typed = ""

                    for idx, row in enumerate(rows):
                        try:
                            if int(row.get("number", idx + 1)) == n:
                                return idx
                        except Exception:
                            continue
            else:
                return _activate_entry(entries, selected)

            continue

        if kind == "CHAR" and isinstance(data, str):
            if data.isdigit():
                if not typed.startswith("."):
                    typed += data
            elif data == ".":
                typed = (typed + ".")[-2:]


def select_album_table(
    rows,
    meta,
    back_label="Back",
    initial=0,
    allow_zero_selection=False,
):
    """Compact album/playlist list: number + title + time."""
    if not rows:
        return None

    import time

    entries = _menu_entries_with_back(rows, back_label)
    selected = max(0, min(initial + 1, len(entries) - 1))
    scroll = 0
    typed = ""

    last_tap_index = None
    last_tap_time = 0.0

    while True:
        clear_screen()
        width = terminal_width()
        height = terminal_height()

        print(ANSI_BOLD + fit_text(meta, width).rstrip() + ANSI_RESET)
        print("─" * width)

        numeric_numbers = []
        for r in rows:
            try:
                numeric_numbers.append(int(r.get("number", 0)))
            except Exception:
                pass

        max_number = max(numeric_numbers or [0])
        num_w = max(4, len(f"[{max_number}]"))
        # 6 columns safely fits m:ss and mm:ss.
        time_w = 6
        # Two spaces + scrollbar are kept outside content.
        content_w = max(10, width - 3)
        title_w = max(8, content_w - num_w - time_w - 2)

        header = (
            fit_text("#", num_w)
            + " "
            + fit_text("TITLE", title_w)
            + " "
            + "TIME".rjust(time_w)
        )
        print(ANSI_DIM + full_line(header, content_w) + "  " + ANSI_RESET)
        print("─" * width)

        list_start_y = 4
        footer_lines = 2
        visible_count = max(1, height - (list_start_y - 1) - footer_lines)
        visible_count = min(visible_count, len(entries))

        if selected < scroll:
            scroll = selected
        elif selected >= scroll + visible_count:
            scroll = selected - visible_count + 1

        max_scroll = max(0, len(entries) - visible_count)
        scroll = max(0, min(scroll, max_scroll))
        view_end = min(len(entries), scroll + visible_count)

        thumb_start, thumb_size = _scrollbar_geometry(
            len(entries), visible_count, scroll, max_scroll
        )

        hitboxes = []

        for local, idx in enumerate(range(scroll, view_end)):
            row = entries[idx]
            is_back = row.get("_is_back", False)

            number_text = "[..]" if is_back else f"[{row.get('number', '')}]"
            title = str(row.get("left") or "")

            badge = str(row.get("badge") or "")
            if badge and not is_back:
                title = f"{title} {badge}"

            right = "" if is_back else str(row.get("right") or "")

            line = (
                fit_text(number_text, num_w)
                + " "
                + fit_text(title, title_w)
                + " "
                + right.rjust(time_w)
            )

            sb = "█" if thumb_start <= local < thumb_start + thumb_size else "│"
            print(selected_line(line, content_w, idx == selected) + "  " + sb)

            hitboxes.append(
                {
                    "index": idx,
                    "y1": list_start_y + local,
                    "y2": list_start_y + local,
                }
            )

        print("─" * width)
        typed_info = f" input:{typed}" if typed else ""
        hint = (
            f"↑↓ • tap×2 • swipe • Enter • number • .. Back"
            f"  {selected + 1}/{len(entries)}{typed_info}"
        )
        print(ANSI_DIM + full_line(fit_text(hint, width).rstrip(), width) + ANSI_RESET)

        kind, data = read_menu_event()

        if kind is None:
            value = text_input("> ").strip()
            if not value or value == "..":
                return None
            if value.isdigit():
                n = int(value)
                for idx, row in enumerate(rows):
                    try:
                        if int(row.get("number", idx + 1)) == n:
                            return idx
                    except Exception:
                        continue
            continue

        if kind == "UP":
            selected = max(0, selected - 1)
            typed = ""
            last_tap_index = None
            continue

        if kind == "DOWN":
            selected = min(len(entries) - 1, selected + 1)
            typed = ""
            last_tap_index = None
            continue

        if kind == "SWIPE_UP":
            amount = max(3, min(visible_count, abs(int(data.get("dy", -3)))))
            selected = min(len(entries) - 1, selected + amount)
            typed = ""
            last_tap_index = None
            continue

        if kind == "SWIPE_DOWN":
            amount = max(3, min(visible_count, abs(int(data.get("dy", 3)))))
            selected = max(0, selected - amount)
            typed = ""
            last_tap_index = None
            continue

        if kind == "SWIPE_RIGHT":
            return None

        if kind == "HOVER":
            x = int(data.get("x", 0))
            y = int(data.get("y", 0))

            # Hover changes focus only.
            # It NEVER arms or activates double-tap.
            hovered = None

            for box in hitboxes:
                if box["y1"] <= y <= box["y2"]:
                    hovered = box["index"]
                    break

            if hovered is not None:
                selected = hovered

            typed = ""
            last_tap_index = None
            continue

        if kind == "TAP":
            x = int(data.get("x", 0))
            y = int(data.get("y", 0))

            if x >= width - 1:
                new_scroll = _tap_scrollbar(y, list_start_y, visible_count, max_scroll)
                if new_scroll is not None:
                    scroll = new_scroll
                    selected = scroll
                    typed = ""
                    last_tap_index = None
                continue

            clicked = None
            for box in hitboxes:
                if box["y1"] <= y <= box["y2"]:
                    clicked = box["index"]
                    break

            if clicked is None:
                last_tap_index = None
                continue

            now = time.monotonic()

            if (
                clicked == selected
                and last_tap_index == clicked
                and now - last_tap_time <= DOUBLE_TAP_TIMEOUT
            ):
                return _activate_entry(entries, clicked)

            selected = clicked
            last_tap_index = clicked
            last_tap_time = now
            typed = ""
            continue

        if kind in ("ESC", "QUIT"):
            return None

        if kind == "BACKSPACE":
            typed = typed[:-1]
            continue

        if kind == "ENTER":
            last_tap_index = None

            if typed:
                if typed == "..":
                    return None

                if typed.isdigit():
                    n = int(typed)
                    typed = ""

                    for idx, row in enumerate(rows):
                        try:
                            if int(row.get("number", idx + 1)) == n:
                                return idx
                        except Exception:
                            continue
            else:
                return _activate_entry(entries, selected)

            continue

        if kind == "CHAR" and isinstance(data, str):
            if data.isdigit():
                if not typed.startswith("."):
                    typed += data
            elif data == ".":
                typed = (typed + ".")[-2:]


def select_simple_menu(title, options, subtitle=None, back_label="Back"):
    rows = [
        {
            "number": n,
            "left": label,
            "middle": desc,
            "right": "",
        }
        for n, label, desc in options
    ]

    idx = select_table(
        rows,
        title,
        subtitle=subtitle,
        back_label=back_label,
        left_header="SECTION",
        middle_header="DESCRIPTION",
        right_header="",
        require_double_tap=False,
    )

    if idx is None:
        return "BACK"

    return str(rows[idx]["number"])


def choose_media_action(title, subtitle=None):
    return select_simple_menu(
        title,
        [
            (1, "Play", "open audio stream in VLC"),
            (2, "Download", "save M4A with metadata and square artwork"),
        ],
        subtitle=subtitle,
        back_label="Back",
    )


def get_video_type(track):
    video_type = (
        track.get("videoType")
        or ""
    )

    prefix = "MUSIC_VIDEO_TYPE_"

    if video_type.startswith(prefix):
        return video_type[len(prefix):]

    return video_type or None


# ============================================================
# RESOLVE REAL ALBUM TRACK
# ============================================================

def resolve_album_track_video_id(track):
    """
    Строгий resolver альбомного трека.

    Приоритет:
    1. Исходный ATV.
    2. Тот же track index из audioPlaylistId альбома.
    3. Строгий поиск по title/artist/album/duration/explicit.
    4. Если уверенного совпадения нет — пропускаем.
    """

    original_video_id = track.get(
        "videoId"
    )

    if not original_video_id:
        return None

    video_type = get_video_type(
        track
    )

    # Уже настоящий Art Track.
    if video_type == "ATV":
        print(
            "   ✓ Исходный трек уже ATV"
        )
        return original_video_id

    target_title = normalize_text(
        track.get("title")
    )

    target_album = normalize_text(
        album_string(track)
    )

    target_explicit = track.get(
        "isExplicit"
    )

    target_duration = (
        track.get("duration_seconds")
        or track.get("durationSeconds")
    )

    target_artist_ids = {
        artist.get("id")
        for artist in (
            track.get("artists")
            or []
        )
        if isinstance(artist, dict)
        and artist.get("id")
    }

    target_artist_names = {
        normalize_text(
            artist.get("name")
        )
        for artist in (
            track.get("artists")
            or []
        )
        if isinstance(artist, dict)
        and artist.get("name")
    }

    album_playlist_id = (
        track.get(
            "_album_audio_playlist_id"
        )
    )

    album_index = track.get(
        "_album_index"
    )

    print(
        f"   ⚠️ source type: "
        f"{video_type or 'unknown'}"
    )

    # ========================================================
    # 1. ALBUM AUDIO PLAYLIST
    # ========================================================

    if (
        album_playlist_id
        and album_index is not None
    ):

        print(
            "   🔍 Проверяю audio playlist альбома..."
        )

        try:

            playlist = yt.get_playlist(
                album_playlist_id,
                limit=None
            )

            playlist_tracks = (
                playlist.get("tracks")
                or []
            )

            if (
                0 <= album_index
                < len(playlist_tracks)
            ):

                candidate = (
                    playlist_tracks[
                        album_index
                    ]
                )

                if candidate:

                    candidate_id = (
                        candidate.get(
                            "videoId"
                        )
                    )

                    candidate_type = (
                        get_video_type(
                            candidate
                        )
                    )

                    candidate_title = (
                        normalize_text(
                            candidate.get(
                                "title"
                            )
                        )
                    )

                    candidate_explicit = (
                        candidate.get(
                            "isExplicit"
                        )
                    )

                    candidate_duration = (
                        candidate.get(
                            "duration_seconds"
                        )
                        or candidate.get(
                            "durationSeconds"
                        )
                    )

                    candidate_artist_ids = {
                        artist.get("id")
                        for artist in (
                            candidate.get(
                                "artists"
                            )
                            or []
                        )
                        if isinstance(
                            artist,
                            dict
                        )
                        and artist.get("id")
                    }

                    candidate_artist_names = {
                        normalize_text(
                            artist.get(
                                "name"
                            )
                        )
                        for artist in (
                            candidate.get(
                                "artists"
                            )
                            or []
                        )
                        if isinstance(
                            artist,
                            dict
                        )
                        and artist.get(
                            "name"
                        )
                    }

                    valid = True

                    # Только ATV.
                    if (
                        candidate_type
                        != "ATV"
                    ):
                        valid = False

                    # Название обязательно совпадает.
                    if (
                        candidate_title
                        != target_title
                    ):
                        valid = False

                    # Explicit/Clean обязательно совпадает,
                    # если оба значения известны.
                    if (
                        target_explicit
                        is not None
                        and candidate_explicit
                        is not None
                        and candidate_explicit
                        != target_explicit
                    ):
                        valid = False

                    # Длительность.
                    if (
                        target_duration
                        and candidate_duration
                    ):
                        try:
                            diff = abs(
                                int(
                                    target_duration
                                )
                                - int(
                                    candidate_duration
                                )
                            )

                            if diff > 3:
                                valid = False

                        except Exception:
                            valid = False

                    # Artist.
                    if (
                        target_artist_ids
                        and candidate_artist_ids
                    ):

                        if not (
                            target_artist_ids
                            & candidate_artist_ids
                        ):
                            valid = False

                    elif (
                        target_artist_names
                        and candidate_artist_names
                    ):

                        if not (
                            target_artist_names
                            & candidate_artist_names
                        ):
                            valid = False

                    if (
                        valid
                        and candidate_id
                    ):

                        print(
                            "   ✓ Найден точный "
                            "трек в album audio playlist"
                        )

                        print(
                            f"   ATV: {candidate_id}"
                        )

                        return candidate_id

                    else:

                        print(
                            "   ⚠️ Трек по позиции "
                            "не прошёл проверку."
                        )

        except Exception as e:

            print(
                f"   ⚠️ audio playlist: {e}"
            )

    # ========================================================
    # 2. STRICT SEARCH
    # ========================================================

    print(
        "   🔎 Выполняю строгий поиск..."
    )

    artist_text = artist_string(
        track
    )

    query = " ".join(
        value
        for value in (
            track.get("title"),
            artist_text,
            album_string(track),
        )
        if value
    )

    try:

        results = yt.search(
            query,
            filter="songs",
            limit=25,
            ignore_spelling=True
        )

    except Exception as e:

        print(
            f"   ❌ Search: {e}"
        )

        return None

    valid_candidates = []

    for candidate in results:

        candidate_id = (
            candidate.get(
                "videoId"
            )
        )

        if not candidate_id:
            continue

        if (
            get_video_type(
                candidate
            )
            != "ATV"
        ):
            continue

        candidate_title = (
            normalize_text(
                candidate.get(
                    "title"
                )
            )
        )

        # Название — строго.
        if candidate_title != target_title:
            continue

        candidate_explicit = (
            candidate.get(
                "isExplicit"
            )
        )

        if (
            target_explicit
            is not None
            and candidate_explicit
            is not None
            and candidate_explicit
            != target_explicit
        ):
            continue

        candidate_album_data = (
            candidate.get("album")
            or {}
        )

        candidate_album_name = (
            normalize_text(
                candidate_album_data.get(
                    "name"
                )
                if isinstance(
                    candidate_album_data,
                    dict
                )
                else candidate_album_data
            )
        )

        # Альбом тоже должен совпасть.
        if (
            target_album
            and candidate_album_name
            != target_album
        ):
            continue

        candidate_duration = (
            candidate.get(
                "duration_seconds"
            )
            or candidate.get(
                "durationSeconds"
            )
        )

        if (
            target_duration
            and candidate_duration
        ):

            try:

                diff = abs(
                    int(target_duration)
                    - int(
                        candidate_duration
                    )
                )

                if diff > 3:
                    continue

            except Exception:
                continue

        candidate_artist_ids = {
            artist.get("id")
            for artist in (
                candidate.get(
                    "artists"
                )
                or []
            )
            if isinstance(
                artist,
                dict
            )
            and artist.get("id")
        }

        candidate_artist_names = {
            normalize_text(
                artist.get("name")
            )
            for artist in (
                candidate.get(
                    "artists"
                )
                or []
            )
            if isinstance(
                artist,
                dict
            )
            and artist.get(
                "name"
            )
        }

        if (
            target_artist_ids
            and candidate_artist_ids
        ):

            if not (
                target_artist_ids
                & candidate_artist_ids
            ):
                continue

        elif (
            target_artist_names
            and candidate_artist_names
        ):

            if not (
                target_artist_names
                & candidate_artist_names
            ):
                continue

        valid_candidates.append(
            candidate
        )

    # ========================================================
    # ONLY ONE EXACT RESULT
    # ========================================================

    if len(
        valid_candidates
    ) == 1:

        candidate = (
            valid_candidates[0]
        )

        candidate_id = (
            candidate["videoId"]
        )

        print(
            "   ✓ Найден единственный "
            "точный ATV"
        )

        print(
            f"   ATV: {candidate_id}"
        )

        return candidate_id

    if len(valid_candidates) > 1:

        print(
            f"   ⚠️ Найдено "
            f"{len(valid_candidates)} "
            "подходящих ATV."
        )

        def candidate_rank(candidate):
            # Чем меньше tuple, тем лучше.

            # 1. Разница по длительности.
            candidate_duration = (
                candidate.get("duration_seconds")
                or candidate.get("durationSeconds")
            )

            duration_diff = 999999

            try:
                if (
                    target_duration
                    and candidate_duration
                ):
                    duration_diff = abs(
                        int(target_duration)
                        - int(candidate_duration)
                    )
            except Exception:
                pass

            # 2. Explicit/Clean.
            candidate_explicit = candidate.get(
                "isExplicit"
            )

            explicit_penalty = 0

            if (
                target_explicit is not None
                and candidate_explicit is not None
            ):
                explicit_penalty = (
                    0
                    if candidate_explicit == target_explicit
                    else 1000
                )

            # 3. Album exact match.
            candidate_album = normalize_text(
                album_string(candidate)
            )

            album_penalty = (
                0
                if candidate_album == target_album
                else 100
            )

            # 4. Стабильный fallback.
            video_id = candidate.get(
                "videoId"
            ) or ""

            return (
                explicit_penalty,
                duration_diff,
                album_penalty,
                video_id,
            )

        valid_candidates.sort(
            key=candidate_rank
        )

        best = valid_candidates[0]

        candidate_id = best.get(
            "videoId"
        )

        print(
            "   ✓ Выбираю лучший ATV"
        )

        print(
            f"   ATV: {candidate_id}"
        )

        print(
            f"   Title: "
            f"{best.get('title', '')}"
        )

        print(
            f"   Duration: "
            f"{best.get('duration_seconds') or best.get('durationSeconds') or '?'}"
        )

        print(
            f"   Explicit: "
            f"{best.get('isExplicit')}"
        )

        return candidate_id

    # ========================================================
    # NO SAFE ATV MATCH
    # ========================================================

    print(
        "   ❌ Точный ATV не найден."
    )

    # Последний fallback:
    # если исходный трек был OMV/UGC,
    # всё равно используем его, чтобы не пропускать трек.

    if video_type in (
        "OMV",
        "UGC",
    ):

        print(
            f"   ⚠️ Использую исходный "
            f"{video_type} как fallback."
        )

        print(
            f"   videoId: {original_video_id}"
        )

        return original_video_id

    # Если тип неизвестный —
    # тоже используем исходный ID.

    if original_video_id:

        print(
            "   ⚠️ Использую исходный videoId."
        )

        return original_video_id

    return None


# ============================================================
# VLC
# ============================================================

def open_vlc():
    try:

        subprocess.run(
            [
                "open",
                "vlc://"
            ],
            check=False
        )

        return True

    except Exception as e:

        print(
            f"❌ VLC: {e}"
        )

        return False


def open_stream_in_vlc(stream_url):
    if not stream_url:
        return False

    try:

        subprocess.run(
            [
                "open",
                "vlc://" + stream_url
            ],
            check=False
        )

        return True

    except Exception as e:

        print(
            f"❌ VLC: {e}"
        )

        return False


# ============================================================
# LOCAL LIBRARY
# ============================================================

def scan_local_music():
    tracks = []

    try:

        for path in VLC_DIR.rglob("*"):

            if not path.is_file():
                continue

            try:

                relative = path.relative_to(
                    VLC_DIR
                )

            except ValueError:
                continue

            if any(
                part.startswith(".")
                for part in relative.parts
            ):
                continue

            if is_audio_file(path):
                tracks.append(path)

    except Exception as e:

        print(
            f"❌ Ошибка сканирования: {e}"
        )

        return []

    tracks.sort(
        key=lambda p: str(p).lower()
    )

    return tracks


# ============================================================
# GLOBAL PLAYLIST
# ============================================================

def create_m3u(tracks):
    try:

        with PLAYLIST_PATH.open(
            "w",
            encoding="utf-8",
            newline="\n"
        ) as file:

            file.write(
                "#EXTM3U\n"
            )

            for track in tracks:

                try:

                    relative = (
                        track.relative_to(
                            VLC_DIR
                        )
                    )

                except ValueError:
                    continue

                file.write(
                    relative.as_posix()
                    + "\n"
                )

        return True

    except Exception as e:

        print(
            f"❌ Playlist error: {e}"
        )

        return False


# ============================================================
# ALBUM PLAYLIST
# ============================================================

def create_album_playlist(
    album_dir,
    artist=None,
    album_title=None
):
    tracks = [
        path

        for path in album_dir.rglob("*")

        if is_audio_file(
            path
        )
    ]

    tracks.sort(
        key=lambda p: str(p).lower()
    )

    if not tracks:
        return False

    if artist and album_title:

        playlist_name = (
            safe_filename(
                f"{artist} - {album_title}"
            )
            + ".m3u"
        )

    else:

        playlist_name = (
            safe_filename(
                album_dir.name
            )
            + ".m3u"
        )

    playlist_path = (
        album_dir
        / playlist_name
    )

    try:

        with playlist_path.open(
            "w",
            encoding="utf-8",
            newline="\n"
        ) as file:

            file.write(
                "#EXTM3U\n"
            )

            for track in tracks:

                relative = (
                    track.relative_to(
                        album_dir
                    )
                )

                file.write(
                    relative.as_posix()
                    + "\n"
                )

        return True

    except Exception as e:

        print(
            f"❌ Album playlist: {e}"
        )

        return False


def create_album_playlists():
    ALBUMS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    count = 0

    for album_dir in sorted(
        ALBUMS_DIR.iterdir(),
        key=lambda p: p.name.lower()
    ):

        if not album_dir.is_dir():
            continue

        if create_album_playlist(
            album_dir
        ):

            count += 1

    return count


def rebuild_playlist_silent():
    tracks = scan_local_music()

    if tracks:

        create_m3u(
            tracks
        )

    create_album_playlists()


# ============================================================
# PLAYLIST MODE
# ============================================================

def playlist_mode():
    clear_screen()
    render_header("PLAYLISTS", subtitle="Scanning local VLC library")

    tracks = scan_local_music()
    if not tracks:
        print("No music found.")
        return

    create_m3u(tracks)
    album_count = create_album_playlists()

    width = terminal_width()
    rows = [
        ("Tracks", str(len(tracks))),
        ("Main playlist", "all_music.m3u"),
        ("Album playlists", str(album_count)),
    ]
    label_w = min(22, max(len(r[0]) for r in rows) + 2)
    print()
    for label, value in rows:
        value_w = max(1, width - label_w - 1)
        print(fit_text(label, label_w) + " " + fit_text(value, value_w).rstrip())
    print("─" * width)
    print(ANSI_DIM + fit_text("VLC opened; playlists updated", width).rstrip() + ANSI_RESET)
    open_vlc()


# ============================================================
# SEARCH TYPE
# ============================================================

def choose_search_type():
    print()
    print(
        "Что ищем?"
    )

    print()
    print(
        "[1] Трек"
    )

    print(
        "[2] Альбом"
    )

    print(
        "[0] Назад"
    )

    print()
    print("Выбор")
    choice = text_input("> ").strip()

    if choice == "1":
        return "song"

    if choice == "2":
        return "album"

    return None


# ============================================================
# SEARCH SONGS → PLAY LOOP
# ============================================================

def search_songs_play_loop():
    query = prompt_text(
        "SEARCH → PLAY / TRACKS",
        "Search track / artist"
    )
    if not query:
        return

    clear_screen()
    print("Searching tracks…")
    try:
        results = yt.search(query, filter="songs", limit=10)
    except Exception as e:
        print(f"❌ Search: {e}")
        pause()
        return
    if not results:
        print("❌ Nothing found.")
        pause()
        return

    selected = 0
    while True:
        rows = []
        for i, track in enumerate(results, start=1):
            rows.append({
                "number": i,
                "left": f"{artist_string(track)} — {track.get('title', 'Unknown')}",
                "middle": album_string(track),
                "right": duration_text(track),
                "badge": "[E]" if track.get("isExplicit") is True else "",
            })
        idx = select_table(
            rows, "SEARCH → PLAY / TRACKS", subtitle=f"Search: {query}",
            initial=selected, left_header="ARTIST — TRACK",
            middle_header="ALBUM", right_header="TIME", back_label="Back"
        )
        if idx is None:
            return
        selected = idx
        play_track(results[idx])

def search_songs():
    query = prompt_text(
        "SEARCH → DOWNLOAD / TRACK",
        "Search track / artist"
    )
    if not query:
        return None

    clear_screen()
    print("Ищу треки…")
    try:
        results = yt.search(query, filter="songs", limit=10)
    except Exception as e:
        print(f"❌ Search: {e}")
        pause()
        return None
    if not results:
        print("❌ Ничего не найдено.")
        pause()
        return None

    rows = []
    for i, track in enumerate(results, start=1):
        rows.append({
            "number": i,
            "left": f"{artist_string(track)} — {track.get('title', 'Unknown')}",
            "middle": album_string(track),
            "right": duration_text(track),
            "badge": "[E]" if track.get("isExplicit") is True else "",
        })
    idx = select_table(
        rows, "SEARCH → DOWNLOAD / TRACK", subtitle=f"Search: {query}",
        left_header="ARTIST — TRACK", middle_header="ALBUM", right_header="TIME"
    )
    return results[idx] if idx is not None else None

def play_album_loop(album, fallback=None):
    fallback = fallback or {}
    tracks = album.get("tracks") or []
    if not tracks:
        clear_screen()
        print("❌ Album has no tracks.")
        pause()
        return

    album_title = album.get("title") or fallback.get("title") or "Unknown Album"
    album_artist = artist_string(album)
    if not album_artist or album_artist == "Unknown Artist":
        album_artist = artist_string(fallback)

    thumbnails = album.get("thumbnails") or fallback.get("thumbnails") or []
    audio_playlist_id = album.get("audioPlaylistId")
    browse_id = album.get("_browse_id")
    for index, track in enumerate(tracks):
        track["_album_track"] = True
        track["_album_index"] = index
        track["_album_audio_playlist_id"] = audio_playlist_id
        track["_album_browse_id"] = browse_id
        track["album"] = {"name": album_title}
        if not track.get("thumbnails") and thumbnails:
            track["thumbnails"] = thumbnails

    selected = 0
    while True:
        rows = []
        for i, track in enumerate(tracks, start=1):
            rows.append({
                "number": i,
                "left": track.get("title", "Unknown"),
                "middle": artist_string(track),
                "right": duration_text(track),
                "badge": "[E]" if track.get("isExplicit") is True else "",
            })
        album_year = album.get("year") or fallback.get("year") or ""
        album_meta = f"{album_artist} — {album_title}"
        if album_year:
            album_meta += f" ({album_year})"

        idx = select_album_table(
            rows, album_meta, back_label="Back", initial=selected
        )
        if idx is None:
            return
        selected = idx
        play_track(tracks[idx])

def search_albums_play_loop():
    query = prompt_text(
        "SEARCH → PLAY / ALBUMS",
        "Search album / artist"
    )
    if not query:
        return

    clear_screen()
    print("Searching albums…")
    try:
        results = yt.search(query, filter="albums", limit=10)
    except Exception as e:
        print(f"❌ Search: {e}")
        pause()
        return
    if not results:
        print("❌ No albums found.")
        pause()
        return

    selected = 0
    while True:
        rows = [
            {
                "number": i,
                "left": album.get("title", "Unknown Album"),
                "middle": artist_string(album),
                "right": str(album.get("year") or ""),
            }
            for i, album in enumerate(results, start=1)
        ]
        idx = select_table(
            rows, "SEARCH → PLAY / ALBUMS", subtitle=f"Search: {query}",
            initial=selected, left_header="ALBUM", middle_header="ARTIST",
            right_header="YEAR", back_label="Back"
        )
        if idx is None:
            return
        selected = idx
        album_result = results[idx]
        browse_id = album_result.get("browseId")
        if not browse_id:
            continue
        clear_screen()
        print("Loading album…")
        try:
            album = yt.get_album(browse_id)
            album["_browse_id"] = browse_id
        except Exception as e:
            print(f"❌ Album: {e}")
            pause()
            continue
        play_album_loop(album, fallback=album_result)

def search_albums(action="play"):
    query = prompt_text(
        "SEARCH → DOWNLOAD / ALBUM",
        "Search album / artist"
    )
    if not query:
        return None

    clear_screen()
    print("Ищу альбомы…")
    try:
        results = yt.search(query, filter="albums", limit=10)
    except Exception as e:
        print(f"❌ Search: {e}")
        pause()
        return None
    if not results:
        print("❌ Альбомы не найдены.")
        pause()
        return None

    rows = [
        {
            "number": i,
            "left": album.get("title", "Unknown Album"),
            "middle": artist_string(album),
            "right": str(album.get("year") or ""),
        }
        for i, album in enumerate(results, start=1)
    ]
    idx = select_table(
        rows, "SEARCH → DOWNLOAD / ALBUM", subtitle=f"Search: {query}",
        left_header="ALBUM", middle_header="ARTIST", right_header="YEAR"
    )
    if idx is None:
        return None

    result = results[idx]
    browse_id = result.get("browseId")
    if not browse_id:
        return None
    clear_screen()
    print("Загружаю альбом…")
    try:
        album = yt.get_album(browse_id)
        album["_browse_id"] = browse_id
    except Exception as e:
        print(f"❌ Album: {e}")
        pause()
        return None
    return show_album(album, fallback=result, action=action)

def show_album(album, fallback=None, action="play"):
    fallback = fallback or {}
    tracks = album.get("tracks") or []
    if not tracks:
        return None

    album_title = album.get("title") or fallback.get("title") or "Unknown Album"
    album_artist = artist_string(album)
    if not album_artist or album_artist == "Unknown Artist":
        album_artist = artist_string(fallback)
    thumbnails = album.get("thumbnails") or fallback.get("thumbnails") or []
    audio_playlist_id = album.get("audioPlaylistId")
    browse_id = album.get("_browse_id")

    for index, track in enumerate(tracks):
        track["_album_track"] = True
        track["_album_index"] = index
        track["_album_audio_playlist_id"] = audio_playlist_id
        track["_album_browse_id"] = browse_id
        track["album"] = {"name": album_title}
        if not track.get("thumbnails") and thumbnails:
            track["thumbnails"] = thumbnails

    rows = []
    if action == "download":
        rows.append({"number": 0, "left": "Скачать весь альбом", "middle": album_artist, "right": "ALL"})
    for i, track in enumerate(tracks, start=1):
        rows.append({
            "number": i,
            "left": track.get("title", "Unknown"),
            "middle": artist_string(track),
            "right": duration_text(track),
            "badge": "[E]" if track.get("isExplicit") is True else "",
        })

    # Для этого экрана 0 — «скачать весь альбом», назад только Esc.
    album_year = album.get("year") or fallback.get("year") or ""
    album_meta = f"{album_artist} — {album_title}"
    if album_year:
        album_meta += f" ({album_year})"

    idx = select_album_table(
        rows, album_meta, back_label="Back", allow_zero_selection=True
    )
    if idx is None:
        return None
    chosen = rows[idx]["number"]
    if action == "download" and chosen == 0:
        return {
            "_action": "download_album",
            "tracks": tracks,
            "album_title": album_title,
            "album_artist": album_artist,
            "thumbnails": thumbnails,
            "audio_playlist_id": audio_playlist_id,
            "browse_id": browse_id,
        }
    if chosen <= 0 or chosen > len(tracks):
        return None
    return tracks[chosen - 1]

def search_music(
    action="play"
):
    search_type = (
        choose_search_type()
    )

    if search_type == "song":

        return search_songs()

    if search_type == "album":

        return search_albums(
            action=action
        )

    return None


# ============================================================
# STREAM
# ============================================================

def get_audio_url(video_id):
    if not video_id:
        return None

    url = (
        "https://www.youtube.com/watch?v="
        + video_id
    )

    options = {
        "format":
            "bestaudio[ext=m4a]/bestaudio",

        "quiet":
            True,

        "no_warnings":
            True,

        "noplaylist":
            True,

        "nocheckcertificate":
            True,
    }

    try:

        with YoutubeDL(
            options
        ) as ydl:

            info = (
                ydl.extract_info(
                    url,
                    download=False
                )
            )

            if not info:
                return None

            direct_url = (
                info.get("url")
            )

            if direct_url:
                return direct_url

            formats = (
                info.get("formats")
                or []
            )

            audio = [
                fmt

                for fmt in formats

                if fmt.get("url")
                and fmt.get(
                    "vcodec"
                ) == "none"
            ]

            m4a = [
                fmt

                for fmt in audio

                if fmt.get(
                    "ext"
                ) == "m4a"
            ]

            if m4a:

                return (
                    m4a[-1]
                    .get("url")
                )

            if audio:

                return (
                    audio[-1]
                    .get("url")
                )

    except Exception as e:

        print(
            f"❌ yt-dlp: {e}"
        )

    return None


# ============================================================
# PLAY
# ============================================================

def play_track(track):
    video_id = (
        track.get("videoId")
    )

    if not video_id:

        print(
            "❌ videoId отсутствует."
        )

        return False

    artist = artist_string(
        track
    )

    title = track.get(
        "title",
        "Unknown"
    )

    print()
    print(
        f"▶️ {artist} — {title}"
    )

    print(
        "⏳ Получаю stream..."
    )

    stream_url = (
        get_audio_url(
            video_id
        )
    )

    if not stream_url:

        print(
            "❌ Stream не получен."
        )

        return False

    return (
        open_stream_in_vlc(
            stream_url
        )
    )


# ============================================================
# IMAGE
# ============================================================

def download_image_url(
    url,
    destination
):
    if not url:
        return False

    try:

        request = (
            urllib.request.Request(
                url,
                headers={
                    "User-Agent":
                        "Mozilla/5.0"
                }
            )
        )

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            destination.write_bytes(
                response.read()
            )

        return True

    except Exception as e:

        print(
            f"⚠️ Cover: {e}"
        )

        return False


def get_cover_url(track):
    thumbnails = (
        track.get("thumbnails")
        or []
    )

    if not thumbnails:
        return None

    return (
        thumbnails[-1]
        .get("url")
    )


def make_square_cover(
    source,
    destination
):
    ffmpeg = find_ffmpeg()

    if not ffmpeg:
        return False

    command = [
        ffmpeg,
        "-y",

        "-i",
        str(source),

        "-vf",
        (
            "crop="
            "'min(iw,ih)':"
            "'min(iw,ih)',"
            "scale=1000:1000"
        ),

        "-frames:v",
        "1",

        "-q:v",
        "2",

        str(destination),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    return (
        result.returncode == 0
        and destination.exists()
    )


# ============================================================
# METADATA
# ============================================================

def embed_metadata(
    source_audio,
    output_audio,
    cover,
    artist,
    title,
    album="",
    track_number=None
):
    ffmpeg = find_ffmpeg()

    if not ffmpeg:
        return False

    command = [
        ffmpeg,

        "-y",

        "-i",
        str(source_audio),

        "-i",
        str(cover),

        "-map",
        "0:a:0",

        "-map",
        "1:v:0",

        "-c:a",
        "copy",

        "-c:v",
        "mjpeg",

        "-disposition:v:0",
        "attached_pic",

        "-metadata",
        f"title={title}",

        "-metadata",
        f"artist={artist}",
    ]

    if album:

        command.extend(
            [
                "-metadata",
                f"album={album}"
            ]
        )

    if track_number is not None:

        command.extend(
            [
                "-metadata",
                f"track={track_number}"
            ]
        )

    command.extend(
        [
            "-metadata:s:v",
            "title=Album cover",

            "-metadata:s:v",
            "comment=Cover (front)",

            str(output_audio)
        ]
    )

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    return (
        result.returncode == 0
        and output_audio.exists()
    )


# ============================================================
# FINALIZE DOWNLOAD
# ============================================================

def finalize_download(
    temp_audio,
    destination,
    artist,
    title,
    album="",
    cover_url=None,
    track_number=None
):
    destination_dir = (
        destination.parent
    )

    if not find_ffmpeg():

        print(
            "⚠️ ffmpeg не найден."
        )

        temp_audio.replace(
            destination
        )

        return True

    temp_id = safe_filename(
        temp_audio.stem
    )

    original_cover = (
        destination_dir
        / f".cover_{temp_id}.jpg"
    )

    square_cover = (
        destination_dir
        / f".square_{temp_id}.jpg"
    )

    tagged_audio = (
        destination_dir
        / f".tagged_{temp_id}.m4a"
    )

    tagged_success = False

    if cover_url:

        print(
            "🖼 Загружаю обложку..."
        )

        if download_image_url(
            cover_url,
            original_cover
        ):

            print(
                "✂️ Обложка 1000×1000..."
            )

            if make_square_cover(
                original_cover,
                square_cover
            ):

                print(
                    "🏷 Metadata..."
                )

                tagged_success = (
                    embed_metadata(
                        temp_audio,
                        tagged_audio,
                        square_cover,
                        artist,
                        title,
                        album,
                        track_number
                    )
                )

    try:

        if tagged_success:

            tagged_audio.replace(
                destination
            )

        else:

            temp_audio.replace(
                destination
            )

    except Exception as e:

        print(
            f"❌ Save: {e}"
        )

        return False

    for path in (
        temp_audio,
        original_cover,
        square_cover,
        tagged_audio,
    ):

        try:

            if (
                path.exists()
                and path != destination
            ):

                path.unlink()

        except Exception:
            pass

    print(
        f"✅ {destination.name}"
    )

    return True


# ============================================================
# DOWNLOAD TRACK
# ============================================================

def download_track(
    track,
    destination_dir=None,
    track_number=None,
    rebuild=True
):
    # --------------------------------------------------------
    # ALBUM TRACK => RESOLVE ATV
    # --------------------------------------------------------

    if track.get(
        "_album_track"
    ):

        video_id = (
            resolve_album_track_video_id(
                track
            )
        )

    else:

        video_id = (
            track.get(
                "videoId"
            )
        )

    if not video_id:

        print()
        print(
            "⏭ Трек пропущен."
        )

        print(
            "Причина: не удалось получить "
            "правильную album audio-версию."
        )

        return False

    destination_dir = (
        destination_dir
        or DOWNLOAD_DIR
    )

    destination_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    artist = artist_string(
        track
    )

    title = track.get(
        "title",
        "Unknown Track"
    )

    album = album_string(
        track
    )

    if track_number is None:

        filename = (
            f"{safe_filename(artist)}"
            f" - "
            f"{safe_filename(title)}"
            f".m4a"
        )

    else:

        filename = (
            f"{track_number:02d} - "
            f"{safe_filename(artist)}"
            f" - "
            f"{safe_filename(title)}"
            f".m4a"
        )

    final_file = (
        destination_dir
        / filename
    )

    if final_file.exists():

        print(
            f"⏭ Уже есть: "
            f"{final_file.name}"
        )

        return True

    url = (
        "https://www.youtube.com/watch?v="
        + video_id
    )

    temp_template = str(
        destination_dir
        / (
            f".audio_"
            f"{video_id}.%(ext)s"
        )
    )

    print()
    print(
        f"⬇️ {artist} — {title}"
    )

    options = {
        "format":
            "bestaudio[ext=m4a]/bestaudio",

        "noplaylist":
            True,

        "outtmpl":
            temp_template,

        "quiet":
            False,

        "nocheckcertificate":
            True,
    }

    try:

        with YoutubeDL(
            options
        ) as ydl:

            info = (
                ydl.extract_info(
                    url,
                    download=True
                )
            )

            temp_audio = Path(
                ydl.prepare_filename(
                    info
                )
            )

    except Exception as e:

        print(
            f"❌ Download: {e}"
        )

        return False

    if not temp_audio.exists():

        print(
            "❌ Файл не найден."
        )

        return False

    cover_url = (
        get_cover_url(
            track
        )
    )

    success = finalize_download(
        temp_audio=
            temp_audio,

        destination=
            final_file,

        artist=
            artist,

        title=
            title,

        album=
            album,

        cover_url=
            cover_url,

        track_number=
            track_number
    )

    if success and rebuild:

        rebuild_playlist_silent()

    return success


# ============================================================
# DOWNLOAD ALBUM
# ============================================================

def download_album(
    album_data
):
    tracks = (
        album_data.get(
            "tracks"
        )
        or []
    )

    album_title = (
        album_data.get(
            "album_title"
        )
        or "Unknown Album"
    )

    album_artist = (
        album_data.get(
            "album_artist"
        )
        or "Unknown Artist"
    )

    album_thumbnails = (
        album_data.get(
            "thumbnails"
        )
        or []
    )

    if not tracks:

        print(
            "❌ Альбом пуст."
        )

        return False

    folder_name = safe_filename(
        f"{album_artist} - "
        f"{album_title}"
    )

    album_dir = (
        ALBUMS_DIR
        / folder_name
    )

    album_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    print()
    print(
        "══════════════════════════════"
    )

    print(
        f"💿 {album_artist}"
    )

    print(
        f"   {album_title}"
    )

    print(
        f"🎵 Tracks: {len(tracks)}"
    )

    print(
        "══════════════════════════════"
    )

    success_count = 0
    skipped_count = 0

    for index, track in enumerate(
        tracks,
        start=1
    ):

        track["_album_track"] = True

        track["album"] = {
            "name":
                album_title
        }

        if (
            not track.get(
                "thumbnails"
            )
            and album_thumbnails
        ):

            track[
                "thumbnails"
            ] = (
                album_thumbnails
            )

        print()
        print(
            f"[{index}/{len(tracks)}] "
            f"{artist_string(track)} — "
            f"{track.get('title', 'Unknown')}"
        )

        if track.get(
            "isExplicit"
        ) is True:

            print(
                "   🔞 Explicit"
            )

        success = download_track(
            track,
            destination_dir=
                album_dir,
            track_number=
                index,
            rebuild=False
        )

        if success:

            success_count += 1

        else:

            skipped_count += 1

    create_album_playlist(
        album_dir,
        artist=
            album_artist,
        album_title=
            album_title
    )

    rebuild_playlist_silent()

    print()
    print(
        "══════════════════════════════"
    )

    print(
        f"✅ Скачано: "
        f"{success_count}"
    )

    if skipped_count:

        print(
            f"⏭ Пропущено: "
            f"{skipped_count}"
        )

    print(
        "══════════════════════════════"
    )

    print(
        f"📁 {album_dir}"
    )

    return (
        success_count > 0
    )


# ============================================================
# ALBUM FROM URL
# ============================================================

def get_album_from_url(url):
    try:

        parsed = urlparse(
            url
        )

        query = parse_qs(
            parsed.query
        )

        playlist_id = (
            query.get(
                "list",
                [None]
            )[0]
        )

    except Exception:
        return None

    if not playlist_id:
        return None

    if not playlist_id.startswith(
        "OLAK"
    ):
        return None

    print(
        "💿 Обнаружен YouTube Music album"
    )

    try:

        browse_id = (
            yt.get_album_browse_id(
                playlist_id
            )
        )

        if not browse_id:

            print(
                "❌ Album browseId не найден."
            )

            return None

        print(
            f"✓ Album ID: {browse_id}"
        )

        album = yt.get_album(
            browse_id
        )

        album["_browse_id"] = browse_id

        return album

    except Exception as e:

        print(
            f"❌ Album URL: {e}"
        )

        return None


# ============================================================
# PREPARE ALBUM
# ============================================================

def prepare_album_data(
    album
):
    if not album:
        return None

    tracks = (
        album.get("tracks")
        or []
    )

    if not tracks:
        return None

    title = (
        album.get("title")
        or "Unknown Album"
    )

    artist = (
        artist_string(
            album
        )
    )

    thumbnails = (
        album.get("thumbnails")
        or []
    )

    for index, track in enumerate(
        tracks
    ):
        track["_album_track"] = True
        track["_album_index"] = index

        track["_album_audio_playlist_id"] = (
            album.get("audioPlaylistId")
        )

        track["_album_browse_id"] = (
            album.get("_browse_id")
        )

        track["album"] = {
            "name": title
        }

        if (
            not track.get("thumbnails")
            and thumbnails
        ):
            track["thumbnails"] = thumbnails
            
    return {
        "_action":
            "download_album",

        "tracks":
            tracks,

        "album_title":
            title,

        "album_artist":
            artist,

        "thumbnails":
            thumbnails,
    }


# ============================================================
# DOWNLOAD ALBUM BY URL
# ============================================================

def download_album_by_url():
    url = prompt_text(
        "ALBUM BY URL",
        "Paste YouTube Music album URL"
    )
    if not url:
        return

    clear_screen()
    render_header("ALBUM BY URL")
    print()
    print("Loading album metadata…")

    album = get_album_from_url(url)
    if not album:
        print("❌ Could not recognize YouTube Music album.")
        pause()
        return

    album_data = prepare_album_data(album)
    if not album_data:
        return

    artist = album_data["album_artist"]
    title = album_data["album_title"]
    count = len(album_data["tracks"])

    if prompt_confirm(
        "ALBUM BY URL",
        "Download album?",
        default=True,
        subtitle=f"{artist} — {title} · {count} tracks"
    ):
        download_album(album_data)

def search_songs_download_loop():
    query = prompt_text(
        "SEARCH → DOWNLOAD / TRACKS",
        "Search track / artist"
    )
    if not query:
        return

    clear_screen()
    print("Searching tracks…")
    try:
        results = yt.search(query, filter="songs", limit=10)
    except Exception as e:
        print(f"❌ Search: {e}")
        pause()
        return
    if not results:
        print("❌ Nothing found.")
        pause()
        return

    selected = 0
    while True:
        rows = []
        for i, track in enumerate(results, start=1):
            rows.append({
                "number": i,
                "left": f"{artist_string(track)} — {track.get('title', 'Unknown')}",
                "middle": album_string(track),
                "right": duration_text(track),
                "badge": "[E]" if track.get("isExplicit") is True else "",
            })
        idx = select_table(
            rows, "SEARCH → DOWNLOAD / TRACKS", subtitle=f"Search: {query}",
            initial=selected, left_header="ARTIST — TRACK", middle_header="ALBUM",
            right_header="TIME", back_label="Back"
        )
        if idx is None:
            return
        selected = idx
        download_track(results[idx])
        # Stay in the same results list after download.


def download_album_tracks_loop(album, fallback=None):
    fallback = fallback or {}
    tracks = album.get("tracks") or []
    if not tracks:
        clear_screen()
        print("❌ Album has no tracks.")
        pause()
        return

    album_title = album.get("title") or fallback.get("title") or "Unknown Album"
    album_artist = artist_string(album)
    if not album_artist or album_artist == "Unknown Artist":
        album_artist = artist_string(fallback)
    thumbnails = album.get("thumbnails") or fallback.get("thumbnails") or []
    audio_playlist_id = album.get("audioPlaylistId")
    browse_id = album.get("_browse_id")

    for index, track in enumerate(tracks):
        track["_album_track"] = True
        track["_album_index"] = index
        track["_album_audio_playlist_id"] = audio_playlist_id
        track["_album_browse_id"] = browse_id
        track["album"] = {"name": album_title}
        if not track.get("thumbnails") and thumbnails:
            track["thumbnails"] = thumbnails

    album_data = {
        "_action": "download_album",
        "tracks": tracks,
        "album_title": album_title,
        "album_artist": album_artist,
        "thumbnails": thumbnails,
        "audio_playlist_id": audio_playlist_id,
        "browse_id": browse_id,
    }

    selected = 0
    while True:
        rows = [{"number": 0, "left": "Download full album", "middle": "", "right": "ALL"}]
        for i, track in enumerate(tracks, start=1):
            rows.append({
                "number": i,
                "left": track.get("title", "Unknown"),
                "middle": artist_string(track),
                "right": duration_text(track),
                "badge": "[E]" if track.get("isExplicit") is True else "",
            })

        year = album.get("year") or fallback.get("year") or ""
        meta = f"{album_artist} — {album_title}"
        if year:
            meta += f" ({year})"

        idx = select_album_table(rows, meta, back_label="Back", initial=selected, allow_zero_selection=True)
        if idx is None:
            return
        selected = idx
        chosen = rows[idx]["number"]
        if chosen == 0:
            download_album(album_data)
        elif 1 <= chosen <= len(tracks):
            download_track(tracks[chosen - 1])
        # Stay inside album after any download.


def search_albums_download_loop():
    query = prompt_text(
        "SEARCH → DOWNLOAD / ALBUMS",
        "Search album / artist"
    )
    if not query:
        return

    clear_screen()
    print("Searching albums…")
    try:
        results = yt.search(query, filter="albums", limit=10)
    except Exception as e:
        print(f"❌ Search: {e}")
        pause()
        return
    if not results:
        print("❌ No albums found.")
        pause()
        return

    selected = 0
    while True:
        rows = [
            {"number": i, "left": a.get("title", "Unknown Album"), "middle": artist_string(a), "right": str(a.get("year") or "")}
            for i, a in enumerate(results, start=1)
        ]
        idx = select_table(
            rows, "SEARCH → DOWNLOAD / ALBUMS", subtitle=f"Search: {query}",
            initial=selected, left_header="ALBUM", middle_header="ARTIST", right_header="YEAR", back_label="Back"
        )
        if idx is None:
            return
        selected = idx
        result = results[idx]
        browse_id = result.get("browseId")
        if not browse_id:
            continue
        clear_screen()
        print("Loading album…")
        try:
            album = yt.get_album(browse_id)
            album["_browse_id"] = browse_id
        except Exception as e:
            print(f"❌ Album: {e}")
            pause()
            continue
        download_album_tracks_loop(album, fallback=result)


def search_and_play():
    while True:
        choice = select_simple_menu(
            "SEARCH → PLAY",
            [
                (1, "Tracks", "search tracks and keep the results open"),
                (2, "Albums", "open an album and switch tracks"),
            ],
            subtitle="Arrows / mouse / trackpad / number",
            back_label="Main Menu",
        )
        if choice == "1":
            search_songs_play_loop()
        elif choice == "2":
            search_albums_play_loop()
        else:
            return

def search_and_download():
    while True:
        choice = select_simple_menu(
            "SEARCH → DOWNLOAD",
            [
                (1, "Tracks", "search and download individual tracks"),
                (2, "Albums", "search albums and download tracks / full album"),
                (3, "Album by URL", "paste a YouTube Music album URL"),
            ],
            subtitle="After download you stay in the current list",
            back_label="Main Menu",
        )
        if choice == "1":
            search_songs_download_loop()
        elif choice == "2":
            search_albums_download_loop()
        elif choice == "3":
            download_album_by_url()
        else:
            return


# ============================================================
# UNIFIED SEARCH
# ============================================================

def search_tracks_loop():
    query = prompt_text(
        "SEARCH / TRACKS",
        "Search track / artist"
    )
    if not query:
        return

    clear_screen()
    render_header("SEARCH / TRACKS")
    print()
    print("Searching…")

    try:
        results = yt.search(
            query,
            filter="songs",
            limit=20
        )
    except Exception as e:
        print(f"Search error: {e}")
        pause()
        return

    if not results:
        print("Nothing found.")
        pause()
        return

    selected = 0

    while True:
        rows = []

        for i, track in enumerate(
            results,
            start=1
        ):
            rows.append(
                {
                    "number": i,
                    "left": (
                        f"{artist_string(track)} — "
                        f"{track.get('title', 'Unknown')}"
                    ),
                    "middle": album_string(track),
                    "right": duration_text(track),
                    "badge": "[E]" if track.get("isExplicit") is True else "",
                }
            )

        idx = select_table(
            rows,
            "SEARCH / TRACKS",
            subtitle=f"Search: {query}",
            initial=selected,
            left_header="ARTIST — TRACK",
            middle_header="ALBUM",
            right_header="TIME",
            back_label="Search",
        )

        if idx is None:
            return

        selected = idx
        track = results[idx]

        action = choose_media_action(
            track.get("title", "Track"),
            subtitle=artist_string(track),
        )

        if action == "1":
            play_track(track)

        elif action == "2":
            download_track(track)

        # Always stay in the same result list.


def _prepare_album_context(album, fallback=None):
    fallback = fallback or {}

    tracks = album.get("tracks") or []
    if not tracks:
        return None

    album_title = (
        album.get("title")
        or fallback.get("title")
        or "Unknown Album"
    )

    album_artist = artist_string(album)
    if (
        not album_artist
        or album_artist == "Unknown Artist"
    ):
        album_artist = artist_string(fallback)

    thumbnails = (
        album.get("thumbnails")
        or fallback.get("thumbnails")
        or []
    )

    audio_playlist_id = album.get("audioPlaylistId")
    browse_id = album.get("_browse_id")

    for index, track in enumerate(tracks):
        track["_album_track"] = True
        track["_album_index"] = index
        track["_album_audio_playlist_id"] = audio_playlist_id
        track["_album_browse_id"] = browse_id
        track["album"] = {"name": album_title}

        if (
            not track.get("thumbnails")
            and thumbnails
        ):
            track["thumbnails"] = thumbnails

    return {
        "tracks": tracks,
        "album_title": album_title,
        "album_artist": album_artist,
        "thumbnails": thumbnails,
        "audio_playlist_id": audio_playlist_id,
        "browse_id": browse_id,
        "year": album.get("year") or fallback.get("year") or "",
    }


def album_media_loop(album, fallback=None):
    context = _prepare_album_context(
        album,
        fallback
    )

    if not context:
        clear_screen()
        print("Album has no tracks.")
        pause()
        return

    tracks = context["tracks"]
    album_title = context["album_title"]
    album_artist = context["album_artist"]

    album_data = {
        "_action": "download_album",
        "tracks": tracks,
        "album_title": album_title,
        "album_artist": album_artist,
        "thumbnails": context["thumbnails"],
        "audio_playlist_id": context["audio_playlist_id"],
        "browse_id": context["browse_id"],
    }

    meta = f"{album_artist} — {album_title}"
    if context["year"]:
        meta += f" ({context['year']})"

    selected = 0

    while True:
        rows = [
            {
                "number": 0,
                "left": "Download full album",
                "right": "ALL",
            }
        ]

        for i, track in enumerate(
            tracks,
            start=1
        ):
            rows.append(
                {
                    "number": i,
                    "left": track.get("title", "Unknown"),
                    "right": duration_text(track),
                    "badge": "[E]" if track.get("isExplicit") is True else "",
                }
            )

        idx = select_album_table(
            rows,
            meta,
            back_label="Albums",
            initial=selected,
            allow_zero_selection=True,
        )

        if idx is None:
            return

        selected = idx
        chosen_number = rows[idx]["number"]

        # 0 is always the direct full-album download action.
        if chosen_number == 0:
            download_album(album_data)
            continue

        if not (
            1 <= chosen_number <= len(tracks)
        ):
            continue

        track = tracks[
            chosen_number - 1
        ]

        action = choose_media_action(
            track.get("title", "Track"),
            subtitle=f"{artist_string(track)} • {album_title}",
        )

        if action == "1":
            play_track(track)

        elif action == "2":
            download_track(track)

        # Stay inside the album after Play/Download.


def search_albums_loop():
    query = prompt_text(
        "SEARCH / ALBUMS",
        "Search album / artist"
    )
    if not query:
        return

    clear_screen()
    render_header("SEARCH / ALBUMS")
    print()
    print("Searching…")

    try:
        results = yt.search(
            query,
            filter="albums",
            limit=20
        )
    except Exception as e:
        print(f"Search error: {e}")
        pause()
        return

    if not results:
        print("No albums found.")
        pause()
        return

    selected = 0

    while True:
        rows = [
            {
                "number": i,
                "left": album.get("title", "Unknown Album"),
                "middle": artist_string(album),
                "right": str(album.get("year") or ""),
            }
            for i, album in enumerate(
                results,
                start=1
            )
        ]

        idx = select_table(
            rows,
            "SEARCH / ALBUMS",
            subtitle=f"Search: {query}",
            initial=selected,
            left_header="ALBUM",
            middle_header="ARTIST",
            right_header="YEAR",
            back_label="Search",
        )

        if idx is None:
            return

        selected = idx
        result = results[idx]
        browse_id = result.get("browseId")

        if not browse_id:
            continue

        clear_screen()
        render_header("ALBUM")
        print()
        print("Loading…")

        try:
            album = yt.get_album(
                browse_id
            )
            album["_browse_id"] = browse_id
        except Exception as e:
            print(f"Album error: {e}")
            pause()
            continue

        album_media_loop(
            album,
            fallback=result
        )


def search_menu():
    while True:
        choice = select_simple_menu(
            "SEARCH",
            [
                (
                    1,
                    "Tracks",
                    "search and download individual tracks",
                ),
                (
                    2,
                    "Track by URL",
                    "paste a YouTube Music track URL",
                ),
                (
                    3,
                    "Albums",
                    "search albums and download tracks / full album",
                ),
                (
                    4,
                    "Album by URL",
                    "paste a YouTube Music album URL",
                ),
            ],
            subtitle="tap×2 open • swipe scroll",
            back_label="Main Menu",
        )

        if choice == "1":
            search_tracks_loop()

        elif choice == "2":
            track_by_url_loop()

        elif choice == "3":
            search_albums_loop()

        elif choice == "4":
            download_album_by_url()

        else:
            return


# ============================================================
# TRACK BY URL
# ============================================================

def youtube_video_id_from_url(url):
    try:
        parsed = urlparse(url)

        if parsed.netloc in (
            "youtu.be",
            "www.youtu.be",
        ):
            return parsed.path.strip("/").split("/")[0] or None

        if "youtube.com" in parsed.netloc:
            query = parse_qs(parsed.query)
            video_id = query.get("v", [None])[0]

            if video_id:
                return video_id

            parts = [
                p
                for p in parsed.path.split("/")
                if p
            ]

            if len(parts) >= 2 and parts[0] in (
                "shorts",
                "embed",
                "live",
            ):
                return parts[1]

    except Exception:
        pass

    return None


def ytmusic_track_from_video_id(video_id):
    """Best effort: turn a YouTube video id into YTMusic song metadata."""
    if not video_id:
        return None

    try:
        watch = yt.get_watch_playlist(
            videoId=video_id,
            limit=10
        )
    except Exception:
        watch = None

    tracks = (
        watch.get("tracks")
        if watch
        else None
    ) or []

    # Prefer exact id, otherwise first YTMusic track.
    for track in tracks:
        if track.get("videoId") == video_id:
            return track

    if tracks:
        return tracks[0]

    return None


def play_url_direct(url):
    """Generic yt-dlp stream playback fallback."""
    options = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=False
            )

            direct = info.get("url")

            if not direct:
                formats = info.get("formats") or []
                audio = [
                    f
                    for f in formats
                    if f.get("url")
                    and f.get("vcodec") == "none"
                ]
                if audio:
                    direct = audio[-1].get("url")

            if direct:
                return open_stream_in_vlc(
                    direct
                )

    except Exception as e:
        clear_screen()
        print(f"Play error: {e}")
        pause()

    return False


def track_by_url_loop():
    while True:
        url = prompt_text(
            "TRACK BY URL",
            "Paste YouTube / YouTube Music track URL",
            subtitle="After selection choose Play or Download",
        )

        if not url:
            return

        video_id = youtube_video_id_from_url(
            url
        )

        if not video_id:
            clear_screen()
            render_header("TRACK BY URL")
            print()
            print("This does not look like a single YouTube track URL.")
            print("Use Download by URL for generic resources / playlists.")
            pause()
            continue

        track = ytmusic_track_from_video_id(
            video_id
        )

        if track:
            title = track.get(
                "title",
                "Track"
            )
            subtitle = artist_string(
                track
            )
        else:
            info = extract_url_info(
                url
            )
            title = (
                info.get("track")
                or info.get("title")
                or "Track"
            ) if info else "Track"

            subtitle = (
                info.get("artist")
                or info.get("uploader")
                or "YouTube"
            ) if info else "YouTube"

        action = choose_media_action(
            title,
            subtitle=subtitle,
        )

        if action == "1":
            if track:
                play_track(track)
            else:
                play_url_direct(url)

        elif action == "2":
            # Use the same Search download path whenever YTMusic metadata exists.
            if track:
                download_track(track)
            else:
                download_url_track(url)

        # Return to Track by URL input, not Main Menu.


# ============================================================
# URL PLAYLIST BROWSER
# ============================================================

def url_playlist_loop(url, playlist_info):
    title = (
        playlist_info.get("title")
        or "Playlist"
    )

    entries = (
        playlist_info.get("entries")
        or []
    )

    if not entries:
        clear_screen()
        print("Playlist is empty.")
        pause()
        return

    selected = 0

    while True:
        rows = [
            {
                "number": 0,
                "left": "Download full playlist",
                "right": "ALL",
            }
        ]

        for i, entry in enumerate(
            entries,
            start=1
        ):
            entry = entry or {}

            rows.append(
                {
                    "number": i,
                    "left": (
                        entry.get("title")
                        or f"Track {i}"
                    ),
                    "right": (
                        entry.get("duration_string")
                        or ""
                    ),
                }
            )

        idx = select_album_table(
            rows,
            title,
            back_label="URL",
            initial=selected,
            allow_zero_selection=True,
        )

        if idx is None:
            return

        selected = idx
        chosen = rows[idx]["number"]

        if chosen == 0:
            download_url_playlist(
                url,
                playlist_info
            )
            continue

        if not (
            1 <= chosen <= len(entries)
        ):
            continue

        entry = entries[
            chosen - 1
        ] or {}

        entry_url = (
            entry.get("webpage_url")
            or entry.get("url")
        )

        entry_id = entry.get("id")

        if (
            entry_url
            and not str(entry_url).startswith("http")
        ):
            entry_url = None

        if not entry_url and entry_id:
            entry_url = (
                "https://www.youtube.com/watch?v="
                + str(entry_id)
            )

        if not entry_url:
            continue

        # Download by URL is a download-only workflow.
        video_id = youtube_video_id_from_url(
            entry_url
        )

        track = ytmusic_track_from_video_id(
            video_id
        ) if video_id else None

        if track:
            download_track(track)
        else:
            download_url_track(entry_url)

        # Stay in playlist.


def _looks_like_direct_audio_url(url):
    try:
        suffix = Path(
            urlparse(url).path
        ).suffix.lower()
        return suffix in AUDIO_EXTENSIONS
    except Exception:
        return False


def download_direct_audio_url(url):
    """Simple direct audio-file downloader for non-extractor resources."""
    try:
        parsed = urlparse(url)
        name = Path(parsed.path).name or "audio"
        name = safe_filename(name)

        suffix = Path(name).suffix.lower()
        if suffix not in AUDIO_EXTENSIONS:
            suffix = ".m4a"
            name += suffix

        destination = (
            DOWNLOAD_DIR
            / name
        )

        DOWNLOAD_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=60
        ) as response:
            with destination.open(
                "wb"
            ) as output:
                shutil.copyfileobj(
                    response,
                    output
                )

        rebuild_playlist_silent()

        clear_screen()
        render_header("DOWNLOAD BY URL")
        print()
        print(f"Saved: {destination.name}")
        pause()

        return True

    except Exception as e:
        clear_screen()
        render_header("DOWNLOAD BY URL")
        print()
        print(f"Direct download error: {e}")
        pause()
        return False


def extract_url_info(
    url,
    flat=False
):
    options = {
        "quiet":
            True,

        "no_warnings":
            True,

        "skip_download":
            True,
    }

    if flat:

        options[
            "extract_flat"
        ] = "in_playlist"

    try:

        with YoutubeDL(
            options
        ) as ydl:

            return (
                ydl.extract_info(
                    url,
                    download=False
                )
            )

    except Exception as e:

        print(
            f"❌ URL: {e}"
        )

        return None


def is_playlist_info(
    info
):
    if not info:
        return False

    return (
        info.get("_type")
        == "playlist"
        or bool(
            info.get("entries")
        )
    )


# ============================================================
# DOWNLOAD URL TRACK
# ============================================================

def download_url_track(
    url,
    destination_dir=None,
    track_number=None,
    rebuild=True
):
    destination_dir = (
        destination_dir
        or DOWNLOAD_DIR
    )

    destination_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    print()
    print(
        "🔍 Получаю metadata..."
    )

    info = extract_url_info(
        url
    )

    if not info:
        return False

    title = (
        info.get("track")
        or info.get("title")
        or "Unknown Track"
    )

    artist = (
        info.get("artist")
        or info.get("creator")
        or info.get("uploader")
        or info.get("channel")
        or "Unknown Artist"
    )

    album = (
        info.get("album")
        or ""
    )

    cover_url = (
        info.get("thumbnail")
    )

    if not cover_url:

        thumbnails = (
            info.get(
                "thumbnails"
            )
            or []
        )

        if thumbnails:

            cover_url = (
                thumbnails[-1]
                .get("url")
            )

    if track_number is None:

        filename = (
            f"{safe_filename(artist)}"
            f" - "
            f"{safe_filename(title)}"
            f".m4a"
        )

    else:

        filename = (
            f"{track_number:02d} - "
            f"{safe_filename(artist)}"
            f" - "
            f"{safe_filename(title)}"
            f".m4a"
        )

    final_file = (
        destination_dir
        / filename
    )

    if final_file.exists():

        print(
            f"⏭ Уже есть: "
            f"{final_file.name}"
        )

        return True

    temp_template = str(
        destination_dir
        / (
            ".url_audio_"
            + safe_filename(
                str(
                    info.get("id")
                    or "track"
                )
            )
            + ".%(ext)s"
        )
    )

    options = {
        "format":
            "bestaudio[ext=m4a]"
            "/bestaudio",

        "noplaylist":
            True,

        "outtmpl":
            temp_template,

        "quiet":
            False,

        "nocheckcertificate":
            True,
    }

    try:

        with YoutubeDL(
            options
        ) as ydl:

            downloaded = (
                ydl.extract_info(
                    url,
                    download=True
                )
            )

            temp_audio = Path(
                ydl.prepare_filename(
                    downloaded
                )
            )

    except Exception as e:

        print(
            f"❌ Download: {e}"
        )

        return False

    if not temp_audio.exists():

        return False

    success = finalize_download(
        temp_audio=
            temp_audio,

        destination=
            final_file,

        artist=
            artist,

        title=
            title,

        album=
            album,

        cover_url=
            cover_url,

        track_number=
            track_number
    )

    if success and rebuild:

        rebuild_playlist_silent()

    return success


# ============================================================
# DOWNLOAD URL PLAYLIST
# ============================================================

def download_url_playlist(
    url,
    playlist_info
):
    playlist_title = (
        playlist_info.get(
            "title"
        )
        or "Playlist"
    )

    destination = (
        DOWNLOAD_DIR
        / safe_filename(
            playlist_title
        )
    )

    destination.mkdir(
        parents=True,
        exist_ok=True
    )

    entries = (
        playlist_info.get(
            "entries"
        )
        or []
    )

    if not entries:

        print(
            "❌ Playlist пуст."
        )

        return False

    print()
    print(
        f"📚 {playlist_title}"
    )

    print(
        f"🎵 Треков: "
        f"{len(entries)}"
    )

    success_count = 0

    for index, entry in enumerate(
        entries,
        start=1
    ):

        if not entry:
            continue

        entry_url = (
            entry.get(
                "webpage_url"
            )
            or entry.get("url")
        )

        entry_id = (
            entry.get("id")
        )

        if (
            entry_url
            and not str(
                entry_url
            ).startswith("http")
        ):

            entry_url = None

        if (
            not entry_url
            and entry_id
        ):

            entry_url = (
                "https://www.youtube.com/watch?v="
                + str(entry_id)
            )

        if not entry_url:

            print(
                f"⚠️ [{index}] URL отсутствует"
            )

            continue

        print()
        print(
            f"[{index}/"
            f"{len(entries)}]"
        )

        if download_url_track(
            entry_url,
            destination_dir=
                destination,
            track_number=
                index,
            rebuild=False
        ):

            success_count += 1

    rebuild_playlist_silent()

    print()
    print(
        f"✅ Playlist: "
        f"{success_count}/"
        f"{len(entries)}"
    )

    return (
        success_count > 0
    )


# ============================================================
# DOWNLOAD BY URL
# ============================================================

def download_by_url():
    while True:
        url = prompt_text(
            "DOWNLOAD BY URL",
            "Paste URL",
            subtitle=(
                "YouTube track / playlist / album • direct audio URL • "
                "other yt-dlp supported resources"
            ),
        )

        if not url:
            return

        # ----------------------------------------------------
        # YouTube Music album
        # ----------------------------------------------------
        album = get_album_from_url(
            url
        )

        if album:
            album_media_loop(
                album
            )
            continue

        # ----------------------------------------------------
        # Direct audio file
        # ----------------------------------------------------
        if _looks_like_direct_audio_url(
            url
        ):
            download_direct_audio_url(
                url
            )
            continue

        # ----------------------------------------------------
        # YouTube single track: same metadata/artwork pipeline
        # as Search whenever YTMusic can resolve it.
        # ----------------------------------------------------
        video_id = youtube_video_id_from_url(
            url
        )

        if video_id:
            # A watch URL may also carry a playlist. Inspect flat first.
            info_flat = extract_url_info(
                url,
                flat=True
            )

            if (
                info_flat
                and is_playlist_info(info_flat)
                and len(info_flat.get("entries") or []) > 1
            ):
                url_playlist_loop(
                    url,
                    info_flat
                )
                continue

            track = ytmusic_track_from_video_id(
                video_id
            )

            if track:
                download_track(
                    track
                )
            else:
                download_url_track(
                    url
                )

            continue

        # ----------------------------------------------------
        # Generic extractor / playlist
        # ----------------------------------------------------
        clear_screen()
        render_header(
            "DOWNLOAD BY URL"
        )
        print()
        print("Analyzing URL…")

        info = extract_url_info(
            url,
            flat=True
        )

        if not info:
            pause()
            continue

        if is_playlist_info(
            info
        ):
            url_playlist_loop(
                url,
                info
            )
            continue

        # Generic non-YouTube resource supported by yt-dlp.
        download_url_track(
            url
        )




# ============================================================
# TELEGRAM: PUBLIC CHANNEL PREVIEW
# ============================================================

TELEGRAM_PREVIEW_LIMIT = 20


class _TelegramTextParser(HTMLParser):
    """Converts a small Telegram HTML fragment to readable terminal text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "p":
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        value = "".join(self.parts)
        lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _telegram_html_text(fragment):
    parser = _TelegramTextParser()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:
        return ""
    return parser.text()


def _telegram_matching_div_end(source, start):
    """Return the closing position of the DIV that starts at ``start``."""
    depth = 0
    tag_pattern = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)

    for match in tag_pattern.finditer(source, start):
        if match.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                return match.start()
        else:
            depth += 1

    return None


def _telegram_message_text(block):
    match = re.search(
        r'<div\s+class="[^"]*tgme_widget_message_text[^"]*"[^>]*>',
        block,
        re.IGNORECASE,
    )
    if not match:
        return ""

    end = _telegram_matching_div_end(block, match.start())
    if end is None:
        return ""

    return _telegram_html_text(block[match.end():end])


def _telegram_document_tracks(block, username, date):
    """Extract audio names and public post links from Telegram's web preview."""
    document_pattern = re.compile(
        r'<a\b(?=[^>]*\bclass="[^"]*tgme_widget_message_document_wrap[^"]*")'
        r'(?=[^>]*\bhref="([^"]+)")[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    tracks = []

    for match in document_pattern.finditer(block):
        href, document = match.groups()
        if not re.search(
            r'class="[^"]*tgme_widget_message_document_icon[^"]*\baudio\b[^"]*"',
            document,
            re.IGNORECASE,
        ):
            continue

        title_match = re.search(
            r'<div\s+class="[^"]*tgme_widget_message_document_title[^"]*"[^>]*>',
            document,
            re.IGNORECASE,
        )
        if not title_match:
            continue

        title_end = _telegram_matching_div_end(document, title_match.start())
        title = _telegram_html_text(
            document[title_match.end():title_end]
        ) if title_end is not None else ""
        href = unescape(href)
        if href.startswith("/"):
            href = "https://t.me" + href

        id_match = re.search(r"/([0-9]+)(?:\?|$)", href)
        post_id = id_match.group(1) if id_match else ""
        tracks.append({
            "id": post_id,
            "title": title or "Audio",
            "text": "",
            "date": date,
            "has_media": True,
            "is_audio": True,
            "url": href or f"https://t.me/{username}",
        })

    return tracks


def telegram_channel_username(value):
    """Normalize only public t.me channel/group links to a username."""
    value = (value or "").strip()
    if not value:
        return None

    if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE):
        value = "https://" + value.lstrip("@")

    try:
        parsed = urlparse(value)
        host = (parsed.netloc or "").lower().split(":")[0]
        if host not in ("t.me", "www.t.me", "telegram.me", "www.telegram.me"):
            return None

        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].lower() == "s":
            parts = parts[1:]

        if len(parts) != 1:
            return None

        username = parts[0].lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            return None

        return username
    except Exception:
        return None


def load_telegram_channels():
    try:
        with TELEGRAM_CHANNELS_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"⚠️ Telegram channels: {e}")
        return []

    if not isinstance(data, list):
        return []

    channels = []
    known = set()
    for item in data:
        username = telegram_channel_username(
            item.get("url") if isinstance(item, dict) else item
        )
        if not username or username.lower() in known:
            continue
        known.add(username.lower())
        channels.append({
            "username": username,
            "title": (item.get("title") if isinstance(item, dict) else "") or username,
            "url": f"https://t.me/{username}",
        })
    return channels


def save_telegram_channels(channels):
    try:
        with TELEGRAM_CHANNELS_PATH.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(channels, file, ensure_ascii=False, indent=2)
            file.write("\n")
        return True
    except Exception as e:
        print(f"❌ Telegram channels: {e}")
        return False


def fetch_telegram_public_posts(channel):
    """Fetch the latest posts exposed by Telegram's public web preview.

    This endpoint intentionally has no account/session. Telegram does not expose
    downloadable audio documents here; such messages are only marked as media.
    """
    username = channel["username"]
    request = urllib.request.Request(
        f"https://t.me/s/{username}",
        headers={"User-Agent": "Mozilla/5.0 (Music CLI public preview)"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"Telegram: {e}"

    title_match = re.search(
        r'<meta\s+property="og:title"\s+content="([^"]+)"',
        page,
        re.IGNORECASE,
    )
    if title_match:
        title = _telegram_html_text(title_match.group(1))
        if title:
            channel["title"] = title

    blocks = re.split(
        r'(?=<div\s+class="tgme_widget_message_wrap\s+js-widget_message_wrap")',
        page,
        flags=re.IGNORECASE,
    )
    posts = []

    for block in blocks:
        post_match = re.search(r'data-post="([^"/]+)/([0-9]+)"', block)
        if not post_match:
            continue

        post_id = post_match.group(2)
        text = _telegram_message_text(block)
        date_match = re.search(r'<time\s+datetime="([^"]+)"', block, re.IGNORECASE)
        date = date_match.group(1).replace("T", " ").replace("+00:00", " UTC") if date_match else ""
        has_media = bool(re.search(r'media_(?:supported|not_supported)_cont', block, re.IGNORECASE))
        tracks = _telegram_document_tracks(block, username, date)

        posts.append({
            "id": post_id,
            "text": text,
            "date": date,
            "has_media": has_media,
            "tracks": tracks,
            "url": f"https://t.me/{username}/{post_id}",
        })

    if not posts:
        return None, "Публичные посты не найдены. Возможно, ссылка приватная или канал недоступен."

    posts.sort(key=lambda post: int(post["id"]), reverse=True)
    return posts[:TELEGRAM_PREVIEW_LIMIT], None


def show_telegram_post(channel, post):
    clear_screen()
    item_title = post.get("title") or channel.get("title") or channel["username"]
    render_header(
        item_title,
        subtitle=f"Post {post['id']} • {post['date'] or 'public preview'}",
    )
    print()
    if post.get("is_audio"):
        print("Public audio post")
    else:
        print(post["text"] or "[Media post — text is unavailable in the public preview]")
    print()
    print("─" * terminal_width())
    print(post["url"])
    if post["has_media"]:
        print(ANSI_DIM + "Media detected. Playback/download needs Telegram login; public preview has no file URL." + ANSI_RESET)
    pause()


def view_telegram_channel(channel):
    clear_screen()
    render_header("TELEGRAM", subtitle=f"Loading public posts from @{channel['username']}…")

    posts, error = fetch_telegram_public_posts(channel)
    if error:
        print()
        print(f"❌ {error}")
        pause()
        return

    while True:
        audio_tracks = [
            track
            for post in posts
            for track in post.get("tracks", [])
        ]
        items = audio_tracks or posts
        rows = []
        for index, post in enumerate(items, start=1):
            text = (post.get("title") or post["text"]).replace("\n", " ").strip()
            rows.append({
                "number": index,
                "left": text or "[Media post]",
                "middle": f"Post {post['id']} • {post['date'] or 'date unavailable'}",
                "right": "AUDIO" if post.get("is_audio") else ("MEDIA" if post["has_media"] else ""),
            })

        selected = select_table(
            rows,
            channel.get("title") or channel["username"],
            subtitle=(
                f"{len(audio_tracks)} audio tracks"
                if audio_tracks
                else f"Latest {len(posts)} public posts"
            ) + f" • @{channel['username']}",
            back_label="Back",
            left_header="POST",
            middle_header="TELEGRAM",
            right_header="",
        )

        if selected is None:
            return

        show_telegram_post(channel, items[selected])


def add_telegram_channel(channels):
    value = prompt_text(
        "ADD TELEGRAM CHANNEL",
        "Public channel/group link",
        subtitle="Example: https://t.me/channel_name",
    )
    if not value:
        return

    username = telegram_channel_username(value)
    if not username:
        clear_screen()
        render_header("ADD TELEGRAM CHANNEL")
        print()
        print("❌ Only public links like https://t.me/channel_name are supported without login.")
        print("Private invite links, t.me/+…, and private chats need Telegram authorization.")
        pause()
        return

    if any(item["username"].lower() == username.lower() for item in channels):
        clear_screen()
        render_header("ADD TELEGRAM CHANNEL")
        print()
        print(f"ℹ️ @{username} is already in the list.")
        pause()
        return

    channel = {
        "username": username,
        "title": username,
        "url": f"https://t.me/{username}",
    }
    channels.append(channel)
    save_telegram_channels(channels)
    view_telegram_channel(channel)
    save_telegram_channels(channels)


def telegram_public_channels_menu():
    while True:
        channels = load_telegram_channels()
        options = [
            (index, channel.get("title") or channel["username"], f"@{channel['username']}")
            for index, channel in enumerate(channels, start=1)
        ]
        add_number = len(options) + 1
        options.append((add_number, "Add channel", "public t.me link, no login"))

        choice = select_simple_menu(
            "TELEGRAM",
            options,
            subtitle="Public channels • latest posts only • no login",
            back_label="Back",
        )

        if choice == "BACK":
            return

        try:
            selected = int(choice)
        except ValueError:
            continue

        if selected == add_number:
            add_telegram_channel(channels)
        elif 1 <= selected <= len(channels):
            view_telegram_channel(channels[selected - 1])
            save_telegram_channels(channels)


# ============================================================
# TELEGRAM: LOCAL BOT API CATALOG
# ============================================================

TELEGRAM_BOT_API = "https://api.telegram.org"
TELEGRAM_BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TELEGRAM_BOT_TRACK_LIMIT = 500


def _telegram_bot_file_mode():
    """Best-effort privacy for a local token file on POSIX file systems."""
    try:
        os.chmod(TELEGRAM_BOTS_PATH, 0o600)
    except Exception:
        pass


def load_telegram_bots():
    try:
        with TELEGRAM_BOTS_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"⚠️ Telegram bots: {e}")
        return []

    if not isinstance(data, list):
        return []

    bots = []
    known_ids = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or "").strip()
        bot_id = str(item.get("id") or "").strip()
        if not token or not bot_id or bot_id in known_ids:
            continue
        known_ids.add(bot_id)
        channels = item.get("channels")
        if not isinstance(channels, list):
            channels = []
        bots.append({
            "id": bot_id,
            "token": token,
            "username": str(item.get("username") or ""),
            "name": str(item.get("name") or item.get("username") or "Telegram bot"),
            "last_update_id": int(item.get("last_update_id") or 0),
            "channels": channels,
        })

    return bots


def save_telegram_bots(bots):
    try:
        with TELEGRAM_BOTS_PATH.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(bots, file, ensure_ascii=False, indent=2)
            file.write("\n")
        _telegram_bot_file_mode()
        return True
    except Exception as e:
        print(f"❌ Telegram bots: {e}")
        return False


def telegram_bot_api(token, method, params=None):
    """Call the official Bot API without exposing a token in error messages."""
    if not token or ":" not in token or any(ch.isspace() for ch in token):
        return None, "Invalid bot token."

    url = f"{TELEGRAM_BOT_API}/bot{token}/{method}"
    if params:
        url += "?" + urlencode(params)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Music CLI Telegram Bot API"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        return None, f"Telegram request failed: {e}"

    if not payload.get("ok"):
        return None, str(payload.get("description") or "Telegram API error.")

    return payload.get("result"), None


def _telegram_bot_display_name(bot):
    username = bot.get("username") or ""
    if username:
        return "@" + username
    return bot.get("name") or "Telegram bot"


def _telegram_bot_channel(bot, chat):
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return None

    for channel in bot["channels"]:
        if str(channel.get("id")) == chat_id:
            channel["title"] = (
                chat.get("title")
                or chat.get("username")
                or channel.get("title")
                or f"Telegram {chat_id}"
            )
            channel["username"] = chat.get("username") or channel.get("username") or ""
            channel["type"] = chat.get("type") or channel.get("type") or ""
            return channel

    channel = {
        "id": chat_id,
        "title": chat.get("title") or chat.get("username") or f"Telegram {chat_id}",
        "username": chat.get("username") or "",
        "type": chat.get("type") or "",
        "tracks": [],
    }
    bot["channels"].append(channel)
    return channel


def _telegram_audio_track(message, channel):
    media = message.get("audio")
    kind = "audio"
    if not isinstance(media, dict):
        media = message.get("voice")
        kind = "voice"
    if not isinstance(media, dict):
        media = message.get("document")
        kind = "document"
    if not isinstance(media, dict):
        return None

    file_name = str(media.get("file_name") or "")
    mime_type = str(media.get("mime_type") or "")
    extension = Path(file_name).suffix.lower()
    if kind == "document" and not (
        mime_type.startswith("audio/")
        or extension in AUDIO_EXTENSIONS
    ):
        return None

    file_id = str(media.get("file_id") or "")
    if not file_id:
        return None

    title = str(media.get("title") or "").strip()
    performer = str(media.get("performer") or "").strip()
    if performer and title:
        label = f"{performer} - {title}"
    elif title:
        label = title
    elif file_name:
        label = Path(file_name).stem
    elif kind == "voice":
        label = "Voice message"
    else:
        label = f"Audio {message.get('message_id') or ''}".strip()

    return {
        "message_id": int(message.get("message_id") or 0),
        "file_id": file_id,
        "file_unique_id": str(media.get("file_unique_id") or ""),
        "file_name": file_name,
        "mime_type": mime_type,
        "title": label,
        "duration_seconds": int(media.get("duration") or 0),
        "file_size": int(media.get("file_size") or 0),
        "date": int(message.get("date") or 0),
        "kind": kind,
        "chat_id": channel["id"],
    }


def _telegram_store_track(channel, track):
    tracks = channel.setdefault("tracks", [])
    key = (
        str(track.get("message_id") or ""),
        str(track.get("file_unique_id") or track.get("file_id") or ""),
    )
    for index, item in enumerate(tracks):
        item_key = (
            str(item.get("message_id") or ""),
            str(item.get("file_unique_id") or item.get("file_id") or ""),
        )
        if item_key == key:
            tracks[index] = track
            break
    else:
        tracks.append(track)

    tracks.sort(
        key=lambda item: (
            int(item.get("date") or 0),
            int(item.get("message_id") or 0),
        ),
        reverse=True,
    )
    del tracks[TELEGRAM_BOT_TRACK_LIMIT:]


def sync_telegram_bot(bot):
    """Import queued Bot API updates into the local channel/audio catalog."""
    offset = int(bot.get("last_update_id") or 0) + 1
    imported = 0
    pages = 0

    while pages < 20:
        updates, error = telegram_bot_api(
            bot["token"],
            "getUpdates",
            {
                "offset": offset,
                "limit": 100,
                "timeout": 0,
                "allowed_updates": json.dumps(
                    ["message", "channel_post", "my_chat_member"],
                    separators=(",", ":"),
                ),
            },
        )
        if error:
            return imported, error

        if not updates:
            break

        for update in updates:
            update_id = int(update.get("update_id") or 0)
            if update_id:
                bot["last_update_id"] = max(
                    int(bot.get("last_update_id") or 0),
                    update_id,
                )
                offset = update_id + 1

            message = update.get("channel_post") or update.get("message")
            membership = update.get("my_chat_member")
            if isinstance(membership, dict):
                chat = membership.get("chat")
                if isinstance(chat, dict):
                    _telegram_bot_channel(bot, chat)

            if not isinstance(message, dict):
                continue

            chat = message.get("chat")
            if not isinstance(chat, dict):
                continue
            channel = _telegram_bot_channel(bot, chat)
            if not channel:
                continue

            track = _telegram_audio_track(message, channel)
            if track:
                _telegram_store_track(channel, track)
                imported += 1

        pages += 1
        if len(updates) < 100:
            break

    bot["channels"].sort(
        key=lambda channel: str(channel.get("title") or "").lower()
    )
    return imported, None


def add_telegram_bot(bots):
    token = prompt_text(
        "ADD TELEGRAM BOT",
        "BotFather token",
        subtitle="Stored only on this iPhone • never shown in the menu",
    )
    if not token:
        return

    identity, error = telegram_bot_api(token, "getMe")
    if error or not isinstance(identity, dict):
        clear_screen()
        render_header("ADD TELEGRAM BOT")
        print()
        print(f"❌ {error or 'Invalid bot response.'}")
        pause()
        return

    bot_id = str(identity.get("id") or "")
    if not bot_id:
        clear_screen()
        render_header("ADD TELEGRAM BOT")
        print()
        print("❌ Telegram did not return a bot ID.")
        pause()
        return

    if any(str(bot.get("id")) == bot_id for bot in bots):
        clear_screen()
        render_header("ADD TELEGRAM BOT")
        print()
        print("ℹ️ This bot is already configured.")
        pause()
        return

    bot = {
        "id": bot_id,
        "token": token,
        "username": str(identity.get("username") or ""),
        "name": str(identity.get("first_name") or identity.get("username") or "Telegram bot"),
        "last_update_id": 0,
        "channels": [],
    }
    bots.append(bot)
    _, sync_error = sync_telegram_bot(bot)
    save_telegram_bots(bots)

    clear_screen()
    render_header("ADD TELEGRAM BOT")
    print()
    print(f"✅ Added {_telegram_bot_display_name(bot)}")
    print("Add this bot as a channel administrator, then post audio and open the bot here to sync.")
    if sync_error:
        print(ANSI_DIM + f"Sync: {sync_error}" + ANSI_RESET)
    pause()


def telegram_file_extension(track):
    suffix = Path(str(track.get("file_name") or "")).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return suffix
    mime_type = str(track.get("mime_type") or "").lower()
    return {
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/flac": ".flac",
        "audio/wav": ".wav",
    }.get(mime_type, ".ogg" if track.get("kind") == "voice" else ".m4a")


def download_telegram_track(bot, channel, track, rebuild=True):
    size = int(track.get("file_size") or 0)
    if size > TELEGRAM_BOT_DOWNLOAD_LIMIT:
        print("❌ Telegram Bot API downloads are limited to 20 MB per file.")
        return False

    file_info, error = telegram_bot_api(
        bot["token"],
        "getFile",
        {"file_id": track["file_id"]},
    )
    file_path = file_info.get("file_path") if isinstance(file_info, dict) else None
    if error or not file_path:
        print(f"❌ {error or 'Telegram did not return a file path.'}")
        return False

    folder = DOWNLOAD_DIR / "Telegram" / safe_filename(
        channel.get("title") or channel["id"]
    )
    folder.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(track.get("title") or "Telegram audio")
    destination = folder / (filename + telegram_file_extension(track))
    if destination.exists():
        print(f"⏭ Already downloaded: {destination.name}")
    else:
        temporary = destination.with_name("." + destination.name + ".part")
        encoded_path = quote(str(file_path), safe="/")
        url = f"{TELEGRAM_BOT_API}/file/bot{bot['token']}/{encoded_path}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Music CLI Telegram Bot API"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
            temporary.replace(destination)
        except Exception as e:
            try:
                if temporary.exists():
                    temporary.unlink()
            except Exception:
                pass
            print(f"❌ Telegram download: {e}")
            return False

        if rebuild:
            rebuild_playlist_silent()
        print(f"✅ {destination.name}")

    return True


def download_telegram_playlist(bot, channel):
    tracks = channel.get("tracks") or []
    if not tracks:
        return

    if not prompt_confirm(
        channel.get("title") or channel["id"],
        f"Download all {len(tracks)} tracks?",
        default=True,
        subtitle="Existing files will be skipped",
    ):
        return

    clear_screen()
    render_header(
        "TELEGRAM DOWNLOAD ALL",
        subtitle=f"{channel.get('title') or channel['id']} • {len(tracks)} tracks",
    )
    succeeded = 0
    failed = 0
    for index, track in enumerate(tracks, start=1):
        print()
        print(f"[{index}/{len(tracks)}] {track.get('title') or 'Telegram audio'}")
        if download_telegram_track(bot, channel, track, rebuild=False):
            succeeded += 1
        else:
            failed += 1

    if succeeded:
        rebuild_playlist_silent()

    print()
    print("─" * terminal_width())
    print(f"Downloaded or already present: {succeeded}")
    if failed:
        print(f"Failed: {failed}")
    pause()


def telegram_track_menu(bot, channel):
    tracks = channel.get("tracks") or []
    if not tracks:
        clear_screen()
        render_header(channel.get("title") or channel["id"])
        print()
        print("No audio received from this bot yet.")
        print("Add the bot as a channel administrator, publish audio, then refresh.")
        pause()
        return

    while True:
        rows = [{
            "number": 0,
            "left": "Download all",
            "middle": f"{len(tracks)} tracks • save the whole playlist",
            "right": "",
        }] + [
            {
                "number": index,
                "left": track.get("title") or "Telegram audio",
                "middle": track.get("file_name") or "Telegram audio",
                "right": duration_text(track),
            }
            for index, track in enumerate(tracks, start=1)
        ]
        selected = select_table(
            rows,
            channel.get("title") or channel["id"],
            subtitle=f"{len(tracks)} audio tracks • {_telegram_bot_display_name(bot)}",
            back_label="Back",
            left_header="TRACK",
            middle_header="FILE",
            right_header="TIME",
        )
        if selected is None:
            return

        if selected == 0:
            download_telegram_playlist(bot, channel)
        else:
            track = tracks[selected - 1]
            clear_screen()
            render_header("TELEGRAM DOWNLOAD", subtitle=track.get("title"))
            download_telegram_track(bot, channel, track)
            pause()


def telegram_bot_channels_menu(bot, bots):
    while True:
        clear_screen()
        render_header("TELEGRAM BOT", subtitle=f"Syncing {_telegram_bot_display_name(bot)}…")
        imported, error = sync_telegram_bot(bot)
        save_telegram_bots(bots)

        if error:
            print()
            print(f"⚠️ {error}")
            print("If this bot uses a webhook, remove it before using this local polling mode.")
            pause()

        channels = bot.get("channels") or []
        options = [
            (
                index,
                channel.get("title") or channel.get("id") or "Telegram channel",
                f"{len(channel.get('tracks') or [])} audio • {channel.get('type') or 'chat'}",
            )
            for index, channel in enumerate(channels, start=1)
        ]
        refresh_number = len(options) + 1
        options.append((refresh_number, "Refresh", "get new Bot API updates"))

        choice = select_simple_menu(
            "TELEGRAM BOT",
            options,
            subtitle=(
                f"{_telegram_bot_display_name(bot)} • {len(channels)} discovered chats"
                + (f" • {imported} new audio" if imported else "")
            ),
            back_label="Back",
        )
        if choice == "BACK":
            return

        try:
            selected = int(choice)
        except ValueError:
            continue
        if selected == refresh_number:
            continue
        if 1 <= selected <= len(channels):
            telegram_track_menu(bot, channels[selected - 1])


def telegram_channels_menu():
    while True:
        bots = load_telegram_bots()
        options = [
            (
                index,
                _telegram_bot_display_name(bot),
                f"{len(bot.get('channels') or [])} discovered chats",
            )
            for index, bot in enumerate(bots, start=1)
        ]
        add_number = len(options) + 1
        options.append((add_number, "Add bot token", "personal BotFather token, stored locally"))

        choice = select_simple_menu(
            "TELEGRAM",
            options,
            subtitle="Multiple personal bots • new posts only • no shared token",
            back_label="Back",
        )
        if choice == "BACK":
            return

        try:
            selected = int(choice)
        except ValueError:
            continue
        if selected == add_number:
            add_telegram_bot(bots)
        elif 1 <= selected <= len(bots):
            telegram_bot_channels_menu(bots[selected - 1], bots)


# ============================================================
# LIBRARY INFO
# ============================================================

def library_info():
    tracks = scan_local_music()
    albums = 0
    if ALBUMS_DIR.exists():
        albums = len([p for p in ALBUMS_DIR.iterdir() if p.is_dir()])

    clear_screen()
    render_header("LIBRARY INFO")
    width = terminal_width()

    rows = [
        ("Tracks", str(len(tracks))),
        ("Albums", str(albums)),
        ("VLC", str(VLC_DIR)),
        ("Downloads", str(DOWNLOAD_DIR)),
        ("Albums dir", str(ALBUMS_DIR)),
        ("Playlist", str(PLAYLIST_PATH)),
    ]

    label_w = 12 if width < 72 else 16
    print()
    for label, value in rows:
        value_w = max(1, width - label_w - 1)
        print(fit_text(label, label_w) + " " + fit_text(value, value_w).rstrip())
    print("─" * width)
    print(ANSI_DIM + "[..] ← Back" + ANSI_RESET)


# ============================================================
# MAIN MENU
# ============================================================

def main():
    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    ALBUMS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    while True:
        choice = select_simple_menu(
            "MUSIC CLI",
            [
                (
                    1,
                    "Playlists",
                    "create / rebuild local M3U playlists",
                ),
                (
                    2,
                    "Search",
                    "tracks, track URL, albums, album URL",
                ),
                (
                    3,
                    "Download by URL",
                    "playlists, direct audio and other resources",
                ),
                (
                    4,
                    "Telegram",
                    "saved public channels and latest posts",
                ),
                (
                    5,
                    "Library Info",
                    "local library information",
                ),
            ],
            subtitle=(
                f"VLC: {VLC_DIR} • tap open • swipe scroll"
            ),
            back_label="Exit",
        )

        if choice == "1":
            clear_screen()
            playlist_mode()
            pause()

        elif choice == "2":
            search_menu()

        elif choice == "3":
            download_by_url()

        elif choice == "4":
            telegram_channels_menu()

        elif choice == "5":
            clear_screen()
            library_info()
            pause()

        else:
            clear_screen()
            print("Exit.")
            return


if __name__ == "__main__":
    try:
        main()
    finally:
        disable_mouse_reporting()
