/* 系统状态与运维面板
   数据源: /api/system/status (聚合快照) + /api/system/trash + /api/system/audit-log
   设计: 单一 refresh() 拉取全量并渲染, 危险操作二次确认, 自动刷新可开关 */

const API_BASE = '/api/system';
let autoTimer = null;
let autoEnabled = true;
const AUTO_INTERVAL = 10000;

/* ---------------- 工具 ---------------- */

function bytes(n) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 ** 3) return (n / 1024 / 1024).toFixed(2) + ' MB';
    return (n / 1024 ** 3).toFixed(2) + ' GB';
}

function gb(n) {
    return (n === null || n === undefined || isNaN(n)) ? '-' : n.toFixed(2) + ' GB';
}

function pct(n) {
    return (n === null || n === undefined || isNaN(n)) ? '-' : n.toFixed(1) + '%';
}

function duration(sec) {
    if (sec === null || sec === undefined) return '-';
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (d) return `${d} 天 ${h} 小时`;
    if (h) return `${h} 小时 ${m} 分`;
    if (m) return `${m} 分 ${s} 秒`;
    return `${s} 秒`;
}

/** 按占用率给 meter 上色: <70 正常, 70~90 警告, >90 危险 */
function meterClass(percent) {
    if (percent >= 90) return 'meter danger';
    if (percent >= 70) return 'meter warn';
    return 'meter';
}

function setMeter(id, percent) {
    const el = $('#' + id);
    if (!el) return;
    el.className = meterClass(percent);
    const bar = el.querySelector('span');
    if (bar) bar.style.width = Math.max(0, Math.min(100, percent || 0)) + '%';
}

/* ---------------- 渲染: 顶部卡片 ---------------- */

function renderTop(d) {
    const modelOk = d.model && d.model.loaded;
    $('#stat-service').innerHTML = modelOk
        ? '<span class="status-pill ok"><i></i>正常</span>'
        : '<span class="status-pill warn"><i></i>模型未加载</span>';
    $('#stat-uptime').textContent = `运行时长 ${duration(d.process.uptime_sec)} · PID ${d.process.pid}`;

    $('#stat-storage').textContent = bytes(d.storage.managed_bytes);
    $('#stat-storage-detail').textContent =
        `上传 ${bytes(d.storage.uploads.bytes)} / 结果 ${bytes(d.storage.results.bytes)}`;

    $('#stat-reclaim').textContent = bytes(d.storage.reclaimable_bytes);
    $('#stat-reclaim').textContent = bytes(d.storage.reclaimable_bytes);
    $('#stat-reclaim-detail').textContent =
        `孤儿 ${bytes(d.storage.orphan_bytes)} / 回收站 ${bytes(d.storage.trash_bytes)}`;

    $('#stat-tasks').textContent = `${d.video_tasks.count} / ${d.video_tasks.limit}`;
    $('#stat-tasks-detail').innerHTML = d.tracking_busy
        ? '<span class="status-pill warn"><i></i>跟踪中</span>'
        : '<span class="status-pill ok"><i></i>槽位空闲</span>';
}

/* ---------------- 渲染: 水位 ---------------- */

function renderStorage(d) {
    const sys = d.system, st = d.storage;

    if (sys.disk_percent !== null) {
        setMeter('disk-meter', sys.disk_percent);
        $('#disk-used').textContent = gb(sys.disk_used_gb);
        $('#disk-free').textContent = gb(sys.disk_free_gb);
        $('#disk-free2').textContent = gb(sys.disk_free_gb);
        $('#disk-total').textContent = `总容量 ${gb(sys.disk_total_gb)} · 已用 ${pct(sys.disk_percent)}`;
    } else {
        $('#disk-total').textContent = '磁盘信息不可用';
    }

    // 受管目录构成: 上传 / 结果 / 回收站 的相对占比
    const total = st.total_bytes || 1;
    const up = (st.uploads.bytes / total) * 100;
    const res = (st.results.bytes / total) * 100;
    const tr = (st.trash.bytes / total) * 100;

    const el = $('#storage-meter');
    if (el) {
        el.className = 'meter';
        el.innerHTML =
            `<span style="width:${up}%;background:var(--primary);border-radius:0"></span>` +
            `<span style="width:${res}%;background:var(--accent);border-radius:0;margin-left:-1px"></span>` +
            `<span style="width:${tr}%;background:var(--warning);border-radius:0;margin-left:-1px"></span>`;
        el.style.display = 'flex';
    }
    const bar = $('#storage-meter span');
    if (bar) bar.style.display = 'block';

    $('#st-uploads').textContent = bytes(st.uploads.bytes);
    $('#st-results').textContent = bytes(st.results.bytes);
    $('#st-trash').textContent = bytes(st.trash.bytes);

    const notes = [];
    notes.push(`受管文件合计 <b>${bytes(st.total_bytes)}</b>`);
    if (st.orphan_files > 0) {
        notes.push(`<span style="color:var(--warning)">孤儿文件 ${st.orphan_files} 个 (${bytes(st.orphan_bytes)}) 待回收</span>`);
    } else {
        notes.push('无孤儿文件 (引用关系完好)');
    }
    $('#storage-note').innerHTML = notes.join(' · ');
}

