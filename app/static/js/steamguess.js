'use strict';

const app = document.getElementById('sg-app');
const countInput = document.getElementById('sg-count');
const colsInput = document.getElementById('sg-cols');
const maxHoursInput = document.getElementById('sg-maxhours');
const rollBtn = document.getElementById('sg-roll-btn');

// ── Generic picker (genre / rating / duration checkbox lists) ──────────────

const PICKER_DEFAULT_VISIBLE = 50;

function createPicker({ searchEl, listEl, chipsEl }) {
  let allItems = [];
  const selected = new Set();

  const renderList = (query) => {
    const q = query.toLowerCase();
    let filtered = allItems.filter((t) => (t.label || t.name).toLowerCase().includes(q));
    const truncated = !query && filtered.length > PICKER_DEFAULT_VISIBLE;
    if (!query) filtered = filtered.slice(0, PICKER_DEFAULT_VISIBLE);
    listEl.innerHTML = '';
    if (filtered.length === 0) {
      listEl.innerHTML = '<div class="px-2 py-1.5 text-xs text-slate-500">Sin resultados</div>';
      return;
    }
    for (const item of filtered) {
      const label = item.label || item.name;
      const row = document.createElement('label');
      row.className =
        'flex items-center justify-between gap-2 px-1.5 py-1 text-xs rounded cursor-pointer hover:bg-steam-panel text-slate-300';
      row.innerHTML = `
        <span class="flex items-center gap-2 min-w-0">
          <input type="checkbox" class="shrink-0 rounded border-steam-line bg-steam-bg text-steam-accent focus:ring-steam-accent focus:ring-offset-0" ${selected.has(item.name) ? 'checked' : ''}>
          <span class="truncate">${esc(label)}</span>
        </span>
        <span class="text-slate-500 shrink-0">${item.count}</span>
      `;
      row.querySelector('input').addEventListener('change', () => toggle(item.name));
      listEl.appendChild(row);
    }
    if (truncated) {
      const hint = document.createElement('div');
      hint.className = 'px-1.5 py-1 text-[11px] text-slate-500 italic';
      hint.textContent = `Mostrando los ${PICKER_DEFAULT_VISIBLE} más frecuentes — escribe para buscar entre el resto`;
      listEl.appendChild(hint);
    }
  };

  const labelFor = (name) => {
    const item = allItems.find((t) => t.name === name);
    return item && item.label ? item.label : name;
  };

  const renderChips = () => {
    chipsEl.innerHTML = '';
    for (const name of selected) {
      const chip = document.createElement('div');
      chip.className = 'flex items-center gap-1 text-[11px] bg-steam-bg border border-steam-line rounded-full pl-2 pr-1 py-0.5 text-slate-200';
      chip.innerHTML = `${esc(labelFor(name))}<button type="button" class="text-slate-500 hover:text-red-400 px-1" title="Quitar">×</button>`;
      chip.querySelector('button').addEventListener('click', () => toggle(name));
      chipsEl.appendChild(chip);
    }
  };

  const toggle = (name) => {
    selected.has(name) ? selected.delete(name) : selected.add(name);
    renderChips();
    renderList(searchEl.value);
  };

  searchEl.addEventListener('input', () => renderList(searchEl.value));

  return {
    load: (items) => {
      allItems = items;
      renderList(searchEl.value);
    },
    getSelected: () => [...selected],
  };
}

const genrePicker = createPicker({
  searchEl: document.getElementById('sg-genre-search'),
  listEl: document.getElementById('sg-genre-list'),
  chipsEl: document.getElementById('sg-genre-chips'),
});

const ratingPicker = createPicker({
  searchEl: document.getElementById('sg-rating-search'),
  listEl: document.getElementById('sg-rating-list'),
  chipsEl: document.getElementById('sg-rating-chips'),
});

const durationPicker = createPicker({
  searchEl: document.getElementById('sg-duration-search'),
  listEl: document.getElementById('sg-duration-list'),
  chipsEl: document.getElementById('sg-duration-chips'),
});

const tagPicker = createPicker({
  searchEl: document.getElementById('sg-tag-search'),
  listEl: document.getElementById('sg-tag-list'),
  chipsEl: document.getElementById('sg-tag-chips'),
});

async function loadGenres() {
  try {
    const res = await fetch('/steamguess/api/genres');
    genrePicker.load(await res.json());
  } catch (_) {}
}

async function loadRatings() {
  try {
    const res = await fetch('/steamguess/api/ratings');
    ratingPicker.load(await res.json());
  } catch (_) {}
}

async function loadDurations() {
  try {
    const res = await fetch('/steamguess/api/durations');
    durationPicker.load(await res.json());
  } catch (_) {}
}

