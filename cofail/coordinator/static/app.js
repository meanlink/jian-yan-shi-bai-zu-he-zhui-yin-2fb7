// 协调端页面逻辑：只展示轮次状态、步骤到齐情况与三室合计简报
const $ = (id) => document.getElementById(id);
const STEPS = ["frozen", "triple", "input", "mult", "aggregate"];

function adminHeaders() {
  return {
    "Content-Type": "application/json",
    "X-Admin-Token": $("admin-token").value.trim(),
  };
}
function show(el, text, ok) {
  el.className = "msg " + (ok ? "ok" : "err");
  el.textContent = typeof text === "string" ? text : JSON.stringify(text, null, 2);
}

async function createRound() {
  const el = $("create-msg");
  try {
    const resp = await fetch("/api/admin/rounds", {
      method: "POST",
      headers: adminHeaders(),
      body: JSON.stringify({
        manifest: JSON.parse($("manifest").value),
        thresholds: JSON.parse($("thresholds").value),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error);
    $("round-id").value = data.round_id;
    show(el, `已冻结并发起轮次：${data.round_id}\n成员：${data.frozen.members.join(", ")}` +
      `\n样本数：${data.frozen.manifest.length}\n清单指纹：${data.frozen.manifest_fingerprint}`, true);
    pollStatus();
  } catch (e) {
    show(el, e.message, false);
  }
}

async function pollStatus() {
  const rid = $("round-id").value.trim();
  const box = $("status");
  try {
    if (!rid) {
      const all = await (await fetch("/api/rounds")).json();
      box.innerHTML = all.rounds.map((r) =>
        `<div class="step"><span class="pill ${r.state}">${r.state}</span>` +
        ` <a onclick="$('round-id').value='${r.round_id}';pollStatus()">${r.round_id}</a></div>`
      ).join("") || "暂无轮次。";
      return;
    }
    const p = await (await fetch(`/api/rounds/${rid}/status`)).json();
    if (p.error) throw new Error(p.error);
    const steps = p.progress.map((s) =>
      `<div class="step"><span class="dot ${s.complete ? "on" : ""}"></span>` +
      `${s.step}：${s.received.length}/3 ${s.complete ? "✓" : "（等待 " +
        (3 - s.received.length) + " 家）"}</div>`).join("");
    let extra = "";
    if (p.state === "void") extra = `<p class="msg err">整轮作废：${p.void_reason}</p>`;
    if (p.state === "completed" && p.briefing) {
      const t = p.briefing.totals;
      extra = `<p class="msg ok">三室合计（分母 ${t.samples}，含全通过）：` +
        `共败 ${t.common_fail} ｜ 池化判败 ${t.pooled_fail} ｜ 全通过 ${t.all_pass}</p>` +
        renderRules(p.briefing.rules);
    }
    box.innerHTML = `
      <div>状态：<span class="pill ${p.state}">${p.state}</span>
      成员：${p.members.join(", ") || "（尚未有人加入）"}</div>
      <div class="muted kv" style="margin:6px 0">清单指纹：${p.frozen.manifest_fingerprint}</div>
      <div style="margin-top:8px">${steps}</div>${extra}`;
  } catch (e) {
    box.textContent = "查询失败：" + e.message;
  }
}

function renderRules(rules) {
  const rows = rules.map((r) => `<tr>
    <td>${r.cell}</td><td>${r.denominator}</td>
    <td>${r.common_fail}</td><td>${r.common_rate}</td>
    <td>${r.pooled_fail}</td><td>${r.pooled_rate}</td>
    <td>${r.all_pass}</td><td>${r.lift_common ?? "—"}</td>
    <td class="${r.hit ? "hit" : ""}">${r.hit ? "命中" : "未达阈值"}</td></tr>`).join("");
  return `<table><thead><tr><th>规则单元</th><th>分母(含全通过)</th>
    <th>共败数</th><th>共败率</th><th>池化败次</th><th>池化败率</th>
    <th>全通过</th><th>共败lift</th><th>结论</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function listBriefings() {
  const box = $("briefings");
  const data = await (await fetch("/api/briefings")).json();
  if (!data.briefings.length) {
    box.textContent = "暂无已完成轮次的简报。";
    return;
  }
  box.innerHTML = data.briefings.map((b) =>
    `<div class="step"><a onclick="showBriefing('${b.briefing_id}')">${b.briefing_id}</a>` +
    ` <span class="muted">轮次 ${b.round_id} · ${b.created_at}</span></div>`).join("");
}

async function showBriefing(bid) {
  const b = await (await fetch(`/api/briefings/${bid}`)).json();
  $("briefing-card").style.display = "block";
  $("briefing-detail").innerHTML = `
    <div class="kv muted">简报ID：${b.briefing_id}<br>轮次：${b.round_id}<br>
    清单指纹：${b.manifest_fingerprint}<br>成员公钥指纹：${JSON.stringify(b.member_key_fingerprints)}<br>
    冻结阈值：${JSON.stringify(b.thresholds)}</div>
    <div style="margin:10px 0">${renderRules(b.rules)}</div>`;
}

setInterval(() => {
  if ($("round-id").value.trim()) pollStatus();
}, 4000);
