/* 数据统计页: 概览 / 柱状图 / 折线图 / 类别趋势 / 时段分布 / 饼图 / 环形图 */

const charts = {};
let currentDays = 7;
let currentGranularity = 'day';
const PIE_COLORS = ['#0e7490', '#06b6d4', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6'];

function destroyChart(key) {
    if (charts[key]) {
        charts[key].destroy();
        charts[key] = null;
    }
}

/** 主色随主题变化(深色下用更亮的青色) */
function chartPrimary() {
    return isDarkTheme() ? '#22b8d8' : '#0e7490';
}

function renderTrend(trend) {
    const labels = trend.map((t) => t.label || t.day.slice(5));
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
                backgroundColor: chartPrimary(),
                borderRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { title: { display: true, text: '每期检测次数' } },
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
            plugins: { title: { display: true, text: '每期检出目标数量' } },
            scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
        },
    });
}

/* ---------------- 类别趋势(多折线) ---------------- */
function renderClassTrend(data) {
    destroyChart('classTrend');
    const labels = (data && data.labels) ? data.labels : [];
    const series = (data && data.series) ? data.series : [];
    charts.classTrend = new Chart($('#chart-class-trend').getContext('2d'), {
        type: 'line',
        data: {
            labels,
            datasets: series.map((s, i) => ({
                label: s.name,
                data: s.data,
                borderColor: PIE_COLORS[i % PIE_COLORS.length],
                backgroundColor: 'transparent',
                borderWidth: 2,
                tension: 0.3,
                pointRadius: 3,
            })),
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                title: {
                    display: true,
                    text: series.length ? '各类别检出数量随时间变化' : '暂无类别趋势数据',
                },
                legend: { position: 'bottom' },
            },
            scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
        },
    });
}

