/* 数据统计页: 概览 / 柱状图 / 折线图 / 类别趋势 / 时段分布 / 饼图 / 环形图 */

const charts = {};
let currentDays = 7;
let currentGranularity = 'day';
let dashboardRequest = 0;
let ecologyRequest = 0;
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
                label: '记录检出计数',
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
            plugins: { title: { display: true, text: '每期记录检出计数（重复处理会累加）' } },
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
                spanGaps: false,
            })),
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                title: {
                    display: true,
                    text: series.length ? `各类别检出计数（按${data.granularity === 'week' ? '周' : '日'}，缺测日期留空）` : '暂无类别趋势数据',
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
    const scope = currentDays > 0
        ? `最近 ${currentDays} ${currentGranularity === 'week' ? '周' : '天'}` : '全部历史';
    $('#class-scope').textContent = scope;
    $('#class-detail-scope').textContent = scope;
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
                title: { display: true, text: labels.length ? `${scope}各类别检出计数占比` : `${scope}暂无类别数据` },
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
            plugins: { legend: { position: 'bottom' }, title: { display: true, text: '全部历史检测类型分布' } },
        },
    });
}

async function loadDashboard(days = 7, granularity = 'day') {
    const requestId = ++dashboardRequest;
    currentDays = days;
    currentGranularity = granularity;
    applyChartTheme();
    ['bar', 'line', 'classTrend', 'hourly', 'pie', 'doughnut'].forEach(destroyChart);
    ['stat-today', 'stat-today-objects', 'stat-total', 'stat-total-detail', 'stat-objects',
        'stat-classes', 'stat-avg'].forEach((id) => { $('#' + id).textContent = '—'; });
    $('#class-tags').textContent = '加载中...';
    $('#recent-body').innerHTML = '<tr><td colspan="5" class="empty">加载中...</td></tr>';
    $('#dashboard-error').textContent = '正在加载统计数据...';
    try {
        const d = await API.get(`/api/stats/dashboard?days=${days}&granularity=${granularity}`);
        if (requestId !== dashboardRequest) return;
        $('#dashboard-error').innerHTML = '';

        $('#stat-today').textContent = fmtNumber(d.overview.today_count);
        $('#stat-today-objects').textContent = `今日记录检出计数 ${fmtNumber(d.overview.today_objects)}`;
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
                    <td>${mediaTypeBadge(r.task_type)}</td>
                    <td>${fmtNumber(r.total_objects)}</td>
                    <td>${recordStatusBadge(r.status)}</td>
                </tr>`).join('')
            : '<tr><td colspan="5" class="empty">暂无检测记录</td></tr>';
    } catch (err) {
        if (requestId !== dashboardRequest) return;
        $('#dashboard-error').innerHTML =
            `<div class="alert alert-error">统计数据加载失败: ${escapeHtml(err.message)}</div>`;
    }
}

/* ---------------- 生态多样性指标 ---------------- */

// 时间选项统一成 {days, hours} 双口径: days 走自然日, hours 走滚动窗口。
// 不能把「最近 24 小时」塞成 days=24 —— 后端会读成最近 24 天, 图表标签也会写错。
let ecologyDays = 0;
let ecologyHours = 0;

/** 把 select 的 value 解析成时间口径; "24h" 是滚动 24 小时, 数字是自然日 */
function parseEcologyScope(value) {
    const raw = String(value === undefined || value === null ? '0' : value).trim();
    if (raw === '24h') return { days: 0, hours: 24 };
    const days = parseInt(raw, 10);
    return { days: Number.isFinite(days) && days > 0 ? days : 0, hours: 0 };
}

/** 当前口径的中文描述, 同时用于 scope 文案与图表标题 */
function ecologyScopeText() {
    if (ecologyHours > 0) return `最近 ${ecologyHours} 小时`;
    return ecologyDays > 0 ? `最近 ${ecologyDays} 天` : '全部历史';
}

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
        ? `检出类别 ${d.richness} 类 / 检出框 ${fmtNumber(d.total)}` : '暂无数据';

    $('#eco-dominant').textContent = interp.dominant_text || '检出最多类别: —';
    $('#eco-total').textContent = `去重图片检出框数: ${fmtNumber(d.total || 0)}`;
    $('#eco-richness').textContent = `类别数: ${d.richness || 0}`;
    $('#ecology-scope').textContent = (ecologyHours > 0 || ecologyDays)
        ? `最新图片检测日期：${ecologyScopeText()}` : '最新图片检测日期：全部历史';

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

async function renderEcologyTrend(days, requestId, hours) {
    const safeHours = Math.max(0, Math.min(Number(hours) || 0, 168));
    const safeDays = Math.max(2, Math.min(days || 14, 60));
    const query = safeHours > 0 ? `hours=${safeHours}` : `days=${safeDays}`;
    let data;
    try {
        data = await API.get(`/api/stats/ecology-trend?${query}`);
    } catch (err) {
        if (requestId === ecologyRequest) {
            destroyChart('ecoTrend');
            $('#ecology-error').textContent = `类别分布趋势加载失败：${err.message}`;
        }
        return;
    }
    if (requestId !== ecologyRequest) return;
    const points = (data && data.points) || [];
    // 小时口径下 24 个桶如果仍标注"天的缺测断开"会误导, 这里按实际粒度写标题
    const title = safeHours > 0
        ? `最近 ${safeHours} 小时的图片类别分布（按小时分桶, 缺测小时断开）`
        : `最近 ${safeDays} 天的图片类别分布（缺测日期断开）`;
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
                    spanGaps: false,
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
                    spanGaps: false,
                    yAxisID: 'y1',
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: { display: true, text: title },
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

async function loadEcology(scopeValue) {
    const requestId = ++ecologyRequest;
    const scope = parseEcologyScope(scopeValue);
    ecologyDays = scope.days;
    ecologyHours = scope.hours;
    $('#ecology-error').textContent = '正在加载去重图片类别分布...';
    ['abundance', 'ecoTrend'].forEach(destroyChart);
    ['eco-shannon', 'eco-shannon-note', 'eco-simpson', 'eco-pielou', 'eco-pielou-note',
        'eco-margalef', 'eco-margalef-note', 'eco-dominant', 'eco-total', 'eco-richness']
        .forEach((id) => { $('#' + id).textContent = '—'; });
    try {
        const query = ecologyHours > 0 ? `?hours=${ecologyHours}` : `?days=${ecologyDays}`;
        const data = await API.get('/api/stats/ecology' + query);
        if (requestId !== ecologyRequest) return;
        $('#ecology-error').innerHTML = '';
        renderEcology(data);
    } catch (err) {
        if (requestId !== ecologyRequest) return;
        $('#ecology-error').innerHTML =
            `<div class="alert alert-error">类别分布指标加载失败: ${escapeHtml(err.message)}</div>`;
    }
    // 趋势图在全部历史口径下仍按 14 天渲染(趋势必须有时间轴); 24 小时口径走小时桶
    await renderEcologyTrend(ecologyDays || 14, requestId, ecologyHours);
}

/* ---------------- 下钻: 统计页的"这个数怎么来的" ---------------- */

/** 概览卡片的通用下钻: 逐条列出构成该数字的记录 */
function overviewDrill(kind, label) {
    return {
        title: label,
        caliber: `overview.${kind}`,
        loader: async () => {
            const records = await API.get('/api/records?page=1&page_size=50&task_type=');
            const rows = (records.items || []);
            const isToday = kind === 'today_count' || kind === 'today_objects';
            const today = new Date();
            const pad = (n) => String(n).padStart(2, '0');
            const todayStr = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
            const list = isToday ? rows.filter((r) => String(r.created_at).startsWith(todayStr)) : rows;
            return {
                summary: [
                    { label: '本页原始记录', value: String(list.length) },
                    { label: '记录总数', value: String(records.total ?? list.length) },
                    { label: '展示口径', value: '最近 50 条' },
                ],
                note: '下钻用于核对口径而非全量导出, 因此只取最近 50 条记录; '
                    + '要查看任意记录的完整构成, 请到“检测记录”页打开详情。',
                columns: ['记录号', '时间', '文件', '类型', '计数', '口径'],
                rows: list.map((r) => [
                    `<a href="records.html" data-record-link="${r.id}">#${r.id}</a>`,
                    escapeHtml(r.created_at),
                    escapeHtml(r.source_name),
                    r.task_type === 'video' ? '视频' : '图片',
                    fmtNumber(r.effective_count ?? r.total_objects),
                    escapeHtml(r.effective_count_source || '-'),
                ]),
                truncated: true,
            };
        },
    };
}

