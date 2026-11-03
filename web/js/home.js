/* 首页: 统计概览 + 模型信息 + 最近记录 */

async function loadOverview() {
    try {
        const d = await API.get('/api/stats/overview');
        $('#stat-today').textContent = fmtNumber(d.today_count);
        $('#stat-today-objects').textContent = `今日检出目标 ${fmtNumber(d.today_objects)} 个`;
        $('#stat-total').textContent = fmtNumber(d.total_count);
        $('#stat-total-detail').textContent = `图片 ${d.image_count} 次 / 视频 ${d.video_count} 次`;
        $('#stat-objects').textContent = fmtNumber(d.total_objects);
        $('#stat-classes').textContent = fmtNumber(d.class_count);
        $('#stat-device').textContent = `平均推理耗时 ${d.avg_inference_ms} ms`;
    } catch (err) {
        toast('统计数据加载失败: ' + err.message, 'error');
    }
}

async function loadModelInfo() {
    const tbody = $('#model-info-body');
    try {
        const info = await API.get('/api/model/info');
        const rows = [
            ['模型文件', info.model_name || '未配置'],
            ['模型状态', info.model_exists ? (info.loaded ? '已加载' : '待加载') : '文件不存在'],
            ['推理设备', String(info.device).toUpperCase()],
            ['输入尺寸', `${info.imgsz} px`],
            ['置信度阈值', info.conf_threshold],
            ['类别数量', `${info.class_count} 类`],
            ['类别名称', info.class_count
                ? info.classes.map((c) => c.name).join('、')
                : '加载模型后自动读取'],
        ];
        tbody.innerHTML = rows.map(([k, v]) =>
            `<tr><th style="width:110px;">${escapeHtml(k)}</th><td>${escapeHtml(v)}</td></tr>`
        ).join('');

        if (!info.model_exists) {
            $('#alert-box').innerHTML =
                '<div class="alert alert-error">未检测到模型权重文件。请将训练好的 .pt 文件放入项目 <span class="mono">models/</span> 目录后刷新页面。</div>';
        }
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="2" class="empty">模型信息加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
}

async function loadRecent() {
    const tbody = $('#recent-body');
    try {
        const rows = await API.get('/api/stats/recent?limit=6');
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty">暂无检测记录, 先去上传一张图片试试</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map((r) => `
            <tr>
                <td>${escapeHtml(r.created_at)}</td>
                <td>${escapeHtml(r.source_name)}</td>
                <td>${mediaTypeBadge(r.task_type)}</td>
                <td>${fmtNumber(r.total_objects)}</td>
                <td class="muted">${r.inference_ms !== null && r.inference_ms !== undefined && Number.isFinite(Number(r.inference_ms)) ? `${Number(r.inference_ms).toFixed(1)} ms${r.task_type === 'video' ? '/帧' : ''}` : '—'}</td>
            </tr>`).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty">加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    loadOverview();
    loadModelInfo();
    loadRecent();
});
