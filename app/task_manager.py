"""轻量任务管理器：进度 0-100、状态、结果。单进程内存实现。
生产环境可替换为 Redis（结构一致：task_id -> {progress,status,result}）。
"""
import uuid
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
