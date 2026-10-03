// ScheduleKit Scriptable 小组件
//
// 安装：
//   1. 安装 Scriptable（App Store，免费）
//   2. 网页控制台 → 设置 → API Key → 新建，勾「只读」
//   3. 复制本文件内容到 Scriptable 新建的脚本里，命名如 ScheduleKit
//   4. 长按主屏/锁屏 → 添加小组件 → Scriptable → 选本脚本
//   5. **长按小组件 → 编辑小组件 → Parameter 填：baseUrl|apiKey**
//      例如： https://canisa1ph.duckdns.org:8443|sk_xxxxxxxx
//
// 为什么把凭据放在「Parameter」而不是写进代码：
//   * 脚本内容可能被分享/备份/贴到别处，写死就等于泄漏；
//   * Parameter 存在系统的小组件配置里，不会随脚本走；
//   * 也可以只填地址，Key 从 Keychain 读（见 buildConfig）。
//
// 为什么建议用**只读** Key：小组件只需要 GET。只读 Key 泄了也改不了任务，
// 写权限留给快捷指令。
//
// ─────────────────────────────────────────────────────────────
// 支持的尺寸族
//   主屏：small / medium / large
//   锁屏：accessoryCircular / accessoryRectangular / accessoryInline
//
// **锁屏三族是独立实现，不是把主屏布局缩小。** 它们的要求完全不同：
//   * 空间极小（矩形大约只有两行，圆形四角会被裁掉）
//   * 系统会渲染成单色（染色/材质），所以颜色基本无效，状态要靠形状与文字
//   * 颜色要用 `Color.white()`；主屏那种 `Color.dynamic()` 在锁屏上没有意义
//   * `accessoryInline` 只接受**一行纯文本**，不能加 stack

// ─────────────────────────────────────────────────────────────
// 配置
// ─────────────────────────────────────────────────────────────
const CACHE_KEY = "schedulekit_widget_cache";
const KEYCHAIN_KEY = "schedulekit_key";

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
  } else {
    baseUrl = "https://canisa1ph.duckdns.org:8443";
  }

  if (!apiKey && Keychain.contains(KEYCHAIN_KEY)) {
    apiKey = Keychain.get(KEYCHAIN_KEY);
  }

  return { baseUrl: baseUrl.replace(/\/+$/, ""), apiKey };
}

const CFG = buildConfig();

// 缓存上一次成功的数据：网络失败时至少能看到"上次是什么"，
// 而不是一个空白组件。手机在电梯/地铁里尤其重要。
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

async function loadAll() {
  const results = await Promise.all([
    api("/api/tasks?view=ordered"),
    api("/api/tasks?view=unordered"),
    api("/api/status"),
  ]);
  const payload = {
    ordered: results[0],
    unordered: results[1],
    status: results[2],
    fetchedAt: new Date().toISOString(),
  };
  writeCache(payload);
  return payload;
}

// ─────────────────────────────────────────────────────────────
// 文案
//
// 一条原则：**短到不换行**。小组件里换行会挤掉别的行；
// 而截断（`…`）比换行好——至少还能看出"有这么一条"。
// ─────────────────────────────────────────────────────────────
const PRIORITY_LABELS = { 1: "Ⅰ", 2: "Ⅱ", 3: "Ⅲ", 4: "Ⅳ", 5: "Ⅴ" };

/** 倒计时。刻意比服务端的 due_in_human 短（"3天"而不是"3 天后"）。 */
function shortDue(iso) {
  if (!iso) return "";
  const minutes = Math.round((new Date(iso) - new Date()) / 60000);
  const overdue = minutes < 0;
  const abs = Math.abs(minutes);
  let text;
  if (abs < 60) text = abs + "分";
  else if (abs < 60 * 24) text = Math.round(abs / 60) + "时";
  else text = Math.round(abs / 1440) + "天";
  return overdue ? "逾" + text : text;
}

