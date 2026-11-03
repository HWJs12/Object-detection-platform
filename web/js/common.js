/* ==========================================================
   公共工具: 请求封装 / 提示 / 格式化 / 导航高亮
   ========================================================== */

const API = {
    /** 统一请求: 自动解析 {success, message, data}, 失败抛 Error */
    async request(url, options = {}) {
        let resp;
        try {
            resp = await fetch(url, options);
        } catch (err) {
            throw new Error('无法连接服务器, 请确认后端已启动');
        }
        let payload;
        try {
            payload = await resp.json();
        } catch (err) {
            throw new Error(`服务器返回内容无法解析 (HTTP ${resp.status})`);
        }
        if (!payload.success) {
            const error = new Error(payload.message || '请求失败');
            error.code = payload.code;
            error.status = resp.status;
            throw error;
        }
        return payload.data;
    },

    get(url) {
        return API.request(url);
    },

    /** 上传文件(可带进度回调) */
    upload(url, formData, onProgress) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.responseType = 'json';
            if (onProgress && xhr.upload) {
                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
                };
            }
            xhr.onload = () => {
                const payload = xhr.response;
                if (!payload) return reject(new Error(`服务器返回异常 (HTTP ${xhr.status})`));
                if (payload.success) return resolve(payload.data);
                reject(new Error(payload.message || '上传失败'));
            };
            xhr.onerror = () => reject(new Error('网络错误, 上传失败'));
            xhr.onabort = () => reject(new Error('上传已中止，请先检查检测记录再重试'));
            xhr.ontimeout = () => reject(new Error('上传请求超时，请先检查检测记录再重试'));
            xhr.send(formData);
        });
    },

    del(url) {
        return API.request(url, { method: 'DELETE' });
    },

    putJSON(url, body) {
        return API.request(url, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    },

    postJSON(url, body) {
        return API.request(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
    },
};

/** 轻量提示框 */
function toast(message, type = 'info', duration = 3000) {
    let box = document.getElementById('toast-container');
    if (!box) {
        box = document.createElement('div');
        box.id = 'toast-container';
        document.body.appendChild(box);
    }
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => el.remove(), duration);
}

/** HTML 转义, 防止用户输入造成 XSS */
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    return String(text).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