function renderMetrics(d) {
    const p = d.process, s = d.system, db = d.database;
    $('#sys-mem').textContent = p.memory_mb !== null ? `${p.memory_mb} MB (${pct(p.memory_percent)})` : '不可用';
    $('#sys-cpu').textContent = p.cpu_percent !== null ? pct(p.cpu_percent) : '不可用';
    $('#sys-host-mem').textContent = s.memory_percent !== null
        ? `${pct(s.memory_percent)} (${s.memory_used_mb} / ${s.memory_total_mb} MB)` : '不可用';
    $('#sys-cores').textContent = s.cpu_count || '-';
    $('#db-size').textContent = db.wal_bytes
        ? `${bytes(db.db_bytes)} + WAL ${bytes(db.wal_bytes)}` : bytes(db.db_bytes);
    $('#sys-model').textContent = d.model.name
        ? `${d.model.name} · ${d.model.device} · ${d.model.class_count} 类` : '未加载';
    $('#sys-tracker').textContent = `${d.model.tracker}${d.model.track_enabled ? '' : ' (已关闭)'}`;

    const ds = d.data_scale || {};
    $('#sys-records').textContent =
        `${ds.records || 0} 条 (图片 ${ds.image_records || 0} / 视频 ${ds.video_records || 0})`;
    $('#sys-objects').textContent = `${ds.objects || 0} 个目标 · 明细 ${ds.items || 0} 行`;
    $('#sys-time').textContent = d.server_time;
}

/* ---------------- 渲染: 任务中心 ---------------- */

function renderTasks(d) {
    const body = $('#task-body');
    const tasks = (d.video_tasks && d.video_tasks.tasks) || [];
    if (!tasks.length) {
        body.innerHTML = '<tr><td colspan="6" class="empty">当前没有驻留任务</td></tr>';
        return;
    }
    body.innerHTML = tasks.map((t) => {
        const progress = t.total_frames
            ? Math.round(((t.frame_index ?? t.processed) / t.total_frames) * 100) + '%' : '-';
        return `<tr>
            <td class="mono small">${escapeHtml(t.task_id)}</td>
            <td>${escapeHtml(t.source_name || '-')}</td>
            <td>${t.paused
                ? '<span class="status-pill warn"><i></i>已暂停</span>'
                : t.running
                    ? '<span class="status-pill ok"><i></i>检测中</span>'
                : '<span class="status-pill warn"><i></i>等待中</span>'}</td>
            <td>${progress}</td>
            <td>${duration(t.idle_sec)}</td>
            <td><button class="btn btn-sm btn-danger" data-stop-task="${escapeHtml(t.task_id)}">停止</button></td>
        </tr>`;
    }).join('');
}

/* ---------------- 渲染: 回收站 ---------------- */