/** "今天 23:59" / "10月4日 23:59"。不是今天才带日期。 */
function whenLabel(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (sameDay) return "今天 " + hh + ":" + mm;
  return d.getMonth() + 1 + "月" + d.getDate() + "日 " + hh + ":" + mm;
}

function isDone(task) {
  return task.status === "done";
}

/** 已完成只看完成时间；未完成才看是否逾期。
 *  否则"逾期后完成"的任务会一直显示成逾期。 */
function dueState(task) {
  if (isDone(task)) return "done";
  if (!task.due_at) return "none";
  const diff = new Date(task.due_at) - new Date();
  if (diff < 0) return "overdue";
  if (diff <= 86400000) return "today";
  return "normal";
}

// ─────────────────────────────────────────────────────────────
// 主屏组件
// ─────────────────────────────────────────────────────────────
function severityColor(task) {
  const state = dueState(task);
  if (state === "overdue") return Color.red();
  if (state === "today") return Color.orange();
  if (task.priority === 1) return Color.red();
  if (task.priority === 2) return Color.orange();
  return null; // 用系统默认色，深浅色模式都好看
}

/**
 * 一行任务。
 *
 * ★ 这里曾经有一个把布局彻底搞坏的 bug：函数创建了 `row` 这个 stack，
 *   却把子元素都加到 `w`（父容器）上。结果是每个"行"的零件被摊平到
 *   整个组件的垂直流里，看起来就是标题、圆点、时间各占一行地错位。
 *   **所有子元素必须加到 `row` 上**，这是横向布局的唯一前提。
 */
function taskRow(host, task, compact) {
  const row = host.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  const marker = row.addText(
    isDone(task) ? "✓" : task.due_at ? "•" : PRIORITY_LABELS[task.priority] || "•"
  );
  marker.font = Font.systemFont(compact ? 11 : 12);
  const color = severityColor(task);
  if (color) marker.textColor = color;
  else if (isDone(task)) marker.textColor = Color.gray();

  row.addSpacer(compact ? 5 : 7);

  const title = row.addText(task.title || "(无标题)");
  title.font = Font.systemFont(compact ? 12 : 13);
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.85;

  // 弹性 spacer 把右侧的倒计时推到行尾
  row.addSpacer();

  const due = shortDue(task.due_at);
  if (due) {
    const tail = row.addText(due);
    tail.font = Font.systemFont(compact ? 10 : 11);
    tail.textColor = dueState(task) === "overdue" ? Color.red() : Color.gray();
    tail.lineLimit = 1;
  }
  return row;
}

function sectionLabel(host, text, count) {
  const row = host.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  const label = row.addText(text);
  label.font = Font.boldSystemFont(11);
  label.textColor = Color.gray();
  if (typeof count === "number") {
    row.addSpacer(5);
    const badge = row.addText(String(count));
    badge.font = Font.systemFont(10);
    badge.textColor = Color.gray();
  }
  row.addSpacer();
  return row;
}

function headerStack(w, status, stale) {
  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  const title = row.addText("ScheduleKit");
  title.font = Font.boldSystemFont(13);

  row.addSpacer(6);

  const week = status && status.week > 0 ? "第 " + status.week + " 周" : "假期";
  const badge = row.addText(week);
  badge.font = Font.systemFont(11);
  badge.textColor = Color.gray();

  row.addSpacer();

  if (stale) {
    const warn = row.addText("离线");
    warn.font = Font.systemFont(10);
    warn.textColor = Color.orange();
  } else if (status && status.around && status.around.length > 0) {
    // "正在上什么课"比"更新时间"有用得多，所以优先显示它
    const course = status.around[0];
    const live = row.addText(course.relation_label + "：" + course.course_name);
    live.font = Font.systemFont(10);
    live.textColor = Color.blue();
    live.lineLimit = 1;
  }
  return row;
}

/** 学期周数与配置不符时提示一次——否则课表上下文会静默不准。 */
function alertBanner(w, status) {
  const warnings = (status && status.warnings) || [];
  if (!warnings.length) return;
  const text = w.addText("⚠ " + warnings[0]);
  text.font = Font.systemFont(10);
  text.textColor = Color.orange();
  text.lineLimit = 2;
}

