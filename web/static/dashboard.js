const ROUTE_META = {
    overview: { label: '개요', icon: '⌂' },
    portfolio: { label: '포트폴리오', icon: '◫' },
    signals: { label: '시그널', icon: '◌' },
    automation: { label: '자동화', icon: '⚙' },
    logs: { label: '운영 로그', icon: '☰' },
};

const initialViewScript = document.getElementById('initial-view');
const appStateScript = document.getElementById('app-state');
const activeThemeScript = document.getElementById('active-theme');
const themeDesignsScript = document.getElementById('theme-designs');
let appData = appStateScript ? JSON.parse(appStateScript.textContent) : {};
const activeTheme = activeThemeScript ? JSON.parse(activeThemeScript.textContent) : 'light';
const themeDesigns = themeDesignsScript ? JSON.parse(themeDesignsScript.textContent) : {};
const initialView = initialViewScript ? JSON.parse(initialViewScript.textContent) : 'overview';

const uiState = {
    currentView: normalizeView(initialView || 'overview'),
    chartRange: 'ALL',
    drawerOpen: false,
    activeModal: null,
    selectedHoldingId: null,
    themeMode: normalizeTheme(appData.theme?.current || activeTheme || 'light'),
};

function normalizeView(view) {
    return ROUTE_META[view] ? view : 'overview';
}

function normalizeTheme(themeMode) {
    return String(themeMode).toLowerCase() === 'dark' ? 'dark' : 'light';
}

function getThemeSpec(themeMode = uiState.themeMode) {
    return themeDesigns[normalizeTheme(themeMode)] || themeDesigns.light || {};
}

function getThemeValue(themeMode, paths, fallback = '') {
    const themeSpec = getThemeSpec(themeMode);
    const allPaths = Array.isArray(paths[0]) ? paths : [paths];

    for (const path of allPaths) {
        let cursor = themeSpec;
        let found = true;
        for (const key of path) {
            if (cursor && Object.prototype.hasOwnProperty.call(cursor, key)) {
                cursor = cursor[key];
            } else {
                found = false;
                break;
            }
        }
        if (found && cursor !== undefined && cursor !== null && cursor !== '') {
            return cursor;
        }
    }

    return fallback;
}

function setThemeToggleState() {
    const icon = document.getElementById('themeToggleIcon');
    const label = document.getElementById('themeToggleLabel');
    if (icon) icon.textContent = uiState.themeMode === 'dark' ? '🌙' : '☀';
    if (label) label.textContent = uiState.themeMode === 'dark' ? '야간' : '주간';
}

