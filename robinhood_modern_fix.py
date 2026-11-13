"""
Patch Game.exe (Robin Hood: The Legend of Sherwood 1.1.0.0 GOG) so the render,
mouse input, and camera panning loop runs at ~60 FPS (16 ms frame interval)
while keeping the game simulation tick (FUN_004c6ef0) locked to 25 Hz (40 ms).

Usage:
    python tools/patch_fps.py           # backup Game.exe -> Game.exe.bak and apply patch
    python tools/patch_fps.py --revert  # restore Game.exe from Game.exe.bak
"""

import argparse
import hashlib
import shutil
import struct
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    _script_dir = Path(__file__).resolve().parent
    ROOT = _script_dir.parent if (_script_dir.parent / "Game.exe").exists() else _script_dir
    if not (ROOT / "Game.exe").exists() and (Path.cwd() / "Game.exe").exists():
        ROOT = Path.cwd()
GAME_EXE = ROOT / "Game.exe"
BACKUP_EXE = ROOT / "Game.exe.bak"
PROFILES_PATH = ROOT / "DATA" / "Savegame" / "Profiles"
PROFILES_BAK = ROOT / "DATA" / "Savegame" / "Profiles.bak"
PROFILES_RES_OFF = 0x104
DEFAULT_RES = "1920x1080"

EXPECTED_ORIG_SHA256 = "b0963242470d96cdd8021e2cb81ccbc5e26b4fb2cb122febdec50ad30774f08e"

# VA 0x0050fee6 -> Raw 0x0010fee6: call 0x004c6ef0 (e8 05 70 fb ff) -> call 0x00676e50
SIM_CALL_OFF = 0x10FEE6
ORIG_SIM_CALL = bytes.fromhex("e8 05 70 fb ff")
PATCH_SIM_CALL = bytes.fromhex("e8 65 6f 16 00")

# VA 0x00510114..0x00510125 -> Raw 0x00110114..0x00110125 (18 bytes):
# Replace 100%-CPU busy-wait loop with Sleep(1)-yielding 16 ms wait cave @ 0x00676e90
WAIT_LOOP_OFF = 0x110114
ORIG_WAIT_LOOP = bytes.fromhex(
    "ff d7 2b c6 83 f8 28 73 09 ff d7 2b c6 83 f8 28 72 f7"
)
PATCH_WAIT_LOOP = bytes.fromhex("e8 77 6d 16 00 eb 0b") + b"\x90" * 11

WAIT_CAVE_OFF = 0x276E90
WAIT_CAVE_BYTES = bytes.fromhex(
    "ff d7"                 # wait_loop: call edi (GetTickCount)
    "2b c6"                 # sub eax, esi (elapsed)
    "83 f8 10"              # cmp eax, 0x10 (16 ms = ~62.5 FPS)
    "73 0f"                 # jae done (+0x0f -> 0x676ea8)
    "83 f8 0c"              # cmp eax, 0x0c (remaining <= 4 ms -> spin final ms)
    "73 f2"                 # jae wait_loop (-0x0e -> 0x676e90)
    "6a 00"                 # push 0 (Sleep(0) avoids 15.6ms Windows timer overshoot)
    "ff 15 b0 70 67 00"     # call dword ptr [0x006770b0] (Sleep)
    "eb e8"                 # jmp wait_loop (-0x18 -> 0x676e90)
    "c3"                    # done: ret
)

# PE Characteristics (LAA: 0x010f -> 0x012f, computed from e_lfanew + 0x16)
IMAGE_FILE_LARGE_ADDRESS_AWARE = 0x0020

