import urllib.request, urllib.error, socket
socket.setdefaulttimeout(25)
base = "http://1559463156850707.cn-hangzhou.pai-eas.aliyuncs.com"
for path in ["/api/predict/gpuview", "/"]:
    u = base + path
    print("=== ", u)
    try:
        r = urllib.request.urlopen(u)
        print("  STATUS", r.status, "BODY", repr(r.read()[:300]))
    except urllib.error.HTTPError as e:
        print("  HTTPError", e.code, "BODY", repr(e.read()[:300]))
    except Exception as e:
        print("  ERR", type(e).__name__, str(e)[:200])