function applyTheme(themeMode, persist = false) {
    uiState.themeMode = normalizeTheme(themeMode);
    document.documentElement.dataset.theme = uiState.themeMode;
    document.body.dataset.theme = uiState.themeMode;

    const root = document.documentElement;
    const variableMap = {
        '--canvas': [['designSystem', 'globalTokens', 'colors', 'canvas']],
        '--sidebar-surface': [['designSystem', 'globalTokens', 'colors', 'sidebarSurface']],
        '--topbar-surface': [['designSystem', 'globalTokens', 'colors', 'topbarSurface']],
        '--content-surface': [['designSystem', 'globalTokens', 'colors', 'contentSurface']],
        '--card-surface': [['designSystem', 'globalTokens', 'colors', 'cardSurface']],
        '--card-surface-elevated': [['designSystem', 'globalTokens', 'colors', 'cardSurfaceElevated']],
        '--input-surface': [['designSystem', 'globalTokens', 'colors', 'inputSurface']],
        '--segmented-surface': [['designSystem', 'globalTokens', 'colors', 'segmentedSurface']],
        '--divider': [['designSystem', 'globalTokens', 'colors', 'divider']],
        '--border-subtle': [['designSystem', 'globalTokens', 'colors', 'borderSubtle']],
        '--text-primary': [['designSystem', 'globalTokens', 'colors', 'textPrimary']],
        '--text-secondary': [['designSystem', 'globalTokens', 'colors', 'textSecondary']],
        '--text-muted': [['designSystem', 'globalTokens', 'colors', 'textMuted']],
        '--text-inverse': [['designSystem', 'globalTokens', 'colors', 'textInverse']],
        '--accent-primary': [
            ['designSystem', 'globalTokens', 'colors', 'accentGold'],
            ['designSystem', 'globalTokens', 'colors', 'brandPrimary']
        ],
        '--accent-primary-strong': [
            ['designSystem', 'globalTokens', 'colors', 'accentGoldStrong'],
            ['designSystem', 'globalTokens', 'colors', 'brandPrimaryDark']
        ],
        '--accent-primary-soft': [
            ['designSystem', 'globalTokens', 'colors', 'accentGoldSoft'],
            ['designSystem', 'globalTokens', 'colors', 'brandPrimarySoft']
        ],
        '--positive': [
            ['designSystem', 'globalTokens', 'colors', 'positive'],
            ['designSystem', 'globalTokens', 'colors', 'accentGreen']
        ],
        '--negative': [
            ['designSystem', 'globalTokens', 'colors', 'negative'],
            ['designSystem', 'globalTokens', 'colors', 'accentRed']
        ],
        '--white-soft': [['designSystem', 'globalTokens', 'colors', 'whiteSoft']],
        '--black-soft': [
            ['designSystem', 'globalTokens', 'colors', 'blackSoft'],
            ['designSystem', 'globalTokens', 'colors', 'tooltipBg']
        ],
        '--gradient-cta': [
            ['designSystem', 'globalTokens', 'gradients', 'ctaGold', 'css'],
            ['designSystem', 'globalTokens', 'gradients', 'primaryCta', 'css']
        ],
        '--gradient-chart': [
            ['designSystem', 'globalTokens', 'gradients', 'chartAreaGold', 'css'],
            ['designSystem', 'globalTokens', 'gradients', 'lineAreaGreen', 'css']
        ],
        '--gradient-wallet': [
            ['designSystem', 'globalTokens', 'gradients', 'sidebarWalletPanel', 'css'],
            ['designSystem', 'globalTokens', 'gradients', 'promoPanel', 'css']
        ],
        '--shadow-card': [['designSystem', 'globalTokens', 'shadows', 'cardLow']],
        '--shadow-card-strong': [
            ['designSystem', 'globalTokens', 'shadows', 'cardMedium'],
            ['designSystem', 'globalTokens', 'shadows', 'floating']
        ],
        '--shadow-gold': [
            ['designSystem', 'globalTokens', 'shadows', 'buttonGlowGold'],
            ['designSystem', 'globalTokens', 'shadows', 'floating']
        ],
        '--shadow-chart': [
            ['designSystem', 'globalTokens', 'shadows', 'chartGlowGold'],
            ['designSystem', 'globalTokens', 'shadows', 'tooltip']
        ],
        '--radius-panel': [
            ['designSystem', 'globalTokens', 'radii', 'panel'],
            ['designSystem', 'globalTokens', 'radii', 'card']
        ],
        '--radius-control': [['designSystem', 'globalTokens', 'radii', 'control']],
        '--radius-pill': [['designSystem', 'globalTokens', 'radii', 'pill']],
        '--radius-small': [['designSystem', 'globalTokens', 'radii', 'small']],
        '--nav-hover-surface': [['designSystem', 'componentStyles', 'sidebar', 'navItem', 'hover', 'surface']],
        '--nav-hover-text': [['designSystem', 'componentStyles', 'sidebar', 'navItem', 'hover', 'textColor']],
        '--nav-active-surface': [['designSystem', 'componentStyles', 'sidebar', 'navItem', 'active', 'surface']],
        '--nav-active-text': [['designSystem', 'componentStyles', 'sidebar', 'navItem', 'active', 'textColor']],
        '--utility-hover-surface': [
            ['designSystem', 'componentStyles', 'topbar', 'utilityIcons', 'hoverSurface'],
            ['designSystem', 'componentStyles', 'heroChartCard', 'actionButtons', 'iconButton', 'surface']
        ],
        '--wallet-balance-surface': [
            ['designSystem', 'componentStyles', 'sidebar', 'walletPanel', 'balancePanel', 'surface'],
            ['designSystem', 'componentStyles', 'summaryCards', 'baseCard', 'surfaceColor']
        ],
        '--secondary-surface': [
            ['designSystem', 'componentStyles', 'assetHeader', 'utilityActions', 'secondaryButton', 'surface'],
            ['designSystem', 'componentStyles', 'heroChartCard', 'actionButtons', 'secondary', 'surface']
        ],
        '--secondary-text': [
            ['designSystem', 'componentStyles', 'assetHeader', 'utilityActions', 'secondaryButton', 'textColor'],
            ['designSystem', 'componentStyles', 'heroChartCard', 'actionButtons', 'secondary', 'text']
        ],
        '--secondary-border': [
            ['designSystem', 'componentStyles', 'assetHeader', 'utilityActions', 'secondaryButton', 'border'],
            ['designSystem', 'componentStyles', 'heroChartCard', 'actionButtons', 'secondary', 'border']
        ],
        '--secondary-hover-surface': [
            ['designSystem', 'componentStyles', 'assetHeader', 'utilityActions', 'secondaryButtonHover', 'surface'],
            ['designSystem', 'stateArchitecture', 'secondaryButton', 'hover', 'surface']
        ],
        '--surface-subtle': [
            ['designSystem', 'globalTokens', 'colors', 'cardSurfaceAlt'],
            ['designSystem', 'globalTokens', 'colors', 'shellSurface']
        ],
        '--surface-subtle-strong': [['designSystem', 'globalTokens', 'colors', 'shellSurface']],
        '--table-head-surface': [['designSystem', 'globalTokens', 'colors', 'cardSurfaceAlt']],
        '--row-hover-surface': [['designSystem', 'componentStyles', 'assetListCard', 'row', 'hoverSurface']],
        '--table-row-border': [['designSystem', 'componentStyles', 'assetListCard', 'row', 'borderBottom']],
        '--progress-track-color': [
            ['designSystem', 'componentStyles', 'cards', 'marketStatsCard', 'trackColor'],
            ['designSystem', 'globalTokens', 'colors', 'chartGrid']
        ],
        '--chart-grid-color': [
            ['designSystem', 'componentStyles', 'cards', 'chartCard', 'contentElements', 'chartGrid', 'lineColor'],
            ['designSystem', 'componentStyles', 'heroChartCard', 'chart', 'gridColor']
        ],
        '--log-surface': [
            ['designSystem', 'globalTokens', 'colors', 'blackSoft'],
            ['designSystem', 'globalTokens', 'colors', 'tooltipBg']
        ]
    };

    Object.entries(variableMap).forEach(([name, paths]) => {
        const value = getThemeValue(uiState.themeMode, paths, root.style.getPropertyValue(name));
        if (value) {
            root.style.setProperty(name, value);
        }
    });

    root.style.setProperty('--accent-gold', getComputedStyle(root).getPropertyValue('--accent-primary'));
    root.style.setProperty('--accent-gold-strong', getComputedStyle(root).getPropertyValue('--accent-primary-strong'));
    root.style.setProperty('--accent-gold-soft', getComputedStyle(root).getPropertyValue('--accent-primary-soft'));
    root.style.setProperty('--overlay-backdrop', 'rgba(0, 0, 0, 0.5)');
    root.style.setProperty('--focus-ring', uiState.themeMode === 'dark' ? 'rgba(245, 199, 91, 0.55)' : 'rgba(108, 76, 246, 0.28)');

    appData.theme = {
        ...(appData.theme || {}),
        current: uiState.themeMode,
        available: ['light', 'dark'],
    };
    if (appData.config) {
        appData.config.theme_mode = uiState.themeMode;
    }

    setThemeToggleState();

    if (persist) {
        window.localStorage.setItem('us-etf-sniper-theme', uiState.themeMode);
    }
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatCurrency(value, currency = 'KRW', digits = 0) {
    const numeric = Number(value || 0);
    if (currency === 'USD') {
        return `$${numeric.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
    }
    return `₩${numeric.toLocaleString('ko-KR', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function formatNumber(value, digits = 0) {
    return Number(value || 0).toLocaleString('ko-KR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function formatSignedNumber(value, prefix = '', digits = 0) {
    const numeric = Number(value || 0);
    const sign = numeric >= 0 ? '+' : '-';
    return `${sign}${prefix}${Math.abs(numeric).toLocaleString('ko-KR', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function formatPercent(value, digits = 2) {
    const numeric = Number(value || 0);
    return `${numeric >= 0 ? '+' : ''}${numeric.toFixed(digits)}%`;
}

function toneClassFromValue(value) {
    return Number(value || 0) >= 0 ? 'positive-text' : 'negative-text';
}

function badgeTone(tone) {
    return ['positive', 'negative', 'warning', 'info', 'accent'].includes(tone) ? tone : 'info';
}

function setDocumentTitle() {
    const route = ROUTE_META[uiState.currentView] || ROUTE_META.overview;
    document.title = `US-ETF-Sniper · ${route.label}`;
}

function navigate(route, replace = false) {
    const nextRoute = normalizeView(route);
    uiState.currentView = nextRoute;
    uiState.drawerOpen = false;
    if (uiState.activeModal) {
        closeModal(uiState.activeModal);
    }
    const nextPath = `/${nextRoute}`;
    if (replace) {
        window.history.replaceState({}, '', nextPath);
    } else {
        window.history.pushState({}, '', nextPath);
    }
    renderApp();
}

function getCombinedAccount() {
    return appData.accounts?.combined || {
        totalKrw: 0,
        totalUsd: 0,
        profitKrw: 0,
        profitUsd: 0,
        profitPct: 0,
        exchangeRate: 1450,
    };
}

function getHoldings(region) {
    return (appData.holdings || []).filter((item) => !region || item.region === region);
}

function getTrendPoints() {
    const points = appData.charts?.assetTrend || [];
    if (uiState.chartRange === '1W') return points.slice(-7);
    if (uiState.chartRange === '1M') return points.slice(-30);
    if (uiState.chartRange === '1Y') return points.slice(-12);
    return points;
}

function renderSidebar() {
    const nav = document.getElementById('sidebarNav');
    if (!nav) return;
    const views = appData.views || [];
    nav.innerHTML = views.map((view) => {
        const meta = ROUTE_META[view.id] || ROUTE_META.overview;
        const activeClass = view.id === uiState.currentView ? 'active' : '';
        return `
            <button type="button" class="nav-item ${activeClass}" data-route="${view.id}">
                <span class="nav-icon">${meta.icon}</span>
                <span class="nav-text">
                    <span class="nav-label">${escapeHtml(view.label)}</span>
                    <span class="nav-description">${escapeHtml(view.description)}</span>
                </span>
            </button>
        `;
    }).join('');
}

function renderWalletPanel() {
    const wallet = document.getElementById('walletPanel');
    if (!wallet) return;
    const combined = getCombinedAccount();
    const topHoldings = getHoldings().slice(0, 6);
    wallet.innerHTML = `
        <div class="wallet-heading">
            <div>
                <div class="wallet-title">Your Wallet</div>
                <div class="wallet-subtitle">실시간 보유 종목 요약</div>
            </div>
            <span class="badge accent">LIVE</span>
        </div>
        <div class="wallet-avatars">
            ${topHoldings.map((holding) => `
                <span class="wallet-avatar" style="background:${escapeHtml(holding.accentColor)};">${escapeHtml(holding.symbol.slice(0, 3))}</span>
            `).join('') || '<span class="muted-text">보유 종목 없음</span>'}
        </div>
        <div class="wallet-balance">
            <strong>${formatCurrency(combined.totalUsd, 'USD', 2)}</strong>
            <span>${formatCurrency(combined.totalKrw, 'KRW')} · ${formatPercent(combined.profitPct)}</span>
        </div>
    `;
}

function renderTopbar() {
    const route = ROUTE_META[uiState.currentView] || ROUTE_META.overview;
    const breadcrumb = document.getElementById('breadcrumbTrail');
    const pageMeta = document.getElementById('pageMeta');
    const ticker = document.getElementById('marketTicker');
    const session = appData.session?.user || { role: 'admin', avatarInitials: 'OP' };
    const profileAvatar = document.getElementById('profileAvatar');
    const profileRole = document.getElementById('profileRole');

    if (breadcrumb) {
        breadcrumb.innerHTML = `<span>Home</span><span>/</span><strong>${escapeHtml(route.label)}</strong>`;
    }
    if (pageMeta) {
        pageMeta.textContent = `업데이트 ${appData.status?.generatedAt || '-'} · 마지막 로그 ${appData.status?.lastUpdate || '-'}`;
    }
    if (profileAvatar) profileAvatar.textContent = session.avatarInitials || 'OP';
    if (profileRole) profileRole.textContent = (session.role || 'admin').toUpperCase();
    if (ticker) {
        ticker.innerHTML = (appData.marketTicker || []).map((item) => `
            <div class="ticker-item">
                <div>
                    <div class="ticker-kicker">${escapeHtml(item.label)}</div>
                    <strong>${escapeHtml(item.value)}</strong>
                    <span class="${item.tone === 'negative' ? 'negative-text' : item.tone === 'positive' ? 'positive-text' : ''}">${escapeHtml(item.trend)}</span>
                </div>
            </div>
        `).join('');
    }
}

function renderHero() {
    const container = document.getElementById('heroSection');
    if (!container) return;
    const combined = getCombinedAccount();
    const status = appData.status || {};
    const config = appData.config || {};
    const profitTone = combined.profitKrw >= 0 ? 'positive' : 'negative';

    container.innerHTML = `
        <div class="hero-top">
            <div class="hero-identity">
                <div class="asset-badge">₿</div>
                <div>
                    <div class="hero-label">US ETF Sniper / Operations</div>
                    <div class="hero-title">${formatCurrency(combined.totalKrw, 'KRW')}</div>
                    <div class="hero-subtitle">
                        ${formatSignedNumber(combined.profitKrw, '₩')} · <span class="${profitTone === 'positive' ? 'positive-text' : 'negative-text'}">${formatPercent(combined.profitPct)}</span>
                        · ${status.botRunning ? '봇 실행 중' : '봇 중지'}
                    </div>
                </div>
            </div>
            <div class="hero-actions">
                <button type="button" class="secondary-action" data-action="refresh">실시간 동기화</button>
                <button type="button" class="ghost-action" data-route="automation">설정 열기</button>
                <button type="button" class="primary-action" data-action="open-restart-modal">봇 재시작</button>
            </div>
        </div>
        <div class="hero-stats">
            <div class="hero-stat">
                <div class="hero-stat-label">시장 상태</div>
                <div class="hero-stat-value">${escapeHtml(status.marketStatus || '-')}</div>
                <div class="hero-stat-meta">${escapeHtml(status.botStatusLabel || '-')}</div>
            </div>
            <div class="hero-stat">
                <div class="hero-stat-label">자동 전략</div>
                <div class="hero-stat-value">${config.auto_strategy ? 'ON' : 'OFF'}</div>
                <div class="hero-stat-meta">${escapeHtml((config.strategy || 'day').toUpperCase())} / ${escapeHtml((config.trading_mode || 'safe').toUpperCase())}</div>
            </div>
            <div class="hero-stat">
                <div class="hero-stat-label">페르소나</div>
                <div class="hero-stat-value">${escapeHtml((config.persona || 'neutral').toUpperCase())}</div>
                <div class="hero-stat-meta">리스크 기준 ${formatNumber(appData.preferences?.risk?.portfolio_drawdown_pct || 0, 1)}%</div>
            </div>
            <div class="hero-stat">
                <div class="hero-stat-label">보유 종목</div>
                <div class="hero-stat-value">${formatNumber((appData.holdings || []).length)}</div>
                <div class="hero-stat-meta">US ${formatNumber(getHoldings('US').length)} / KR ${formatNumber(getHoldings('KR').length)}</div>
            </div>
        </div>
    `;
}

function renderRouteTabs() {
    const tabs = document.getElementById('routeTabs');
    if (!tabs) return;
    tabs.innerHTML = (appData.views || []).map((view) => `
        <button type="button" class="route-tab ${view.id === uiState.currentView ? 'active' : ''}" data-route="${view.id}">${escapeHtml(view.label)}</button>
    `).join('');
}

function renderSparklineSvg(points) {
    if (!points.length) {
        return {
            svg: '<div class="empty-state">차트 데이터가 없습니다.</div>',
            labels: '',
        };
    }
    const safePoints = points.length > 1 ? points : [...points, points[0]];
    const values = safePoints.map((point) => Number(point.value || 0));
    const width = 640;
    const height = 260;
    const paddingX = 24;
    const paddingY = 18;
    const min = Math.min(...values) * 0.98;
    const max = Math.max(...values) * 1.02;
    const delta = Math.max(max - min, 1);
    const step = (width - paddingX * 2) / Math.max(safePoints.length - 1, 1);

    const chartPoints = safePoints.map((point, index) => {
        const x = paddingX + step * index;
        const y = height - paddingY - (((Number(point.value || 0) - min) / delta) * (height - paddingY * 2));
        return { x, y, raw: point };
    });

    const linePath = chartPoints.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
    const areaPath = `${linePath} L ${chartPoints[chartPoints.length - 1].x.toFixed(2)} ${(height - paddingY).toFixed(2)} L ${chartPoints[0].x.toFixed(2)} ${(height - paddingY).toFixed(2)} Z`;
    const gridLines = new Array(5).fill(null).map((_, index) => {
        const y = paddingY + ((height - paddingY * 2) / 4) * index;
        return `<line x1="${paddingX}" y1="${y}" x2="${width - paddingX}" y2="${y}" stroke="rgba(255,255,255,0.08)" stroke-width="1" />`;
    }).join('');

    const labels = chartPoints.map((point) => `
        <div class="mini-stat">
            <span>${escapeHtml(point.raw.label)}</span>
            <strong>${formatCurrency(point.raw.value, 'KRW')}</strong>
        </div>
    `).join('');

    return {
        svg: `
            <svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="자산 추이 차트">
                <defs>
                    <linearGradient id="assetAreaGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stop-color="rgba(245,199,91,0.42)" />
                        <stop offset="58%" stop-color="rgba(245,199,91,0.18)" />
                        <stop offset="100%" stop-color="rgba(245,199,91,0.02)" />
                    </linearGradient>
                </defs>
                ${gridLines}
                <path d="${areaPath}" fill="url(#assetAreaGradient)" />
                <path d="${linePath}" fill="none" stroke="var(--accent-gold)" stroke-width="3" filter="drop-shadow(0 0 10px rgba(245,199,91,0.28))" />
                ${chartPoints.map((point) => `<circle cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="6" fill="var(--card-surface)" stroke="var(--accent-gold)" stroke-width="3" />`).join('')}
            </svg>
        `,
        labels,
    };
}

function renderOverviewView() {
    const points = getTrendPoints();
    const latest = points[points.length - 1] || { value: 0 };
    const first = points[0] || latest;
    const valueSeries = points.length ? points.map((point) => Number(point.value || 0)) : [0];
    const high = Math.max(...valueSeries);
    const low = Math.min(...valueSeries);
    const sparkline = renderSparklineSvg(points);
    const combined = getCombinedAccount();
    const usTotal = Number(appData.accounts?.us?.total_asset_krw || 0);
    const krTotal = Number(appData.accounts?.kr?.total_asset_krw || 0);
    const holdings = getHoldings();
    const positiveHoldings = holdings.filter((item) => Number(item.profitPct || 0) >= 0).length;
    const totalHoldings = Math.max(holdings.length, 1);
    const metrics = [
        { label: '미국 자산 비중', value: `${((usTotal / Math.max(combined.totalKrw, 1)) * 100).toFixed(1)}%`, progress: (usTotal / Math.max(combined.totalKrw, 1)) * 100 },
        { label: '국내 자산 비중', value: `${((krTotal / Math.max(combined.totalKrw, 1)) * 100).toFixed(1)}%`, progress: (krTotal / Math.max(combined.totalKrw, 1)) * 100 },
        { label: '수익권 보유 비율', value: `${positiveHoldings}/${holdings.length || 0}`, progress: (positiveHoldings / totalHoldings) * 100 },
        { label: 'DCA 투자 비중', value: `${formatNumber(appData.preferences?.dca?.daily_investment_pct || 0, 1)}%`, progress: Number(appData.preferences?.dca?.daily_investment_pct || 0) },
    ];

    return `
        <div class="content-grid">
            <div class="stack-grid">
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">자산 추이</div>
                            <div class="card-subtitle">design.json 토큰에 맞춘 골드 라인 차트</div>
                        </div>
                        <div class="segment-group">
                            ${['1W', '1M', '1Y', 'ALL'].map((range) => `
                                <button type="button" class="segment-option ${uiState.chartRange === range ? 'active' : ''}" data-range="${range}">${range}</button>
                            `).join('')}
                        </div>
                    </div>
                    <div class="chart-surface">
                        <div class="chart-wrapper">
                            <div class="chart-value-pill">${formatCurrency(latest.value, 'KRW')}</div>
                            ${sparkline.svg}
                        </div>
                        <div class="chart-foot">
                            <div class="mini-stat"><span>기간 변화</span><strong>${formatSignedNumber(latest.value - first.value, '₩')}</strong></div>
                            <div class="mini-stat"><span>최고</span><strong>${formatCurrency(high, 'KRW')}</strong></div>
                            <div class="mini-stat"><span>최저</span><strong>${formatCurrency(low, 'KRW')}</strong></div>
                            <div class="mini-stat"><span>환율</span><strong>${formatNumber(combined.exchangeRate, 1)}</strong></div>
                        </div>
                    </div>
                </section>
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">시장 통계</div>
                            <div class="card-subtitle">포트폴리오 구성과 운영 상태를 한 번에 확인합니다.</div>
                        </div>
                    </div>
                    <div class="stat-list">
                        ${metrics.map((metric) => `
                            <div class="stat-row">
                                <div class="stat-row-head">
                                    <span class="row-label">${escapeHtml(metric.label)}</span>
                                    <span class="row-value">${escapeHtml(metric.value)}</span>
                                </div>
                                <div class="progress-track"><span class="progress-fill" style="width:${Math.max(0, Math.min(100, metric.progress)).toFixed(1)}%"></span></div>
                            </div>
                        `).join('')}
                    </div>
                </section>
            </div>
            <div class="stack-grid">
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">운영 제어</div>
                            <div class="card-subtitle">현재 설정과 실행 동작을 빠르게 관리합니다.</div>
                        </div>
                    </div>
                    <div class="summary-grid">
                        <div class="metric-card">
                            <div class="metric-label">전략</div>
                            <div class="metric-value">${escapeHtml((appData.config?.strategy || 'day').toUpperCase())}</div>
                            <div class="metric-meta">자동 전략 ${appData.config?.auto_strategy ? 'ON' : 'OFF'}</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-label">모드</div>
                            <div class="metric-value">${escapeHtml((appData.config?.trading_mode || 'safe').toUpperCase())}</div>
                            <div class="metric-meta">페르소나 ${(appData.config?.persona || 'neutral').toUpperCase()}</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-label">실시간 제어</div>
                            <div class="metric-value">${appData.status?.botRunning ? 'LIVE' : 'STOP'}</div>
                            <div class="metric-meta">마지막 업데이트 ${escapeHtml(appData.status?.lastUpdate || '-')}</div>
                        </div>
                    </div>
                    <div class="hero-actions" style="margin-top:18px; justify-content:flex-start;">
                        <button type="button" class="secondary-action" data-route="automation">설정 상세</button>
                        <button type="button" class="ghost-action" data-action="refresh">데이터 동기화</button>
                        <button type="button" class="primary-action" data-action="open-restart-modal">세션 재시작</button>
                    </div>
                </section>
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">Top Stories</div>
                            <div class="card-subtitle">전략, 로그, 운영 이슈를 카드형으로 요약했습니다.</div>
                        </div>
                    </div>
                    <div class="story-list">
                        ${(appData.stories || []).map((story) => `
                            <div class="story-item">
                                <div class="story-head">
                                    <span class="badge ${badgeTone(story.tone)}">${escapeHtml(story.badge)}</span>
                                    <span class="story-meta">${escapeHtml(story.meta)}</span>
                                </div>
                                <div class="story-title">${escapeHtml(story.title)}</div>
                                <div class="story-summary">${escapeHtml(story.summary)}</div>
                            </div>
                        `).join('') || '<div class="empty-state">표시할 이벤트가 없습니다.</div>'}
                    </div>
                </section>
            </div>
        </div>
    `;
}

function renderAllocationCard() {
    const allocation = appData.charts?.allocation || [];
    return `
        <section class="card">
            <div class="card-head">
                <div>
                    <div class="card-title">자산 배분</div>
                    <div class="card-subtitle">보유 비중 상위 종목 기준</div>
                </div>
            </div>
            <div class="stat-list">
                ${allocation.map((item) => `
                    <div class="stat-row">
                        <div class="stat-row-head">
                            <div class="symbol-cell">
                                <span class="symbol-dot" style="background:${escapeHtml(item.color)}"></span>
                                <span class="row-value">${escapeHtml(item.label)}</span>
                            </div>
                            <span class="row-label">${item.share.toFixed(2)}%</span>
                        </div>
                        <div class="progress-track"><span class="progress-fill" style="width:${item.share}%; background:${escapeHtml(item.color)}"></span></div>
                        <div class="table-muted">${escapeHtml(item.name)} · ${formatCurrency(item.value, 'KRW')}</div>
                    </div>
                `).join('') || '<div class="empty-state">배분 데이터를 생성할 수 없습니다.</div>'}
            </div>
        </section>
    `;
}

function renderHoldingsTable(region) {
    const items = getHoldings(region);
    const title = region === 'US' ? '미국 계좌 보유 종목' : '국내 계좌 보유 종목';
    const empty = region === 'US' ? '미국 보유 종목이 없습니다.' : '국내 보유 종목이 없습니다.';
    const currency = region === 'US' ? 'USD' : 'KRW';
    return `
        <section class="card">
            <div class="card-head">
                <div>
                    <div class="card-title">${title}</div>
                    <div class="card-subtitle">행을 클릭하면 상세 정보를 확인할 수 있습니다.</div>
                </div>
                <span class="badge info">${items.length} positions</span>
            </div>
            ${items.length ? `
                <div class="table-wrap">
                    <table class="holdings-table">
                        <thead>
                            <tr>
                                <th>종목</th>
                                <th>수량</th>
                                <th>평단가</th>
                                <th>현재가</th>
                                <th>평가금액</th>
                                <th>손익</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${items.map((item) => `
                                <tr class="holding-row" data-holding-id="${item.id}">
                                    <td>
                                        <div class="symbol-cell">
                                            <span class="symbol-dot" style="background:${escapeHtml(item.accentColor)}"></span>
                                            <div>
                                                <strong>${escapeHtml(item.symbol)}</strong>
                                                <div class="table-muted">${escapeHtml(item.name)}</div>
                                            </div>
                                        </div>
                                    </td>
                                    <td>${formatNumber(item.qty, 0)}</td>
                                    <td>${formatCurrency(item.avgPrice, currency, region === 'US' ? 2 : 0)}</td>
                                    <td>${formatCurrency(item.currentPrice, currency, region === 'US' ? 2 : 0)}</td>
                                    <td>${formatCurrency(region === 'US' ? item.evalAmount : item.evalAmountKrw, currency, region === 'US' ? 2 : 0)}</td>
                                    <td class="${toneClassFromValue(item.profitPct)}">${formatPercent(item.profitPct)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            ` : `<div class="empty-state">${empty}</div>`}
        </section>
    `;
}

function renderPortfolioView() {
    const combined = getCombinedAccount();
    const us = appData.accounts?.us || {};
    const kr = appData.accounts?.kr || {};
    return `
        <div class="stack-grid">
            <div class="summary-grid">
                <div class="metric-card">
                    <div class="metric-label">통합 자산</div>
                    <div class="metric-value">${formatCurrency(combined.totalKrw, 'KRW')}</div>
                    <div class="metric-meta">${formatCurrency(combined.totalUsd, 'USD', 2)}</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">미국 계좌</div>
                    <div class="metric-value">${formatCurrency(us.total_asset_krw || 0, 'KRW')}</div>
                    <div class="metric-meta">${formatSignedNumber(us.profit_krw || 0, '₩')}</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">국내 계좌</div>
                    <div class="metric-value">${formatCurrency(kr.total_asset_krw || 0, 'KRW')}</div>
                    <div class="metric-meta">${formatSignedNumber(kr.profit_krw || 0, '₩')}</div>
                </div>
            </div>
            <div class="content-grid">
                <div class="stack-grid">
                    ${renderAllocationCard()}
                </div>
                <div class="stack-grid">
                    ${renderHoldingsTable('US')}
                    ${renderHoldingsTable('KR')}
                </div>
            </div>
        </div>
    `;
}

function renderSignalsView() {
    const signals = appData.signals || [];
    const timeline = appData.history?.strategy || [];
    return `
        <div class="stack-grid">
            <section class="card">
                <div class="card-head">
                    <div>
                        <div class="card-title">실시간 감시 시그널</div>
                        <div class="card-subtitle">보유/감시 종목의 추세 강도와 목표값을 시각화했습니다.</div>
                    </div>
                    <span class="badge accent">${signals.length} tracked</span>
                </div>
                <div class="signal-grid">
                    ${signals.map((signal) => `
                        <article class="signal-card">
                            <div class="signal-head">
                                <div>
                                    <span class="badge ${signal.strength >= 60 ? 'accent' : 'warning'}">${escapeHtml(signal.region)}</span>
                                    <div class="row-value" style="margin-top:8px;">${escapeHtml(signal.symbol)}</div>
                                    <div class="signal-name">${escapeHtml(signal.name)}</div>
                                </div>
                                <div class="badge ${signal.owned ? 'positive' : 'info'}">${signal.owned ? 'OWNED' : 'WATCH'}</div>
                            </div>
                            <div class="signal-values">
                                <div class="signal-value"><span>현재가</span><strong>${signal.current ? formatNumber(signal.current, signal.region === 'US' ? 2 : 0) : '-'}</strong></div>
                                <div class="signal-value"><span>MA20</span><strong>${signal.ma20 ? formatNumber(signal.ma20, signal.region === 'US' ? 2 : 0) : '-'}</strong></div>
                                <div class="signal-value"><span>목표가</span><strong>${signal.target ? formatNumber(signal.target, signal.region === 'US' ? 2 : 0) : '-'}</strong></div>
                            </div>
                            <div class="stat-row">
                                <div class="stat-row-head">
                                    <span class="row-label">강도 ${signal.strength}</span>
                                    <span class="row-value ${signal.deltaPct >= 0 ? 'positive-text' : 'negative-text'}">${formatPercent(signal.deltaPct)}</span>
                                </div>
                                <div class="progress-track"><span class="progress-fill" style="width:${signal.strength}%; background:${escapeHtml(signal.accentColor)}"></span></div>
                            </div>
                            <div class="table-muted">${escapeHtml(signal.trend)} · ${escapeHtml(signal.action)}</div>
                        </article>
                    `).join('') || '<div class="empty-state">수집된 시그널이 없습니다.</div>'}
                </div>
            </section>
            <section class="card">
                <div class="card-head">
                    <div>
                        <div class="card-title">전략 변경 타임라인</div>
                        <div class="card-subtitle">자동 전략 엔진이 남긴 최근 변경 내역</div>
                    </div>
                </div>
                <div class="preference-list">
                    ${timeline.map((item) => `
                        <div class="timeline-item">
                            <div class="timeline-head">
                                <div>
                                    <div class="timeline-title">${escapeHtml(item.market)} · ${escapeHtml((item.strategy || '-').toUpperCase())} / ${escapeHtml((item.mode || '-').toUpperCase())}</div>
                                    <div class="timeline-meta">${escapeHtml(item.timestamp)}</div>
                                </div>
                                <span class="badge info">confidence ${Number(item.confidence || 0).toFixed(2)}</span>
                            </div>
                            <div class="timeline-text">${escapeHtml(item.reason)}</div>
                        </div>
                    `).join('') || '<div class="empty-state">전략 변경 이력이 없습니다.</div>'}
                </div>
            </section>
        </div>
    `;
}

function renderAutomationView() {
    const config = appData.config || {};
    const dca = appData.preferences?.dca || {};
    const risk = appData.preferences?.risk || {};
    const telegram = appData.preferences?.telegram || {};

    return `
        <div class="content-grid">
            <div class="stack-grid">
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">운영 설정</div>
                            <div class="card-subtitle">클라이언트 라우팅 환경에서도 설정 값을 즉시 반영합니다.</div>
                        </div>
                        <button type="button" class="primary-action" data-action="save-settings">설정 저장</button>
                    </div>
                    <div class="switch-row">
                        <div class="switch-meta">
                            <strong>자동 전략</strong>
                            <span>시장 상황에 따라 전략/모드/페르소나를 자동 조정합니다.</span>
                        </div>
                        <label class="switch">
                            <input type="checkbox" id="autoStrategyInput" ${config.auto_strategy ? 'checked' : ''}>
                            <span class="switch-slider"></span>
                        </label>
                    </div>
                    <div class="form-grid" style="margin-top:16px;">
                        <div class="field">
                            <label for="tradingModeInput">트레이딩 모드</label>
                            <select id="tradingModeInput">
                                <option value="safe" ${config.trading_mode === 'safe' ? 'selected' : ''}>SAFE</option>
                                <option value="risky" ${config.trading_mode === 'risky' ? 'selected' : ''}>RISKY</option>
                            </select>
                        </div>
                        <div class="field">
                            <label for="strategyInput">전략</label>
                            <select id="strategyInput">
                                <option value="day" ${config.strategy === 'day' ? 'selected' : ''}>DAY</option>
                                <option value="swing" ${config.strategy === 'swing' ? 'selected' : ''}>SWING</option>
                                <option value="dca" ${config.strategy === 'dca' ? 'selected' : ''}>DCA</option>
                            </select>
                        </div>
                        <div class="field">
                            <label for="personaInput">페르소나</label>
                            <select id="personaInput">
                                <option value="aggressive" ${config.persona === 'aggressive' ? 'selected' : ''}>AGGRESSIVE</option>
                                <option value="neutral" ${config.persona === 'neutral' ? 'selected' : ''}>NEUTRAL</option>
                                <option value="conservative" ${config.persona === 'conservative' ? 'selected' : ''}>CONSERVATIVE</option>
                            </select>
                        </div>
                        <div class="field">
                            <label for="leverageThresholdInput">레버리지 전환 기준 (KRW)</label>
                            <input id="leverageThresholdInput" type="text" value="${escapeHtml(formatNumber(config.leverage_threshold_krw || 0))}" disabled>
                        </div>
                    </div>
                </section>
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">리스크 관리</div>
                            <div class="card-subtitle">핵심 손실 방어 임계값</div>
                        </div>
                    </div>
                    <div class="preference-list">
                        ${[
                            ['손절 기준', `${formatNumber(risk.stop_loss_pct || 0, 1)}%`],
                            ['트레일링 활성화', `${formatNumber(risk.trailing_stop_activation_pct || 0, 1)}%`],
                            ['트레일링 하락 폭', `${formatNumber(risk.trailing_stop_drop_pct || 0, 1)}%`],
                            ['포트폴리오 낙폭 한도', `${formatNumber(risk.portfolio_drawdown_pct || 0, 1)}%`],
                        ].map(([title, value]) => `
                            <div class="preference-item">
                                <div class="preference-title">${title}</div>
                                <div class="preference-text">${value}</div>
                            </div>
                        `).join('')}
                    </div>
                </section>
            </div>
            <div class="stack-grid">
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">DCA / 알림 설정</div>
                            <div class="card-subtitle">분할 매수 및 텔레그램 전송 조건</div>
                        </div>
                    </div>
                    <div class="preference-list">
                        <div class="preference-item">
                            <div class="preference-title">일일 투자 비율</div>
                            <div class="preference-text">${formatNumber(dca.daily_investment_pct || 0, 1)}%</div>
                        </div>
                        <div class="preference-item">
                            <div class="preference-title">최소 / 최대 투자금</div>
                            <div class="preference-text">${formatCurrency(dca.min_investment_usd || 0, 'USD', 0)} ~ ${formatCurrency(dca.max_investment_usd || 0, 'USD', 0)}</div>
                        </div>
                        <div class="preference-item">
                            <div class="preference-title">텔레그램 리포트</div>
                            <div class="preference-text">${telegram.enabled ? `매일 ${telegram.daily_report_hour || 0}:00` : '비활성화'}</div>
                        </div>
                    </div>
                </section>
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">빠른 실행</div>
                            <div class="card-subtitle">운영 컨트롤을 별도 페이지 이동 없이 수행합니다.</div>
                        </div>
                    </div>
                    <div class="hero-actions" style="justify-content:flex-start;">
                        <button type="button" class="secondary-action" data-action="refresh">데이터 새로고침</button>
                        <button type="button" class="ghost-action" data-action="toggle-notifications">알림 센터</button>
                        <button type="button" class="primary-action" data-action="open-restart-modal">봇 재시작</button>
                    </div>
                </section>
            </div>
        </div>
    `;
}

function renderLogsView() {
    const logs = appData.logs || [];
    const alerts = appData.alerts || [];
    return `
        <div class="content-grid">
            <div class="stack-grid">
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">시스템 로그</div>
                            <div class="card-subtitle">최근 40개 로그 엔트리를 터미널 스타일로 제공합니다.</div>
                        </div>
                        <button type="button" class="secondary-action" data-action="refresh">새로고침</button>
                    </div>
                    <div class="log-terminal">
                        <div class="log-list">
                            ${logs.map((log) => {
                                const levelClass = String(log.level || '').toLowerCase();
                                return `
                                    <div class="log-item">
                                        <span class="log-time">${escapeHtml(log.timestamp)}</span>
                                        <span class="log-level ${levelClass}">${escapeHtml(log.level)}</span>
                                        <span class="log-message">${escapeHtml(log.message)}</span>
                                    </div>
                                `;
                            }).join('') || '<div class="empty-state">로그가 없습니다.</div>'}
                        </div>
                    </div>
                </section>
            </div>
            <div class="stack-grid">
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">운영 알림</div>
                            <div class="card-subtitle">상태 카드와 연동된 핵심 알림 목록</div>
                        </div>
                    </div>
                    <div class="alert-list">
                        ${alerts.map((alert) => `
                            <div class="alert-item">
                                <div class="alert-head">
                                    <div class="alert-title">${escapeHtml(alert.title)}</div>
                                    <span class="badge ${badgeTone(alert.severity)}">${escapeHtml(alert.value)}</span>
                                </div>
                                <div class="alert-text">${escapeHtml(alert.text)}</div>
                            </div>
                        `).join('')}
                    </div>
                </section>
                <section class="card">
                    <div class="card-head">
                        <div>
                            <div class="card-title">수익 기록</div>
                            <div class="card-subtitle">실현손익 히스토리 요약</div>
                        </div>
                    </div>
                    ${(appData.history?.profitDays || []).length ? `
                        <div class="table-wrap">
                            <table class="data-table">
                                <thead>
                                    <tr>
                                        <th>날짜</th>
                                        <th>시장</th>
                                        <th>실현손익</th>
                                        <th>거래수</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${(appData.history?.profitDays || []).map((day) => `
                                        <tr>
                                            <td>${escapeHtml(day.date)}</td>
                                            <td>${escapeHtml(day.market)}</td>
                                            <td class="${toneClassFromValue(day.realizedProfit)}">${formatSignedNumber(day.realizedProfit)}</td>
                                            <td>${formatNumber(day.tradeCount)}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state">실현손익 기록 파일이 아직 생성되지 않았습니다.</div>'}
                </section>
            </div>
        </div>
    `;
}

function renderView() {
    const target = document.getElementById('viewContent');
    if (!target) return;

    let html = '';
    if (uiState.currentView === 'overview') html = renderOverviewView();
    if (uiState.currentView === 'portfolio') html = renderPortfolioView();
    if (uiState.currentView === 'signals') html = renderSignalsView();
    if (uiState.currentView === 'automation') html = renderAutomationView();
    if (uiState.currentView === 'logs') html = renderLogsView();

    target.innerHTML = html;
}

function renderNotificationDrawer() {
    const drawer = document.getElementById('notificationDrawer');
    const list = document.getElementById('notificationList');
    if (!drawer || !list) return;
    drawer.classList.toggle('hidden', !uiState.drawerOpen);
    drawer.setAttribute('aria-hidden', String(!uiState.drawerOpen));
    list.innerHTML = [...(appData.alerts || []), ...(appData.stories || [])].map((item) => {
        const tone = badgeTone(item.severity || item.tone || 'info');
        const title = item.title || item.badge || '알림';
        const summary = item.text || item.summary || '';
        const meta = item.value || item.meta || '';
        return `
            <div class="alert-item">
                <div class="alert-head">
                    <div class="alert-title">${escapeHtml(title)}</div>
                    <span class="badge ${tone}">${escapeHtml(meta)}</span>
                </div>
                <div class="alert-text">${escapeHtml(summary)}</div>
            </div>
        `;
    }).join('');
}

function updateOverlayState() {
    const backdrop = document.getElementById('overlayBackdrop');
    if (!backdrop) return;
    const shouldShow = uiState.drawerOpen || Boolean(uiState.activeModal);
    backdrop.classList.toggle('hidden', !shouldShow);
}

function openModal(id) {
    closeModal(uiState.activeModal);
    const modal = document.getElementById(id);
    if (!modal) return;
    uiState.activeModal = id;
    modal.classList.remove('hidden');
    modal.setAttribute('aria-hidden', 'false');
    updateOverlayState();
}

function closeModal(id) {
    if (!id) return;
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.add('hidden');
        modal.setAttribute('aria-hidden', 'true');
    }
    if (uiState.activeModal === id) uiState.activeModal = null;
    updateOverlayState();
}

function closeAllOverlays() {
    uiState.drawerOpen = false;
    renderNotificationDrawer();
    if (uiState.activeModal) closeModal(uiState.activeModal);
    updateOverlayState();
}

function openHoldingModal(holdingId) {
    const holding = getHoldings().find((item) => item.id === holdingId);
    if (!holding) return;
    uiState.selectedHoldingId = holdingId;
    const title = document.getElementById('holdingModalTitle');
    const subtitle = document.getElementById('holdingModalSubtitle');
    const body = document.getElementById('holdingModalBody');
    const currency = holding.region === 'US' ? 'USD' : 'KRW';

    if (title) title.textContent = `${holding.symbol} · ${holding.name}`;
    if (subtitle) subtitle.textContent = `${holding.region} 계좌 포지션 상세`;
    if (body) {
        body.innerHTML = `
            <div class="summary-grid">
                <div class="metric-card">
                    <div class="metric-label">보유 수량</div>
                    <div class="metric-value">${formatNumber(holding.qty, 0)}</div>
                    <div class="metric-meta">평단가 ${formatCurrency(holding.avgPrice, currency, holding.region === 'US' ? 2 : 0)}</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">현재가</div>
                    <div class="metric-value">${formatCurrency(holding.currentPrice, currency, holding.region === 'US' ? 2 : 0)}</div>
                    <div class="metric-meta">평가금액 ${formatCurrency(holding.region === 'US' ? holding.evalAmount : holding.evalAmountKrw, currency, holding.region === 'US' ? 2 : 0)}</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">손익률</div>
                    <div class="metric-value ${toneClassFromValue(holding.profitPct)}">${formatPercent(holding.profitPct)}</div>
                    <div class="metric-meta">손익 ${holding.region === 'US' ? formatSignedNumber(holding.profit, '$', 2) : formatSignedNumber(holding.profit, '₩')}</div>
                </div>
            </div>
            <div class="story-item" style="margin-top:18px;">
                <div class="story-head">
                    <span class="badge info">${holding.region}</span>
                    <span class="story-meta">${formatCurrency(holding.evalAmountKrw, 'KRW')}</span>
                </div>
                <div class="story-summary">이 모달은 design.json의 카드 표면, 라운드, 섀도우 규칙을 그대로 사용합니다. 행 단위 클릭으로 열리며, 컨테이너에만 깊이 효과를 적용합니다.</div>
            </div>
        `;
    }
    openModal('holdingModal');
}

function toast(message, tone = 'accent') {
    const stack = document.getElementById('toastStack');
    if (!stack) return;
    const node = document.createElement('div');
    node.className = `toast ${tone}`;
    node.textContent = message;
    stack.appendChild(node);
    window.setTimeout(() => {
        node.remove();
    }, 3400);
}

async function refreshDashboard(force = false, silent = false) {
    try {
        const response = await fetch(`/api/dashboard-data${force ? '?force=true' : ''}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        appData = await response.json();
        applyTheme(appData.theme?.current || uiState.themeMode, true);
        renderApp();
        if (!silent) {
            toast(force ? '실시간 데이터를 다시 동기화했습니다.' : '화면 데이터를 갱신했습니다.', 'positive');
        }
    } catch (error) {
        console.error(error);
        toast('대시보드 데이터를 불러오지 못했습니다.', 'negative');
    }
}

async function saveSettings() {
    const payload = {
        auto_strategy: document.getElementById('autoStrategyInput')?.checked || false,
        trading_mode: document.getElementById('tradingModeInput')?.value || 'safe',
        strategy: document.getElementById('strategyInput')?.value || 'day',
        persona: document.getElementById('personaInput')?.value || 'neutral',
        theme_mode: uiState.themeMode,
    };

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json();
        appData.config = result.config;
        toast('설정을 저장했습니다. 필요 시 봇을 재시작하세요.', 'positive');
        await refreshDashboard(false, true);
    } catch (error) {
        console.error(error);
        toast('설정 저장에 실패했습니다.', 'negative');
    }
}

async function saveThemeMode(themeMode) {
    const nextTheme = normalizeTheme(themeMode);
    applyTheme(nextTheme, true);

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ theme_mode: nextTheme }),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json();
        appData.config = result.config;
        appData.theme = { ...(appData.theme || {}), current: nextTheme, available: ['light', 'dark'] };
        renderApp();
        toast(nextTheme === 'dark' ? '야간 모드를 적용했습니다.' : '주간 모드를 적용했습니다.', 'positive');
    } catch (error) {
        console.error(error);
        toast('테마 저장에 실패했습니다.', 'negative');
    }
}

async function restartBot() {
    try {
        const response = await fetch('/api/restart', { method: 'POST' });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.error || `HTTP ${response.status}`);
        }
        closeModal('restartModal');
        toast('봇 재시작 명령을 전송했습니다.', 'positive');
        window.setTimeout(() => {
            refreshDashboard(true, true);
        }, 1800);
    } catch (error) {
        console.error(error);
        toast('봇 재시작에 실패했습니다.', 'negative');
    }
}

function renderApp() {
    applyTheme(appData.theme?.current || uiState.themeMode, true);
    setDocumentTitle();
    renderSidebar();
    renderWalletPanel();
    renderTopbar();
    renderHero();
    renderRouteTabs();
    renderView();
    renderNotificationDrawer();
    updateOverlayState();
}

function handleClick(event) {
    const routeTrigger = event.target.closest('[data-route]');
    if (routeTrigger) {
        navigate(routeTrigger.dataset.route);
        return;
    }

    const rangeTrigger = event.target.closest('[data-range]');
    if (rangeTrigger) {
        uiState.chartRange = rangeTrigger.dataset.range;
        renderView();
        return;
    }

    const modalCloseTrigger = event.target.closest('[data-close-modal]');
    if (modalCloseTrigger) {
        closeModal(modalCloseTrigger.dataset.closeModal);
        return;
    }

    const holdingTrigger = event.target.closest('[data-holding-id]');
    if (holdingTrigger) {
        openHoldingModal(holdingTrigger.dataset.holdingId);
        return;
    }

    const actionTrigger = event.target.closest('[data-action]');
    if (!actionTrigger) return;

    const action = actionTrigger.dataset.action;
    if (action === 'refresh') {
        refreshDashboard(true, false);
    }
    if (action === 'toggle-notifications') {
        uiState.drawerOpen = !uiState.drawerOpen;
        renderNotificationDrawer();
        updateOverlayState();
    }
    if (action === 'open-restart-modal') {
        openModal('restartModal');
    }
    if (action === 'confirm-restart') {
        restartBot();
    }
    if (action === 'save-settings') {
        saveSettings();
    }
    if (action === 'toggle-theme') {
        saveThemeMode(uiState.themeMode === 'dark' ? 'light' : 'dark');
    }
}

function handleKeydown(event) {
    if (event.key === 'Escape') {
        closeAllOverlays();
    }
}

function bindEvents() {
    document.addEventListener('click', handleClick);
    document.addEventListener('keydown', handleKeydown);
    window.addEventListener('popstate', () => {
        const route = normalizeView(window.location.pathname.replace(/^\//, '') || 'overview');
        uiState.currentView = route;
        renderApp();
    });

    const backdrop = document.getElementById('overlayBackdrop');
    if (backdrop) {
        backdrop.addEventListener('click', closeAllOverlays);
    }
}

function init() {
    const storedTheme = window.localStorage.getItem('us-etf-sniper-theme');
    applyTheme(storedTheme || appData.theme?.current || uiState.themeMode, true);

    if (window.location.pathname === '/') {
        navigate(uiState.currentView, true);
    } else {
        uiState.currentView = normalizeView(window.location.pathname.replace(/^\//, '') || 'overview');
        renderApp();
    }

    bindEvents();
    window.setInterval(() => refreshDashboard(false, true), 30000);
}

window.addEventListener('DOMContentLoaded', init);
