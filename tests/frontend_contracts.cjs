/* Regression checks using browser-independent DOM stubs and deferred uploads. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page(script) {
    const elements = new Map();
    const notices = [];
    const charts = [];
    function element(selector) {
        if (!elements.has(selector)) elements.set(selector, {
            value: selector === '#conf-slider' ? '0.25' : '', textContent: '', innerHTML: '',
            disabled: false, style: {}, dataset: {}, children: [],
            classList: {
                add() {}, remove() {}, toggle() {},
                // 面板显隐判断用的是 contains('hidden'), 桩里必须给出来
                contains: () => true,
            },
            setAttribute() {}, addEventListener() {}, getContext() { return {}; },
            appendChild() {},
        });
        return elements.get(selector);
    }
    const context = vm.createContext({
        document: { addEventListener() {} }, window: { addEventListener() {} },
        $: element, $$: () => [], API: {}, performance: { now: () => 1000 },
        Image: class {}, crypto: require("node:crypto"),
        FormData: class { constructor() { this.values = []; } append(k, v) { this.values.push([k, v]); } entries() { return this.values.values(); } },
        WebSocket: { CLOSED: 3, OPEN: 1 },
        toast: (...args) => notices.push(args),
        fmtNumber: (v) => String(v ?? 0),
        mediaTypeBadge: (type) => `<span aria-label="${type}"></span>`, 
        recordStatusBadge: (status) => `<span aria-label="${status}"></span>`,
        // 与 web/js/common.js 同义的精简实现: 只关心单位换算是否正确
        fmtBytes(n) {
            const v = Number(n);
            if (!Number.isFinite(v) || v <= 0) return '0 B';
            const units = ['B', 'KB', 'MB', 'GB'];
            let value = v;
            let i = 0;
            while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
            return `${i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
        },
        // 对比表渲染依赖的口径文案表与"不适用"占位
        COUNT_SOURCE_LABEL: {
            unique_tracks: '同一目标跨帧去重',
            maxn: '单帧峰值 MaxN',
            total_detections: '累计检出框',
            image_objects: '图片检出框',
        },
        notApplicable: (reason) => `<span class="na-hint">(${reason})</span>`,
        fmtValue: (v) => (v === null || v === undefined ? '-' : String(v)),
        compareColor: () => '#0e7490',
        compareHead: () => '<th>#</th>',
        compareRow: (label, cells) => `<tr><th>${label}</th>${cells.join('')}</tr>`,
        renderCompareCaliber: () => '',
        renderClassMatrix: () => '',
        compareHistogramNote: () => '',
        renderSensitivity: () => '',
        invalidateCompareView() {},
        destroyCompareCharts() {},
        MAX_COMPARE: 4,
        MAX_EXPORT_RECORDS: 50,
        escapeHtml: (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c])),
        Chart: function (canvas, config) { charts.push(config); this.destroy = () => {}; },
        applyChartTheme() {}, isDarkTheme: () => false, drawDetections() {},
        URLSearchParams,
        // 调查维度辅助: 由 common.js 提供, 这里给出等价的精简实现
        appendSurvey(formData, prefix = 'survey-') {
            [['site_id', `${prefix}site`], ['transect', `${prefix}transect`],
             ['depth_m', `${prefix}depth`], ['survey_round', `${prefix}round`],
            ].forEach(([field, id]) => {
                const el = elements.get(`#${id}`);
                const value = el ? String(el.value ?? '').trim() : '';
                if (value) formData.append(field, value);
            });
            return formData;
        },
        // 口径与下钻组件: 只验证不抛错, 不在 Node 环境里渲染真实 DOM
        caliberBadge: (key, label = '口径') => `<details class="caliber">${label}:${key}</details>`,
        caliberBlock: (keys) => `<details class="caliber caliber-block">${keys.length}</details>`,
        numberDrill: (text) => `<button class="num-drill">${text}</button>`,
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'web', 'js', script), 'utf8'), context);
    return { context, elements, notices, charts, element, run: (code) => vm.runInContext(code, context) };
}

async function main() {
    const image = page('image.js');
    let uploads = 0;
    let completeUpload;
    image.context.API.upload = () => { uploads += 1; return new Promise((resolve) => { completeUpload = resolve; }); };
    image.run("selectedFiles = [{name:'first.jpg',size:12,type:'image/jpeg'}]; newQueue(selectedFiles); renderResults = (data) => { window.result = data; };");
    const pending = image.run('runDetect()');
    assert.equal(image.element('#btn-reset').disabled, true);
    assert.equal(image.element('#file-input').disabled, true);
    assert.equal(image.element('#conf-slider').disabled, true);
    image.run("selectFiles([{name:'second.jpg',size:12,type:'image/jpeg'}])");
    assert.equal(image.run('selectedFiles[0].name'), 'first.jpg');
    await image.run('runDetect()');
    assert.equal(uploads, 1);
    completeUpload({ total: 2, record_id: 1 });
    await pending;
    assert.equal(image.element('#btn-reset').disabled, false);
    assert.equal(image.context.window.result.total_objects, 2);
    image.run("selectFiles([{name:'too-large.jpg',size:301*1024*1024,type:'image/jpeg'}])");
    assert.equal(image.run('selectedFiles[0].name'), 'first.jpg');
    image.run("selectFiles([{name:'scan.tiff',size:12,type:''}])");
    assert.equal(image.run('selectedFiles[0].name'), 'scan.tiff');

    const records = page('records.js');
    assert.equal(records.run('hasValidUniqueCount({unique_count_valid:0})'), false);
    assert.equal(records.run('hasValidUniqueCount({unique_count_valid:1})'), true);
    records.run("renderDetailEcology({task_type:'video', unique_count_valid:false, class_stats:[{class_name:'fish', count:50}]}, {})");
    assert.match(records.element('#detail-ecology').textContent, /不纳入/);
    const escapedProvenance = records.run("provenanceHtml({inference_snapshot:{model:{name:'<unsafe>',sha256:'hash'},parameters:{imgsz:640}}})");
    assert.ok(escapedProvenance.includes('&lt;unsafe&gt;'));
    assert.ok(!escapedProvenance.includes('<unsafe>'));
    records.run('state.page = 2');
    let requests = 0;
    records.context.API.get = async () => {
        requests += 1;
        return requests === 1
            ? { items: [], total: 1, total_pages: 1 }
            : { items: [{ id: 1, task_type: 'image', total_objects: 16, unique_objects: 0 }], total: 1, page: 1, total_pages: 1 };
    };
    await records.run('loadRecords()');
    assert.equal(records.run('state.page'), 1);
    // 计数列现在同时给出数值与口径来源, 便于一眼看出这个数是怎么来的
    assert.match(records.element('#records-body').innerHTML, /<td>16\s*<span[^>]*>图片检出框<\/span><\/td>/);

    // 计数口径: 三档兜底后不再出现"不可用", 且展示取用来源
    assert.equal(records.run('effectiveCount({task_type:"video",unique_count_valid:false,unique_objects:0,total_objects:0,maxn_total:7,total_detections:99})'), 7);
    assert.equal(records.run('effectiveCount({task_type:"video",unique_count_valid:false,unique_objects:0,maxn_total:0,total_detections:99})'), 99);
    assert.equal(records.run('effectiveCount({task_type:"video",unique_count_valid:true,unique_objects:5,maxn_total:7,total_detections:99})'), 5);
    assert.equal(records.run('effectiveCount({task_type:"image",total_objects:4})'), 4);
    assert.equal(records.run('countSourceLabel({effective_count_source:"maxn"})'), '单帧峰值 MaxN');
    // 负面表述已改写为客观口径叙述
    const partialDesc = records.run('countingDescription({task_type:"video",unique_count_valid:false,effective_count_source:"maxn",tracked_objects:3})');
    assert.ok(!/失效|不可用|不足/.test(partialDesc), `口径说明仍含负面词: ${partialDesc}`);
    assert.match(partialDesc, /单帧峰值/);
    const gran = records.run('trackingGranularity({task_type:"video",tracked_objects:4,frames_processed:60})');
    assert.match(gran, /4 条轨迹/);
    assert.match(gran, /逐帧框坐标/);

    const video = page('video.js');
    video.run("selectFile({name:'clip.mkv',size:12,type:''})");
    assert.equal(video.run('currentFile.name'), 'clip.mkv');
    video.run("handleFrame({total:3,unique:2,unique_count_valid:false,counting_mode:'tracking_partial',maxn_total:2,warnings:['跟踪中断'],fps:2,timestamp:5,counts:[],detections:[],progress:10,frame_index:3,conf_threshold:0.25,image:''})");
    // 逐帧轨迹编号仍会增长, 因此照常展示并标注含义, 不再写"不可用"
    assert.equal(video.element('#live-unique').textContent, '2(轨迹编号)');
    assert.match(video.element('#progress-text').textContent, /离线分析.*预计剩余/);
    assert.match(video.element('#count-warning').textContent, /跟踪中断/);
    assert.match(video.element('#count-warning').textContent, /单帧峰值 MaxN 2 条/);
    video.run("handleDone({summary:{unique_objects:null,unique_count_valid:false,counting_mode:'frame_detections',maxn_total:6,warnings:[],total_detections:12,frames_processed:4,total_frames:4,avg_fps:2,peak_objects:3,avg_objects:3,counts:[]}})");
    // 汇总卡落到 MaxN 口径, 不再是"不可用"
    assert.equal(video.element('#sum-unique').textContent, '6');
    assert.equal(video.element('#sum-tags-title').textContent, '各类别单帧峰值 MaxN');
    assert.match(video.element('#sum-count-note').textContent, /相对丰度/);

    const stats = page('stats.js');
    stats.run("renderClassTrend({granularity:'week',labels:['10-01 周'],series:[{name:'fish',data:[3]}]})");
    assert.match(stats.charts[0].options.plugins.title.text, /按周/);
    stats.context.API.get = async () => ({ points: [{label:'10-01',shannon:null,pielou:null}] });
    await stats.run('renderEcologyTrend(7, 0)');
    for (const series of stats.charts[1].data.datasets) assert.equal(series.spanGaps, false);

    // 同名的当前站点、另一地图站点及历史站点必须分别下钻，不能只按名称混合。
    // 故意让接口返回混合记录，以同时验证请求参数和前端的最后一道筛选。
    const stationRequests = [];
    const mixedStationRecords = [
        { id: 901, site_id: 11, site: '同名站', transect: 'T1', total_objects: 2 },
        { id: 902, site_id: 22, site: '同名站', transect: 'T1', total_objects: 99 },
        { id: 903, site_id: null, site: '同名站', transect: 'T1', total_objects: 3 },
        { id: 904, site_id: null, site: '', transect: 'T1', total_objects: 5 },
        { id: 905, site_id: null, site: '其他历史站', transect: 'T1', total_objects: 7 },
        { id: 906, site_id: 11, site: '同名站', transect: 'T2', total_objects: 11 },
        { id: 907, site_id: 11, site: '同名站', transect: '', total_objects: 13 },
        { id: 908, site: '同名站', transect: 'T1', total_objects: 17 },
        { id: 909, site_id: null, site: '', transect: null, total_objects: 19 },
    ].map((r) => ({
        task_type: 'image', source_name: `${r.id}.jpg`, created_at: '2026-10-28 10:00:00',
        effective_count_source: 'image_objects', ...r,
    }));
    stats.context.API.get = async (url) => {
        stationRequests.push(new URL(url, 'http://test.local'));
        return { items: mixedStationRecords, total: mixedStationRecords.length };
    };
    const drillRecordIds = (result) => Array.from(result.rows, (row) => {
        const link = row[0].match(/data-record-link="(\d+)"/);
        assert.ok(link, '下钻记录应保留可打开详情的记录号');
        return Number(link[1]);
    });
    const drillSummary = (result, label) => Array.from(result.summary)
        .find((item) => item.label === label)?.value;

    const currentStation = await stats.run(`transectDrill(
        {site_id:11, site:'同名站', site_label:'同名站', legacy_site:false},
        {transect:'T1', transect_label:'T1'}, 7).loader()`);
    assert.equal(stationRequests[0].pathname, '/api/records');
    assert.equal(stationRequests[0].searchParams.get('site_id'), '11', '当前站点请求必须携带稳定编号');
    assert.deepEqual(drillRecordIds(currentStation), [901], '当前站点不能混入另一编号、历史记录或其他样方');
    assert.equal(drillSummary(currentStation, '匹配记录'), '1');
    assert.equal(drillSummary(currentStation, '检出计数'), '2');

    const legacyStation = await stats.run(`transectDrill(
        {site_id:null, site:'同名站', site_label:'同名站', legacy_site:true},
        {transect:'T1', transect_label:'T1'}, 0).loader()`);
    assert.equal(stationRequests[1].searchParams.has('site_id'), false, '历史站点不能发送 null 字符串编号');
    assert.deepEqual(drillRecordIds(legacyStation), [903, 908], '历史同名站点只接受 null 或未保存编号的历史记录');
    assert.equal(drillSummary(legacyStation, '匹配记录'), '2');
    assert.equal(drillSummary(legacyStation, '检出计数'), '20');

    const unlabeledStation = await stats.run(`transectDrill(
        {site_id:null, site:'', site_label:'未标注站点', legacy_site:true},
        {transect:'T1', transect_label:'T1'}, 0).loader()`);
    assert.equal(stationRequests[2].searchParams.has('site_id'), false);
    assert.deepEqual(drillRecordIds(unlabeledStation), [904], '未标注站点不能混入具名历史站点或其他样方');
    assert.equal(drillSummary(unlabeledStation, '匹配记录'), '1');
    assert.equal(drillSummary(unlabeledStation, '检出计数'), '5');

    assert.equal(stats.run("stationDisplayLabel({site_id:11,site:'同名站',site_label:'同名站',legacy_site:false})"), '同名站');
    assert.equal(stats.run("stationDisplayLabel({site_id:null,site:'同名站',site_label:'同名站',legacy_site:true})"), '同名站（历史站点）');
    assert.equal(stats.run("stationDisplayLabel({site_id:null,site:'',site_label:'未标注站点',legacy_site:true})"), '未标注站点');

    // ---- 复现包与多记录对比 ----
    const sel = page('records.js');    sel.elements.set('#select-hint', {
        value: '', textContent: '', innerHTML: '', className: '', disabled: false,
        style: {}, dataset: {}, children: [],
        classList: { add() {}, remove() {}, toggle() {} },
        setAttribute() {}, addEventListener() {}, getContext() { return {}; }, appendChild() {},
    });
    // 勾选上限: 第 5 条被拒绝, 且给出提示
    sel.run('selectedIds = []');
    for (const id of [1, 2, 3, 4]) assert.equal(sel.run(`toggleSelect(${id}, true)`), true);
    assert.equal(sel.run('toggleSelect(5, true)'), false, '超过 4 条应被拒绝');
    assert.equal(sel.run('selectedIds.length'), 4);
    assert.ok(sel.notices.slice(-1)[0][0].includes('最多同时对比 4 条'));
    // 取消后再勾选应成功, 顺序即并列顺序
    sel.run('toggleSelect(2, false)');
    assert.equal(sel.run('toggleSelect(5, true)'), true);
    assert.deepEqual(Array.from(sel.run('selectedIds')), [1, 3, 4, 5]);
    assert.equal(sel.run('isSelected(3)'), true);
    assert.equal(sel.run('isSelected(2)'), false);
    // 勾选条: 少于 2 条时对比按钮不可用
    sel.run('selectedIds = [7]; renderSelectBar()');
    assert.equal(sel.element('#btn-compare').disabled, true);
    sel.run('selectedIds = [7, 8]; renderSelectBar()');
    assert.equal(sel.element('#btn-compare').disabled, false);
    assert.match(sel.element('#btn-compare').textContent, /对比所选 \(2\)/);
    assert.match(sel.element('#select-hint').textContent, /已选 2 条/);

    // 复现包: 按钮必须指向正确的 GET 接口
    const created = [];
    sel.context.document.createElement = (tag) => ({
        tag, href: '', rel: '', download: '', click() { created.push(this); },
        remove() {},
    });
    sel.context.document.body = { appendChild() {} };
    sel.run('downloadRepro(4301)');
    assert.equal(created.length, 1);
    assert.equal(created[0].tag, 'a');
    assert.equal(created[0].href, '/api/report/repro/4301');
    assert.ok(sel.notices.slice(-1)[0][0].includes('4301'));

    // 对比请求: 失败时给出提示而不是抛出未捕获异常
    sel.run('selectedIds = [4300, 4301]');
    const comparePayloads = [];
    sel.context.API.postJSON = async (url, body) => {
        comparePayloads.push([url, body]);
        return {
            columns: [
                { record_id: 4300, source_name: 'a.jpg', task_type: 'image', created_at: '2026-10-20',
                  result_url: '/data/results/image_4300.jpg', result_video_url: null,
                  caliber: { source: 'image_objects', value: 13 }, total_detections: 13,
                  tracked_objects: 0, maxn_total: 0, cpunx: null, class_count: 2,
                  conf_threshold: 0.25, params: { iou_threshold: 0.45, imgsz: 640 },
                  conf: { count: 13, mean: 0.71, median: 0.7, p25: 0.6, p75: 0.8,
                          min: 0.3, max: 0.95,
                          sensitivity: [{ threshold: 0.5, kept: 12, ratio: 92.3 }] } },
                { record_id: 4301, source_name: 'b.mp4', task_type: 'video', created_at: '2026-10-20',
                  result_url: null, result_video_url: '/data/results/v.mp4',
                  caliber: { source: 'maxn', value: 8 }, total_detections: 240,
                  tracked_objects: 9, maxn_total: 8, cpunx: 1200.0, class_count: 2,
                  conf_threshold: 0.25, params: { iou_threshold: 0.45, imgsz: 640, frame_skip: 2 },
                  conf: { count: 240, mean: 0.6, median: 0.58, p25: 0.45, p75: 0.72,
                          min: 0.25, max: 0.99,
                          sensitivity: [{ threshold: 0.5, kept: 180, ratio: 75.0 }] } },
            ],
            class_matrix: [
                { class_name: '海胆', counts: { 4300: 11, 4301: 0 } },
                { class_name: '海星', counts: { 4300: 2, 4301: 5 } },
            ],
            conf_histograms: [
                { record_id: 4300, bins: [{ lower: 0.01, upper: 0.1, count: 1 }] },
                { record_id: 4301, bins: [{ lower: 0.01, upper: 0.1, count: 3 }] },
            ],
            comparable: false,
            caliber_sources: ['image_objects', 'maxn'],
            warnings: ['所选记录的计数口径不完全一致, 上表数值不能直接相减或排序比较。'],
            missing_ids: [],
            scope: { records: 2, selection: '按用户勾选的记录逐条取数', limitation: '差异可能来自参数' },
        };
    };
    const shown = [];
    sel.context.setTimeout = (fn) => fn();
    await sel.run('openCompare()');
    assert.equal(comparePayloads.length, 1);
    assert.equal(comparePayloads[0][0], '/api/compare');
    assert.deepEqual(Array.from(comparePayloads[0][1].record_ids), [4300, 4301]);
    assert.equal(comparePayloads[0][1].bins, 10);

    const bodyHtml = sel.element('#compare-body').innerHTML;
    // 口径不一致时必须明说"不可直接比较", 而不是只并排列出数字
    assert.match(bodyHtml, /口径不一致/);
    assert.match(bodyHtml, /不可直接比较/);
    assert.match(bodyHtml, /所选记录的计数口径不完全一致/);
    // 类别并集: 缺失类别补 0, 不是省略该行
    assert.match(bodyHtml, /海胆/);
    assert.match(bodyHtml, /海星/);
    assert.match(bodyHtml, /zero-cell/);
    // 视频要标明明细是采样, 避免把采样数当全量
    assert.match(bodyHtml, /采样/);
    // 结果缩略图必须用后端给的 result_url(不是裸文件名 result_image), 否则图片记录会显示"无结果文件"
    assert.match(bodyHtml, /compare-thumb/);
    assert.match(bodyHtml, /src="\/data\/results\/image_4300\.jpg"/);
    assert.match(bodyHtml, /▶ 结果视频/);
    // 阈值敏感性的行必须来自后端给的阈值(0.5), 前端不得自行增删 -> 不应出现 0.1 这一行
    const sensHtml = sel.run(`renderSensitivity({
        columns: [{ record_id: 1, conf: { sensitivity: [{ threshold: 0.5, kept: 3, ratio: 60 }] } }],
    })`);
    assert.match(sensHtml, /阈值 ≥ 0\.5/);
    assert.ok(!/阈值 ≥ 0\.1<|阈值 ≥ 0\.1</.test(sensHtml), `不应出现后端未提供的阈值: ${sensHtml}`);
    assert.match(sel.run(`renderSensitivity({ columns: [{ record_id: 1, conf: { sensitivity: [] } }] })`),
        /无法给出阈值敏感性/);

    // 图表只创建一次且按列配色, 不出现重复 canvas id
    assert.equal((bodyHtml.match(/compare-chart-conf/g) || []).length, 1, '图表容器只能出现一次');
    assert.equal(sel.charts.length, 1);
    assert.equal(sel.charts[0].data.datasets.length, 2);

    // 失败路径: 必须提示, 不静默
    sel.context.API.postJSON = async () => { throw new Error('记录不存在'); };
    await sel.run('openCompare()');
    assert.ok(sel.notices.slice(-1)[0][0].includes('对比失败'));
    assert.ok(sel.notices.slice(-1)[0][0].includes('记录不存在'));

    // ---- 批量导出(记录页): 只收图片记录、上限与后端一致、失败要可读 ----
    const bulk = page('records.js');
    bulk.elements.set('#select-hint', {
        value: '', textContent: '', innerHTML: '', className: '', disabled: false,
        style: {}, dataset: {}, children: [],
        classList: { add() {}, remove() {}, toggle() {} },
        setAttribute() {}, addEventListener() {}, getContext() { return {}; }, appendChild() {},
    });
    // 还原成后端返回的记录形态, 让"是否图片"的判断有依据
    bulk.run(`currentItems = new Map([
        [1, { id: 1, task_type: 'image' }],
        [2, { id: 2, task_type: 'video' }],
    ]);`);
    assert.deepEqual(Array.from(bulk.run('selectedIds = [1, 2]; exportableSelection()')), [1]);

    // 只选视频: 两个导出按钮都不可点, 且提示说明原因
    bulk.run('selectedIds = [2]; renderSelectBar()');
    assert.equal(bulk.element('#btn-bulk-report').disabled, true);
    assert.equal(bulk.element('#btn-bulk-export').disabled, true);
    assert.match(bulk.element('#select-hint').textContent, /视频不参与报告/);

    // 只选图片: 按"实际参与导出的张数"显示计数, 而不是勾选总数
    bulk.run('selectedIds = [1]; renderSelectBar()');
    assert.equal(bulk.element('#btn-bulk-report').disabled, false);
    assert.match(bulk.element('#btn-bulk-report').textContent, /批量报告 \(1\)/);
    assert.match(bulk.element('#btn-bulk-export').textContent, /导出标签 \(1\)/);

    // 超过 50 张: 不放行, 而不是点了才报 400
    bulk.run(`currentItems = new Map(Array.from({length: 51}, (_, i) => [i + 1, { id: i + 1, task_type: 'image' }]));
              selectedIds = Array.from({length: 51}, (_, i) => i + 1);
              renderSelectBar();`);
    assert.equal(bulk.element('#btn-bulk-report').disabled, true);
    assert.equal(bulk.element('#btn-bulk-export').disabled, true);
    // 提示必须给出上限与实际勾选数, 让用户知道差多少。
    // 注意: 这里不用 /最多 \d+/ 这类"中文字 + 空格 + 数字"的正则,
    // 因为本机 Node 版本在该组合上匹配不稳定; 改为拆开校验纯 ASCII 数字片段。
    for (const sel of ['#btn-bulk-report', '#btn-bulk-export']) {
        const tip = bulk.element(sel).title;
        assert.ok(tip.includes('50'), `${sel} 的提示应含上限 50, 实际: ${tip}`);
        assert.ok(tip.includes('51'), `${sel} 的提示应含实际勾选数 51, 实际: ${tip}`);
    }

    // 请求体必须带明确的 ID 列表与格式; 文件名按 RFC 5987 解码, 不能带乱码
    bulk.run(`currentItems = new Map([[7, { id: 7, task_type: 'image' }], [8, { id: 8, task_type: 'image' }]]);
              selectedIds = [7, 8];`);
    bulk.elements.set('#bulk-export-format', {
        value: 'coco', textContent: '', innerHTML: '', disabled: false, style: {},
        dataset: {}, children: [], classList: { add() {}, remove() {}, toggle() {} },
        setAttribute() {}, addEventListener() {}, getContext() { return {}; }, appendChild() {},
    });
    const bulkCalls = [];
    const savedBlobs = [];
    bulk.context.fetch = async (url, options) => {
        bulkCalls.push([url, JSON.parse(options.body)]);
        return {
            ok: true,
            headers: { get: () => "attachment; filename=\"labels.json\"; filename*=UTF-8''%E6%A0%87%E7%AD%BE.json" },
            blob: async () => ({ size: 10, type: 'application/json' }),
        };
    };
    // 沙箱里没有浏览器 URL 对象, 这里补一个最小桩给 saveBlob 用;
    // saveBlob 会延后 revoke, 因此 setTimeout 也要有
    bulk.context.URL = {
        createObjectURL: () => 'blob:x',
        revokeObjectURL: () => {},
    };
    bulk.context.setTimeout = (fn) => 0;
    bulk.context.clearTimeout = () => {};
    bulk.context.document = {
        addEventListener() {},
        body: { appendChild() {} },
        createElement: (tag) => ({ tag, href: '', download: '', click() { savedBlobs.push(this.download); }, remove() {} }),
    };
    await bulk.run("bulkExport('labels')");
    assert.equal(bulkCalls.length, 1);
    assert.equal(bulkCalls[0][0], '/api/report/export');
    assert.deepEqual(Array.from(bulkCalls[0][1].record_ids), [7, 8]);
    assert.equal(bulkCalls[0][1].format, 'coco');
    assert.deepEqual(savedBlobs, ['标签.json'], '文件名必须按 RFC 5987 解码');
    assert.match(bulk.notices.slice(-1)[0][0], /已导出 2 张影像/);

    // 报告走另一个端点, 请求体只有 record_ids
    bulk.context.fetch = async (url, options) => {
        bulkCalls.push([url, JSON.parse(options.body)]);
        return { ok: true, headers: { get: () => 'attachment; filename="r.pdf"' },
                 blob: async () => ({ size: 3, type: 'application/pdf' }) };
    };
    bulkCalls.length = 0;
    await bulk.run("bulkExport('report')");
    assert.equal(bulkCalls[0][0], '/api/report/images');
    assert.deepEqual(Object.keys(bulkCalls[0][1]), ['record_ids']);

    // 失败路径: 把后端 message 原样带给用户, 而不是只显示 HTTP 状态
    bulk.context.fetch = async () => ({
        ok: false, status: 400,
        json: async () => ({ success: false, message: '图片报告和训练标签仅支持图片记录' }),
    });
    await bulk.run("bulkExport('labels')");
    assert.match(bulk.notices.slice(-1)[0][0], /仅支持图片记录/);

    // ============================================================ B 组纯前端改动
    //
    // 1) 对比结果导出 CSV: 必须与页面同源(同一份 data), 且口径结论要写进文件
    // 2) 详情弹窗内嵌预检: 预检失败不能影响详情其它内容
    // 3) 快捷键: 输入框内不抢 Ctrl+A / 弹窗打开时不响应
    // 4) 空态: 区分"库里就没有"与"被筛掉"

    const bx = page('records.js');
    const saved2 = [];
    bx.context.URL = { createObjectURL: () => 'blob:y', revokeObjectURL() {} };
    bx.context.setTimeout = () => 0;
    // 记录最后构造的 Blob, 用来核对 CSV 是否带 BOM
    bx.context.Blob = class {
        constructor(parts, opts) { this.parts = parts; this.type = (opts || {}).type; bx.context.__lastBlob = this; }
    };
    bx.context.document = {
        addEventListener() {},
        body: { appendChild() {} },
        createElement: () => ({ tag: '', href: '', download: '', click() { saved2.push(this.download); }, remove() {} }),
    };

    const sampleCompare = {
        comparable: false,
        columns: [
            { record_id: 4300, source_name: 'reef_01.jpg', task_type: 'image',
              created_at: '2026-10-20 12:00:00', total_detections: 13, tracked_objects: 0,
              maxn_total: 0, cpunx: null, class_count: 3, site_label: 'A站',
              transect_label: 'Q1', survey_round: '第1轮', depth_m: 5,
              caliber: { source: 'image_objects', value: 13 },
              conf: { count: 13, mean: 0.62, median: 0.61, p25: 0.4, p75: 0.8, min: 0.3, max: 0.95 } },
            { record_id: 4301, source_name: 'clip.mp4', task_type: 'video',
              created_at: '2026-10-20 13:00:00', total_detections: 2332, tracked_objects: 88,
              maxn_total: 9, cpunx: 1.5, class_count: 2, site_label: 'B站',
              transect_label: '', survey_round: '', depth_m: null,
              caliber: { source: 'total_detections', value: 2332 },
              conf: { count: 200, mean: 0.55, median: 0.54, p25: 0.35, p75: 0.75, min: 0.25, max: 0.99 } },
        ],
        scope: { selection: '按当前筛选勾选' },
    };
    bx.context.__sample = sampleCompare;

    // CSV: 表头是指标为行、记录为列; 含逗号的值要加引号转义
    const csv = bx.run('compareToCsv(__sample)');
    const lines = csv.split('\r\n');
    assert.equal(lines[0], '对比项,#4300,#4301', 'CSV 表头应为 指标 + 各记录ID');
    assert.ok(lines[1].startsWith('文件名称,'), `首行指标应为文件名称: ${lines[1]}`);
    assert.ok(lines[2].includes('视频'), '检测类型应把 task_type 翻成中文');
    // CSV 里单元格含逗号必须加引号, 否则 Excel 会错列
    assert.equal(bx.run("csvCell('a,b')"), '"a,b"');
    assert.equal(bx.run("csvCell('he said \"hi\"')"), '"he said ""hi"""', '引号要双写转义');
    assert.equal(bx.run("csvCell('plain')"), 'plain', '普通值不应被多余加引号');
    // 每个指标只出现一次: 防止将来加行时重复 push
    const metricLabels = lines.slice(1).filter((l) => l && !l.includes('口径不一致'))
        .map((l) => l.split(',')[0]);
    assert.equal(new Set(metricLabels).size, metricLabels.length, 'CSV 指标行不能重复');

    // 导出动作: 纯前端拼 Blob, 不经过后端; 文件名含条数与日期
    bx.run("lastCompareData = __sample");
    bx.run('exportCompareCsv()');
    assert.equal(saved2.length, 1);
    assert.match(saved2[0], /^对比_2条_\d{8}\.csv$/, `文件名应含条数与日期: ${saved2[0]}`);
    // Excel 按系统编码猜, 没有 BOM 中文会乱码
    const blob = bx.context.__lastBlob;
    assert.equal(blob.type, 'text/csv;charset=utf-8');
    assert.ok(blob.parts[0].charCodeAt(0) === 0xFEFF, 'CSV 必须带 UTF-8 BOM, 否则 Excel 打开中文乱码');

    // 没有对比数据时不能凭空导出, 且要说明原因
    bx.run('lastCompareData = null');
    const before2 = saved2.length;
    bx.run('exportCompareCsv()');
    assert.equal(saved2.length, before2, '无数据时不应产生下载');
    assert.match(bx.notices.slice(-1)[0][0], /还没有可导出的对比数据/);

    // ---- 预检区块 ----
    // 把 $ 换成"按选择器返回固定盒子", 这样能直接观察渲染进盒子的内容
    bx.context.__box = { innerHTML: '', hidden: false, isConnected: true, parentNode: {},
                         classList: { add() {}, remove() {}, toggle() {} } };
    bx.context.$ = (sel) => (sel === '#detail-preflight' ? bx.context.__box : null);
    bx.context.escapeHtml = (v) => String(v ?? '');
    const noticeCountBefore = bx.notices.length;

    // 预检失败: 必须静默降级, 不能弹错误打扰用户
    bx.context.API.get = async () => { throw new Error('backend down'); };
    await bx.run('renderDetailPreflight(4300, {})');
    assert.equal(bx.notices.length, noticeCountBefore, '预检失败不应弹 toast');

    // 预检正常: 两个导出口分别给结论, 告警逐条列出, 体积按 MB 展示
    bx.context.API.get = async () => ({
        bundle_exportable: true, label_exportable: false, label_formats: [],
        asset_bytes: 4881765, bundle_size_limit: 536870912,
        item_rows: 200, total_detections: 2332,
        counting_source_label: '累计检出框',
        notes: ['明细仅入库 200 条, 累计检出 2332 框', '视频记录不做标签导出'],
    });
    await bx.run('renderDetailPreflight(4301, {})');
    const pfHtml = bx.context.__box.innerHTML;
    assert.ok(pfHtml.includes('可导出'), '复现包可导时必须显示"可导出"');
    assert.ok(pfHtml.includes('不可导出'), '视频标签不可导时必须显示"不可导出"');
    assert.ok(pfHtml.includes('4.7 MB'), `素材体积应按 MB 展示: ${pfHtml}`);
    assert.ok(pfHtml.includes('视频记录不做标签导出'), '告警条目必须逐条渲染');
    assert.ok(pfHtml.includes('累计检出框'), '计数口径应出现在预检里');

    // 全选的本页上限保护: 快捷键与表头复选框共用同一份逻辑, 不能静默超限
    const tooMany = new Map(Array.from({ length: 6 }, (_, i) => [i + 1, { id: i + 1, task_type: 'image' }]));
    bx.context.currentItems = tooMany;
    bx.context.selectedIds = [];
    bx.context.$$ = () => Array.from({ length: 6 }, (_, i) => ({
        checked: false, dataset: { select: String(i + 1) }, closest: () => null,
    }));
    const allowed = bx.run('applySelectAll(true)');
    assert.equal(allowed, false, '超过对比上限时全选必须被拦下');
    assert.match(bx.notices.slice(-1)[0][0], /超过单次对比上限/, '拦下时要说明原因');

    // 快捷键走另一条路径: 每页 10 条而上限 4 条时, 不能像表头复选框那样干脆拒绝,
    // 否则 Ctrl+A 永远无效。应按剩余额度选中前 N 条, 并同步表头半选态。
    // 上面为了观察预检渲染把 $ 换成了定制版, 这里恢复成元素工厂, 否则 #select-all 会拿到 null
    bx.context.$ = bx.element;
    const head = bx.element('#select-all');
    head.checked = false;
    head.indeterminate = false;
    // 沙箱里的 $ 是闭包取 element(), 先确认它拿到的就是同一个对象, 否则后面的断言毫无意义
    bx.context.__head = head;
    assert.equal(bx.run("$('#select-all') === __head"), true, '沙箱内 $ 必须解析到同一个节点');
    // selectedIds 是脚本顶层的 let 绑定, 不会挂到 context 对象上(真实浏览器里 let 也不上 window),
    // 所以清空与读取都必须在沙箱内求值, 从外部给 context.selectedIds 赋值是无效的。
    // $$ 也须临时换回 10 条: 真实列表每页 10 条, 而对比上限只有 4 条, 这正是"必须截断"的场景。
    const pageBoxes = Array.from({ length: 10 }, (_, i) => ({
        checked: false, dataset: { select: String(100 + i) }, closest: () => null,
    }));
    // renderSelectBar 会按 DOM 复选框重算表头, 所以半选态/打勾态最终由 checkbox 的 checked 裁决,
    // 这里对齐成"前 n 条选中"的样貌
    const tickFirstN = (n) => pageBoxes.forEach((cb, i) => { cb.checked = i < n; });
    bx.context.$$ = () => pageBoxes;
    bx.run('selectedIds.length = 0');
    tickFirstN(0);
    const okUpTo = bx.run('selectPageUpToLimit()');
    assert.equal(okUpTo, true, '快捷键应尽力选中而不是直接失败');
    assert.equal(bx.run('selectedIds.length'), 4, '选中数应正好等于对比上限');
    tickFirstN(4);
    assert.equal(head.indeterminate, true, '只选了部分记录时表头应显示半选态');
    assert.equal(head.checked, false, '只选了部分记录时表头不应打勾');
    // 已满员时再按一次, 不应继续增加, 且要提示先清空
    const noticeBefore = bx.notices.length;
    bx.run('selectPageUpToLimit()');
    assert.equal(bx.run('selectedIds.length'), 4, '满员后不得超出上限');
    assert.equal(bx.notices.length, noticeBefore + 1, '满员时应给出提示');
    assert.match(bx.notices.slice(-1)[0][0], /上限/, '满员提示应指出上限');
    // 本页恰好不超限时, 表头应打勾而不是半选
    bx.run('selectedIds.length = 0');
    const shortPage = pageBoxes.slice(0, 3);
    bx.context.$$ = () => shortPage;
    head.checked = false;
    head.indeterminate = false;
    const okShort = bx.run('selectPageUpToLimit()');
    assert.equal(okShort, true, '本页不超限时应全选');
    shortPage.forEach((cb) => { cb.checked = true; });
    const noticeShort = bx.notices.slice(-1)[0][0];
    assert.doesNotMatch(noticeShort, /已选中前/, '全选到本页末尾时不应再提示"只选了前 N 条"');
    bx.context.$$ = () => Array.from({ length: 6 }, (_, i) => ({
        checked: false, dataset: { select: String(i + 1) }, closest: () => null,
    }));

    // 空态: 有筛选 vs 无筛选必须给出不同指引
    const emptyFiltered = bx.run("emptyStateHtml({ filtered: true })");
    const emptyFresh = bx.run("emptyStateHtml({ filtered: false })");
    assert.ok(emptyFiltered.includes('重置'), '被筛掉时应引导重置筛选');
    assert.ok(emptyFresh.includes('图片检测'), '库里为空时应引导去检测页');
    assert.notEqual(emptyFiltered, emptyFresh, '两种空态不能是同一句话');

    console.log('Frontend contracts passed: upload isolation, counts, provenance, pagination, '
        + 'tracking fallback, ETA, missing dates, station drill isolation, selection cap, repro download, compare view, '
        + 'bulk export, compare csv, detail preflight, empty state.');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
