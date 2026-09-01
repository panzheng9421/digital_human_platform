"""轻量任务管理器：进度 0-100、状态、结果。单进程内存实现。
生产环境可替换为 Redis（结构一致：task_id -> {progress,status,result}）。
"""
import uuid
import time
import threading

_tasks = {}
_lock = threading.Lock()


def create_task(kind: str, user_id: int = None) -> str:
    tid = uuid.uuid4().hex
    with _lock:
        _tasks[tid] = {
            "id": tid, "kind": kind, "user_id": user_id,
            "progress": 0, "status": "pending", "result": None, "error": None,
        }
    return tid


def get_task(tid: str):
    with _lock:
        return _tasks.get(tid)


def update(tid: str, progress: int = None, status: str = None, result=None, error=None):
    with _lock:
        t = _tasks.get(tid)
        if not t:
            return
        if progress is not None:
            t["progress"] = max(0, min(100, progress))
        if status is not None:
            t["status"] = status
        if result is not None:
            t["result"] = result
        if error is not None:
            t["error"] = error


def set_result(tid: str, result, status="done"):
    update(tid, progress=100, status=status, result=result)


class ProgressTicker:
    """start_progress_ticker 的句柄：暴露 current() / stop(end_at=None)。"""

    __slots__ = ("_stop", "_get")

    def __init__(self, stop_fn, get_fn):
        self._stop = stop_fn
        self._get = get_fn

    def current(self) -> int:
        """当前已推到的进度值，供 on_progress 回调取 max 用（避免 EAS 假进度把进度拉低）。"""
        try:
            return int(self._get())
        except Exception:
            return 0

    def stop(self, end_at=None):
        """停止推进；end_at 可指定收尾落在多少（不传则保持当前进度）。"""
        self._stop(end_at)


def start_progress_ticker(tid: str, start: int, end: int, seconds: float,
                          step: float = 0.4, curve: float = 0.7):
    """后台按预计耗时平滑推进进度，用于「拿不到真实中间进度」的阻塞调用。

    典型场景：百炼 CosyVoice 的 SpeechSynthesizer.call() 会一直阻塞到整段音频返回，
    期间无法知道真实进度，前端进度条就会长时间卡在起步值。这里按「预计耗时」推着走，
    让进度条是活的一一预估用完仍未完成时，停在 end-2 处等真实完成，绝不越界到 end。

    曲线：ratio ** curve（默认 0.7），前期走得快、后期放缓，
    避免"最后几秒一动不动"的观感；curve=1 即线性。

    返回 ProgressTicker 句柄，调用其 stop(end_at=None) 停止推进
    （end_at 可指定收尾落在多少，不传则保持当前进度）；current() 读当前进度。
    """
    stop_event = threading.Event()
    start, end = max(0, int(start)), min(100, int(end))
    last_p = {"v": start}  # 当前已推到的进度，供 current() 读取

    def _loop():
        try:
            total = max(0.5, float(seconds))
            t0 = time.time()
            while not stop_event.wait(step):
                ratio = min(1.0, (time.time() - t0) / total)
                # 只升不降：与任务当前进度取 max，避免覆盖外部（如 EAS 20/80 锚点）
                # 推高的进度，也避免回调抖动导致进度条回退。
                cur = (get_task(tid) or {}).get("progress", start)
                if ratio >= 1.0:
                    # 预估用尽还没完成：停在 end-2，把最后一步留给真实完成
                    last_p["v"] = max(cur, max(start, end - 2))
                    update(tid, progress=last_p["v"])
                    break
                last_p["v"] = max(cur, int(start + (end - start) * (ratio ** curve)))
                update(tid, progress=last_p["v"])
        except Exception:
            pass

    threading.Thread(target=_loop, daemon=True).start()

    def stop(end_at=None):
        stop_event.set()
        if end_at is not None:
            last_p["v"] = int(end_at)
            update(tid, progress=end_at)

    return ProgressTicker(stop, lambda: last_p["v"])
