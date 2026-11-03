/* 视频检测页: 上传 -> WebSocket 实时逐帧检测(暂停/继续/调阈值/类别筛选) */

let currentFile = null;
let socket = null;
let currentTaskId = null;
let paused = false;
let uploading = false;
let peak = 0;
let pendingFrame = false;      // 上一帧是否渲染完成, 未完成时丢弃新帧避免堆积
let renderedMsg = null;        // 最近一次"已渲染"的帧数据(供筛选重绘使用)
let lastFilterSignature = '';
let processingStartedAt = 0;
let pauseStartedAt = 0;
let pausedDuration = 0;
let stopping = false;
let frameGeneration = 0;
let reconnectTimer = null;
let jobsTimer = null;
let leavingPage = false;
let jobsRequest = 0;
let attachmentRequest = 0;
const VIDEO_TASK_KEY = 'uwd-background-video-task';

function rememberTask(taskId) {
    try {
        if (taskId) localStorage.setItem(VIDEO_TASK_KEY, taskId);
        else localStorage.removeItem(VIDEO_TASK_KEY);
    } catch (err) { /* The server task list still permits recovery. */ }
}

function applyTaskSnapshot(task) {
    paused = !!task.paused;
    currentTaskId = task.terminal ? null : task.task_id;
    $('#file-name').textContent = `后台任务: ${task.source_name}`;
    $('#file-name').classList.remove('hidden');
    const meta = task.meta;
    $('#video-meta').textContent = `${meta.width}×${meta.height} | ${meta.fps.toFixed(1)} FPS | 共 ${meta.total_frames} 帧 | 阈值 ${task.conf_threshold}`;
    $('#conf-slider').value = task.conf_threshold;
    $('#conf-value').textContent = Number(task.conf_threshold).toFixed(2);
    processingStartedAt = performance.now() - (task.elapsed_active_sec || 0) * 1000;
    pausedDuration = 0;
    pauseStartedAt = paused ? performance.now() : 0;
    setControls(task.terminal ? 'idle' : paused ? 'paused' : 'running');
    if (task.state === 'stopped' || task.terminal?.type === 'stopped') {
        handleStopped(task.terminal || {});
        return;
    }
    if (stopping || task.state === 'stopping') {
        showStoppingPreview();
        return;
    }
    if (!task.terminal) $('#progress-text').textContent = paused
        ? `后台任务已暂停（第 ${task.frame_index} 帧），可继续或停止`
        : `已恢复后台任务，正在处理第 ${task.frame_index} 帧`;
}

async function loadVideoJobs(restore = false) {
    const requestId = ++jobsRequest;
    try {
        const data = await API.get('/api/detect/video/tasks');
        if (requestId !== jobsRequest) return;
        const names = { starting: '准备中', running: '检测中', paused: '已暂停', stopping: '正在停止', completed: '已完成', failed: '失败', stopped: '已停止' };
        $('#video-jobs-list').innerHTML = data.items.length ? data.items.map((task) => `<div class="video-job-row">
            <span class="job-name">${escapeHtml(task.source_name)}</span>
            <span class="small">${names[task.state] || task.state} · ${task.progress.toFixed(0)}%</span>
            <button class="btn btn-sm btn-secondary" data-view-job="${escapeHtml(task.task_id)}">${task.terminal ? '查看结果' : '恢复查看'}</button>
        </div>`).join('') : '<p class="small muted">暂无后台任务</p>';
        $('#video-jobs-error').textContent = '';
        if (restore && !currentTaskId && !uploading && !currentFile) {
            let saved = null;
            try { saved = localStorage.getItem(VIDEO_TASK_KEY); } catch (err) { /* optional */ }
            const active = data.items.find((task) => !task.terminal);
            const chosen = data.items.find((task) => task.task_id === saved) || active;
            if (chosen) await attachVideoJob(chosen.task_id);
            else if (saved) rememberTask(null);
        }
    } catch (err) {
        if (requestId === jobsRequest) $('#video-jobs-error').textContent = '任务列表加载失败：' + err.message;
    }
}

