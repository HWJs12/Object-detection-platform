/* 检测记录页: 筛选 / 分页 / 详情 / 删除 */

const state = { page: 1, page_size: 10 };

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
            tbody.innerHTML = '<tr><td colspan="7" class="empty">暂无符合条件的记录</td></tr>';
            $('#pagination').innerHTML = '';
            return;
        }

        tbody.innerHTML = data.items.map((r) => {
            const isVideo = r.task_type === 'video';
            const cost = isVideo
                ? `${r.frames_processed ?? 0} 帧 / ${(r.avg_fps ?? 0).toFixed(1)} FPS`
                : `${(r.inference_ms ?? 0).toFixed(0)} ms`;
            return `<tr>
                <td>${r.id}</td>
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
        $$('#records-body [data-delete]').forEach((btn) => {
            btn.addEventListener('click', () => removeRecord(btn.dataset.delete));
        });

        renderPagination(data.page, data.total_pages);
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty">加载失败: ${escapeHtml(err.message)}</td></tr>`;
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

    const preview = record.result_url
        ? `<img class="preview" src="${record.result_url}" alt="检测结果">`
        : '<p class="muted small">该记录无结果图(视频检测仅保存统计数据)</p>';

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
                <span>目标数量: <b>${record.total_objects}</b></span>
                ${isVideo ? `<span>帧数: <b>${record.frames_processed ?? 0}</b></span>
                             <span>平均速度: <b>${(record.avg_fps ?? 0).toFixed(1)} FPS</b></span>
                             <span>时长: <b>${record.duration_sec ?? 0}s</b></span>`
                        : `<span>推理耗时: <b>${(record.inference_ms ?? 0).toFixed(0)} ms</b></span>
                           <span>分辨率: <b>${record.image_width ?? '-'} × ${record.image_height ?? '-'}</b></span>`}
                <span>置信度阈值: <b>${record.conf_threshold ?? '-'}</b></span>
            </div>
            <h3 style="font-size:14px;margin:16px 0 8px;">检测结果图</h3>
            ${preview}
            <h3 style="font-size:14px;margin:16px 0 8px;">类别统计</h3>
            <div class="tag-list">${classTags}</div>
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

    const close = () => modal.remove();
    $('#modal-close', modal).addEventListener('click', close);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) close();
    });
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
