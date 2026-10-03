<script setup lang="ts">
/**
 * 课表页：整周网格 + 课程编辑。
 *
 * **编辑是"整体替换"**（`PUT /api/timetable`），不是逐条增删改。
 * 课表本身就是"一整张周表"，改完一次提交最符合直觉；
 * 这同时消除了"删了两门、改了三门"的部分失败中间态。
 *
 * 网格用 CSS Grid 的 `grid-row` 按时间定位，而不是画一堆空单元格：
 * 用时间算行号，课程块的纵向位置就自然反映它的上课时间，
 * 空白时段也不会占用 DOM。
 */
import { computed, onMounted, ref } from 'vue'
import { api, type Timetable } from '@/api/client'
import { notify, report } from '@/state/store'

const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

/** 网格的起止时间与粒度。8:00–22:00 / 30 分钟一格是大学课表的常见范围。 */
const DAY_START = 8
const DAY_END = 22
const SLOT_MINUTES = 30
const SLOTS = ((DAY_END - DAY_START) * 60) / SLOT_MINUTES

interface EditorSession {
  weekday: number
  start_time: string
  end_time: string
  weeks: string
  location: string
}

interface EditorCourse {
  name: string
  teacher: string
  location: string
  color: string
  note: string
  sessions: EditorSession[]
}

const timetable = ref<Timetable | null>(null)
const loading = ref(true)
const saving = ref(false)
const editing = ref(false)
const editor = ref<EditorCourse[]>([])

const term = computed(() => timetable.value?.term ?? { start_date: '', total_weeks: 0, current_week: 0 })

/** 把 "HH:MM" 转成从网格顶部开始的 30 分钟格数 */
function slotOf(time: string): number {
  const [hh, mm] = time.split(':').map(Number)
  const minutes = (hh ?? 0) * 60 + (mm ?? 0) - DAY_START * 60
  return Math.max(0, Math.round(minutes / SLOT_MINUTES))
}

function durationSlots(start: string, end: string): number {
  return Math.max(1, slotOf(end) - slotOf(start))
}

/** 某个 weekday 的课程（来自 matrix，服务端已按开始时间排好） */
function dayEntries(weekdayIndex: number): Array<Record<string, unknown>> {
  const key = WEEKDAYS[weekdayIndex]
  const rows = (key && timetable.value?.matrix?.[key]) || []
  return rows as Array<Record<string, unknown>>
}

/**
 * 某个 weekday 要渲染的块（**扁平列表**）。
 *
 * 刻意不返回"行"的嵌套结构：块的定位是相对 `.grid__col` 的绝对定位
 * （top 由时间算出），中间多套一层容器就会让 top 相对错的对象，
 * 整列的位置全错。所以这里把重叠组的布局信息拍平到每个块上。
 */
function dayBlocks(weekdayIndex: number): Array<{
  entry: Record<string, unknown>
  style: Record<string, string>
  badge: string
  /** 并排时才为 true：窄栏里放不下时间与教室 */
  split: boolean
}> {
  const entries = dayEntries(weekdayIndex)
  if (!entries.length) return []
  const total = term.value.total_weeks || 16
  const out: Array<{
    entry: Record<string, unknown>
    style: Record<string, string>
    badge: string
    split: boolean
  }> = []
  for (const group of overlapGroups(entries)) {
    const { styles, badges } = layoutColumns(group, total)
    group.forEach((entry, i) => {
      out.push({
        entry,
        style: styles[i] ?? {},
        badge: badges[i] ?? '',
        split: group.length > 1,
      })
    })
  }
  return out
}

const toMinutes = (t: string): number => {
  const [h, m] = t.split(':').map(Number)
  return (h ?? 0) * 60 + (m ?? 0)
}

/**
 * 一天里**时间重叠**的时段分组。
 *
 * 为什么需要：网格块是按时间绝对定位的（top/height 只由 start/end 决定），
 * 所以同一时间段的两个时段会算出完全相同的坐标、直接叠在一起，后者盖住前者。
 * 而"同一时间、不同周次去不同教室"在国内大学很常见（例：周三 13:25
 * 1-8 周在 A 教室、9-16 周在 B 教室）。
 *
 * 分组用扫描线：按开始时间排序后，只要下一段的开始时间早于**当前组的
 * 最晚结束时间**，就并进同一组（这样能正确处理链式重叠 A-B、B-C）。
 */
