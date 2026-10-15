(function () {
  const data = JSON.parse(document.getElementById('chart-data').textContent);
  const css = () => getComputedStyle(document.documentElement);
  const v = (name) => css().getPropertyValue(name).trim();
  const fmt = (x) => '$' + Number(x || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmt0 = (x) => '$' + Number(x || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const empColor = (slot) => v('--s' + ((slot % 8) + 1));
  const charts = [];

  function base() {
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 12;
    Chart.defaults.color = v('--text-2');
    Chart.defaults.borderColor = v('--grid');
  }

  // Round only the outermost segment of a stack, like a data-end cap.
  function capRadius(ctx) {
    const ds = ctx.chart.data.datasets;
    const i = ctx.dataIndex;
    for (let k = ds.length - 1; k >= 0; k--) {
      if (!ctx.chart.isDatasetVisible(k)) continue;
      if ((ds[k].data[i] || 0) > 0) {
        return k === ctx.datasetIndex ? { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 } : 0;
      }
    }
    return 0;
  }
  function capRadiusH(ctx) {
    const r = capRadius(ctx);
    return r ? { topRight: 4, bottomRight: 4, topLeft: 0, bottomLeft: 0 } : 0;
  }

  const tooltip = () => ({
    backgroundColor: v('--surface'), titleColor: v('--text'), bodyColor: v('--text-2'),
    borderColor: v('--border'), borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true,
    callbacks: {
      label: (c) => ` ${c.dataset.label}: ${fmt(c.parsed.y ?? c.parsed.x)}`,
    },
  });

  function stackedFooter(items) {
    if (items.length < 2) return '';
    const sum = items.reduce((a, c) => a + (c.parsed.y ?? c.parsed.x ?? 0), 0);
    return 'Total: ' + fmt(sum);
  }

  function segment(label, values, color, horizontal) {
    return {
      label, data: values, backgroundColor: color, hoverBackgroundColor: color,
      borderColor: v('--surface'),
      borderWidth: horizontal ? { right: 2 } : { top: 2 },
      borderSkipped: false,
      borderRadius: horizontal ? capRadiusH : capRadius,
      maxBarThickness: horizontal ? 28 : 36,
      pointStyle: 'rectRounded',
    };
  }

  function build() {
    base();
    charts.forEach((c) => c.destroy());
    charts.length = 0;
    const grid = { color: v('--grid'), drawTicks: false };
    const noGrid = { display: false };

    // 1. Earnings by employer: one horizontal bar each, split wages / cash / card.
    const emps = data.employers;
    const box = document.getElementById('empBox');
    box.style.height = Math.max(120, 56 + emps.length * 46) + 'px';
    charts.push(new Chart(document.getElementById('empChart'), {
      type: 'bar',
      data: {
        labels: emps.map((e) => e.name),
        datasets: [
          segment('Base wages', emps.map((e) => e.wages), v('--wages'), true),
          segment('Cash tips', emps.map((e) => e.cash), v('--cash'), true),
          segment('Card tips', emps.map((e) => e.card), v('--card'), true),
        ],
      },
      options: {
        indexAxis: 'y', maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', axis: 'y', intersect: false },
        scales: {
          x: { stacked: true, grid, border: { display: false }, ticks: { callback: fmt0, padding: 6, maxRotation: 0, maxTicksLimit: 5 } },
          y: { stacked: true, grid: noGrid, border: { display: false }, ticks: { color: v('--text'), padding: 8 } },
        },
        plugins: { legend: { display: false }, tooltip: { ...tooltip(), callbacks: { ...tooltip().callbacks, footer: stackedFooter } } },
      },
    }));
    document.getElementById('empTable').innerHTML = emps.map((e) =>
      `<tr><td>${escapeHtml(e.name)}</td><td class="r">${e.hours}</td><td class="r">${fmt(e.wages)}</td>` +
      `<td class="r">${fmt(e.cash)}</td><td class="r">${fmt(e.card)}</td><td class="r"><b>${fmt(e.total)}</b></td>` +
      `<td class="r">${e.hours ? fmt(e.total / e.hours) : '—'}</td></tr>`).join('');

    // 2. Cash vs card tips per period.
    const p = data.period;
    const unit = p.unit === 'week' ? 'per week' : 'per month';
    document.getElementById('tipUnit').textContent = unit;
    document.getElementById('periodUnit').textContent = unit;
    const colOpts = (legendless) => ({
      maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { stacked: true, grid: noGrid, border: { color: v('--border') }, ticks: { maxRotation: 0, autoSkipPadding: 12 } },
        y: { stacked: true, grid, border: { display: false }, ticks: { callback: fmt0, padding: 6, maxTicksLimit: 6 } },
      },
      plugins: {
        legend: { display: !legendless, position: 'top', align: 'start', labels: { usePointStyle: true, pointStyle: 'rectRounded', boxWidth: 10, boxHeight: 10, color: v('--text-2') } },
        tooltip: { ...tooltip(), callbacks: { ...tooltip().callbacks, footer: stackedFooter } },
      },
    });
    charts.push(new Chart(document.getElementById('tipChart'), {
      type: 'bar',
      data: { labels: p.labels, datasets: [segment('Cash tips', p.cash, v('--cash')), segment('Card tips', p.card, v('--card'))] },
      options: colOpts(true),
    }));

    // 3. Earnings per period, stacked by employer (fixed color per employer).
    charts.push(new Chart(document.getElementById('periodChart'), {
      type: 'bar',
      data: { labels: p.labels, datasets: p.employers.map((e) => segment(e.name, e.values, empColor(e.slot))) },
      options: colOpts(true),
    }));
    document.getElementById('periodLegend').innerHTML = p.employers.map((e) =>
      `<span><i class="swatch" style="background:${empColor(e.slot)}"></i>${escapeHtml(e.name)}</span>`).join('');

    // 4. Effective hourly rate per week vs minimum wage.
    const eff = data.effective;
    const crosshair = {
      id: 'crosshair',
      afterDatasetsDraw(chart) {
        const act = chart.tooltip && chart.tooltip.getActiveElements();
        if (!act || !act.length) return;
        const x = act[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
        ctx.save(); ctx.strokeStyle = v('--text-3'); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
      },
    };
    const lo = Math.min(...eff.rate.filter((x) => x != null), ...eff.minimum.filter((x) => x != null));
    charts.push(new Chart(document.getElementById('effChart'), {
      type: 'line',
      data: {
        labels: eff.labels,
        datasets: [
          { label: 'Your $/hr', data: eff.rate, borderColor: v('--s1'), backgroundColor: v('--s1'), borderWidth: 2,
            pointRadius: eff.labels.length > 20 ? 0 : 3, pointHoverRadius: 5, pointBackgroundColor: v('--s1'),
            pointBorderColor: v('--surface'), pointBorderWidth: 2, cubicInterpolationMode: 'monotone', spanGaps: true },
          { label: 'Minimum wage', data: eff.minimum, borderColor: v('--text-3'), borderDash: [5, 4], borderWidth: 1.5,
            pointRadius: 0, pointHoverRadius: 0, stepped: true, fill: false },
        ],
      },
      options: {
        maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { grid: noGrid, border: { color: v('--border') }, ticks: { maxRotation: 0, autoSkipPadding: 12 } },
          y: { suggestedMin: Math.max(0, Math.floor(lo - 4)), grid, border: { display: false }, ticks: { callback: fmt0, padding: 6, maxTicksLimit: 6 } },
        },
        plugins: { legend: { display: false }, tooltip: { ...tooltip(), callbacks: { label: (c) => ` ${c.dataset.label}: ${fmt(c.parsed.y)}/hr` } } },
      },
      plugins: [crosshair],
    }));
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  build();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', build);
})();
