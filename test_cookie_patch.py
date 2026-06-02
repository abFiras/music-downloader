#!/usr/bin/env python
import os
import tempfile

# Read cookie file
with open('cookies.txt', 'r', encoding='utf-8') as f:
    cookies_content = f.read()

# Simulate setting env var to cookie contents
os.environ['YTDLP_COOKIES_FILE'] = cookies_content

# Simulate patch logic
COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE")
COOKIES_TEMP_FILE = None

print(f"✓ Env var length: {len(COOKIES_FILE)} chars")
print(f"✓ Starts with '# Netscape': {COOKIES_FILE.strip().startswith('# Netscape')}")

# This is what app.py does now:
if COOKIES_FILE and not os.path.exists(COOKIES_FILE):
    looks_like_content = ("\n" in COOKIES_FILE) or COOKIES_FILE.strip().startswith("# Netscape")
    if looks_like_content:
        print(f"✓ Detected as cookie content (not a file path)")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
        tmp.write(COOKIES_FILE)
        tmp.close()
        COOKIES_TEMP_FILE = tmp.name
        COOKIES_FILE = COOKIES_TEMP_FILE
        print(f"✓ Wrote to temp file: {COOKIES_FILE}")
        print(f"✓ Temp file exists: {os.path.exists(COOKIES_FILE)}")
        print(f"✓ Temp file size: {os.path.getsize(COOKIES_FILE)} bytes")
        
        # Clean up
        os.unlink(COOKIES_FILE)
        print("✓ Cleanup successful")
else:
    print("✗ Could not detect as content or file already exists")