async function renderTrash() {
    let data;
    try {
        data = await API.get(API_BASE + '/trash?limit=30');
    } catch (err) {
        return;
    }
    $('#trash-summary').textContent =
        `${data.files} 个文件 · ${bytes(data.bytes)} · 最久滞留 ${data.oldest_days == null ? '未知' : data.oldest_days + ' 天'}`;

    const body = $('#trash-body');
    const files = data.files_detail || [];
    if (!files.length) {
        body.innerHTML = '<tr><td colspan="5" class="empty">回收站为空</td></tr>';
        return;
    }
    body.innerHTML = files.map((f) => `
        <tr>
            <td class="small">${escapeHtml(f.original_name)}</td>
            <td>${bytes(f.bytes)}</td>
            <td class="small muted">${escapeHtml(f.deleted_at)}</td>
            <td>${f.age_days == null ? '未知' : escapeHtml(f.age_days) + ' 天'}</td>
            <td><button class="btn btn-sm btn-secondary" data-restore="${escapeHtml(f.trash_name)}">恢复</button></td>
        </tr>
    `).join('');

    $$('[data-restore]', body).forEach((btn) => {
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            try {
                const r = await API.postJSON(API_BASE + '/trash/restore?name=' +
                    encodeURIComponent(btn.dataset.restore), {});
                toast(r && r.restored ? `已恢复 ${r.restored}` : '已恢复', 'success');
                await refresh();
            } catch (err) {
                toast('恢复失败: ' + err.message, 'error');
                btn.disabled = false;
            }
        });
    });
}

/* ---------------- 渲染: 审计日志 ---------------- */

const ACTION_LABEL = {
    record_delete: '记录删除',
    orphan_cleanup: '孤儿回收',
    trash_purge: '回收站清除',
    trash_restore: '回收站恢复',
};

function renderAudit(items) {
    const box = $('#audit-list');
    if (!items || !items.length) {
        box.innerHTML = '<div class="empty">暂无记录</div>';
        return;
    }
    box.innerHTML = items.map((it) => {
        const names = (it.files || []).map((f) => f.name).join(', ');
        return `<div class="audit-item">
            <span class="act">${escapeHtml(ACTION_LABEL[it.action] || it.action)}</span>
            <span>${it.count} 个文件</span>
            <span class="muted small" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:280px;"
                  title="${escapeHtml(names)}">${escapeHtml(names)}</span>
            <span class="time">${escapeHtml(it.ts)}</span>
        </div>`;
    }).join('');
}

/* ---------------- 渲染: 策略 ---------------- */

function renderPolicy(cfg, d) {
    const left = [
        ['记录保留期', `${cfg.retention_days} 天`],
        ['启动自动清理', cfg.auto_cleanup ? '已开启' : '已关闭'],
        ['上传内容去重', cfg.dedupe_upload ? '已开启' : '已关闭'],
        ['回收站滞留窗口', `${cfg.trash_purge_days} 天`],
    ];
    const right = [
        ['视频任务并发上限', `${cfg.max_active_video_tasks} 个`],
        ['任务闲置回收 TTL', duration(cfg.video_task_ttl_sec)],
        ['单文件上传上限', `${cfg.max_upload_mb} MB`],
        ['批量图片上限', `${cfg.max_batch_images} 张`],
    ];
    const rows = (arr) => arr.map(([k, v]) =>
        `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(String(v))}</td></tr>`).join('');
    $('#policy-body-left').innerHTML = rows(left);
    $('#policy-body-right').innerHTML = rows(right);

    $('#retention-days').textContent = cfg.retention_days;
    $('#trash-days').textContent = cfg.trash_purge_days;
}

/* ---------------- 刷新 ---------------- */

let refreshing = false;

async function refresh() {
    if (refreshing) return;
    refreshing = true;
    $('#refresh-dot').classList.add('spin');
    try {
        const [status, cfg, audit] = await Promise.all([
            API.get(API_BASE + '/status'),
            API.get(API_BASE + '/config-summary'),
            API.get(API_BASE + '/audit-log?limit=15'),
        ]);
        renderTop(status);
        renderStorage(status);
        renderMetrics(status);
        renderTasks(status);
        renderPolicy(cfg, status);
        renderAudit(audit.items);
        await renderTrash();
        $('#alert-box').innerHTML = '';
    } catch (err) {
        $('#alert-box').innerHTML =
            `<div class="alert alert-error">状态获取失败: ${escapeHtml(err.message)}</div>`;
    } finally {
        refreshing = false;
        $('#refresh-dot').classList.remove('spin');
    }
}

/* ---------------- 运维操作 ---------------- */

