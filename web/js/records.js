/* 检测记录页: 筛选 / 分页 / 详情(含结果视频与图表) / 删除 / 复现包 / 多记录对比 */

const state = { page: 1, page_size: 10 };
const PIE_COLORS = ['#0e7490', '#06b6d4', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6'];
let detailCharts = [];
let recordsRequest = 0;
let detailRequest = 0;

/* ---------------- 勾选与多记录对比 ---------------- */

// 单次最多对比 4 条: 再多就无法在一屏内并排阅读
const MAX_COMPARE = 4;
// 与后端 app/api/records.py 的 _MAX_REPORT_RECORDS 保持一致;
// 后端是最终防线, 这里只是把"点了才会失败"提前成"按钮不可点"
const MAX_EXPORT_RECORDS = 50;
// 勾选顺序 = 对比时的列顺序, 因此用数组而不是 Set
let selectedIds = [];
let currentItems = new Map();
// 上次加载时的筛选条件(不含分页)。用来判断"翻页"还是"换了筛选":
// 前者保留勾选, 后者必须清空。
let lastFilterKey = null;
let compareCharts = [];
// 最近一次对比结果: 导出 CSV 时直接复用, 保证与页面显示同源
let lastCompareData = null;

function isSelected(id) {
    return selectedIds.includes(Number(id));
}

function recordById(id) {
    return currentItems.get(Number(id)) || null;
}

/** 勾选/取消勾选一条记录, 返回是否成功(超出上限时提示并拒绝)。 */
function toggleSelect(id, on) {
    const rid = Number(id);
    if (on) {
        if (isSelected(rid)) return true;
        if (selectedIds.length >= MAX_COMPARE) {
            toast(`最多同时对比 ${MAX_COMPARE} 条记录, 请先取消勾选其他记录`, 'error');
            return false;
        }
        selectedIds.push(rid);
    } else {
        selectedIds = selectedIds.filter((x) => x !== rid);
    }
    return true;
}

/** 同步勾选列、全选框与按钮的可用状态。 */
function renderSelectBar() {
    const count = selectedIds.length;
    const hint = $('#select-hint');
    const all = $$('#records-body [data-select]');
    const checked = all.filter((cb) => cb.checked).length;

    const allBox = $('#select-all');
    if (allBox) {
        allBox.checked = all.length > 0 && checked === all.length;
        allBox.indeterminate = checked > 0 && checked < all.length;
        allBox.disabled = all.length === 0;
    }

    if (hint) {
        if (!count) {
            hint.textContent = `勾选 2–${MAX_COMPARE} 条记录可并列对比`;
            hint.className = 'small muted';
        } else if (count === 1) {
            hint.textContent = '已选 1 条, 再选 1 条即可开始对比';
            hint.className = 'small muted';
        } else {
            hint.textContent = `已选 ${count} 条(顺序即并列顺序)`;
            hint.className = 'small select-count';
        }
    }
    const btn = $('#btn-compare');
    if (btn) {
        btn.disabled = count < 2;
        btn.textContent = count >= 2 ? `对比所选 (${count})` : '对比所选';
    }
    const clear = $('#btn-clear-select');
    if (clear) clear.disabled = count === 0;

    // 导出类按钮: 数量上限与"只收图片记录"由后端强制, 这里只做前端的提前拦截,
    // 让用户在点之前就看见为什么不能导 —— 与对比按钮"不足 2 条禁用"是同一套交互。
    const imageIds = selectedIds.filter((id) => {
        const rec = recordById(id);
        return rec && rec.task_type !== 'video';
    });
    const videoCount = count - imageIds.length;
    const overLimit = imageIds.length > MAX_EXPORT_RECORDS;
    if (videoCount > 0 && hint) {
        // 视频不能出标签/报告, 单独提示; 放在按钮状态之后, 覆盖默认的"已选 N 条"
        hint.textContent = `已选 ${count} 条, 其中 ${videoCount} 条视频不参与报告 / 标签导出`;
        hint.className = 'small muted';
    }
    const reportBtn = $('#btn-bulk-report');
    if (reportBtn) {
        reportBtn.disabled = !imageIds.length || overLimit;
        reportBtn.textContent = imageIds.length > 0 ? `批量报告 (${imageIds.length})` : '批量报告';
        reportBtn.title = overLimit
            ? `单份报告最多 ${MAX_EXPORT_RECORDS} 张影像, 当前勾选 ${imageIds.length} 张`
            : '把所选图片记录合成一份检测报告 PDF';
    }
    const exportBtn = $('#btn-bulk-export');
    if (exportBtn) {
        exportBtn.disabled = !imageIds.length || overLimit;
        exportBtn.textContent = imageIds.length > 0 ? `导出标签 (${imageIds.length})` : '导出标签';
        exportBtn.title = overLimit
            ? `单次最多导出 ${MAX_EXPORT_RECORDS} 张影像的标签, 当前勾选 ${imageIds.length} 张`
            : '把所选图片记录导出为 CSV / COCO / YOLO 标签';
    }
}

function clearSelection() {
    selectedIds = [];
    $$('#records-body [data-select]').forEach((cb) => { cb.checked = false; });
    renderSelectBar();
    $('#compare-card').classList.add('hidden');
    destroyCompareCharts();
}

function destroyCompareCharts() {
    compareCharts.forEach((c) => { try { c.destroy(); } catch (e) { /* ignore */ } });
    compareCharts = [];
}

/** 勾选变化后, 已展示的对比结果就过期了 —— 收起它, 避免"看着旧图说新话"。 */
function invalidateCompareView() {
    const card = $('#compare-card');
    if (card && !card.classList.contains('hidden')) {
        card.classList.add('hidden');
        destroyCompareCharts();
    }
}

/* ---------------- 可复现包 ---------------- */

/**
 * 下载某条记录的复现包。
 *
 * 用 <a download> 直接指向 GET 接口: 让浏览器自己处理流式下载与文件名,
 * 不必把几 MB 的 zip 先读进内存再转 Blob。后端会在响应头里带文件数与缺失数,
 * 但跨域/下载场景前端读不到这些头, 因此这里只提示"已开始下载",
 * 具体缺了什么由包内 README 与 manifest.json 说明。
 */
function downloadRepro(recordId) {
    const url = `/api/report/repro/${encodeURIComponent(recordId)}`;
    const a = document.createElement('a');
    a.href = url;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast(`正在生成记录 #${recordId} 的复现包, 生成完成后浏览器会开始下载`, 'info', 4000);
}

/* ---------------- 批量导出(报告 PDF / 训练标签) ---------------- */

/**
 * 解析 Content-Disposition 里的文件名, 优先 filename*=UTF-8''...
 * 字段名由后端 RFC 5987 编码, 直接取 filename= 会拿到乱码。
 */
function parseDownloadName(disposition) {
    const raw = String(disposition || '');
    const star = /filename\*=UTF-8''([^;]+)/i.exec(raw);
    if (star) {
        try { return decodeURIComponent(star[1]); } catch (e) { /* 落到下面的分支 */ }
    }
    const plain = /filename="?([^";]+)"?/i.exec(raw);
    return plain ? plain[1] : '';
}

/** 触发一次浏览器下载; revoke 延后, 避免 Safari 上还没开始下就被回收。 */
function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * 请求一个返回附件的接口并把结果交给浏览器下载。
 *
 * 之所以不直接用 <a href> 指过去: 这两个接口是 POST + JSON 请求体,
 * 链接发不出请求体; 且需要读到错误响应里的 message 才能给出可读提示。
 */
async function postAttachment(url, body, fallbackName) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) {
        let msg = `导出失败 (HTTP ${resp.status})`;
        try {
            const payload = await resp.json();
            if (payload && payload.message) msg = payload.message;
        } catch (e) { /* 保持默认信息 */ }
        throw new Error(msg);
    }
    const blob = await resp.blob();
    const filename = parseDownloadName(resp.headers.get('content-disposition')) || fallbackName;
    saveBlob(blob, filename);
    return filename;
}