function stationDisplayLabel(site) {
    return `${site.site_label}${site.legacy_site && site.site ? '（历史站点）' : ''}`;
}

/** 站点维度的下钻: 站点 -> 样方 -> 记录 */
function siteDrill(site, transects, days) {
    return {
        title: `站点「${stationDisplayLabel(site)}」`,
        caliber: 'site.objects',
        loader: async () => ({
            summary: [
                { label: '站点', value: stationDisplayLabel(site) },
                { label: '记录数', value: String(site.records) },
                { label: '检出计数', value: fmtNumber(site.objects) },
                { label: '样方数', value: String((transects || []).length) },
            ],
            note: '站点值由各记录 total_objects 相加得到; 未做面积或努力量归一。',
            columns: ['样方 / 断面', '记录数', '检出计数', '单帧峰值合计', '累计检出框'],
            rows: (transects || []).map((t) => [
                escapeHtml(t.transect_label),
                fmtNumber(t.records),
                fmtNumber(t.objects),
                fmtNumber(t.maxn_peak),
                fmtNumber(t.detections),
            ]),
            children: (transects || []).map((t) => ({
                label: `查看「${t.transect_label}」的记录`,
                drill: transectDrill(site, t, days),
            })),
        }),
    };
}

/** 样方 -> 具体记录列表(最终落到可点击的记录) */
function transectDrill(site, transect, days) {
    return {
        title: `${stationDisplayLabel(site)} / ${transect.transect_label}`,
        loader: async () => {
            const params = new URLSearchParams({ page: '1', page_size: '50', task_type: '' });
            if (days && Number(days) > 0) {
                const from = new Date();
                from.setDate(from.getDate() - (Number(days) - 1));
                const pad = (n) => String(n).padStart(2, '0');
                params.set('date_from',
                    `${from.getFullYear()}-${pad(from.getMonth() + 1)}-${pad(from.getDate())}`);
            }
            if (site.site_id !== null && site.site_id !== undefined) params.set('site_id', String(site.site_id));
            const data = await API.get(`/api/records?${params.toString()}`);
            const isUnlabeledSite = !site.site;
            const isUnlabeledTransect = !transect.transect;
            const list = (data.items || []).filter((r) => (
                (site.site_id !== null && site.site_id !== undefined
                    ? Number(r.site_id) === Number(site.site_id)
                    : r.site_id == null && (isUnlabeledSite ? !r.site : String(r.site || '').trim() === site.site))
                && (isUnlabeledTransect ? !r.transect : r.transect === transect.transect)
            ));
            return {
                summary: [
                    { label: '匹配记录', value: String(list.length) },
                    { label: '检出计数', value: fmtNumber(list.reduce(
                        (s, r) => s + Number(r.effective_count ?? r.total_objects ?? 0), 0)) },
                ],
                note: '记录列表来自最近 50 条检测记录的再筛选, 更早的记录请到“检测记录”页按日期查询。',
                columns: ['记录号', '时间', '文件', '类型', '计数', '口径'],
                rows: list.map((r) => [
                    `<a href="records.html" data-record-link="${r.id}">#${r.id}</a>`,
                    escapeHtml(r.created_at),
                    escapeHtml(r.source_name),
                    r.task_type === 'video' ? '视频' : '图片',
                    fmtNumber(r.effective_count ?? r.total_objects),
                    escapeHtml(r.effective_count_source || '-'),
                ]),
                truncated: true,
            };
        },
    };
}