function renderHome(payload, stale) {
  const w = new ListWidget();
  w.setPadding(12, 14, 12, 14);
  w.backgroundColor = Color.dynamic(new Color("#ffffff", 1), new Color("#1c1e21", 1));

  // 注意：**不要**在这里调 `w.layoutVertically()`。
  // Scriptable 的 `layoutVertically()` / `layoutHorizontally()` 只属于
  // **stack 元素**（`addStack()` 的返回值），ListWidget 上没有这两个方法——
  // 调了会在真机上直接抛
  //   TypeError: w.layoutVertically is not a function
  // 而且 ListWidget 的内容本来就是自上而下排列的，本来也不需要声明。
  // （假 API 里我给 ListWidget 也提供了这两个方法，所以测试没抓到，
  //   这是 harness 与真机的差异，见 docs/widget.md。）

  const status = payload.status || {};
  const ordered = (payload.ordered && payload.ordered.items) || [];
  const unordered = (payload.unordered && payload.unordered.items) || [];
  const family = config.widgetFamily || "medium";

  headerStack(w, status, stale);

  if (family === "small") {
    // 小尺寸：优先带有截止时间的任务（那是会过期的东西）
    w.addSpacer(5);
    const items = (ordered.length ? ordered : unordered).slice(0, 3);
    if (!items.length) {
      w.addSpacer();
      const empty = w.addText("没有待办");
      empty.font = Font.systemFont(12);
      empty.textColor = Color.gray();
      w.addSpacer();
    } else {
      items.forEach(function (task, i) {
        taskRow(w, task, true);
        if (i < items.length - 1) w.addSpacer(3);
      });
      w.addSpacer();
    }
  } else if (family === "large") {
    w.addSpacer(6);
    sectionLabel(w, "待截止", ordered.length);
    w.addSpacer(3);
    if (!ordered.length) {
      const none = w.addText("没有带截止时间的任务");
      none.font = Font.systemFont(11);
      none.textColor = Color.gray();
    } else {
      const shown = ordered.slice(0, 6);
      shown.forEach(function (task, i) {
        taskRow(w, task, false);
        if (i < shown.length - 1) w.addSpacer(3);
      });
    }

    w.addSpacer(9);
    sectionLabel(w, "待排序", unordered.length);
    w.addSpacer(3);
    if (!unordered.length) {
      const none = w.addText("没有按优先级排的任务");
      none.font = Font.systemFont(11);
      none.textColor = Color.gray();
    } else {
      const shown = unordered.slice(0, 4);
      shown.forEach(function (task, i) {
        taskRow(w, task, false);
        if (i < shown.length - 1) w.addSpacer(3);
      });
    }
    w.addSpacer();
    alertBanner(w, status);
  } else {
    // medium（默认）：左右两栏
    w.addSpacer(6);
    const columns = w.addStack();
    columns.layoutHorizontally();
    columns.topAlignContent();

    const left = columns.addStack();
    left.layoutVertically();
    sectionLabel(left, "待截止", ordered.length);
    left.addSpacer(3);
    if (!ordered.length) {
      const none = left.addText("无");
      none.font = Font.systemFont(11);
      none.textColor = Color.gray();
    } else {
      const shown = ordered.slice(0, 3);
      shown.forEach(function (task, i) {
        taskRow(left, task, true);
        if (i < shown.length - 1) left.addSpacer(3);
      });
    }
    left.addSpacer();

    columns.addSpacer(12);

    const right = columns.addStack();
    right.layoutVertically();
    sectionLabel(right, "待排序", unordered.length);
    right.addSpacer(3);
    if (!unordered.length) {
      const none = right.addText("无");
      none.font = Font.systemFont(11);
      none.textColor = Color.gray();
    } else {
      const shown = unordered.slice(0, 3);
      shown.forEach(function (task, i) {
        taskRow(right, task, true);
        if (i < shown.length - 1) right.addSpacer(3);
      });
    }
    right.addSpacer();

    w.addSpacer();
    alertBanner(w, status);
  }

  if (!stale && payload.fetchedAt) {
    const stamp = w.addText("更新于 " + new Date(payload.fetchedAt).toLocaleTimeString());
    stamp.font = Font.systemFont(9);
    stamp.textColor = Color.gray();
  }

  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
  return w;
}