/** 数字千分位 */
function fmtNumber(n) {
    return String(n ?? 0).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** 字节数转可读体积(B/KB/MB/GB)。用于导出预检的体积上界提示。 */
function fmtBytes(n) {
    const v = Number(n);
    if (!Number.isFinite(v) || v <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = v;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
        value /= 1024;
        i += 1;
    }
    // B 级不给小数, 更大单位保留一位
    return `${i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

/** 元素快捷选择 */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 当前页面导航高亮 */
function initNav() {
    const path = location.pathname.split('/').pop() || 'index.html';
    $$('.navbar nav a').forEach((a) => {
        const href = a.getAttribute('href');
        if (href === path || (path === '' && href === 'index.html')) {
            a.classList.add('active');
        }
    });
    const nav = document.querySelector('.navbar nav');
    const bar = document.querySelector('.navbar .nav-inner');
    if (nav && bar && !bar.querySelector('.nav-menu-toggle')) {
        const moreLinks = Array.from(nav.querySelectorAll('a')).filter((a) => ['system.html', 'api_docs.html'].includes(a.getAttribute('href')));
        if (moreLinks.length) {
            const more = document.createElement('details');
            more.className = 'nav-more';
            const summary = document.createElement('summary');
            summary.textContent = '更多';
            if (moreLinks.some((a) => a.classList.contains('active'))) summary.classList.add('active');
            more.appendChild(summary);
            const panel = document.createElement('div');
            panel.className = 'nav-more-panel';
            moreLinks.forEach((a) => panel.appendChild(a));
            more.appendChild(panel);
            nav.appendChild(more);
        }
        nav.id = 'site-navigation';
        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'nav-menu-toggle';
        toggle.textContent = '菜单';
        toggle.setAttribute('aria-controls', nav.id);
        toggle.setAttribute('aria-expanded', 'false');
        toggle.addEventListener('click', () => {
            const opened = nav.classList.toggle('is-open');
            toggle.setAttribute('aria-expanded', String(opened));
        });
        nav.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                nav.classList.remove('is-open');
                toggle.setAttribute('aria-expanded', 'false');
                nav.querySelectorAll('details[open]').forEach((item) => { item.open = false; });
                if (window.matchMedia('(max-width: 1100px)').matches) toggle.focus();
            }
        });
        bar.insertBefore(toggle, nav);
        bar.classList.add('nav-ready');
    }
    loadRuntimeStatus();
}

/** 顶部状态: 模型是否就绪 / 推理设备 */
async function loadRuntimeStatus() {
    const el = document.getElementById('nav-status');
    if (!el) return;
    try {
        const info = await API.get('/api/model/info');
        if (info.model_exists) {
            el.textContent = `模型: ${info.model_name || '已加载'} | ${String(info.device).toUpperCase()}`;
            el.title = `类别数: ${info.class_count} | 输入尺寸: ${info.imgsz}`;
        } else {
            el.textContent = '未检测到模型权重文件';
            el.title = '请将 .pt 文件放入 models/ 目录后刷新';
        }
    } catch (err) {
        el.textContent = '后端未连接';
    }
}

/* ---------------- 主题(明/暗) ---------------- */

const THEME_KEY = 'uwd-theme';

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = document.getElementById('theme-toggle');
    if (btn) {
        btn.textContent = theme === 'dark' ? '☀️' : '🌙';
        btn.title = theme === 'dark' ? '切换到浅色主题' : '切换到深色主题';
    }
    // 通知需要跟随主题重绘的组件(如图表)
    if (Array.isArray(window.__themeListeners)) {
        window.__themeListeners.forEach((fn) => {
            try { fn(theme); } catch (e) { /* 单个监听器失败不影响切换 */ }
        });
    }
}

/** 注册主题变化监听(供图表等组件使用) */
function onThemeChange(fn) {
    window.__themeListeners = window.__themeListeners || [];
    window.__themeListeners.push(fn);
}

/** 当前是否深色主题 */
function isDarkTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark';
}

/** 让 Chart.js 默认配色跟随明/暗主题(创建图表前调用) */
function applyChartTheme() {
    if (typeof Chart === 'undefined') return;
    const dark = isDarkTheme();
    Chart.defaults.color = dark ? '#93a4b8' : '#4b5563';
    Chart.defaults.borderColor = dark ? 'rgba(148, 163, 184, .18)' : 'rgba(15, 23, 42, .08)';
    Chart.defaults.font.family = '"Microsoft YaHei", "PingFang SC", sans-serif';
}

function initTheme() {
    const nav = document.querySelector('.navbar .nav-inner');
    if (nav && !document.getElementById('theme-toggle')) {
        const btn = document.createElement('button');
        btn.id = 'theme-toggle';
        btn.className = 'theme-toggle';
        btn.type = 'button';
        btn.addEventListener('click', () => {
            const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
            try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 隐私模式忽略 */ }
            applyTheme(next);
        });
        nav.appendChild(btn);
    }
    let saved = 'light';
    try { saved = localStorage.getItem(THEME_KEY) || 'light'; } catch (e) { /* ignore */ }
    applyTheme(saved);
}

/* ---------------- 检测框绘制(配色与后端 class_color 完全一致) ----------------- */

/** HSV -> RGB, h: 0-360, s/v: 0-1 */
function hsvToRgb(h, s, v) {
    const c = v * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = v - c;
    let rgb = [0, 0, 0];
    if (h < 60) rgb = [c, x, 0];
    else if (h < 120) rgb = [x, c, 0];
    else if (h < 180) rgb = [0, c, x];
    else if (h < 240) rgb = [0, x, c];
    else if (h < 300) rgb = [x, 0, c];
    else rgb = [c, 0, x];
    return rgb.map((n) => Math.round((n + m) * 255));
}

/** 按类别 ID 生成稳定颜色(与后端 47*hue%180, S=220, V=240 对齐) */
function classColor(classId) {
    const h = ((Number(classId) || 0) * 47 % 180) * 2;
    const [r, g, b] = hsvToRgb(h, 220 / 255, 240 / 255);
    return `rgb(${r}, ${g}, ${b})`;
}

/**
 * 在 canvas 上绘制底图 + 检测框。
 * @param {HTMLCanvasElement} canvas 目标画布
 * @param {HTMLImageElement} image   已加载完成的底图
 * @param {Array} detections         检测结果 [{class_id,class_name,confidence,bbox,track_id}]
 * @param {Object} opts              { hidden:Set<类别名>, showId:boolean }
 */
function drawDetections(canvas, image, detections, opts = {}) {
    const hidden = opts.hidden || new Set();
    const showId = !!opts.showId;
    const ctx = canvas.getContext('2d');
    const w = image.naturalWidth || image.width;
    const h = image.naturalHeight || image.height;
    if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
    }
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(image, 0, 0, w, h);

    const base = Math.max(w, h);
    const lw = Math.max(2, Math.round(base / 700));
    const fs = Math.max(12, Math.round(base / 45));
    ctx.lineWidth = lw;
    ctx.font = `600 ${fs}px "Microsoft YaHei", "PingFang SC", sans-serif`;
    ctx.textBaseline = 'top';

    (detections || []).forEach((d) => {
        if (hidden.has(d.class_name)) return;
        const [x1, y1, x2, y2] = d.bbox;
        const color = classColor(d.class_id);
        ctx.strokeStyle = color;
        ctx.strokeRect(x1, y1, Math.max(1, x2 - x1), Math.max(1, y2 - y1));

        const label = (showId && d.track_id !== undefined && d.track_id !== null ? `#${d.track_id} ` : '')
            + `${d.class_name} ${Number(d.confidence).toFixed(2)}`;
        const tw = ctx.measureText(label).width;
        const lh = fs + 6;
        const ly = y1 - lh >= 0 ? y1 - lh : y1;
        ctx.fillStyle = color;
        ctx.fillRect(x1, ly, tw + 10, lh);
        ctx.fillStyle = '#fff';
        ctx.fillText(label, x1 + 5, ly + 3);
    });
    return canvas;
}

