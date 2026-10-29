"""Check every markdown link in the README. Prints status per URL and a summary line.

Usage: python3 check_links.py [../README.md]
"""
import concurrent.futures
import re
import sys
import urllib.error
import urllib.request

path = sys.argv[1] if len(sys.argv) > 1 else "../README.md"
urls = sorted(set(re.findall(r"\]\((https?://[^)\s]+)\)", open(path).read())))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) link-check"}


def status(url):
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return url, r.status
        except urllib.error.HTTPError as e:
            if method == "GET":
                return url, e.code
        except Exception as e:
            if method == "GET":
                return url, type(e).__name__
    return url, "?"


with concurrent.futures.ThreadPoolExecutor(16) as pool:
    results = list(pool.map(status, urls))

ok = [u for u, s in results if s == 200]
for u, s in results:
    if s != 200:
        print(s, u)
print(f"{len(ok)}/{len(results)} links returned 200")