/** 当前勾选中可参与报告 / 标签导出的记录(仅图片; 视频的明细跨帧, 不能对到一张图上)。 */
function exportableSelection() {
    return selectedIds.filter((id) => {
        const rec = recordById(id);
        return rec && rec.task_type !== 'video';
    });
}

// ------------------------------------------------------------ 对比结果 CSV
//
// 对比表已经在页面上渲染了一份, 但用户要把它带进论文 / Excel 时只能截图。
// 这里从"同一份 data"重算一遍行, 保证导出的数字与页面显示逐格一致 ——
// 绝不能另写一套取值逻辑, 否则两处会对不上。

/** 把一个单元格值按 CSV 规则转义: 含逗号/引号/换行时必须加引号。 */
function csvCell(value) {
    const text = value === null || value === undefined ? '' : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** 对比视图的指标定义: 与 renderCompareView 的表格行一一对应。
 *
 * 每项返回 { label, value(col) }。加指标时两处必须一起改, 否则导出会漏行。
 */
function compareMetrics() {
    return [
        { label: '文件名称', value: (c) => c.source_name || '' },
        { label: '检测类型', value: (c) => (c.task_type === 'video' ? '视频' : '图片') },
        { label: '检测时间', value: (c) => c.created_at || '' },
        { label: '计数口径', value: (c) => COUNT_SOURCE_LABEL[c.caliber.source] || c.caliber.source },
        { label: '本记录计数', value: (c) => c.caliber.value },
        { label: '累计检出框', value: (c) => c.total_detections },
        { label: '轨迹数', value: (c) => (c.tracked_objects ? c.tracked_objects : 'N/A') },
        { label: '单帧峰值 MaxN', value: (c) => (c.maxn_total ? c.maxn_total : 'N/A') },
        { label: 'CPUnx', value: (c) => (c.cpunx === null || c.cpunx === undefined ? 'N/A' : c.cpunx) },
        { label: '检出类别数', value: (c) => c.class_count },
        { label: '明细样本量', value: (c) => c.conf.count },
        { label: '置信度均值', value: (c) => c.conf.mean },
        { label: '置信度中位数', value: (c) => c.conf.median },
        { label: 'P25', value: (c) => (c.conf.p25 === null ? 'N/A' : c.conf.p25) },
        { label: 'P75', value: (c) => (c.conf.p75 === null ? 'N/A' : c.conf.p75) },
        { label: '置信度区间', value: (c) => (c.conf.min === null ? 'N/A' : `${c.conf.min} ~ ${c.conf.max}`) },
        { label: '调查站点', value: (c) => c.site_label || '未标注站点' },
        { label: '样方 / 断面', value: (c) => c.transect_label || '未标注样方' },
        { label: '调查轮次', value: (c) => c.survey_round || '未标注轮次' },
        { label: '水深(m)', value: (c) => (c.depth_m === null || c.depth_m === undefined ? '未记录' : c.depth_m) },
    ];
}

/** 把对比数据序列化成 CSV 文本(指标为行、记录为列, 与页面表格同向)。 */
function compareToCsv(data) {
    const cols = data.columns || [];
    const head = ['对比项', ...cols.map((c) => `#${c.record_id}`)];
    const lines = [head.map(csvCell).join(',')];
    for (const metric of compareMetrics()) {
        lines.push([metric.label, ...cols.map((c) => metric.value(c))].map(csvCell).join(','));
    }
    // 口径不一致时把结论写进文件, 否则脱离页面后没人知道这些数字不可直接横比
    lines.push('');
    lines.push(csvCell('口径判定') + ',' + csvCell(data.comparable ? '口径一致, 可直接横向比较' : '口径不一致, 不可直接横向比较'));
    if ((data.scope || {}).selection) {
        lines.push(csvCell('选择范围') + ',' + csvCell(data.scope.selection));
    }
    return lines.join('\r\n');
}

/** 导出对比结果为 CSV。纯前端拼装 + Blob, 不经过后端。 */
function exportCompareCsv() {
    const data = lastCompareData;
    if (!data || !(data.columns || []).length) {
        toast('还没有可导出的对比数据', 'error');
        return;
    }
    const csv = compareToCsv(data);
    // 加 BOM: 否则 Excel 打开中文会乱码(它是按系统编码猜的)
    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    saveBlob(blob, `对比_${data.columns.length}条_${stamp}.csv`);
    toast(`已导出 ${data.columns.length} 条记录的对比结果`, 'success');
}

async function bulkExport(kind) {
    const ids = exportableSelection();
    if (!ids.length) {
        toast('请先勾选至少 1 条图片记录', 'error');
        return;
    }
    if (ids.length > MAX_EXPORT_RECORDS) {
        toast(`单次最多导出 ${MAX_EXPORT_RECORDS} 张影像, 当前勾选 ${ids.length} 张`, 'error', 4000);
        return;
    }

    const isReport = kind === 'report';
    const btn = $(isReport ? '#btn-bulk-report' : '#btn-bulk-export');
    const oldText = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = isReport ? '生成中...' : '导出中...';
    }

    try {
        let filename;
        if (isReport) {
            filename = await postAttachment(
                '/api/report/images', { record_ids: ids },
                `detection_report_${ids.length}pcs.pdf`,
            );
        } else {
            const fmt = $('#bulk-export-format') ? $('#bulk-export-format').value : 'csv';
            const ext = fmt === 'csv' ? 'csv' : (fmt === 'coco' ? 'json' : 'zip');
            filename = await postAttachment(
                '/api/report/export', { record_ids: ids, format: fmt },
                `detection_labels_${ids.length}pcs.${ext}`,
            );
        }
        toast(`已导出 ${ids.length} 张影像: ${filename}`, 'success', 4000);
    } catch (err) {
        toast(err.message || '导出失败', 'error', 4500);
    } finally {
        if (btn) btn.textContent = oldText;
        renderSelectBar();
    }
}


function hasValidUniqueCount(record) {
    return record.unique_count_valid === true || record.unique_count_valid === 1;
}

/** 该记录实际取用的计数值与口径来源(后端已在读层统一算好, 此处只做展示兜底)。 */
function effectiveCount(record) {
    if (record.effective_count !== undefined && record.effective_count !== null) {
        return record.effective_count;
    }
    // 后端旧版本未返回该字段时的本地兜底, 优先级与 dao._effective_count 保持一致
    if (record.task_type !== 'video') return Number(record.total_objects) || 0;
    if (hasValidUniqueCount(record) && record.unique_objects !== null) {
        return Number(record.unique_objects) || 0;
    }
    const maxn = (record.maxn && record.maxn.maxn_total) || Number(record.maxn_total) || 0;
    if (maxn > 0) return maxn;
    return Number(record.total_detections ?? record.total_objects) || 0;
}

/** 口径来源 -> 展示文案。四个来源互斥, 不存在"不可用"这一档。 */
const COUNT_SOURCE_LABEL = {
    unique_tracks: '跨帧轨迹数',
    maxn: '单帧峰值 MaxN',
    total_detections: '累计检出框',
    image_objects: '图片检出框',
};

function countSourceLabel(record) {
    const source = record.effective_count_source
        || (record.task_type === 'video'
            ? (hasValidUniqueCount(record) ? 'unique_tracks' : 'maxn') : 'image_objects');
    return COUNT_SOURCE_LABEL[source] || '记录计数';
}

/** 计数口径说明: 只讲"本次用了哪套口径、依据是什么", 不使用不可用/失效等否定表述。 */
function countingDescription(record) {
    if (record.task_type !== 'video') {
        return '图片记录以本图检出框数为计数, 与明细逐条对应';
    }
    const source = record.effective_count_source
        || (hasValidUniqueCount(record) ? 'unique_tracks' : 'maxn');
    const tracked = Number(record.tracked_objects) || 0;
    if (source === 'unique_tracks') {
        return tracked > 0
            ? `已建立跨帧关联, 采用轨迹去重计数(本次形成 ${tracked} 条轨迹)`
            : '已建立跨帧关联, 采用轨迹去重计数';
    }
    if (source === 'maxn') {
        return tracked > 0
            ? `采用单帧峰值(MaxN)计数; 本次已形成 ${tracked} 条轨迹并据此提供了逐帧框对比`
            : '采用单帧峰值(MaxN)计数, 依据是各帧内同类别的同时出现数量';
    }
    return '采用累计检出框计数, 即所有已处理帧的检出框求和';
}