/* ---------------- 今日检测数据 ---------------- */

let todayHours = 24;
let todayRequest = 0;

function renderToday(today) {
    const d = today || {};
    $('#today-records').textContent = fmtNumber(d.records || 0);
    $('#today-since').textContent = d.since ? `自 ${d.since} 起` : '-';
    $('#today-objects').textContent = fmtNumber(d.objects || 0);
    $('#today-objects-note').textContent = `${d.hours || 24} 小时内成功记录的检出计数之和`;
    $('#today-detections').textContent = fmtNumber(d.detections || 0);
    const sites = d.by_site || [];
    $('#today-sites').textContent = String(sites.length);

    // 时段产出: 按小时补齐, 缺的小时显式补 0 而不是让折线跳过去
    const hours = d.by_hour || [];
    destroyChart('todayHour');
    charts.todayHour = new Chart($('#chart-today-hour').getContext('2d'), {
        type: 'bar',
        data: {
            labels: hours.map((h) => `${String(h.hour).padStart(2, '0')}:00`),
            datasets: [{
                label: '检出计数',
                data: hours.map((h) => h.objects),
                backgroundColor: chartPrimary(),
                borderRadius: 3,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false },
                title: { display: true, text: `最近 ${d.hours || 24} 小时各时段检出计数` } },
            scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
        },
    });

    // 计数口径构成: 让"这个数来自哪套口径"一眼可见
    const MODE_LABEL = {
        tracked_objects: '跟踪轨迹数', unique_tracks: '跟踪轨迹数',
        tracking_partial: '部分跟踪（按记录回退口径）',
        frame_detections: '逐帧检测（按记录回退口径）', image_objects: '图片检出框',
        legacy_unknown: '历史记录',
    };
    const modes = d.by_counting_mode || [];
    destroyChart('todayMode');
    charts.todayMode = new Chart($('#chart-today-mode').getContext('2d'), {
        type: 'doughnut',
        data: {
            labels: modes.map((m) => MODE_LABEL[m.counting_mode] || m.counting_mode),
            datasets: [{
                data: modes.map((m) => m.records),
                backgroundColor: modes.map((_, i) => PIE_COLORS[i % PIE_COLORS.length]),
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'bottom' },
                title: { display: true, text: modes.length ? '窗口内记录按计数口径分布' : '窗口内暂无成功检测' },
            },
        },
    });
}

