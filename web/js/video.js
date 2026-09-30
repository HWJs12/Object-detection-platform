/* 视频检测页: 上传 -> WebSocket 实时逐帧检测(支持暂停/继续/运行中调阈值) */

let currentFile = null;
let socket = null;
let currentTaskId = null;
let paused = false;
let peak = 0;
let pendingFrame = false;   // 上一帧是否渲染完成, 未完成时丢弃新帧避免堆积
const canvasImage = new Image();

/* ---------------- 按钮状态机: idle | running | paused ---------------- */
function setControls(state) {
    $('#btn-start').disabled = !(state === 'idle' && !!currentFile);
    $('#btn-pause').disabled = state !== 'running';
    $('#btn-resume').disabled = state !== 'paused';
    $('#btn-stop').classList.toggle('hidden', state === 'idle');
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
    if (!file.type.startsWith('video/')) {
        toast('请选择视频文件', 'error');
        return;
    }
    currentFile = file;
    $('#file-name').textContent = `已选择: ${file.name} (${(file.size / 1024 / 1024).toFixed(2)} MB)`;
    $('#file-name').classList.remove('hidden');
    $('#video-meta').textContent = '';
    $('#alert-box').innerHTML = '';
    $('#summary-card').classList.add('hidden');
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
    peak = 0;
    $('#live-total').textContent = '0';
    $('#live-fps').textContent = '0.0';
    $('#live-time').textContent = '0.0 s';
    $('#live-peak').textContent = '0';
    $('#live-tags').innerHTML = '<span class="muted small">暂无数据</span>';
    $('#progress-bar').style.width = '0%';
    $('#progress-text').textContent = '';
}

/* ---------------- 开始检测 ---------------- */
async function startDetect() {
    if (!currentFile) {
        toast('请先选择视频', 'error');
        return;
    }
    $('#btn-start').disabled = true;
    $('#progress-text').textContent = '正在上传视频...';
    $('#alert-box').innerHTML = '';
    $('#summary-card').classList.add('hidden');
    resetLiveStats();

    let taskId;
    try {
        const formData = new FormData();
        formData.append('file', currentFile);
        formData.append('conf', $('#conf-slider').value);
        const data = await API.upload('/api/detect/video', formData, (p) => {
            $('#progress-text').textContent = `上传进度 ${p}%`;
        });
        taskId = data.task_id;
        currentTaskId = taskId;
        $('#video-meta').textContent =
            `分辨率 ${data.meta.width}×${data.meta.height} | ${data.meta.fps.toFixed(1)} FPS | ` +
            `共 ${data.meta.total_frames} 帧 | 时长 ${data.meta.duration_sec}s | ` +
            `阈值 ${data.config.conf_threshold}`;
    } catch (err) {
        $('#alert-box').innerHTML = `<div class="alert alert-error">上传失败: ${escapeHtml(err.message)}</div>`;
        setControls('idle');
        $('#progress-text').textContent = '';
        return;
    }

    openSocket(taskId);
}

/* ---------------- WebSocket ---------------- */
function openSocket(taskId) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${proto}://${location.host}/ws/video/${taskId}`);

    socket.onopen = () => {
        paused = false;
        setControls('running');
        $('#progress-text').textContent = '正在逐帧检测...';
    };

    socket.onmessage = (event) => {
        let msg;
        try {
            msg = JSON.parse(event.data);
        } catch (err) {
            return;
        }
        if (msg.type === 'frame') handleFrame(msg);
        else if (msg.type === 'done') handleDone(msg);
        else if (msg.type === 'error') handleError(msg);
    };

    socket.onerror = () => handleError({ message: 'WebSocket 连接异常, 请重试' });

    socket.onclose = () => {
        socket = null;
        currentTaskId = null;
        paused = false;
        setControls('idle');
    };
}

function handleFrame(msg) {
    // 渲染画面(丢弃未及时显示的帧, 保证实时性)
    if (!pendingFrame) {
        pendingFrame = true;
        canvasImage.onload = () => {
            const canvas = $('#video-canvas');
            if (canvas.width !== canvasImage.width) {
                canvas.width = canvasImage.width;
                canvas.height = canvasImage.height;
            }
            canvas.getContext('2d').drawImage(canvasImage, 0, 0);
            canvas.classList.remove('hidden');
            $('#stage-placeholder').classList.add('hidden');
            pendingFrame = false;
        };
        canvasImage.onerror = () => { pendingFrame = false; };
        canvasImage.src = 'data:image/jpeg;base64,' + msg.image;
    }

    $('#live-total').textContent = msg.total;
    $('#live-fps').textContent = msg.fps.toFixed(1);
    $('#live-time').textContent = msg.timestamp.toFixed(1) + ' s';
    peak = Math.max(peak, msg.total);
    $('#live-peak').textContent = peak;

    $('#live-tags').innerHTML = msg.counts.length
        ? msg.counts.map((c) => `<span class="tag">${escapeHtml(c.class_name)}: <b>${c.count}</b></span>`).join('')
        : '<span class="muted small">当前帧未检出目标</span>';

    $('#progress-bar').style.width = `${Math.min(100, msg.progress)}%`;
    $('#progress-text').textContent =
        `进度 ${msg.progress}% | 第 ${msg.frame_index} 帧 | 检测速度 ${msg.fps} FPS` +
        ` | 阈值 ${msg.conf_threshold}`;
}

function handleDone(msg) {
    const s = msg.summary;
    $('#progress-bar').style.width = '100%';
    $('#progress-text').textContent = '检测完成, 结果已保存到数据库';
    $('#summary-card').classList.remove('hidden');
    $('#sum-frames').textContent = `${s.frames_processed} / ${s.total_frames}`;
    $('#sum-fps').textContent = s.avg_fps.toFixed(1);
    $('#sum-peak').textContent = s.peak_objects;
    $('#sum-avg').textContent = s.avg_objects;
    $('#sum-tags').innerHTML = s.counts.length
        ? s.counts.map((c) => `<span class="tag">${escapeHtml(c.class_name)}: <b>${c.count}</b></span>`).join('')
        : '<span class="muted small">全程未检出目标</span>';
    setControls('idle');
    toast('视频检测完成', 'success');
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
    try {
        const st = await API.postJSON(
            `/api/detect/video/${currentTaskId}/control`, { action }
        );
        paused = !!st.paused;
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
        if (socket) {
            socket.close();
            toast('已停止检测', 'info');
            $('#progress-text').textContent = '已手动停止（本次不保存记录）';
        }
    });

    window.addEventListener('beforeunload', () => {
        if (socket) socket.close();
    });

    API.get('/api/config').then((cfg) => {
        const slider = $('#conf-slider');
        slider.value = cfg.conf_threshold;
        $('#conf-value').textContent = Number(cfg.conf_threshold).toFixed(2);
    }).catch(() => {});
});