async function attachVideoJob(taskId, keepFile = false) {
    if (uploading) { toast('请等待上传完成再切换任务', 'info'); return; }
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    const requestId = ++attachmentRequest;
    let task;
    try { task = await API.get(`/api/detect/video/${encodeURIComponent(taskId)}/status`); }
    catch (err) { if (requestId !== attachmentRequest) return; throw err; }
    if (requestId !== attachmentRequest || uploading || leavingPage) return;
    if (!keepFile) { currentFile = null; $('#file-input').value = ''; }
    if (socket) { const previous = socket; socket = null; previous.close(); }
    resetLiveStats();
    rememberTask(taskId);
    applyTaskSnapshot(task);
    if (task.state === 'stopped' || task.terminal?.type === 'stopped') return;
    openSocket(taskId);
}

function scheduleReconnect(taskId) {
    if (leavingPage || currentTaskId !== taskId || reconnectTimer) return;
    $('#progress-text').textContent = '预览连接中断，后台继续处理，正在重新连接...';
    reconnectTimer = setTimeout(async () => {
        reconnectTimer = null;
        if (leavingPage || currentTaskId !== taskId) return;
        try { await attachVideoJob(taskId, true); }
        catch (err) {
            if (err.code === 'TASK_NOT_FOUND') {
                rememberTask(null);
                currentTaskId = null;
                handleError({ message: err.message });
            } else scheduleReconnect(taskId);
        }
    }, 1000);
}

const videoLimits = {
    maxUploadMb: 300,
    extensions: ['.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm'],
};
const canvasImage = new Image();
const hiddenClasses = new Set();   // 被隐藏的类别(仅影响显示)
const knownClasses = new Map();    // class_name -> class_id

function validUniqueCount(data) {
    return data.unique_count_valid === true || data.unique_count_valid === 1;
}

function showCountingStatus(data) {
    const warning = $('#count-warning');
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    // 口径叙述: 说明"本次取用了哪一套口径、依据是什么", 不使用"失效/不可用"这类否定结论。
    let text;
    if (validUniqueCount(data)) {
        text = '计数口径：跟踪轨迹数。目标丢失后重新建立轨迹仍可能重复计数, 不能视为经确认的生物个体数。';
    } else if ((data.maxn_total || 0) > 0) {
        const reason = data.counting_mode_reason ? `（${data.counting_mode_reason}）` : '';
        text = `计数口径：单帧峰值 MaxN ${fmtNumber(data.maxn_total)} 条${reason}。`
            + '该方法只要求同一帧内不重复计数, 不依赖跨帧个体识别, 是 BRUV 调查常用的相对丰度指标。';
    } else {
        text = '计数口径：累计检测框。本次各帧未形成稳定的类别峰值, 因此以逐帧检出框作为相对丰度的替代口径。';
    }
    warning.textContent = [text, ...warnings].join(' ');
    warning.classList.remove('hidden');
}

function formatRemaining(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '估算中';
    const rounded = Math.ceil(seconds);
    return rounded >= 60 ? `${Math.floor(rounded / 60)} 分 ${rounded % 60} 秒` : `${rounded} 秒`;
}

/* ---------------- 按钮状态机: idle | running | paused ---------------- */
function setControls(state) {
    $('#btn-start').disabled = !(state === 'idle' && !!currentFile && !currentTaskId && !uploading);
    $('#btn-pause').disabled = state !== 'running';
    $('#btn-resume').disabled = state !== 'paused';
    $('#btn-stop').classList.toggle('hidden', state === 'idle');
    $('#btn-stop').disabled = state === 'idle';
    $('#paused-mask').classList.toggle('hidden', state !== 'paused');
    $('#video-canvas').style.opacity = state === 'paused' ? '0.35' : '1';
}

/* ---------------- 上传 ---------------- */
function bindUpload() {
    const area = $('#upload-area');
    const input = $('#file-input');

    area.addEventListener('click', () => input.click());
    area.addEventListener('dragover', (e) => {
        e.preventDefault();
        area.classList.add('dragover');
    });
    area.addEventListener('dragleave', () => area.classList.remove('dragover'));
    area.addEventListener('drop', (e) => {
        e.preventDefault();
        area.classList.remove('dragover');
        if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
    });
    input.addEventListener('change', () => {
        if (input.files.length) selectFile(input.files[0]);
    });
}