async function loadToday(hours) {
    const requestId = ++todayRequest;
    todayHours = hours || 24;
    $('#today-error').textContent = '正在加载今日检测数据...';
    ['todayHour', 'todayMode'].forEach(destroyChart);
    ['today-records', 'today-since', 'today-objects', 'today-objects-note',
        'today-detections', 'today-sites'].forEach((id) => { $('#' + id).textContent = '—'; });
    try {
        const data = await API.get(`/api/stats/today?hours=${todayHours}`);
        if (requestId !== todayRequest) return;
        $('#today-error').innerHTML = '';
        renderToday(data);
    } catch (err) {
        if (requestId !== todayRequest) return;
        $('#today-error').innerHTML =
            `<div class="alert alert-error">今日检测数据加载失败: ${escapeHtml(err.message)}</div>`;
    }
}

/* ---------------- 站点 / 样方维度 ---------------- */

let siteDays = 0;
let sitesRequest = 0;

function renderSites(payload) {
    const box = $('#sites-list');
    const items = (payload && payload.items) || [];
    if (!items.length) {
        box.innerHTML = '<p class="muted small">暂无带站点信息的检测记录, 可在上传页填写调查站点后重新检测。</p>';
        return;
    }
    const total = payload.total_objects || 1;
    box.innerHTML = items.map((site, idx) => {
        const transects = site.transects || [];
        const rows = transects.map((t) => `<tr>
            <td>${escapeHtml(t.transect_label)}</td>
            <td>${fmtNumber(t.records)}</td>
            <td>${fmtNumber(t.objects)}</td>
            <td class="small muted">${(t.objects / (site.objects || 1) * 100).toFixed(1)}%</td>
            <td>${fmtNumber(t.maxn_peak)}</td>
        </tr>`).join('');
        return `<div class="site-card">
            <div class="site-head" data-site-toggle="${idx}">
                <span class="name">${escapeHtml(stationDisplayLabel(site))}</span>
                <span><span class="k">记录 </span><span class="v">${fmtNumber(site.records)}</span></span>
                <span><span class="k">检出计数 </span><span class="v">${numberDrill(
                    fmtNumber(site.objects), siteDrill(site, transects, siteDays))}</span></span>
                <span><span class="k">占全部 </span><span class="v">${(site.objects / total * 100).toFixed(1)}%</span></span>
                <span><span class="k">样方 </span><span class="v">${transects.length}</span></span>
                <span class="caret" data-caret>展开 ▾</span>
            </div>
            <div class="site-transects hidden" data-site-body="${idx}">
                <div class="table-wrap"><table>
                    <thead><tr><th>样方 / 断面</th><th>记录数</th><th>检出计数</th>
                        <th>占本站点</th><th>单帧峰值</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>
            </div>
        </div>`;
    }).join('');

    // 展开/收起: 使用事件委托不重复绑定的写法
    $$('#sites-list [data-site-toggle]').forEach((head) => {
        head.addEventListener('click', () => {
            const idx = head.dataset.siteToggle;
            const body = $(`[data-site-body="${idx}"]`);
            const caret = head.querySelector('[data-caret]');
            if (!body) return;
            body.classList.toggle('hidden');
            if (caret) caret.textContent = body.classList.contains('hidden') ? '展开 ▾' : '收起 ▴';
        });
    });
}