function overlapGroups(entries: Array<Record<string, unknown>>): Array<Array<Record<string, unknown>>> {
  const sorted = [...entries].sort(
    (a, b) => toMinutes(String(a.start_time)) - toMinutes(String(b.start_time)),
  )
  const groups: Array<Array<Record<string, unknown>>> = []
  let current: Array<Record<string, unknown>> = []
  let groupEnd = -1

  for (const entry of sorted) {
    const start = toMinutes(String(entry.start_time))
    if (current.length && start >= groupEnd) {
      groups.push(current)
      current = []
      groupEnd = -1
    }
    current.push(entry)
    groupEnd = Math.max(groupEnd, toMinutes(String(entry.end_time)))
  }
  if (current.length) groups.push(current)
  return groups
}

/** 把周次串（"1-8" / "1,3,5" / "2-8,10"）展开成周次集合。留空表示全周。 */
function weeksToSet(spec: string, total: number): Set<number> {
  const raw = (spec || '').trim()
  const out = new Set<number>()
  if (!raw) {
    for (let w = 1; w <= total; w += 1) out.add(w)
    return out
  }
  for (const part of raw.split(',')) {
    const piece = part.trim()
    if (!piece) continue
    if (piece.includes('-')) {
      const parts = piece.split('-').map((x) => Number(x.trim()))
      const a = parts[0]
      const b = parts[1]
      if (a === undefined || b === undefined) continue
      if (!Number.isFinite(a) || !Number.isFinite(b)) continue
      for (let w = a; w <= b; w += 1) out.add(w)
    } else {
      const n = Number(piece)
      if (Number.isFinite(n)) out.add(n)
    }
  }
  return out
}

/**
 * 给重叠组里的每一块算出定位样式与要显示的周次提示。
 *
 * 周次提示的策略：**只在重叠时才显示**。单块占满整列时不显示周次
 * （那是噪音，正常课表也不会写）；一旦并排，就必须能区分
 * "这几周上 A、那几周上 B"，否则并排只是把两块挤窄而已。
 */
function layoutColumns(
  group: Array<Record<string, unknown>>,
  totalWeeks: number,
): {
  styles: Array<Record<string, string>>
  badges: Array<string>
} {
  const n = group.length
  const styles = group.map((_, i) => {
    const pct = 100 / n
    return {
      left: `${i * pct}%`,
      // 减去 1.5% 留出缝隙，两块之间才看得出分界
      width: `calc(${pct}% - ${(i === n - 1 ? 0 : 1.5).toFixed(2)}%)`,
    }
  })

  // 组内每个周次分别归谁
  const owner = new Map<number, Array<Record<string, unknown>>>()
  for (const entry of group) {
    for (const w of weeksToSet(String(entry.weeks ?? ''), totalWeeks)) {
      const list = owner.get(w) ?? []
      list.push(entry)
      owner.set(w, list)
    }
  }

  const badges = group.map((entry) => {
    const mine = weeksToSet(String(entry.weeks ?? ''), totalWeeks)
    // 只保留"这些周只有我在上"的周次
    const exclusive: number[] = []
    for (const w of [...mine].sort((a, b) => a - b)) {
      if ((owner.get(w) ?? []).length === 1) exclusive.push(w)
    }
    const ranges: string[] = []
    for (const w of exclusive) {
      const last = ranges[ranges.length - 1]
      if (!last) {
        ranges.push(String(w))
      } else if (last.includes('-')) {
        const [, end] = last.split('-')
        if (Number(end) + 1 === w) ranges[ranges.length - 1] = `${last.split('-')[0]}-${w}`
        else ranges.push(String(w))
      } else if (Number(last) + 1 === w) {
        ranges[ranges.length - 1] = `${last}-${w}`
      } else {
        ranges.push(String(w))
      }
    }
    return ranges.join(',')
  })

  return { styles, badges }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    timetable.value = await api.timetable()
  } catch (error) {
    report(error)
  } finally {
    loading.value = false
  }
}

function startEdit(): void {
  const courses = timetable.value?.courses ?? []
  // 拷贝一份，取消时直接丢弃
  editor.value = courses.map((course) => ({
    name: course.name,
    teacher: course.teacher,
    location: course.location,
    color: course.color,
    note: course.note,
    sessions: course.sessions.map((session) => ({
      weekday: session.weekday,
      start_time: session.start_time,
      end_time: session.end_time,
      weeks: session.weeks,
      location: session.location,
    })),
  }))
  editing.value = true
}

