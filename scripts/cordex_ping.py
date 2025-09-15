#!/usr/bin/env python3
# Quick smoke test for key packages and modules.
import sys

def main():
    print("Python:", sys.version.replace("\n"," "))
    try:
        import requests  # optional
        print("requests:", getattr(requests, '__version__', 'present'))
    except Exception as e:
        print("requests: missing or error:", e)

    for mod in ("core", "reports", "telegram", "utils"):
        try:
            __import__(mod)
            print(f"{mod}: OK")
        except Exception as e:
            print(f"{mod}: FAIL -> {e}")

if __name__ == "__main__":
    main()