# Code cave in .text trailing padding: VA 0x00676e50 -> Raw 0x00276e50
# BSS scratch DWORD in .data: VA 0x006c3800
CAVE_OFF = 0x276E50
CAVE_BYTES = bytes.fromhex(
    "51"                    # push ecx
    "ff 15 8c 71 67 00"     # call dword ptr [0x0067718c] (GetTickCount)
    "8b 15 00 38 6c 00"     # mov edx, [0x006c3800] (last_sim_tick)
    "89 c1"                 # mov ecx, eax
    "29 d1"                 # sub ecx, edx (elapsed)
    "83 f9 28"              # cmp ecx, 0x28 (40 ms = 25 Hz)
    "72 13"                 # jb skip_sim (+0x13 -> 0x676e79)
    "83 f9 50"              # cmp ecx, 0x50 (80 ms)
    "73 03"                 # jae reset_tick (+0x03 -> 0x676e6e)
    "8d 42 28"              # lea eax, [edx+0x28]
    "a3 00 38 6c 00"        # mov [0x006c3800], eax
    "59"                    # pop ecx
    "e9 77 00 e5 ff"        # jmp 0x004c6ef0
    "59"                    # skip_sim: pop ecx
    "8b 85 50 35 00 00"     # mov eax, [ebp+0x3550]
    "c3"                    # ret
)

# Remove camera panning jitter while keeping smooth 1.05x exponential ramp & deceleration coasting (default),
# or optionally use instant linear panning (--linear-pan).
PAN_INIT_VAL_OFF = 0x0BEF78
ORIG_PAN_INIT_VAL = bytes.fromhex("00 00 c0 40")  # 6.0f
DEFAULT_PAN_SPEED = 2.5  # Smooth ramp: 2.5f * 1.05^30 ~= 10.8f

PAN_JITTER_JZ_OFF = 0x0BEFA7
PAN_RAMP_JZ_OFF = 0x0BEFF4
ORIG_JZ_0E = bytes.fromhex("74 0e")
PATCH_JMP_0E = bytes.fromhex("eb 0e")

# 2) FUN_004c8380 @ 0x004c85af & 0x004c8610: stop panning immediately instead of 31-frame coasting
COAST_X_OFF = 0x0C85AF
ORIG_COAST_X = bytes.fromhex("66 8b 86 82 47 00 00 66 3b c5 74 47")
PATCH_COAST_X = bytes.fromhex("66 89 ae 82 47 00 00 89 ae b8 48 00 00 eb 44")

COAST_Y_OFF = 0x0C8610
ORIG_COAST_Y = bytes.fromhex("66 8b 86 84 47 00 00 66 3b c5 74 47")
PATCH_COAST_Y = bytes.fromhex("66 89 ae 84 47 00 00 89 ae bc 48 00 00 eb 44")