/* ==========================================================
   口径说明与数字下钻 (纯前端)
   ----------------------------------------------------------
   设计目标: 页面上的每个关键数字都能回答"这个数怎么来的"。
   用途分两类:
     1) 口径徽标 `caliberBadge()`: 鼠标悬停/点击展开一段说明,
        内容为统计范围 / 计算公式 / 过滤条件, 纯文本, 不需要请求;
     2) 下钻 `numberDrill()`: 点击数字逐层展开, 直到原始明细记录。
   所有数据都来自已有接口, 不新增后端字段。
   ========================================================== */

/** 口径说明文案库: 每个 key 对应一个数字的完整定义, 供各页面复用。 */
const CALIBERS = {
    'overview.today_count': {
        title: '今日检测次数',
        range: '创建时间落在今日 00:00 ~ 24:00 的全部检测记录(含失败记录)',
        formula: 'COUNT(记录) WHERE date(created_at) = 今日',
        filter: '不区分图片/视频; 同一素材重复检测会重复计数',
        note: '该数与下方“今日检测数据”卡片的记录数口径不同: 后者只统计成功记录且按滚动 24 小时切分。',
    },
    'overview.today_objects': {
        title: '今日记录检出计数',
        range: '今日各记录的 total_objects 之和',
        formula: 'SUM(total_objects) WHERE date(created_at) = 今日',
        filter: '视频记录取“最可用的那个数”: 轨迹数 → MaxN → 累计检出框',
        note: '视频跟踪不完整时该数来自 MaxN, 为相对丰度而非个体总量。',
    },
    'overview.total_count': {
        title: '累计检测次数',
        range: '数据库中全部检测记录',
        formula: 'COUNT(记录)',
        filter: '含失败记录; 不做素材去重',
    },
    'overview.total_objects': {
        title: '累计记录检出计数',
        range: '全部记录的 total_objects 之和',
        formula: 'SUM(total_objects)',
        filter: '同一素材重复处理会累加, 因此不等于独立个体总数',
        note: '需要去重口径请查看“图片检出类别分布指标”或站点维度(同素材仅取最新一次)。',
    },
    'overview.class_count': {
        title: '不同类别数量',
        range: '所有成功记录的类别统计表',
        formula: 'COUNT(DISTINCT class_name)',
        filter: '只统计实际检出过的类别, 不含模型定义但未检出的类别',
    },
    'overview.avg_inference_ms': {
        title: '平均推理耗时',
        range: '所有记录了 inference_ms 的检测(图片为单张耗时, 视频为每帧均值)',
        formula: 'AVG(inference_ms)',
        filter: '不含视频解码、结果绘制、落库耗时; 不同设备不可直接比较',
    },
    'ecology.shannon': {
        title: "Shannon H'",
        range: '去重后的图片检出类别分布(每个源图片仅取最新一次成功检测)',
        formula: "H' = −Σ pᵢ·ln(pᵢ), pᵢ = 类别 i 的检出框数 / 总检出框数",
        filter: '不包含视频; 按检测日期筛选',
        note: '仅描述模型检出的框数分布, 不代表实际物种数或生态质量。',
    },
    'ecology.simpson': {
        title: 'Simpson D',
        range: '同 Shannon 的数据范围',
        formula: 'D = 1 − Σ pᵢ²',
        filter: '与 Shannon 使用同一批类别计数',
    },
    'ecology.pielou': {
        title: "Pielou J'",
        range: '同 Shannon 的数据范围',
        formula: "J' = H' / ln(S), S 为检出类别数; S ≤ 1 时不可计算",
        filter: '类别数不足 2 时返回空值, 不伪造 1.0',
    },
    'ecology.margalef': {
        title: 'Margalef 丰富度',
        range: '同 Shannon 的数据范围',
        formula: 'd = (S − 1) / ln(N), S 为类别数, N 为检出框数; N ≤ 1 时不可计算',
        filter: '类别数不足 2 或框数不足 2 时返回空值',
    },
    'record.effective_count': {
        title: '本记录计数',
        range: '单条检测记录',
        formula: '按口径优先级取第一个可用的数: 跨帧轨迹数 → 单帧峰值 MaxN → 累计检出框',
        filter: '图片记录直接取本图检出框数',
        note: '具体取用了哪一套, 见下方“计数口径”说明。',
    },
    'record.maxn': {
        title: '单帧峰值 MaxN',
        range: '该视频所有已处理帧中的单帧计数',
        formula: 'MaxN = max over frames (该帧内某类别的同时出现数量)',
        filter: '同一帧内不重复计数; 不要求跨帧关联成立',
        note: '属于相对丰度, 通常低估实际经过的目标总数, 不等同于生物个体数。',
    },
    'record.cpunx': {
        title: 'CPUnx (单位努力量)',
        range: '该视频的 MaxN 与时长',
        formula: 'CPUnx = MaxN / 时长(小时)',
        filter: '时长取自视频元信息; 时长缺失时不计算',
        note: '沿用 CPUE 思路, 用于不同时长视频之间的相对比较。',
    },
    'site.objects': {
        title: '站点检出计数',
        range: '该站点下全部记录(默认同素材仅取最新一次成功检测)',
        formula: 'SUM(各记录 total_objects)',
        filter: '未填写站点的记录归入“未标注站点”, 不丢弃',
        note: '未做面积或努力量归一, 跨站点比较前请结合调查时长与样方面积。',
    },
    'confidence.total': {
        title: '检出框总数',
        range: '全部成功检测的明细框(视频为采样帧明细)',
        formula: 'COUNT(detection_items)',
        filter: '视频明细存在入库采样上限, 因此视频部分为抽样口径',
    },
};

