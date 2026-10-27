// 校园 RFID 取件系统 - 前端
// Vue 3 由 index.html 以本地文件引入（frontend/vue.global.prod.js），断网也能正常打开；
// 万一本地文件缺失，index.html 会自动回退到 CDN。
const { createApp, ref, computed, reactive, onMounted, onBeforeUnmount, watch, nextTick } = Vue

// 页面现在由 Flask 托管，和 API 同源（6066 / 5173 都一样），直接用相对路径，
// 不用再跨域去 6066 —— 跨域请求在浏览器里常被代理拦掉，会导致大屏不刷新。
// 只有双击打开的单文件版（file://，origin 为 'null'）才回退到绝对地址。
const _origin = (typeof location !== 'undefined' && location.origin) ? location.origin : ''
const API = (!_origin || _origin === 'null') ? 'http://127.0.0.1:6066' : ''

async function req(path, opts = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...opts }
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body)
  const r = await fetch(API + path, init)
  const text = await r.text()
  if (!r.ok) {
    let msg = text
    try { msg = JSON.parse(text).error || text } catch {}
    throw new Error(msg)
  }
  return text ? JSON.parse(text) : {}
}

async function reqForm(path, formData) {
  const r = await fetch(API + path, { method: 'POST', body: formData })
  const text = await r.text()
  let data = {}
  try { data = text ? JSON.parse(text) : {} } catch { data = { error: text } }
  if (!r.ok) throw new Error(data.error || text)
  return data
}