function selectFile(file) {
    if (uploading || currentTaskId || (socket && socket.readyState !== WebSocket.CLOSED)) {
        toast('当前视频任务尚未结束，请先停止或等待完成后再选择视频', 'warning');
        return;
    }
    const extension = (/\.[^.]+$/.exec(file.name)?.[0] || '').toLowerCase();
    if (!videoLimits.extensions.includes(extension)) {
        toast('请选择视频文件', 'error');
        return;
    }
    if (!file.size || file.size > videoLimits.maxUploadMb * 1024 * 1024) {
        toast(`视频不能为空，且不能超过 ${videoLimits.maxUploadMb} MB`, 'error');
        return;
    }
    currentFile = file;
    $('#file-name').textContent = `已选择: ${file.name} (${(file.size / 1024 / 1024).toFixed(2)} MB)`;
    $('#file-name').classList.remove('hidden');
    $('#video-meta').textContent = '';
    $('#alert-box').innerHTML = '';
    resetLiveStats();
    setControls('idle');
}

/* ---------------- 阈值滑动条 ---------------- */
function bindSlider() {
    const slider = $('#conf-slider');

    slider.addEventListener('input', () => {
        $('#conf-value').textContent = parseFloat(slider.value).toFixed(2);
    });

    // 松开滑块时才发请求: 检测中则实时调整, 未在检测则仅作为下次上传的参数
    slider.addEventListener('change', async () => {
        const value = parseFloat(slider.value);
        if (!currentTaskId || !socket) return;
        try {
            const st = await API.postJSON(
                `/api/detect/video/${currentTaskId}/control`, { action: 'set_conf', value }
            );
            toast(`置信度阈值已实时调整为 ${st.conf_threshold}`, 'info', 2000);
        } catch (err) {
            toast('阈值调整失败: ' + err.message, 'error');
        }
    });
}

function resetLiveStats() {
    frameGeneration += 1;
    pendingFrame = false;
    paused = false;
    canvasImage.onload = null;
    canvasImage.onerror = null;
    canvasImage.removeAttribute?.('src');
    const canvas = $('#video-canvas');
    canvas.getContext('2d')?.clearRect?.(0, 0, canvas.width, canvas.height);
    canvas.width = 0;
    canvas.height = 0;
    processingStartedAt = 0;
    pauseStartedAt = 0;
    pausedDuration = 0;
    stopping = false;
    peak = 0;
    renderedMsg = null;
    hiddenClasses.clear();
    knownClasses.clear();
    lastFilterSignature = '';
    $('#class-filter').innerHTML = '<span class="muted small">开始检测后可在此按类别筛选检测框</span>';
    $('#live-total').textContent = '0';
    $('#live-unique').textContent = '—';
    $('#live-fps').textContent = '0.0';
    $('#live-time').textContent = '0.0 s';
    $('#live-peak').textContent = '0';
    $('#live-tags').innerHTML = '<span class="muted small">暂无数据</span>';
    $('#progress-bar').style.width = '0%';
    $('#progress-text').textContent = '';
    $('#btn-download-video').classList.add('hidden');
    $('#count-warning').classList.add('hidden');
    $('#video-canvas').classList.add('hidden');
    $('#stage-placeholder').classList.remove('hidden');
    $('#stage-placeholder').textContent = '上传并开始分析后，此处按处理进度更新检测画面';
    $('#summary-card').classList.add('hidden');
    $('#video-meta').textContent = '';
}

function showStoppingPreview() {
    resetLiveStats();
    stopping = true;
    setControls('stopping');
    $('#btn-stop').disabled = true;
    $('#stage-placeholder').textContent = '正在停止检测，画面已清空';
    $('#progress-text').textContent = '正在停止并释放检测资源...';
}

function handleStopped(msg = {}) {
    currentTaskId = null;
    rememberTask(null);
    resetLiveStats();
    $('#stage-placeholder').textContent = '已停止检测，重新开始后显示分析画面';
    $('#progress-text').textContent = msg.message || '已停止检测，未完成的结果不保存';
    setControls('idle');
}

