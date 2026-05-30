const form = document.querySelector("#task-form");
const refreshButton = document.querySelector("#refresh");
const cancelButton = document.querySelector("#cancel");
const statusBadge = document.querySelector("#status");
const taskTitle = document.querySelector("#task-title");
const eventsView = document.querySelector("#events-view");
const logsView = document.querySelector("#logs-view");
const artifactList = document.querySelector("#artifact-list");
const artifactView = document.querySelector("#artifact-view");
const taskList = document.querySelector("#task-list");

let activeTaskId = null;
let pollTimer = null;

const STATUS_LABELS = {
  idle: "空闲",
  queued: "排队中",
  running: "运行中",
  cancelling: "停止中",
  cancelled: "已停止",
  completed: "已完成",
  failed: "失败",
};

const PHASE_LABELS = {
  init: "初始化",
  report_md: "Markdown 报告",
  report_json: "JSON 报告",
  source: "源码分析包",
  subsource: "下游源码包",
  calls: "上层调用链",
  params: "入参约束",
  harness_generation_agent: "Harness 生成 Agent",
  fuzz_harness: "Fuzz 驱动 harness.c",
  harness_mocks_h: "Mock 头文件",
  harness_mocks_c: "Mock 源文件",
  harness_build_sh: "Unix 构建脚本",
  harness_build_ps1: "Windows 构建脚本",
  harness_spec: "Harness 规格",
  harness_dict: "Fuzz 字典",
  harness_compile_log: "编译日志",
  harness_run_log: "10 秒运行日志",
  harness_llm_transcript: "LLM 交互日志",
  harness_compile: "Harness 编译",
  harness_run: "libFuzzer 试跑",
  complete: "完成",
  failed: "失败",
  cancelled: "已停止",
  error: "错误",
};

const ARTIFACT_LABELS = {
  report_md: "Markdown 报告",
  report_json: "JSON 报告",
  source: "源码分析包",
  subsource: "下游源码包",
  calls: "上层调用链",
  params: "入参约束",
  harness_generation_agent: "Harness 生成 Agent",
  fuzz_harness: "Fuzz 驱动 harness.c",
  harness_mocks_h: "Mock 头文件",
  harness_mocks_c: "Mock 源文件",
  harness_build_sh: "Unix 构建脚本",
  harness_build_ps1: "Windows 构建脚本",
  harness_spec: "Harness 规格",
  harness_dict: "Fuzz 字典",
  harness_compile_log: "编译日志",
  harness_run_log: "10 秒运行日志",
  harness_llm_transcript: "LLM 交互日志",
};

const KREPO_ARTIFACTS = new Set(["report_md", "report_json", "source", "subsource", "calls", "params"]);
const HARNESS_ARTIFACTS = new Set([
  "harness_generation_agent",
  "fuzz_harness",
  "harness_mocks_h",
  "harness_mocks_c",
  "harness_build_sh",
  "harness_build_ps1",
  "harness_spec",
  "harness_dict",
  "harness_compile_log",
  "harness_run_log",
  "harness_llm_transcript",
]);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "object" && payload.error ? payload.error : response.statusText;
    throw new Error(message);
  }
  return payload;
}

function setField(name, value) {
  const input = form.elements[name];
  if (input && value !== undefined && value !== null) {
    input.value = value;
  }
}

function applyDefaults(defaults) {
  setField("repo", defaults.repo);
  setField("db", defaults.db);
  setField("function", defaults.function);
  setField("file", defaults.file);
  setField("model", defaults.model);
  setField("chat_url", defaults.chat_url);
  setField("api_key_env", defaults.api_key_env);
  setField("model_timeout", defaults.model_timeout);
  setField("model_max_retries", defaults.model_max_retries);
  setField("clang", defaults.clang);
  setField("max_repair_rounds", defaults.max_repair_rounds);
  setField("compile_timeout", defaults.compile_timeout);
  setField("max_deps", defaults.max_deps);
  setField("max_snippet_lines", defaults.max_snippet_lines);
  setField("max_depth", defaults.max_depth);
  setField("max_functions", defaults.max_functions);
  setField("call_depth", defaults.call_depth);
  setField("max_candidates", defaults.max_candidates);
  const selected = new Set(defaults.artifacts || []);
  for (const input of form.querySelectorAll('input[name="artifacts"]')) {
    input.checked = selected.has(input.value);
  }
}

