/* 检测记录页: 筛选 / 分页 / 详情(含结果视频与图表) / 删除 */

const state = { page: 1, page_size: 10 };
const PIE_COLORS = ['#0e7490', '#06b6d4', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6'];
let detailCharts = [];

/** 关闭详情时销毁其中的图表, 避免 canvas 复用冲突与内存泄漏 */
function destroyDetailCharts() {
    detailCharts.forEach((c) => { try { c.destroy(); } catch (e) { /* ignore */ } });
    detailCharts = [];
}

function buildQuery() {
    const params = new URLSearchParams({
        page: String(state.page),
        page_size: String(state.page_size),
        task_type: $('#filter-type').value,
        keyword: $('#filter-keyword').value.trim(),
        date_from: $('#filter-from').value,
        date_to: $('#filter-to').value,
    });
    return `/api/records?${params.toString()}`;
}

async function loadRecords() {
    const tbody = $('#records-body');
    try {
        const data = await API.get(buildQuery());
        $('#record-count').textContent = `共 ${data.total} 条记录`;

        if (!data.items.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无符合条件的记录</td></tr>';
            $('#pagination').innerHTML = '';
            return;
        }

        tbody.innerHTML = data.items.map((r) => {
            const isVideo = r.task_type === 'video';
            const cost = isVideo
                ? `${r.frames_processed ?? 0} 帧 / ${(r.avg_fps ?? 0).toFixed(1)} FPS`
                : `${(r.inference_ms ?? 0).toFixed(0)} ms`;
            const thumb = r.result_url
                ? `<img class="thumb" src="${escapeHtml(r.result_url)}" data-preview="${r.id}"
                        alt="结果预览" title="点击查看大图">`
                : '<div class="thumb-none" title="视频检测仅保存统计数据">视频</div>';
            return `<tr>
                <td>${r.id}</td>
                <td>${thumb}</td>
                <td>${escapeHtml(r.created_at)}</td>
                <td>${escapeHtml(r.source_name)}</td>
                <td><span class="badge ${isVideo ? 'video' : ''}">${isVideo ? '视频' : '图片'}</span></td>
                <td>${fmtNumber(r.total_objects)}</td>
                <td class="small muted">${cost}</td>
                <td>
                    <button class="btn btn-sm" data-detail="${r.id}">详情</button>
                    <button class="btn btn-sm btn-danger" data-delete="${r.id}">删除</button>
                </td>
            </tr>`;
        }).join('');

        $$('#records-body [data-detail]').forEach((btn) => {
            btn.addEventListener('click', () => openDetail(btn.dataset.detail));
        });
        $$('#records-body [data-preview]').forEach((img) => {
            img.addEventListener('click', () => openDetail(img.dataset.preview));
        });
        $$('#records-body [data-delete]').forEach((btn) => {
            btn.addEventListener('click', () => removeRecord(btn.dataset.delete));
        });

        renderPagination(data.page, data.total_pages);
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" class="empty">加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function renderPagination(page, totalPages) {
    const box = $('#pagination');
    if (totalPages <= 1) {
        box.innerHTML = `<span class="small muted">第 ${page} / ${totalPages} 页</span>`;
        return;
    }
    let html = `<button class="btn btn-sm btn-secondary" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>`;
    html += `<span class="small muted">第 ${page} / ${totalPages} 页</span>`;
    html += `<button class="btn btn-sm btn-secondary" data-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''}>下一页</button>`;
    box.innerHTML = html;
    $$('#pagination [data-page]').forEach((btn) => {
        btn.addEventListener('click', () => {
            state.page = parseInt(btn.dataset.page, 10);
            loadRecords();
        });
    });
}

async function openDetail(id) {
    let record;
    try {
        record = await API.get(`/api/records/${id}`);
    } catch (err) {
        toast('详情加载失败: ' + err.message, 'error');
        return;
    }

    const isVideo = record.task_type === 'video';
    const classTags = (record.class_stats || []).map(
        (c) => `<span class="tag">${escapeHtml(c.class_name)}: <b>${c.count}</b></span>`
    ).join('') || '<span class="muted small">无</span>';

    const detailRows = (record.items || []).slice(0, 30).map((it, i) => `
        <tr>
            <td>${i + 1}</td>
            <td>${isVideo ? `第 ${it.frame_index} 帧` : '-'}</td>
            <td>${escapeHtml(it.class_name)}</td>
            <td>${(it.confidence * 100).toFixed(1)}%</td>
            <td class="mono small">${[it.x1, it.y1, it.x2, it.y2].map((v) => (v === null ? '-' : v.toFixed(0))).join(', ')}</td>
        </tr>`).join('') || '<tr><td colspan="5" class="empty">无明细数据</td></tr>';

    let frameTable = '';
    if (isVideo && (record.frame_stats || []).length) {
        frameTable = `
            <h3 style="font-size:14px;margin:16px 0 8px;">逐帧目标数量(采样)</h3>
            <div class="table-wrap" style="max-height:220px;overflow:auto;">
                <table>
                    <thead><tr><th>帧号</th><th>时间 (s)</th><th>目标数量</th></tr></thead>
                    <tbody>${record.frame_stats.map((f) => `
                        <tr><td>${f.frame_index}</td><td>${f.timestamp_sec}</td><td>${f.total_objects}</td></tr>
                    `).join('')}</tbody>
                </table>
            </div>`;
    }

    // 结果展示: 视频记录优先播放结果视频, 图片记录显示结果图
    let media;
    if (record.result_video_url) {
        media = `<div class="modal-media">
            <video class="preview" controls preload="metadata"
                   src="${escapeHtml(record.result_video_url)}"></video>
        </div>
        <p style="margin:10px 0 0;">
            <a class="btn btn-sm" href="${escapeHtml(record.result_video_url)}" download>⬇ 下载结果视频</a>
        </p>`;
    } else if (record.result_url) {
        media = `<img class="preview" src="${escapeHtml(record.result_url)}" alt="检测结果">`;
    } else {
        media = '<p class="muted small">该记录无结果图 / 结果视频(历史记录, 或导出功能已关闭)</p>';
    }

    const chartHtml = `
        <div class="grid-2" style="margin-top:18px;">
            <div>
                <h3 style="font-size:14px;margin:0 0 8px;">类别分布</h3>
                <div class="chart-box" style="height:220px;"><canvas id="detail-chart-classes"></canvas></div>
            </div>
            ${isVideo ? `<div>
                <h3 style="font-size:14px;margin:0 0 8px;">逐帧目标数量</h3>
                <div class="chart-box" style="height:220px;"><canvas id="detail-chart-frames"></canvas></div>
            </div>` : ''}
        </div>`;

    destroyDetailCharts();
    const modal = document.createElement('div');
    modal.className = 'modal-mask';
    modal.innerHTML = `
        <div class="modal">
            <div class="modal-header">
                <h3>记录详情 #${record.id} - ${escapeHtml(record.source_name)}</h3>
                <button class="modal-close" id="modal-close">×</button>
            </div>
            <div class="summary-line">
                <span>类型: <b>${isVideo ? '视频' : '图片'}</b></span>
                <span>时间: <b>${escapeHtml(record.created_at)}</b></span>
                <span>唯一目标数: <b>${record.unique_objects ?? record.total_objects}</b></span>
                ${isVideo ? `<span>累计检测框: <b>${record.total_detections ?? '-'}</b></span>
                             <span>帧数: <b>${record.frames_processed ?? 0}</b></span>
                             <span>平均速度: <b>${(record.avg_fps ?? 0).toFixed(1)} FPS</b></span>
                             <span>时长: <b>${record.duration_sec ?? 0}s</b></span>`
                        : `<span>推理耗时: <b>${(record.inference_ms ?? 0).toFixed(0)} ms</b></span>
                           <span>分辨率: <b>${record.image_width ?? '-'} × ${record.image_height ?? '-'}</b></span>`}
                <span>置信度阈值: <b>${record.conf_threshold ?? '-'}</b></span>
            </div>
            <h3 style="font-size:14px;margin:16px 0 8px;">检测结果</h3>
            ${media}
            <h3 style="font-size:14px;margin:16px 0 8px;">类别统计</h3>
            <div class="tag-list">${classTags}</div>
            <h3 style="font-size:14px;margin:16px 0 8px;">生态多样性指标</h3>
            <div id="detail-ecology" class="summary-line" style="border:none;padding:0;">
                <span class="muted small">计算中...</span>
            </div>
            ${chartHtml}
            ${frameTable}
            <h3 style="font-size:14px;margin:16px 0 8px;">检测明细(最多显示 30 条)</h3>
            <div class="table-wrap" style="max-height:240px;overflow:auto;">
                <table>
                    <thead><tr><th>#</th><th>帧</th><th>类别</th><th>置信度</th><th>边界框</th></tr></thead>
                    <tbody>${detailRows}</tbody>
                </table>
            </div>
        </div>`;
    $('#modal-root').appendChild(modal);

    // 详情内图表(Chart.js, 配色跟随主题)
    if (typeof Chart !== 'undefined') {
        applyChartTheme();
        const cls = record.class_stats || [];
        if (cls.length) {
            detailCharts.push(new Chart($('#detail-chart-classes', modal), {
                type: 'doughnut',
                data: {
                    labels: cls.map((c) => c.class_name),
                    datasets: [{
                        data: cls.map((c) => c.count),
                        backgroundColor: cls.map((_, i) => PIE_COLORS[i % PIE_COLORS.length]),
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'bottom' } },
                },
            }));
        }
        const fsRows = record.frame_stats || [];
        if (isVideo && fsRows.length) {
            detailCharts.push(new Chart($('#detail-chart-frames', modal), {
                type: 'line',
                data: {
                    labels: fsRows.map((f) => f.timestamp_sec),
                    datasets: [{
                        label: '目标数量',
                        data: fsRows.map((f) => f.total_objects),
                        borderColor: isDarkTheme() ? '#22b8d8' : '#0e7490',
                        backgroundColor: 'rgba(6, 182, 212, .15)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 0,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        title: { display: true, text: '逐帧目标数量(按时间)' },
                    },
                    scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
                },
            }));
        }
    }

    const close = () => { destroyDetailCharts(); modal.remove(); };
    $('#modal-close', modal).addEventListener('click', close);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) close();
    });

    renderDetailEcology(record, modal);
}

