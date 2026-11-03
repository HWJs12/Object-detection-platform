/* 图片检测页: 批量上传 -> 检测 -> Canvas 绘框(支持类别筛选) */

let selectedFiles = [];
let results = [];                    // 后端返回的结果项
const hiddenClasses = new Set();     // 被隐藏的类别(仅影响显示)
const imageCache = new Map();        // 下标 -> 已加载的原图
let detectionBusy = false;
let detectionRequest = 0;
let imageQueue = [];
let queueSnapshot = null;
let queueStartedAt = 0;
let queueElapsedMs = 0;

function newQueue(files) {
    imageQueue = files.map((file, index) => ({
        file, index, key: crypto.randomUUID(), state: 'queued', progress: 0,
        data: null, error: '',
    }));
    queueSnapshot = null;
    queueElapsedMs = 0;
}

function renderQueue() {
    const panel = $('#queue-card');
    if (!panel) return;
    panel.classList.toggle('hidden', !imageQueue.length);
    const labels = { queued: '等待中', running: '处理中', success: '已完成', failed: '失败', cancelled: '已取消' };
    $('#queue-body').innerHTML = imageQueue.map((item) => `<tr>
        <td>${item.index + 1}</td><td class="queue-filename">${escapeHtml(item.file.name)}</td>
        <td><span class="queue-state ${item.state}" role="status">${labels[item.state]}</span>
            ${item.state === 'running' ? `<span class="small muted">${item.progress < 100 ? ` 上传 ${item.progress}%` : ' 推理中'}</span>` : ''}
            ${item.error ? `<div class="small queue-error">${escapeHtml(item.error)}</div>` : ''}</td>
        <td>${item.state === 'queued' ? `<button class="btn btn-sm btn-secondary" data-cancel-image="${item.index}">取消</button>` : '—'}</td>
    </tr>`).join('');
    const n = (state) => imageQueue.filter((item) => item.state === state).length;
    $('#queue-status').textContent = `已完成 ${n('success')} / 失败 ${n('failed')} / 已取消 ${n('cancelled')} / 等待 ${n('queued')}`;
    $('#btn-cancel-pending').disabled = !n('queued');
    $('#btn-retry-failed').disabled = detectionBusy || !n('failed');
    if (!detectionBusy) {
        $('#btn-detect').disabled = !n('queued') && !n('success');
        $('#btn-detect').textContent = n('success') && !n('queued') ? '重新检测全部' : '开始检测';
    }
}

function cancelQueued(index = null) {
    imageQueue.forEach((item) => {
        if (item.state === 'queued' && (index === null || item.index === index)) item.state = 'cancelled';
    });
    renderQueue();
}

function renderQueueResults() {
    const settled = imageQueue.filter((item) => item.state === 'success' || item.state === 'failed');
    if (!settled.length) return;
    renderResults({
        items: settled.map((item) => item.data || { source_name: item.file.name, error: item.error, total: 0 }),
        total_images: imageQueue.length,
        success_count: imageQueue.filter((item) => item.state === 'success').length,
        fail_count: imageQueue.filter((item) => item.state === 'failed').length,
        total_objects: imageQueue.reduce((total, item) => total + (item.data ? item.data.total : 0), 0),
    }, Math.round(queueElapsedMs + (detectionBusy ? performance.now() - queueStartedAt : 0)));
}

const imageLimits = {
    maxUploadMb: 300, maxBatch: 20,
    extensions: ['.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff'],
};

function setDetectionBusy(busy) {
    detectionBusy = busy;
    $('#btn-detect').disabled = busy || !selectedFiles.length;
    $('#btn-reset').disabled = busy;
    $('#file-input').disabled = busy;
    $('#conf-slider').disabled = busy;
    $('#upload-area').setAttribute('aria-disabled', String(busy));
    $$('.survey-field input, .survey-field select').forEach((input) => { input.disabled = busy; input.dataset.locked = busy ? '1' : '0'; });
    renderQueue();
}

/* ---------------- 文件选择 ---------------- */
function bindUpload() {
    const area = $('#upload-area');
    const input = $('#file-input');

    area.addEventListener('click', () => { if (!detectionBusy) input.click(); });
    area.addEventListener('dragover', (e) => {
        e.preventDefault();
        area.classList.add('dragover');
    });
    area.addEventListener('dragleave', () => area.classList.remove('dragover'));
    area.addEventListener('drop', (e) => {
        e.preventDefault();
        area.classList.remove('dragover');
        if (e.dataTransfer.files.length) selectFiles(e.dataTransfer.files);
    });
    input.addEventListener('change', () => {
        if (input.files.length) selectFiles(input.files);
    });
}

