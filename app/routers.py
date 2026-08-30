"""全部 API 路由。串联：文案(入口1/入口2) → 配音 → 数字人 → 剪辑 → 封面 → 发布。"""
import os
import time
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse

from app import db, auth
from app.config import STORAGE_DIR, AVATAR_PROVIDER, COSYVOICE_FORMAT
from app.task_manager import create_task, update, get_task, set_result
from app.data import viral_scripts as vs
from app.data import sensitive_words as sw
from app.services import script_service as ss
from app.services import media_utils as mu
from app.services import heygem_client as hg
from app.services import oss_client as oss
from app.services import cosyvoice_client as cv

api = APIRouter(prefix="/api")
get_user = auth.get_current_user


def _rel(path: str) -> str:
    """绝对路径 -> 相对 storage 的 url 路径。"""
    return os.path.relpath(path, STORAGE_DIR).replace("\\", "/")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _looks_like_audio(head: bytes, ext: str) -> bool:
    """按扩展名对应的文件头 magic 校验是否为真实音频；命中任一已知音频签名也放行。"""
    if ext == ".pcm":
        return True  # raw PCM 无文件头 magic，信任扩展名
    sigs = {
        ".wav": [b"RIFF", b"WAVE"],
        ".wma": [b"\x30\x26\xb2\x75"],
        ".mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
        ".m4a": [b"ftyp"],
        ".aac": [b"\xff\xf1", b"\xff\xf9", b"ID3"],
        ".flac": [b"fLaC"],
        ".ogg": [b"OggS"],
        ".opus": [b"OggS"],
        ".webm": [b"\x1a\x45\xdf\xa3"],
    }
    for sig in sigs.get(ext, []):
        if sig in head:
            return True
    for others in sigs.values():
        for sig in others:
            if sig in head:
                return True
    return False


def _audio_duration(path: str, ext: str) -> float:
    """尽量从音频文件读真实时长（秒）；读不到返回 0.0。"""
    try:
        if ext == "wav":
            import wave
            with wave.open(path, "rb") as wf:
                return round(wf.getnframes() / wf.getframerate(), 2)
    except Exception:
        pass
    return 0.0


def _run(task_id, fn):
    """后台执行并捕获异常。"""
    try:
        fn()
    except Exception as e:
        update(task_id, status="error", error=str(e))


# ============ 鉴权 ============
@api.post("/auth/register")
def register(username: str = Form(...), password: str = Form(...), code: str = Form(...)):
    return auth.register(username, password, code)


@api.post("/auth/login")
def login(username: str = Form(...), password: str = Form(...)):
    return auth.login(username, password)


# ============ 行业爆款（入口1）============
@api.post("/scripts/industry")
def industry_scripts(industry: str = Form(...), user=Depends(get_user)):
    name, items = vs.match_industry(industry)
    if not items:
        # 未命中库，返回全部行业的提示 + 兜底样本
        return {"matched": False, "industry": None,
                "items": [{"title": "未找到该行业专属文案，先看看通用爆款",
                           "content": "你可以先输入更具体的行业，如：餐饮、房产、教育、美妆、穿搭、健身、数码、本地生活。下方为通用示例。"}],
                "available": list(vs.VIRAL_SCRIPTS.keys())}
    return {"matched": True, "industry": name, "items": items}


@api.post("/scripts/save")
def save_script(source: str = Form(...), industry: str = Form(""),
                original_text: str = Form(...), type_: str = Form("解题型"),
                persona: str = Form("老板"), user=Depends(get_user)):
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO scripts(user_id,source,industry,original_text,type,persona,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (user["id"], source, industry, original_text, type_, persona, "created", _now()))
    sid = cur.lastrowid
    conn.commit(); conn.close()
    return {"script_id": sid}


