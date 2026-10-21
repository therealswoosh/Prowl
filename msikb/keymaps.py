"""Keymap handling: X11/evdev keycodes <-> MSI lighting keycodes <-> key names.

The reference MSI keymaps use **X11 keycodes** (evdev code + 8), i.e. exactly the
numbers ``xev`` prints and the numbers ``msi-perkeyrgb`` configuration files use.
Keeping that convention means existing config files and every forum post about
MSI RGB keycodes stay usable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .protocol import KEYCODE_REGION, ProtocolError

# Keymaps live in the repository's data/ dir; an installed copy may carry its own
# msikb/data/keymaps. Both are searched, in that order.
DATA_DIRS = (
    Path(__file__).resolve().parent.parent / "data" / "keymaps",
    Path(__file__).resolve().parent / "data" / "keymaps",
)
X11_OFFSET = 8


def find_keymap_file(name: str) -> Optional[Path]:
    for directory in DATA_DIRS:
        candidate = directory / ("%s.json" % name)
        if candidate.is_file():
            return candidate
    return None


def available_keymaps() -> List[str]:
    found = set()
    for directory in DATA_DIRS:
        if directory.is_dir():
            found.update(path.stem for path in directory.glob("*.json"))
    return sorted(found)

# evdev keycode -> short Linux input key name (KEY_ prefix stripped).
EVDEV_NAMES: Dict[int, str] = {
    1: "esc", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8",
    10: "9", 11: "0", 12: "minus", 13: "equal", 14: "backspace", 15: "tab",
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u", 23: "i",
    24: "o", 25: "p", 26: "leftbrace", 27: "rightbrace", 28: "enter",
    29: "leftctrl", 30: "a", 31: "s", 32: "d", 33: "f", 34: "g", 35: "h",
    36: "j", 37: "k", 38: "l", 39: "semicolon", 40: "apostrophe", 41: "grave",
    42: "leftshift", 43: "backslash", 44: "z", 45: "x", 46: "c", 47: "v",
    48: "b", 49: "n", 50: "m", 51: "comma", 52: "dot", 53: "slash",
    54: "rightshift", 55: "kpasterisk", 56: "leftalt", 57: "space",
    58: "capslock", 59: "f1", 60: "f2", 61: "f3", 62: "f4", 63: "f5", 64: "f6",
    65: "f7", 66: "f8", 67: "f9", 68: "f10", 69: "numlock", 70: "scrolllock",
    71: "kp7", 72: "kp8", 73: "kp9", 74: "kpminus", 75: "kp4", 76: "kp5",
    77: "kp6", 78: "kpplus", 79: "kp1", 80: "kp2", 81: "kp3", 82: "kp0",
    83: "kpdot", 84: "102nd", 85: "zenkakuhankaku", 86: "backslash2",
    87: "f11", 88: "f12", 89: "ro", 90: "katakana", 91: "hiragana",
    92: "henkan", 93: "katakanahiragana", 94: "muhenkan", 95: "kpjpcomma",
    96: "kpenter", 97: "rightctrl", 98: "kpslash", 99: "sysrq",
    100: "rightalt", 102: "home", 103: "up", 104: "pageup", 105: "left",
    106: "right", 107: "end", 108: "down", 109: "pagedown", 110: "insert",
    111: "delete", 112: "macro", 113: "mute", 114: "volumedown",
    115: "volumeup", 116: "power", 117: "kpequal", 118: "kpplusminus",
    119: "pause", 121: "kpcomma", 125: "leftmeta", 126: "rightmeta",
    127: "compose",
}

# Aliases mirror msi-perkeyrgb's config-file aliases so existing configs still work.
ALIASES: Dict[str, Tuple[str, ...]] = {
    "f_row": tuple("f%d" % i for i in range(1, 13)),
    "arrows": ("up", "left", "right", "down"),
    "num_row": ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "minus", "equal"),
    "numpad": (
        "kpasterisk", "numlock", "kp7", "kp8", "kp9", "kpminus", "kp4", "kp5",
        "kp6", "kpplus", "kp1", "kp2", "kp3", "kp0", "kpdot", "kpenter", "kpslash",
    ),
    "characters": (
        "q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "leftbrace", "rightbrace",
        "a", "s", "d", "f", "g", "h", "j", "k", "l", "semicolon", "apostrophe",
        "z", "x", "c", "v", "b", "n", "m", "comma", "dot", "slash",
    ),
}
ALIASES["all"] = ()  # filled in per keymap ("every key in the map" + fn)


@dataclass(frozen=True)
class Entry:
    name: str
    x11: int
    msi: int
    region: Optional[str]


class KeymapError(ValueError):
    pass


class Keymap:
    """A model keymap loaded from JSON."""

    def __init__(self, name: str, x11_to_msi: Dict[int, int], fn_code: int = 240,
                 source: str = "", notes: str = "", unnamed_msi: Sequence[int] = ()):
        self.name = name
        self.source = source
        self.notes = notes
        self.fn_code = fn_code
        self.x11_to_msi = dict(x11_to_msi)
        # LEDs the controller addresses that no X11 keycode describes (media keys,
        # vendor keys, ...). They still need a colour in every whole-keyboard frame,
        # otherwise they keep the colour the last writer left on them. They are
        # addressable by name as ``msi50`` and sort out of range listings.
        self.unnamed_msi = sorted({int(c) for c in unnamed_msi})
        self._entries: Dict[str, Entry] = {}
        self._by_x11: Dict[int, Entry] = {}
        for x11, msi in self.x11_to_msi.items():
            evdev = x11 - X11_OFFSET
            base = EVDEV_NAMES.get(evdev, "keycode%d" % x11)
            name = base
            suffix = 1
            while name in self._entries:  # namespace collisions, keep both usable
                suffix += 1
                name = "%s_%d" % (base, suffix)
            entry = Entry(name=name, x11=x11, msi=msi, region=KEYCODE_REGION.get(msi))
            self._entries[name] = entry
            self._by_x11[x11] = entry
        for msi in self.unnamed_msi:
            self._entries.setdefault(
                "msi%d" % msi,
                Entry(name="msi%d" % msi, x11=-1, msi=msi, region=KEYCODE_REGION.get(msi)),
            )
        fn_entry = Entry(name="fn", x11=-1, msi=fn_code, region=KEYCODE_REGION.get(fn_code))
        self._entries.setdefault("fn", fn_entry)

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_file(cls, path: Path) -> "Keymap":
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        x11_to_msi = {int(k): int(v) for k, v in raw.get("x11_to_msi", {}).items()}
        if not x11_to_msi:
            raise KeymapError("%s contains no x11_to_msi entries" % path)
        return cls(
            name=raw.get("name", path.stem),
            x11_to_msi=x11_to_msi,
            fn_code=int(raw.get("fn_code", 240)),
            source=raw.get("source", ""),
            notes=raw.get("notes", ""),
            unnamed_msi=raw.get("unnamed_msi", ()),
        )

    @classmethod
    def by_name(cls, name_or_path: str) -> "Keymap":
        candidate = Path(name_or_path)
        if candidate.is_file():
            return cls.from_file(candidate)
        json_path = find_keymap_file(name_or_path)
        if json_path is not None:
            return cls.from_file(json_path)
        raise KeymapError(
            "unknown keymap %r (available: %s)"
            % (name_or_path, ", ".join(available_keymaps()) or "none")
        )

    # ------------------------------------------------------------------ lookups
    def entries(self) -> List[Entry]:
        return [e for e in self._entries.values() if e.name != "fn"]

    def entry(self, name: str) -> Entry:
        try:
            return self._entries[name.lower()]
        except KeyError:
            raise KeymapError(
                "unknown key %r for keymap %s (try `msikb keys --keymap %s`)"
                % (name, self.name, self.name)
            ) from None

    def code_for(self, token: str) -> int:
        """Resolve a name, alias, or X11 keycode into an MSI keycode."""
        token = token.strip().lower()
        if not token:
            raise KeymapError("empty key spec")
        if token in self._entries:
            return self._entries[token].msi
        if token in ALIASES:
            codes = self.codes_for_spec(token)
            if len(codes) != 1:
                raise KeymapError("%r expands to %d keys, not one" % (token, len(codes)))
            return codes[0]
        if re.fullmatch(r"[0-9]+", token):
            x11 = int(token)
            entry = self._by_x11.get(x11)
            if entry is None:
                raise KeymapError(
                    "X11 keycode %d is not in keymap %s -- capture a keymap with `learn`"
                    % (x11, self.name)
                )
            return entry.msi
        raise KeymapError("unknown key %r" % token)

    def codes_for_spec(self, spec: str) -> List[int]:
        """Expand a comma-separated spec of names, aliases and ranges."""
        codes: List[int] = []
        for token in (part.strip() for part in spec.split(",")):
            if not token:
                continue
            if token in ALIASES:
                if token == "all":
                    names = [e.name for e in self.entries()] + ["fn"]
                else:
                    names = [n for n in ALIASES[token] if n in self._entries]
                if not names:
                    # A GS65 alias such as `numpad` has nothing to expand to on a
                    # keyboard without that cluster; say so instead of silently
                    # sending no keys at all.
                    raise KeymapError(
                        "alias %r has no keys on this model (keymap %s)" % (token, self.name)
                    )
                codes.extend(self._entries[n].msi for n in names)
                continue
            match = re.fullmatch(r"([0-9]+)-([0-9]+)", token)
            if match:
                start, end = int(match.group(1)), int(match.group(2))
                if end <= start:
                    raise KeymapError("invalid range %r" % token)
                missing = [x11 for x11 in range(start, end + 1) if x11 not in self._by_x11]
                if missing:
                    raise KeymapError(
                        "range %r covers keycodes not in this keymap: %s"
                        % (token, ", ".join(str(m) for m in missing[:8]))
                    )
                codes.extend(self._by_x11[x11].msi for x11 in range(start, end + 1))
                continue
            codes.append(self.code_for(token))
        if len(set(codes)) != len(codes):
            codes = list(dict.fromkeys(codes))
        return codes

    def name_for_msi(self, msi: int) -> str:
        for entry in self._entries.values():
            if entry.msi == msi:
                return entry.name
        return "unknown(%d)" % msi

    def x11_for_msi(self, msi: int) -> Optional[int]:
        for entry in self._entries.values():
            if entry.msi == msi:
                return entry.x11
        return None

    def to_json(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "source": self.source or "captured from SteelSeries GG on this machine",
            "notes": self.notes,
            "fn_code": self.fn_code,
            "x11_to_msi": {str(k): v for k, v in sorted(self.x11_to_msi.items())},
            "unnamed_msi": list(self.unnamed_msi),
        }

    def unused_regions(self) -> List[str]:
        used = {e.region for e in self.entries() if e.region}
        return [r for r in ("alphanum", "enter", "modifiers", "numpad") if r not in used]

    def unmapped_msi_codes(self) -> List[int]:
        return sorted({e.msi for e in self.entries() if e.region is None})


def keymap_from_msi_mapping(name: str, name_to_msi: Dict[str, int], **kwargs) -> Keymap:
    """Build a Keymap from ``{key name: MSI keycode}`` (used by `learn`)."""
    x11_to_msi: Dict[int, int] = {}
    unknown = []
    for key_name, msi in name_to_msi.items():
        if key_name == "fn":
            kwargs.setdefault("fn_code", int(msi))
            continue
        x11 = None
        for evdev, ev_name in EVDEV_NAMES.items():
            if ev_name == key_name:
                x11 = evdev + X11_OFFSET
                break
        if x11 is None:
            unknown.append(key_name)
            continue
        x11_to_msi[x11] = int(msi)
    if unknown:
        raise KeymapError("cannot map these names to X11 keycodes: %s" % ", ".join(sorted(unknown)))
    return Keymap(name=name, x11_to_msi=x11_to_msi, **kwargs)


def default_keymap() -> Keymap:
    """Prefer the keymap measured on this model; fall back to the GS65 reference."""
    try:
        return Keymap.by_name("gp66-leopard")
    except KeymapError:
        return Keymap.by_name("reference-gs65")
