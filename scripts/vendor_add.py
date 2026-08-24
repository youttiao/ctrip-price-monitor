#!/usr/bin/env python3
"""在 VPS 上跑：admin login → POST /admin/vendors/add。

Usage: vendor_add.py <vendor_id> [<label>]
env: ADMIN_PWD (required)
"""
import os, sys, urllib.parse, http.cookiejar, urllib.request

VID = sys.argv[1]
LABEL = sys.argv[2] if len(sys.argv) > 2 else ""
ADMIN_PWD = os.environ["ADMIN_PWD"]

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [("User-Agent", "vendor_add/1.0")]

data = urllib.parse.urlencode({"username": "admin", "password": ADMIN_PWD}).encode()
r = opener.open("http://127.0.0.1:8000/login", data=data)
print("login:", r.status)
sid = next((c.value for c in jar if c.name == "ctrip_sid"), None)
if not sid:
    sys.exit("no sid after login")

data = urllib.parse.urlencode({"vendor_id": VID, "label": LABEL}).encode()
r = opener.open("http://127.0.0.1:8000/admin/vendors/add", data=data)
print("add:", r.status, r.url)