createApp({
  setup() {
    const tab = ref('pickup')
    const tabs = [
      { key: 'pickup',    label: '取件大屏' },
      { key: 'inbound',   label: '快递录入' },
      { key: 'import',    label: '数据导入' },
      { key: 'students',  label: '学生管理' },
      { key: 'dashboard', label: '统计' },
    ]

    // 读卡器状态
    const readerStatus = reactive({ status: 'connecting', port: null, error: null })
    const readerStatusText = computed(() => ({
      connected: '已连接',
      no_port: '未发现设备',
      silent: '设备无响应',
      error: '连接错误',
      starting: '启动中',
      connecting: '连接中',
    }[readerStatus.status] || readerStatus.status))
    const readerPillClass = computed(() => ({
      connected: 'ok',
      error: 'err',
      silent: 'err',
      no_port: 'warn',
    }[readerStatus.status] || 'idle'))
    // 设备心跳：正常时每轮盘点都会回帧，rx_age 变大说明硬件掉线了
    const readerAlive = computed(() => {
      if (readerStatus.status !== 'connected') return null
      const age = readerStatus.rx_age
      if (age === null || age === undefined) return 'unknown'
      return age < 5 ? 'alive' : 'dead'
    })

    // 取件大屏
    const lastCard = ref(null)
    const cooldownPct = ref(0)
    const cooldownText = ref('')
    let beatTimer = null
    let cooldownTimer = null
    let cooldownStart = 0

    function startCooldown(seconds) {
      cooldownStart = Date.now()
      cooldownPct.value = 100
      const fmt = (s) => {
        const m = Math.floor(s / 60)
        const r = Math.floor(s % 60)
        return `${m}:${String(r).padStart(2, '0')}`
      }
      cooldownText.value = fmt(seconds)
      clearInterval(cooldownTimer)
      cooldownTimer = setInterval(() => {
        const elapsed = (Date.now() - cooldownStart) / 1000
        const left = Math.max(0, seconds - elapsed)
        cooldownPct.value = (left / seconds) * 100
        cooldownText.value = fmt(left)
        if (left <= 0) {
          clearInterval(cooldownTimer)
          cooldownText.value = '0:00'
        }
      }, 100)
    }

    const cardScreenClass = computed(() => {
      if (!lastCard.value) return 'idle'
      return lastCard.value.kind
    })

    // ---------- 新卡绑定 ----------
    const bindForm = reactive({ epc: '', name: '', student_no: '', phone: '', student_id: '' })
    const bindMsg = ref(null)
    const bindBusy = ref(false)
    const unknownEpc = ref('')

    async function loadUnknownCard() {
      try {
        const u = await req('/api/unknown-card')
        unknownEpc.value = u.epc || ''
        if (u.epc && !lastCard.value) {
          lastCard.value = { kind: 'unknown_card', epc: u.epc, message: '这是一张新卡，请填写个人信息完成绑定' }
        }
      } catch {}
    }

    watch(() => lastCard.value && lastCard.value.kind, (k) => {
      if (k === 'unknown_card') {
        bindForm.epc = lastCard.value.epc
        bindForm.name = ''
        bindForm.student_no = ''
        bindForm.phone = ''
        bindForm.student_id = ''
        bindMsg.value = null
        unknownEpc.value = lastCard.value.epc
        nextTick(() => { try { document.getElementById('bind-name') && document.getElementById('bind-name').focus() } catch {} })
      }
    })

    async function submitBind() {
      bindBusy.value = true
      bindMsg.value = null
      try {
        const r = await req('/api/bind-new-card', {
          method: 'POST',
          body: {
            epc: bindForm.epc,
            name: bindForm.name,
            phone: bindForm.phone,
            student_no: bindForm.student_no,
            student_id: bindForm.student_id ? Number(bindForm.student_id) : null,
          },
        })
        bindMsg.value = { ok: true, text: `已绑定到 ${r.student.name}` }
        flashMsg('操作成功')
        unknownEpc.value = ''
        await refreshAfterSuccess(loadStudents, loadPkgs, loadStats, loadCardEvents)
      } catch (e) {
        bindMsg.value = { ok: false, text: friendlyError(e) }
      } finally {
        bindBusy.value = false
      }
    }
    function ignoreUnknown() {
      unknownEpc.value = ''
      lastCard.value = null
    }

    // 录入快递
    const newPkg = reactive({ pickup_code: '', phone: '', tracking_no: '' })
    const pkgMsg = ref(null)
    const packages = ref([])
    const pkgFilter = ref('')

    async function loadPkgs() {
      const q = pkgFilter.value ? `?status=${pkgFilter.value}` : ''
      packages.value = await req('/api/packages' + q)
    }
    const pkgNeedCreate = ref('')   // 手机号未登记时，提示是否新建收件人

    async function submitPkg(autoCreate = false) {
      pkgMsg.value = null
      try {
        if (!newPkg.pickup_code || !newPkg.phone) {
          pkgMsg.value = { ok: false, text: '取件码和手机号必填' }
          return
        }
        const r = await req('/api/packages', {
          method: 'POST',
          body: { ...newPkg, auto_create: !!autoCreate },
        })
        pkgMsg.value = { ok: true, text: `已录入取件码 ${r.pickup_code}，关联到 ${r.student_name}` }
        pkgNeedCreate.value = ''
        flashMsg('操作成功')
        newPkg.pickup_code = ''
        newPkg.tracking_no = ''
        await refreshAfterSuccess(loadPkgs, loadStudents, loadStats)
      } catch (e) {
        pkgMsg.value = { ok: false, text: friendlyError(e) }
        if (/未找到手机号/.test(e.message)) pkgNeedCreate.value = newPkg.phone
      }
    }
    async function pickOne(p) {
      if (!confirm(`确认将取件码 ${p.pickup_code} 标记为已取？`)) return
      try {
        await req(`/api/packages/${p.id}/pick`, { method: 'POST' })
        flashMsg('操作成功')
        await refreshAfterSuccess(loadPkgs, loadStudents, loadStats)
      } catch (e) { flashMsg(friendlyError(e), true) }
    }
    async function restoreOne(p) {
      try {
        await req(`/api/packages/${p.id}/restore`, { method: 'POST' })
        flashMsg('操作成功')
        await refreshAfterSuccess(loadPkgs, loadStudents, loadStats)
      } catch (e) { flashMsg(friendlyError(e), true) }
    }

    // ---------- 数据导入 ----------
    const imp = reactive({
      kind: 'auto',
      file: null,
      filename: '',
      dragging: false,
      busy: false,
      preview: null,     // 预览结果
      summary: null,     // 导入结果
    })
    const impMsg = ref(null)

    function onPickFile(e) {
      const f = e.target.files && e.target.files[0]
      if (f) doPreview(f)
      e.target.value = ''
    }
    function onDrop(e) {
      imp.dragging = false
      const f = e.dataTransfer.files && e.dataTransfer.files[0]
      if (f) doPreview(f)
    }
    async function doPreview(f) {
      imp.busy = true; impMsg.value = null; imp.summary = null
      imp.filename = f.name
      try {
        const fd = new FormData()
        fd.append('file', f)
        fd.append('kind', imp.kind)
        const r = await reqForm('/api/import/preview', fd)
        imp.preview = r
        impMsg.value = {
          ok: true,
          text: `已解析 ${r.total} 行，其中有效 ${r.valid} 行${r.invalid ? '，' + r.invalid + ' 行有问题' : ''}（识别为${r.kind === 'packages' ? '快递信息' : '学生名单'}）`,
        }
      } catch (e) {
        imp.preview = null
        impMsg.value = { ok: false, text: friendlyError(e) }
      } finally {
        imp.busy = false
      }
    }
    async function commitImport() {
      if (!imp.preview) return
      imp.busy = true
      try {
        const r = await req('/api/import/commit', { method: 'POST', body: { token: imp.preview.token } })
        imp.summary = r
        flashMsg('操作成功')
        imp.preview = null
        await refreshAfterSuccess(loadPkgs, loadStudents, loadStats)
      } catch (e) {
        impMsg.value = { ok: false, text: friendlyError(e) }
      } finally {
        imp.busy = false
      }
    }
    function resetImport() {
      imp.preview = null; imp.summary = null; imp.filename = ''; impMsg.value = null
    }
    const kindLabel = computed(() => imp.preview ? (imp.preview.kind === 'packages' ? '快递信息' : '学生名单') : '')
    const previewCols = computed(() => {
      if (!imp.preview || !imp.preview.preview.length) return []
      return Object.keys(imp.preview.preview[0])
    })
    function colLabel(c) {
      return ({
        name: '姓名', student_no: '学号', phone: '手机号', epc: '卡号EPC',
        pickup_code: '取件码', tracking_no: '运单号', shelf: '货架',
      }[c] || c)
    }

    // 学生管理
    const newStu = reactive({ student_no: '', name: '', phone: '', rfid_epc: '' })
    const stuMsg = ref(null)
    const students = ref([])
    const stuQ = ref('')
    const pendingByStudent = ref({})
    const awaitingBindId = ref(null)
    let awaitTimer = null

    async function loadStudents() {
      const q = stuQ.value ? `?q=${encodeURIComponent(stuQ.value)}` : ''
      students.value = await req('/api/students' + q)
      const all = await req('/api/packages?status=pending')
      const m = {}
      for (const p of all) m[p.student_id] = (m[p.student_id] || 0) + 1
      pendingByStudent.value = m
    }
    async function submitStu() {
      stuMsg.value = null
      try {
        if (!newStu.name || !newStu.phone) {
          stuMsg.value = { ok: false, text: '姓名和手机号必填' }
          return
        }
        const r = await req('/api/students', { method: 'POST', body: newStu })
        stuMsg.value = { ok: true, text: `已添加 ${r.name}` }
        flashMsg('操作成功')
        newStu.name = ''; newStu.student_no = ''; newStu.rfid_epc = ''
        newStu.phone = ''
        await refreshAfterSuccess(loadStudents)
      } catch (e) {
        stuMsg.value = { ok: false, text: friendlyError(e) }
      }
    }
    async function cancelAwaitBind(s) {
      try {
        await req(`/api/students/${s.id}/await-bind`, { method: 'DELETE' })
        clearTimeout(awaitTimer)
        awaitingBindId.value = null
        flashMsg('操作成功')
      } catch (e) { flashMsg(friendlyError(e), true) }
    }
    async function startAwaitBind(s) {
      try {
        await req(`/api/students/${s.id}/await-bind`, { method: 'POST' })
        awaitingBindId.value = s.id
        flashMsg(`请把 ${s.name} 的卡放在读写器上`)
        clearTimeout(awaitTimer)
        awaitTimer = setTimeout(() => { awaitingBindId.value = null }, 60000)
      } catch (e) {
        flashMsg(friendlyError(e), true)
      }
    }
    async function bindCard(s) {
      const epc = prompt(`把 RFID EPC 手动绑定到 ${s.name}：`)
      if (!epc) return
      try {
        await req(`/api/students/${s.id}/bind-card`, { method: 'POST', body: { epc } })
        flashMsg('操作成功')
        await refreshAfterSuccess(loadStudents)
      } catch (e) {
        flashMsg(friendlyError(e), true)
      }
    }
    async function bindUnknownTo(s) {
      if (!unknownEpc.value) { alert('最近没有读到新卡，请先刷一张未注册的卡'); return }
      try {
        await req('/api/bind-new-card', { method: 'POST', body: { epc: unknownEpc.value, student_id: s.id } })
        flashMsg('操作成功')
        unknownEpc.value = ''
        await refreshAfterSuccess(loadStudents)
      } catch (e) { flashMsg(friendlyError(e), true) }
    }

    // 统计
    const stats = reactive({ total_students: 0, pending: 0, today_arrived: 0, today_picked: 0 })
    async function loadStats() {
      Object.assign(stats, await req('/api/stats'))
    }

    // 刷卡事件日志
    const cardEvents = ref([])
    async function loadCardEvents() {
      cardEvents.value = await req('/api/card-events')
    }

    // 通用
    const flash = ref('')
    const flashError = ref(false)
    function friendlyError(e) {
      const message = e && e.message || ''
      if (/Failed to fetch|NetworkError|Load failed/i.test(message)) return '暂时无法连接服务，请检查系统是否已启动后重试'
      if (e instanceof TypeError || /is not a function|<html|<!doctype|Traceback|SyntaxError/i.test(message)) return '暂时未能完成操作，请稍后重试'
      return message || '暂时未能完成操作，请稍后重试'
    }
    async function refreshAfterSuccess(...loaders) {
      const results = await Promise.allSettled(loaders.map(fn => fn()))
      if (results.some(r => r.status === 'rejected')) flashMsg('操作成功，列表暂未更新，请点击刷新')
    }
    let flashTimer = null
    function flashMsg(text, error = false) {
      flashError.value = error
      flash.value = text
      clearTimeout(flashTimer)
      flashTimer = setTimeout(() => flash.value = '', 2200)
    }
    function formatTime(t) {
      if (!t) return ''
      const d = new Date(t * 1000)
      const pad = n => String(n).padStart(2, '0')
      return `${d.getMonth()+1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    }
    function shortEpc(epc) { return epc ? epc.slice(0, 8) + '…' : '' }
    function eventText(e) { return ({ show: '显示', checkout: '清空', unknown_card: '未注册', bind: '绑卡' }[e] || e) }
    function eventClass(e) { return ({ show: 'pending', checkout: 'picked', bind: 'picked', unknown_card: 'pending' }[e] || 'pending') }


    const speechSupported = typeof window !== 'undefined' && 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window
    const voiceEnabled = ref(true)
    try { voiceEnabled.value = localStorage.getItem('pickup-voice') !== 'off' } catch {}
    const voiceReady = ref(false)
    const voiceNotice = ref('')
    const voiceLabel = computed(() => !speechSupported ? '此浏览器不支持语音' : !voiceEnabled.value ? '语音已关闭' : voiceReady.value ? '语音已开启' : '点击开启语音')
    let pendingSpeech = ''
    function studentAddress(name) {
      name = String(name || '').trim()
      if (!name || /^手机尾号/.test(name)) return '同学'
      if (name.endsWith('同学')) return name
      const compound = ['欧阳','司马','上官','诸葛','司徒','司空','夏侯','皇甫','尉迟','公孙','慕容','令狐','长孙','宇文','东方','独孤','南宫']
      return (compound.find(s => name.startsWith(s)) || Array.from(name)[0]) + '同学'
    }
    function speak(text) {
      if (!speechSupported || !voiceEnabled.value) return
      if (!voiceReady.value) { pendingSpeech = text; return }
      try {
        window.speechSynthesis.cancel()
        const utterance = new SpeechSynthesisUtterance(text)
        const voices = window.speechSynthesis.getVoices()
        const voice = voices.find(v => /^zh[-_]CN$/i.test(v.lang)) || voices.find(v => /^zh/i.test(v.lang))
        if (voice) utterance.voice = voice
        utterance.lang = 'zh-CN'
        utterance.rate = 0.92
        utterance.onend = () => { voiceNotice.value = '' }
        utterance.onerror = e => {
          if (e.error === 'canceled' || e.error === 'interrupted') return
          voiceNotice.value = '语音未能播放，请检查设备音量和系统中文语音包后重试'
          if (e.error === 'not-allowed') voiceReady.value = false
        }
        voiceNotice.value = ''
        window.speechSynthesis.speak(utterance)
      } catch { voiceNotice.value = '语音暂不可用，请检查系统中文语音设置' }
    }
    function enableVoice() {
      if (!speechSupported) return
      voiceEnabled.value = true
      voiceReady.value = true
      try { localStorage.setItem('pickup-voice', 'on') } catch {}
      const text = pendingSpeech || '语音播报已开启，欢迎使用校园取件服务'
      pendingSpeech = ''
      speak(text)
    }
    function toggleVoice() {
      if (!voiceReady.value || !voiceEnabled.value) { enableVoice(); return }
      voiceEnabled.value = false
      pendingSpeech = ''
      voiceNotice.value = ''
      window.speechSynthesis.cancel()
      try { localStorage.setItem('pickup-voice', 'off') } catch {}
    }
    function testVoice() {
      voiceEnabled.value = true
      voiceReady.value = true
      pendingSpeech = ''
      try { localStorage.setItem('pickup-voice', 'on') } catch {}
      speak('戴同学，有2个待取件。李同学，已无待取件。')
    }
    function announceCard(data) {
      const address = studentAddress(data.student && data.student.name)
      if (data.kind === 'show') {
        const count = (data.packages || []).length
        speak(count ? `${address}，有${count}个待取件` : `${address}，已无待取件`)
      } else if (data.kind === 'checkout') {
        speak(`${address}，取件成功，已无待取件`)
      } else if (data.kind === 'unknown_card') {
        speak('这张校园卡尚未绑定，请先完成绑定')
      } else if (data.kind === 'bind_success') {
        speak(`${address}，校园卡绑定成功`)
      }
    }
    onBeforeUnmount(() => {
      clearInterval(beatTimer)
      clearInterval(cooldownTimer)
      clearTimeout(flashTimer)
      clearTimeout(awaitTimer)
      if (speechSupported) window.speechSynthesis.cancel()
    })

    // 拉取最新一次刷卡结果。页面刚打开 / SSE 还没连上时，
    // 靠它补上刚才那次刷卡，不会出现「刷了但屏幕没反应」。
    let lastCardTs = 0
    async function resetDemo() {
      try {
        await req('/api/reset', { method: 'POST' })
        lastCard.value = null
        pendingSpeech = ''
        if (speechSupported) window.speechSynthesis.cancel()
        lastCardTs = 0
        clearInterval(cooldownTimer)
        cooldownPct.value = 0
        await refreshAfterSuccess(loadPkgs, loadStudents, loadStats, loadCardEvents)
        flashMsg('操作成功')
      } catch (e) {
        flashMsg(friendlyError(e), true)
      }
    }

    // 调试用：把大屏状态挂到 window，方便在浏览器控制台/自动化里观察
    if (typeof window !== 'undefined') {
      window.__lastCard = lastCard
      window.__pullLatestCard = pullLatestCard      // 诊断用：可手动触发一次拉取
    }

    async function pullLatestCard() {
      let c
      try {
        c = await req('/api/card/latest')
      } catch (e) { return }
      if (!c || !c.kind) return
      // 只认「刚刚发生」的刷卡结果：超过 90 秒的历史不补显示，
      // 这样刷新网页 / 隔一会儿回来，大屏都会回到初始的「请刷卡」状态。
      const fresh = c.ts && (Date.now() / 1000 - c.ts) < 90
      if (!fresh) {
        lastCard.value = null
        pendingSpeech = ''
        if (speechSupported) window.speechSynthesis.cancel()
        lastCardTs = 0
        return
      }
      if (c.ts && c.ts === lastCardTs) return   // 同一次事件，不重复处理
      lastCardTs = c.ts
      applyCardEvent(c)
    }
    // 把一次刷卡结果应用到界面上。
    // 以前这里靠 SSE 长连接推送，但 Flask 自带的开发服务器扛不住长连接：
    // 跑几十秒后整个服务（连静态文件一起）会被占满、请求全部挂死，
    // 表现就是「刷第一次卡有反应，再刷就没反应了」。现在改成统一由轮询驱动。
    function applyCardEvent(data) {
      if (!data) return
      announceCard(data)
      if (data.kind === 'reset') {
        lastCard.value = null
        pendingSpeech = ''
        if (speechSupported) window.speechSynthesis.cancel()
        lastCardTs = 0
        clearInterval(cooldownTimer)
        cooldownPct.value = 0
      } else {
        lastCard.value = data
        if (data.ts) lastCardTs = data.ts
      }
      if (data.kind === 'show' && data.cooldown_seconds) {
        startCooldown(data.cooldown_seconds)
      } else {
        clearInterval(cooldownTimer)
        cooldownPct.value = 0
      }
      if (data.kind === 'bind_success') {
        clearTimeout(awaitTimer)
        awaitingBindId.value = null
        unknownEpc.value = ''
      }
      if (tab.value !== 'pickup') tab.value = 'pickup'
      Promise.allSettled([loadPkgs(), loadStudents(), loadStats(), loadCardEvents()])
    }

    onMounted(async () => {
      // 顺序很重要：先把「能刷新大屏」的通道建起来，再加载其它数据。
      // 以前这些数据加载放在前面，一旦某个请求失败/挂住，
      // 后面的 SSE 和轮询就永远不会启动 —— 表现为「刷卡了网页没反应」。
      const boot = async (fn, label) => {
        try { await fn() } catch (e) { console.warn('boot:' + label, e) }
      }
      await boot(async () => Object.assign(readerStatus, await req('/api/reader/status')), 'reader')

      // 每 1.5 秒轮询：读卡器心跳 + 最新刷卡结果（唯一驱动源）
      let beating = false
      beatTimer = setInterval(async () => {
        if (beating) return          // 上一轮还没回来就不要叠加，防止请求堆积
        beating = true
        try { Object.assign(readerStatus, await req('/api/reader/status')) } catch {}
        try { await pullLatestCard() } catch {}
        beating = false
      }, 1500)


      await boot(pullLatestCard, 'latestCard')

      await Promise.all([
        boot(loadPkgs, 'pkgs'),
        boot(loadStudents, 'students'),
        boot(loadStats, 'stats'),
        boot(loadCardEvents, 'events'),
      ])
      await boot(loadUnknownCard, 'unknown')
    })

    return {
      API,
      tab, tabs,
      readerStatus, readerStatusText, readerPillClass, readerAlive,
      lastCard, cardScreenClass, cooldownPct, cooldownText, resetDemo,
      bindForm, bindMsg, bindBusy, unknownEpc, submitBind, ignoreUnknown,
      newPkg, pkgMsg, packages, pkgFilter, loadPkgs, submitPkg, pickOne, restoreOne, pkgNeedCreate,
      imp, impMsg, onPickFile, onDrop, commitImport, resetImport, kindLabel, previewCols, colLabel,
      newStu, stuMsg, students, stuQ, pendingByStudent, loadStudents, submitStu, bindCard, startAwaitBind, cancelAwaitBind, awaitingBindId, bindUnknownTo,
      stats, loadStats,
      cardEvents, loadCardEvents,
      flash, flashError,
      speechSupported, voiceEnabled, voiceReady, voiceNotice, voiceLabel, toggleVoice, testVoice,
      formatTime, shortEpc, eventText, eventClass,
    }
  }
}).mount('#app')
