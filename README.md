# Robin Hood: The Legend of Sherwood — Modern FPS & Camera Fix

Performance, frame-pacing, and camera-panning patch for **Robin Hood: The Legend of Sherwood (GOG 1.1.0.0)**.

## What It Fixes
- **Widescreen Resolution Fix (`1920x1080` default)**: Patches `DATA\Savegame\Profiles` (`0x104`/`0x108` float32) to run at `1920x1080` or any custom `--res WxH` resolution (automatically backs up `Profiles.bak`), and seamlessly extends the bottom-bar HUD foliage in `DATA\Interface\DEFAULT.RES` to span the full widescreen width without gaps.
- **60 FPS Rendering, Cursor & Camera / Native 25 Hz Simulation**: Unlocks the render/input/camera loop to 60 FPS (`timeBeginPeriod(1)` + `Sleep(1)` wait cave using `timeGetTime`) while keeping game logic and animations locked at 25 Hz (`40 ms`) so gameplay speed is unchanged.
- **Experimental 60 FPS Entity `(x, y)` Interpolation (`--interp`)**: Smooths character and world-object draw positions between 25 Hz simulation ticks.
- **DirectDraw `DDSCAPS_SYSTEMMEMORY` Fix**: Moves offscreen DirectDraw surfaces into system RAM (`0x0840`) to eliminate GPU↔CPU `Lock()` readback stalls during software alpha-blending and zoomed-out `Stretching16` blits (fixes 15–17 FPS zoomed-out drops).
- **Smooth Camera Panning**: Removes stepped `+1.0` pan jitter and sets a smooth exponential pan curve (`3.0` default base speed with 60 FPS coasting), configurable via `--pan-speed` or `--linear-pan`.
- **Large Address Aware (LAA)**: Sets `IMAGE_FILE_LARGE_ADDRESS_AWARE` (`0x0020`) in `Game.exe` for 4 GB virtual memory on 64-bit Windows.
- **Borderless / Alt-Tab Focus Fix**: Clears `WS_EX_TOPMOST` and gates `SetCursorPos` to foreground window focus so Alt-Tab works cleanly.
- **In-Game Options Resolution Buttons**: Repurposes the Options screen resolution buttons to `1280x720`, `1920x1080`, and `2560x1440` (or custom `WxH`).
- **DDrawCompat 60 Hz Preset (`DDrawCompat.ini`)**: Pairs with DDrawCompat v0.7.2 using `FullscreenMode = borderless` for tear-free 60 FPS VSync without monitor "No Signal" / mode-switch issues on 120 Hz+ and modern displays.
- **Widescreen Zoom & Black-Texture Hotfix**: Clips out-of-bounds DirectDraw `Blt` source rectangles (`DDERR_INVALIDRECT`) so ground textures render cleanly when zoomed out on smaller maps (`Sherwood`, `Croisement01/02/03`), and starts narrow maps (`< screen_w`) at `2.0x` zoom while keeping `0.5x` disabled at `1080p`.

## Supported Game Version
- **GOG 1.1.0.0** (`Game.exe` original SHA256: `b0963242470d96cdd8021e2cb81ccbc5e26b4fb2cb122febdec50ad30774f08e`).
- The patcher checks this hash before modifying anything and automatically creates `Game.exe.bak`.

## Installation & GUI
1. Download `RobinHood_ModernFix.zip` from the **Releases** page (includes `RobinHood_ModernFix.exe`, `DDrawCompat.ini`, and `ddraw.dll` from [narzoul/DDrawCompat](https://github.com/narzoul/DDrawCompat) v0.7.2).
2. In your game folder, back up the original `ddraw.dll` (e.g. rename to `ddraw.dll.gog.bak`).
3. Extract `RobinHood_ModernFix.zip` into the game folder next to `Game.exe` (overwrite `ddraw.dll` when prompted).
4. Run `RobinHood_ModernFix.exe` (or `python robinhood_modern_fix.py`) with no arguments to open the **GUI**, toggle any fixes you want, choose a resolution, and click **Apply Selected Patches**.

## CLI Options & Revert
Passing any CLI flag runs headlessly without opening the GUI (pass `--gui` to force the GUI, or `--cli` to run headlessly with defaults):
```powershell
RobinHood_ModernFix.exe --cli                  # Apply all recommended defaults headlessly
RobinHood_ModernFix.exe --res 2560x1440        # Set custom resolution (default: 1920x1080)
RobinHood_ModernFix.exe --no-res               # Skip resolution & HUD foliage patching
RobinHood_ModernFix.exe --pan-speed 2.5        # Custom base pan speed
RobinHood_ModernFix.exe --linear-pan           # Instant linear pan (no ramp/coasting)
RobinHood_ModernFix.exe --interp               # Experimental 60 FPS entity (x, y) interpolation
RobinHood_ModernFix.exe --no-fps               # Skip 60 FPS unlock & 1 ms Sleep(1) pacing
RobinHood_ModernFix.exe --no-sysmem            # Keep original VIDEOMEMORY surface flags
RobinHood_ModernFix.exe --no-pan               # Skip smooth camera panning patch
RobinHood_ModernFix.exe --no-laa               # Skip Large Address Aware (4 GB) PE flag
RobinHood_ModernFix.exe --no-zoom              # Skip zoom & viewport bounds fix
RobinHood_ModernFix.exe --no-window            # Skip borderless / Alt-Tab focus fix
RobinHood_ModernFix.exe --no-options-buttons   # Keep original Options resolution buttons
RobinHood_ModernFix.exe --revert               # Restore Game.exe, Profiles, and DEFAULT.RES
```

## Building the `.exe` Locally
```powershell
pip install pyinstaller
pyinstaller --onefile --add-data "assets/banner.png;assets" --name RobinHood_ModernFix robinhood_modern_fix.py
```