@api.post("/scripts/rewrite")
def rewrite(script_id: int = Form(...), type_: str = Form("解题型"),
            persona: str = Form("老板"), user=Depends(get_user)):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM scripts WHERE id=? AND user_id=?", (script_id, user["id"])).fetchone()
    conn.close()
    if not row:
        raise HTTPException(400, "文案不存在")
    # 若已是改写结果，基于 original 重改
    base = row["original_text"]
    res = ss.rewrite(base, type_, persona)
    conn = db.get_conn()
    conn.execute("UPDATE scripts SET type=?,persona=?,generated_text=?,title=?,status='done' WHERE id=?",
                 (type_, persona, res["generated_text"], res["title"], script_id))
    conn.commit(); conn.close()
    return res


@api.post("/scripts/check")
def check_words(text: str = Form(...)):
    hits = sw.check(text)
    return {"hits": hits, "count": len(hits), "safe": len(hits) == 0}


@api.get("/scripts/{sid}")
def get_script(sid: int, user=Depends(get_user)):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM scripts WHERE id=? AND user_id=?", (sid, user["id"])).fetchone()
    conn.close()
    return dict(row) if row else {}


# ============ 链接提取（入口2）============
@api.post("/extract")
def extract(url: str = Form(...), user=Depends(get_user)):
    return ss.extract_from_link(url)


# ============ 配音 ============
@api.post("/timbres/upload")
def upload_timbre(name: str = Form("我的音色"), file: UploadFile = File(...), user=Depends(get_user)):
    ALLOWED_AUDIO = {".wav", ".mp3", ".opus", ".aac", ".flac", ".pcm"}
    ext = (os.path.splitext(file.filename)[1] or "").lower()
    if ext not in ALLOWED_AUDIO:
        raise HTTPException(400,
            f"仅支持音频格式：{', '.join(sorted(ALLOWED_AUDIO))}（当前上传：{ext or '无扩展名'}）")
    # 二次校验：读文件头 magic bytes，拦截改后缀的非音频文件
    head = file.file.read(12)
    file.file.seek(0)
    if not _looks_like_audio(head, ext):
        raise HTTPException(400, "文件内容不是有效音频，请重新选择")
    path = os.path.join(STORAGE_DIR, "timbre", f"t{user['id']}_{int(time.time())}{ext}")
    with open(path, "wb") as f:
        f.write(file.file.read())
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO timbres(user_id,name,file_path,created_at) VALUES(?,?,?,?)",
                (user["id"], name, _rel(path), _now()))
    tid = cur.lastrowid
    conn.commit(); conn.close()
    return {"timbre_id": tid, "name": name, "url": "/files/" + _rel(path)}


@api.get("/timbres")
def list_timbres(user=Depends(get_user)):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM timbres WHERE user_id=?", (user["id"],)).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "url": "/files/" + r["file_path"]} for r in rows]


