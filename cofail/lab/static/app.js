// 参与端页面逻辑：所有私有数据只 POST 到本室服务，不直接发往协调端
const $ = (id) => document.getElementById(id);
const STEPS = ["frozen", "triple", "input", "mult", "aggregate"];

function headers() {
  const t = $("token").value.trim();
  return {
    "Content-Type": "application/json",
    ...(t ? { Authorization: `Bearer ${t}` } : {}),
  };
}
async function api(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body || {}),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || "请求失败");
  return data;
}
function msg(el, text, ok) {
  el.className = "msg " + (ok ? "ok" : "err");
  el.textContent = typeof text === "string" ? text : JSON.stringify(text, null, 2);
}
function parse(id) {
  return JSON.parse($(id).value);
}

async function importData() {
  const el = $("action-msg");
  try {
    const body = {
      round_id: $("round-id").value.trim(),
      manifest: parse("manifest"),
      thresholds: parse("thresholds"),
      labels: parse("private").labels,
      batch_ids: parse("private").batch_ids,
    };
    msg(el, await api("/api/local-data", body), true);
  } catch (e) {
    msg(el, e.message, false);
  }
}

async function joinRound() {
  const el = $("action-msg");
  try {
    const rid = $("round-id").value.trim();
    // advance 内部自动 join，并按协调端当前状态推进一步（幂等可重试）
    msg(el, await api("/api/advance", { round_id: rid }), true);
    pollProgress();
  } catch (e) {
    msg(el, e.message, false);
  }
}

async function exitRound() {
  const el = $("action-msg");
  try {
    msg(el, await api("/api/exit", {
      round_id: $("round-id").value.trim(),
      reason: $("exit-reason").value,
    }), true);
    pollProgress();
  } catch (e) {
    msg(el, e.message, false);
  }
}

async function pollProgress() {
  const rid = $("round-id").value.trim();
  const t = $("token").value.trim();
  const box = $("progress");
  if (!rid || !t) {
    box.textContent = "请先填写轮次 ID 与令牌。";
    return;
  }
  try {
    const resp = await fetch(`/api/progress?round=${encodeURIComponent(rid)}`,
      { headers: { Authorization: `Bearer ${t}` } });
    const p = await resp.json();
    if (!resp.ok) throw new Error(p.error);
    const steps = STEPS.map((s) => {
      const done = p.my_steps.includes(s);
      return `<div class="step"><span class="dot ${done ? "on" : ""}"></span>` +
        `${s} ${done ? "✓ 已提交" : ""}</div>`;
    }).join("");
    box.innerHTML = `
      <div class="row">
        <div>协调端状态：<span class="pill ${p.coordinator_state}">${p.coordinator_state}</span></div>
        <div>本地数据：${p.have_local_data ? "已导入 ✓" : "未导入"}</div>
      </div>
      <div class="muted" style="margin:6px 0">清单指纹：${p.manifest_hash || "—"}</div>
      <div style="margin-top:8px">${steps}</div>`;
  } catch (e) {
    box.textContent = "进度查询失败：" + e.message;
  }
}

// 健康检查，确认本端身份
fetch("/health").then((r) => r.json()).then((d) => {
  $("lab-badge").textContent = d.lab_id;
  $("token").value = localStorage.getItem(d.lab_id + "-token") || "";
  $("token").onchange = () =>
    localStorage.setItem(d.lab_id + "-token", $("token").value);
});
setInterval(() => {
  if ($("round-id").value.trim()) pollProgress();
}, 4000);