function readConfig() {
  const data = new FormData(form);
  const config = {};
  for (const [key, value] of data.entries()) {
    if (key !== "artifacts" && typeof value === "string" && value.trim()) {
      config[key] = value.trim();
    }
  }
  config.artifacts = data.getAll("artifacts");
  return config;
}

function setTask(task) {
  statusBadge.textContent = taskStatusLabel(task);
  statusBadge.className = `badge ${taskStatusClass(task)}`;
  cancelButton.disabled = !task || !["queued", "running"].includes(task.status);
  taskTitle.textContent = task ? taskDisplayName(task) : "未开始";
}

function renderTask(task) {
  setTask(task);
  eventsView.textContent = task.events && task.events.length
    ? task.events.map(formatEvent).join("\n")
    : "等待任务事件...";
  logsView.textContent = task.log && task.log.length ? task.log.join("\n") : "等待命令日志...";
  logsView.scrollTop = logsView.scrollHeight;
  renderArtifacts(task.artifacts || {});
}

function formatEvent(event) {
  const time = event.ts ? new Date(event.ts * 1000).toLocaleTimeString() : "";
  const phase = PHASE_LABELS[event.phase] || event.phase;
  return `[${time} | ${phase}] ${event.message}`;
}

function renderArtifacts(artifacts) {
  const entries = Object.entries(artifacts);
  if (!entries.length) {
    artifactList.innerHTML = '<div class="item muted">暂无产物。</div>';
    return;
  }
  const groups = [
    ["kRepo 知识产物", entries.filter(([name]) => KREPO_ARTIFACTS.has(name))],
    ["Harness 生成 Agent 产物", entries.filter(([name]) => HARNESS_ARTIFACTS.has(name))],
    ["其他产物", entries.filter(([name]) => !KREPO_ARTIFACTS.has(name) && !HARNESS_ARTIFACTS.has(name))],
  ].filter(([, groupEntries]) => groupEntries.length);

  artifactList.innerHTML = "";
  for (const [title, groupEntries] of groups) {
    const heading = document.createElement("div");
    heading.className = "artifact-group-title";
    heading.textContent = title;
    artifactList.appendChild(heading);
    for (const [name, path] of groupEntries) {
      artifactList.appendChild(artifactButton(name, path));
    }
  }
}

function artifactButton(name, path) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "item";
    button.innerHTML = `<strong>${escapeHtml(ARTIFACT_LABELS[name] || name)}</strong><span class="muted">${escapeHtml(path)}</span>`;
    button.addEventListener("click", () => loadArtifact(name));
    return button;
}

async function loadArtifact(name) {
  if (!activeTaskId) {
    return;
  }
  artifactView.textContent = await api(`/api/tasks/${activeTaskId}/artifact?name=${encodeURIComponent(name)}`);
  switchTab("artifacts");
}

async function refreshTasks() {
  const payload = await api("/api/tasks");
  const tasks = payload.tasks || [];
  renderTaskList(tasks);
  if (!activeTaskId && tasks[0]) {
    activeTaskId = tasks[0].id;
  }
}

function renderTaskList(tasks) {
  if (!tasks.length) {
    taskList.innerHTML = '<div class="item muted">暂无任务。</div>';
    return;
  }
  taskList.innerHTML = "";
  for (const task of tasks) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "item";
    button.innerHTML = taskListItemHtml(task);
    button.addEventListener("click", () => selectTask(task.id));
    taskList.appendChild(button);
  }
}

function taskListItemHtml(task) {
  return [
    `<div class="item-head">`,
    `<strong>${escapeHtml(taskDisplayName(task))}</strong>`,
    `<span class="mini-badge ${escapeHtml(taskStatusClass(task))}">${escapeHtml(taskStatusLabel(task))}</span>`,
    `</div>`,
    `<div class="task-target">${escapeHtml(taskTargetDetail(task))}</div>`,
    `<div class="task-summary">${escapeHtml(taskPipelineSummary(task))}</div>`,
    `<div class="muted">${escapeHtml(taskDetail(task))}</div>`,
  ].join("");
}

