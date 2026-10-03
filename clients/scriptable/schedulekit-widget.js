// ScheduleKit Scriptable 小组件
//
// 安装：
//   1. 安装 Scriptable（App Store，免费）
//   2. 网页控制台 → 设置 → API Key → 新建，勾「只读」
//   3. 复制本文件内容到 Scriptable 新建的脚本里，命名如 ScheduleKit
//   4. 长按主屏 → 添加小组件 → Scriptable → 选本脚本
//   5. **长按小组件 → 编辑小组件 → Parameter 填：baseUrl|apiKey**
//      例如： https://canisa1ph.duckdns.org:8443|sk_xxxxxxxx
//
// 为什么把凭据放在「Parameter」而不是写进代码：
//   * 脚本内容可能被分享/备份/贴到别处，写死就等于泄漏；
//   * Parameter 存在系统的小组件配置里，不会随脚本走；
//   * 如果你更谨慎，可以改成从 Keychain 读（见文件末尾的 buildConfig）。
//
// 为什么建议用**只读** Key：小组件只需要 GET。
// 只读 Key 泄了也改不了你的任务，而写权限在快捷指令那边用。

// ─────────────────────────────────────────────────────────────
// 配置解析
// ─────────────────────────────────────────────────────────────
function buildConfig() {
  const raw = (args.widgetParameter || "").trim();
  let baseUrl = "";
  let apiKey = "";

  if (raw.includes("|")) {
    const parts = raw.split("|");
    baseUrl = parts[0].trim();
    apiKey = parts.slice(1).join("|").trim();
  } else if (raw.startsWith("http")) {
    baseUrl = raw;
    // 没给 Key 时退回 Keychain（首次运行会提示输入）
    apiKey = Keychain.contains("schedulekit_key")
      ? Keychain.get("schedulekit_key")
      : "";
  } else {
    baseUrl = "https://canisa1ph.duckdns.org:8443";
    apiKey = Keychain.contains("schedulekit_key")
      ? Keychain.get("schedulekit_key")
      : "";
  }

  baseUrl = baseUrl.replace(/\/+$/, "");
  return { baseUrl, apiKey };
}

const CFG = buildConfig();
const CACHE_KEY = "schedulekit_widget_cache";

// 缓存上一次成功的数据：网络失败时至少能显示"上次看到什么"，
// 而不是一个空白小组件。**这对手机在电梯/地铁里尤其重要。**
function readCache() {
  if (!Keychain.contains(CACHE_KEY)) return null;
  try {
    return JSON.parse(Keychain.get(CACHE_KEY));
  } catch (e) {
    return null;
  }
}

function writeCache(payload) {
  try {
    Keychain.set(CACHE_KEY, JSON.stringify(payload));
  } catch (e) {
    /* 缓存失败不影响主流程 */
  }
}

async function api(path) {
  const req = new Request(CFG.baseUrl + path);
  req.headers = { Authorization: "Bearer " + CFG.apiKey };
  req.timeoutInterval = 12;
  return req.loadJSON();
}

// ─────────────────────────────────────────────────────────────
// 数据
// ─────────────────────────────────────────────────────────────
async function loadAll() {
  const [ordered, unordered, status] = await Promise.all([
    api("/api/tasks?view=ordered"),
    api("/api/tasks?view=unordered"),
    api("/api/status"),
  ]);
  const payload = { ordered, unordered, status, fetchedAt: new Date().toISOString() };
  writeCache(payload);
  return payload;
}

