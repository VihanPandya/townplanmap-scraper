"""Run the fixture site manually:  python3 tests/serve_fixture.py 8777"""
import sys
import time

from fixture_server import start

if __name__ == "__main__":
    base, httpd = start(int(sys.argv[1]) if len(sys.argv) > 1 else 8777)
    print(f"serving fixture site at {base}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