# 3) Force offscreen DirectDraw surfaces into DDSCAPS_SYSTEMMEMORY | DDSCAPS_OFFSCREENPLAIN (0x0840)
#    to eliminate GPU<->CPU Lock() readback stalls in D3D11 wrappers during software alpha/stretching blits.
SURF_NONLOCAL_OFF = 0x1E4FED
ORIG_SURF_NONLOCAL = bytes.fromhex("40 40 00 20")  # 0x20004040
SURF_VIDMEM_OFF = 0x1E4FFC
ORIG_SURF_VIDMEM = bytes.fromhex("40 40 00 00")    # 0x00004040
PATCH_SURF_SYSMEM = bytes.fromhex("40 08 00 00")   # 0x00000840


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_patch(pan_speed: float = DEFAULT_PAN_SPEED, sysmem: bool = True, linear_pan: bool = False) -> None:
    if not GAME_EXE.exists():
        raise FileNotFoundError(f"Missing {GAME_EXE}")

    if not BACKUP_EXE.exists():
        shutil.copy2(GAME_EXE, BACKUP_EXE)
        print(f"Backed up {GAME_EXE.name} -> {BACKUP_EXE.name}")

    data = bytearray(BACKUP_EXE.read_bytes())
    orig_hash = hashlib.sha256(data).hexdigest()
    if orig_hash != EXPECTED_ORIG_SHA256:
        raise RuntimeError(
            f"Unexpected backup SHA256 {orig_hash} (expected {EXPECTED_ORIG_SHA256})"
        )

    assert data[SIM_CALL_OFF : SIM_CALL_OFF + 5] == ORIG_SIM_CALL, "Sim call mismatch"
    assert data[WAIT_LOOP_OFF : WAIT_LOOP_OFF + len(ORIG_WAIT_LOOP)] == ORIG_WAIT_LOOP, "Wait loop mismatch"
    assert all(b == 0 for b in data[CAVE_OFF : CAVE_OFF + len(CAVE_BYTES)]), "Cave not empty"
    assert all(b == 0 for b in data[WAIT_CAVE_OFF : WAIT_CAVE_OFF + len(WAIT_CAVE_BYTES)]), "Wait cave not empty"
    assert data[PAN_INIT_VAL_OFF : PAN_INIT_VAL_OFF + 4] == ORIG_PAN_INIT_VAL, "Pan init val mismatch"
    assert data[PAN_JITTER_JZ_OFF : PAN_JITTER_JZ_OFF + 2] == ORIG_JZ_0E, "Pan jitter jz mismatch"
    assert data[PAN_RAMP_JZ_OFF : PAN_RAMP_JZ_OFF + 2] == ORIG_JZ_0E, "Pan ramp jz mismatch"
    assert data[COAST_X_OFF : COAST_X_OFF + len(ORIG_COAST_X)] == ORIG_COAST_X, "Coast X mismatch"
    assert data[COAST_Y_OFF : COAST_Y_OFF + len(ORIG_COAST_Y)] == ORIG_COAST_Y, "Coast Y mismatch"
    assert data[SURF_NONLOCAL_OFF : SURF_NONLOCAL_OFF + 4] == ORIG_SURF_NONLOCAL, "Surf nonlocal mismatch"
    assert data[SURF_VIDMEM_OFF : SURF_VIDMEM_OFF + 4] == ORIG_SURF_VIDMEM, "Surf vidmem mismatch"

    data[SIM_CALL_OFF : SIM_CALL_OFF + 5] = PATCH_SIM_CALL
    data[WAIT_LOOP_OFF : WAIT_LOOP_OFF + len(PATCH_WAIT_LOOP)] = PATCH_WAIT_LOOP
    data[CAVE_OFF : CAVE_OFF + len(CAVE_BYTES)] = CAVE_BYTES
    data[WAIT_CAVE_OFF : WAIT_CAVE_OFF + len(WAIT_CAVE_BYTES)] = WAIT_CAVE_BYTES
    data[PAN_INIT_VAL_OFF : PAN_INIT_VAL_OFF + 4] = struct.pack("<f", pan_speed)
    data[PAN_JITTER_JZ_OFF : PAN_JITTER_JZ_OFF + 2] = PATCH_JMP_0E
    if linear_pan:
        data[PAN_RAMP_JZ_OFF : PAN_RAMP_JZ_OFF + 2] = PATCH_JMP_0E
        data[COAST_X_OFF : COAST_X_OFF + len(PATCH_COAST_X)] = PATCH_COAST_X
        data[COAST_Y_OFF : COAST_Y_OFF + len(PATCH_COAST_Y)] = PATCH_COAST_Y
    if sysmem:
        data[SURF_NONLOCAL_OFF : SURF_NONLOCAL_OFF + 4] = PATCH_SURF_SYSMEM
        data[SURF_VIDMEM_OFF : SURF_VIDMEM_OFF + 4] = PATCH_SURF_SYSMEM

    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    chars_off = pe_off + 0x16
    chars = struct.unpack_from("<H", data, chars_off)[0]
    struct.pack_into("<H", data, chars_off, chars | IMAGE_FILE_LARGE_ADDRESS_AWARE)

    GAME_EXE.write_bytes(data)
    print(f"Patched {GAME_EXE.name} successfully (60 FPS Sleep(1)-yielding + 25 Hz sim + linear pan={pan_speed} + LAA + sysmem={sysmem}).")
    print(f"New SHA256: {sha256_file(GAME_EXE)}")


def parse_resolution(res_str: str) -> tuple[int, int]:
    parts = res_str.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"Invalid resolution '{res_str}', expected WxH (e.g. 1920x1080)")
    w, h = int(parts[0]), int(parts[1])
    if w < 640 or h < 480 or w > 7680 or h > 4320:
        raise ValueError(f"Resolution out of supported range: {w}x{h}")
    return w, h