async function loadSites(days) {
    const requestId = ++sitesRequest;
    siteDays = days || 0;
    $('#sites-error').textContent = '正在加载站点维度...';
    $('#sites-list').innerHTML = '<p class="muted small">加载中...</p>';
    try {
        const q = days && Number(days) > 0 ? `?days=${days}` : '?days=0';
        const data = await API.get('/api/stats/sites' + q);
        if (requestId !== sitesRequest) return;
        $('#sites-error').innerHTML = '';
        renderSites(data);
    } catch (err) {
        if (requestId !== sitesRequest) return;
        $('#sites-error').innerHTML =
            `<div class="alert alert-error">站点维度加载失败: ${escapeHtml(err.message)}</div>`;
        $('#sites-list').innerHTML = '';
    }
}

/* ---------------- 置信度分布 ---------------- */

let confDays = 0;
let confidenceRequest = 0;

function renderConfidence(payload) {
    const d = payload || {};
    $('#conf-total').innerHTML = numberDrill(fmtNumber(d.total || 0), {
        title: '检出框置信度明细',
        caliber: 'confidence.total',
        loader: async () => {
            const bins = d.bins || [];
            return {
                summary: [
                    { label: '检出框总数', value: fmtNumber(d.total || 0) },
                    { label: '中位数', value: d.median === null ? '—' : String(d.median) },
                ],
                note: '下钻列出的是直方图各桶的计数(构成置信度分布这一数字的原始分桶), '
                    + '而非逐框坐标; 逐框明细请在“检测记录”中打开对应记录查看。',
                columns: ['置信度区间', '检出框数', '占比'],
                rows: bins.map((b) => [
                    `${b.lower.toFixed(2)} ~ ${b.upper.toFixed(2)}`,
                    fmtNumber(b.count),
                    d.total ? `${(b.count / d.total * 100).toFixed(1)}%` : '-',
                ]),
            };
        },
    });
    $('#conf-median').textContent = d.median === null || d.median === undefined ? '—' : d.median;
    $('#conf-mean').textContent = d.mean === null || d.mean === undefined ? '—' : d.mean;
    $('#conf-p10').textContent = d.p10 === null || d.p10 === undefined ? '—' : d.p10;
    $('#conf-p90').textContent = d.p90 === null || d.p90 === undefined ? '—' : d.p90;
    const scope = d.scope || {};
    $('#conf-sample-note').textContent = scope.sampled
        ? `其中视频采样明细 ${fmtNumber(scope.video_boxes || 0)} 个框, 视频部分为抽样口径`
        : '';

    const bins = d.bins || [];
    destroyChart('confHist');
    charts.confHist = new Chart($('#chart-conf-hist').getContext('2d'), {
        type: 'bar',
        data: {
            labels: bins.map((b) => `${b.lower.toFixed(2)}~${b.upper.toFixed(2)}`),
            datasets: [{
                label: '检出框数',
                data: bins.map((b) => b.count),
                backgroundColor: chartPrimary(),
                borderRadius: 3,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false },
                title: { display: true, text: bins.length ? '置信度分布' : '暂无检出框' } },
            scales: {
                x: { ticks: { maxRotation: 60, minRotation: 45, font: { size: 10 } } },
                y: { beginAtZero: true, ticks: { precision: 0 } },
            },
        },
    });

    const sens = d.sensitivity || [];
    destroyChart('confSens');
    charts.confSens = new Chart($('#chart-conf-sens').getContext('2d'), {
        type: 'line',
        data: {
            labels: sens.map((s) => String(s.threshold)),
            datasets: [{
                label: '保留比例 %',
                data: sens.map((s) => s.ratio),
                borderColor: '#f59e0b',
                backgroundColor: 'rgba(245, 158, 11, .14)',
                fill: true,
                tension: 0.25,
                pointRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false },
                title: { display: true, text: '阈值提高后保留的检出框比例' } },
            scales: {
                x: { title: { display: true, text: '置信度阈值' } },
                y: { beginAtZero: true, max: 100, ticks: { callback: (v) => v + '%' } },
            },
        },
    });

    $('#conf-sens-body').innerHTML = sens.length
        ? sens.map((s) => `<tr>
            <td>${s.threshold}</td>
            <td>${fmtNumber(s.kept)}</td>
            <td>${s.ratio}%</td>
        </tr>`).join('')
        : '<tr><td colspan="3" class="empty">暂无数据</td></tr>';
}