/** 追踪粒度说明: 正面说明"当前能提供什么", 替代原"追踪能力不足"一类表述。 */
function trackingGranularity(record) {
    if (record.task_type !== 'video') return '';
    const tracked = Number(record.tracked_objects) || 0;
    const trackedFrames = Number(record.frames_processed) || 0;
    const parts = [];
    if (tracked > 0) {
        parts.push(`累计建立 ${tracked} 条轨迹编号, 覆盖 ${trackedFrames} 个已处理帧`);
    } else {
        parts.push(`本次逐帧处理 ${trackedFrames} 帧, 每帧输出类别与检出框`);
    }
    parts.push('可提供的粒度: 逐帧框坐标与置信度、单帧类别计数、单帧峰值(MaxN)、按类别与时间的分布');
    return parts.join('；') + '。';
}

function provenanceHtml(record) {
    const snapshot = record.inference_snapshot;
    if (!snapshot || !snapshot.model) {
        return '<p class="small muted">此历史记录未保存模型版本及完整推理参数，无法从当前模型补推当时的配置。</p>';
    }
    const model = snapshot.model || {};
    const params = snapshot.parameters || {};
    const values = [
        ['模型', model.name || '未知'], ['权重 SHA-256', model.sha256 || '未记录'],
        ['推理设备', snapshot.device || '未记录'], ['输入尺寸', params.imgsz ?? '未记录'],
        ['IoU 阈值', params.iou_threshold ?? '未记录'],
    ];
    if (record.task_type === 'video') {
        values.push(['抽帧间隔', params.frame_skip ?? '未记录'],
            ['跟踪器', params.track_enabled ? (params.tracker || '未记录') : '未启用']);
    }
    return `<details style="margin-top:12px;"><summary>本次检测的模型版本与参数</summary>
        <div class="table-wrap"><table><tbody>${values.map(([label, value]) =>
            `<tr><th>${escapeHtml(label)}</th><td style="overflow-wrap:anywhere;">${escapeHtml(value)}</td></tr>`
        ).join('')}</tbody></table></div></details>`;
}

/* ---------------- 下钻与 MaxN 区块 ---------------- */

/** 把明细行渲染成表格单元格(统一转义, 避免各处重复写) */
function itemCells(it, isVideo) {
    const box = [it.x1, it.y1, it.x2, it.y2]
        .map((v) => (v === null || v === undefined ? '-' : Number(v).toFixed(0))).join(', ');
    return [
        isVideo ? `第 ${it.frame_index} 帧` : '-',
        escapeHtml(it.class_name),
        `${(Number(it.confidence) * 100).toFixed(1)}%`,
        `<span class="mono small">${box}</span>`,
    ];
}

/** 第一层下钻: 该记录的类别构成 -> 每个类别可继续下钻到明细 */
function recordDrill(record) {
    const isVideo = record.task_type === 'video';
    return {
        title: `记录 #${record.id} 的计数构成`,
        caliber: 'record.effective_count',
        loader: async () => {
            const stats = record.class_stats || [];
            const total = stats.reduce((s, c) => s + (Number(c.count) || 0), 0);
            return {
                summary: [
                    { label: '本记录计数', value: fmtNumber(effectiveCount(record)) },
                    { label: '口径', value: countSourceLabel(record) },
                    { label: '类别数', value: String(stats.length) },
                    { label: '类别计数合计', value: fmtNumber(total) },
                ],
                note: isVideo
                    ? `类别构成的合计(${total})与本记录计数(${effectiveCount(record)})不一定相等: `
                      + '前者按类别累加检出框, 后者是从更高层口径取的一个数。点击下方按钮可继续查看每类的明细。'
                    : '图片记录的类别构成合计应等于本图检出框数; 若不等, 说明类别表与明细表不同步。',
                columns: ['类别', '计数', '占比'],
                rows: stats.map((c) => [
                    escapeHtml(c.class_name),
                    fmtNumber(c.count),
                    total ? `${(c.count / total * 100).toFixed(1)}%` : '-',
                ]),
                children: stats.map((c) => ({
                    label: `展开「${c.class_name}」明细`,
                    drill: classDetailDrill(record, c.class_name),
                })),
            };
        },
    };
}

/** 第二层下钻: 某一类别在该记录中的逐条明细(最终落到原始记录) */
function classDetailDrill(record, className) {
    return {
        title: `「${className}」的明细`,
        loader: async () => {
            const items = (record.items || []).filter((it) => it.class_name === className);
            const isVideo = record.task_type === 'video';
            return {
                summary: [
                    { label: '类别', value: className },
                    { label: '明细条数', value: String(items.length) },
                    { label: '明细入库上限', value: '本记录最多 500 条' },
                ],
                note: record.task_type === 'video'
                    ? '视频明细为采样帧明细, 受入库采样上限限制, 不代表所有帧的全部目标。'
                    : undefined,
                columns: [isVideo ? '帧' : '序号', '类别', '置信度', '边界框'],
                rows: items.map((it) => itemCells(it, isVideo)),
                truncated: items.length >= 500,
            };
        },
    };
}

/** 逐帧来源下钻: 视频的累计检出框按采样帧拆开 */
function frameRowsDrill(record, title = '逐帧来源') {
    return {
        title,
        loader: async () => {
            const rows = record.frame_stats || [];
            const sum = rows.reduce((s, f) => s + (Number(f.total_objects) || 0), 0);
            return {
                summary: [
                    { label: '采样帧数', value: String(rows.length) },
                    { label: '采样帧检出框合计', value: fmtNumber(sum) },
                    { label: '记录累计检出框', value: fmtNumber(record.total_detections ?? 0) },
                ],
                note: '逐帧统计按时间间隔采样写入, 采样帧合计通常小于全部帧的累计检出框; '
                    + '这里展示的是可复核的那部分原始帧记录。',
                columns: ['帧号', '时间(s)', '该帧检出框', '类别构成'],
                rows: rows.map((f) => {
                    const cc = f.class_counts || {};
                    const text = Object.keys(cc).length
                        ? Object.entries(cc).map(([k, v]) => `${k} ${v}`).join('、')
                        : '-';
                    return [String(f.frame_index), String(f.timestamp_sec),
                        fmtNumber(f.total_objects), escapeHtml(text)];
                }),
            };
        },
    };
}

/** MaxN 相对丰度区块: 只有真的算出了峰值才展示, 否则说明为什么没有 */
function maxnBlock(record, isVideo) {
    if (!isVideo) return '';
    const maxn = record.maxn || {};
    const perClass = maxn.per_class || {};
    const names = Object.keys(perClass);
    if (!maxn.available || !names.length) {
        return `<p class="small muted">单帧峰值(MaxN): 本记录未保存逐帧类别计数
            (历史记录或未产生采样帧), 因此不提供该指标; 可用计数见上方“本记录计数”。</p>`;
    }
    const rows = names.map((name) => `<tr>
        <td>${escapeHtml(name)}</td>
        <td>${numberDrill(fmtNumber(perClass[name]), maxnClassDrill(record, name))}</td>
        <td class="small muted">${maxn.maxn_total
            ? `${(perClass[name] / maxn.maxn_total * 100).toFixed(1)}%` : '-'}</td>
    </tr>`).join('');
    return `<h3 style="font-size:14px;margin:16px 0 8px;">
            单帧峰值 MaxN ${caliberBadge('record.maxn', '口径')}</h3>
        <div class="summary-line" style="margin-bottom:8px;">
            <span>总体峰值: ${numberDrill(fmtNumber(maxn.maxn_total),
                frameRowsDrill(record, 'MaxN 的逐帧来源'))}</span>
            <span>单位努力量 CPUnx: ${maxn.cpunx === null || maxn.cpunx === undefined ? '—'
                : `${numberDrill(String(maxn.cpunx), cpunxDrill(record))} ${escapeHtml(maxn.unit || '条/小时')}`}</span>
            ${caliberBadge('record.cpunx', 'CPUnx 口径')}
        </div>
        <div class="table-wrap"><table>
            <thead><tr><th>类别</th><th>单帧峰值</th><th>占总体峰值</th></tr></thead>
            <tbody>${rows}</tbody>
        </table></div>
        <p class="small muted">${escapeHtml(maxn.scope || '')} ${escapeHtml(maxn.limitation || '')}</p>`;
}