/* ---------------- 开始检测 ---------------- */
async function startDetect() {
    if (uploading || currentTaskId || (socket && socket.readyState !== WebSocket.CLOSED)) {
        toast('当前已有视频任务，请先停止或等待完成', 'warning');
        return;
    }
    if (!currentFile) {
        toast('请先选择视频', 'error');
        return;
    }
    if (typeof requireSurveyStation === 'function' && !requireSurveyStation()) return;
    attachmentRequest += 1;
    $('#btn-start').disabled = true;
    uploading = true;
    $('#alert-box').innerHTML = '';
    $('#summary-card').classList.add('hidden');
    resetLiveStats();
    $('#progress-text').textContent = '正在上传视频...';

    let taskId;
    try {
        const formData = new FormData();
        formData.append('file', currentFile);
        formData.append('conf', $('#conf-slider').value);
        appendSurvey(formData);
        const data = await API.upload('/api/detect/video', formData, (p) => {
            $('#progress-text').textContent = `上传进度 ${p}%`;
        });
        taskId = data.task_id;
        currentTaskId = taskId;
        rememberTask(taskId);
        loadVideoJobs();
        $('#video-meta').textContent =
            `分辨率 ${data.meta.width}×${data.meta.height} | ${data.meta.fps.toFixed(1)} FPS | ` +
            `共 ${data.meta.total_frames} 帧 | 时长 ${data.meta.duration_sec}s | ` +
            `阈值 ${data.config.conf_threshold}`;
    } catch (err) {
        uploading = false;
        $('#alert-box').innerHTML = `<div class="alert alert-error">上传失败: ${escapeHtml(err.message)}</div>`;
        setControls('idle');
        $('#progress-text').textContent = '';
        return;
    }

    uploading = false;
    openSocket(taskId);
}