function selectFiles(fileList) {
    if (detectionBusy) {
        toast('图片正在检测，请完成后再选择文件', 'info');
        return;
    }
    const selected = Array.from(fileList);
    const files = selected.filter((f) => imageLimits.extensions.includes((/\.[^.]+$/.exec(f.name)?.[0] || '').toLowerCase()));
    if (!files.length) {
        toast('请选择支持的 JPG / PNG / BMP / WEBP / TIFF 图片', 'error');
        return;
    }
    if (files.length !== selected.length) toast(`已忽略 ${selected.length - files.length} 个不支持的文件`, 'info');
    if (files.some((file) => !file.size || file.size > imageLimits.maxUploadMb * 1024 * 1024)) {
        toast(`图片不能为空，且每张不能超过 ${imageLimits.maxUploadMb} MB`, 'error');
        return;
    }
    const maxBatch = imageLimits.maxBatch;
    if (files.length > maxBatch) {
        toast(`单次最多检测 ${maxBatch} 张, 已保留前 ${maxBatch} 张`, 'info');
        files.length = maxBatch;
    }
    selectedFiles = files;
    newQueue(files);
    renderQueue();
    const totalMb = files.reduce((s, f) => s + f.size, 0) / 1024 / 1024;
    $('#file-name').textContent = files.length === 1
        ? `已选择: ${files[0].name} (${(files[0].size / 1024).toFixed(1)} KB)`
        : `已选择 ${files.length} 张图片, 共 ${totalMb.toFixed(2)} MB`;
    $('#file-name').classList.remove('hidden');
    $('#btn-detect').disabled = false;
    $('#alert-box').innerHTML = '';
    resetResults();
}

function resetResults() {
    results = [];
    imageCache.clear();
    hiddenClasses.clear();
    $('#summary-card').classList.add('hidden');
    $('#filter-card').classList.add('hidden');
    $('#results-card').classList.add('hidden');
    $('#result-grid').innerHTML = '';
    $('#class-filter').innerHTML = '';
    setReportButton(false);
    syncExportState();
}

/* ---------------- 下载检测报告 ----------------
   按需生成: 仅点击时才请求后端合成 PDF, 不在检测完成后自动生成, 避免占用内存。
   报告覆盖本次上传的全部图片(按 record_id 顺序), 后端流式返回, 不在服务端落盘。 */
function setReportButton(enabled, text) {
    const btn = $('#btn-report');
    if (!btn) return;
    btn.disabled = detectionBusy || !enabled;
    btn.textContent = text || '成功图片报告 (PDF)';
    btn.title = `仅包含本次成功检测的 ${exportableIds().length} 张图片；失败项不包含在报告中`;
}

async function downloadReport() {
    const okItems = results.filter((it) => !it.error && it.record_id);
    if (!okItems.length) {
        toast('本次没有可生成报告的检测结果', 'error');
        return;
    }
    const ids = okItems.map((it) => it.record_id);
    setReportButton(false, `正在生成 ${ids.length} 张成功图片报告...`);
    const started = performance.now();

    try {
        const resp = await fetch('/api/report/images', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ record_ids: ids }),
        });
        if (!resp.ok) {
            // 失败时后端返回 JSON 错误体, 尝试解析出可读信息
            let msg = `报告生成失败 (HTTP ${resp.status})`;
            try {
                const payload = await resp.json();
                if (payload && payload.message) msg = payload.message;
            } catch (e) { /* 保持默认信息 */ }
            throw new Error(msg);
        }
        const blob = await resp.blob();
        const cost = Math.round(performance.now() - started);

        // 从响应头解析文件名(优先 RFC 5987 的 filename*)
        const filename = parseFilename(resp.headers.get('content-disposition')) ||
            `检测报告_${ids.length}张.pdf`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // 释放内存: 触发下载后立即回收 Blob URL
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        toast(`成功图片报告已生成: ${ids.length} 张，失败项不包含在内，耗时 ${cost} ms`, 'success');
    } catch (err) {
        toast(err.message || '报告生成失败', 'error');
    } finally {
        setReportButton(exportableIds().length > 0);
    }
}

