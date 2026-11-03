/* 接口文档页: 从 /openapi.json 实时读取接口定义并渲染
   设计: 不硬编码接口列表 —— 后端新增接口后本页自动同步, 不会与代码脱节 */

let SPEC = null;
const TAG_ORDER = ['养殖地形图', '系统状态', '图片检测', '视频检测', '历史记录', '多记录对比', '数据统计', '检测报告', '模型与配置', '系统', '存储治理'];

/* ---------------- 工具 ---------------- */

function methodClass(m) {
    return 'm-' + String(m || '').toLowerCase();
}

function tagOf(ep) {
    const t = (ep.tags && ep.tags[0]) || '未分组';
    return t;
}

function pathOf(ep) {
    // openapi 里的路径带 {param}, 这里保持原样展示
    return ep.path;
}

function summaryOf(ep) {
    return ep.summary || ep.description || '(无说明)';
}

function esc(s) {
    return escapeHtml(String(s === null || s === undefined ? '' : s));
}

/** 把路径中的 {param} 替换为示例值, 生成可直接运行的示例 */
function concretize(path) {
    return path.replace(/\{(\w+)\}/g, (m, name) => {
        if (/id$/i.test(name)) return '1';
        return name;
    });
}

/** 生成某个端点的 cURL 示例 */
function curlFor(ep) {
    const m = ep.method.toUpperCase();
    const url = `${location.origin}${concretize(ep.path)}`;
    if (ep.hasFile) {
        const field = ep.path === '/api/detect/images' ? 'files' : 'file';
        const filename = ep.path === '/api/detect/video' ? 'sample.mp4' : 'sample.jpg';
        return `curl -X ${m} "${url}" \\\n     -F "${field}=@${filename}" \\\n     -F "site_id=1"`;
    }
    if (ep.hasBody) {
        return `curl -X ${m} "${url}" \\\n     -H "Content-Type: application/json" \\\n     -d '{}'`;
    }
    return `curl -X ${m} "${url}"`;
}

/* ---------------- 解析 openapi ---------------- */

function parseSpec(spec) {
    const endpoints = [];
    const paths = spec.paths || {};
    for (const [path, methods] of Object.entries(paths)) {
        for (const [method, op] of Object.entries(methods)) {
            if (!['get', 'post', 'put', 'delete', 'patch'].includes(method)) continue;
            const params = (op.parameters || []).filter((p) => p.in !== 'header');
            endpoints.push({
                method,
                path,
                summary: op.summary || '',
                description: op.description || '',
                tag: (op.tags && op.tags[0]) || '未分组',
                params: params.map((p) => ({
                    name: p.name,
                    where: p.in,
                    required: !!p.required,
                    type: (p.schema && p.schema.type) || 'string',
                    default: p.schema && p.schema.default !== undefined ? p.schema.default : null,
                    desc: p.description || '',
                })),
                hasBody: !!op.requestBody,
                hasFile: JSON.stringify(op.requestBody || {}).includes('multipart/form-data'),
            });
        }
    }
    return endpoints;
}

/* ---------------- 渲染 ---------------- */

function renderSummary(endpoints, spec) {
    $('#api-total').textContent = endpoints.length;
    $('#api-base').textContent = location.origin;
    const tags = [...new Set(endpoints.map((e) => e.tag))];
    $('#api-tags').textContent = tags.length;

    const order = [...tags].sort((a, b) => {
        const ia = TAG_ORDER.indexOf(a), ib = TAG_ORDER.indexOf(b);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });

    $('#group-nav').innerHTML = order.map((t) => {
        const n = endpoints.filter((e) => e.tag === t).length;
        return `<a class="tag" href="#group-${encodeURIComponent(t)}" style="text-decoration:none;">
            ${esc(t)} <b>${n}</b></a>`;
    }).join('');
}

