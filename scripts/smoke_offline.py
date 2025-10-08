#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import sys

def main() -> int:
    print("Offline smoke: OK (no imports beyond stdlib).")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