/** 在详情弹窗内展示该条记录的生态指标。
 *
 * 直接复用后端同一套公式(见 app/core/ecology.py), 但用已有的 class_stats 就地计算,
 * 省一次请求。公式必须与后端保持一致, 否则同一条记录在两处显示不同数值。
 */
function renderDetailEcology(record, modal) {
    const box = $('#detail-ecology', modal);
    if (!box) return;

    const counts = (record.class_stats || [])
        .map((c) => ({ name: c.class_name, n: Number(c.count) || 0 }))
        .filter((c) => c.n > 0)
        .sort((a, b) => b.n - a.n);

    const total = counts.reduce((s, c) => s + c.n, 0);
    if (!total) {
        box.innerHTML = '<span class="muted small">该记录无检出目标, 无法计算生态指标</span>';
        return;
    }

    const S = counts.length;
    const props = counts.map((c) => c.n / total);
    const shannon = -props.reduce((s, p) => s + (p > 0 ? p * Math.log(p) : 0), 0);
    const simpson = 1 - props.reduce((s, p) => s + p * p, 0);
    const pielou = S > 1 ? shannon / Math.log(S) : null;
    const margalef = total > 1 ? (S - 1) / Math.log(total) : null;
    const dom = counts[0];

    const fmt = (v, d = 4) => (v === null ? '—' : v.toFixed(d));

    box.innerHTML = `
        <span>Shannon H': <b>${fmt(shannon)}</b></span>
        <span>Simpson D: <b>${fmt(simpson)}</b></span>
        <span>Pielou J': <b>${fmt(pielou)}</b></span>
        <span>Margalef: <b>${fmt(margalef)}</b></span>
        <span>优势种: <b>${escapeHtml(dom.name)}</b>
              (${(dom.n / total * 100).toFixed(1)}%)</span>
        <span>类别数: <b>${S}</b> / 个体总数: <b>${total}</b></span>`;
}

async function removeRecord(id) {
    if (!confirm(`确认删除记录 #${id} ? 该操作不可恢复。`)) return;
    try {
        await API.del(`/api/records/${id}`);
        toast('记录已删除', 'success');
        loadRecords();
    } catch (err) {
        toast('删除失败: ' + err.message, 'error');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    loadRecords();
    $('#btn-search').addEventListener('click', () => {
        state.page = 1;
        loadRecords();
    });
    $('#btn-clear').addEventListener('click', () => {
        $('#filter-type').value = '';
        $('#filter-keyword').value = '';
        $('#filter-from').value = '';
        $('#filter-to').value = '';
        state.page = 1;
        loadRecords();
    });
    $('#filter-keyword').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            state.page = 1;
            loadRecords();
        }
    });
});