// ─────────────────────────────────────────────────────────────
// 锁屏组件（accessory）
// ─────────────────────────────────────────────────────────────
function renderAccessory(payload, family, stale) {
  const w = new ListWidget();
  const status = payload.status || {};
  const ordered = (payload.ordered && payload.ordered.items) || [];
  const unordered = (payload.unordered && payload.unordered.items) || [];
  const first = ordered[0] || unordered[0];

  if (family === "accessoryInline") {
    // 只能有**一行文本**，不能加 stack、不要 setPadding、不要换行。
    //
    // 注意必须把文本挂到 ListWidget 上再返回 widget —— `w.addText(...)`
    // 返回的是**文本元素**，直接把它当 widget 返回给 Script.setWidget()
    // 在真机上是错的（一个元素不是组件）。
    if (!first) {
      w.addText(stale ? "离线" : "没有待办");
      return w;
    }
    const due = shortDue(first.due_at);
    w.addText(first.title + (due ? " · " + due : ""));
    return w;
  }

  // 锁屏是单色渲染，用白色；间距也要收紧。
  // 不要在这里调 `w.layoutVertically()` / `w.centerAlignContent()` ——
  // 它们是 stack 专属方法，ListWidget 上没有（真机会抛 TypeError）。
  w.setPadding(0, 2, 0, 2);

  if (family === "accessoryCircular") {
    if (!first) {
      const none = w.addText(stale ? "离线" : "无");
      none.font = Font.systemFont(12);
      none.textColor = Color.white();
      return w;
    }
    // 圆里只放得下两行：倒计时 + 极短标题
    const due = shortDue(first.due_at);
    if (due) {
      const top = w.addText(due);
      top.font = Font.boldSystemFont(14);
      top.textColor = Color.white();
      top.centerAlignText();
    }
    const title = w.addText(first.title);
    title.font = Font.systemFont(10);
    title.textColor = Color.white();
    title.lineLimit = 1;
    title.minimumScaleFactor = 0.7;
    title.centerAlignText();
    return w;
  }

  // accessoryRectangular：空/错误态
  if (!first) {
    const none = w.addText(stale ? "离线，无缓存" : "没有待办");
    none.font = Font.systemFont(12);
    none.textColor = Color.white();
    return w;
  }

  // accessoryRectangular：锁屏上大约只有三行，所以按两行排：
  //   第 1 行  标记 + 标题 + 倒计时   ← 倒计时并到标题行，避免多占一行
  //   第 2 行  具体时间 …… 右侧 另 N 条
  //
  // 一开始写成了"标题一行 / 时间一行 / 还有几条一行"，加上标记那行就变成 4 行，
  // 会溢出被系统裁掉。这是靠 widget-assert 的行数断言发现的。
  const titleRow = w.addStack();
  titleRow.layoutHorizontally();
  titleRow.centerAlignContent();
  const mark = titleRow.addText(
    first.due_at ? "•" : PRIORITY_LABELS[first.priority] || "•"
  );
  mark.font = Font.systemFont(11);
  mark.textColor = Color.white();
  titleRow.addSpacer(4);
  const title = titleRow.addText(first.title);
  title.font = Font.boldSystemFont(12);
  title.textColor = Color.white();
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.8;
  titleRow.addSpacer(4);
  const dueShort = shortDue(first.due_at);
  if (dueShort) {
    const tail = titleRow.addText(dueShort);
    tail.font = Font.systemFont(10);
    tail.textColor = Color.white();
    tail.lineLimit = 1;
  }

  w.addSpacer(1);

  const metaRow = w.addStack();
  metaRow.layoutHorizontally();
  const rest = ordered.length + unordered.length - 1;
  const metaText = first.due_at
    ? whenLabel(first.due_at)
    : "优先级 " + (PRIORITY_LABELS[first.priority] || "—");
  const meta = metaRow.addText(metaText);
  meta.font = Font.systemFont(10);
  meta.textColor = Color.white();
  meta.lineLimit = 1;
  metaRow.addSpacer();
  // 剩余条数合并到第二行右侧：锁屏上多占一行就会被裁。
  // 措辞用"另 N 条"而不是"还有 N 条"——后者容易被读成"这条任务还有 N 个子项"。
  if (rest > 0) {
    const more = metaRow.addText("另 " + rest + " 条");
    more.font = Font.systemFont(9);
    more.textColor = Color.white();
    more.lineLimit = 1;
  }
  return w;
}

