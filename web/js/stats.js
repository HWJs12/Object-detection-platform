/* 数据统计页: 概览 / 柱状图 / 折线图 / 饼图 / 环形图 */

const charts = {};
const PIE_COLORS = ['#0e7490', '#06b6d4', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6'];

function destroyChart(key) {
    if (charts[key]) {
        charts[key].destroy();
        charts[key] = null;
    }
}

function renderTrend(trend) {
    const labels = trend.map((t) => t.day.slice(5));
    const counts = trend.map((t) => t.count);
    const objects = trend.map((t) => t.objects);

    destroyChart('bar');
    destroyChart('line');

    charts.bar = new Chart($('#chart-bar').getContext('2d'), {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: '检测次数',
                data: counts,
                backgroundColor: '#0e7490',
                borderRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { title: { display: true, text: '每日检测次数' } },
            scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
        },
    });

    charts.line = new Chart($('#chart-line').getContext('2d'), {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: '检出目标数',
                data: objects,
                borderColor: '#06b6d4',
                backgroundColor: 'rgba(6, 182, 212, .15)',
                fill: true,
                tension: 0.3,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { title: { display: true, text: '每日检出目标数量' } },
            scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
        },
    });
}

function renderClasses(classes) {
    destroyChart('pie');
    const labels = classes.map((c) => c.class_name);
    const data = classes.map((c) => c.count);
    charts.pie = new Chart($('#chart-pie').getContext('2d'), {
        type: 'pie',
        data: {
            labels,
            datasets: [{
                data,
                backgroundColor: labels.map((_, i) => PIE_COLORS[i % PIE_COLORS.length]),
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'right' },
                title: { display: true, text: labels.length ? '各类别目标占比' : '暂无类别数据' },
            },
        },
    });

    const tags = $('#class-tags');
    tags.innerHTML = classes.length
        ? classes.map((c) => `<span class="tag">${escapeHtml(c.class_name)}: <b>${c.count}</b> (${c.ratio}%)</span>`).join('')
        : '<span class="muted small">暂无数据, 请先进行检测</span>';
}

function renderTypes(types) {
    destroyChart('doughnut');
    const map = {};
    types.forEach((t) => { map[t.task_type] = t.count; });
    charts.doughnut = new Chart($('#chart-doughnut').getContext('2d'), {
        type: 'doughnut',
        data: {
            labels: ['图片检测', '视频检测'],
            datasets: [{
                data: [map.image || 0, map.video || 0],
                backgroundColor: ['#0e7490', '#8b5cf6'],
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { position: 'bottom' }, title: { display: true, text: '检测类型分布' } },
        },
    });
}

async function loadDashboard(days = 7) {
    try {
        const d = await API.get(`/api/stats/dashboard?days=${days}`);

        $('#stat-today').textContent = fmtNumber(d.overview.today_count);
        $('#stat-today-objects').textContent = `今日检出目标 ${fmtNumber(d.overview.today_objects)} 个`;
        $('#stat-total').textContent = fmtNumber(d.overview.total_count);
        $('#stat-total-detail').textContent = `图片 ${d.overview.image_count} / 视频 ${d.overview.video_count}`;
        $('#stat-objects').textContent = fmtNumber(d.overview.total_objects);
        $('#stat-classes').textContent = fmtNumber(d.overview.class_count);
        $('#stat-avg').textContent = `平均推理耗时 ${d.overview.avg_inference_ms} ms`;

        renderTrend(d.trend);
        renderClasses(d.classes);
        renderTypes(d.types);

        const tbody = $('#recent-body');
        tbody.innerHTML = d.recent.length
            ? d.recent.map((r) => `
                <tr>
                    <td>${escapeHtml(r.created_at)}</td>
                    <td>${escapeHtml(r.source_name)}</td>
                    <td><span class="badge ${r.task_type === 'video' ? 'video' : ''}">${r.task_type === 'video' ? '视频' : '图片'}</span></td>
                    <td>${fmtNumber(r.total_objects)}</td>
                    <td><span class="badge ${r.status === 'success' ? '' : 'gray'}">${r.status === 'success' ? '成功' : '失败'}</span></td>
                </tr>`).join('')
            : '<tr><td colspan="5" class="empty">暂无检测记录</td></tr>';
    } catch (err) {
        $('#alert-box').innerHTML =
            `<div class="alert alert-error">统计数据加载失败: ${escapeHtml(err.message)}</div>`;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (typeof Chart === 'undefined') {
        $('#alert-box').innerHTML =
            '<div class="alert alert-error">Chart.js 未加载, 请检查 web/vendor/chart.js 是否存在</div>';
    }
    loadDashboard(7);
    $('#trend-days').addEventListener('change', (e) => loadDashboard(parseInt(e.target.value, 10)));
});
