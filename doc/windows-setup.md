# Setting Up a Windows Development Environment

pitash runs natively on Windows (see "Platform support" in `CLAUDE.md`), but the
*build* tooling — Python, `make`, POSIX utilities — isn't preinstalled the way it
is on Linux/macOS. This doc covers getting a working dev environment from a clean
Windows machine.

## Python

Install via the official **Python Install Manager** (the successor to the old
per-version MSI installers and the `py` launcher's backing store):

```
winget install Python.PythonInstallManager
```

This installs Python under a per-user path such as:

```
%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe
```

and registers the `py` launcher, which is version-aware:

```
py -0p          # list all installed Python versions and their paths
py -3.14        # run a specific version
```

`python`/`python3` on `PATH` may resolve to a Windows "app execution alias" stub
in `%LOCALAPPDATA%\Microsoft\WindowsApps\` that transparently redirects to the
real install above — this is expected and not the broken Microsoft Store stub.

The project `Makefile` bootstraps its venv from `python3.14` if that exact name
is found on `PATH`, otherwise falls back to the `py` launcher — so neither name
needs to be on `PATH` for `make install` to work.

## make (GNU Make)

Windows has no built-in `make`. Recommended source: **MSYS2**, which provides a
signed, actively maintained `make` package (stronger supply-chain story than
single-maintainer ports like `ezwinports`):

```
winget install MSYS2.MSYS2
"C:\msys64\usr\bin\bash.exe" -lc "pacman -Sy --noconfirm make"
```

Then put MSYS2's `usr\bin` early on `PATH` (user-level) so its `make` — and
other POSIX tools it provides — resolve ahead of anything else:

```powershell
$parts = [System.Environment]::GetEnvironmentVariable("PATH","User") -split ';'
[System.Environment]::SetEnvironmentVariable(
    "PATH", ("C:\msys64\usr\bin;" + ($parts -join ';')), "User"
)
```

Open a **new** terminal afterward — existing sessions keep their old `PATH`.

### Gotcha: `make` picks its shell from `PATH`, which changes `2>nul` behavior

GNU Make on Windows looks for `sh.exe` on `PATH` to run recipe lines and
`$(shell ...)` calls; if found (e.g. once MSYS2 or Git for Windows is on
`PATH`), it uses that POSIX shell instead of `cmd.exe`.

This matters for any Makefile that redirects to the Windows null device with
`2>nul` — `cmd.exe` understands `nul` as the special null device, but `sh.exe`
does not; it creates a **literal file** named `nul` in the working directory
instead of discarding the output. Always write `2>/dev/null` in Makefiles in
this repo — it works under both shells (`sh.exe` natively, and `cmd.exe`
harmlessly fails to find a `dev` directory and discards the line to stderr
instead of creating a stray file).

If a stray `nul` file shows up in the repo root, this is almost always the
cause — check for a bare `2>nul` in `Makefile`.

## POSIX utilities (`rm`, `grep`, …)

Native Windows `cmd.exe` doesn't have `rm`, `grep`, etc. — those are Unix
commands. pitash falls through unrecognized commands to the system shell, so
once a POSIX toolset is on `PATH` (MSYS2's `usr\bin`, or Git for Windows'
`usr\bin` at `C:\Program Files\Git\usr\bin`), commands like `rm -rf` work
transparently inside pitash too. Windows-native equivalents (`del`,
`rmdir /s /q`) work without any extra setup if you'd rather not rely on that.

## Verifying the toolchain

From the repo root, in a **new** terminal (so the `PATH` changes above are
picked up):

```
make install   # bootstraps .venv, installs pitash + pytest
make test      # runs the test suite
make run       # launches pitash itself
```

## Known quirks

- **winget uninstall can fail with "Access is denied"** on some per-user
  package installs (observed with `ezwinports.make`). If uninstall isn't
  critical, reordering `PATH` to prefer a different install (as done above
  for MSYS2 vs. `ezwinports`) is a fine workaround — the orphaned package
  just sits unused on disk.
- **winget's first run per machine** can appear to hang — it's actually
  blocked on an interactive prompt to accept the `msstore` source's terms of
  use. Pass `--accept-source-agreements` to avoid this.