@api.post("/dubbing/generate")
def dubbing_generate(script_id: int = Form(...), timbre_id: int = Form(0),
                     emotion: str = Form("自然"), speed: float = Form(1.0), user=Depends(get_user)):
    conn = db.get_conn()
    s = conn.execute("SELECT * FROM scripts WHERE id=? AND user_id=?", (script_id, user["id"])).fetchone()
    conn.close()
    if not s:
        raise HTTPException(400, "文案不存在")
    text = s["generated_text"] or s["original_text"]
    tid = create_task("dubbing", user["id"])

    def work():
        update(tid, progress=15, status="running")
        time.sleep(0.3)
        fmt = (COSYVOICE_FORMAT or "wav").lower()
        ext = "mp3" if fmt == "mp3" else "wav"
        out = os.path.join(STORAGE_DIR, "audios", f"a{user['id']}_{int(time.time()*1000)}.{ext}")
        duration = 3.0
        provider = "placeholder"
        note = ""
        if cv.available() and timbre_id:
            # —— 真实声音克隆分支：CosyVoice2（替代 fish+asr）——
            try:
                conn = db.get_conn()
                t = conn.execute("SELECT * FROM timbres WHERE id=? AND user_id=?",
                                 (timbre_id, user["id"])).fetchone()
                conn.close()
                ref_path = os.path.join(STORAGE_DIR, t["file_path"]) if t and t["file_path"] else None
                if not ref_path or not os.path.exists(ref_path):
                    raise RuntimeError("音色文件缺失")
                raid = (t["reference_audio_id"] or "") if t else ""
                if not raid:
                    # 首次：上传参考音频拿音色 id 并缓存到 timbre 行
                    raid = cv.upload_reference_audio(ref_path)
                    conn = db.get_conn()
                    conn.execute("UPDATE timbres SET reference_audio_id=? WHERE id=?",
                                 (raid, timbre_id))
                    conn.commit(); conn.close()
                audio_bytes = cv.synthesize(text, raid, speed=speed)
                with open(out, "wb") as f:
                    f.write(audio_bytes)
                duration = _audio_duration(out, ext)
                provider = "cosyvoice"
            except Exception as e:
                # 任意失败 -> 回退占位 wav，保证流程不中断
                msg = str(e)
                if "格式不支持" in msg or "suffix" in msg or "InvalidFormData" in msg:
                    note = "音色格式不被 CosyVoice 支持，请重新上传 wav/mp3/opus/aac/flac/pcm 格式的音色"
                else:
                    note = f"CosyVoice 失败已回退占位音频: {e}"
                out = out.rsplit(".", 1)[0] + ".wav"
                mu.gen_wav(text, emotion, speed, out)
        else:
            # —— 占位分支：本地合成可播放 WAV（未启用 CosyVoice 或无音色）——
            out = out.rsplit(".", 1)[0] + ".wav"
            mu.gen_wav(text, emotion, speed, out)
        update(tid, progress=70)
        conn = db.get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO audios(user_id,script_id,timbre_id,emotion,speed,file_path,duration,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (user["id"], script_id, timbre_id, emotion, speed, _rel(out), duration, "done", _now()))
        aid = cur.lastrowid
        conn.commit(); conn.close()
        r = {"audio_id": aid, "url": "/files/" + _rel(out), "duration": duration,
             "emotion": emotion, "speed": speed, "tts": provider}
        if note:
            r["note"] = note
        set_result(tid, r)
    threading.Thread(target=_run, args=(tid, work)).start()
    return {"task_id": tid}


# ============ 数字人 ============
@api.post("/avatars/upload")
def upload_avatar(name: str = Form("我的形象"), file: UploadFile = File(...), user=Depends(get_user)):
    ext = os.path.splitext(file.filename)[1] or ".jpg"
    path = os.path.join(STORAGE_DIR, "avatars", f"av{user['id']}_{int(time.time())}{ext}")
    with open(path, "wb") as f:
        f.write(file.file.read())
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO avatars(user_id,name,file_path,status,created_at) VALUES(?,?,?,?,?)",
                (user["id"], name, _rel(path), "done", _now()))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    return {"avatar_id": aid, "name": name, "url": "/files/" + _rel(path)}


@api.get("/avatars")
def list_avatars(user=Depends(get_user)):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM avatars WHERE user_id=?", (user["id"],)).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "url": "/files/" + r["file_path"]} for r in rows]


