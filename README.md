# `bwrap-umu`
`bwrap` wrapper for games launched with [UMU](https://github.com/Open-Wine-Components/umu-launcher)

```sh
bwrap-umu [additional bwrap options...] <command to execute...>
```

Environment variable | Description | Set by _Heroic_ | Set by _Faugus_
:-:|:--|:-:|:-:
`STEAM_COMPAT_INSTALL_PATH` | Path to the installation directory of the game | ✅ | ⚠️
`WINEPREFIX` | Path to the Wine prefix used by the game | ✅ | ✅
`PROTONPATH` | Path to the installation directory of Proton | ✅ | ✅

> [!WARNING]
> If you use _Faugus Launcher_
> and the installation directory of your game is outside the `WINEPREFIX`,
> then you need to manually set `STEAM_COMPAT_INSTALL_PATH`.

## Security notes

Variable | Description
:-:|:--
`BWRAP_NETWORK` | `internet` (default): internet only, the host's own services and the LAN are blocked. `lan`: internet and LAN. `none`: no network. `host`: share the host network (insecure).

Requirements: [`pasta`](https://passt.top) (package `passt`) and `ip` (iproute2) for the `internet` and `lan` modes.

> [!CAUTION]
> **X11 is not isolated.** Any X11 client can log keys, read the screen and
> type into other windows (e.g. a terminal). Run games nested in
> [gamescope](https://github.com/ValveSoftware/gamescope)
> (`gamescope -- bwrap-umu …`, or the launcher's gamescope option) so the game
> only sees gamescope's own X server. `bwrap-umu` prints a warning when it
> exposes the host X server.

Writable host paths that remain shared between all sandboxed games:
the UMU runtime (`~/.local/share/umu`, `~/.cache/umu`), the protonfixes and
winetricks caches. Don't run `umu-run` outside the sandbox, or a compromised
game could plant code there that runs unsandboxed.
Shader caches are kept separate from the host's, in `~/.cache/bwrap-sandbox`.