/* ---------------- 检测时段分布(24 小时) ---------------- */
function renderHourly(data) {
    destroyChart('hourly');
    const rows = data || [];
    const labels = rows.map((h) => `${String(h.hour).padStart(2, '0')}:00`);
    const counts = rows.map((h) => h.count);
    const max = counts.length ? Math.max(...counts) : 0;
    charts.hourly = new Chart($('#chart-hourly').getContext('2d'), {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: '检测次数',
                data: counts,
                backgroundColor: counts.map((c) => (c === max && max > 0 ? '#f59e0b' : chartPrimary())),
                borderRadius: 3,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: { display: true, text: '全天各时段检测次数(橙色为最活跃时段)' },
                legend: { display: false },
            },
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

async function loadDashboard(days = 7, granularity = 'day') {
    currentDays = days;
    currentGranularity = granularity;
    applyChartTheme();
    try {
        const d = await API.get(`/api/stats/dashboard?days=${days}&granularity=${granularity}`);

        $('#stat-today').textContent = fmtNumber(d.overview.today_count);
        $('#stat-today-objects').textContent = `今日检出目标 ${fmtNumber(d.overview.today_objects)} 个`;
        $('#stat-total').textContent = fmtNumber(d.overview.total_count);
        $('#stat-total-detail').textContent = `图片 ${d.overview.image_count} / 视频 ${d.overview.video_count}`;
        $('#stat-objects').textContent = fmtNumber(d.overview.total_objects);
        $('#stat-classes').textContent = fmtNumber(d.overview.class_count);
        $('#stat-avg').textContent = `平均推理耗时 ${d.overview.avg_inference_ms} ms`;

        renderTrend(d.trend);
        renderClassTrend(d.class_trend);
        renderHourly(d.hourly);
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

/* ---------------- 生态多样性指标 ---------------- */

let ecologyDays = 0;

function ecoFmt(v, digits = 4) {
    return (v === null || v === undefined) ? '—' : Number(v).toFixed(digits);
}

function renderEcology(payload) {
    const d = payload || {};
    const interp = d.interpretation || {};

    $('#eco-shannon').textContent = ecoFmt(d.shannon);
    $('#eco-shannon-note').textContent = interp.shannon_text || '—';
    $('#eco-simpson').textContent = ecoFmt(d.simpson);

    $('#eco-pielou').textContent = ecoFmt(d.pielou);
    $('#eco-pielou-note').textContent = interp.evenness_text || '—';

    $('#eco-margalef').textContent = ecoFmt(d.margalef);
    $('#eco-margalef-note').textContent = d.total
        ? `类别 ${d.richness} 种 / 个体 ${fmtNumber(d.total)} 个` : '暂无数据';

    $('#eco-dominant').textContent = interp.dominant_text || '优势种: —';
    $('#eco-total').textContent = `个体总数: ${fmtNumber(d.total || 0)}`;
    $('#eco-richness').textContent = `类别数: ${d.richness || 0}`;
    $('#ecology-scope').textContent = ecologyDays
        ? `统计范围: 最近 ${ecologyDays} 天` : '统计范围: 全部历史';

    // 帕累托图: 柱=各类数量, 折线=累计占比
    const abundance = d.abundance || [];
    destroyChart('abundance');
    if (abundance.length) {
        charts.abundance = new Chart($('#chart-abundance').getContext('2d'), {
            data: {
                labels: abundance.map((a) => a.class_name),
                datasets: [
                    {
                        type: 'bar',
                        label: '检出数量',
                        data: abundance.map((a) => a.count),
                        backgroundColor: chartPrimary(),
                        borderRadius: 4,
                        yAxisID: 'y',
                    },
                    {
                        type: 'line',
                        label: '累计占比 %',
                        data: abundance.map((a) => a.cumulative_ratio),
                        borderColor: '#f59e0b',
                        backgroundColor: 'rgba(245, 158, 11, .12)',
                        tension: 0.25,
                        pointRadius: 3,
                        yAxisID: 'y1',
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { title: { display: true, text: '类别丰度 (柱) 与累计占比 (线)' } },
                scales: {
                    y: { beginAtZero: true, position: 'left', ticks: { precision: 0 } },
                    y1: {
                        beginAtZero: true, position: 'right', max: 100,
                        grid: { drawOnChartArea: false },
                        ticks: { callback: (v) => v + '%' },
                    },
                },
            },
        });
    }
}

async function renderEcologyTrend(days) {
    const safeDays = Math.max(2, Math.min(days || 14, 60));
    let data;
    try {
        data = await API.get(`/api/stats/ecology-trend?days=${safeDays}`);
    } catch (err) {
        return;
    }
    const points = (data && data.points) || [];
    destroyChart('ecoTrend');
    charts.ecoTrend = new Chart($('#chart-eco-trend').getContext('2d'), {
        data: {
            labels: points.map((p) => p.label),
            datasets: [
                {
                    type: 'line',
                    label: "Shannon H'",
                    data: points.map((p) => p.shannon),
                    borderColor: chartPrimary(),
                    backgroundColor: 'rgba(14, 116, 144, .12)',
                    tension: 0.3,
                    pointRadius: 3,
                    spanGaps: true,
                    yAxisID: 'y',
                },
                {
                    type: 'line',
                    label: "Pielou J'",
                    data: points.map((p) => p.pielou),
                    borderColor: '#22c55e',
                    backgroundColor: 'rgba(34, 197, 94, .12)',
                    tension: 0.3,
                    pointRadius: 3,
                    spanGaps: true,
                    yAxisID: 'y1',
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: { display: true, text: '多样性指数随时间变化(无数据的日期断开)' },
            },
            scales: {
                y: { beginAtZero: true, position: 'left', title: { display: true, text: "H'" } },
                y1: {
                    beginAtZero: true, max: 1, position: 'right',
                    grid: { drawOnChartArea: false },
                    title: { display: true, text: "J'" },
                },
            },
        },
    });
}

async function loadEcology(days) {
    ecologyDays = days || 0;
    try {
        const scope = ecologyDays > 0 ? `?days=${ecologyDays}` : '?days=0';
        const data = await API.get('/api/stats/ecology' + scope);
        renderEcology(data);
    } catch (err) {
        $('#alert-box').innerHTML =
            `<div class="alert alert-error">生态指标加载失败: ${escapeHtml(err.message)}</div>`;
    }
    await renderEcologyTrend(Math.max(ecologyDays || 0, 14));
}

document.addEventListener('DOMContentLoaded', () => {
    if (typeof Chart === 'undefined') {
        $('#alert-box').innerHTML =
            '<div class="alert alert-error">Chart.js 未加载, 请检查 web/vendor/chart.js 是否存在</div>';
    }
    loadDashboard(7, 'day');
    loadEcology(0);

    $('#trend-days').addEventListener('change', (e) => {
        const [d, g] = String(e.target.value).split(':');
        loadDashboard(parseInt(d, 10), g || 'day');
    });

    $('#ecology-days').addEventListener('change', (e) => {
        loadEcology(parseInt(e.target.value, 10) || 0);
    });

    // Chart.js 配色不会自动跟随 CSS 变量, 主题切换后重绘图表
    onThemeChange(() => {
        loadDashboard(currentDays, currentGranularity);
        loadEcology(ecologyDays);
    });
});
