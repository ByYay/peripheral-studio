"""Absolute imports work both in a checkout and a PyInstaller archive."""

from multiprocessing import freeze_support

from desktop.launcher import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