/** MaxN 单类别下钻: 找出峰值出现在哪一帧 */
function maxnClassDrill(record, className) {
    return {
        title: `「${className}」峰值的帧来源`,
        caliber: 'record.maxn',
        loader: async () => {
            const rows = (record.frame_stats || [])
                .map((f) => ({ ...f, n: Number((f.class_counts || {})[className]) || 0 }))
                .filter((f) => f.n > 0)
                .sort((a, b) => b.n - a.n || a.frame_index - b.frame_index);
            const peak = rows.length ? rows[0].n : 0;
            return {
                summary: [
                    { label: '类别', value: className },
                    { label: '单帧峰值', value: fmtNumber(peak) },
                    { label: '达到峰值的采样帧', value: String(rows.filter((r) => r.n === peak).length) },
                ],
                note: '峰值取所有已处理帧中的最大值; 下表按单帧数量降序, 便于核对峰值出现在哪一帧。',
                columns: ['帧号', '时间(s)', `该帧「${className}」数量`, '该帧总检出框'],
                rows: rows.map((r) => [String(r.frame_index), String(r.timestamp_sec),
                    fmtNumber(r.n), fmtNumber(r.total_objects)]),
                truncated: rows.length > 500,
            };
        },
    };
}

/** CPUnx 下钻: 把公式里的两个输入摊开 */
function cpunxDrill(record) {
    return {
        title: 'CPUnx 的计算过程',
        caliber: 'record.cpunx',
        loader: async () => {
            const maxn = record.maxn || {};
            const duration = Number(record.duration_sec) || 0;
            const hours = duration / 3600;
            return {
                summary: [
                    { label: 'MaxN', value: fmtNumber(maxn.maxn_total || 0) },
                    { label: '时长', value: `${duration} 秒` },
                    { label: '时长(小时)', value: hours ? hours.toFixed(4) : '0' },
                    { label: 'CPUnx', value: maxn.cpunx === null || maxn.cpunx === undefined
                        ? '未计算' : String(maxn.cpunx) },
                ],
                note: 'CPUnx = MaxN ÷ 时长(小时)。时长取自视频元信息, 缺失时该指标不计算, 也不会用 0 代替。',
                columns: ['输入项', '取值', '说明'],
                rows: [
                    ['单帧峰值 MaxN', fmtNumber(maxn.maxn_total || 0), '各帧同类别同时出现数量的最大值'],
                    ['视频时长', `${duration} s`, '视频元信息中的 duration_sec'],
                    ['折算小时', hours ? hours.toFixed(4) : '0', 'seconds / 3600'],
                    ['结果', maxn.cpunx === null || maxn.cpunx === undefined
                        ? '未计算' : String(maxn.cpunx), '保留 1 位小数'],
                ],
            };
        },
    };
}

/** 关闭详情时销毁其中的图表, 避免 canvas 复用冲突与内存泄漏 */
function destroyDetailCharts() {
    detailCharts.forEach((c) => { try { c.destroy(); } catch (e) { /* ignore */ } });
    detailCharts = [];
}

/** 当前是否有生效的筛选条件(类型 / 关键字 / 日期任一非空)。 */
function isFiltered() {
    return Boolean(
        ($('#filter-type').value || '').trim()
        || ($('#filter-keyword').value || '').trim()
        || ($('#filter-from').value || '').trim()
        || ($('#filter-to').value || '').trim()
        || ($('#filter-site')?.value || '').trim()
    );
}

function buildQuery() {
    const params = new URLSearchParams({
        page: String(state.page),
        page_size: String(state.page_size),
        task_type: $('#filter-type').value,
        keyword: $('#filter-keyword').value.trim(),
        date_from: $('#filter-from').value,
        date_to: $('#filter-to').value,
        ...($('#filter-site')?.value ? { site_id: $('#filter-site').value } : {}),
    });
    return `/api/records?${params.toString()}`;
}

/** 空态文案: 区分"库里就没有"和"筛掉了", 并给出下一步动作。
 *
 * "暂无符合条件的记录"这一句同时覆盖了两种完全不同的处境:
 * 新用户看到会以为系统坏了, 老用户看到会以为漏了筛选条件。
 */
function emptyStateHtml({ filtered }) {
    if (filtered) {
        return '<tr><td colspan="9" class="empty">'
            + '<b>当前筛选条件下没有记录</b>'
            + '<span class="small muted" style="display:block;margin-top:4px;">'
            + '试着放宽日期范围或清空关键字；点「重置」可恢复全部记录。</span>'
            + '</td></tr>';
    }
    return '<tr><td colspan="9" class="empty">'
        + '<b>还没有任何检测记录</b>'
        + '<span class="small muted" style="display:block;margin-top:4px;">'
        + '去「图片检测」页上传一张影像，或到「视频检测」页上传一段视频即可生成第一条记录。</span>'
        + '</td></tr>';
}

