"""Rebuild the Linux CLI engine and Python charset module."""
import argparse
import os

import build
import build_settings


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    os.environ["PBB_BUILD_MODE"] = "native"
    os.environ.pop("PBB_SRC_DIR", None)
    os.environ.pop("PBB_CXXFLAGS", None)
    build.ensure_all(rebuild=True, verbose=True)
    print("Rebuild complete: CLI engine and Python core. UI is unavailable in PBB_Linux.")


if __name__ == "__main__":
    main()
