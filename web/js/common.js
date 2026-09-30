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

document.addEventListener('DOMContentLoaded', initNav);