/* ---------------- WebSocket ---------------- */
function openSocket(taskId) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/video/${taskId}`);
    socket = ws;
    let finished = false;
    ws.onopen = () => {
        if (socket !== ws || currentTaskId !== taskId || stopping) return;
        if (!processingStartedAt) processingStartedAt = performance.now();
        setControls(paused ? 'paused' : 'running');
    };
    ws.onmessage = (event) => {
        if (socket !== ws || finished) return;
        let msg;
        try { msg = JSON.parse(event.data); } catch (err) { return; }
        if (msg.type === 'task') {
            applyTaskSnapshot(msg.task);
            if (msg.task.state === 'stopped' || msg.task.terminal?.type === 'stopped') finished = true;
        }
        else if (msg.type === 'frame') handleFrame(msg);
        else if (msg.type === 'paused' && !stopping) {
            paused = true;
            setControls('paused');
        } else if (msg.type === 'done') { finished = true; stopping = false; currentTaskId = null; handleDone(msg); }
        else if (msg.type === 'error') { finished = true; currentTaskId = null; handleError(msg); }
        else if (msg.type === 'stopped') {
            finished = true;
            handleStopped(msg);
        }
    };
    ws.onerror = () => { /* onclose schedules recovery without stopping the job. */ };
    ws.onclose = () => {
        if (socket !== ws) return;
        socket = null;
        if (finished) {
            currentTaskId = null;
            paused = false;
            setControls('idle');
            loadVideoJobs();
        } else scheduleReconnect(taskId);
    };
}

function handleFrame(msg) {
    if (stopping || leavingPage) return;
    const generation = frameGeneration;
    if (!pendingFrame) {
        pendingFrame = true;
        canvasImage.onload = () => {
            if (generation !== frameGeneration) return;
            renderedMsg = msg;
            const canvas = $('#video-canvas');
            drawDetections(canvas, canvasImage, msg.detections,
                { hidden: hiddenClasses, showId: true });
            canvas.classList.remove('hidden');
            $('#stage-placeholder').classList.add('hidden');
            pendingFrame = false;
            updateClassFilter(msg.detections);
        };
        canvasImage.onerror = () => { if (generation === frameGeneration) pendingFrame = false; };
        canvasImage.src = 'data:image/jpeg;base64,' + msg.image;
    } else {
        // 该帧未及时渲染被丢弃, 但类别信息仍需更新
        updateClassFilter(msg.detections);
    }

    $('#live-total').textContent = msg.total;
    // 逐帧轨迹数在未建立完整跨帧关联时仍会随帧增长, 因此照常展示并如实标注含义。
    $('#live-unique').textContent = validUniqueCount(msg)
        ? fmtNumber(msg.unique)
        : (Number.isFinite(msg.unique) ? `${fmtNumber(msg.unique)}(轨迹编号)` : '—');
    showCountingStatus(msg);
    $('#live-fps').textContent = msg.fps.toFixed(1);
    $('#live-time').textContent = msg.timestamp.toFixed(1) + ' s';
    peak = Math.max(peak, msg.total);
    $('#live-peak').textContent = peak;

    $('#live-tags').innerHTML = msg.counts.length
        ? msg.counts.map((c) => `<span class="tag">${escapeHtml(c.class_name)}: <b>${c.count}</b></span>`).join('')
        : '<span class="muted small">当前帧未检出目标</span>';

    $('#progress-bar').style.width = `${Math.min(100, msg.progress)}%`;
    const activeMs = Math.max(0, performance.now() - processingStartedAt - pausedDuration
        - (pauseStartedAt ? performance.now() - pauseStartedAt : 0));
    const progress = Number(msg.progress);
    const remaining = progress > 0 && progress < 100
        ? activeMs / 1000 * (100 / progress - 1) : NaN;
    $('#progress-text').textContent =
        `离线分析 ${msg.progress}% | 视频第 ${msg.frame_index} 帧 | 处理速度 ${msg.fps} FPS` +
        ` | 预计剩余 ${formatRemaining(remaining)}（估算） | 阈值 ${msg.conf_threshold}`;
}

/* ---------------- 类别筛选 ---------------- */
function updateClassFilter(detections) {
    let changed = false;
    (detections || []).forEach((d) => {
        if (!knownClasses.has(d.class_name)) {
            knownClasses.set(d.class_name, d.class_id);
            changed = true;
        }
    });
    const signature = Array.from(knownClasses.keys()).sort().join('|');
    if (!changed && signature === lastFilterSignature) return;
    lastFilterSignature = signature;

    const container = $('#class-filter');
    if (!knownClasses.size) {
        container.innerHTML = '<span class="muted small">当前帧未检出目标</span>';
        return;
    }
    container.innerHTML = '<span class="chips-label">类别筛选:</span>' +
        Array.from(knownClasses.entries()).map(([name, id]) => {
            const active = !hiddenClasses.has(name);
            return `<span class="chip ${active ? 'active' : ''}" data-class="${escapeHtml(name)}">
                <span class="dot" style="background:${classColor(id)}"></span>${escapeHtml(name)}
            </span>`;
        }).join('');

    $$('.chip', container).forEach((chip) => {
        chip.addEventListener('click', () => {
            const name = chip.dataset.class;
            if (hiddenClasses.has(name)) {
                hiddenClasses.delete(name);
                chip.classList.add('active');
            } else {
                hiddenClasses.add(name);
                chip.classList.remove('active');
            }
            redrawCurrentFrame();
        });
    });
}

/** 筛选变化后按当前已渲染帧重绘检测框 */
function redrawCurrentFrame() {
    if (!renderedMsg || !canvasImage.complete) return;
    drawDetections($('#video-canvas'), canvasImage, renderedMsg.detections,
        { hidden: hiddenClasses, showId: true });
}

function handleDone(msg) {
    const s = msg.summary;
    $('#progress-bar').style.width = '100%';
    $('#progress-text').textContent = msg.result_video_url
        ? '检测完成: 结果已保存, 结果视频已导出'
        : '检测完成, 结果已保存到数据库';
    $('#summary-card').classList.remove('hidden');

    // 结果视频下载入口
    const dl = $('#btn-download-video');
    if (msg.result_video_url) {
        dl.href = msg.result_video_url;
        dl.classList.remove('hidden');
    } else {
        dl.classList.add('hidden');
    }

    // 汇总计数: 优先轨迹去重数, 其次单帧峰值 MaxN, 再次累计检出框 —— 三档都是可用数值
    const hasMaxn = (s.maxn_total || 0) > 0;
    $('#sum-unique').textContent = validUniqueCount(s)
        ? fmtNumber(s.unique_objects)
        : (hasMaxn ? fmtNumber(s.maxn_total) : fmtNumber(s.total_detections ?? 0));
    $('#sum-count-note').textContent = validUniqueCount(s)
        ? '按轨迹编号去重，仍可能存在重新入镜或跟踪丢失后的重复计数'
        : (hasMaxn
            ? '跨帧去重依据不完整，改以单帧峰值 MaxN 作为相对丰度口径'
            : '以累计检出框作为相对丰度口径，不做个体数推断');
    $('#sum-tags-title').textContent = validUniqueCount(s)
        ? '各类别跟踪轨迹数'
        : (hasMaxn ? '各类别单帧峰值 MaxN' : '各类别累计检测框数');
    showCountingStatus(s);
    $('#sum-detections').textContent = fmtNumber(s.total_detections ?? 0);
    $('#sum-frames').textContent = `${s.frames_processed} / ${s.total_frames}`;
    $('#sum-fps').textContent = s.avg_fps.toFixed(1);
    $('#sum-peak').textContent = s.peak_objects;
    $('#sum-avg').textContent = s.avg_objects;
    $('#sum-tags').innerHTML = s.counts.length
        ? s.counts.map((c) => `<span class="tag">${escapeHtml(c.class_name)}: <b>${c.count}</b></span>`).join('')
        : '<span class="muted small">全程未检出目标</span>';
    setControls('idle');
    toast(validUniqueCount(s)
        ? `视频分析完成：跟踪轨迹 ${s.unique_objects} 条`
        : (hasMaxn
            ? `视频分析完成：单帧峰值 MaxN ${s.maxn_total} 条（相对丰度）`
            : `视频分析完成：累计 ${s.total_detections ?? 0} 个检测框`), 'success');
}

function handleError(msg) {
    $('#alert-box').innerHTML =
        `<div class="alert alert-error">视频检测出错: ${escapeHtml(msg.message || '未知错误')}</div>`;
    toast('视频检测出错', 'error');
    $('#progress-text').textContent = '';
    setControls('idle');
}

/* ---------------- 暂停 / 继续 ---------------- */
async function controlVideo(action) {
    if (!currentTaskId) return;
    const taskId = currentTaskId;
    if (stopping) return;
    const wasPaused = paused;
    if (action === 'stop') showStoppingPreview();
    try {
        const st = await API.postJSON(
            `/api/detect/video/${taskId}/control`, { action }
        );
        if (currentTaskId !== taskId || (stopping && action !== 'stop')) return;
        if (action === 'stop') return;
        paused = !!st.paused;
        if (paused && !pauseStartedAt) pauseStartedAt = performance.now();
        if (!paused && pauseStartedAt) {
            pausedDuration += performance.now() - pauseStartedAt;
            pauseStartedAt = 0;
        }
        setControls(paused ? 'paused' : 'running');
        if (paused) {
            $('#progress-text').textContent =
                `已暂停（第 ${st.frame_index} 帧）| 点击“继续检测”从当前位置接着处理`;
            toast('已暂停检测', 'info', 1800);
        } else {
            $('#progress-text').textContent = '已继续检测...';
            toast('已继续检测', 'info', 1800);
        }
    } catch (err) {
        if (currentTaskId !== taskId) return;
        if (action === 'stop') {
            stopping = false;
            paused = wasPaused;
            setControls(paused ? 'paused' : 'running');
            $('#stage-placeholder').textContent = paused ? '任务仍在暂停，继续后恢复分析画面' : '等待检测画面更新';
            $('#progress-text').textContent = '停止失败，任务仍' + (paused ? '处于暂停状态' : '在运行') + '：' + err.message;
        }
        toast(err.message, 'error');
    }
}

/* ---------------- 初始化 ---------------- */
document.addEventListener('DOMContentLoaded', () => {
    bindUpload();
    bindSlider();
    setControls('idle');

    $('#btn-start').addEventListener('click', startDetect);
    $('#btn-pause').addEventListener('click', () => controlVideo('pause'));
    $('#btn-resume').addEventListener('click', () => controlVideo('resume'));
    $('#btn-stop').addEventListener('click', () => {
        controlVideo('stop');
    });

    window.addEventListener('beforeunload', (event) => {
        if (!uploading) return;
        event.preventDefault();
        event.returnValue = '';
    });
    // Closing a preview leaves server-owned detection running.
    window.addEventListener('pagehide', () => {
        leavingPage = true;
        if (reconnectTimer) clearTimeout(reconnectTimer);
        if (jobsTimer) clearInterval(jobsTimer);
        if (socket) socket.close();
    });
    $('#btn-refresh-jobs').addEventListener('click', () => loadVideoJobs());
    $('#video-jobs-list').addEventListener('click', (event) => {
        const button = event.target.closest('[data-view-job]');
        if (button) attachVideoJob(button.dataset.viewJob).catch((err) => toast(err.message, 'error'));
    });
    loadVideoJobs(true);
    jobsTimer = setInterval(() => { if (!document.hidden) loadVideoJobs(); }, 5000);

    API.get('/api/config').then((cfg) => {
        const slider = $('#conf-slider');
        if (!currentTaskId && !socket) {
            slider.value = cfg.conf_threshold;
            $('#conf-value').textContent = Number(cfg.conf_threshold).toFixed(2);
        }
        videoLimits.maxUploadMb = Number(cfg.max_upload_mb) || 300;
        if (Array.isArray(cfg.allowed_video_ext)) videoLimits.extensions = cfg.allowed_video_ext;
        $('#file-input').accept = videoLimits.extensions.join(',');
        $('#max-video-size').textContent = videoLimits.maxUploadMb;
    }).catch(() => {});
});
