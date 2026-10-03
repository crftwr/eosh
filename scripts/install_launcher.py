"""Install a `pitash` launcher on PATH so it can be run from anywhere.

Cross-platform:

* Windows  -> create %USERPROFILE%\\bin with two shims (a .cmd for PowerShell/cmd
              and an extensionless POSIX script for Git bash), then prepend that
              directory to the persistent user PATH.
* POSIX     -> symlink the venv's `pitash` entry-point into ~/.local/bin.

Paths are derived from this file's location (project_root/.venv), so the launcher
keeps working as long as the repo and its venv stay put.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _venv_exe() -> Path:
    if os.name == "nt":
        return PROJECT_ROOT / ".venv" / "Scripts" / "pitash.exe"
    return PROJECT_ROOT / ".venv" / "bin" / "pitash"


def _broadcast_environment_change() -> None:
    # Registry writes to HKCU\Environment don't propagate to running processes.
    # explorer.exe (which spawns new cmd.exe/PowerShell windows from the Start
    # Menu) caches its environment block at logon and only refreshes it on this
    # broadcast -- without it, new terminals stay stale until logoff/reboot.
    import ctypes

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_long()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
    )


def install_windows() -> None:
    import winreg

    exe = _venv_exe()
    if not exe.exists():
        sys.exit(f"venv launcher not found: {exe}\nRun `make install` first.")

    bin_dir = Path(os.environ["USERPROFILE"]) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    # cmd.exe + PowerShell find the .cmd; Git bash auto-appends .exe but not .cmd,
    # so it needs its own extensionless POSIX shim.
    cmd_shim = bin_dir / "pitash.cmd"
    cmd_shim.write_text(f'@echo off\r\n"{exe}" %*\r\n', encoding="ascii")

    posix_path = str(exe).replace("\\", "/")
    sh_shim = bin_dir / "pitash"
    sh_shim.write_text(f'#!/bin/sh\nexec "{posix_path}" "$@"\n', encoding="ascii", newline="\n")

    # Prepend bin_dir to the persistent (HKCU) user PATH if not already present.
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        entries = [p for p in current.split(";") if p]
        if str(bin_dir) not in entries:
            new_value = ";".join([str(bin_dir), *entries])
            winreg.SetValueEx(key, "Path", 0, kind or winreg.REG_EXPAND_SZ, new_value)
            print(f"Added {bin_dir} to user PATH.")
        else:
            print(f"{bin_dir} already on user PATH.")

    _broadcast_environment_change()
    print(f"Installed shims in {bin_dir}.")
    print("Open a NEW terminal (PowerShell / cmd / Git bash), then run: pitash")


def install_posix() -> None:
    exe = _venv_exe()
    if not exe.exists():
        sys.exit(f"venv launcher not found: {exe}\nRun `make install` first.")

    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "pitash"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(exe)
    print(f"Symlinked {link} -> {exe}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"NOTE: {bin_dir} is not on your PATH. Add it to your shell profile:")
        print(f'  export PATH="{bin_dir}:$PATH"')


def main() -> None:
    if os.name == "nt":
        install_windows()
    else:
        install_posix()


if __name__ == "__main__":
    main()
