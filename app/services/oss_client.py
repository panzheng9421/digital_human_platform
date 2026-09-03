"""OSS 客户端（本地平台侧）。

解决「本地平台 + 云端 EAS(duix.avatar)」的文件互通死结：
  - 输入：把用户上传的音频/形象上传到 OSS，生成 EAS 容器可拉取的 URL（签名/公网）传给 EAS；
  - 输出：EAS 跑完把结果视频回传到 OSS，本客户端用 download_url() 把结果拉回本地 storage。

依赖 oss2（已装入 venv）。所有配置从 app.config 读取。
"""
import os
import time
import uuid

import oss2

from app.config import (
    OSS_BUCKET, OSS_ENDPOINT, OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET,
    OSS_REGION, OSS_PUBLIC_READ, OSS_ENDPOINT_INTERNAL,
)


def _normalize_endpoint(ep: str) -> str:
    """去掉协议头，保留 host（用于拼公网 URL）。"""
    if not ep:
        return ""
    ep = ep.strip()
    for s in ("https://", "http://"):
        if ep.startswith(s):
            ep = ep[len(s):]
    return ep.rstrip("/")


_auth = None
_bucket = None


def _get_auth():
    global _auth
    if _auth is None:
        _auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
    return _auth


def _get_bucket():
    global _bucket
    if _bucket is None:
        endpoint = OSS_ENDPOINT
        if not endpoint.startswith("http"):
            endpoint = "https://" + endpoint
        _bucket = oss2.Bucket(_get_auth(), endpoint, OSS_BUCKET)
    return _bucket


def available() -> bool:
    """是否已配置可用的 OSS。"""
    return bool(OSS_BUCKET and OSS_ENDPOINT and OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET)


def _internal_host() -> str:
    """OSS 内网 host（用于生成 EAS 容器可拉的 URL）。"""
    ep = OSS_ENDPOINT_INTERNAL or OSS_ENDPOINT
    if ep.startswith("http"):
        ep = ep.split("://", 1)[1]
    return ep.rstrip("/")


def upload_file(local_path: str, object_key: str = None, expires: int = 7200,
                for_eas: bool = True) -> str:
    """上传本地文件到 OSS，返回可访问 URL。

    - OSS_PUBLIC_READ=True：返回公网直链（EAS/浏览器均可直接访问）；
    - 否则返回签名 URL（带过期时间，默认 2 小时，足够 EAS 拉取）。
    - for_eas=True（默认）：把返回的 host 改写为内网 host，因为 EAS 容器没有公网出口，
      只能经同区域内网 endpoint 拉取；OSS 签名与 host 无关，替换 host 安全。
    """
    if not available():
        raise RuntimeError("OSS 未配置：请在 start.bat / 环境变量设置 OSS_BUCKET/ENDPOINT/AK/SK")
    if object_key is None:
        ext = os.path.splitext(local_path)[1] or ""
        object_key = f"digihuman/in/{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{ext}"
    _get_bucket().put_object_from_file(object_key, local_path)
    url = _public_url(object_key) if OSS_PUBLIC_READ else _get_bucket().sign_url("GET", object_key, expires)
    if for_eas:
        # 本地平台用公网上传，但给 EAS 的 URL 必须换成内网 host（容器才能拉到）
        pub_host = _normalize_endpoint(OSS_ENDPOINT)
        int_host = _internal_host()
        url = url.replace(f"{OSS_BUCKET}.{pub_host}", f"{OSS_BUCKET}.{int_host}")
    return url


def _public_url(object_key: str) -> str:
    host = _normalize_endpoint(OSS_ENDPOINT)
    return f"https://{OSS_BUCKET}.{host}/{object_key}"


def sign_url(object_key: str, expires: int = 7200) -> str:
    return _get_bucket().sign_url("GET", object_key, expires)


def download_url(url: str, out_path: str, timeout: int = 600) -> str:
    """把 OSS/公网 URL 下载到本地（复用 media_utils.download_file 的语义）。"""
    import urllib.request
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "digihuman-platform/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return out_path


def object_key_from_url(url: str) -> str:
    """从签名 URL / 公网直链解析 object_key（用于事后清理中转临时对象）。

    签名 URL 形如 https://bucket.oss-cn-xxx.aliyuncs.com/key?OSSAccessKeyId=...，
    公网直链形如 https://bucket.oss-cn-xxx.aliyuncs.com/key。均取 path 去前导 /。
    """
    from urllib.parse import urlparse, unquote
    return unquote(urlparse(url).path).lstrip("/")


def delete_object(object_key: str) -> None:
    """删除 OSS 对象（清理中转临时文件）。失败时仅打印警告，不影响主流程。"""
    try:
        _get_bucket().delete_object(object_key)
        print(f"[oss] 已清理中转对象: {object_key}")
    except Exception as e:
        print(f"[oss] 清理中转对象失败(可忽略): {object_key} -> {e}")