@api.post("/digital_human/generate")
def dh_generate(audio_id: int = Form(...), avatar_id: int = Form(...), user=Depends(get_user)):
    conn = db.get_conn()
    a = conn.execute("SELECT * FROM audios WHERE id=? AND user_id=?", (audio_id, user["id"])).fetchone()
    av = conn.execute("SELECT * FROM avatars WHERE id=? AND user_id=?", (avatar_id, user["id"])).fetchone()
    conn.close()
    if not a or not av:
        raise HTTPException(400, "音频或形象不存在")
    audio_path = os.path.join(STORAGE_DIR, a["file_path"])
    avatar_path = os.path.join(STORAGE_DIR, av["file_path"])
    tid = create_task("digital_human", user["id"])

    def work():
        update(tid, progress=20, status="running")
        time.sleep(0.3)
        out = os.path.join(STORAGE_DIR, "videos", f"v{user['id']}_{int(time.time()*1000)}.mp4")
        if hg.available():
            # —— 真实分支：调用 PAI-EAS 上的 HeyGem / duix.avatar 出片 ——
            if not oss.available():
                update(tid, status="error",
                       error="未配置 OSS：云端 EAS 无法拉取本地上传的音频/形象文件。"
                             "请在 start.bat / 环境变量设置 OSS_BUCKET / OSS_ENDPOINT / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET。")
                return
            # 输入先传 OSS，拿到 EAS 可拉取的 URL
            try:
                audio_url = oss.upload_file(audio_path)
                video_url = oss.upload_file(avatar_path)
            except Exception as e:
                update(tid, status="error", error=f"OSS 上传失败: {e}")
                return
            try:
                res = hg.generate_talking_video(audio_url, video_url)
            except Exception as e:
                update(tid, status="error", error=f"HeyGem 推理失败: {e}")
                return
            remote = res.get("video_url")
            if not remote:
                update(tid, status="error",
                       error="HeyGem 未返回视频地址。请确认 EAS 容器入口 serve_oss.py 已部署（结果回传 OSS）。")
                return
            try:
                # remote 为 OSS 签名 URL 或公网直链，拉回本地 storage
                oss.download_url(remote, out)
            except Exception as e:
                update(tid, status="error",
                       error=f"视频回传失败: {e}。请确认 EAS 返回的 result 是 OSS 可下载 URL。")
                return
            poster_path = avatar_path  # 用上传形象作封面
            update(tid, progress=80)
            conn = db.get_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO videos(user_id,audio_id,avatar_id,file_path,poster_path,status,created_at) VALUES(?,?,?,?,?,?,?)",
                        (user["id"], audio_id, avatar_id, _rel(out), _rel(poster_path), "done", _now()))
            vid = cur.lastrowid
            conn.commit(); conn.close()
            set_result(tid, {"video_id": vid,
                             "poster_url": "/files/" + _rel(poster_path),
                             "video_url": "/files/" + _rel(out),
                             "provider": "heygem"})
        else:
            # —— 占位分支：本地合成静态形象+配音（无需 GPU，开发/演示用）——
            res = mu.make_talking_video(avatar_path, audio_path, out)
            update(tid, progress=80)
            conn = db.get_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO videos(user_id,audio_id,avatar_id,file_path,poster_path,status,created_at) VALUES(?,?,?,?,?,?,?)",
                        (user["id"], audio_id, avatar_id, _rel(res.get("video_path")) if res.get("video_path") else "",
                         _rel(res.get("poster_path")), "done", _now()))
            vid = cur.lastrowid
            conn.commit(); conn.close()
            r = {"video_id": vid, "poster_url": "/files/" + _rel(res["poster_path"]), "provider": "mock"}
            if res.get("video_path"):
                r["video_url"] = "/files/" + _rel(res["video_path"])
            if res.get("note"):
                r["note"] = res["note"]
            set_result(tid, r)
    threading.Thread(target=_run, args=(tid, work)).start()
    return {"task_id": tid}


