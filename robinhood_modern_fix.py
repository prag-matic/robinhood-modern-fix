"""
Robin Hood: The Legend of Sherwood (1.1.0.0 GOG) Modern Fix Patcher.

Provides both a graphical interface (default when launched with no arguments
or --gui) and a headless CLI for toggling individual engine, camera, windowing,
resolution, and HUD patches.
"""

import argparse
import hashlib
import shutil
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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

DEFAULT_RES_PAK = ROOT / "DATA" / "Interface" / "DEFAULT.RES"
DEFAULT_RES_PAK_BAK = ROOT / "DATA" / "Interface" / "DEFAULT.RES.bak"
HUD_FOLIAGE_ENTRIES = (
    (316186, 160, 110, 1, 9702),
    (325912, 384, 110, 1, 23848),
)

EXPECTED_ORIG_SHA256 = "b0963242470d96cdd8021e2cb81ccbc5e26b4fb2cb122febdec50ad30774f08e"

# VA 0x0050fee6 -> Raw 0x0010fee6: call 0x004c6ef0 (e8 05 70 fb ff) -> call 0x00676e50
SIM_CALL_OFF = 0x10FEE6
ORIG_SIM_CALL = bytes.fromhex("e8 05 70 fb ff")
PATCH_SIM_CALL = bytes.fromhex("e8 65 6f 16 00")

# VA 0x00510114..0x00510125 -> Raw 0x00110114..0x00110125 (18 bytes):
# Replace 100%-CPU busy-wait loop with 1 ms timeBeginPeriod(1)+Sleep(1) wait cave in .fix @ 0x006c7134
WAIT_LOOP_OFF = 0x110114
ORIG_WAIT_LOOP = bytes.fromhex(
    "ff d7 2b c6 83 f8 28 73 09 ff d7 2b c6 83 f8 28 72 f7"
)
PATCH_WAIT_LOOP = bytes.fromhex("e8 1b 70 1b 00 eb 0b") + b"\x90" * 11

# Switch frame-start timestamp (0x0050f994) and wait-loop timer (0x005100f2) from GetTickCount -> timeGetTime
FRAME_START_TICK_OFF = 0x10F994
ORIG_FRAME_START_TICK = bytes.fromhex("ff 15 8c 71 67 00")
PATCH_FRAME_START_TICK = bytes.fromhex("ff 15 c8 72 67 00")

WAIT_TIMER_LOAD_OFF = 0x1100F2
ORIG_WAIT_TIMER_LOAD = bytes.fromhex("8b 3d 8c 71 67 00")
PATCH_WAIT_TIMER_LOAD = bytes.fromhex("8b 3d c8 72 67 00")

# PE Characteristics (LAA: 0x010f -> 0x012f, computed from e_lfanew + 0x16)
IMAGE_FILE_LARGE_ADDRESS_AWARE = 0x0020