async function loadRecords() {
    const from = $('#filter-from').value;
    const to = $('#filter-to').value;
    if (from && to && from > to) {
        toast('起始日期不能晚于结束日期', 'error');
        return;
    }
    const requestId = ++recordsRequest;
    const tbody = $('#records-body');
    try {
        const data = await API.get(buildQuery());
        if (requestId !== recordsRequest) return;
        $('#record-count').textContent = `共 ${data.total} 条记录`;

        if (!data.items.length) {
            if (data.total > 0 && state.page > 1) {
                state.page = Math.max(1, data.total_pages);
                return loadRecords();
            }
            tbody.innerHTML = emptyStateHtml({ filtered: isFiltered() });
            $('#pagination').innerHTML = '';
            currentItems = new Map();
            renderSelectBar();
            return;
        }

        currentItems = new Map(data.items.map((r) => [Number(r.id), r]));
        // 勾选按"ID 列表"而不是"本页可见行"维护: 报告 / 标签导出走明确的 ID 列表,
        // 不受分页影响, 因此同一筛选条件下跨页勾选是合法的。
        // 只有筛选条件变了(数据换了一批)才清空勾选, 否则会拿着上一批的 id 去导出。
        const visible = new Set(data.items.map((r) => Number(r.id)));
        const filterKey = buildQuery().replace(/[?&]page=\d+/, '');
        if (lastFilterKey !== null && lastFilterKey !== filterKey) {
            selectedIds = [];
            const card = $('#compare-card');
            if (card && !card.classList.contains('hidden')) {
                card.classList.add('hidden');
                destroyCompareCharts();
            }
        }
        lastFilterKey = filterKey;

        const offPage = selectedIds.filter((id) => !visible.has(id));
        if (offPage.length) {
            // 只提示不剔除 —— 免得用户翻页看一眼就把辛苦勾的几条弄丢
            toast(`${offPage.length} 条已选记录不在当前页, 勾选依然有效`, 'info', 3000);
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
            return `<tr class="${isSelected(r.id) ? 'row-selected' : ''}">
                <td class="col-check"><input type="checkbox" data-select="${r.id}"
                    ${isSelected(r.id) ? 'checked' : ''}
                    title="勾选后可参与多记录对比" aria-label="选择记录 ${r.id}"></td>
                <td>${r.id}</td>
                <td>${thumb}</td>
                <td>${escapeHtml(r.created_at)}</td>
                <td>${escapeHtml(r.source_name)}</td>
                <td>${mediaTypeBadge(r.task_type)}</td>
                <td>${fmtNumber(effectiveCount(r))}
                    <span class="small muted" style="display:block;font-size:11px;">${escapeHtml(countSourceLabel(r))}</span></td>
                <td class="small muted">${cost}</td>
                <td class="row-actions">
                    <button class="btn btn-sm" data-detail="${r.id}">详情</button>
                    <button class="btn btn-sm btn-secondary" data-repro="${r.id}"
                        title="打包源文件 / 参数快照 / 明细 CSV / 逐帧统计 / 环境信息 / README">复现包</button>
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
        $$('#records-body [data-repro]').forEach((btn) => {
            btn.addEventListener('click', () => downloadRepro(btn.dataset.repro));
        });
        $$('#records-body [data-select]').forEach((cb) => {
            cb.addEventListener('change', () => {
                const ok = toggleSelect(cb.dataset.select, cb.checked);
                if (!ok) {
                    cb.checked = false;
                    return;
                }
                const row = cb.closest('tr');
                if (row) row.classList.toggle('row-selected', cb.checked);
                renderSelectBar();
                invalidateCompareView();
            });
        });

        renderSelectBar();
        renderPagination(data.page, data.total_pages);
    } catch (err) {
        if (requestId !== recordsRequest) return;
        tbody.innerHTML = `<tr><td colspan="9" class="empty">加载失败: ${escapeHtml(err.message)}</td></tr>`;
        $('#pagination').innerHTML = '';
        $('#record-count').textContent = '';
        currentItems = new Map();
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
    const requestId = ++detailRequest;
    let record;
    try {
        record = await API.get(`/api/records/${id}`);
        if (requestId !== detailRequest) return;
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
        </p>
        <p class="small muted hidden" data-playback-error>此浏览器无法播放该文件，请下载结果视频后使用本地播放器查看。</p>`;
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
    $('#modal-root').innerHTML = '';
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
                <span>本记录计数: ${numberDrill(fmtNumber(effectiveCount(record)), recordDrill(record))}
                    <span class="small muted">(${escapeHtml(countSourceLabel(record))})</span></span>
                ${caliberBadge('record.effective_count', '口径')}
                ${isVideo ? `<span>累计检出框: ${numberDrill(String(record.total_detections ?? 0),
                        frameRowsDrill(record, '累计检出框的逐帧来源'))}</span>
                             <span>帧数: <b>${record.frames_processed ?? 0}</b></span>
                             <span>平均速度: <b>${(record.avg_fps ?? 0).toFixed(1)} FPS</b></span>
                             <span>时长: <b>${record.duration_sec ?? 0}s</b></span>`
                        : `<span>推理耗时: <b>${(record.inference_ms ?? 0).toFixed(0)} ms</b></span>
                           <span>分辨率: <b>${record.image_width ?? '-'} × ${record.image_height ?? '-'}</b></span>`}
                <span>置信度阈值: <b>${record.conf_threshold ?? '-'}</b></span>
            </div>
            <div class="summary-line" style="margin-top:8px;">
                <span>调查站点: <b>${escapeHtml(record.site_label || '未标注站点')}</b></span>
                <span>样方 / 断面: <b>${escapeHtml(record.transect_label || '未标注样方')}</b></span>
                <span>水深: <b>${record.depth_m === null || record.depth_m === undefined
                    ? '未记录' : `${record.depth_m} m`}</b></span>
                <span>调查轮次: <b>${escapeHtml(record.survey_round || '未标注轮次')}</b></span>
            </div>
            <p class="small muted">计数口径：${escapeHtml(countingDescription(record))}。</p>
            ${isVideo ? `<p class="small muted">追踪粒度：${escapeHtml(trackingGranularity(record))}</p>` : ''}
            <p style="margin:10px 0 0;">
                <button class="btn btn-sm btn-secondary" data-repro-detail="${record.id}"
                    title="打包源文件 / 参数快照 / 明细 CSV / 逐帧统计 / 模型摘要 / 环境信息 / README">
                    ⬇ 导出可复现包 (zip)
                </button>
                <span class="small muted" style="margin-left:8px;">包内 README 说明如何用同一套参数重跑</span>
            </p>
            <div id="detail-preflight" class="preflight-box">
                <span class="muted small">正在检查导出条件...</span>
            </div>
            ${maxnBlock(record, isVideo)}
            ${provenanceHtml(record)}
            <h3 style="font-size:14px;margin:16px 0 8px;">检测结果</h3>
            ${media}
            <h3 style="font-size:14px;margin:16px 0 8px;">类别统计</h3>
            <div class="tag-list">${classTags}</div>
            <h3 style="font-size:14px;margin:16px 0 8px;">检出类别分布指标
                ${caliberBadge('ecology.shannon', '公式')}</h3>
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
    const player = $('video', modal);
    if (player) player.addEventListener('error', () => {
        $('[data-playback-error]', modal).classList.remove('hidden');
    });

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
    const reproBtn = $('[data-repro-detail]', modal);
    if (reproBtn) {
        reproBtn.addEventListener('click', () => downloadRepro(reproBtn.dataset.reproDetail));
    }

    renderDetailEcology(record, modal);
    renderDetailPreflight(record.id, modal);
}

/** 在详情弹窗里展示导出预检结论。
 *
 * 预检本身是只读接口, 失败不能影响详情弹窗的其它内容 ——
 * 因此单独请求、单独降级: 拿不到就折叠掉这一块, 不弹错误。
 */
async function renderDetailPreflight(id, modal) {
    const box = $('#detail-preflight', modal);
    if (!box) return;
    let data;
    try {
        data = await API.get(`/api/report/preflight/${id}`);
    } catch (err) {
        // 弹窗可能已被用户关掉, 此时 box 已脱离文档, 直接放弃渲染
        box.classList.add('hidden');
        return;
    }
    if (!box.isConnected && !box.parentNode) return;

    const notes = data.notes || [];
    const sizeText = data.asset_bytes
        ? `${fmtBytes(data.asset_bytes)} / 上限 ${fmtBytes(data.bundle_size_limit)}`
        : '无素材文件';

    box.innerHTML = `
        <div class="pf-title">导出预检
            <span class="small muted">（只读检查, 不生成文件）</span>
        </div>
        <div class="pf-grid">
            <span>可复现包</span><span>${data.bundle_exportable
                ? '<span class="pf-ok">可导出</span>' : '<span class="pf-no">不可导出</span>'}</span>
            <span>训练标签</span><span>${data.label_exportable
                ? `<span class="pf-ok">可导出</span><span class="small muted">（${(data.label_formats || []).join(' / ')}）</span>`
                : '<span class="pf-no">不可导出</span>'}</span>
            <span>素材体积</span><span class="small">${escapeHtml(sizeText)}</span>
            <span>明细行数</span><span class="small">${fmtNumber(data.item_rows)}
                ${data.total_detections && data.item_rows < data.total_detections
                    ? `<span class="muted"> / 累计检出 ${fmtNumber(data.total_detections)} 框</span>` : ''}</span>
            <span>计数口径</span><span class="small">${escapeHtml(data.counting_source_label || '')}</span>
        </div>
        ${notes.length ? `<ul class="pf-notes">${notes.map((n) => `<li>${escapeHtml(n)}</li>`).join('')}</ul>`
            : '<p class="small pf-ok" style="margin:6px 0 0;">一切就绪, 导出不会缺件。</p>'}`;
}

/** 在详情弹窗内展示该条记录的生态指标。
 *
 * 直接复用后端同一套公式(见 app/core/ecology.py), 但用已有的 class_stats 就地计算,
 * 省一次请求。公式必须与后端保持一致, 否则同一条记录在两处显示不同数值。
 */
function renderDetailEcology(record, modal) {
    const box = $('#detail-ecology', modal);
    if (!box) return;
    if (record.task_type === 'video') {
        box.textContent = '视频轨迹数或逐帧累计框数不纳入图片类别分布指标；请结合计数口径查看类别统计。';
        return;
    }

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
        <span>检出最多类别: <b>${escapeHtml(dom.name)}</b>
              (${(dom.n / total * 100).toFixed(1)}%)</span>
        <span>检出类别数: <b>${S}</b> / 检出框数: <b>${total}</b></span>
        <span class="small muted">仅描述模型检出类别，不代表实际物种数或生态质量。</span>`;
}

/* ---------------- 多记录对比视图 ---------------- */

/** 对比用的列标识色(与类别饼图配色分开, 避免"颜色 = 类别"的误读)。 */
const COMPARE_COLORS = ['#0e7490', '#8b5cf6', '#d97706', '#be123c'];

function compareColor(index) {
    return COMPARE_COLORS[index % COMPARE_COLORS.length];
}

/** 每列的表头: 记录 id / 文件 / 类型 / 关键参数, 让人看清"在比什么"。 */
function compareHead(col, index) {
    const isVideo = col.task_type === 'video';
    const params = col.params || {};
    const bits = [
        `置信度阈值 ${col.conf_threshold ?? '-'}`,
        `IoU ${params.iou_threshold ?? '-'}`,
        `尺寸 ${params.imgsz ?? '-'}`,
    ];
    if (isVideo) bits.push(`抽帧 ${params.frame_skip ?? '-'}`);
    // 后端给的是已拼好的 result_url / result_video_url(而不是裸文件名),
    // 这里直接使用, 不要再用 result_image 之类的字段名。
    const preview = col.result_url
        ? `<a href="${escapeHtml(col.result_url)}" target="_blank" rel="noopener"
              title="点击查看结果大图"><img class="compare-thumb"
              src="${escapeHtml(col.result_url)}" alt="结果 ${escapeHtml(col.source_name)}"></a>`
        : (col.result_video_url
            ? `<a class="compare-noimg" href="${escapeHtml(col.result_video_url)}"
                  target="_blank" rel="noopener">▶ 结果视频</a>`
            : '<span class="compare-noimg muted">该记录未生成结果文件</span>');
    return `<th>
        <div class="compare-head" style="border-top:3px solid ${compareColor(index)}">
            <div class="compare-id">#${col.record_id}
                ${mediaTypeBadge(col.task_type)}</div>
            <div class="compare-name" title="${escapeHtml(col.source_name)}">${escapeHtml(col.source_name)}</div>
            <div class="small muted">${escapeHtml(col.created_at || '')}</div>
            <div class="compare-params">${bits.map((b) => `<span>${escapeHtml(b)}</span>`).join('')}</div>
            ${preview}
        </div>
    </th>`;
}

/** 表体的一行数字, 按列取值。 */
function compareRow(label, cells) {
    return `<tr><th class="compare-metric">${escapeHtml(label)}</th>${cells.join('')}</tr>`;
}

/** 各口径的一句话解释: 只说"这个数是怎么数的", 不评价好坏。 */
const CALIBER_EXPLAIN = {
    unique_tracks: '跨帧关联后按轨迹去重, 一条鱼经过多帧只算一次',
    maxn: '取单帧内同类别的同时出现数量最大值, 属相对丰度',
    total_detections: '所有已处理帧的检出框求和, 同一目标跨帧会重复计入',
    image_objects: '本图内的检出框数, 与明细逐条对应',
};

function caliberExplain(source) {
    return CALIBER_EXPLAIN[source] || '记录中保存的计数口径';
}

/** 对比表里的"该口径在此记录不适用"占位: 说明原因, 而不是一个孤零零的破折号。 */
function notApplicable(reason) {
    return `<span class="muted small">不适用<span class="na-hint">(${escapeHtml(reason)})</span></span>`;
}

function fmtValue(value, suffix = '') {
    if (value === null || value === undefined || value === '') {
        return notApplicable('无可用取值');
    }
    return `${escapeHtml(value)}${escapeHtml(suffix)}`;
}

/** 置信度直方图说明文字(图表本体由 renderCompareView 统一创建, 避免同一 id 重复)。 */
function compareHistogramNote(data) {
    const hist = data.conf_histograms || [];
    const first = hist.length ? (hist[0].bins || []) : [];
    if (!first.length) return '<p class="muted small">所选记录没有明细样本, 无法绘制置信度分布。</p>';
    return `<p class="small muted">横轴为置信度区间下界(共 ${first.length} 个分箱,
        与统计页同一边界规则: 0.01 ~ 0.99 等宽); 纵轴为该区间内的检出框数。
        分布形状可比, 绝对高度受样本量影响。</p>`;
}

/** 阈值敏感性表: 同一行看"若把阈值提到 x, 各记录还剩多少"。
 *
 * 阈值列表直接取自后端返回的 sensitivity 数组, 不在前端另写一份 ——
 * 否则前后端一旦各自增删阈值, 表格就会出现"有行无数/有多余行"的错位。
 */
function renderSensitivity(data) {
    const cols = data.columns || [];
    const thresholds = [];
    cols.forEach((col) => (col.conf.sensitivity || []).forEach((s) => {
        if (!thresholds.includes(s.threshold)) thresholds.push(s.threshold);
    }));
    thresholds.sort((a, b) => a - b);
    if (!thresholds.length) {
        return '<p class="muted small">所选记录没有明细样本, 无法给出阈值敏感性。</p>';
    }
    const rows = thresholds.map((t) => {
        const cells = cols.map((col) => {
            const hit = (col.conf.sensitivity || []).find((s) => Math.abs(s.threshold - t) < 1e-9);
            if (!hit) return `<td class="muted small">不适用</td>`;
            return `<td>${fmtNumber(hit.kept)} <span class="small muted">(${hit.ratio}%)</span></td>`;
        });
        return `<tr><th class="compare-metric">阈值 ≥ ${t}</th>${cells.join('')}</tr>`;
    }).join('');
    return `<div class="table-wrap"><table class="compare-table">
        <thead><tr><th class="compare-metric">置信度阈值</th>${cols.map((col, i) =>
            `<th><span class="compare-swatch" style="background:${compareColor(i)}"></span>#${col.record_id}</th>`
        ).join('')}</tr></thead>
        <tbody>${rows}</tbody>
    </table></div>
    <p class="small muted">数值为"明细样本中置信度不低于该阈值的框数(占比)", 用于判断阈值是否偏松或偏紧。</p>`;
}

/** 类别构成矩阵: 行=类别并集, 列=记录, 缺失补 0; 右侧附各记录最大值标记。 */
function renderClassMatrix(data) {
    const cols = data.columns || [];
    const matrix = data.class_matrix || [];
    if (!matrix.length) {
        return '<p class="muted small">所选记录都没有类别汇总数据(可能未产生检出目标)。</p>';
    }
    const body = matrix.map((row) => {
        const values = cols.map((col) => Number(row.counts[String(col.record_id)] || 0));
        const peak = Math.max(...values);
        const cells = cols.map((col, i) => {
            const v = values[i];
            const isPeak = peak > 0 && v === peak && values.filter((x) => x === peak).length < values.length;
            return `<td class="${v ? '' : 'zero-cell'}">${v ? fmtNumber(v) : '0'}${isPeak
                ? '<span class="peak-mark" title="该类别在本组中最多">▲</span>' : ''}</td>`;
        });
        return `<tr><th class="compare-metric">${escapeHtml(row.class_name)}</th>${cells.join('')}</tr>`;
    }).join('');
    return `<div class="table-wrap"><table class="compare-table">
        <thead><tr><th class="compare-metric">类别</th>${cols.map((col, i) =>
            `<th><span class="compare-swatch" style="background:${compareColor(i)}"></span>#${col.record_id}</th>`
        ).join('')}</tr></thead>
        <tbody>${body}</tbody>
    </table></div>
    <p class="small muted">类别构成取自入库的类别汇总(视频为采样汇总), 未出现的类别补 0 以便横向对齐;
        ▲ 标记该类别在本组内最多的一项。</p>`;
}

/** 口径说明区: 先讲清"这些数能不能直接比", 再讲各自取的是哪套口径。 */
function renderCompareCaliber(data) {
    const cols = data.columns || [];
    const warnings = data.warnings || [];
    const badge = data.comparable
        ? '<span class="caliber-verdict ok">口径一致, 数值可直接比较</span>'
        : '<span class="caliber-verdict warn">口径不一致, 数值不可直接比较</span>';
    const list = cols.map((col, i) => {
        const label = COUNT_SOURCE_LABEL[col.caliber.source] || col.caliber.source;
        return `<tr>
        <th>#${col.record_id}</th>
        <td><span class="compare-swatch" style="background:${compareColor(i)}"></span>
            ${escapeHtml(label)}
            <span class="mono small muted">${escapeHtml(col.caliber.source)}</span></td>
        <td>${fmtNumber(col.caliber.value)}</td>
        <td class="small muted">${escapeHtml(caliberExplain(col.caliber.source))}</td>
    </tr>`;
    }).join('');
    return `${badge}
    ${warnings.length ? `<ul class="compare-warnings">${
        warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul>` : ''}
    <div class="table-wrap"><table class="compare-table">
        <thead><tr><th class="compare-metric">记录</th><th>计数口径</th><th>该口径下的计数</th><th>口径说明</th></tr></thead>
        <tbody>${list}</tbody>
    </table></div>
    <p class="small muted">${escapeHtml((data.scope || {}).limitation || '')}</p>`;
}

/** 主渲染: 把对比结果整块铺开。 */
/** 渲染对比表中的单个单元格。
 *
 * 与 compareMetrics() 的取值一一对应, 但这里保留页面需要的富展示:
 * 口径色块、计数加粗、不可用项的灰字原因。CSV 导出只取纯值。
 */
function compareCell(label, c, index, data) {
    const na = (text) => notApplicable(text);
    switch (label) {
        case '文件名称':
            return `<td class="compare-cell-text" title="${escapeHtml(c.source_name)}">${escapeHtml(c.source_name)}</td>`;
        case '检测类型':
            return `<td>${c.task_type === 'video' ? '视频' : '图片'}</td>`;
        case '检测时间':
            return `<td class="small">${escapeHtml(c.created_at || '')}</td>`;
        case '计数口径':
            return `<td><span class="compare-swatch" style="background:${compareColor(index)}"></span>`
                + `${escapeHtml(COUNT_SOURCE_LABEL[c.caliber.source] || c.caliber.source)}</td>`;
        case '本记录计数':
            return `<td><b>${fmtNumber(c.caliber.value)}</b></td>`;
        case '累计检出框':
            return `<td>${fmtNumber(c.total_detections)}</td>`;
        case '轨迹数':
            return `<td>${c.tracked_objects
                ? fmtNumber(c.tracked_objects)
                : na(c.task_type === 'video' ? '未建立完整轨迹' : '仅视频记录有')}</td>`;
        case '单帧峰值 MaxN':
            return `<td>${c.maxn_total
                ? fmtNumber(c.maxn_total)
                : na(c.task_type === 'video' ? '未保存逐帧类别计数' : '仅视频记录有')}</td>`;
        case 'CPUnx':
            return `<td>${c.cpunx === null || c.cpunx === undefined
                ? na('需 MaxN 与时长') : fmtValue(c.cpunx)}</td>`;
        case '检出类别数':
            return `<td>${c.class_count}</td>`;
        case '明细样本量':
            return `<td>${fmtNumber(c.conf.count)}${c.task_type === 'video'
                ? '<span class="small muted"> (采样)</span>' : ''}</td>`;
        case '置信度均值':
            return `<td>${fmtValue(c.conf.mean)}</td>`;
        case '置信度中位数':
            return `<td>${fmtValue(c.conf.median)}</td>`;
        case 'P25':
            return `<td>${c.conf.p25 === null ? na('无明细样本') : c.conf.p25}</td>`;
        case 'P75':
            return `<td>${c.conf.p75 === null ? na('无明细样本') : c.conf.p75}</td>`;
        case '置信度区间':
            return `<td class="small">${c.conf.min === null
                ? na('无明细样本') : `${c.conf.min} ~ ${c.conf.max}`}</td>`;
        case '调查站点':
            return `<td class="small">${escapeHtml(c.site_label || '未标注站点')}</td>`;
        case '样方 / 断面':
            return `<td class="small">${escapeHtml(c.transect_label || '未标注样方')}</td>`;
        case '调查轮次':
            return `<td class="small">${escapeHtml(c.survey_round || '未标注轮次')}</td>`;
        case '水深(m)':
            return `<td class="small">${c.depth_m === null || c.depth_m === undefined
                ? '<span class="muted">未记录</span>' : `${c.depth_m} m`}</td>`;
        default:
            void data;
            return '<td class="muted small">-</td>';
    }
}

function renderCompareView(data) {
    const cols = data.columns || [];
    if (!cols.length) return;
    // 表格与 CSV 导出共用 compareMetrics(), 数值只会算一次, 不会两处对不上
    const rowsHtml = compareMetrics().map((metric) => compareRow(
        metric.label,
        cols.map((c, i) => compareCell(metric.label, c, i, data)),
    )).join('');

    const card = $('#compare-card');
    $('#compare-subtitle').textContent = data.comparable
        ? `${cols.length} 条记录 · 口径一致`
        : `${cols.length} 条记录 · 口径不一致`;
    $('#compare-body').innerHTML = `
        <div class="table-wrap">
            <table class="compare-table compare-overview">
                <thead><tr><th class="compare-metric">对比项</th>${cols.map(compareHead).join('')}</tr></thead>
                <tbody>${rowsHtml}</tbody>
            </table>
        </div>
        <h3 class="compare-section-title">计数口径与可比性</h3>
        ${renderCompareCaliber(data)}
        <h3 class="compare-section-title">类别构成并列</h3>
        ${renderClassMatrix(data)}
        <h3 class="compare-section-title">置信度分布</h3>
        <div class="chart-box" style="height:320px;">
            <canvas id="compare-chart-conf"></canvas>
        </div>
        ${compareHistogramNote(data)}
        <h3 class="compare-section-title">阈值敏感性</h3>
        ${renderSensitivity(data)}
        <div class="compare-foot">
            <span class="small muted">${escapeHtml((data.scope || {}).selection || '')}</span>
            <span>
                <button class="btn btn-sm btn-secondary" id="btn-compare-export"
                        title="把这张对比表(指标为行、记录为列)导出为 CSV, 可直接粘进 Excel 或论文">导出对比 CSV</button>
                <button class="btn btn-sm btn-secondary" id="btn-compare-close">收起对比</button>
            </span>
        </div>`;

    card.classList.remove('hidden');
    // 滚动放到渲染之后: 即便某个环境不支持 scrollIntoView, 也不能让整块视图渲染失败
    if (typeof card.scrollIntoView === 'function') {
        card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    $('#btn-compare-close').addEventListener('click', () => {
        card.classList.add('hidden');
        destroyCompareCharts();
    });
    const exportBtn = $('#btn-compare-export');
    if (exportBtn) exportBtn.addEventListener('click', exportCompareCsv);

    // 图表: 每条记录一条折线/柱, 颜色与表头标识色一致
    if (typeof Chart !== 'undefined') {
        applyChartTheme();
        const hist = data.conf_histograms || [];
        const firstBins = hist.length ? (hist[0].bins || []) : [];
        if (firstBins.length) {
            const canvas = $('#compare-chart-conf');
            if (canvas) {
                compareCharts.push(new Chart(canvas, {
                    type: 'bar',
                    data: {
                        labels: firstBins.map((b) => b.lower.toFixed(2)),
                        datasets: hist.map((h, i) => {
                            const col = cols[i] || {};
                            return {
                                label: `#${h.record_id} ${col.source_name || ''}`,
                                data: (h.bins || []).map((b) => b.count),
                                backgroundColor: compareColor(i),
                                borderRadius: 2,
                            };
                        }),
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            title: { display: true, text: '置信度分布并列对比' },
                            legend: { position: 'bottom' },
                        },
                        scales: {
                            y: { beginAtZero: true, ticks: { precision: 0 } },
                            x: { title: { display: true, text: '置信度区间下界' } },
                        },
                    },
                }));
            }
        }
    }
    renderSelectBar();
}

