// Patreon Watch Dog - one-click test report page script.
//
// Runs through the AstrBot Plugin Page bridge:
//   bridge.apiPost("report", {}) reaches the plugin backend endpoint
//   /astrbot_plugin_patreon_watch_dog/report registered via
//   context.register_web_api().

const bridge = window.AstrBotPluginPage;

const runBtn = document.getElementById("run-btn");
const statusEl = document.getElementById("status");
const summaryEl = document.getElementById("summary");
const errorsEl = document.getElementById("errors");
const previewEl = document.getElementById("preview");

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = `status ${kind}`;
}

function show(element) {
  element.classList.remove("hidden");
}

function hide(element) {
  element.classList.add("hidden");
}

runBtn.addEventListener("click", async () => {
  runBtn.disabled = true;
  setStatus("正在获取帖子并发送报告…", "loading");
  hide(summaryEl);
  hide(errorsEl);
  previewEl.textContent = "";

  try {
    const result = await bridge.apiPost("report", {});
    setStatus(
      result.ok ? "✅ 测试完成" : "⚠️ 测试完成（存在错误）",
      result.ok ? "success" : "error",
    );
    summaryEl.textContent =
      `博主: ${result.creators} | 帖子: ${result.posts} | ` +
      `发送成功: ${result.sent} | 失败: ${result.failed}`;
    show(summaryEl);

    if (result.errors && result.errors.length) {
      errorsEl.textContent = result.errors.join("\n");
      show(errorsEl);
    }

    previewEl.textContent = result.markdown || "（当前没有获取到帖子）";
  } catch (error) {
    setStatus(`❌ 测试失败: ${error.message}`, "error");
  } finally {
    runBtn.disabled = false;
  }
});