const OPERATIONS = {
    'orphan-dry': { url: '/orphans/cleanup?dry_run=true', method: 'POST', label: '孤儿预检' },
    'orphan-run': {
        url: '/orphans/cleanup?dry_run=false', method: 'POST', label: '孤儿回收',
        confirm: '确认回收所有无引用的孤儿文件? 文件会先进入回收站, 可恢复。',
    },
    'dup-dry': { url: '/duplicates/cleanup?dry_run=true', method: 'POST', label: '重复预检' },
    'dup-run': {
        url: '/duplicates/cleanup?dry_run=false', method: 'POST', label: '重复清理',
        confirm: '确认清理重复副本? 未被任何记录引用且内容重复的文件将被删除。',
    },
    'tmp-run': { url: '/tmp/cleanup', method: 'POST', label: '临时残留清理' },
    'expire-dry': { url: '/cleanup?dry_run=true', method: 'POST', label: '过期预检' },
    'expire-run': {
        url: '/cleanup?dry_run=false', method: 'POST', label: '过期记录清理',
        confirm: '确认清理过期记录? 超过保留期的记录及其文件将被删除, 文件进回收站。',
    },
    'checkpoint': { url: '/checkpoint', method: 'POST', label: 'WAL 合并' },
    'trash-purge': {
        url: '/trash/purge?days=7', method: 'POST', label: '回收站清理',
        confirm: '确认清除回收站中滞留超过 7 天的文件? 此操作不可恢复。',
    },
    'trash-purge-all': {
        url: '/trash/purge?days=0&confirm=true', method: 'POST', label: '清空回收站',
        confirm: '⚠️ 将彻底清空回收站中全部文件, 无法恢复。确认继续?',
    },
};

async function runOp(key, btn) {
    const op = OPERATIONS[key];
    if (!op) return;
    if (op.confirm && !window.confirm(op.confirm)) return;

    const raw = btn.textContent;
    btn.disabled = true;
    btn.textContent = '执行中...';
    try {
        const res = await API.postJSON(API_BASE + op.url, {});
        toast(res && res.message ? res.message : `${op.label}完成`, 'success', 4000);
        await refresh();
    } catch (err) {
        toast(`${op.label}失败: ${err.message}`, 'error', 5000);
    } finally {
        btn.disabled = false;
        btn.textContent = raw;
    }
}

/* ---------------- 自动刷新 ---------------- */

function setAuto(on) {
    autoEnabled = on;
    if (autoTimer) {
        clearInterval(autoTimer);
        autoTimer = null;
    }
    if (on) {
        autoTimer = setInterval(() => {
            // 页面不可见时不刷新, 避免后台标签页无谓消耗
            if (!document.hidden) refresh();
        }, AUTO_INTERVAL);
        $('#refresh-hint').textContent = `自动刷新 ${AUTO_INTERVAL / 1000}s`;
    } else {
        $('#refresh-hint').textContent = '自动刷新已暂停';
    }
}

/* ---------------- 初始化 ---------------- */

document.addEventListener('DOMContentLoaded', () => {
    refresh();
    setAuto(true);

    $('#btn-refresh').addEventListener('click', () => {
        refresh();
        toast('已刷新', 'info', 1200);
    });

    // 点击提示文字可暂停 / 恢复自动刷新
    $('#refresh-hint').addEventListener('click', () => {
        setAuto(!autoEnabled);
        toast(autoEnabled ? '已恢复自动刷新' : '已暂停自动刷新', 'info', 1500);
    });
    $('#refresh-hint').style.cursor = 'pointer';

    $('#btn-reap').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
            const r = await API.postJSON(API_BASE + '/stale-tasks/reap', {});
            toast(r.reaped ? `已回收 ${r.reaped} 个僵尸任务` : '没有需要回收的任务', 'success');
            await refresh();
        } catch (err) {
            toast('回收失败: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
        }
    });

    $('#task-body').addEventListener('click', async (e) => {
        const btn = e.target.closest('[data-stop-task]');
        if (!btn) return;
        const taskId = btn.dataset.stopTask;
        btn.disabled = true;
        try {
            await API.postJSON(`/api/detect/video/${encodeURIComponent(taskId)}/control`, { action: 'stop' });
            toast('已发送停止指令，正在释放任务资源', 'success');
            window.setTimeout(refresh, 500);
        } catch (err) {
            toast('停止失败: ' + err.message, 'error');
            btn.disabled = false;
        }
    });

    $$('[data-op]').forEach((btn) => {
        btn.addEventListener('click', () => runOp(btn.dataset.op, btn));
    });

    // 页面隐藏时暂停定时器, 回来立即刷新一次
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && autoEnabled) refresh();
    });
});