/** 请求并渲染对比。失败时在卡片内给出可读原因, 不清空已有内容以外的状态。 */
async function openCompare() {
    if (selectedIds.length < 2) {
        toast('请至少勾选 2 条记录再对比', 'error');
        return;
    }
    const btn = $('#btn-compare');
    const prevText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '对比加载中...';
    try {
        const data = await API.postJSON('/api/compare', {
            record_ids: selectedIds,
            bins: 10,
        });
        destroyCompareCharts();
        const missing = data.missing_ids || [];
        if (missing.length) {
            toast(`有 ${missing.length} 条记录已不存在, 已从对比中跳过: ${missing.join(', ')}`, 'error', 4500);
            selectedIds = selectedIds.filter((id) => !missing.includes(id));
            $$('#records-body [data-select]').forEach((cb) => {
                if (missing.includes(Number(cb.dataset.select))) {
                    cb.checked = false;
                    const row = cb.closest('tr');
                    if (row) row.classList.remove('row-selected');
                }
            });
        }
        renderCompareView(data);
        lastCompareData = data;
    } catch (err) {
        toast('对比失败: ' + err.message, 'error', 4500);
    } finally {
        btn.disabled = selectedIds.length < 2;
        btn.textContent = selectedIds.length >= 2 ? `对比所选 (${selectedIds.length})` : prevText;
        renderSelectBar();
    }
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

/** 全选 / 取消全选本页记录。
 *
 * 表头复选框与 Ctrl+A 快捷键共用这一份逻辑: 两处各写一遍必然会分叉,
 * 比如快捷键不遵守对比上限、或忘了同步表头复选框状态。
 */
function applySelectAll(on) {
    const boxes = $$('#records-body [data-select]');
    const head = $('#select-all');
    if (!boxes.length) return false;
    if (on && selectedIds.length + boxes.length > MAX_COMPARE) {
        // 全选会超上限时, 明确告知而不是静默截断
        toast(`本页有 ${boxes.length} 条记录, 超过单次对比上限 ${MAX_COMPARE} 条, 请逐个勾选`,
            'error', 4500);
        if (head) {
            head.checked = false;
            head.indeterminate = false;
        }
        return false;
    }
    boxes.forEach((cb) => {
        cb.checked = on;
        toggleSelect(cb.dataset.select, on);
        const row = cb.closest('tr');
        if (row) row.classList.toggle('row-selected', on);
    });
    if (head) head.checked = on;
    renderSelectBar();
    invalidateCompareView();
    return true;
}

/** Ctrl/Cmd+A 的语义: 尽力选中本页记录, 但绝不超过对比上限。
 *
 * 表头复选框沿用 applySelectAll 的"要么全选、要么拒绝"语义(用户明确点了全选);
 * 快捷键不同 —— 每页 10 条而上限 4 条时, 若同样干脆拒绝, Ctrl+A 就永远没用。
 * 因此这里按剩余额度选中前 N 条, 并说明只选了一部分。
 */
function selectPageUpToLimit() {
    const boxes = $$('#records-body [data-select]');
    if (!boxes.length) return false;
    const room = MAX_COMPARE - selectedIds.length;
    if (room <= 0) {
        toast(`已达单次对比上限 ${MAX_COMPARE} 条, 如需换一批请先按 Esc 清空`, 'error', 4000);
        return false;
    }
    const picked = boxes.slice(0, room);
    picked.forEach((cb) => {
        cb.checked = true;
        toggleSelect(cb.dataset.select, true);
        const row = cb.closest('tr');
        if (row) row.classList.add('row-selected');
    });
    const head = $('#select-all');
    if (head) {
        head.checked = picked.length === boxes.length;
        head.indeterminate = picked.length > 0 && picked.length < boxes.length;
    }
    renderSelectBar();
    invalidateCompareView();
    if (picked.length < boxes.length) {
        toast(`已选中前 ${picked.length} 条（单次对比上限 ${MAX_COMPARE} 条）`, 'info', 3500);
    }
    return true;
}

document.addEventListener('DOMContentLoaded', () => {
    const stationFilter = $('#filter-site');
    const linkedStation = new URLSearchParams(location.search).get('site_id');
    if (stationFilter) {
        if (linkedStation && /^[1-9][0-9]*$/.test(linkedStation)) {
            stationFilter.add(new Option(`站点 #${linkedStation}`, linkedStation));
            stationFilter.value = linkedStation;
        }
        API.get('/api/stations').then(data => {
            const previous = stationFilter.value;
            stationFilter.replaceChildren(new Option('全部站点（含旧未标注记录）',''));
            data.items.forEach(s => stationFilter.add(new Option(s.name + (s.active ? '' : '（已停用）'), String(s.id))));
            if (previous && !data.items.some(s => String(s.id)===previous)) stationFilter.add(new Option(`站点 #${previous}（不存在）`,previous));
            stationFilter.value = previous;
        }).catch(err => toast('站点筛选加载失败：' + err.message,'error'));
        stationFilter.addEventListener('change', () => {state.page=1;loadRecords();});
    }
    loadRecords();
    const linkedRecord = new URLSearchParams(window.location.search).get('record_id');
    if (linkedRecord && /^[1-9][0-9]*$/.test(linkedRecord)) openDetail(linkedRecord);
    $('#btn-search').addEventListener('click', () => {
        state.page = 1;
        loadRecords();
    });
    $('#btn-clear').addEventListener('click', () => {
        $('#filter-type').value = '';
        $('#filter-keyword').value = '';
        $('#filter-from').value = '';
        $('#filter-to').value = '';
        if ($('#filter-site')) $('#filter-site').value = '';
        state.page = 1;
        loadRecords();
    });
    $('#filter-keyword').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            state.page = 1;
            loadRecords();
        }
    });

    // ---- 勾选 / 对比 ----
    $('#select-all').addEventListener('change', (e) => applySelectAll(e.target.checked));
    $('#btn-compare').addEventListener('click', openCompare);
    $('#btn-bulk-report').addEventListener('click', () => bulkExport('report'));
    $('#btn-bulk-export').addEventListener('click', () => bulkExport('labels'));
    $('#btn-clear-select').addEventListener('click', clearSelection);

    // 键盘快捷键: 批量操作要一条条点复选框太慢, 但快捷键必须让位于输入框与弹窗
    document.addEventListener('keydown', (e) => {
        // 在输入框里按 Ctrl+A 是要全选文字, 不能抢; 弹窗打开时也不抢
        const tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (e.target && e.target.isContentEditable) return;
        if ($('#modal-root') && $('#modal-root').children.length) return;

        if ((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A')) {
            // 保住浏览器默认的"全选页面文字"只在没有可勾选内容时才有意义
            const boxes = $$('#records-body [data-select]');
            if (!boxes.length) return;
            e.preventDefault();
            selectPageUpToLimit();
            return;
        }
        if (e.key === 'Escape') {
            if (!selectedIds.length) return;
            e.preventDefault();
            clearSelection();
            invalidateCompareView();
            toast('已清空所选', 'info', 2000);
        }
    });

    // 主题切换后对比图表需要重绘(颜色写在创建时, Chart.js 不会自动跟随)
    onThemeChange(() => {
        const card = $('#compare-card');
        if (card && !card.classList.contains('hidden')) {
            // 标记需要重绘: 下次打开对比时自动使用新配色
            destroyCompareCharts();
        }
    });
});