async function loadTags() {
  try {
    const res = await fetch('/steamguess/api/tags');
    tagPicker.load(await res.json());
  } catch (_) {}
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function reviewClass(desc) {
  const d = (desc || '').toLowerCase();
  if (d.includes('overwhelmingly positive')) return 'great';
  if (d.includes('very positive') || d.includes('positive')) return 'good';
  if (d.includes('mostly positive')) return 'ok';
  if (d.includes('mixed')) return 'mixed';
  return 'bad';
}

function formatHours(h) {
  if (h < 0.02) return 'Sin jugar';
  if (h < 1) return `${Math.round(h * 60)}m jugados`;
  return `${h.toFixed(1)}h jugadas`;
}

function metacriticClass(score) {
  if (score >= 75) return 'great';
  if (score >= 50) return 'ok';
  return 'bad';
}

// ── API ───────────────────────────────────────────────────────────────────────

async function fetchRoll(count, maxHours) {
  const params = new URLSearchParams({ count, max_hours: maxHours });
  const genres = genrePicker.getSelected();
  const ratings = ratingPicker.getSelected();
  const durations = durationPicker.getSelected();
  const tags = tagPicker.getSelected();
  if (genres.length) params.set('genres', genres.join(','));
  if (ratings.length) params.set('ratings', ratings.join(','));
  if (durations.length) params.set('durations', durations.join(','));
  if (tags.length) params.set('tags', tags.join(','));
  const res = await fetch('/steamguess/api/roll?' + params.toString());
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Error desconocido');
  return data;
}

// ── Grouping ──────────────────────────────────────────────────────────────────

function groupByGenre(games) {
  const map = new Map();
  for (const game of games) {
    const genre = game.genres && game.genres[0] ? game.genres[0] : 'Otros';
    if (!map.has(genre)) map.set(genre, []);
    map.get(genre).push(game);
  }
  return [...map.entries()].sort((a, b) => {
    const diff = b[1].length - a[1].length;
    return diff !== 0 ? diff : a[0].localeCompare(b[0]);
  });
}

// ── Card ──────────────────────────────────────────────────────────────────────

function createCard(game) {
  const a = document.createElement('a');
  a.className = 'sg-card';
  a.href = game.store_url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.title = game.name;

  const img = document.createElement('img');
  img.className = 'sg-cover';
  img.loading = 'lazy';
  img.src = game.cover_url;
  img.alt = game.name;
  img.onerror = () => {
    img.onerror = null;
    img.src = game.header_url || `https://cdn.akamai.steamstatic.com/steam/apps/${game.appid}/header.jpg`;
    img.style.objectFit = 'contain';
  };

  const info = document.createElement('div');
  info.className = 'sg-info';
  info.innerHTML = `<div class="sg-name">${esc(game.name)}</div><div class="sg-hours">${formatHours(game.hours)}</div>`;

  a.appendChild(img);
  a.appendChild(info);

  const isHLS = game.trailer_url && /\.(m3u8|mpd)/.test(game.trailer_url);
  const microUrl = isHLS
    ? game.trailer_url
        .split('?')[0]
        .replace('video.akamai.steamstatic.com', 'video.fastly.steamstatic.com')
        .replace(/\/[^/]+$/, '/microtrailer.webm')
    : null;

  const buildMeta = () => {
    const meta = document.createElement('div');
    meta.className = 'sg-popup-meta';

    const titleEl = document.createElement('div');
    titleEl.className = 'sg-popup-title';
    titleEl.textContent = game.name;
    meta.appendChild(titleEl);

    if (game.metacritic_score || game.hltb_main) {
      const stats = document.createElement('div');
      stats.className = 'sg-popup-stats';
      if (game.metacritic_score) {
        const mc = document.createElement('span');
        mc.className = `sg-metacritic sg-metacritic--${metacriticClass(game.metacritic_score)}`;
        mc.textContent = game.metacritic_score;
        mc.title = 'Metacritic';
        stats.appendChild(mc);
      }
      if (game.hltb_main) {
        const hltb = document.createElement('span');
        hltb.className = 'sg-hltb';
        hltb.title = 'HowLongToBeat · historia principal';
        hltb.textContent = `⏱ ${game.hltb_main.toFixed(0)}h historia`;
        stats.appendChild(hltb);
      }
      meta.appendChild(stats);
    }

    if (game.tags && game.tags.length > 0) {
      const tagsEl = document.createElement('div');
      tagsEl.className = 'sg-popup-tags';
      game.tags.forEach((tag) => {
        const chip = document.createElement('span');
        chip.className = 'sg-popup-tag';
        chip.textContent = tag;
        tagsEl.appendChild(chip);
      });
      meta.appendChild(tagsEl);
    }

    const footer = document.createElement('div');
    footer.className = 'sg-popup-footer';
    if (game.review_desc && game.review_count > 0) {
      const reviewEl = document.createElement('span');
      reviewEl.className = `sg-review sg-review--${reviewClass(game.review_desc)}`;
      reviewEl.textContent = `${game.review_desc} (${game.review_count.toLocaleString()})`;
      footer.appendChild(reviewEl);
    }
    if (game.release_date) {
      const dateEl = document.createElement('span');
      dateEl.className = 'sg-release';
      dateEl.textContent = game.release_date;
      footer.appendChild(dateEl);
    }
    if (footer.children.length > 0) meta.appendChild(footer);
    return meta;
  };

  let popup = null;
  let hls = null;

  a.addEventListener('mouseenter', () => {
    popup = document.createElement('div');
    popup.className = 'sg-popup';

    if (!game.trailer_url && !microUrl) {
      const noVideo = document.createElement('div');
      noVideo.className = 'sg-no-trailer';
      noVideo.style.backgroundImage = `url('${game.cover_url}')`;
      noVideo.innerHTML = '<span class="sg-no-trailer-label">Sin tráiler</span>';
      popup.appendChild(noVideo);
      popup.appendChild(buildMeta());
      a.appendChild(popup);
      a.classList.add('has-video');
      return;
    }

    const video = document.createElement('video');
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    const badge = document.createElement('div');
    badge.className = 'sg-badge';
    popup.appendChild(badge);

    const setBadge = (type) => {
      if (type === 'micro') {
        badge.className = 'sg-badge micro';
        badge.textContent = 'Micro';
      } else {
        badge.className = 'sg-badge trailer';
        badge.textContent = 'Tráiler';
      }
    };

    const loadTrailer = () => {
      if (!game.trailer_url) return;
      video.removeAttribute('src');
      video.load();
      setBadge('trailer');
      if (isHLS) {
        if (window.Hls && Hls.isSupported()) {
          hls = new Hls({ autoStartLoad: true, startLevel: -1 });
          hls.loadSource(game.trailer_url);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
          video.src = game.trailer_url;
          video.play().catch(() => {});
        }
      } else {
        video.src = game.trailer_url;
        video.play().catch(() => {});
      }
    };

    if (microUrl) {
      setBadge('micro');
      video.src = microUrl;
      video.addEventListener('error', loadTrailer, { once: true });
      video.play().catch(() => {});
    } else {
      loadTrailer();
    }

    popup.appendChild(video);
    popup.appendChild(buildMeta());
    a.appendChild(popup);
    a.classList.add('has-video');
  });

  a.addEventListener('mouseleave', () => {
    if (hls) {
      hls.destroy();
      hls = null;
    }
    if (popup) {
      popup.remove();
      popup = null;
    }
    a.classList.remove('has-video');
  });

  return a;
}

// ── Render ────────────────────────────────────────────────────────────────────

function render(games, cols) {
  app.innerHTML = '';

  if (!games || games.length === 0) {
    app.innerHTML = '<div class="text-center py-24 text-slate-500">No se encontraron juegos con esos filtros.</div>';
    return;
  }

  const groups = groupByGenre(games);

  for (const [genre, genreGames] of groups) {
    const section = document.createElement('section');
    section.className = 'sg-genre-section';

    const header = document.createElement('div');
    header.className = 'sg-genre-header';
    header.innerHTML = `
      <h2 class="sg-genre-title">${esc(genre)}</h2>
      <div class="sg-genre-line"></div>
      <span class="sg-genre-badge">${genreGames.length}</span>
    `;

    const grid = document.createElement('div');
    grid.className = 'sg-grid';
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;

    for (const game of genreGames) grid.appendChild(createCard(game));

    section.appendChild(header);
    section.appendChild(grid);
    app.appendChild(section);
  }
}

function showLoading() {
  app.innerHTML = '<div class="text-center py-24 text-slate-500">Tirando de tu backlog…</div>';
}

function showError(msg) {
  app.innerHTML = `<div class="text-center py-24 text-red-400">Error<pre class="text-xs text-slate-500 mt-2 whitespace-pre-wrap">${esc(msg)}</pre></div>`;
}

// ── Roll ──────────────────────────────────────────────────────────────────────

async function roll() {
  const count = Math.max(1, parseInt(countInput.value) || 20);
  const cols = Math.max(2, Math.min(8, parseInt(colsInput.value) || 5));
  const maxHours = Math.max(0, parseFloat(maxHoursInput.value) || 5);

  showLoading();
  try {
    const games = await fetchRoll(count, maxHours);
    render(games, cols);
  } catch (err) {
    showError(err.message);
  }
}

colsInput.addEventListener('input', () => {
  const cols = Math.max(2, Math.min(8, parseInt(colsInput.value) || 5));
  document.querySelectorAll('.sg-grid').forEach((g) => {
    g.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  });
});

rollBtn.addEventListener('click', roll);

(async () => {
  await Promise.all([loadGenres(), loadRatings(), loadDurations(), loadTags()]);
  roll();
})();