function humanDue(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  const minutes = Math.round((then - new Date()) / 60000);
  if (minutes < 0) {
    const over = Math.abs(minutes);
    if (over < 60) return `${over}分前逾期`;
    if (over < 60 * 24) return `${Math.round(over / 60)}时前逾期`;
    return `${Math.round(over / 1440)}天前逾期`;
  }
  if (minutes < 60) return `${minutes}分后`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}时后`;
  return `${Math.round(minutes / 1440)}天后`;
}

const PRIORITY_LABELS = { 1: "Ⅰ", 2: "Ⅱ", 3: "Ⅲ", 4: "Ⅳ", 5: "Ⅴ" };

// ─────────────────────────────────────────────────────────────
// 渲染
// ─────────────────────────────────────────────────────────────
function severityColor(item) {
  if (item.is_overdue) return Color.red();
  if (item.priority === 1) return Color.red();
  if (item.priority === 2) return Color.orange();
  if (item.due_at) {
    const hours = (new Date(item.due_at) - new Date()) / 3600000;
    if (hours <= 24) return Color.orange();
  }
  return null; // 用系统默认色，深浅色模式都好看
}

function taskRow(w, item, { compact }) {
  const line = w.addStack();
  line.layoutHorizontally();
  line.centerAlignContent();

  const marker = w.addText(item.due_at ? "•" : PRIORITY_LABELS[item.priority] || "•");
  marker.font = Font.systemFont(compact ? 10 : 12);
  const color = severityColor(item);
  if (color) marker.textColor = color;

  w.addSpacer(compact ? 4 : 6);

  const title = w.addText(item.title);
  title.font = Font.systemFont(compact ? 11 : 13);
  title.lineLimit = 1;

  w.addSpacer();

  if (item.due_at) {
    const due = w.addText(humanDue(item.due_at));
    due.font = Font.systemFont(compact ? 9 : 11);
    due.textColor = item.is_overdue ? Color.red() : Color.gray();
  }
  return line;
}

function header(w, status, stale) {
  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  const week = status && status.week > 0 ? `第 ${status.week} 周` : "假期";
  const title = w.addText("ScheduleKit");
  title.font = Font.boldSystemFont(14);
  row.addSpacer(6);
  const badge = w.addText(week);
  badge.font = Font.systemFont(11);
  badge.textColor = Color.gray();
  row.addSpacer();
  if (stale) {
    const warn = w.addText("离线");
    warn.font = Font.systemFont(10);
    warn.textColor = Color.orange();
  }
  return row;
}

function footer(w, counts, fetchedAt) {
  w.addSpacer(4);
  const row = w.addStack();
  row.layoutHorizontally();
  const left = w.addText(`${counts.ordered} 待截止 · ${counts.unordered} 待排序`);
  left.font = Font.systemFont(9);
  left.textColor = Color.gray();
  row.addSpacer();
  if (fetchedAt) {
    const stamp = w.addText(new Date(fetchedAt).toLocaleTimeString());
    stamp.font = Font.systemFont(9);
    stamp.textColor = Color.gray();
  }
  return row;
}

function emptyState(w, message) {
  w.addSpacer();
  const text = w.addText(message);
  text.font = Font.systemFont(12);
  text.textColor = Color.gray();
  text.centerAlignText();
  w.addSpacer();
}

function render(payload, stale) {
  const w = new ListWidget();
  w.setPadding(12, 12, 12, 12);
  // 深色模式下用系统背景，浅色模式也一样——不要硬编码白底
  w.backgroundColor = Color.dynamic(
    new Color("#ffffff", 1),
    new Color("#1c1e21", 1)
  );

  const status = payload.status || {};
  header(w, status, stale);
  w.addSpacer(6);

  const family = config.widgetFamily || "medium";
  const ordered = payload.ordered?.items || [];
  const unordered = payload.unordered?.items || [];

  if (family === "small") {
    // 小尺寸：只放最紧要的那几条有序任务
    const slice = ordered.slice(0, 3);
    if (!slice.length && !unordered.length) {
      emptyState(w, "没有待办");
    } else {
      for (const item of slice) {
        taskRow(w, item, { compact: true });
        w.addSpacer(2);
      }
      if (!slice.length) {
        const fallback = unordered.slice(0, 3);
        for (const item of fallback) {
          taskRow(w, item, { compact: true });
          w.addSpacer(2);
        }
      }
    }
  } else if (family === "medium") {
    // 中尺寸：有序 3 条 + 无序 2 条。分栏是为了让"有没有截止时间"一眼可辨。
    w.addSpacer(2);
    const columns = w.addStack();
    columns.layoutHorizontally();
    columns.topAlignContent();

    const left = columns.addStack();
    left.layoutVertically();
    const leftLabel = left.addText("待截止");
    leftLabel.font = Font.boldSystemFont(10);
    leftLabel.textColor = Color.gray();
    left.addSpacer(2);
    const leftItems = ordered.slice(0, 3);
    if (!leftItems.length) emptyState(left, "无");
    else leftItems.forEach((i) => { taskRow(left, i, { compact: true }); left.addSpacer(2); });

    columns.addSpacer(10);

    const right = columns.addStack();
    right.layoutVertically();
    const rightLabel = right.addText("待排序");
    rightLabel.font = Font.boldSystemFont(10);
    rightLabel.textColor = Color.gray();
    right.addSpacer(2);
    const rightItems = unordered.slice(0, 3);
    if (!rightItems.length) emptyState(right, "无");
    else rightItems.forEach((i) => { taskRow(right, i, { compact: true }); right.addSpacer(2); });
  } else {
    // 大尺寸：有序 5 条 + 无序 3 条
    w.addSpacer(2);
    const label = w.addText("待截止");
    label.font = Font.boldSystemFont(11);
    label.textColor = Color.gray();
    w.addSpacer(2);
    const orderedItems = ordered.slice(0, 5);
    if (!orderedItems.length) emptyState(w, "没有带截止时间的任务");
    else {
      for (const item of orderedItems) {
        taskRow(w, item, { compact: false });
        w.addSpacer(2);
      }
    }

    w.addSpacer(6);
    const label2 = w.addText("待排序");
    label2.font = Font.boldSystemFont(11);
    label2.textColor = Color.gray();
    w.addSpacer(2);
    const unorderedItems = unordered.slice(0, 3);
    if (!unorderedItems.length) emptyState(w, "没有按优先级排的任务");
    else {
      for (const item of unorderedItems) {
        taskRow(w, item, { compact: false });
        w.addSpacer(2);
      }
    }
  }

  footer(w, {
    ordered: payload.ordered?.counts?.ordered ?? ordered.length,
    unordered: payload.unordered?.counts?.unordered ?? unordered.length,
  }, stale ? null : payload.fetchedAt);

  // 15 分钟后再刷新。iOS 不保证这个时间点，只是"不早于"。
  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
  return w;
}

// ─────────────────────────────────────────────────────────────
// 入口
// ─────────────────────────────────────────────────────────────
let widget;
if (!CFG.apiKey) {
  // 没配 Key 时给出**可操作的**提示，而不是一个空白小组件
  const w = new ListWidget();
  w.setPadding(12, 12, 12, 12);
  const title = w.addText("ScheduleKit 未配置");
  title.font = Font.boldSystemFont(13);
  w.addSpacer(6);
  const hint = w.addText(
    "长按小组件 → 编辑小组件 → Parameter 填：\n" +
    "https://canisa1ph.duckdns.org:8443|sk_你的只读Key"
  );
  hint.font = Font.systemFont(11);
  hint.textColor = Color.gray();
  widget = w;
} else {
  try {
    const payload = await loadAll();
    widget = render(payload, false);
  } catch (error) {
    // 失败时用缓存，并在标题栏标"离线"——让用户知道看到的是旧数据
    const cached = readCache();
    if (cached) {
      widget = render(cached, true);
    } else {
      const w = new ListWidget();
      w.setPadding(12, 12, 12, 12);
      const title = w.addText("ScheduleKit");
      title.font = Font.boldSystemFont(13);
      w.addSpacer(4);
      const err = w.addText("加载失败：" + (error.message || String(error)));
      err.font = Font.systemFont(11);
      err.textColor = Color.red();
      w.addSpacer(4);
      const hint = w.addText("检查网络，以及 Key 是否被吊销。");
      hint.font = Font.systemFont(10);
      hint.textColor = Color.gray();
      widget = w;
    }
  }
}

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  // 在 Scriptable 里直接运行时展示预览，方便调样式
  await widget.presentMedium();
}
Script.complete();