/* ---------------- 导出训练标签 ----------------
   交互: 先勾选「导出标签」, 再选择格式并下载。
   后端按 record_ids 取标注数据后流式返回, 不在服务端落盘。 */
const EXPORT_LABELS = { csv: 'CSV 标注表', coco: 'COCO JSON', yolo: 'YOLO 标签包' };

/** 本次可导出的记录 ID(仅成功且已落库的) */
function exportableIds() {
    return results.filter((it) => !it.error && it.record_id).map((it) => it.record_id);
}

function isExportChecked() {
    const chk = $('#chk-export-tags');
    return !!(chk && chk.checked);
}

/** 同步导出区状态: 无可用结果时强制收起; 按钮仅在"已勾选且有结果"时可用 */
function syncExportState() {
    const chk = $('#chk-export-tags');
    const row = $('#export-format-row');
    const btn = $('#btn-export-tags');
    if (!chk || !row || !btn) return;

    const usable = exportableIds().length > 0;
    if (!usable) {
        chk.checked = false;
        row.classList.add('hidden');
    }
    btn.disabled = detectionBusy || !usable || !chk.checked;
}

/** 勾选/取消「导出标签」时展开或收起格式选择 */
function toggleExportRow() {
    const row = $('#export-format-row');
    if (row) row.classList.toggle('hidden', !isExportChecked());
    syncExportState();
}

async function downloadLabels() {
    const ids = exportableIds();
    if (!ids.length) {
        toast('本次没有可导出的检测结果', 'error');
        return;
    }
    const fmt = $('#export-format').value;
    const btn = $('#btn-export-tags');
    const oldText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '导出中...';

    try {
        const resp = await fetch('/api/report/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ record_ids: ids, format: fmt }),
        });
        if (!resp.ok) {
            let msg = `标签导出失败 (HTTP ${resp.status})`;
            try {
                const payload = await resp.json();
                if (payload && payload.message) msg = payload.message;
            } catch (e) { /* 保持默认信息 */ }
            throw new Error(msg);
        }
        const blob = await resp.blob();
        const fallbackExt = fmt === 'csv' ? 'csv' : (fmt === 'coco' ? 'json' : 'zip');
        const filename = parseFilename(resp.headers.get('content-disposition')) ||
            `检测标签_${ids.length}张.${fallbackExt}`;

        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // 释放内存: 触发下载后立即回收 Blob URL
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        toast(`已导出 ${ids.length} 张影像的${EXPORT_LABELS[fmt] || fmt}`, 'success');
    } catch (err) {
        toast(err.message || '标签导出失败', 'error');
    } finally {
        btn.textContent = oldText;
        syncExportState();
    }
}

/** 解析 Content-Disposition 中的文件名, 优先 filename*=UTF-8''... */
function parseFilename(disposition) {
    if (!disposition) return '';
    const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    if (star && star[1]) {
        try { return decodeURIComponent(star[1].trim()); } catch (e) { /* 忽略 */ }
    }
    const plain = /filename="?([^";]+)"?/i.exec(disposition);
    return plain && plain[1] ? plain[1].trim() : '';
}