# Code cave in .text trailing padding: VA 0x00676e50 -> Raw 0x00276e50
# BSS scratch DWORD in .data: VA 0x006c3800
CAVE_OFF = 0x276E50
CAVE_BYTES = bytes.fromhex(
    "51"                    # push ecx
    "ff 15 c8 72 67 00"     # call dword ptr [0x006772c8] (timeGetTime)
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

# 6) Widescreen zoom & black-texture fix:
#    - Clip Blit src_rect to [0, 0, src_w, src_h] and scale dst_rect proportionally (prevents DDERR_INVALIDRECT
#      black/missing ground textures on smaller maps like Sherwood and Croisement01/02/03).
#    - Start maps narrower than screen_w (Croisement01/02/03 at 1080p) at 2.0x zoom.
BLT_HOOK_OFF = 0x1E67AF
ORIG_BLT_BYTES = bytes.fromhex(
    "8b4c2410"
    "6a00"
    "51"
    "8b8c2484000000"
    "8b4310"
    "51"
    "8b4c2428"
    "8b10"
    "8b4910"
    "51"
    "8b4c2424"
    "51"
    "50"
    "ff5214"
)
PATCH_BLT_BYTES = bytes.fromhex("e8f6060900eb1e") + b"\x90" * (len(ORIG_BLT_BYTES) - 7)

BLT_CAVE_OFF = 0x276EAA  # 306 bytes (VA 0x00676eaa)
BLT_CAVE_BYTES = bytes.fromhex(
    "5589e583ec2856578bb5840000008b7d1c85f60f84f8000000"
    "578d7df06a0459f3a55e8d7de085f6751031c0abab668b4314"
    "ab668b4316abeb056a0459f3a58d75f08d7de08b55240fb74a"
    "140fb75216833e007c14837e04007c0e394e087f0939560c0f"
    "8ea80000008b46082b060f8eb60000008945d88b47082b0789"
    "45dc8b0685c07d10f7d8f76ddcf77dd80107c706000000008b"
    "460839c87e0e29c8f76ddcf77dd8294708894e088b063b4608"
    "7d798b073b47087d728b45240fb748168b460c2b46047e6389"
    "45d88b470c2b47048945dc8b460485c07d12f7d8f76ddcf77d"
    "d8014704c74604000000008b460c39c87e0e29c8f76ddcf77d"
    "d829470c894e0c8b46043b460c7d218b47043b470c7d196a00"
    "ff7518568b4d248b491051578b4310508b10ff5214eb0231c0"
    "5f5e89ec5dc3"
)

INIT_ZOOM_HOOK_OFF = 0x0C2F36
ORIG_INIT_ZOOM_BYTES = bytes.fromhex("8b96304700008986c4480000")
PATCH_INIT_ZOOM_BYTES = bytes.fromhex("8986c4480000e89f401b0090")

INIT_ZOOM_CAVE_OFF = 0x276FE0  # 32 bytes (VA 0x00676fe0)
INIT_ZOOM_CAVE_BYTES = bytes.fromhex(
    "8b96304700003b96284700007311c6869848000002c7865c47000000000040c3"
)

# 7) Windowing, Focus & Cursor Trap fixes:
#    - Clear WS_EX_TOPMOST (0x08 -> 0x00) in CreateWindowExA @ 0x005e3e4e
#    - Gate SetCursorPos @ 0x0050f98e on GetForegroundWindow() == hwnd in .fix section @ 0x006c7000
TOPMOST_OFF = 0x1E3E4E
ORIG_TOPMOST = bytes.fromhex("6a 08")
PATCH_TOPMOST = bytes.fromhex("6a 00")

SETCURSOR_HOOK_OFF = 0x10F98E
ORIG_SETCURSOR = bytes.fromhex("ff 15 a8 72 67 00")
PATCH_SETCURSOR = bytes.fromhex("e8 91 76 1b 00 90")  # call 0x006c7024; nop

FIX_SEC_VA = 0x006C7000
FIX_SEC_SIZE = 0x4000
FIX_SEC_BYTES = bytes.fromhex(
    "00000000"                              # 0x006c7000: g_pGetForegroundWindow
    "7573657233322e646c6c00"                # 0x006c7004: "user32.dll\0"
    "476574466f726567726f756e6457696e646f7700"  # 0x006c700f: "GetForegroundWindow\0"
    "90"                                    # 0x006c7023: align 4
    "a100706c0085c075246804706c00ff15c0716700"  # 0x006c7024: Gate_SetCursorPos
    "85c07426680f706c0050ff15a470670085c07416"
    "a300706c00ffd08b4d1085c974083b8100010000"
    "7506ff25a8726700c2080090"
)

# 8) In-game Graphics Options (RHMenuGraphics) 3-button resolution repurposing:
#    - Button 1 (0x0052cfa9): 640x480  -> 1280x720
#    - Button 2 (0x0052cff2): 800x600  -> 1920x1080
#    - Button 3 (0x0052d038): 1024x768 -> 2560x1440 (or custom --res WxH if different)
#    - 0x0052d0a1..0x0052d0c4: update highlight width checks (1280, 1920, btn3_w) and je -> jae
#    - 0x0052cb77: hook LoadLocalizedString to return the new button label strings from .fix @ 0x006c7070
OPT_RES1_H_OFF = 0x12CFAA
ORIG_OPT_RES1_H = struct.pack("<f", 480.0)
OPT_RES1_W_OFF = 0x12CFAF
ORIG_OPT_RES1_W = struct.pack("<f", 640.0)

OPT_RES2_H_OFF = 0x12CFF3
ORIG_OPT_RES2_H = struct.pack("<f", 600.0)
OPT_RES2_W_OFF = 0x12CFF8
ORIG_OPT_RES2_W = struct.pack("<f", 800.0)

OPT_RES3_H_OFF = 0x12D039
ORIG_OPT_RES3_H = struct.pack("<f", 768.0)
OPT_RES3_W_OFF = 0x12D03E
ORIG_OPT_RES3_W = struct.pack("<f", 1024.0)

OPT_CMP1_W_OFF = 0x12D0A2
ORIG_OPT_CMP1_W = struct.pack("<I", 640)
OPT_CMP2_W_OFF = 0x12D0B9
ORIG_OPT_CMP2_W = struct.pack("<I", 800)
OPT_CMP3_W_OFF = 0x12D0C0
ORIG_OPT_CMP3_W = struct.pack("<I", 1024)

OPT_RES_HIGHLIGHT_OFF = 0x12D0C4
ORIG_OPT_RES_JE = bytes.fromhex("74 1e")
PATCH_OPT_RES_JAE = bytes.fromhex("73 1e")

OPT_LABEL_HOOK_OFF = 0x12CB77
ORIG_OPT_LABEL_CALL = bytes.fromhex("e8 54 58 0b 00")
PATCH_OPT_LABEL_CALL = bytes.fromhex("e8 18 a5 19 00")  # call 0x006c7094

RES_LABEL_CODE_BYTES = bytes.fromhex(
    "565753ff742414ff742414b8d0235e00ffd083c408"
    "89c68b44241483f82b740c83f82c740e83f82d7410"
    "eb4bbf70706c00eb0cbf79706c00eb05bf83706c00"
    "5731c94931c0f2aef7d14989cb6a015389f1b8e039"
    "4200ffd05a84c0741a8b7e0489d985c9740a8a0230"
    "e466ab424975f631c066ab895e0889f05b5f5ec390"
    "9090"
)


WAIT_1MS_CODE_BYTES = bytes.fromhex(
    "00000000"                              # 0x006c7114: g_TimePeriodInit
    "77696e6d6d2e646c6c00"                  # 0x006c7118: "winmm.dll\0"
    "74696d65426567696e506572696f6400"      # 0x006c7122: "timeBeginPeriod\0"
    "9090"                                  # 0x006c7132: align 4
    "833d14716c0000752dc70514716c0001000000"  # 0x006c7134: Wait_16ms_1ms
    "6818716c00ff15c071670085c074146822716c"
    "0050ff15a470670085c074046a01ffd0ffd729"
    "f083f810730f83f80e73f26a01ff15b0706700"
    "ebe8c390"
)


# 9) Experimental 60 FPS entity (x, y) interpolation (--interp):
#    - 0x0050fee6: hook sim tick -> .fix @ 0x006c7245 (snapshots prev/curr pos of [world+0x48dc] entities)
#    - 0x0051061b: hook world draw (0x004c8140) -> .fix @ 0x006c738c (lerps pos by (now-last_sim)/40ms, restores after)
WORLD_DRAW_CALL_OFF = 0x11061B
ORIG_WORLD_DRAW_CALL = bytes.fromhex("e8 20 7b fb ff")
PATCH_WORLD_DRAW_CALL = bytes.fromhex("e8 6c 6d 1b 00")   # call 0x006c738c
PATCH_SIM_CALL_INTERP = bytes.fromhex("e8 5a 73 1b 00")   # call 0x006c7245

INTERP_CODE_BYTES = bytes.fromhex(
    "00000000000000000000000000002042000000430000803f0000000000000000"
    "51528b0d04726c0085c9740fba00806c003902740b83c2184975f631c05a59c3"
    "89d05a59c351ff15c87267008b1500386c0089c129d183f9280f822501000083"
    "f95073038d4228a300386c005951b8f06e4c00ffd059505356573b0d08726c00"
    "7410890d08726c00c70504726c0000000000ff0500726c008bb1dc4800008bb9"
    "e048000085f60f84d300000039fe0f83cb0000008b1e85db0f84b90000008b43"
    "0483e0073c040f85ab0000008b535085d20f84a000000089d8e842ffffff85c0"
    "745c89c1d94224d9410cdee9d9e1d81d10726c00dfe09e7323d94228d94110de"
    "e9d9e1d81d10726c00dfe09e730e8b410c8941048b4110894108eb0c8b422489"
    "41048b42288941088b422489410c8b4228894110a100726c00894114eb398b0d"
    "04726c0081f900020000732b6bc91881c100806c00ff0504726c0089198b4224"
    "89410489410c8b4228894108894110a100726c0089411483c604e92dffffff5f"
    "5e5b58c3598b8550350000c35356575589cdff15c87267002b0500386c0083f8"
    "007d0231c083f828720ca114726c00a31c726c00eb1350db0424d8350c726c00"
    "d91d1c726c0083c4048bb5dc4800008bbde048000085f6745a39fe73568b1e85"
    "db744b8b430483e0073c0475418b535085d2743a89d8e825feffff85c0742f8b"
    "0d00726c003948147524d9400cd86004d80d1c726c00d84004d95a24d94010d8"
    "6008d80d1c726c00d84008d95a2883c604eba689e9b840814c00ffd08bb5dc48"
    "00008bbde048000085f6744239fe733e8b1e85db74338b430483e0073c047529"
    "8b535085d2742289d8e8b2fdffff85c074178b0d00726c00394814750c8b480c"
    "894a248b4810894a2883c604ebbe5d5f5e5bc390"
)


def _build_fix_section_blob(btn3_w: int, btn3_h: int, interp: bool = False) -> bytes:
    blob = bytearray(FIX_SEC_BYTES)
    blob.extend(b"\x00" * (0x70 - len(blob)))
    lbl1 = b"1280x720\x00"
    lbl2 = b"1920x1080\x00"
    lbl3 = f"{btn3_w}x{btn3_h}\x00".encode("ascii")
    if len(lbl3) > 16:
        raise ValueError(f"Resolution label too long: {lbl3!r}")
    blob.extend(lbl1)
    blob.extend(lbl2)
    blob.extend(lbl3.ljust(16, b"\x00"))
    blob.append(0x90)  # align 0x94
    assert len(blob) == 0x94
    blob.extend(RES_LABEL_CODE_BYTES)
    assert len(blob) == 0x114
    blob.extend(WAIT_1MS_CODE_BYTES)
    if interp:
        blob.extend(b"\x00" * (0x200 - len(blob)))
        blob.extend(INTERP_CODE_BYTES)
    return bytes(blob)


def _append_fix_section(data: bytearray, blob: bytes) -> None:
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    num_sec = struct.unpack_from("<H", data, pe_off + 6)[0]
    opt_hdr_size = struct.unpack_from("<H", data, pe_off + 20)[0]
    sec_table_off = pe_off + 24 + opt_hdr_size
    new_sec_hdr_off = sec_table_off + num_sec * 40
    size_of_headers = struct.unpack_from("<I", data, pe_off + 24 + 60)[0]
    if new_sec_hdr_off + 40 > size_of_headers:
        raise RuntimeError("No room in PE headers for .fix section")
    img_base = struct.unpack_from("<I", data, pe_off + 24 + 28)[0]
    rva = FIX_SEC_VA - img_base
    if struct.unpack_from("<I", data, pe_off + 24 + 56)[0] != rva:
        raise RuntimeError("Unexpected SizeOfImage before adding .fix section")
    raw_off = len(data)
    struct.pack_into("<H", data, pe_off + 6, num_sec + 1)
    struct.pack_into("<I", data, pe_off + 24 + 56, rva + FIX_SEC_SIZE)
    hdr = struct.pack(
        "<8sIIIIIIHHI",
        b".fix\x00\x00\x00\x00",
        FIX_SEC_SIZE,
        rva,
        FIX_SEC_SIZE,
        raw_off,
        0, 0, 0, 0,
        0xE0000060,
    )
    data[new_sec_hdr_off : new_sec_hdr_off + 40] = hdr
    data.extend(blob + b"\x00" * (FIX_SEC_SIZE - len(blob)))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class PatchConfig:
    """Configuration of all togglable patches and parameters."""

    fps_unlock: bool = True
    interp: bool = False
    sysmem: bool = True
    pan_fix: bool = True
    pan_speed: float = DEFAULT_PAN_SPEED
    linear_pan: bool = False
    laa: bool = True
    zoom_fix: bool = True
    window_fix: bool = True
    options_res_buttons: bool = True
    apply_res: bool = True
    res_width: int = 1920
    res_height: int = 1080


def _paths(root: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    return (
        root / "Game.exe",
        root / "Game.exe.bak",
        root / "DATA" / "Savegame" / "Profiles",
        root / "DATA" / "Savegame" / "Profiles.bak",
        root / "DATA" / "Interface" / "DEFAULT.RES",
        root / "DATA" / "Interface" / "DEFAULT.RES.bak",
    )


def inspect_status(root: Path) -> dict[str, object]:
    """Return status metadata for the target game directory."""
    game_exe, backup_exe, profiles_path, _, _, _ = _paths(root)
    game_exists = game_exe.exists()
    backup_exists = backup_exe.exists()
    exe_hash = sha256_file(game_exe) if game_exists else None
    is_original = exe_hash == EXPECTED_ORIG_SHA256
    current_res: tuple[int, int] | None = None
    if profiles_path.exists():
        pdata = profiles_path.read_bytes()
        if len(pdata) >= PROFILES_RES_OFF + 8:
            rw, rh = struct.unpack_from("<ff", pdata, PROFILES_RES_OFF)
            if 640 <= rw <= 7680 and 480 <= rh <= 4320:
                current_res = (int(rw), int(rh))
    return {
        "game_exists": game_exists,
        "backup_exists": backup_exists,
        "sha256": exe_hash,
        "is_original": is_original,
        "profiles_exists": profiles_path.exists(),
        "current_res": current_res,
    }


def apply_patch(
    cfg: PatchConfig | None = None,
    root: Path = ROOT,
    log: Callable[[str], None] = print,
) -> None:
    if cfg is None:
        cfg = PatchConfig()
    if cfg.interp and not cfg.fps_unlock:
        raise ValueError("60 FPS entity interpolation requires 60 FPS Unlock.")

    game_exe, backup_exe, _, _, _, _ = _paths(root)
    if not game_exe.exists():
        raise FileNotFoundError(f"Missing {game_exe}")

    if not backup_exe.exists():
        shutil.copy2(game_exe, backup_exe)
        log(f"Backed up {game_exe.name} -> {backup_exe.name}")

    data = bytearray(backup_exe.read_bytes())
    orig_hash = hashlib.sha256(data).hexdigest()
    if orig_hash != EXPECTED_ORIG_SHA256:
        raise RuntimeError(
            f"Unexpected backup SHA256 {orig_hash} (expected {EXPECTED_ORIG_SHA256})"
        )

    if cfg.fps_unlock:
        assert data[SIM_CALL_OFF : SIM_CALL_OFF + 5] == ORIG_SIM_CALL, "Sim call mismatch"
        assert data[WAIT_LOOP_OFF : WAIT_LOOP_OFF + len(ORIG_WAIT_LOOP)] == ORIG_WAIT_LOOP, "Wait loop mismatch"
        assert data[FRAME_START_TICK_OFF : FRAME_START_TICK_OFF + 6] == ORIG_FRAME_START_TICK, "Frame start tick mismatch"
        assert data[WAIT_TIMER_LOAD_OFF : WAIT_TIMER_LOAD_OFF + 6] == ORIG_WAIT_TIMER_LOAD, "Wait timer load mismatch"
        assert all(b == 0 for b in data[CAVE_OFF : CAVE_OFF + len(CAVE_BYTES)]), "Cave not empty"
        data[SIM_CALL_OFF : SIM_CALL_OFF + 5] = PATCH_SIM_CALL
        data[WAIT_LOOP_OFF : WAIT_LOOP_OFF + len(PATCH_WAIT_LOOP)] = PATCH_WAIT_LOOP
        data[FRAME_START_TICK_OFF : FRAME_START_TICK_OFF + 6] = PATCH_FRAME_START_TICK
        data[WAIT_TIMER_LOAD_OFF : WAIT_TIMER_LOAD_OFF + 6] = PATCH_WAIT_TIMER_LOAD
        data[CAVE_OFF : CAVE_OFF + len(CAVE_BYTES)] = CAVE_BYTES

    if cfg.pan_fix:
        assert data[PAN_INIT_VAL_OFF : PAN_INIT_VAL_OFF + 4] == ORIG_PAN_INIT_VAL, "Pan init val mismatch"
        assert data[PAN_JITTER_JZ_OFF : PAN_JITTER_JZ_OFF + 2] == ORIG_JZ_0E, "Pan jitter jz mismatch"
        assert data[PAN_RAMP_JZ_OFF : PAN_RAMP_JZ_OFF + 2] == ORIG_JZ_0E, "Pan ramp jz mismatch"
        assert data[COAST_X_OFF : COAST_X_OFF + len(ORIG_COAST_X)] == ORIG_COAST_X, "Coast X mismatch"
        assert data[COAST_Y_OFF : COAST_Y_OFF + len(ORIG_COAST_Y)] == ORIG_COAST_Y, "Coast Y mismatch"
        data[PAN_INIT_VAL_OFF : PAN_INIT_VAL_OFF + 4] = struct.pack("<f", cfg.pan_speed)
        data[PAN_JITTER_JZ_OFF : PAN_JITTER_JZ_OFF + 2] = PATCH_JMP_0E
        if cfg.linear_pan:
            data[PAN_RAMP_JZ_OFF : PAN_RAMP_JZ_OFF + 2] = PATCH_JMP_0E
            data[COAST_X_OFF : COAST_X_OFF + len(PATCH_COAST_X)] = PATCH_COAST_X
            data[COAST_Y_OFF : COAST_Y_OFF + len(PATCH_COAST_Y)] = PATCH_COAST_Y

    if cfg.sysmem:
        assert data[SURF_NONLOCAL_OFF : SURF_NONLOCAL_OFF + 4] == ORIG_SURF_NONLOCAL, "Surf nonlocal mismatch"
        assert data[SURF_VIDMEM_OFF : SURF_VIDMEM_OFF + 4] == ORIG_SURF_VIDMEM, "Surf vidmem mismatch"
        data[SURF_NONLOCAL_OFF : SURF_NONLOCAL_OFF + 4] = PATCH_SURF_SYSMEM
        data[SURF_VIDMEM_OFF : SURF_VIDMEM_OFF + 4] = PATCH_SURF_SYSMEM

    if cfg.zoom_fix:
        assert data[BLT_HOOK_OFF : BLT_HOOK_OFF + len(ORIG_BLT_BYTES)] == ORIG_BLT_BYTES, "Blt hook mismatch"
        assert data[INIT_ZOOM_HOOK_OFF : INIT_ZOOM_HOOK_OFF + len(ORIG_INIT_ZOOM_BYTES)] == ORIG_INIT_ZOOM_BYTES, "Init zoom hook mismatch"
        assert all(b == 0 for b in data[BLT_CAVE_OFF : BLT_CAVE_OFF + len(BLT_CAVE_BYTES)]), "Blt cave not empty"
        assert all(b == 0 for b in data[INIT_ZOOM_CAVE_OFF : INIT_ZOOM_CAVE_OFF + len(INIT_ZOOM_CAVE_BYTES)]), "Init zoom cave not empty"
        data[BLT_CAVE_OFF : BLT_CAVE_OFF + len(BLT_CAVE_BYTES)] = BLT_CAVE_BYTES
        data[BLT_HOOK_OFF : BLT_HOOK_OFF + len(PATCH_BLT_BYTES)] = PATCH_BLT_BYTES
        data[INIT_ZOOM_CAVE_OFF : INIT_ZOOM_CAVE_OFF + len(INIT_ZOOM_CAVE_BYTES)] = INIT_ZOOM_CAVE_BYTES
        data[INIT_ZOOM_HOOK_OFF : INIT_ZOOM_HOOK_OFF + len(PATCH_INIT_ZOOM_BYTES)] = PATCH_INIT_ZOOM_BYTES

    if cfg.window_fix:
        assert data[TOPMOST_OFF : TOPMOST_OFF + len(ORIG_TOPMOST)] == ORIG_TOPMOST, "Topmost flag mismatch"
        assert data[SETCURSOR_HOOK_OFF : SETCURSOR_HOOK_OFF + len(ORIG_SETCURSOR)] == ORIG_SETCURSOR, "SetCursorPos hook mismatch"
        data[TOPMOST_OFF : TOPMOST_OFF + len(PATCH_TOPMOST)] = PATCH_TOPMOST
        data[SETCURSOR_HOOK_OFF : SETCURSOR_HOOK_OFF + len(PATCH_SETCURSOR)] = PATCH_SETCURSOR

    btn3_w, btn3_h = (
        (cfg.res_width, cfg.res_height)
        if cfg.apply_res and (cfg.res_width, cfg.res_height) not in ((1280, 720), (1920, 1080))
        else (2560, 1440)
    )
    if cfg.options_res_buttons:
        assert data[OPT_RES1_H_OFF : OPT_RES1_H_OFF + 4] == ORIG_OPT_RES1_H, "Options res1 H mismatch"
        assert data[OPT_RES1_W_OFF : OPT_RES1_W_OFF + 4] == ORIG_OPT_RES1_W, "Options res1 W mismatch"
        assert data[OPT_RES2_H_OFF : OPT_RES2_H_OFF + 4] == ORIG_OPT_RES2_H, "Options res2 H mismatch"
        assert data[OPT_RES2_W_OFF : OPT_RES2_W_OFF + 4] == ORIG_OPT_RES2_W, "Options res2 W mismatch"
        assert data[OPT_RES3_H_OFF : OPT_RES3_H_OFF + 4] == ORIG_OPT_RES3_H, "Options res3 H mismatch"
        assert data[OPT_RES3_W_OFF : OPT_RES3_W_OFF + 4] == ORIG_OPT_RES3_W, "Options res3 W mismatch"
        assert data[OPT_CMP1_W_OFF : OPT_CMP1_W_OFF + 4] == ORIG_OPT_CMP1_W, "Options cmp1 W mismatch"
        assert data[OPT_CMP2_W_OFF : OPT_CMP2_W_OFF + 4] == ORIG_OPT_CMP2_W, "Options cmp2 W mismatch"
        assert data[OPT_CMP3_W_OFF : OPT_CMP3_W_OFF + 4] == ORIG_OPT_CMP3_W, "Options cmp3 W mismatch"
        assert data[OPT_RES_HIGHLIGHT_OFF : OPT_RES_HIGHLIGHT_OFF + 2] == ORIG_OPT_RES_JE, "Options res highlight mismatch"
        assert data[OPT_LABEL_HOOK_OFF : OPT_LABEL_HOOK_OFF + 5] == ORIG_OPT_LABEL_CALL, "Options label hook mismatch"
        data[OPT_RES1_H_OFF : OPT_RES1_H_OFF + 4] = struct.pack("<f", 720.0)
        data[OPT_RES1_W_OFF : OPT_RES1_W_OFF + 4] = struct.pack("<f", 1280.0)
        data[OPT_RES2_H_OFF : OPT_RES2_H_OFF + 4] = struct.pack("<f", 1080.0)
        data[OPT_RES2_W_OFF : OPT_RES2_W_OFF + 4] = struct.pack("<f", 1920.0)
        data[OPT_RES3_H_OFF : OPT_RES3_H_OFF + 4] = struct.pack("<f", float(btn3_h))
        data[OPT_RES3_W_OFF : OPT_RES3_W_OFF + 4] = struct.pack("<f", float(btn3_w))
        data[OPT_CMP1_W_OFF : OPT_CMP1_W_OFF + 4] = struct.pack("<I", 1280)
        data[OPT_CMP2_W_OFF : OPT_CMP2_W_OFF + 4] = struct.pack("<I", 1920)
        data[OPT_CMP3_W_OFF : OPT_CMP3_W_OFF + 4] = struct.pack("<I", btn3_w)
        data[OPT_RES_HIGHLIGHT_OFF : OPT_RES_HIGHLIGHT_OFF + 2] = PATCH_OPT_RES_JAE
        data[OPT_LABEL_HOOK_OFF : OPT_LABEL_HOOK_OFF + 5] = PATCH_OPT_LABEL_CALL

    if cfg.interp:
        assert data[WORLD_DRAW_CALL_OFF : WORLD_DRAW_CALL_OFF + 5] == ORIG_WORLD_DRAW_CALL, "World draw hook mismatch"
        data[SIM_CALL_OFF : SIM_CALL_OFF + 5] = PATCH_SIM_CALL_INTERP
        data[WORLD_DRAW_CALL_OFF : WORLD_DRAW_CALL_OFF + 5] = PATCH_WORLD_DRAW_CALL

    if cfg.fps_unlock or cfg.interp or cfg.window_fix or cfg.options_res_buttons:
        _append_fix_section(data, _build_fix_section_blob(btn3_w, btn3_h, interp=cfg.interp))

    if cfg.laa:
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        chars_off = pe_off + 0x16
        chars = struct.unpack_from("<H", data, chars_off)[0]
        struct.pack_into("<H", data, chars_off, chars | IMAGE_FILE_LARGE_ADDRESS_AWARE)

    game_exe.write_bytes(data)
    enabled = [
        name
        for name, on in (
            ("60fps", cfg.fps_unlock),
            ("interp", cfg.interp),
            ("sysmem", cfg.sysmem),
            (f"pan={cfg.pan_speed:g}" + ("(linear)" if cfg.linear_pan else ""), cfg.pan_fix),
            ("laa", cfg.laa),
            ("zoom", cfg.zoom_fix),
            ("window", cfg.window_fix),
            (f"options-btns(1280x720,1920x1080,{btn3_w}x{btn3_h})", cfg.options_res_buttons),
        )
        if on
    ]
    summary = ", ".join(enabled) if enabled else "none (clean original)"
    log(f"Patched {game_exe.name} successfully [{summary}].")
    log(f"New SHA256: {sha256_file(game_exe)}")


def parse_resolution(res_str: str) -> tuple[int, int]:
    parts = res_str.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"Invalid resolution '{res_str}', expected WxH (e.g. 1920x1080)")
    w, h = int(parts[0]), int(parts[1])
    if w < 640 or h < 480 or w > 7680 or h > 4320:
        raise ValueError(f"Resolution out of supported range: {w}x{h}")
    return w, h


def apply_resolution(
    width: int,
    height: int,
    root: Path = ROOT,
    log: Callable[[str], None] = print,
) -> None:
    _, _, profiles_path, profiles_bak, _, _ = _paths(root)
    if not profiles_path.exists():
        log(f"Note: {profiles_path} not found yet. Launch the game once to create a profile, then re-run to set {width}x{height}.")
        return
    if not profiles_bak.exists():
        shutil.copy2(profiles_path, profiles_bak)
        log(f"Backed up {profiles_path.name} -> {profiles_bak.name}")
    pdata = bytearray(profiles_path.read_bytes())
    if len(pdata) < PROFILES_RES_OFF + 8:
        raise RuntimeError(f"{profiles_path} is too small ({len(pdata)} bytes)")
    struct.pack_into("<ff", pdata, PROFILES_RES_OFF, float(width), float(height))
    profiles_path.write_bytes(pdata)
    log(f"Set resolution in {profiles_path.relative_to(root)} -> {width}x{height}")


def _mirror_tile_rgb565(raw: bytes, w: int, h: int, target_w: int) -> bytes:
    out = bytearray()
    row_bytes = w * 2
    for y in range(h):
        row = raw[y * row_bytes : (y + 1) * row_bytes]
        rev = b"".join(row[i : i + 2] for i in range(row_bytes - 2, -1, -2))
        built = bytearray()
        flip = False
        while len(built) < target_w * 2:
            src = rev if flip else row
            need = target_w * 2 - len(built)
            built += src[:need]
            flip = not flip
        out += built
    return bytes(out)


def build_extended_default_res(res_bak: bytes, width: int) -> bytes:
    target_w = max(384, width - 640)
    out = bytearray()
    last_end = 0
    for off, exp_w, exp_h, exp_flags, exp_cs in HUD_FOLIAGE_ENTRIES:
        w, h, flags, cs = struct.unpack_from("<HHII", res_bak, off)
        if (w, h, flags, cs) != (exp_w, exp_h, exp_flags, exp_cs):
            raise RuntimeError(f"Unexpected DEFAULT.RES entry at 0x{off:x}: {(w, h, flags, cs)}")
        out += res_bak[last_end:off]
        raw = zlib.decompress(res_bak[off + 12 : off + 12 + cs])
        new_raw = _mirror_tile_rgb565(raw, w, h, target_w)
        comp = zlib.compress(new_raw, level=9)
        out += struct.pack("<HHII", target_w, h, flags, len(comp)) + comp
        last_end = off + 12 + cs
    out += res_bak[last_end:]
    return bytes(out)


def apply_hud_foliage(
    width: int,
    root: Path = ROOT,
    log: Callable[[str], None] = print,
) -> None:
    _, _, _, _, default_res_pak, default_res_pak_bak = _paths(root)
    if width <= 1024 or not default_res_pak.exists():
        return
    if not default_res_pak_bak.exists():
        shutil.copy2(default_res_pak, default_res_pak_bak)
        log(f"Backed up {default_res_pak.name} -> {default_res_pak_bak.name}")
    res_bak = default_res_pak_bak.read_bytes()
    default_res_pak.write_bytes(build_extended_default_res(res_bak, width))
    log(f"Extended bottom-bar HUD foliage in {default_res_pak.relative_to(root)} -> {max(384, width - 640)}x110")


def apply_all(
    cfg: PatchConfig,
    root: Path = ROOT,
    log: Callable[[str], None] = print,
) -> None:
    """Apply Game.exe patches and, if enabled, Profiles resolution and HUD foliage."""
    apply_patch(cfg, root=root, log=log)
    if cfg.apply_res:
        apply_resolution(cfg.res_width, cfg.res_height, root=root, log=log)
        apply_hud_foliage(cfg.res_width, root=root, log=log)


def revert_patch(
    root: Path = ROOT,
    log: Callable[[str], None] = print,
) -> None:
    game_exe, backup_exe, profiles_path, profiles_bak, default_res_pak, default_res_pak_bak = _paths(root)
    if not backup_exe.exists():
        raise FileNotFoundError(f"Backup not found: {backup_exe}")
    shutil.copy2(backup_exe, game_exe)
    log(f"Restored {game_exe.name} from {backup_exe.name} (SHA256: {sha256_file(game_exe)})")
    if profiles_bak.exists():
        shutil.copy2(profiles_bak, profiles_path)
        log(f"Restored {profiles_path.name} from {profiles_bak.name}")
    if default_res_pak_bak.exists():
        shutil.copy2(default_res_pak_bak, default_res_pak)
        log(f"Restored {default_res_pak.name} from {default_res_pak_bak.name}")


PRESET_RESOLUTIONS = ("1280x720", "1920x1080", "2560x1440", "3440x1440", "3840x2160")


GITHUB_URL = "https://github.com/prag-matic/robinhood-modern-fix"


def _find_banner_path(game_root: Path) -> Path | None:
    """Locate an optional banner image (banner.png / banner.gif) for the GUI header."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    search_dirs = [
        *([] if meipass is None else [Path(meipass)]),
        Path(__file__).resolve().parent,
        game_root,
    ]
    for base in search_dirs:
        for rel in ("banner.png", "banner.gif", "assets/banner.png", "assets/banner.gif"):
            candidates.append(base / rel)
    for path in candidates:
        if path.is_file():
            return path
    return None


PALETTE = {
    "primary": "#7CA329",   # Primary Green
    "forest": "#2D4C13",    # Deep Forest Green
    "gold": "#D49A62",      # Warm Skin Tone / Gold
    "wood": "#5E3A25",      # Dark Brown / Wood
    "highlight": "#F2E880", # Highlight Yellow / Gold
}


class ToolTip:
    """Hover tooltip displaying technical details for a widget."""

    def __init__(self, widget: object, text: str) -> None:
        self.widget = widget
        self.text = text
        self._after_id: str | None = None
        self._tip: object | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self._after_id = self.widget.after(320, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        import tkinter as tk

        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(
            tip,
            text=self.text,
            justify="left",
            background=PALETTE["wood"],
            foreground=PALETTE["highlight"],
            highlightbackground=PALETTE["primary"],
            highlightthickness=1,
            relief="flat",
            padx=8,
            pady=6,
            wraplength=420,
            font=("Segoe UI", 9),
        )
        lbl.pack()
        self._tip = tip

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


APP_TITLE = "Robin Hood: The Legend of Sherwood Modern Fix v1.0.4"


class PatcherGUI:
    """Tabbed Tkinter/ttk GUI with custom title bar, plain-English options, tooltips, banner slot, and credits."""

    def __init__(self, root_tk: "tk.Tk", initial_dir: Path = ROOT) -> None:
        import tkinter as tk
        import webbrowser
        from tkinter import filedialog, messagebox, scrolledtext, ttk

        self._tk = tk
        self._ttk = ttk
        self._filedialog = filedialog
        self._messagebox = messagebox
        self._webbrowser = webbrowser
        self.root = root_tk
        self.root.title(APP_TITLE)
        self.root.geometry("580x550")
        self.root.minsize(560, 520)
        self.root.configure(background=PALETTE["forest"])
        self.root.overrideredirect(True)
        self._drag_x = 0
        self._drag_y = 0
        self._apply_theme()

        self.dir_var = tk.StringVar(value=str(initial_dir))
        self.status_var = tk.StringVar()

        self.fps_var = tk.BooleanVar(value=True)
        self.interp_var = tk.BooleanVar(value=False)
        self.sysmem_var = tk.BooleanVar(value=True)
        self.laa_var = tk.BooleanVar(value=True)
        self.pan_var = tk.BooleanVar(value=True)
        self.pan_speed_var = tk.StringVar(value=f"{DEFAULT_PAN_SPEED:g}")
        self.linear_pan_var = tk.BooleanVar(value=False)
        self.zoom_var = tk.BooleanVar(value=True)
        self.window_var = tk.BooleanVar(value=True)
        self.options_btns_var = tk.BooleanVar(value=True)
        self.apply_res_var = tk.BooleanVar(value=True)
        self.preset_var = tk.StringVar(value="1920x1080")
        self.res_w_var = tk.StringVar(value="1920")
        self.res_h_var = tk.StringVar(value="1080")

        self._syncing_res = False
        self._banner_img: object | None = None
        self._pan_widgets: list[object] = []
        self._res_widgets: list[object] = []
        self._tooltips: list[ToolTip] = []

        shell = tk.Frame(
            self.root,
            background=PALETTE["forest"],
            highlightbackground=PALETTE["primary"],
            highlightthickness=1,
        )
        shell.pack(fill="both", expand=True)

        self._build_titlebar(shell)

        outer = ttk.Frame(shell, padding=8)
        outer.pack(fill="both", expand=True)

        self._build_banner(outer, initial_dir)
        self._build_folder_bar(outer)
        self._build_tabs(outer)
        self._build_actions(outer)
        self._build_log(outer, scrolledtext)

        self._wire_dependencies()
        self._refresh_status()
        self._sync_control_states()
        self.root.bind("<Map>", self._on_map)
        self.root.after(10, self._enable_taskbar_icon)

    def _enable_taskbar_icon(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            gwl_exstyle = -20
            ws_ex_appwindow = 0x00040000
            ws_ex_toolwindow = 0x00000080
            style = ctypes.windll.user32.GetWindowLongW(hwnd, gwl_exstyle)
            style = (style & ~ws_ex_toolwindow) | ws_ex_appwindow
            ctypes.windll.user32.SetWindowLongW(hwnd, gwl_exstyle, style)
            self.root.withdraw()
            self.root.after(10, self.root.deiconify)
        except Exception:
            pass

    def _start_drag(self, event: object) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _on_drag(self, event: object) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _minimize_window(self) -> None:
        self.root.overrideredirect(False)
        self.root.iconify()

    def _on_map(self, _event: object = None) -> None:
        if self.root.state() == "normal" and not self.root.overrideredirect():
            self.root.overrideredirect(True)
            self.root.after(10, self._enable_taskbar_icon)

    def _build_titlebar(self, parent: object) -> None:
        tk = self._tk
        bar = tk.Frame(parent, background=PALETTE["wood"], height=28)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        title_lbl = tk.Label(
            bar,
            text=APP_TITLE,
            background=PALETTE["wood"],
            foreground=PALETTE["highlight"],
            font=("Segoe UI", 9, "bold"),
            padx=10,
        )
        title_lbl.pack(side="left", fill="y")

        for w in (bar, title_lbl):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)

        def make_btn(text: str, hover_bg: str, hover_fg: str, cmd: Callable[[], None]) -> object:
            btn = tk.Label(
                bar,
                text=text,
                width=4,
                background=PALETTE["wood"],
                foreground=PALETTE["gold"],
                font=("Segoe UI", 9, "bold"),
                cursor="hand2",
            )
            btn.pack(side="right", fill="y")
            btn.bind("<Enter>", lambda _e: btn.configure(background=hover_bg, foreground=hover_fg))
            btn.bind("<Leave>", lambda _e: btn.configure(background=PALETTE["wood"], foreground=PALETTE["gold"]))
            btn.bind("<Button-1>", lambda _e: cmd())
            return btn

        make_btn("✕", "#a33327", PALETTE["highlight"], self.root.destroy)
        make_btn("_", PALETTE["primary"], PALETTE["forest"], self._minimize_window)

    def _apply_theme(self) -> None:
        style = self._ttk.Style(self.root)
        style.theme_use("clam")

        forest = PALETTE["forest"]
        primary = PALETTE["primary"]
        gold = PALETTE["gold"]
        wood = PALETTE["wood"]
        highlight = PALETTE["highlight"]

        style.configure(".", background=forest, foreground=highlight, fieldbackground=wood)
        style.configure("TFrame", background=forest)
        style.configure("TLabel", background=forest, foreground=highlight)
        style.configure("Desc.TLabel", background=forest, foreground=gold)
        style.configure("TLabelframe", background=forest, bordercolor=wood, lightcolor=wood, darkcolor=wood)
        style.configure("TLabelframe.Label", background=forest, foreground=gold, font=("Segoe UI", 9, "bold"))

        style.configure("TNotebook", background=forest, bordercolor=wood, tabmargins=(2, 4, 2, 0))
        style.configure(
            "TNotebook.Tab",
            background=wood,
            foreground=gold,
            padding=(10, 4),
            bordercolor=wood,
            lightcolor=wood,
            darkcolor=wood,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", primary), ("active", primary)],
            foreground=[("selected", forest), ("active", highlight)],
        )

        style.configure(
            "TCheckbutton",
            background=forest,
            foreground=highlight,
            indicatorbackground=wood,
            indicatorforeground=highlight,
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "TCheckbutton",
            background=[("active", forest)],
            foreground=[("disabled", gold), ("active", highlight)],
            indicatorbackground=[("selected", primary), ("active", wood)],
            indicatorforeground=[("selected", forest)],
        )

        style.configure(
            "TButton",
            background=wood,
            foreground=highlight,
            bordercolor=gold,
            lightcolor=wood,
            darkcolor=wood,
            padding=(8, 4),
        )
        style.map(
            "TButton",
            background=[("active", primary), ("pressed", primary)],
            foreground=[("active", forest), ("pressed", forest)],
        )
        style.configure(
            "Primary.TButton",
            background=primary,
            foreground=forest,
            bordercolor=highlight,
            font=("Segoe UI", 9, "bold"),
            padding=(10, 4),
        )
        style.map(
            "Primary.TButton",
            background=[("active", highlight), ("pressed", gold)],
            foreground=[("active", forest), ("pressed", forest)],
        )

        style.configure(
            "TEntry",
            fieldbackground=wood,
            foreground=highlight,
            insertcolor=highlight,
            bordercolor=gold,
            lightcolor=wood,
            darkcolor=wood,
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", forest)],
            foreground=[("disabled", gold)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=wood,
            background=wood,
            foreground=highlight,
            arrowcolor=highlight,
            bordercolor=gold,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", wood), ("disabled", forest)],
            foreground=[("readonly", highlight), ("disabled", gold)],
        )

    def _build_banner(self, parent: object, initial_dir: Path) -> None:
        tk = self._tk
        banner_box = tk.Frame(
            parent,
            height=60,
            background=PALETTE["wood"],
            highlightbackground=PALETTE["primary"],
            highlightthickness=1,
        )
        banner_box.pack(fill="x", pady=(0, 6))
        banner_box.pack_propagate(False)

        banner_path = _find_banner_path(initial_dir)
        if banner_path is not None:
            try:
                img = tk.PhotoImage(file=str(banner_path))
                self._banner_img = img
                tk.Label(banner_box, image=img, background=PALETTE["wood"], borderwidth=0).pack(
                    fill="both", expand=True
                )
                return
            except tk.TclError:
                pass

        tk.Label(
            banner_box,
            text="Robin Hood: The Legend of Sherwood",
            font=("Georgia", 13, "bold"),
            foreground=PALETTE["highlight"],
            background=PALETTE["wood"],
        ).pack(pady=(8, 1))
        tk.Label(
            banner_box,
            text="Modern Fix Patcher  •  Drop banner.png next to the tool to customize",
            font=("Segoe UI", 8),
            foreground=PALETTE["gold"],
            background=PALETTE["wood"],
        ).pack()

    def _build_folder_bar(self, parent: object) -> None:
        ttk = self._ttk
        box = ttk.LabelFrame(parent, text="Game Folder", padding=6)
        box.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.dir_var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row, text="Browse...", command=self._browse_folder).pack(side="left")

        ttk.Label(box, textvariable=self.status_var).pack(anchor="w", pady=(4, 0))

    def _add_toggle(
        self,
        parent: object,
        var: object,
        title: str,
        desc: str,
        tooltip: str,
        command: Callable[[], None] | None = None,
    ) -> None:
        tk = self._tk
        ttk = self._ttk
        row = ttk.Frame(parent)
        row.pack(fill="x", anchor="w")
        cb = ttk.Checkbutton(row, text=title, variable=var, command=command)
        cb.pack(side="left")
        badge = tk.Label(
            row,
            text=" ? ",
            font=("Segoe UI", 8, "bold"),
            foreground=PALETTE["highlight"],
            background=PALETTE["wood"],
            relief="flat",
            cursor="question_arrow",
        )
        badge.pack(side="left", padx=(6, 0))
        self._tooltips.append(ToolTip(cb, tooltip))
        self._tooltips.append(ToolTip(badge, tooltip))
        ttk.Label(parent, text=desc, style="Desc.TLabel", wraplength=500).pack(
            anchor="w", padx=(22, 0), pady=(1, 5)
        )

    def _build_tabs(self, parent: object) -> None:
        ttk = self._ttk
        nb = ttk.Notebook(parent)
        nb.pack(fill="x", pady=(0, 6))

        tab_perf = ttk.Frame(nb, padding=8)
        tab_cam = ttk.Frame(nb, padding=8)
        tab_win = ttk.Frame(nb, padding=8)
        tab_res = ttk.Frame(nb, padding=8)
        tab_cred = ttk.Frame(nb, padding=8)

        nb.add(tab_perf, text="Performance")
        nb.add(tab_cam, text="Camera & Zoom")
        nb.add(tab_win, text="Window & Menus")
        nb.add(tab_res, text="Resolution & HUD")
        nb.add(tab_cred, text="Credits & About")

        # --- Tab 1: Performance ---
        self._add_toggle(
            tab_perf,
            self.fps_var,
            "Smooth 60 FPS Gameplay",
            "Makes animations, mouse cursor, and screen scrolling run at 60 FPS while keeping game speed normal.",
            "Technical: Unlocks render/input/camera loop to ~60 FPS using 1 ms timeGetTime + timeBeginPeriod(1)+Sleep(1) "
            "frame pacing while locking simulation tick (FUN_004c6ef0) to 25 Hz (40 ms). "
            "Hooks: 0x0050fee6, 0x0050f994, 0x005100f2, 0x00510114 -> .fix @ 0x006c7134.",
            command=self._on_fps_toggle,
        )
        self._add_toggle(
            tab_perf,
            self.interp_var,
            "Experimental: Smoother Character Movement",
            "Smooths character and object movement between frames for a true 60 FPS look (requires Smooth 60 FPS).",
            "Technical: Interpolates entity (x, y) draw coordinates between 25 Hz simulation ticks via "
            "Hook_SimTick (0x006c7245) & Hook_WorldDraw (0x006c738c) in .fix.",
            command=self._on_interp_toggle,
        )
        self._add_toggle(
            tab_perf,
            self.sysmem_var,
            "Fix Graphics Crashes & Zoom Stutter",
            "Prevents DirectX errors and severe slowdown when zooming out on modern graphics cards.",
            "Technical: Changes DirectDraw offscreen surface flags from VIDEOMEMORY to SYSTEMMEMORY (0x0840) "
            "at 0x005e2876 / 0x005e28a6 to eliminate GPU->CPU Lock() readback stalls and DDERR_GENERIC.",
        )
        self._add_toggle(
            tab_perf,
            self.laa_var,
            "Allow Extra Memory (4 GB RAM)",
            "Lets the game use up to 4 GB of RAM on 64-bit Windows to avoid out-of-memory crashes.",
            "Technical: Sets IMAGE_FILE_LARGE_ADDRESS_AWARE (0x0020) in PE FileHeader.Characteristics.",
        )

        # --- Tab 2: Camera & Zoom ---
        self._add_toggle(
            tab_cam,
            self.pan_var,
            "Smooth Camera Scrolling",
            "Fixes jerky screen scrolling and lets you adjust how fast the camera moves.",
            "Technical: Removes +1.0 stepped pan jitter (0x005121f8) and unlocks 60 FPS camera panning "
            "with configurable base pan speed (0x005121c9).",
            command=self._sync_control_states,
        )
        pan_sub = ttk.Frame(tab_cam)
        pan_sub.pack(anchor="w", padx=(22, 0), pady=(0, 10))
        lbl_spd = ttk.Label(pan_sub, text="Scroll Speed:")
        lbl_spd.pack(side="left")
        ent_spd = ttk.Entry(pan_sub, textvariable=self.pan_speed_var, width=7)
        ent_spd.pack(side="left", padx=(6, 16))
        chk_lin = ttk.Checkbutton(
            pan_sub,
            text="Instant stop (no glide or ramp-up)",
            variable=self.linear_pan_var,
        )
        chk_lin.pack(side="left")
        self._tooltips.append(
            ToolTip(
                chk_lin,
                "Technical: Disables pan acceleration ramp and coasting deceleration at 0x00512202, 0x00512326, 0x00512334.",
            )
        )
        self._pan_widgets.extend([lbl_spd, ent_spd, chk_lin])

        self._add_toggle(
            tab_cam,
            self.zoom_var,
            "Fix Widescreen Zoom & Black Ground",
            "Fixes black ground textures when zoomed out and improves the starting zoom on widescreen monitors.",
            "Technical: Clips out-of-bounds DirectDraw Blt source rects (DDERR_INVALIDRECT) via cave at "
            "0x005e1e9e / 0x00676f20 and sets initial zoom on narrow maps via 0x00510c55 / 0x00676f90.",
        )

        # --- Tab 3: Window & Menus ---
        self._add_toggle(
            tab_win,
            self.window_var,
            "Better Alt-Tab & Window Switching",
            "Lets you Alt-Tab freely without the game forcing itself on top or trapping your mouse cursor.",
            "Technical: Clears WS_EX_TOPMOST at 0x005e3e4e and gates SetCursorPos at 0x0050f98e "
            "to foreground window focus via .fix @ 0x006c7024.",
        )
        self._add_toggle(
            tab_win,
            self.options_btns_var,
            "Modern In-Game Resolution Buttons",
            "Updates the in-game Options menu buttons to 1280x720, 1920x1080, and 2560x1440 (or your custom size).",
            "Technical: Patches the 3 Options resolution handlers (0x0052cfa9..0x0052d0c4) and UTF-16 "
            "std::wstring button labels via hook 0x0052cb77 -> .fix @ 0x006c7094.",
        )

        # --- Tab 4: Resolution & HUD ---
        self._add_toggle(
            tab_res,
            self.apply_res_var,
            "Set Game Resolution & Widescreen Bottom Bar",
            "Applies your chosen screen size and extends the bottom HUD foliage so there are no empty gaps.",
            "Technical: Writes float32 width/height to DATA/Savegame/Profiles (offset 0x104) and extends "
            "mirrored RGB565 foliage strips in DATA/Interface/DEFAULT.RES for widths > 1024 px.",
            command=self._sync_control_states,
        )
        res_sub = ttk.Frame(tab_res)
        res_sub.pack(anchor="w", padx=(22, 0), pady=(0, 4))
        lbl_pre = ttk.Label(res_sub, text="Screen Size:")
        lbl_pre.pack(side="left")
        cb_pre = ttk.Combobox(
            res_sub,
            textvariable=self.preset_var,
            values=(*PRESET_RESOLUTIONS, "Custom"),
            state="readonly",
            width=12,
        )
        cb_pre.pack(side="left", padx=(6, 14))
        cb_pre.bind("<<ComboboxSelected>>", self._on_preset_selected)
        lbl_w = ttk.Label(res_sub, text="Width:")
        lbl_w.pack(side="left")
        ent_w = ttk.Entry(res_sub, textvariable=self.res_w_var, width=6)
        ent_w.pack(side="left", padx=(4, 10))
        lbl_h = ttk.Label(res_sub, text="Height:")
        lbl_h.pack(side="left")
        ent_h = ttk.Entry(res_sub, textvariable=self.res_h_var, width=6)
        ent_h.pack(side="left", padx=(4, 0))
        self._res_widgets.extend([lbl_pre, cb_pre, lbl_w, ent_w, lbl_h, ent_h])

        # --- Tab 5: Credits & About ---
        tk = self._tk
        ttk.Label(
            tab_cred,
            text=APP_TITLE,
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 6))
        ttk.Label(
            tab_cred,
            text=(
                "Created by prag-matic.\n"
                "Includes 60 FPS frame pacing, smooth camera panning, widescreen HUD & zoom fixes, "
                "Alt-Tab windowing improvements, and experimental 60 FPS entity interpolation.\n\n"
                "Recommended pairing: DDrawCompat v0.7.2 by narzoul (included in release zip) "
                "for borderless 60 Hz VSync on modern displays."
            ),
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        link_row = ttk.Frame(tab_cred)
        link_row.pack(anchor="w")
        ttk.Label(link_row, text="GitHub Repository: ").pack(side="left")
        gh_link = tk.Label(
            link_row,
            text=GITHUB_URL,
            foreground=PALETTE["highlight"],
            background=PALETTE["forest"],
            cursor="hand2",
            font=("Segoe UI", 9, "underline"),
        )
        gh_link.pack(side="left")
        gh_link.bind("<Button-1>", lambda _e: self._webbrowser.open(GITHUB_URL))

    def _build_actions(self, parent: object) -> None:
        ttk = self._ttk
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Recommended Settings", command=self._select_recommended).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Clear All", command=self._select_none).pack(side="left")
        ttk.Button(bar, text="Restore Original", command=self._on_revert).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Apply Patches", style="Primary.TButton", command=self._on_apply).pack(side="right")

    def _build_log(self, parent: object, scrolledtext: object) -> None:
        ttk = self._ttk
        box = ttk.LabelFrame(parent, text="Activity Log", padding=5)
        box.pack(fill="both", expand=True)
        self.log_box = scrolledtext.ScrolledText(
            box,
            height=4,
            state="disabled",
            wrap="word",
            background=PALETTE["wood"],
            foreground=PALETTE["highlight"],
            insertbackground=PALETTE["highlight"],
            relief="flat",
        )
        self.log_box.pack(fill="both", expand=True)

    def _wire_dependencies(self) -> None:
        self.res_w_var.trace_add("write", lambda *_: self._on_wh_edited())
        self.res_h_var.trace_add("write", lambda *_: self._on_wh_edited())

    def _on_fps_toggle(self) -> None:
        if not self.fps_var.get() and self.interp_var.get():
            self.interp_var.set(False)

    def _on_interp_toggle(self) -> None:
        if self.interp_var.get() and not self.fps_var.get():
            self.fps_var.set(True)

    def _on_preset_selected(self, _event: object = None) -> None:
        val = self.preset_var.get()
        if "x" in val:
            self._syncing_res = True
            try:
                w, h = parse_resolution(val)
                self.res_w_var.set(str(w))
                self.res_h_var.set(str(h))
            finally:
                self._syncing_res = False

    def _on_wh_edited(self) -> None:
        if self._syncing_res:
            return
        candidate = f"{self.res_w_var.get().strip()}x{self.res_h_var.get().strip()}"
        self.preset_var.set(candidate if candidate in PRESET_RESOLUTIONS else "Custom")

    def _sync_control_states(self) -> None:
        pan_state = "normal" if self.pan_var.get() else "disabled"
        for w in self._pan_widgets:
            w.configure(state=pan_state)
        res_on = self.apply_res_var.get()
        for w in self._res_widgets:
            if w.winfo_class() == "TCombobox":
                w.configure(state="readonly" if res_on else "disabled")
            else:
                w.configure(state="normal" if res_on else "disabled")

    def _browse_folder(self) -> None:
        chosen = self._filedialog.askdirectory(initialdir=self.dir_var.get() or str(ROOT))
        if chosen:
            self.dir_var.set(chosen)
            self._refresh_status()

    def _target_root(self) -> Path:
        return Path(self.dir_var.get().strip() or str(ROOT))

    def _refresh_status(self) -> None:
        info = inspect_status(self._target_root())
        if not info["game_exists"]:
            exe_txt = "Game.exe: Missing"
        elif info["is_original"]:
            exe_txt = "Game.exe: Original (1.1.0.0)"
        else:
            exe_txt = "Game.exe: Patched"
        bak_txt = "Backup: Present" if info["backup_exists"] else "Backup: Not created yet"
        res = info["current_res"]
        res_txt = f"Profile Res: {res[0]}x{res[1]}" if res else "Profile Res: N/A"
        self.status_var.set(f"{exe_txt}  |  {bak_txt}  |  {res_txt}")

    def _log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def _select_recommended(self) -> None:
        self.fps_var.set(True)
        self.interp_var.set(False)
        self.sysmem_var.set(True)
        self.laa_var.set(True)
        self.pan_var.set(True)
        self.pan_speed_var.set(f"{DEFAULT_PAN_SPEED:g}")
        self.linear_pan_var.set(False)
        self.zoom_var.set(True)
        self.window_var.set(True)
        self.options_btns_var.set(True)
        self.apply_res_var.set(True)
        self._sync_control_states()

    def _select_none(self) -> None:
        for var in (
            self.fps_var,
            self.interp_var,
            self.sysmem_var,
            self.laa_var,
            self.pan_var,
            self.linear_pan_var,
            self.zoom_var,
            self.window_var,
            self.options_btns_var,
            self.apply_res_var,
        ):
            var.set(False)
        self._sync_control_states()

    def _collect_config(self) -> PatchConfig:
        pan_speed = float(self.pan_speed_var.get().strip())
        if pan_speed <= 0:
            raise ValueError("Scroll speed must be greater than 0.")
        w, h = 1920, 1080
        if self.apply_res_var.get():
            w, h = parse_resolution(f"{self.res_w_var.get().strip()}x{self.res_h_var.get().strip()}")
        return PatchConfig(
            fps_unlock=self.fps_var.get(),
            interp=self.interp_var.get(),
            sysmem=self.sysmem_var.get(),
            pan_fix=self.pan_var.get(),
            pan_speed=pan_speed,
            linear_pan=self.linear_pan_var.get(),
            laa=self.laa_var.get(),
            zoom_fix=self.zoom_var.get(),
            window_fix=self.window_var.get(),
            options_res_buttons=self.options_btns_var.get(),
            apply_res=self.apply_res_var.get(),
            res_width=w,
            res_height=h,
        )

    def _on_apply(self) -> None:
        try:
            cfg = self._collect_config()
            apply_all(cfg, root=self._target_root(), log=self._log)
            self._refresh_status()
            self._messagebox.showinfo("Success", "Selected patches applied successfully.")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            self._messagebox.showerror("Patch Error", str(exc))

    def _on_revert(self) -> None:
        try:
            revert_patch(root=self._target_root(), log=self._log)
            self._refresh_status()
            self._messagebox.showinfo("Restored", "Restored original backups successfully.")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            self._messagebox.showerror("Restore Error", str(exc))


def launch_gui(initial_dir: Path = ROOT) -> None:
    import tkinter as tk

    root_tk = tk.Tk()
    PatcherGUI(root_tk, initial_dir=initial_dir)
    root_tk.mainloop()


def main() -> None:
    if len(sys.argv) == 1 or "--gui" in sys.argv[1:]:
        launch_gui()
        return

    parser = argparse.ArgumentParser(description="Patch Robin Hood Game.exe for 60 FPS, resolution & camera")
    parser.add_argument("--gui", action="store_true", help="Launch graphical patcher UI")
    parser.add_argument("--cli", action="store_true", help="Run headlessly in CLI mode")
    parser.add_argument("--revert", action="store_true", help="Restore Game.exe, Profiles, and DEFAULT.RES from backups")
    parser.add_argument("--pan-speed", type=float, default=DEFAULT_PAN_SPEED, help=f"Base camera pan speed (default: {DEFAULT_PAN_SPEED})")
    parser.add_argument("--linear-pan", action="store_true", help="Disable pan acceleration/deceleration curve (instant linear pan)")
    parser.add_argument("--no-fps", action="store_true", help="Skip 60 FPS unlock & 1 ms Sleep(1) frame pacing")
    parser.add_argument("--no-sysmem", action="store_true", help="Keep original VIDEOMEMORY surface flags instead of forcing SYSTEMMEMORY")
    parser.add_argument("--no-pan", action="store_true", help="Skip smooth camera panning & pan speed patch")
    parser.add_argument("--no-laa", action="store_true", help="Skip Large Address Aware (4 GB) PE flag")
    parser.add_argument("--no-zoom", action="store_true", help="Skip zoom & viewport bounds fix")
    parser.add_argument("--no-window", action="store_true", help="Skip borderless / Alt-Tab focus fix")
    parser.add_argument("--no-options-buttons", action="store_true", help="Keep original in-game Options resolution buttons")
    parser.add_argument("--res", default=DEFAULT_RES, help=f"Set resolution in DATA/Savegame/Profiles (default: {DEFAULT_RES})")
    parser.add_argument("--no-res", action="store_true", help="Skip patching resolution in DATA/Savegame/Profiles & HUD foliage")
    parser.add_argument("--interp", action="store_true", help="Experimental: 60 FPS entity (x, y) draw interpolation")
    args = parser.parse_args()
    if args.revert:
        revert_patch()
        return

    w, h = (1920, 1080) if args.no_res else parse_resolution(args.res)
    cfg = PatchConfig(
        fps_unlock=not args.no_fps,
        interp=args.interp,
        sysmem=not args.no_sysmem,
        pan_fix=not args.no_pan,
        pan_speed=args.pan_speed,
        linear_pan=args.linear_pan,
        laa=not args.no_laa,
        zoom_fix=not args.no_zoom,
        window_fix=not args.no_window,
        options_res_buttons=not args.no_options_buttons,
        apply_res=not args.no_res,
        res_width=w,
        res_height=h,
    )
    apply_all(cfg)


if __name__ == "__main__":
    main()