function taskStatusLabel(task) {
  if (!task) {
    return STATUS_LABELS.idle;
  }
  if (task.status === "completed") {
    return completedStatusLabel(task);
  }
  if (task.status === "failed") {
    return failedStatusLabel(task);
  }
  return STATUS_LABELS[task.status] || task.status;
}

function failedStatusLabel(task) {
  const error = task.error || "";
  if (error.includes("libFuzzer 试跑失败")) {
    return "试跑失败";
  }
  if (error.includes("libFuzzer 试跑超时")) {
    return "试跑超时";
  }
  if (error.includes("Harness 编译失败")) {
    return "编译失败";
  }
  if (error.includes("Harness 未编译") || error.includes("仅生成")) {
    return "未完成";
  }
  if (error.includes("不支持自动生成")) {
    return "不支持";
  }
  if (error.includes("手工 Fixture")) {
    return "需手工 Fixture";
  }
  return "执行失败";
}

function taskStatusClass(task) {
  if (!task) {
    return "idle";
  }
  if (task.status !== "completed") {
    return task.status;
  }
  const harness = task.harness || {};
  const compile = harness.compile || {};
  const run = harness.run || {};
  if (run.status === "failed" || run.status === "timeout" || harness.status === "runtime_failed") {
    return "failed";
  }
  if (compile.status === "failed" || harness.status === "compile_failed") {
    return "failed";
  }
  if (
    compile.status === "skipped" ||
    run.status === "skipped" ||
    harness.status === "unsupported" ||
    harness.status === "needs_manual_fixture"
  ) {
    return "warning";
  }
  return "completed";
}

function completedStatusLabel(task) {
  const harness = task.harness || {};
  const compile = harness.compile || {};
  const run = harness.run || {};
  if (run.status === "success") {
    return "试跑通过";
  }
  if (run.status === "failed") {
    return "试跑失败";
  }
  if (run.status === "timeout") {
    return "试跑超时";
  }
  if (run.status === "skipped") {
    return "未试跑";
  }
  if (compile.status === "success") {
    return "编译通过";
  }
  if (compile.status === "failed") {
    return "编译失败";
  }
  if (compile.status === "skipped") {
    return skippedStatusLabel(harness.status);
  }
  if (harness.status === "run_succeeded") {
    return "试跑通过";
  }
  if (harness.status === "runtime_failed") {
    return "试跑失败";
  }
  if (harness.status === "compile_failed") {
    return "编译失败";
  }
  if (harness.status === "compiled") {
    return "编译通过";
  }
  if (harness.status === "generated") {
    return "Harness 已生成";
  }
  if (hasHarnessArtifacts(task)) {
    return "Harness 已生成";
  }
  if (hasKnowledgeArtifacts(task)) {
    return "抽取完成";
  }
  return STATUS_LABELS.completed;
}

function skippedStatusLabel(status) {
  if (status === "unsupported") {
    return "不支持";
  }
  if (status === "needs_manual_fixture") {
    return "需手工 Fixture";
  }
  return "未编译";
}

function hasKnowledgeArtifacts(task) {
  const artifacts = task.artifacts || {};
  return Object.keys(artifacts).some((name) => KREPO_ARTIFACTS.has(name));
}

function hasHarnessArtifacts(task) {
  const artifacts = task.artifacts || {};
  return Object.keys(artifacts).some((name) => HARNESS_ARTIFACTS.has(name));
}

function taskDisplayName(task) {
  const config = task.config || {};
  const func = config.function || "未命名函数";
  return `${taskActionName(task)}：${func}`;
}

function taskActionName(task) {
  const config = task.config || {};
  const selected = new Set(config.artifacts || []);
  const artifacts = task.artifacts || {};
  const hasAgent = selected.has("harness_generation_agent") || artifacts.harness_generation_agent || artifacts.fuzz_harness;
  const hasKnowledge = hasAgent || ["report_md", "report_json", "source", "subsource", "calls", "params"].some((name) => (
    selected.has(name) || artifacts[name]
  ));

  if (hasAgent) {
    return "生成 Fuzz Harness";
  }
  if (hasKnowledge) {
    return "抽取函数知识";
  }
  return "运行任务";
}

