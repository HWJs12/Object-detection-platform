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
            throw new Error(payload.message || '请求失败');
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

/** 依据检测结果生成类别筛选 chips 并绑定回调 */
function renderClassFilter(container, detections, hiddenSet, onChange) {
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