async function loadConfidence(days) {
    const requestId = ++confidenceRequest;
    confDays = days || 0;
    $('#confidence-error').textContent = '正在加载置信度分布...';
    ['confHist', 'confSens'].forEach(destroyChart);
    ['conf-total', 'conf-median', 'conf-mean', 'conf-p10', 'conf-p90']
        .forEach((id) => { $('#' + id).textContent = '—'; });
    $('#conf-sample-note').textContent = '';
    $('#conf-sens-body').innerHTML = '<tr><td colspan="3" class="empty">加载中...</td></tr>';
    try {
        const q = days && Number(days) > 0 ? `?days=${days}` : '?days=0';
        const data = await API.get('/api/stats/confidence' + q);
        if (requestId !== confidenceRequest) return;
        $('#confidence-error').innerHTML = '';
        renderConfidence(data);
    } catch (err) {
        if (requestId !== confidenceRequest) return;
        $('#confidence-error').innerHTML =
            `<div class="alert alert-error">置信度分布加载失败: ${escapeHtml(err.message)}</div>`;
    }
}

/** 渲染"各卡片数字的口径说明"区块, 以及页面上的行内口径徽标 */
function renderCaliberInventory() {
    const list = $('#caliber-list');
    if (list) {
        list.innerHTML = caliberBlock([
            'overview.today_count', 'overview.today_objects', 'overview.total_count',
            'overview.total_objects', 'overview.class_count', 'overview.avg_inference_ms',
            'ecology.shannon', 'ecology.simpson', 'ecology.pielou', 'ecology.margalef',
        ], '展开查看统计页各数字的完整定义');
    }
    $$('[data-caliber]').forEach((el) => {
        el.innerHTML = caliberBadge(el.dataset.caliber, '口径');
    });
}

document.addEventListener('DOMContentLoaded', () => {
    if (typeof Chart === 'undefined') {
        $('#alert-box').innerHTML =
            '<div class="alert alert-error">Chart.js 未加载, 请检查 web/vendor/chart.js 是否存在</div>';
        return;
    }
    renderCaliberInventory();
    loadDashboard(7, 'day');
    loadEcology(0);
    loadToday(24);
    loadSites(0);
    loadConfidence(0);

    $('#trend-days').addEventListener('change', (e) => {
        const [d, g] = String(e.target.value).split(':');
        loadDashboard(parseInt(d, 10), g || 'day');
    });

    $('#ecology-days').addEventListener('change', (e) => {
        // 直接透传 select 的原始值:", 由 parseEcologyScope 区分 24h 与自然日
        loadEcology(e.target.value);
    });

    $('#today-hours').addEventListener('change', (e) => {
        loadToday(parseInt(e.target.value, 10) || 24);
    });

    $('#site-days').addEventListener('change', (e) => {
        loadSites(parseInt(e.target.value, 10) || 0);
    });

    $('#conf-days').addEventListener('change', (e) => {
        loadConfidence(parseInt(e.target.value, 10) || 0);
    });

    // Chart.js 配色不会自动跟随 CSS 变量, 主题切换后重绘图表
    onThemeChange(() => {
        loadDashboard(currentDays, currentGranularity);
        // 主题切换重绘需要还原"天/小时"双口径, 这里传回 select 当前值而不是只传天数
        const ecoSelect = $('#ecology-days');
        loadEcology(ecoSelect ? ecoSelect.value : ecologyDays);
        loadToday(todayHours);
        loadSites(siteDays);
        loadConfidence(confDays);
    });
});