/** 锁屏上的空/错误态：同样只能极短。 */
function accessoryEmpty(text) {
  const w = new ListWidget();
  if (FAMILY === "accessoryInline") {
    w.addText(text);
    return w;
  }
  // 居中靠文本自身的 centerAlignText()，不要用 stack 的 centerAlignContent()
  w.setPadding(0, 2, 0, 2);
  const t = w.addText(text);
  t.font = Font.systemFont(11);
  t.textColor = Color.white();
  t.centerAlignText();
  return w;
}

// ─────────────────────────────────────────────────────────────
// 主屏的提示页（没配 Key / 加载失败且无缓存）
// ─────────────────────────────────────────────────────────────
function messageWidget(title, lines, isError) {
  const w = new ListWidget();
  w.setPadding(12, 14, 12, 14);
  // 不调 w.layoutVertically()：ListWidget 本来就没有这个方法，
  // 它的内容天然自上而下排列。

  const head = w.addText(title);
  head.font = Font.boldSystemFont(13);
  if (isError) head.textColor = Color.red();

  w.addSpacer(5);
  lines.forEach(function (line) {
    const text = w.addText(line);
    text.font = Font.systemFont(11);
    text.textColor = Color.gray();
    text.lineLimit = 3;
  });
  w.addSpacer();
  return w;
}

// ─────────────────────────────────────────────────────────────
// 入口
// ─────────────────────────────────────────────────────────────
const FAMILY = config.widgetFamily || "medium";
const IS_ACCESSORY = FAMILY.indexOf("accessory") === 0;
// 锁屏上 runsInAccessoryWidget 为真；主屏是 runsInWidget
const IN_WIDGET = config.runsInWidget || config.runsInAccessoryWidget;

let widget;

if (!CFG.apiKey) {
  widget = IS_ACCESSORY
    ? accessoryEmpty("未配置")
    : messageWidget("ScheduleKit 未配置", [
        "长按小组件 → 编辑小组件 → Parameter 里填：",
        CFG.baseUrl + "|sk_你的只读Key",
        "（中间是一个竖线）",
      ], false);
} else {
  try {
    const payload = await loadAll();
    widget = IS_ACCESSORY
      ? renderAccessory(payload, FAMILY, false)
      : renderHome(payload, false);
  } catch (error) {
    // 失败时用缓存并标"离线"——让用户知道看到的是旧数据
    const cached = readCache();
    if (cached) {
      widget = IS_ACCESSORY
        ? renderAccessory(cached, FAMILY, true)
        : renderHome(cached, true);
    } else {
      widget = IS_ACCESSORY
        ? accessoryEmpty("加载失败")
        : messageWidget(
            "加载失败",
            [String(error.message || error), "检查手机网络，以及 Key 是否被吊销。"],
            true
          );
    }
  }
}

if (IN_WIDGET) {
  Script.setWidget(widget);
} else {
  // 在 Scriptable 里直接运行时给预览，方便调样式。
  // 按**当前实际尺寸族**选择预览方式，否则调锁屏样式时看不到真实效果。
  if (IS_ACCESSORY) await widget.presentAccessoryRectangular();
  else if (FAMILY === "small") await widget.presentSmall();
  else if (FAMILY === "large") await widget.presentLarge();
  else await widget.presentMedium();
}
Script.complete();