/**
 * 生成一个口径说明徽标。
 * @param {string} key CALIBERS 中的键
 * @param {string} label 徽标文字, 默认"口径"
 */
function caliberBadge(key, label = '口径') {
    const c = CALIBERS[key];
    if (!c) return '';
    const rows = [
        ['统计范围', c.range], ['计算公式', c.formula], ['过滤条件', c.filter],
    ].filter(([, v]) => v);
    return `<details class="caliber">
        <summary title="点击查看该数字的统计范围、公式与过滤条件">${escapeHtml(label)}</summary>
        <div class="caliber-body">
            <div class="caliber-title">${escapeHtml(c.title)}</div>
            <dl>${rows.map(([k, v]) =>
                `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('')}</dl>
            ${c.note ? `<p class="caliber-note">${escapeHtml(c.note)}</p>` : ''}
        </div>
    </details>`;
}

/** 口径说明区块(整段展示, 用于卡片底部或详情弹窗) */
function caliberBlock(keys, title = '本页数字的口径说明') {
    const list = (keys || []).filter((k) => CALIBERS[k]);
    if (!list.length) return '';
    return `<details class="caliber caliber-block">
        <summary>${escapeHtml(title)}</summary>
        <div class="caliber-body">
            ${list.map((k) => {
                const c = CALIBERS[k];
                const rows = [['范围', c.range], ['公式', c.formula], ['过滤', c.filter]]
                    .filter(([, v]) => v);
                return `<div class="caliber-item">
                    <div class="caliber-title">${escapeHtml(c.title)}</div>
                    <dl>${rows.map(([kk, vv]) =>
                        `<dt>${escapeHtml(kk)}</dt><dd>${escapeHtml(vv)}</dd>`).join('')}</dl>
                    ${c.note ? `<p class="caliber-note">${escapeHtml(c.note)}</p>` : ''}
                </div>`;
            }).join('')}
        </div>
    </details>`;
}

/**
 * 渲染一个可点击下钻的数字。
 * @param {string} text  显示文本
 * @param {Object} drill {title, caliber, loader: async () => ({columns, rows, summary})}
 */
function numberDrill(text, drill) {
    if (!drill || !drill.loader) return escapeHtml(text);
    const id = 'drill-' + Math.random().toString(36).slice(2, 9);
    DRILL_REGISTRY[id] = drill;
    return `<button type="button" class="num-drill" data-drill="${id}"
        title="点击查看构成该数字的原始记录">${text}<span class="num-drill-icon">↗</span></button>`;
}

const DRILL_REGISTRY = {};

/** 事件委托: 任意带 data-drill 的按钮点击后弹出下钻面板 */
document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-drill]');
    if (!btn) return;
    const drill = DRILL_REGISTRY[btn.dataset.drill];
    if (!drill) return;
    openDrillModal(drill);
});

const DRILL_STACK = [];

/** 打开下钻面板; 支持在面板内继续下钻(压栈), 顶部显示面包屑 */
async function openDrillModal(drill) {
    let root = document.getElementById('drill-root');
    if (!root) {
        root = document.createElement('div');
        root.id = 'drill-root';
        document.body.appendChild(root);
    }
    DRILL_STACK.length = 0;
    root.innerHTML = '';
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML = `<div class="modal modal-drill">
        <div class="modal-header">
            <h3 id="drill-title">${escapeHtml(drill.title || '下钻')}</h3>
            <button class="modal-close" data-drill-close>×</button>
        </div>
        <div class="drill-crumbs" id="drill-crumbs"></div>
        <div id="drill-body"><p class="muted small">正在加载...</p></div>
    </div>`;
    root.appendChild(mask);
    mask.addEventListener('click', (ev) => {
        if (ev.target === mask || ev.target.closest('[data-drill-close]')) {
            mask.remove();
            DRILL_STACK.length = 0;
        }
    });
    await pushDrillLevel(mask, drill);
}

async function pushDrillLevel(mask, drill) {
    const body = mask.querySelector('#drill-body');
    const crumbs = mask.querySelector('#drill-crumbs');
    DRILL_STACK.push({ title: drill.title || '下钻' });
    crumbs.innerHTML = DRILL_STACK.map((lv, i) => i === DRILL_STACK.length - 1
        ? `<span class="crumb current">${escapeHtml(lv.title)}</span>`
        : `<span class="crumb">${escapeHtml(lv.title)}</span>`).join('<span class="crumb-sep">›</span>')
        + (DRILL_STACK.length > 1 ? ' <button class="btn btn-sm btn-secondary" data-drill-back>返回上一层</button>' : '');
    const back = crumbs.querySelector('[data-drill-back]');
    if (back) back.addEventListener('click', () => {
        DRILL_STACK.pop();
        const prev = DRILL_STACK.pop();
        if (prev && prev.drill) pushDrillLevel(mask, prev.drill);
    });

    body.innerHTML = '<p class="muted small">正在加载...</p>';
    let data;
    try {
        data = await drill.loader();
    } catch (err) {
        body.innerHTML = `<div class="alert alert-error">下钻数据加载失败: ${escapeHtml(err.message)}</div>`;
        return;
    }
    // 记住当前层, 供"返回上一层"复原
    DRILL_STACK[DRILL_STACK.length - 1].drill = drill;

    const summary = (data.summary || []).map((s) =>
        `<span>${escapeHtml(s.label)}: <b>${escapeHtml(s.value)}</b></span>`).join('');
    const cols = data.columns || [];
    const rows = data.rows || [];
    const tableHtml = rows.length
        ? `<div class="table-wrap" style="max-height:340px;overflow:auto;">
             <table><thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>
             <tbody>${rows.map((r) => `<tr>${r.map((cell) => `<td>${cell}</td>`).join('')}</tr>`).join('')}
             </tbody></table></div>
           <p class="small muted" style="margin-top:8px;">${
               data.truncated ? `明细较多, 仅展示前 ${rows.length} 条(下钻用于核对口径, 非全量导出)。`
                              : `共 ${rows.length} 条原始记录。`}</p>`
        : '<p class="muted small">该口径下没有可展开的原始记录。</p>';

    body.innerHTML = `${summary ? `<div class="summary-line" style="margin-bottom:10px;">${summary}</div>` : ''}
        ${data.note ? `<p class="small muted" style="margin-top:0;">${escapeHtml(data.note)}</p>` : ''}
        ${tableHtml}
        ${(data.children || []).length ? `<div class="drill-children">${
            (data.children || []).map((ch, i) =>
                `<button class="btn btn-sm btn-secondary" data-drill-child="${i}">${escapeHtml(ch.label)}</button>`
            ).join('')}</div>` : ''}
        ${drill.caliber ? caliberBlock([drill.caliber], '这一层的口径') : ''}`;

    body.querySelectorAll('[data-drill-child]').forEach((btn) => {
        btn.addEventListener('click', () => {
            const child = data.children[Number(btn.dataset.drillChild)];
            if (child && child.drill) pushDrillLevel(mask, child.drill);
        });
    });
}

/* ==========================================================
   调查维度(站点 / 样方 / 水深 / 调查轮次) 表单辅助
   ----------------------------------------------------------
   三个上传页共用同一组输入框 id, 这里统一读取并追加到 FormData。
   只提交非空值: 让后端把"未填写"归入"未标注", 而不是出现
   "" / "   " / "未标注" 三种并存的状态。
   ========================================================== */

/**
 * 把页面上的调查维度输入追加到 FormData。
 * @param {FormData} formData 目标表单
 * @param {string} prefix 元素 id 前缀, 默认 survey-
 */
function appendSurvey(formData, prefix = 'survey-') {
    const mapping = [
        ['site_id', `${prefix}site`],
        ['transect', `${prefix}transect`],
        ['depth_m', `${prefix}depth`],
        ['survey_round', `${prefix}round`],
    ];
    mapping.forEach(([field, id]) => {
        const el = document.getElementById(id);
        if (!el) return;
        const value = String(el.value ?? '').trim();
        if (value) formData.append(field, value);
    });
    return formData;
}

/** 读取调查维度当前值(供回填/校验) */
function readSurvey(prefix = 'survey-') {
    const get = (id) => {
        const el = document.getElementById(id);
        return el ? String(el.value ?? '').trim() : '';
    };
    return {
        site_id: get(`${prefix}site`),
        transect: get(`${prefix}transect`),
        depth_m: get(`${prefix}depth`),
        survey_round: get(`${prefix}round`),
    };
}

/** 依据检测结果生成类别筛选 chips 并绑定回调 */function renderClassFilter(container, detections, hiddenSet, onChange) {
    if (!container) return;
    const counter = new Map();
    (detections || []).forEach((d) => {
        const item = counter.get(d.class_name) || { id: d.class_id, count: 0 };
        item.count += 1;
        counter.set(d.class_name, item);
    });
    const entries = Array.from(counter.entries()).sort((a, b) => b[1].count - a[1].count);

    if (!entries.length) {
        container.innerHTML = '<span class="muted small">未检出目标</span>';
        return;
    }
    container.innerHTML = '<span class="chips-label">类别筛选:</span>' + entries.map(([name, item]) => {
        const active = !hiddenSet.has(name);
        return `<span class="chip ${active ? 'active' : ''}" data-class="${escapeHtml(name)}">
            <span class="dot" style="background:${classColor(item.id)}"></span>
            ${escapeHtml(name)}<span class="num">${item.count}</span>
        </span>`;
    }).join('') + '<button class="btn btn-sm btn-secondary" data-chip-reset>全选</button>';

    $$('.chip', container).forEach((chip) => {
        chip.addEventListener('click', () => {
            const name = chip.dataset.class;
            if (hiddenSet.has(name)) {
                hiddenSet.delete(name);
                chip.classList.add('active');
            } else {
                hiddenSet.add(name);
                chip.classList.remove('active');
            }
            onChange && onChange();
        });
    });
    const resetBtn = container.querySelector('[data-chip-reset]');
    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            hiddenSet.clear();
            $$('.chip', container).forEach((c) => c.classList.add('active'));
            onChange && onChange();
        });
    }
}

document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initNav();
});

// 记录类型和状态使用图标，同时保留可访问名称。
function mediaTypeBadge(type) {
    const video = type === 'video', label = video ? '视频' : '图片';
    const shape = video ? '<rect x="3" y="5" width="14" height="14" rx="2"/><path d="m17 10 4-3v10l-4-3"/>' : '<path d="M4 7h4l2-3h4l2 3h4v12H4z"/><circle cx="12" cy="13" r="3"/>';
    return `<span class="badge icon-badge ${video ? 'video' : ''}" role="img" aria-label="${label}" title="${label}"><svg viewBox="0 0 24 24" aria-hidden="true">${shape}</svg></span>`;
}
function recordStatusBadge(status) {
    const success = status === 'success', label = success ? '成功' : '失败';
    return `<span class="badge icon-badge ${success ? '' : 'gray'}" role="img" aria-label="${label}" title="${label}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="${success ? 'M5 12l5 5L19 7' : 'M6 6l12 12M18 6 6 18'}"/></svg></span>`;
}
