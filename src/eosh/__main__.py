"""Entry point for eosh."""

import argparse

from . import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="eosh",
        description="A lightweight but powerful terminal shell.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.parse_args()

    from .shell import Shell
    shell = Shell()
    shell.run()


if __name__ == "__main__":
    main()