/* ---------------- 执行检测 ---------------- */
async function runDetect(retryFailed = false) {
    if (detectionBusy) return;
    // Event listeners pass a MouseEvent; only explicit true means retry.
    retryFailed = retryFailed === true;
    if (!selectedFiles.length) { toast('请先选择图片', 'error'); return; }
    if (typeof requireSurveyStation === 'function' && !requireSurveyStation()) return;
    if (retryFailed) {
        imageQueue.forEach((item) => {
            if (item.state === 'failed') { item.state = 'queued'; item.error = ''; item.progress = 0; }
        });
    } else if (!imageQueue.some((item) => item.state === 'queued')) {
        if (!imageQueue.some((item) => item.state === 'success')) return;
        newQueue(selectedFiles);
    }
    if (!queueSnapshot) {
        const survey = new FormData();
        appendSurvey(survey);
        queueSnapshot = { confidence: $('#conf-slider').value, survey: Array.from(survey.entries()) };
    }
    const requestId = ++detectionRequest;
    queueStartedAt = performance.now();
    if (!retryFailed) resetResults();
    setDetectionBusy(true);
    $('#alert-box').innerHTML = '';
    try {
        for (const item of imageQueue) {
            if (item.state !== 'queued') continue;
            item.state = 'running';
            renderQueue();
            const form = new FormData();
            form.append('file', item.file);
            form.append('conf', queueSnapshot.confidence);
            form.append('request_key', item.key);
            queueSnapshot.survey.forEach(([key, value]) => form.append(key, value));
            $('#progress-text').textContent = `正在处理第 ${item.index + 1} / ${imageQueue.length} 张`;
            try {
                item.data = await API.upload('/api/detect/image', form, (progress) => {
                    if (requestId !== detectionRequest) return;
                    item.progress = progress;
                    renderQueue();
                });
                item.state = 'success';
                item.error = '';
            } catch (err) {
                item.state = 'failed';
                item.error = err.message || '检测失败';
            }
            renderQueue();
            renderQueueResults();
        }
    } finally {
        if (requestId === detectionRequest) {
            queueElapsedMs += performance.now() - queueStartedAt;
            setDetectionBusy(false);
            renderQueueResults();
            renderQueue();
            $('#progress-text').textContent = '本轮处理结束；失败图片可单独重试，已完成图片不会重复提交';
            toast('图片队列处理结束', imageQueue.some((item) => item.state === 'failed') ? 'warning' : 'success');
        }
    }
}

/* ---------------- 结果渲染 ---------------- */
function renderResults(data, costMs) {
    resetResults();
    results = data.items || [];

    $('#sum-images').textContent = data.total_images;
    $('#sum-objects').textContent = fmtNumber(data.total_objects);
    $('#sum-ok').textContent = `${data.success_count} / ${data.fail_count}`;
    $('#sum-cost').textContent = `${costMs} ms`;
    $('#summary-card').classList.remove('hidden');

    const grid = $('#result-grid');
    grid.innerHTML = '';
    results.forEach((it, index) => {
        const card = document.createElement('div');
        card.className = 'result-card' + (it.error ? ' is-error' : '');
        const classes = (it.counts || [])
            .map((c) => `<span class="mini-tag">${escapeHtml(c.class_name)} ×${c.count}</span>`).join('');
        card.innerHTML = `
            <div class="thumb-wrap"></div>
            <div class="info">
                <div class="name">
                    <span>${escapeHtml(it.source_name || '')}</span>
                    <span class="small muted">${it.error ? '失败' : it.total + ' 个'}</span>
                </div>
                <div class="tags">${classes || '<span class="mini-tag">无检出</span>'}</div>
            </div>`;
        grid.appendChild(card);

        const wrap = card.querySelector('.thumb-wrap');
        if (it.error) {
            wrap.innerHTML = `<div class="err">${escapeHtml(it.error)}</div>`;
        } else {
            loadThumb(index, wrap);
            card.addEventListener('click', () => openDetail(index));
        }
    });

    const allDets = results.flatMap((it) => it.detections || []);
    if (allDets.length) {
        renderClassFilter($('#class-filter'), allDets, hiddenClasses, redrawAll);
        $('#filter-card').classList.remove('hidden');
    }
    $('#results-card').classList.remove('hidden');
    // 有可用的成功结果时才允许下载报告(报告在点击时才生成)
    setReportButton(results.some((it) => !it.error && it.record_id));
    syncExportState();
}

function loadThumb(index, wrap) {
    const it = results[index];
    const img = new Image();
    img.onload = () => {
        if (results[index] !== it) return;
        imageCache.set(index, img);
        const canvas = document.createElement('canvas');
        wrap.innerHTML = '';
        wrap.appendChild(canvas);
        drawDetections(canvas, img, it.detections, { hidden: hiddenClasses });
    };
    img.onerror = () => {
        if (results[index] === it) wrap.innerHTML = '<div class="err">原图加载失败</div>';
    };
    img.src = it.original_url;
}

/** 类别筛选变化后重绘所有缩略图(不重建 DOM) */
function redrawAll() {
    results.forEach((it, index) => {
        if (it.error) return;
        const img = imageCache.get(index);
        if (!img) return;
        const card = $('#result-grid').children[index];
        const canvas = card && card.querySelector('canvas');
        if (canvas) drawDetections(canvas, img, it.detections, { hidden: hiddenClasses });
    });
}

