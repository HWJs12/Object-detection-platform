/* 图片检测页: 批量上传 -> 检测 -> Canvas 绘框(支持类别筛选) */

let selectedFiles = [];
let results = [];                    // 后端返回的结果项
const hiddenClasses = new Set();     // 被隐藏的类别(仅影响显示)
const imageCache = new Map();        // 下标 -> 已加载的原图

/* ---------------- 文件选择 ---------------- */
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
        if (e.dataTransfer.files.length) selectFiles(e.dataTransfer.files);
    });
    input.addEventListener('change', () => {
        if (input.files.length) selectFiles(input.files);
    });
}

function selectFiles(fileList) {
    const files = Array.from(fileList).filter((f) => f.type.startsWith('image/'));
    if (!files.length) {
        toast('请选择图片文件', 'error');
        return;
    }
    const maxBatch = Number($('#max-batch').textContent) || 20;
    if (files.length > maxBatch) {
        toast(`单次最多检测 ${maxBatch} 张, 已保留前 ${maxBatch} 张`, 'info');
        files.length = maxBatch;
    }
    selectedFiles = files;
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
    btn.disabled = !enabled;
    btn.textContent = text || '下载检测报告 (PDF)';
}

async function downloadReport() {
    const okItems = results.filter((it) => !it.error && it.record_id);
    if (!okItems.length) {
        toast('本次没有可生成报告的检测结果', 'error');
        return;
    }
    const ids = okItems.map((it) => it.record_id);
    setReportButton(false, `正在生成 ${ids.length} 张报告...`);
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
        toast(`报告已生成: ${ids.length} 张影像, 耗时 ${cost} ms`, 'success');
    } catch (err) {
        toast(err.message || '报告生成失败', 'error');
    } finally {
        setReportButton(true);
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
    btn.disabled = !usable || !chk.checked;
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
async function runDetect() {
    if (!selectedFiles.length) {
        toast('请先选择图片', 'error');
        return;
    }
    const btn = $('#btn-detect');
    btn.disabled = true;
    $('#progress-text').textContent = '正在上传并推理, 首次加载模型会稍慢...';
    $('#alert-box').innerHTML = '';
    const started = performance.now();

    const formData = new FormData();
    selectedFiles.forEach((f) => formData.append('files', f));
    formData.append('conf', $('#conf-slider').value);

    try {
        const data = await API.upload('/api/detect/images', formData, (p) => {
            $('#progress-text').textContent = `上传进度 ${p}%`;
        });
        const cost = Math.round(performance.now() - started);
        renderResults(data, cost);
        toast(`检测完成: 成功 ${data.success_count} 张, 共识别 ${data.total_objects} 个目标`, 'success');
    } catch (err) {
        $('#alert-box').innerHTML = `<div class="alert alert-error">检测失败: ${escapeHtml(err.message)}</div>`;
        toast('检测失败', 'error');
    } finally {
        btn.disabled = false;
        $('#progress-text').textContent = '';
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
        imageCache.set(index, img);
        const canvas = document.createElement('canvas');
        wrap.innerHTML = '';
        wrap.appendChild(canvas);
        drawDetections(canvas, img, it.detections, { hidden: hiddenClasses });
    };
    img.onerror = () => { wrap.innerHTML = '<div class="err">原图加载失败</div>'; };
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
    $('#btn-detect').addEventListener('click', runDetect);
    $('#btn-report').addEventListener('click', downloadReport);
    $('#chk-export-tags').addEventListener('change', toggleExportRow);
    $('#btn-export-tags').addEventListener('click', downloadLabels);
    $('#btn-reset').addEventListener('click', () => {
        selectedFiles = [];
        $('#file-input').value = '';
        $('#file-name').classList.add('hidden');
        $('#btn-detect').disabled = true;
        $('#alert-box').innerHTML = '';
        resetResults();
    });

    const slider = $('#conf-slider');
    slider.addEventListener('input', () => {
        $('#conf-value').textContent = parseFloat(slider.value).toFixed(2);
    });

    API.get('/api/config').then((cfg) => {
        slider.value = cfg.conf_threshold;
        $('#conf-value').textContent = Number(cfg.conf_threshold).toFixed(2);
        $('#max-size').textContent = cfg.max_upload_mb;
        $('#max-batch').textContent = cfg.max_batch_images;
    }).catch(() => {});
});
