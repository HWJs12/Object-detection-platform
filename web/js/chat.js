/* ==========================================================
   AI 助手浮窗 (接口文档页)
   ----------------------------------------------------------
   三条设计约束:
     1. 默认关闭 —— /api/chat/status 返回 enabled:false 时, 本脚本
        不生成任何 DOM, 页面上看不到助手入口, 也不会发起任何请求
     2. 不阻塞页面 —— 流式读取逐字渲染, 不阻塞页面其它交互
     3. 失败可降级 —— 任何异常只显示提示文案, 不影响页面其它功能
   ========================================================== */

(function () {
    'use strict';

    const STATUS_URL = '/api/chat/status';
    const STREAM_URL = '/api/chat/stream';
    const HISTORY_KEY = 'uwd-chat-history';
    const MAX_TURNS = 24;

    const PRESETS = [
        '图片检测接口怎么调用？',
        '怎么一次检测多张图片？',
        '视频检测的完整流程是什么？',
        '返回 success 为 false 时怎么排查？',
    ];

    let messages = [];      // [{role, content}]
    let streaming = false;
    let replyController = null;
    let replyRequest = 0;

    // ---------------------------------------------------------- 启动

    async function init() {
        let data;
        try {
            data = await API.get(STATUS_URL);
        } catch (err) {
            return;                              // 查不到状态: 静默退出
        }
        if (!data || !data.enabled) return;      // 未启用: 不生成任何 DOM

        buildUI(data.model);
        restoreHistory();
    }

    // ---------------------------------------------------------- 界面

    function buildUI(model) {
        const fab = document.createElement('button');
        fab.className = 'chat-fab';
        fab.type = 'button';
        fab.title = '点击打开助手，拖动调整位置';
        fab.setAttribute('aria-label', '打开接口答疑助手');
        fab.setAttribute('aria-controls', 'chat-panel');
        fab.setAttribute('aria-expanded', 'false');
        fab.innerHTML = '<img class="chat-fab-icon" src="assets/whale-assistant.png" alt="" draggable="false">';

        const panel = document.createElement('section');
        panel.className = 'chat-panel';
        panel.id = 'chat-panel';
        panel.setAttribute('aria-label', '接口答疑助手');
        panel.innerHTML = `
            <header class="chat-head">
                <img class="chat-head-avatar" src="assets/whale-assistant.png" alt="" aria-hidden="true">
                <div class="chat-head-main">
                    <p class="chat-title">接口答疑助手</p>
                    <p class="chat-sub">${escapeHtml(model || 'AI')} · 平台答疑</p>
                </div>
                <div class="chat-head-actions">
                    <button type="button" class="chat-icon-btn" data-act="clear" title="清空对话">清空</button>
                    <button type="button" class="chat-icon-btn" data-act="close" title="收起">收起</button>
                </div>
            </header>
            <div class="chat-body" id="chat-body"></div>
            <form class="chat-foot" id="chat-form">
                <textarea id="chat-input" rows="2" placeholder="问点什么…（Enter 发送，Shift+Enter 换行）"></textarea>
                <button type="submit" id="chat-send" title="发送">发送</button>
            </form>`;

        // 小屏入口位于导航栏，不悬浮覆盖表单；大屏仍保持浮动入口。
        const navigation = document.querySelector('.navbar .nav-inner');
        (navigation || document.body).appendChild(fab);
        document.body.appendChild(panel);

        const wasDragged = enableDragging(fab);
        fab.addEventListener('click', (event) => {
            if (wasDragged(event)) return;
            toggle(panel, fab);
        });
        panel.querySelector('[data-act="close"]').addEventListener('click', () => toggle(panel, fab, false));
        panel.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') { event.preventDefault(); toggle(panel, fab, false); }
        });
        panel.querySelector('[data-act="clear"]').addEventListener('click', () => {
            replyRequest += 1;
            if (replyController) replyController.abort();
            replyController = null;
            streaming = false;
            setBusy(false);
            messages = [];
            saveHistory();
            render();
        });

        const form = panel.querySelector('#chat-form');
        const input = panel.querySelector('#chat-input');
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            submit(input.value);
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                submit(input.value);
            }
        });
    }

    // 拖动后按视口比例保存位置；无保存位置时沿用各设备原来的默认布局。
    function enableDragging(fab) {
        const key = 'uwd-assistant-position-v1';
        let drag = null;
        let suppressClick = false;
        let position = null;
        let suppressTimer = null;

        function place(x, y) {
            fab.classList.add('chat-positioned');
            const maxX = Math.max(0, window.innerWidth - fab.offsetWidth);
            const maxY = Math.max(0, window.innerHeight - fab.offsetHeight);
            const left = Math.min(maxX, Math.max(0, x));
            const top = Math.min(maxY, Math.max(0, y));
            fab.style.left = `${left}px`;
            fab.style.top = `${top}px`;
            position = { x: maxX ? left / maxX : 0, y: maxY ? top / maxY : 0 };
        }
        function restore(pos) {
            fab.classList.add('chat-positioned');
            place(pos.x * Math.max(0, window.innerWidth - fab.offsetWidth),
                  pos.y * Math.max(0, window.innerHeight - fab.offsetHeight));
        }
        function save() {
            if (!position) return;
            try { localStorage.setItem(key, JSON.stringify(position)); } catch (err) { /* 隐私模式 */ }
        }
        try {
            const saved = JSON.parse(localStorage.getItem(key) || 'null');
            if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
                restore({ x: Math.max(0, Math.min(1, saved.x)), y: Math.max(0, Math.min(1, saved.y)) });
            }
        } catch (err) { /* 损坏数据忽略 */ }
        fab.addEventListener('pointerdown', (event) => {
            if (!event.isPrimary || (event.pointerType === 'mouse' && event.button !== 0)) return;
            const rect = fab.getBoundingClientRect();
            drag = { id: event.pointerId, x: event.clientX, y: event.clientY,
                     left: rect.left, top: rect.top, moved: false };
        });
        document.addEventListener('pointermove', (event) => {
            if (!drag || event.pointerId !== drag.id) return;
            const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
            if (!drag.moved && Math.hypot(dx, dy) < 7) return;
            drag.moved = true;
            event.preventDefault();
            place(drag.left + dx, drag.top + dy);
        }, { passive: false });
        function finish(event) {
            if (!drag || (event && event.pointerId !== drag.id)) return;
            if (drag.moved) {
                save();
                suppressClick = true;
                clearTimeout(suppressTimer);
                suppressTimer = setTimeout(() => { suppressClick = false; }, 700);
            }
            drag = null;
        }
        document.addEventListener('pointerup', finish);
        document.addEventListener('pointercancel', finish);
        window.addEventListener('blur', () => finish(null));
        window.addEventListener('resize', () => { if (position) restore(position); });
        fab.addEventListener('keydown', (event) => {
            const delta = { ArrowLeft: [-12, 0], ArrowRight: [12, 0], ArrowUp: [0, -12], ArrowDown: [0, 12] }[event.key];
            if (!delta) return;
            event.preventDefault();
            const rect = fab.getBoundingClientRect();
            place(rect.left + delta[0], rect.top + delta[1]);
            save();
        });
        return (event) => {
            if (!suppressClick || event.detail === 0) return false;
            suppressClick = false;
            clearTimeout(suppressTimer);
            event.preventDefault();
            return true;
        };
    }

    function toggle(panel, fab, force) {
        const open = typeof force === 'boolean' ? force : !panel.classList.contains('open');
        panel.classList.toggle('open', open);
        fab.classList.toggle('hidden', open);
        fab.setAttribute('aria-expanded', String(open));
        if (!open) fab.focus();
        if (open) {
            render();
            const input = panel.querySelector('#chat-input');
            if (input) setTimeout(() => input.focus(), 60);
        }
    }

    // ---------------------------------------------------------- 历史

    function saveHistory() {
        try {
            sessionStorage.setItem(HISTORY_KEY, JSON.stringify(messages.slice(-MAX_TURNS * 2)));
        } catch (err) { /* 隐私模式等场景下写入失败可忽略 */ }
    }

    function restoreHistory() {
        try {
            const raw = sessionStorage.getItem(HISTORY_KEY);
            if (raw) {
                const parsed = JSON.parse(raw);
                if (Array.isArray(parsed)) messages = parsed;
            }
        } catch (err) { /* 解析失败则从空开始 */ }
    }

    // ---------------------------------------------------------- 渲染

    function render() {
        const body = document.getElementById('chat-body');
        if (!body) return;

        if (!messages.length) {
            body.innerHTML = `
                <div class="chat-empty">
                    <p class="chat-empty-title">可以问我这些</p>
                    ${PRESETS.map((t) => `<button type="button" class="chat-preset">${escapeHtml(t)}</button>`).join('')}
                </div>`;
            body.querySelectorAll('.chat-preset').forEach((btn) => {
                btn.addEventListener('click', () => submit(btn.textContent));
            });
            return;
        }

        body.innerHTML = messages.map((m) => `
            <div class="chat-msg chat-${m.role === 'user' ? 'user' : 'bot'}">
                <div class="chat-bubble">${formatContent(m.content)}</div>
            </div>`).join('');
        body.scrollTop = body.scrollHeight;
    }

    /** 极简 Markdown 渲染: 只转义后处理代码块、行内代码与换行, 不引入外部库 */
    function formatContent(text) {
        let html = escapeHtml(text || '');
        html = html.replace(/```(\w*)\n?([\s\S]*?)```/g,
            (_, lang, code) => `<pre class="chat-code">${code.replace(/\n$/, '')}</pre>`);
        html = html.replace(/`([^`\n]+)`/g, '<code class="chat-inline">$1</code>');
        return html.replace(/\n/g, '<br>');
    }

    // ---------------------------------------------------------- 发送

    async function submit(raw) {
        const text = (raw || '').trim();
        if (!text || streaming) return;

        const input = document.getElementById('chat-input');
        if (input) input.value = '';

        messages.push({ role: 'user', content: text });
        messages.push({ role: 'assistant', content: '' });
        render();

        const requestId = ++replyRequest;
        const controller = new AbortController();
        replyController = controller;
        streaming = true;
        setBusy(true);
        try {
            await streamReply(controller.signal);
        } catch (err) {
            if (requestId !== replyRequest) return;
            const last = messages[messages.length - 1];
            if (last) last.content = `（助手调用失败：${err.message || '未知错误'}）`;
            render();
        } finally {
            if (requestId !== replyRequest) return;
            replyController = null;
            streaming = false;
            setBusy(false);
            saveHistory();
            render();
        }
    }

    function setBusy(busy) {
        const btn = document.getElementById('chat-send');
        const input = document.getElementById('chat-input');
        if (btn) { btn.disabled = busy; btn.textContent = busy ? '…' : '发送'; }
        if (input) input.disabled = busy;
    }

    async function streamReply(signal) {
        const body = document.getElementById('chat-body');
        const payload = messages
            .filter((m) => m.content)
            .map((m) => ({ role: m.role, content: m.content }));

        const resp = await fetch(STREAM_URL, {
            method: 'POST',
            signal,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ messages: payload }),
        });

        // 非 2xx: 后端返回的是统一响应体, 取出 message 展示
        if (!resp.ok) {
            let msg = `HTTP ${resp.status}`;
            try {
                const err = await resp.json();
                if (err && err.message) msg = err.message;
            } catch (e) { /* 忽略解析失败 */ }
            throw new Error(msg);
        }
        if (!resp.body) throw new Error('当前浏览器不支持流式响应');

        const reader = resp.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let failed = null;

        while (true) {
            const { done, value } = await reader.read();
            if (signal.aborted) return;
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const blocks = buffer.split('\n\n');
            buffer = blocks.pop() || '';          // 末段可能不完整, 留到下一轮

            for (const block of blocks) {
                const line = block.trim();
                if (!line.startsWith('data:')) continue;
                let evt;
                try {
                    evt = JSON.parse(line.slice(5).trim());
                } catch (e) {
                    continue;                     // 忽略无法解析的片段
                }
                if (evt.type === 'delta' && evt.text) {
                    const last = messages[messages.length - 1];
                    last.content += evt.text;
                    paintLast(last.content);
                } else if (evt.type === 'error') {
                    failed = evt.message || '助手暂时不可用';
                }
            }
        }

        if (failed) {
            const last = messages[messages.length - 1];
            last.content = last.content
                ? `${last.content}\n\n（${failed}）`
                : `（${failed}）`;
            render();
            return;
        }
        if (!messages[messages.length - 1].content) {
            messages[messages.length - 1].content = '（助手没有返回内容）';
        }
    }

    /** 只更新最后一条气泡, 避免每次增量都整表重绘 */
    function paintLast(text) {
        const body = document.getElementById('chat-body');
        if (!body) return;
        const bubbles = body.querySelectorAll('.chat-msg.chat-bot .chat-bubble');
        const node = bubbles[bubbles.length - 1];
        if (node) {
            node.innerHTML = formatContent(text);
            body.scrollTop = body.scrollHeight;
        } else {
            render();
        }
    }

    // ---------------------------------------------------------- 引导

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