function taskTargetDetail(task) {
  const config = task.config || {};
  const parts = [];
  if (config.file) {
    parts.push(`目标文件：${config.file}`);
  }
  if (config.repo) {
    parts.push(`源码：${config.repo}`);
  }
  return parts.join(" · ") || "目标未指定";
}

function taskPipelineSummary(task) {
  const config = task.config || {};
  const selected = new Set(config.artifacts || []);
  const artifacts = task.artifacts || {};
  const stages = [];
  const hasAgent = selected.has("harness_generation_agent") || artifacts.harness_generation_agent || artifacts.fuzz_harness;
  const hasKnowledge = hasAgent || ["report_json", "subsource", "calls", "params"].some((name) => selected.has(name) || artifacts[name]);
  const hasHarness = Boolean(artifacts.fuzz_harness || artifacts.harness_generation_agent);
  const harness = task.harness || {};
  const compile = harness.compile || {};
  const run = harness.run || {};

  if (hasKnowledge) {
    stages.push("知识抽取");
  }
  if (hasAgent) {
    stages.push(hasHarness ? "Harness 已生成" : "Harness 生成 Agent");
  }
  if (compile.status) {
    stages.push(compileStageLabel(compile));
  }
  if (run.status) {
    stages.push(runStageLabel(run));
  } else if (artifacts.harness_run_log) {
    stages.push("10 秒试跑");
  }
  if (!stages.length) {
    stages.push("自定义任务");
  }

  const artifactCount = Object.keys(artifacts).length;
  const suffix = artifactCount ? ` · ${artifactCount} 个产物` : "";
  return stages.join(" -> ") + suffix;
}

function compileStageLabel(compile) {
  if (compile.status === "success") {
    return "编译通过";
  }
  if (compile.status === "failed") {
    return "编译失败";
  }
  if (compile.status === "skipped") {
    return "未编译";
  }
  return `编译 ${compile.status}`;
}

function runStageLabel(run) {
  const seconds = run.seconds || 10;
  if (run.status === "success") {
    return `${seconds} 秒试跑通过`;
  }
  if (run.status === "timeout") {
    return `${seconds} 秒试跑超时`;
  }
  if (run.status === "skipped") {
    return "试跑已跳过";
  }
  if (run.status === "failed") {
    return `${seconds} 秒试跑失败`;
  }
  return `${seconds} 秒试跑 ${run.status}`;
}

function taskDetail(task) {
  const parts = [`ID ${task.id}`];
  if (task.error) {
    parts.push(task.error);
  }
  if (task.task_dir) {
    parts.push(`目录 ${task.task_dir}`);
  }
  return parts.join(" · ");
}

async function selectTask(taskId) {
  activeTaskId = taskId;
  await refreshActiveTask();
  startPolling();
}

async function refreshActiveTask() {
  if (!activeTaskId) {
    setTask(null);
    return null;
  }
  const task = await api(`/api/tasks/${activeTaskId}`);
  renderTask(task);
  return task;
}

function startPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
  pollTimer = setInterval(async () => {
    try {
      const task = await refreshActiveTask();
      await refreshTasks();
      if (!task || !["queued", "running", "cancelling"].includes(task.status)) {
        await refreshTasks();
        clearInterval(pollTimer);
        pollTimer = null;
      }
    } catch (error) {
      eventsView.textContent = `刷新失败: ${error.message}`;
    }
  }, 1200);
}

function switchTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll(".panel")) {
    panel.classList.toggle("active", panel.id === `tab-${name}`);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  eventsView.textContent = "正在创建任务...";
  const task = await api("/api/tasks", {
    method: "POST",
    body: JSON.stringify(readConfig()),
  });
  activeTaskId = task.id;
  renderTask(task);
  switchTab("events");
  await refreshTasks();
  startPolling();
});

cancelButton.addEventListener("click", async () => {
  if (!activeTaskId) {
    return;
  }
  const task = await api(`/api/tasks/${activeTaskId}/cancel`, { method: "POST", body: "{}" });
  renderTask(task);
});

refreshButton.addEventListener("click", async () => {
  await refreshTasks();
  await refreshActiveTask();
});

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
}

(async function init() {
  try {
    applyDefaults(await api("/api/defaults"));
    await refreshTasks();
    await refreshActiveTask();
  } catch (error) {
    eventsView.textContent = `初始化失败: ${error.message}`;
  }
})();