# ============ 剪辑 ============
@api.post("/editing/generate")
def editing_generate(video_id: int = Form(...),
                     color: bool = Form(False), bigtext: bool = Form(False),
                     mg: bool = Form(False), bgm: bool = Form(False), user=Depends(get_user)):
    conn = db.get_conn()
    v = conn.execute("SELECT * FROM videos WHERE id=? AND user_id=?", (video_id, user["id"])).fetchone()
    conn.close()
    if not v:
        raise HTTPException(400, "视频不存在")
    in_path = os.path.join(STORAGE_DIR, v["file_path"]) if v["file_path"] else None
    poster_path = os.path.join(STORAGE_DIR, v["poster_path"]) if v["poster_path"] else None
    # 找配音音频用于无视频降级
    audio_path = None
    if v["audio_id"]:
        conn = db.get_conn()
        a = conn.execute("SELECT file_path FROM audios WHERE id=?", (v["audio_id"],)).fetchone()
        conn.close()
        if a:
            audio_path = os.path.join(STORAGE_DIR, a["file_path"])
    options = {"color": color, "bigtext": bigtext, "mg": mg, "bgm": bgm}
    tid = create_task("editing", user["id"])

    def work():
        update(tid, progress=20, status="running")
        time.sleep(0.3)
        out = os.path.join(STORAGE_DIR, "edits", f"e{user['id']}_{int(time.time()*1000)}.mp4")
        res = mu.edit_video(in_path, poster_path, options, audio_path, out)
        update(tid, progress=85)
        conn = db.get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO edits(user_id,video_id,options,file_path,status,created_at) VALUES(?,?,?,?,?,?)",
                    (user["id"], video_id, str(options), _rel(res.get("video_path")) if res.get("video_path") else "", "done", _now()))
        eid = cur.lastrowid
        conn.commit(); conn.close()
        r = {"edit_id": eid, "poster_url": "/files/" + _rel(res["poster_path"]), "options": options}
        if res.get("video_path"):
            r["video_url"] = "/files/" + _rel(res["video_path"])
        if res.get("note"):
            r["note"] = res["note"]
        set_result(tid, r)
    threading.Thread(target=_run, args=(tid, work)).start()
    return {"task_id": tid}


# ============ 封面 ============
COVER_STYLES = ["大字标题型", "对比型", "悬念型", "表情包型"]


@api.get("/covers/styles")
def cover_styles():
    return {"styles": COVER_STYLES}


@api.post("/covers/generate")
def cover_generate(edit_id: int = Form(...), style: str = Form("大字标题型"),
                   title: str = Form(""), subtitle: str = Form(""), user=Depends(get_user)):
    conn = db.get_conn()
    e = conn.execute("SELECT * FROM edits WHERE id=? AND user_id=?", (edit_id, user["id"])).fetchone()
    conn.close()
    if not e:
        raise HTTPException(400, "剪辑结果不存在")
    if not title:
        title = "爆款短视频"
    out = os.path.join(STORAGE_DIR, "covers", f"c{user['id']}_{int(time.time()*1000)}.jpg")
    mu.make_cover(style, title, subtitle, out)
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO covers(user_id,edit_id,style,title,file_path,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (user["id"], edit_id, style, title, _rel(out), "done", _now()))
    cid = cur.lastrowid
    conn.commit(); conn.close()
    return {"cover_id": cid, "url": "/files/" + _rel(out), "style": style, "title": title}


# ============ 发布 ============
PLATFORMS = ["抖音", "视频号", "小红书", "快手", "B站"]


@api.get("/publish/platforms")
def platforms():
    return {"platforms": PLATFORMS}


@api.post("/publish")
def publish(cover_id: int = Form(...), platform: str = Form("抖音"), user=Depends(get_user)):
    conn = db.get_conn()
    c = conn.execute("SELECT * FROM covers WHERE id=? AND user_id=?", (cover_id, user["id"])).fetchone()
    conn.close()
    if not c:
        raise HTTPException(400, "封面不存在")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO publishes(user_id,cover_id,platform,status,created_at) VALUES(?,?,?,?,?)",
                (user["id"], cover_id, platform, "published", _now()))
    pid = cur.lastrowid
    conn.commit(); conn.close()
    return {"publish_id": pid, "platform": platform, "status": "published",
            "note": "示例发布成功（占位）。生产接入各平台开放平台发布 API 后即为真实发布。"}


# ============ 任务轮询 ============
@api.get("/task/{tid}")
def task_status(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t
