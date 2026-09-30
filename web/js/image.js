/* 图片检测页: 上传 -> 检测 -> 渲染结果 */

let currentFile = null;

function resetResult() {
    $('#result-img').classList.add('hidden');
    $('#result-placeholder').classList.remove('hidden');
    $('#result-placeholder').textContent = '检测完成后在此显示带检测框的结果图';
    $('#stat-total').textContent = '0';
    $('#stat-cost').innerHTML = '0<small style="font-size:13px;"> ms</small>';
    $('#stat-size').textContent = '-';
    $('#class-tags').innerHTML = '<span class="muted small">暂无数据</span>';
    $('#detail-body').innerHTML = '<tr><td colspan="5" class="empty">暂无检测结果</td></tr>';
}

function bindUpload() {
    const area = $('#upload-area');
    const input = $('#file-input');

    area.addEventListener('click', () => input.click());

    area.addEventListener('dragover', (e) => {
        e.preventDefault();
        area.classList.add('dragover');
    });
    area.addEventListener('dragleave', () => area.classList.remove('dragover'));
    area.addEventListener('drop', (e) => {
        e.preventDefault();
        area.classList.remove('dragover');
        if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
    });

    input.addEventListener('change', () => {
        if (input.files.length) selectFile(input.files[0]);
    });
}

function selectFile(file) {
    if (!file.type.startsWith('image/')) {
        toast('请选择图片文件', 'error');
        return;
    }
    currentFile = file;
    $('#file-name').textContent = `已选择: ${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
    $('#file-name').classList.remove('hidden');
    $('#btn-detect').disabled = false;

    const reader = new FileReader();
    reader.onload = (e) => {
        const img = $('#origin-img');
        img.src = e.target.result;
        img.classList.remove('hidden');
        $('#origin-placeholder').classList.add('hidden');
    };
    reader.readAsDataURL(file);
    resetResult();
    $('#alert-box').innerHTML = '';
}

async function runDetect() {
    if (!currentFile) {
        toast('请先选择图片', 'error');
        return;
    }
    const btn = $('#btn-detect');
    btn.disabled = true;
    $('#progress-text').textContent = '正在上传并推理, 首次加载模型会稍慢...';
    $('#alert-box').innerHTML = '';

    const formData = new FormData();
    formData.append('file', currentFile);
    formData.append('conf', $('#conf-slider').value);

    try {
        const data = await API.upload('/api/detect/image', formData, (p) => {
            $('#progress-text').textContent = `上传进度 ${p}%`;
        });
        renderResult(data);
        toast(`检测完成, 共识别 ${data.total} 个目标`, 'success');
    } catch (err) {
        $('#alert-box').innerHTML = `<div class="alert alert-error">检测失败: ${escapeHtml(err.message)}</div>`;
        toast('检测失败', 'error');
    } finally {
        btn.disabled = false;
        $('#progress-text').textContent = '';
    }
}

function renderResult(data) {
    const img = $('#result-img');
    img.src = data.result_url;
    img.classList.remove('hidden');
    $('#result-placeholder').classList.add('hidden');

    $('#stat-total').textContent = fmtNumber(data.total);
    $('#stat-cost').innerHTML = `${data.inference_ms}<small style="font-size:13px;"> ms</small>`;
    $('#stat-size').textContent = `${data.width} × ${data.height}`;

    const tags = $('#class-tags');
    tags.innerHTML = data.counts.length
        ? data.counts.map((c) => `<span class="tag">${escapeHtml(c.class_name)}: <b>${c.count}</b></span>`).join('')
        : '<span class="muted small">未检出目标(可降低置信度阈值后重试)</span>';

    const tbody = $('#detail-body');
    tbody.innerHTML = data.detections.length
        ? data.detections.map((d, i) => {
            const [x1, y1, x2, y2] = d.bbox;
            return `<tr>
                <td>${i + 1}</td>
                <td>${escapeHtml(d.class_name)}</td>
                <td>${(d.confidence * 100).toFixed(1)}%</td>
                <td class="mono small">${x1.toFixed(0)}, ${y1.toFixed(0)}, ${x2.toFixed(0)}, ${y2.toFixed(0)}</td>
                <td class="small muted">${(x2 - x1).toFixed(0)} × ${(y2 - y1).toFixed(0)}</td>
            </tr>`;
        }).join('')
        : '<tr><td colspan="5" class="empty">未检出目标</td></tr>';
}

document.addEventListener('DOMContentLoaded', () => {
    bindUpload();
    $('#btn-detect').addEventListener('click', runDetect);
    $('#btn-reset').addEventListener('click', () => {
        currentFile = null;
        $('#file-input').value = '';
        $('#file-name').classList.add('hidden');
        $('#btn-detect').disabled = true;
        $('#origin-img').classList.add('hidden');
        $('#origin-placeholder').classList.remove('hidden');
        $('#alert-box').innerHTML = '';
        resetResult();
    });

    const slider = $('#conf-slider');
    slider.addEventListener('input', () => {
        $('#conf-value').textContent = parseFloat(slider.value).toFixed(2);
    });

    API.get('/api/config').then((cfg) => {
        slider.value = cfg.conf_threshold;
        $('#conf-value').textContent = Number(cfg.conf_threshold).toFixed(2);
        $('#max-size').textContent = cfg.max_upload_mb;
    }).catch(() => {});
});