def apply_resolution(width: int, height: int) -> None:
    if not PROFILES_PATH.exists():
        print(f"Note: {PROFILES_PATH} not found yet. Launch the game once to create a profile, then re-run to set {width}x{height}.")
        return
    if not PROFILES_BAK.exists():
        shutil.copy2(PROFILES_PATH, PROFILES_BAK)
        print(f"Backed up {PROFILES_PATH.name} -> {PROFILES_BAK.name}")
    pdata = bytearray(PROFILES_PATH.read_bytes())
    if len(pdata) < PROFILES_RES_OFF + 8:
        raise RuntimeError(f"{PROFILES_PATH} is too small ({len(pdata)} bytes)")
    struct.pack_into("<ff", pdata, PROFILES_RES_OFF, float(width), float(height))
    PROFILES_PATH.write_bytes(pdata)
    print(f"Set resolution in {PROFILES_PATH.relative_to(ROOT)} -> {width}x{height}")


def revert_patch() -> None:
    if not BACKUP_EXE.exists():
        raise FileNotFoundError(f"Backup not found: {BACKUP_EXE}")
    shutil.copy2(BACKUP_EXE, GAME_EXE)
    print(f"Restored {GAME_EXE.name} from {BACKUP_EXE.name} (SHA256: {sha256_file(GAME_EXE)})")
    if PROFILES_BAK.exists():
        shutil.copy2(PROFILES_BAK, PROFILES_PATH)
        print(f"Restored {PROFILES_PATH.name} from {PROFILES_BAK.name}")


def interactive_menu() -> None:
    print("Robin Hood: The Legend of Sherwood - Modern Performance, Resolution & Camera Patch")
    print(f"Target folder: {ROOT}\n")
    print(f"  [1] Apply full fix (60 FPS + 25 Hz sim + smooth pan={DEFAULT_PAN_SPEED} + sysmem + LAA + {DEFAULT_RES})")
    print("  [2] Custom resolution / pan speed")
    print("  [3] Revert Game.exe & Profiles to original backups")
    choice = input("\nSelect an option [1/2/3] (default 1): ").strip() or "1"
    try:
        if choice == "3":
            revert_patch()
        elif choice == "2":
            res_in = input(f"Enter resolution WxH (default {DEFAULT_RES}, or 'skip'): ").strip() or DEFAULT_RES
            pan_val = float(input(f"Enter base pan speed (default {DEFAULT_PAN_SPEED}): ").strip() or DEFAULT_PAN_SPEED)
            apply_patch(pan_val, sysmem=True, linear_pan=False)
            if res_in.lower() != "skip":
                w, h = parse_resolution(res_in)
                apply_resolution(w, h)
        else:
            apply_patch(DEFAULT_PAN_SPEED, sysmem=True, linear_pan=False)
            w, h = parse_resolution(DEFAULT_RES)
            apply_resolution(w, h)
    except Exception as exc:
        print(f"\nError: {exc}")
    input("\nPress Enter to exit...")


def main() -> None:
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        interactive_menu()
        return

    parser = argparse.ArgumentParser(description="Patch Robin Hood Game.exe for 60 FPS, resolution & camera")
    parser.add_argument("--revert", action="store_true", help="Restore Game.exe and Profiles from backups")
    parser.add_argument("--pan-speed", type=float, default=DEFAULT_PAN_SPEED, help=f"Base camera pan speed (default: {DEFAULT_PAN_SPEED})")
    parser.add_argument("--linear-pan", action="store_true", help="Disable pan acceleration/deceleration curve (instant linear pan)")
    parser.add_argument("--no-sysmem", action="store_true", help="Keep original VIDEOMEMORY surface flags instead of forcing SYSTEMMEMORY")
    parser.add_argument("--res", default=DEFAULT_RES, help=f"Set resolution in DATA/Savegame/Profiles (default: {DEFAULT_RES})")
    parser.add_argument("--no-res", action="store_true", help="Skip patching resolution in DATA/Savegame/Profiles")
    args = parser.parse_args()
    if args.revert:
        revert_patch()
    else:
        apply_patch(args.pan_speed, sysmem=not args.no_sysmem, linear_pan=args.linear_pan)
        if not args.no_res:
            w, h = parse_resolution(args.res)
            apply_resolution(w, h)


if __name__ == "__main__":
    main()