function addCourse(): void {
  editor.value.push({
    name: '',
    teacher: '',
    location: '',
    color: '',
    note: '',
    sessions: [{ weekday: 1, start_time: '08:00', end_time: '09:40', weeks: '1-16', location: '' }],
  })
}

function removeCourse(index: number): void {
  editor.value.splice(index, 1)
}

function addSession(course: EditorCourse): void {
  const last = course.sessions[course.sessions.length - 1]
  course.sessions.push({
    weekday: last?.weekday ?? 1,
    start_time: last?.end_time ?? '08:00',
    end_time: last ? bumpHour(last.end_time) : '09:40',
    weeks: last?.weeks ?? '1-16',
    location: '',
  })
}

function bumpHour(time: string): string {
  const [hh, mm] = time.split(':').map(Number)
  const next = Math.min(DAY_END, (hh ?? 8) + 2)
  return `${String(next).padStart(2, '0')}:${String(mm ?? 0).padStart(2, '0')}`
}

function removeSession(course: EditorCourse, index: number): void {
  course.sessions.splice(index, 1)
}

async function save(): Promise<void> {
  saving.value = true
  try {
    const payload = editor.value
      .filter((course) => course.name.trim())
      .map((course) => ({
        name: course.name.trim(),
        teacher: course.teacher,
        location: course.location,
        color: course.color,
        note: course.note,
        sessions: course.sessions.map((session) => ({
          weekday: session.weekday,
          start_time: session.start_time,
          end_time: session.end_time,
          weeks: session.weeks,
          location: session.location,
        })),
      }))
    timetable.value = await api.saveTimetable(payload)
    editing.value = false
    notify('课表已保存', 'ok')
  } catch (error) {
    report(error)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="courses">
    <header class="courses__top">
      <div>
        <h2 class="courses__title">课表</h2>
        <p class="courses__term">
          <template v-if="term.current_week > 0">
            第 {{ term.current_week }} 周 / 共 {{ term.total_weeks }} 周 · 起始 {{ term.start_date }}
          </template>
          <template v-else-if="term.start_date">
            按起始日 {{ term.start_date }} 计算，当前不在学期内
          </template>
          <template v-else>尚未设置学期起始日</template>
        </p>
      </div>
      <button v-if="!editing" class="courses__btn" type="button" @click="startEdit">编辑</button>
    </header>

    <p v-if="!editing" class="courses__hint">
      课表会作为上下文注入识别提示词，让「下节课交」「这门课」这类说法能被解析成具体日期。
    </p>

    <div v-if="loading" class="sk-muted courses__loading">加载中…</div>

    <!-- ── 只读：周视图网格 ─────────────────────────────────── -->
    <div v-else-if="!editing" class="grid-wrap">
      <div v-if="!(timetable?.courses?.length)" class="empty">
        <div class="empty__mark" aria-hidden="true">▦</div>
        <h3>还没有课程</h3>
        <p>点右上角「编辑」添加。有了课表，识别时才能把「下节课」解析成具体日期。</p>
      </div>

      <div v-else class="grid">
        <!-- 时间轴 -->
        <div class="grid__times">
          <div
            v-for="slot in SLOTS"
            :key="slot"
            class="grid__time"
            :class="{ 'grid__time--hour': (slot - 1) % 2 === 0 }"
          >
            <span v-if="(slot - 1) % 2 === 0">
              {{ String(DAY_START + (slot - 1) / 2).padStart(2, '0') }}:00
            </span>
          </div>
        </div>

        <!-- 七列 -->
        <div v-for="(day, dayIndex) in WEEKDAYS" :key="day" class="grid__day">
          <div class="grid__head">{{ day }}</div>
          <div class="grid__col" :style="{ height: `${SLOTS * 22}px` }">
            <div v-for="slot in SLOTS" :key="slot" class="grid__cell" aria-hidden="true" />
            <!-- 时间重叠的块并排显示（left/width 由 layoutColumns 算出）。
                 直接挂在这一层：块的 top 是相对 .grid__col 的绝对定位，
                 中间再套容器会让 top 相对错的对象。 -->
            <div
              v-for="(block, index) in dayBlocks(dayIndex)"
              :key="index"
              class="block"
              :class="{ 'block--split': block.split }"
              :data-color="block.entry.color || ''"
              :style="{
                top: `${slotOf(String(block.entry.start_time)) * 22}px`,
                height: `${durationSlots(String(block.entry.start_time), String(block.entry.end_time)) * 22 - 2}px`,
                left: block.style.left,
                width: block.style.width,
              }"
              :title="`${block.entry.course_name} ${block.entry.start_time}–${block.entry.end_time}（第 ${block.entry.weeks || '全部'} 周）${block.entry.location ? ' @ ' + block.entry.location : ''}`"
            >
              <span class="block__name">{{ block.entry.course_name }}</span>
              <!-- 周次只在并排时显示：单块占满整列时它是噪音 -->
              <span v-if="block.badge" class="block__weeks">{{ block.badge }}周</span>
              <span v-if="!block.split" class="block__time">
                {{ block.entry.start_time }}–{{ block.entry.end_time }}
              </span>
              <span v-if="block.entry.location && !block.split" class="block__where">
                {{ block.entry.location }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ── 编辑：整体替换 ───────────────────────────────────── -->
    <div v-else class="editor">
      <article v-for="(course, courseIndex) in editor" :key="courseIndex" class="course">
        <div class="course__head">
          <input
            v-model="course.name"
            class="sk-input course__name"
            type="text"
            maxlength="60"
            placeholder="课程名，例如：电子电路基础"
          />
          <button
            class="course__remove"
            type="button"
            aria-label="删除课程"
            @click="removeCourse(courseIndex)"
          >
            ✕
          </button>
        </div>

        <div class="course__meta">
          <input v-model="course.teacher" class="sk-input sk-input--sm" placeholder="教师" />
          <input v-model="course.location" class="sk-input sk-input--sm" placeholder="地点" />
        </div>

        <div class="sessions">
          <div v-for="(session, sessionIndex) in course.sessions" :key="sessionIndex" class="session">
            <select v-model.number="session.weekday" class="sk-input sk-input--sm session__day">
              <option v-for="(day, i) in WEEKDAYS" :key="day" :value="i + 1">{{ day }}</option>
            </select>
            <input v-model="session.start_time" class="sk-input sk-input--sm session__time" type="time" />
            <span class="session__dash">–</span>
            <input v-model="session.end_time" class="sk-input sk-input--sm session__time" type="time" />
            <input
              v-model="session.weeks"
              class="sk-input sk-input--sm session__weeks"
              placeholder="周次 如 1-16"
            />
            <button
              class="session__remove"
              type="button"
              aria-label="删除时段"
              @click="removeSession(course, sessionIndex)"
            >
              ✕
            </button>
          </div>
          <button class="session__add" type="button" @click="addSession(course)">＋ 加时段</button>
        </div>
      </article>

      <button class="editor__add" type="button" @click="addCourse">＋ 添加课程</button>

      <p class="editor__note">
        周次留空表示全周生效。支持 <code>1-16</code>、<code>1,3,5</code>、<code>2-8,10</code> 三种写法；
        写错了不会报错，会按全周处理并记一条日志。
      </p>

      <div class="editor__actions">
        <button class="sk-btn sk-btn--ghost" type="button" @click="editing = false">取消</button>
        <button class="sk-btn" type="button" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : '保存课表' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.courses {
  padding: 14px 12px 0;
}

.courses__top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}

.courses__title {
  margin: 0;
  font-size: 16px;
}

.courses__term {
  margin: 3px 0 0;
  font-size: 11px;
  color: var(--text-tertiary);
}

.courses__btn {
  flex: 0 0 auto;
  padding: 7px 14px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 12px;
  font-weight: 600;
}

.courses__hint {
  margin: 0 0 14px;
  font-size: 11px;
  line-height: 1.65;
  color: var(--text-tertiary);
}

.courses__loading {
  padding: 40px 0;
  text-align: center;
  font-size: 13px;
}

/* ── 周视图网格 ─────────────────────────────────────────────── */
/* 横向可滚动：手机屏幕放不下 7 列，硬塞会让每列窄到看不清课程名。
   横向滚动比缩字号更好用。 */
.grid-wrap {
  overflow-x: auto;
  padding-bottom: 8px;
  -webkit-overflow-scrolling: touch;
}

.grid {
  display: grid;
  grid-template-columns: 42px repeat(7, minmax(84px, 1fr));
  gap: 0 4px;
  min-width: 660px;
}

.grid__times {
  display: flex;
  flex-direction: column;
  padding-top: 26px;
}

.grid__time {
  height: 22px;
  font-size: 9px;
  color: var(--text-tertiary);
  text-align: right;
  padding-right: 2px;
  font-variant-numeric: tabular-nums;
}

.grid__day {
  min-width: 0;
}

.grid__head {
  height: 26px;
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
}

.grid__col {
  position: relative;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  overflow: hidden;
}

/* 半小时一格的横线。用 repeating-linear-gradient 比画 28 个空 div 省 DOM */
.grid__cell {
  height: 22px;
  border-bottom: 1px solid var(--border);
}

.grid__cell:nth-child(odd) {
  border-bottom-color: transparent;
}

.block {
  position: absolute;
  /* 不再用 left/right 定宽：并排时 left/width 由 JS 给出（layoutColumns）。
     单块时 JS 给的是 left:0% / width:100%，效果与原来的 left/right: 2px 相同
     而少一层定位假设。 */
  box-sizing: border-box;
  padding: 4px 5px;
  border-radius: 6px;
  background: var(--accent);
  color: var(--bg-elevated);
  display: flex;
  flex-direction: column;
  gap: 1px;
  overflow: hidden;
  transition: transform var(--dur-fast) var(--ease);
}

/* 并排的块：窄栏里只留课名与周次，并加左边线以示分界 */
.block--split {
  padding: 3px 4px;
  border-left: 2px solid rgba(255, 255, 255, 0.55);
}

/* 课程色标。前端的设计令牌里没有 --c-<name> 系列，所以没定义时
   会退回 .block 的默认底色 —— 能区分（靠左边线）但不好看。
   这里把常见色名映射成实际色值，避免"配了颜色却看不出来"。 */
.block[data-color='blue'] { background: #4a6fa5; }
.block[data-color='teal'] { background: #2f7d75; }
.block[data-color='green'] { background: #3f7d4f; }
.block[data-color='lime'] { background: #5c7a33; }
.block[data-color='amber'] { background: #9a6b1f; }
.block[data-color='orange'] { background: #a85f28; }
.block[data-color='rose'] { background: #96505f; }
.block[data-color='violet'] { background: #6a5490; }
.block[data-color='indigo'] { background: #4c5798; }
.block[data-color='cyan'] { background: #2d6d80; }
.block[data-color='slate'] { background: #5a6472; }

.block:active {
  transform: scale(0.97);
}

.block__name {
  font-size: 10px;
  font-weight: 700;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 周次提示：只在并排（时间重叠）时出现，用来区分"这几周上哪门" */
.block__weeks {
  font-size: 8px;
  font-weight: 600;
  opacity: 0.95;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.block__time,
.block__where {
  font-size: 8px;
  opacity: 0.8;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── 编辑器 ─────────────────────────────────────────────────── */
.editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.course {
  padding: 11px 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.course__head {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
  margin-bottom: 8px;
}

.course__name {
  font-weight: 600;
}

.course__remove,
.session__remove {
  width: 30px;
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  font-size: 12px;
}

.course__remove:active,
.session__remove:active {
  background: var(--danger-soft);
  color: var(--danger);
}

.course__meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 10px;
}

.sk-input--sm {
  min-height: 34px;
  padding: 5px 9px;
  font-size: 12px;
}

.sessions {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.session {
  display: grid;
  grid-template-columns: 62px 1fr auto 1fr 1fr auto;
  align-items: center;
  gap: 5px;
}

.session__dash {
  font-size: 11px;
  color: var(--text-tertiary);
  text-align: center;
}

.session__add {
  align-self: flex-start;
  margin-top: 2px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
}

.editor__add {
  padding: 12px;
  border-radius: var(--radius);
  border: 1px dashed var(--border-strong);
  font-size: 13px;
  color: var(--text-secondary);
}

.editor__note {
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--text-tertiary);
}

.editor__note code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--accent-soft);
  font-size: 10px;
}

.editor__actions {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 10px;
}

/* ── 空状态 ─────────────────────────────────────────────────── */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 56px 24px;
  color: var(--text-tertiary);
}

.empty__mark {
  width: 54px;
  height: 54px;
  border-radius: 50%;
  background: var(--accent-soft);
  display: grid;
  place-items: center;
  font-size: 22px;
  margin-bottom: 14px;
}

.empty h3 {
  margin: 0 0 5px;
  font-size: 15px;
  color: var(--text-secondary);
}

.empty p {
  margin: 0;
  font-size: 12px;
  max-width: 300px;
  line-height: 1.6;
}

@media (min-width: 900px) {
  .courses {
    padding: 20px 28px 0;
    max-width: 1080px;
    margin: 0 auto;
  }

  .grid {
    min-width: 0;
  }
}
</style>
