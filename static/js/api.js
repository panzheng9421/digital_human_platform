// API 客户端：自动带 token，统一错误处理。
const TOKEN_KEY = "dh_token";

const API = {
  get token() { return localStorage.getItem(TOKEN_KEY); },
  set token(v) { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); },

  async req(path, opts = {}) {
    const headers = opts.headers || {};
    if (this.token) headers["Authorization"] = "Bearer " + this.token;
    let body = opts.body;
    // FormData 不手动设 Content-Type（浏览器自动带 boundary）
    if (body && !(body instanceof FormData) && typeof body === "object") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const res = await fetch("/api" + path, { method: opts.method || "GET", headers, body });
    let data = null;
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      const msg = (data && data.detail) || ("请求失败 " + res.status);
      throw new Error(msg);
    }
    return data;
  },

  // 表单提交（含文件）
  postForm(path, obj) {
    const fd = new FormData();
    for (const k in obj) {
      const v = obj[k];
      if (v === undefined || v === null) continue;
      fd.append(k, v);
    }
    return this.req(path, { method: "POST", body: fd });
  },

  get(path) { return this.req(path); },

  delete(path) { return this.req(path, { method: "DELETE" }); },

  // 轮询任务直到完成
  async pollTask(tid, onProgress) {
    while (true) {
      const t = await this.get("/task/" + tid);
      if (onProgress) onProgress(t.progress || 0, t.status);
      if (t.status === "done") return t.result;
      if (t.status === "error") throw new Error(t.error || "任务失败");
      await new Promise(r => setTimeout(r, 600));
    }
  }
};
