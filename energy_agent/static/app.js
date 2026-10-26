"use strict";
const $ = (id) => document.getElementById(id);
const state = { dataset: null, result: null, history: [], busy: false, previewSequence: 0 };
const palette = ["#4c8061", "#b1b981", "#628c9c", "#c4a267"];
const labels = { load_mw: "实际负荷", forecast_mw: "预测负荷", mae_mw: "MAE", rmse_mw: "RMSE", deviation_pct: "偏差", energy_mwh: "观测电量", robust_z: "稳健异常分数" };
const number = (n, digits = 2) => Number(n).toLocaleString("en-US", { maximumFractionDigits: digits });
function element(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function errorMessage(text) {
  $("global-error").textContent = text || "";
  $("global-error").hidden = !text;
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, signal: AbortSignal.timeout(240000) });
  let body;
  try { body = await response.json(); } catch { throw new Error("服务返回无效响应，请检查本地服务。"); }
  if (!response.ok) {
    const message = typeof body.detail === "string" ? body.detail : "输入格式不正确，请检查问题和日期。";
    throw new Error(message);
  }
  return body;
}
function setBusy(busy) {
  state.busy = busy;
  $("loading").hidden = !busy;
  $("result-content").hidden = busy;
  [$("submit"), $("region"), $("download"), $("start-date"), $("end-date"), $("toggle-dates"), $("clear-dates"),
   ...document.querySelectorAll("[data-question], #history button")].forEach((el) => { el.disabled = busy; });
  $("submit").firstElementChild.textContent = busy ? "分析中…" : "开始分析";
  if (!busy) $("download").disabled = !state.result?.result.rows.length;
}
function view(name) {
  for (const key of ["analysis", "data", "about"]) {
    $(`${key}-view`).hidden = name !== key;
    $(`nav-${key}`).classList.toggle("active", name === key);
  }
  $("mobile-data").textContent = name === "analysis" ? "数据来源" : "返回分析";
  if (name === "analysis") renderChart();
}
function chooseTab(name) {
  for (const tab of document.querySelectorAll("[data-tab]")) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    $(`panel-${tab.dataset.tab}`).hidden = !active;
  }
  if (name === "chart") renderChart();
}
function updateMetrics() {
  const r = state.dataset.regions.find((r) => r.region === $("region").value);
  if (!r) return;
  $("metric-average").textContent = number(r.avg_load_mw);
  $("metric-peak").textContent = number(r.peak_load_mw);
  $("metric-rows").textContent = number(r.rows, 0);
  $("metric-region").textContent = `${r.region} · 区域全量数据 · UTC`;
  $("metric-coverage").textContent = r.missing_hours ? `缺少 ${r.missing_hours} 小时 · 未补零` : "时间序列完整 · 无重复记录";
  $("dataset-range").textContent = `${r.start.slice(0, 10)} — ${r.end.slice(0, 10)}`;
  for (const field of ["start-date", "end-date"]) {
    $(field).min = r.start.slice(0, 10);
    $(field).max = r.end.slice(0, 10);
  }
}
function renderDataset() {
  const { metadata, regions, schema } = state.dataset;
  $("dataset-name").textContent = metadata.synthetic ? "PJM · 合成演示样本" : metadata.name;
  const banner = $("source-banner");
  banner.querySelector("strong").textContent = metadata.synthetic ? "演示数据空间" : "已导入本地数据";
  banner.querySelector("div").querySelector("span").textContent = metadata.synthetic
    ? "数据为可复现的合成样本，并非真实 PJM 观测。支持导入你的小时负荷 CSV。"
    : `来源：${metadata.source}。数据来源标签由导入者提供。`;
  $("dataset-detail").replaceChildren();
  const dl = element("dl", undefined, "catalog-meta");
  for (const [name, value] of [["数据集", metadata.name], ["来源", metadata.source],
                              ["演示数据", metadata.synthetic ? "是 · 不代表真实 PJM 运行" : "否 · 用户导入，来源未自动验证"],
                              ["数据粒度", "1 小时 × 区域"], ["时区", "UTC"], ["导入时间", metadata.created_at]]) {
    dl.append(element("dt", name), element("dd", value));
  }
  $("dataset-detail").append(dl, element("h2", "覆盖与质量"));
  const coverage = element("div", undefined, "table-container");
  const t = element("table"), head = element("tr");
  for (const name of ["区域", "起始日 UTC", "结束日 UTC", "记录数", "缺失小时", "预测覆盖"]) head.append(element("th", name));
  t.append(head);
  for (const r of regions) {
    const tr = element("tr");
    for (const val of [r.region, r.start.slice(0, 10), r.end.slice(0, 10), r.rows, r.missing_hours, `${r.forecast_rows}/${r.rows}`]) tr.append(element("td", String(val)));
    t.append(tr);
  }
  coverage.append(t);
  $("dataset-detail").append(coverage, element("h2", "字段定义"));
  const fields = element("dl", undefined, "catalog-meta");
  for (const [name, desc] of Object.entries(schema.load_hourly)) fields.append(element("dt", name), element("dd", desc));
  $("dataset-detail").append(fields);
}
function renderTable() {
  const { columns, rows } = state.result.result;
  const container = $("data-table"); container.replaceChildren();
  if (!rows.length) { container.append(element("div", "没有结果数据。", "empty-state")); return; }
  const table = element("table"), thead = element("thead"), heading = element("tr"), tbody = element("tbody");
  for (const col of columns) heading.append(element("th", col));
  thead.append(heading);
  for (const row of rows) {
    const tr = element("tr");
    for (const col of columns) tr.append(element("td", row[col] === null ? "NULL" : typeof row[col] === "number" ? number(row[col], 3) : String(row[col])));
    tbody.append(tr);
  }
  table.append(thead, tbody); container.append(table);
}
function renderTrace() {
  const names = { inspect: "读取结构与确定任务", plan: "生成查询计划", check: "查询校验", execute: "执行只读查询", semantics: "独立领域数值核验", repair: "错误纠正", summarize: "生成结果摘要" };
  $("trace-list").replaceChildren();
  for (const step of state.result.trace) {
    const row = element("div", undefined, "trace-item");
    const icon = element("span", step.status === "error" ? "!" : step.status === "retry" ? "↻" : "✓", `trace-icon ${step.status}`);
    const text = element("div"); text.append(element("strong", names[step.node] || step.node), element("p", step.detail));
    row.append(icon, text, element("small", `${number(step.duration_ms)} ms`));
    $("trace-list").append(row);
  }
}
function renderResult(result, preview = false) {
  state.result = result;
  $("result-eyebrow").textContent = preview ? "数据预览 · 固定查询" : "ANALYSIS RESULT";
  $("result-title").textContent = result.title;
  $("answer").textContent = result.answer;
  $("answer").hidden = preview;
  $("answer").classList.toggle("error", result.status === "error");
  $("row-count").textContent = result.result.rows.length;
  $("sql-code").textContent = (result.sql || "本次未执行 SQL。") + (result.sql_parameters ? "\n\n-- 绑定参数（区域、历史起点、结束不含）\n" + JSON.stringify(result.sql_parameters, null, 2) : "");
  $("method").textContent = result.method ? `方法说明：${result.method}` : "";
  $("method").hidden = !result.method;
  $("warnings").replaceChildren(...result.warnings.map((s) => element("p", `ⓘ ${s}`)));
  const scope = result.scope;
  $("scope-label").textContent = scope
    ? `${scope.region} · ${scope.start.slice(0, 10)} 至 ${new Date(Date.parse(scope.end) - 86400000).toISOString().slice(0, 10)} · UTC`
    : "查询范围未通过校验";
  $("scope-label").title = scope?.note || "";
  const usage = result.usage;
  $("run-stats").textContent = `${number(result.latency_ms)} ms · ${usage.calls} 次模型调用 · ${usage.input_tokens + usage.output_tokens} tokens · ${usage.estimated_cost_usd === null ? "费用未配置" : "$" + number(usage.estimated_cost_usd, 6)}`;
  $("download").disabled = !result.result.rows.length;
  renderTable(); renderTrace(); renderValidation(); chooseTab("chart");
}
function renderValidation() {
  const r = state.result, task = r.task, check = r.semantic_check || {status: "not_run", checks: []};
  const names = { verified: "✓ 指标数值核验通过", domain_tool: "✓ 历史基线工具", not_covered: "开放式问题 · 语义未覆盖", not_run: "未进行领域核验", failed: "领域核验未通过" };
  const taskNames = { trend: "平均负荷", peak: "小时峰值", forecast: "预测误差", energy: "观测电量", anomaly: "历史异常筛查", count: "记录数", custom: "开放式 SQL" };
  const grains = { hour: "小时", day: "按日", week: "按周", total: "整体", day_type: "工作日 / 周末" };
  const summary = $("plan-summary"); summary.replaceChildren();
  const badge = element("span", names[check.status] || check.status, `validation-badge ${check.status}`);
  summary.append(badge);
  if (task) summary.append(element("span", `${taskNames[task.kind]} · ${grains[task.granularity]}${["peak", "anomaly"].includes(task.kind) ? " · Top " + task.top_k : ""} · ${task.unit}`));
  const detail = $("validation-detail"); detail.replaceChildren(element("h3", names[check.status] || check.status));
  for (const item of check.checks) detail.append(element("p", "✓ " + item));
  if (check.limitation) detail.append(element("p", check.limitation, "validation-note"));
  const coverage = r.quality;
  if (coverage) {
    detail.append(element("h3", "请求窗口的数据覆盖"));
    const dl = element("dl", undefined, "validation-grid");
    for (const [key, value] of [["小时记录", `${coverage.observed_hours} / ${coverage.expected_hours}`], ["覆盖率", `${coverage.coverage_pct ?? "—"}%`], ["预测可用小时", coverage.forecast_hours], ["MAPE 有效小时", coverage.mape_hours]]) dl.append(element("dt", key), element("dd", String(value)));
    detail.append(dl, element("p", "工作日为 UTC 周一至周五，未接入节假日日历。", "validation-note"));
  }
  const evidence = r.result.anomaly_evidence;
  if (evidence) {
    detail.append(element("h3", "异常筛查依据"));
    for (const text of [`已评分 ${evidence.scored_hours} 小时；历史不足 ${evidence.insufficient_history_hours} 小时。`, `历史读取起点：${evidence.history_read_start}；每点仅使用其之前的数据。`, `中位数与 MAD，至少 ${evidence.min_history_points} 个历史点；阈值 ${evidence.threshold}。`, "历史基线额外读取请求起点前最多 28 天，输出不超出请求窗口；不据此判断故障原因。"]) detail.append(element("p", text));
  }
  if (r.error_code) detail.append(element("p", `错误分类：${r.error_code} · ${r.outcome}`, "validation-note"));
}
function svg(tag, attrs = {}, content) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (content !== undefined) node.textContent = content;
  return node;
}
function shortLabel(value) {
  const text = String(value);
  return /^\d{4}-\d{2}-\d{2}T/.test(text) ? text.slice(5, 10) + " " + text.slice(11, 16)
    : /^\d{4}-\d{2}-\d{2}$/.test(text) ? text.slice(5) : text.slice(0, 20);
}
function renderChart() {
  const container = $("chart");
  if (!state.result || container.getBoundingClientRect().width < 1) return;
  const chart = state.result.chart;
  container.replaceChildren(); $("legend").replaceChildren();
  if (!chart) {
    container.append(element("div", state.result.status === "error" ? "查询未完成，请查看执行记录。" : "此结果没有适合展示的图表，请查看结果数据。", "empty-state"));
    $("chart-unit").textContent = ""; return;
  }
  const width = Math.max(280, container.clientWidth), height = container.clientHeight;
  const pad = { left: width < 400 ? 51 : 61, right: 15, top: 12, bottom: 37 };
  const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
  const data = chart.data, n = data.length;
  const all = data.flatMap((r) => chart.y.map((y) => r[y])).filter((v) => typeof v === "number" && Number.isFinite(v));
  if (!all.length) { container.append(element("div", "所有数值均为空。", "empty-state")); return; }
  let low = Math.min(...all), high = Math.max(...all);
  if (chart.type === "bar") { low = Math.min(0, low); high = Math.max(0, high); }
  const extra = (high - low || Math.abs(high) || 1) * .15;
  if (chart.type === "line") { low = Math.max(0, low - extra); high += extra; }
  else { high += extra; if (low < 0) low -= extra; }
  if (high === low) high = low + 1;
  const x = (i) => pad.left + (chart.type === "bar" ? (i + .5) / n : n === 1 ? .5 : i / (n - 1)) * plotWidth;
  const y = (val) => pad.top + (high - val) / (high - low) * plotHeight;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${state.result.title}，${n} 个数据点。详细数值见结果数据页签。` });
  for (let i = 0; i <= 4; i++) {
    const value = low + (high - low) * i / 4, yy = y(value);
    root.append(svg("line", { x1: pad.left, y1: yy, x2: width - pad.right, y2: yy, class: "grid-line" }));
    root.append(svg("text", { x: pad.left - 10, y: yy + 3, "text-anchor": "end" }, number(value, Math.abs(high) < 10 ? 2 : 0)));
  }
  const tickCount = width < 500 ? 3 : 7;
  for (let i = 0; i < n; i++) {
    if (i !== n - 1 && i % Math.max(1, Math.ceil((n - 1) / (tickCount - 1)))) continue;
    root.append(svg("text", { x: x(i), y: height - 9, "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle" }, shortLabel(data[i][chart.x])));
  }
  const tooltip = element("div", undefined, "chart-tooltip"); tooltip.hidden = true;
  function hover(target, row, i) {
    target.addEventListener("pointerenter", () => {
      tooltip.textContent = `${row[chart.x]} · ${chart.y.map((key) => `${labels[key] || key}: ${row[key] === null ? "NULL" : number(row[key], 3)}`).join(" / ")}`;
      tooltip.hidden = false;
      tooltip.style.left = `${Math.min(width - 225, Math.max(0, x(i) - 85))}px`;
      tooltip.style.top = "0px";
    });
    target.addEventListener("pointerleave", () => { tooltip.hidden = true; });
  }
  chart.y.forEach((key, index) => {
    const color = palette[index % palette.length];
    const item = element("span", undefined, "legend-item"), swatch = element("i", undefined, "legend-swatch");
    swatch.style.backgroundColor = color;
    item.append(swatch, element("span", labels[key] || key)); $("legend").append(item);
    if (chart.type === "bar") {
      const barWidth = Math.min(45, plotWidth / n * .64 / chart.y.length);
      data.forEach((row, i) => {
        if (typeof row[key] !== "number") return;
        const zero = y(0), position = y(row[key]);
        const bar = svg("rect", { x: x(i) - barWidth * chart.y.length / 2 + index * barWidth, y: Math.min(zero, position),
          width: Math.max(1, barWidth - 2), height: Math.max(1, Math.abs(zero - position)), rx: 2, fill: color, opacity: .85 });
        hover(bar, row, i); root.append(bar);
      });
    } else {
      const segments = []; let segment = [];
      data.forEach((row, i) => {
        if (typeof row[key] === "number") segment.push([x(i), y(row[key]), i]);
        else if (segment.length) { segments.push(segment); segment = []; }
      });
      if (segment.length) segments.push(segment);
      for (const points of segments) {
        const path = points.map((p, j) => `${j ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
        if (index === 0 && points.length > 1) root.append(svg("path", { d: `${path} L${points.at(-1)[0]},${y(low)} L${points[0][0]},${y(low)} Z`, fill: color, opacity: .045 }));
        root.append(svg("path", { d: path, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-dasharray": index === 1 ? "5 4" : "none" }));
        for (const [xx, yy, i] of points) {
          root.append(svg("circle", { cx: xx, cy: yy, r: n > 70 ? 1 : 3, fill: "white", stroke: color, "stroke-width": 1.5 }));
          const hit = svg("circle", { cx: xx, cy: yy, r: 8, fill: "transparent" });
          hover(hit, data[i], i); root.append(hit);
        }
      }
    }
  });
  $("chart-unit").textContent = chart.unit || (chart.y.some((k) => k.includes("pct")) ? "偏差 · %" : chart.y.some((k) => k.includes("mwh")) ? "电量 · MWh" : "负荷 · MW");
  container.append(root, tooltip);
}
async function preview() {
  const sequence = ++state.previewSequence;
  try {
    const result = await api(`/api/preview?region=${encodeURIComponent($("region").value)}`);
    if (sequence === state.previewSequence && !state.busy) renderResult(result, true);
  } catch (error) { errorMessage(error.message); }
}
async function submit(question) {
  if (state.busy) return;
  const text = question.trim();
  if (text.length < 2) { errorMessage("请输入至少两个字符的问题。"); return; }
  const request = { question: text, region: $("region").value };
  if ($("start-date").value || $("end-date").value) {
    if (!$("start-date").value || !$("end-date").value) { errorMessage("开始和结束日期必须同时提供。"); return; }
    request.start_date = $("start-date").value; request.end_date = $("end-date").value;
  }
  state.previewSequence++; errorMessage(""); setBusy(true); view("analysis");
  try {
    const result = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
    renderResult(result);
    state.history.unshift({ request, result }); state.history = state.history.slice(0, 12);
    renderHistory();
  } catch (error) {
    errorMessage(error.name === "TimeoutError" ? "请求超时，请稍后重试。" : error.message);
  } finally { setBusy(false); renderChart(); }
}
function renderHistory() {
  $("history").replaceChildren();
  for (const entry of state.history) {
    const button = element("button", `◷  ${entry.request.question}`); button.title = entry.request.question;
    button.disabled = state.busy;
    button.addEventListener("click", () => {
      if (state.busy) return;
      $("question").value = entry.request.question; $("region").value = entry.request.region;
      $("start-date").value = entry.request.start_date || ""; $("end-date").value = entry.request.end_date || "";
      updateMetrics(); view("analysis"); renderResult(entry.result);
    });
    $("history").append(button);
  }
}
$("ask-form").addEventListener("submit", (e) => { e.preventDefault(); submit($("question").value); });
$("question").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit($("question").value); } });
for (const button of document.querySelectorAll("[data-question]")) button.addEventListener("click", () => {
  const question = $("start-date").value || $("end-date").value
    ? button.dataset.question.replace(/^最近7天(?:的)?/, "") : button.dataset.question;
  $("question").value = question; submit(question);
});
for (const button of document.querySelectorAll("[data-tab]")) button.addEventListener("click", () => chooseTab(button.dataset.tab));
document.querySelector(".tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...document.querySelectorAll("[data-tab]")];
  let index = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
  index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  chooseTab(tabs[index].dataset.tab); tabs[index].focus(); event.preventDefault();
});
for (const name of ["analysis", "data", "about"]) $(`nav-${name}`).addEventListener("click", () => view(name));
$("source-details").addEventListener("click", () => view("data"));
$("mobile-data").addEventListener("click", () => view($("analysis-view").hidden ? "analysis" : "data"));
$("region").addEventListener("change", () => { $("start-date").value = ""; $("end-date").value = ""; updateMetrics(); preview(); });
$("toggle-dates").addEventListener("click", () => { const open = $("date-filters").hidden; $("date-filters").hidden = !open; $("toggle-dates").setAttribute("aria-expanded", String(open)); });
$("clear-dates").addEventListener("click", () => { $("start-date").value = ""; $("end-date").value = ""; });
$("copy-sql").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(state.result?.sql || ""); $("copy-sql").textContent = "已复制"; }
  catch { $("copy-sql").textContent = "请手动选中复制"; }
  setTimeout(() => { $("copy-sql").textContent = "复制 SQL"; }, 1800);
});
$("download").addEventListener("click", () => {
  if (!state.result) return;
  const { columns, rows } = state.result.result;
  function cell(value) {
    if (value === null) return "";
    let text = String(value);
    if (typeof value === "string" && /^[\s]*[=+@-]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  }
  const content = [columns.map(cell).join(","), ...rows.map((row) => columns.map((col) => cell(row[col])).join(","))].join("\r\n");
  const url = URL.createObjectURL(new Blob(["\ufeff", content], { type: "text/csv;charset=utf-8" }));
  const link = element("a"); link.href = url; link.download = "gridnote-result.csv"; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
new ResizeObserver(() => renderChart()).observe($("chart"));
async function init() {
  try {
    const [health, dataset] = await Promise.all([api("/api/health"), api("/api/dataset")]);
    state.dataset = dataset;
    $("planner-badge").textContent = health.planner === "demo" ? "规则演示 · 无模型调用" : `OpenAI · ${health.model}`;
    for (const region of dataset.regions) {
      const option = element("option", region.region); option.value = region.region; $("region").append(option);
    }
    $("region").value = dataset.regions.some((r) => r.region === "PJM_RTO") ? "PJM_RTO" : dataset.regions[0].region;
    renderDataset(); updateMetrics(); await preview();
  } catch (error) { errorMessage(`无法连接本地数据：${error.message}`); }
}
init();