function renderEndpoints(endpoints) {
    const tags = [...new Set(endpoints.map((e) => e.tag))];
    const order = [...tags].sort((a, b) => {
        const ia = TAG_ORDER.indexOf(a), ib = TAG_ORDER.indexOf(b);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });

    const html = order.map((tag) => {
        const list = endpoints.filter((e) => e.tag === tag);
        const cards = list.map((ep, i) => {
            const id = `ep-${tag}-${i}`.replace(/[^\w-]/g, '_');
            const rows = ep.params.length
                ? ep.params.map((p) => `
                    <tr>
                        <td class="mono">${esc(p.name)}</td>
                        <td>${esc(p.where === 'path' ? '路径' : '查询')}</td>
                        <td>${esc(p.type)}</td>
                        <td>${p.required ? '<b>必填</b>' : '可选'}</td>
                        <td class="muted small">${esc(p.desc)}${p.default !== null ? ` (默认 ${esc(p.default)})` : ''}</td>
                    </tr>`).join('')
                : '<tr><td colspan="5" class="empty">无参数</td></tr>';

            const bodyHint = ep.hasBody
                ? `<p class="small muted" style="margin:8px 0 0;">请求体: ${ep.hasFile ? 'multipart/form-data (文件上传)' : 'application/json'}</p>`
                : '';

            return `
            <div class="endpoint" id="${id}">
                <div class="endpoint-head" data-toggle="${id}">
                    <span class="method ${methodClass(ep.method)}">${ep.method.toUpperCase()}</span>
                    <span class="mono ep-path">${esc(ep.path)}</span>
                    <span class="ep-summary">${esc(ep.summary)}</span>
                    <span class="ep-caret">▾</span>
                </div>
                <div class="endpoint-body">
                    ${ep.description ? `<p class="small">${esc(ep.description)}</p>` : ''}
                    <div class="table-wrap">
                        <table>
                            <thead><tr><th>参数</th><th>位置</th><th>类型</th><th>必填</th><th>说明</th></tr></thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>
                    ${bodyHint}
                    <div class="row" style="margin-top:10px;">
                        <button class="btn btn-sm btn-secondary" data-copy="#curl-${id}">复制 cURL</button>
                    </div>
                    <pre class="code-block" id="curl-${id}">${esc(curlFor(ep))}</pre>
                </div>
            </div>`;
        }).join('');

        return `
        <div class="card" id="group-${encodeURIComponent(tag)}">
            <h2 class="card-title">${esc(tag)} <span class="small muted">${list.length} 个接口</span></h2>
            ${cards}
        </div>`;
    }).join('');

    $('#endpoint-container').innerHTML = html;

    // 折叠交互: 默认展开, 点击标题行收起/展开
    $$('.endpoint-head').forEach((head) => {
        head.addEventListener('click', () => {
            const card = head.closest('.endpoint');
            card.classList.toggle('collapsed');
        });
    });

    bindCopyButtons();
}

/* ---------------- 复制 ---------------- */

function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
    }
    // http 非安全上下文下 clipboard API 不可用, 回退到 execCommand
    return new Promise((resolve, reject) => {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand('copy') ? resolve() : reject(new Error('复制被拒绝'));
        } catch (e) {
            reject(e);
        } finally {
            document.body.removeChild(ta);
        }
    });
}

function bindCopyButtons() {
    $$('[data-copy]').forEach((btn) => {
        if (btn.dataset.bound) return;
        btn.dataset.bound = '1';
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const target = $(btn.dataset.copy);
            if (!target) return;
            try {
                await copyText(target.textContent);
                toast('已复制到剪贴板', 'success', 1600);
            } catch (err) {
                toast('复制失败, 请手动选择文本', 'error');
            }
        });
    });
}

/* ---------------- 初始化 ---------------- */

document.addEventListener('DOMContentLoaded', async () => {
    $('#btn-copy-base').addEventListener('click', async () => {
        try {
            await copyText(location.origin);
            toast('服务地址已复制', 'success', 1600);
        } catch (err) {
            toast('复制失败', 'error');
        }
    });

    $('#btn-expand').addEventListener('click', () => {
        $$('.endpoint').forEach((el) => el.classList.remove('collapsed'));
    });
    $('#btn-collapse').addEventListener('click', () => {
        $$('.endpoint').forEach((el) => el.classList.add('collapsed'));
    });

    try {
        const resp = await fetch('/openapi.json');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        SPEC = await resp.json();
    } catch (err) {
        $('#alert-box').innerHTML =
            `<div class="alert alert-error">读取接口定义失败: ${escapeHtml(err.message)}</div>`;
        $('#endpoint-container').innerHTML =
            '<div class="card"><div class="empty">接口定义不可用</div></div>';
        return;
    }

    const endpoints = parseSpec(SPEC);
    renderSummary(endpoints, SPEC);
    renderEndpoints(endpoints);
});