/* ---------------- 详情弹窗 ---------------- */
function openDetail(index) {
    const it = results[index];
    if (!it || it.error) return;
    const img = imageCache.get(index);

    const detailRows = (it.detections || []).map((d, i) => {
        const [x1, y1, x2, y2] = d.bbox;
        return `<tr>
            <td>${i + 1}</td>
            <td>${escapeHtml(d.class_name)}</td>
            <td>${(d.confidence * 100).toFixed(1)}%</td>
            <td class="mono small">${x1.toFixed(0)}, ${y1.toFixed(0)}, ${x2.toFixed(0)}, ${y2.toFixed(0)}</td>
            <td class="small muted">${(x2 - x1).toFixed(0)} × ${(y2 - y1).toFixed(0)}</td>
        </tr>`;
    }).join('') || '<tr><td colspan="5" class="empty">未检出目标</td></tr>';

    const modal = document.createElement('div');
    modal.className = 'modal-mask';
    modal.innerHTML = `
        <div class="modal">
            <div class="modal-header">
                <h3>${escapeHtml(it.source_name || '')}</h3>
                <button class="modal-close" id="modal-close">×</button>
            </div>
            <div class="summary-line" style="margin-bottom:12px;">
                <span>检出目标: <b>${it.total}</b></span>
                <span>推理耗时: <b>${it.inference_ms} ms</b></span>
                <span>分辨率: <b>${it.width} × ${it.height}</b></span>
                <span>阈值: <b>${it.conf_threshold}</b></span>
            </div>
            <div class="modal-media"><canvas id="modal-canvas"></canvas></div>
            <h3 style="font-size:14px;margin:16px 0 8px;">检测明细</h3>
            <div class="table-wrap" style="max-height:260px;overflow:auto;">
                <table>
                    <thead><tr><th>#</th><th>类别</th><th>置信度</th>
                        <th>边界框 (x1, y1, x2, y2)</th><th>框尺寸</th></tr></thead>
                    <tbody>${detailRows}</tbody>
                </table>
            </div>
        </div>`;
    $('#modal-root').appendChild(modal);

    const canvas = $('#modal-canvas', modal);
    if (img) drawDetections(canvas, img, it.detections, { hidden: hiddenClasses });

    const close = () => modal.remove();
    $('#modal-close', modal).addEventListener('click', close);
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
}

/* ---------------- 初始化 ---------------- */
document.addEventListener('DOMContentLoaded', () => {
    bindUpload();
    $('#btn-detect').addEventListener('click', () => runDetect());
    $('#btn-retry-failed').addEventListener('click', () => runDetect(true));
    $('#btn-cancel-pending').addEventListener('click', () => cancelQueued());
    $('#queue-body').addEventListener('click', (event) => {
        const button = event.target.closest('[data-cancel-image]');
        if (button) cancelQueued(Number(button.dataset.cancelImage));
    });
    $('#btn-report').addEventListener('click', downloadReport);
    $('#chk-export-tags').addEventListener('change', toggleExportRow);
    $('#btn-export-tags').addEventListener('click', downloadLabels);
    $('#btn-reset').addEventListener('click', () => {
        if (detectionBusy) return;
        detectionRequest += 1;
        selectedFiles = [];
        imageQueue = [];
        queueSnapshot = null;
        renderQueue();
        $('#file-input').value = '';
        $('#file-name').classList.add('hidden');
        $('#btn-detect').disabled = true;
        $('#alert-box').innerHTML = '';
        resetResults();
    });

    window.addEventListener('beforeunload', (event) => {
        if (!detectionBusy) return;
        event.preventDefault();
        event.returnValue = '';
    });

    const slider = $('#conf-slider');
    slider.addEventListener('input', () => {
        $('#conf-value').textContent = parseFloat(slider.value).toFixed(2);
    });

    API.get('/api/config').then((cfg) => {
        if (!detectionBusy && !queueSnapshot) {
            slider.value = cfg.conf_threshold;
            $('#conf-value').textContent = Number(cfg.conf_threshold).toFixed(2);
        }
        $('#max-size').textContent = cfg.max_upload_mb;
        $('#max-batch').textContent = cfg.max_batch_images;
        imageLimits.maxUploadMb = Number(cfg.max_upload_mb) || 300;
        imageLimits.maxBatch = Number(cfg.max_batch_images) || 20;
        if (Array.isArray(cfg.allowed_image_ext)) imageLimits.extensions = cfg.allowed_image_ext;
        $('#file-input').accept = imageLimits.extensions.join(',');
    }).catch(() => {});
});
