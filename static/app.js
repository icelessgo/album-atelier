import { renderCatalogue, readCatalogue, renderOrderChoices, renderApprovalPanel } from './catalogue_ui.js';
import { moveLayoutFrame, resizeLayoutFrame } from './layout_geometry.js';
import { cropGeometry, reflowSlots, swapSlots } from './editing.js';
import { AlbumSaver } from './autosave.js';
import { LayoutTarget } from './async_target.js';
import { patchHtml } from './dom_patch.js';

const root = document.getElementById('app');
const uploadInput = document.getElementById('photo-upload');
const toastNode = document.getElementById('toast');

const state = {
  photos: [], projects: [], specs: {}, printProfiles: {}, designFormats: {}, layouts: {}, customLayouts: {}, project: null,
  mode: 'studio', catalogue: null, catalogueEditor: null, offers: {}, crm: null,
  active: 0, activeSlot: null, selected: new Set(), picker: null, layoutEditor: null, cropInspector: null,
  exporting: false, busy: false, curatePhotoId: null, suggestions: null, targetSpreads: 20,
  photosPerSpread: 3, avoidSimilar: true, timestampPhotoCount: 80, preflight: null, proof: null, newProof: null,
  picpeakGalleries: null, picpeakImportName: '', libraryPage: 0, libraryQuery: '', jobs: [], activeJob: null,
};
const layoutTarget = new LayoutTarget();
let draftClientId = sessionStorage.getItem('album-studio-tab-id');
if (!draftClientId) {
  draftClientId = crypto.randomUUID();
  sessionStorage.setItem('album-studio-tab-id', draftClientId);
}
const saver = new AlbumSaver({storage: localStorage, clientId: draftClientId,
  send: (id, body) => api(`/api/projects/${id}`, {method:'PUT',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}),
  changed: () => { const node = document.getElementById('save-state'); if (node) node.innerHTML = saveStatusHtml(); },
  saved: (id, project) => {
    if (state.project?.id !== id) return;
    state.project.revision = project.revision;
    if (!project.active_proof_id) delete state.project.active_proof_id;
    if (!project.approved_proof_id) delete state.project.approved_proof_id;
    if (!project.approval_record) delete state.project.approval_record;
    state.project.history = project.history;
    state.project.approval_history = project.approval_history;
  },
});
const flushSaves = () => saver.flush(state.project?.id);
const libraryPhotos = () => {
  const ids = state.project?.source_current_ids ?? state.project?.source?.photo_ids;
  const allowed = ids ? new Set(ids) : null;
  return state.photos.filter(photo => (!allowed || allowed.has(photo.id)) && photo.source_available !== false);
};
let toastTimer;
let pointerDrag = null;
let layoutDrag = null;
let suppressedSlotClick = null;
const editHistory = { undo: [], redo: [] };
let sliderBefore = null;

const LAYOUT_NAMES = {
  hero: 'Full bleed', portrait_solo: 'Portrait solo', pair: 'Classic pair',
  portrait_pair: 'Portrait pair', portrait_focus: 'Portrait + wide',
  feature: 'Feature story', triptych: 'Three panels',
  portrait_trio: 'Portrait trio', portrait_story: 'Portrait story',
  grid: 'Four-grid', portrait_four: 'Four portraits', mosaic_four: 'Mixed mosaic',
};

const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);
const clone = value => JSON.parse(JSON.stringify(value));
let photoIndexSource, photoIndex;
const photoMap = () => {
  if (photoIndexSource !== state.photos) {
    photoIndexSource = state.photos;
    photoIndex = Object.fromEntries(state.photos.map(photo => [photo.id, photo]));
  }
  return photoIndex;
};
const currentSpread = () => state.project?.spreads?.[state.active];
const layoutLabel = name => state.customLayouts[name]?.name || LAYOUT_NAMES[name] || name;
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const designFormat = () => state.project?.design_format || 'legacy';
const isSaal = () => Boolean(state.saalOffers?.[designFormat()]);
const minimumSpreads = () => isSaal() ? 13 : 10;
const bookName = () => isSaal() ? 'Saal Professional Line' : designFormat().startsWith('gobook_') ? 'GoBook' : 'EDGE';
const isLegacyFormat = () => designFormat() === 'legacy';
const isVerifiedFormat = () => Boolean(state.printProfiles[designFormat()]);
const printSpec = () => Object.values(state.printProfiles[designFormat()] || {})[0] || state.printProfiles.legacy?.wedding_book;
const guideStyle = () => {
  const spec = printSpec();
  if (!spec) return '';
  return `--safe-x:${spec.safe_mm / (spec.width_cm * 10) * 100}%;--safe-y:${spec.safe_mm / (spec.height_cm * 10) * 100}%;--bleed-x:${spec.bleed_mm / (spec.width_cm * 10) * 100}%;--bleed-y:${spec.bleed_mm / (spec.height_cm * 10) * 100}%`;
};
const spreadRatio = () => {
  const format = state.designFormats[designFormat()];
  return format ? format.spread_width_cm / format.spread_height_cm : 69.7 / 25.5;
};
const spreadStyle = () => `aspect-ratio:${spreadRatio()}`;
const euro = amount => `€${amount}`;

function previewEditions() {
  const keys = isSaal() ? [designFormat()] : designFormat().includes('portrait') ?
    ['gobook_portrait_25x35','portrait_30x40'] : ['gobook_landscape_35x25','landscape_40x30'];
  return keys.map(key => {
    const offer = state.offers[key];
    return offer && {key,name:offer.product_label,size:offer.closed_size,
      open:`${offer.open_width_cm}×${offer.open_height_cm} cm`,price:offer.base_eur,available:offer.available,
      detail:offer.product==='gobook'?'Layflat book with GoBox':offer.product==='saal'?
      'Matte photographic paper · acrylic + white leatherette':'Statement book, without a box'};
  }).filter(Boolean);
}

function companionChoices() {
  const offer = state.offers[designFormat()];
  if (!offer || isLegacyFormat() || isSaal()) return [];
  return Object.entries(offer.companions).map(([key,label])=>({key,label,price:offer.companion_eur}));
}

function renderPreviewOffer() {
  if (isSaal()) return `<section class="preview-offer"><span class="eyebrow">Your album</span><h2>${escapeHtml(state.saalOffers[designFormat()].product_label)} · ${euro(state.saalOffers[designFormat()].base_eur)}</h2><p>${escapeHtml(state.saalOffers[designFormat()].closed_size)} · Matte photographic paper · acrylic + white leatherette.</p><p>Review the crops and cover before publishing a private proof. Your photographer arranges payment and places the order with Saal.</p></section>`;
  const selected = previewEditions().find(edition => edition.key === designFormat());
  const quantities = state.project.companion_quantities ||
    {one_mid: state.project.companion_option === 'one_mid' ? 1 : 0,
     two_small: state.project.companion_option === 'two_small' ? 2 : 0};
  const extras = (state.offers[designFormat()]?.companion_eur ?? state.offers.gobook_landscape_35x25?.companion_eur ?? 0) * (quantities.one_mid + quantities.two_small / 2);
  return `<section class="preview-offer"><div class="preview-offer-head"><span class="eyebrow">Compare the finished books</span><h2>One story, two sizes</h2>
    <p>Switch editions to see these photographs at each open-spread ratio. Rebuild the draft after switching if you want the layouts matched to that shape.</p></div>
    <div class="edition-grid">${previewEditions().map(edition => `<button type="button" class="edition-card ${edition.key === designFormat() ? 'active' : ''}" data-action="preview-format" data-format="${edition.key}" ${edition.available===false?'disabled':''} aria-pressed="${edition.key === designFormat()}">
      <span class="edition-book ${edition.name === 'GoBook' ? 'edition-gobook' : 'edition-edge'}"><span class="edition-book-spread" style="aspect-ratio:${state.designFormats[edition.key].spread_width_cm / state.designFormats[edition.key].spread_height_cm}"></span></span>
      <span class="edition-card-copy"><span class="edition-card-top"><strong>${edition.name}</strong><span class="product-price">${euro(edition.price)}</span></span>
      <span class="edition-size">${edition.size} closed · ${edition.open} open</span><span class="edition-detail">${edition.detail}</span></span></button>`).join('')}</div>
    ${companionChoices().length ? `<div class="companion-offer"><div><span class="eyebrow">Optional extra copies</span><h3>Share the album with family</h3><p>Either size or both can be added. Larger copies cost ${euro(companionChoices()[0].price)} each; smaller copies cost ${euro(companionChoices()[0].price)} per pair.</p></div>
      <div class="companion-options">${companionChoices().map(choice => `<label class="companion-option"><span><strong>${choice.label}</strong><small>${choice.key==='two_small'?'Even number of copies':'Any whole number of copies'}</small></span><input type="number" min="0" max="1000" step="${choice.key==='two_small'?2:1}" data-action="studio-companion-quantity" data-companion="${choice.key}" value="${quantities[choice.key]}"></label>`).join('')}</div></div>` : ''}
    <div class="preview-total"><span><strong>${selected.name} ${selected.size}</strong><small> + ${quantities.one_mid} larger, ${quantities.two_small} smaller copies</small></span><span>${euro(selected.price + extras)}</span></div>
    <p class="preview-disclaimer">This local comparison is not a checkout or client approval. Publish a password-protected proof from Studio to collect approval. EDGE interiors use Graphistudio’s general Print & Bind guide; check a printed test spread before a paid order. Complete the cover and ordering in Graphistudio.</p>
  </section>`;
}

function layoutIssue(rects) {
  if (!rects.length || rects.length > 4) return 'Use one to four photo frames.';
  for (let index = 0; index < rects.length; index++) {
    const [x, y, width, height] = rects[index];
    if (![x, y, width, height].every(Number.isFinite) || x < 0 || y < 0 || width < .06 || height < .12 || x + width > 1.000001 || y + height > 1.000001) {
      return `Photo frame ${index + 1} must stay inside the spread and meet the minimum size.`;
    }
    for (let other = index + 1; other < rects.length; other++) {
      const [bx, by, bw, bh] = rects[other];
      if (Math.min(x + width, bx + bw) - Math.max(x, bx) > .0001 && Math.min(y + height, by + bh) - Math.max(y, by) > .0001) {
        return `Photo frames ${index + 1} and ${other + 1} overlap. Move or resize one before saving.`;
      }
    }
  }
  return '';
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let value;
  try { value = await response.json(); } catch { value = {}; }
  if (!response.ok) {
    const error = new Error(value.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return value;
}

function showToast(message) {
  toastNode.textContent = message;
  toastNode.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastNode.classList.remove('show'), 4200);
}

function setBusy(busy) {
  state.busy = busy;
  render();
}

async function refreshState() {
  const data = await api('/api/state');
  state.photos = data.photos.sort((a, b) => (a.sort_time || a.captured_at || a.name).localeCompare(b.sort_time || b.captured_at || b.name));
  state.projects = data.projects;
  state.jobs = data.jobs || [];
  state.specs = data.specs;
  state.printProfiles = data.print_profiles || {};
  state.designFormats = data.design_formats;
  state.saalOffers = data.saal_offers || {};
  state.catalogue = data.catalogue;
  state.offers = data.offers || {};
  state.layouts = data.layouts;
  state.customLayouts = data.custom_layouts || {};
}

let projectLoadGeneration = 0;
async function loadProject(id) {
  const generation = ++projectLoadGeneration;
  layoutTarget.invalidate();
  const project = await api(`/api/projects/${id}`);
  if (generation !== projectLoadGeneration) return;
  sessionStorage.setItem('album-studio-active-project',id);
  state.project = saver.open(project);
  state.libraryPage = 0; state.libraryQuery = '';
  editHistory.undo = [];
  editHistory.redo = [];
  state.selected = new Set(state.project.shortlist ?? state.project.photo_ids ?? []);
  state.active = 0;
  state.activeSlot = null;
  state.picker = null;
  state.layoutEditor = null;
  state.cropInspector = null;
  state.suggestions = null;
  state.targetSpreads = state.project.draft_settings?.target_spreads ?? 20;
  state.photosPerSpread = state.project.draft_settings?.photos_per_spread ?? 3;
  state.avoidSimilar = state.project.draft_settings?.avoid_similar ?? true;
  state.timestampPhotoCount = state.project.draft_settings?.target_photos ?? 80;
  state.preflight = null;
  state.proof = null;
  state.newProof = null;
  refreshProof();
  render();
}

async function createProject(title = 'New album', importJobId = null) {
  const project = await api('/api/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title, import_job_id: importJobId }),
  });
  await refreshState();
  await loadProject(project.id);
  return project;
}

function queueSave() {
  if (state.project) saver.edit(state.project);
}

function saveSelection() {
  if (state.project) { state.project.shortlist = [...state.selected]; queueSave(); }
}

function saveDraftSettings() {
  if (!state.project) return;
  const previous = state.project.draft_settings || {};
  if (previous.target_spreads === state.targetSpreads && previous.photos_per_spread === state.photosPerSpread &&
      previous.avoid_similar === state.avoidSimilar &&
      (previous.target_photos ?? 80) === state.timestampPhotoCount) return;
  state.project.draft_settings = {...previous, target_spreads:state.targetSpreads,
    photos_per_spread:state.photosPerSpread, avoid_similar:state.avoidSimilar,
    target_photos:state.timestampPhotoCount};
  queueSave();
}

function saveStatusHtml() {
  if (!state.project) return '';
  const status = saver.status(state.project.id);
  const labels = {saved:'All changes saved', saving:'Saving…', pending:'Changes waiting to save',
    error:'Changes kept on this PC — save failed', conflict:'Another tab changed this album — PC draft kept'};
  const recoverable = status === 'saved' ? saver.recoverable(state.project.id) : [];
  return `<span role="status" aria-live="polite" title="${escapeHtml(saver.message(state.project.id))}">${labels[status]}</span>
    ${status === 'error' || status === 'conflict' ? `<button class="small-btn" data-action="retry-save">Retry save</button>
      <button class="small-btn" data-action="download-draft">Download my draft</button>
      <button class="small-btn" data-action="use-saved-album">Use saved album</button>` : ''}
    ${recoverable.map(draft => `<button class="small-btn" data-action="recover-draft" data-draft-key="${escapeHtml(draft.key)}">Recover PC draft: ${escapeHtml(draft.title)}</button>`).join('')}`;
}

function captureEdit() {
  return {
    spreads: clone(state.project.spreads), photo_ids: [...state.project.photo_ids],
    design_format: state.project.design_format || 'legacy', companion_option: state.project.companion_option ?? null,
    companion_quantities: clone(state.project.companion_quantities || {one_mid:0,two_small:0}),
    cover_photo_id: state.project.cover_photo_id ?? null,
    cover_crop: clone(state.project.cover_crop || {focus_x:.5,focus_y:.5}),
    order_details: clone(state.project.order_details || {}), active: state.active, activeSlot: state.activeSlot,
  };
}

function rememberEdit(before) {
  if (JSON.stringify(before) === JSON.stringify(captureEdit())) return;
  editHistory.undo.push(before);
  if (editHistory.undo.length > 50) editHistory.undo.shift();
  editHistory.redo = [];
}

function travelEdit(direction) {
  layoutTarget.invalidate();
  const from = editHistory[direction];
  if (!from.length || !state.project || state.busy || state.exporting) return;
  const before = captureEdit();
  const target = from.pop();
  editHistory[direction === 'undo' ? 'redo' : 'undo'].push(before);
  const designChanged = ['spreads', 'photo_ids', 'design_format', 'companion_option', 'companion_quantities', 'cover_photo_id', 'cover_crop']
    .some(key => JSON.stringify(before[key]) !== JSON.stringify(target[key]));
  for (const key of ['spreads', 'photo_ids', 'design_format', 'companion_option', 'companion_quantities', 'cover_photo_id', 'cover_crop', 'order_details']) {
    state.project[key] = clone(target[key]);
  }
  if (designChanged) invalidateApproval();
  state.active = Math.max(0, Math.min(target.active, state.project.spreads.length - 1));
  state.activeSlot = state.project.spreads[state.active]?.slots[target.activeSlot] ? target.activeSlot : null;
  state.preflight = state.proof = state.newProof = state.suggestions = state.cropInspector = null;
  render();
  queueSave();
  showToast(`${direction === 'undo' ? 'Undid' : 'Redid'} album edit${designChanged ? '; any earlier client proof stays revoked' : ''}.`);
}

function editProject(update, { keepApproval = false } = {}) {
  layoutTarget.invalidate();
  if (!state.project) return;
  const before = captureEdit();
  update(state.project);
  rememberEdit(before);
  if (!keepApproval) invalidateApproval();
  state.preflight = null;
  state.proof = null;
  state.newProof = null;
  state.suggestions = null;
  render();
  queueSave();
}

function invalidateApproval() {
  state.project.status = 'draft';
  state.project.product = null;
  delete state.project.approved_at;
}

function newSlot(photoId) {
  return { photo_id: photoId, focus_x: 0.5, focus_y: 0.5 };
}

function renderHeader() {
  const project = state.project;
  return `<header class="topbar" data-key="topbar">
    <div class="brand"><img class="brand-logo" src="/static/brand/album-atelier.svg" alt="Album Atelier" width="120" height="62"><div class="brand-copy"><span class="brand-name">Album Studio</span><span class="brand-sub">Private album workspace</span></div></div>
    <div class="top-actions">
      ${project ? `<div class="mode-switch" aria-label="Preview mode">
        <button type="button" data-action="mode-studio" class="${state.mode === 'studio' ? 'active' : ''}">Studio</button>
        <button type="button" data-action="mode-client">Client Preview</button>
      </div>` : ''}
      <button class="small-btn" data-action="open-catalogue">Album catalogue</button>
      <span id="save-state" class="save-state">${saveStatusHtml()}</span>
      <span class="chip chip-local">● This PC only</span>
      ${project && state.mode === 'studio' && isVerifiedFormat() ? `<button class="btn btn-primary" data-action="export" ${!project.spreads.length || state.exporting ? 'disabled' : ''}>${state.exporting ? 'Rendering…' : `Export ${isLegacyFormat() ? '' : bookName() + ' '}print files ↗`}</button>` : ''}
    </div>
  </header>`;
}

function renderWelcome() {
  return `<div class="welcome"><div class="welcome-inner">
    <div class="welcome-copy"><span class="eyebrow">A shorter route from gallery to album</span>
      <h1>A finished first draft, before your client even asks.</h1>
      <p>Import your selected photos, create an editable album, and let your client review it as an EDGE, GoBook or Original Wedding Book. This Studio stays private on your computer.</p>
      <div class="row" style="flex-wrap:wrap; margin-top:26px">
        ${state.photos.length ? '<button class="btn btn-primary btn-large" data-action="new-project">Start an album →</button>' : '<button class="btn btn-primary btn-large" data-action="demo">Try sample album →</button>'}
        <button class="btn btn-secondary btn-large" data-action="upload">Import JPEGs</button>
        <button class="btn btn-secondary btn-large" data-action="picpeak-galleries">Import PicPeak gallery</button>
      </div>
      <p class="small" style="margin-top:24px">The sample uses synthetic images. No photos leave this PC.</p>
    </div>
    <div class="welcome-visual"><div class="album-spread"><div class="placeholder-panel"></div></div><div class="album-spread"><div class="placeholder-panel alt"></div></div></div>
  </div></div>`;
}

function renderPhotoTile(photo, selected, action, draggable = false) {
  return `<div class="photo-item" data-key="photo-${photo.id}"><button type="button" class="photo-tile ${selected ? 'selected' : ''} ${photo.excluded ? 'excluded' : ''}" data-action="${action}" data-photo-id="${photo.id}" ${draggable ? 'draggable="true"' : ''}
    title="${escapeHtml(photo.name)} · ${photo.width}×${photo.height}"><img src="${photo.thumb_url}" alt="${escapeHtml(photo.name)}" loading="lazy"><span class="check">${selected ? '✓' : '+'}</span>${photo.height > photo.width ? '<span class="orientation-badge">Portrait</span>' : ''}<span class="photo-label">${escapeHtml(photo.name)}</span></button>
    ${action === 'toggle-photo' ? `<button class="favorite-button ${photo.favorite ? 'on' : ''}" data-action="favorite" data-photo-id="${photo.id}" title="${photo.favorite ? 'Remove favorite' : 'Mark as must-use favorite'}">★</button>` : ''}</div>`;
}

function renderLibrary() {
  const allPhotos = libraryPhotos();
  const photos = allPhotos.filter(photo => photo.name.toLowerCase().includes(state.libraryQuery.toLowerCase()));
  const pages = Math.max(1, Math.ceil(photos.length / 100));
  state.libraryPage = clamp(state.libraryPage, 0, pages - 1);
  const pagePhotos = photos.slice(state.libraryPage * 100, (state.libraryPage + 1) * 100);
  const curate = state.photos.find(photo => photo.id === state.curatePhotoId);
  return `<aside class="sidebar library" data-key="library-${state.project.id}"><div class="sidebar-head"><div><span class="eyebrow">01 / Curate</span><h2>${state.project.source ? 'Gallery photos' : 'Photo library'}</h2></div><span class="chip chip-local">${photos.length}</span></div>
    <p class="small muted">Select a story set, mark must-use moments ★, and group photos into chapters.</p>
    ${state.project.source ? '<button class="btn btn-secondary full" data-action="refresh-source">Refresh this PicPeak gallery</button><p class="small muted">New proofs include current visible photos only. New proofs follow source-gallery expiry and availability. Refresh original photos before reusing an approval.</p>' : ''}
    <div class="stack photo-tools"><button class="btn btn-secondary full" data-action="upload">＋ Import JPEGs</button>
      <button class="btn btn-secondary full" data-action="picpeak-galleries">＋ Import PicPeak gallery</button>
      <div class="row"><button class="small-btn" data-action="select-all">Select all</button><button class="small-btn" data-action="select-none">Clear</button><button class="small-btn" data-action="select-even">Even sample</button></div>
      <div class="row"><select id="bulk-chapter" class="select"><option value="">Set selected chapter…</option>${['Getting ready','Ceremony','Portraits','Reception','Unsorted'].map(chapter => `<option>${chapter}</option>`).join('')}</select><button class="small-btn" data-action="bulk-chapter">Set</button></div>
      <label class="field-label">Target spreads <input id="target-spreads" type="number" min="1" max="40" value="${state.targetSpreads}"></label>
      <label class="field-label">Photos per spread <select id="photos-per-spread" class="select">${[1,2,3,4].map(n => `<option value="${n}" ${state.photosPerSpread===n?'selected':''}>${n}</option>`).join('')}</select></label>
      <label class="small draft-option"><input id="avoid-similar" type="checkbox" ${state.avoidSimilar?'checked':''}> Skip near-identical burst photos</label>
      <button class="btn btn-primary full" data-action="autobuild" ${!allPhotos.length || state.busy ? 'disabled' : ''}>✦ Build directed draft</button>
      <label class="field-label">Timestamp draft: target photos <input id="timestamp-photo-count" type="number" min="50" max="100" value="${state.timestampPhotoCount}"></label>
      <button class="btn btn-secondary full" data-action="timestamp-draft" ${!allPhotos.length || state.busy ? 'disabled' : ''}>Build from timestamps</button>
      <p class="small muted">Samples this whole ${state.project.source ? 'gallery' : 'library'} across the day, keeping your stars and exclusions. Uses up to three photos per spread. Check camera clocks, the story and crops before publishing.</p>
      ${state.project.timestamp_draft ? `<p class="small muted" role="status">${escapeHtml(timestampDraftSummary(state.project.timestamp_draft))}</p>` : ''}</div>
    ${curate ? `<div class="curate-card" data-key="curate-${curate.id}"><strong>${escapeHtml(curate.name)}</strong><label class="field-label">Story section <select id="photo-chapter" class="select">${['Getting ready','Ceremony','Portraits','Reception','Unsorted'].map(chapter => `<option ${curate.chapter===chapter?'selected':''}>${chapter}</option>`).join('')}</select></label><label class="small"><input type="checkbox" id="photo-excluded" ${curate.excluded?'checked':''}> Keep out of automatic drafts</label></div>` : ''}
    <p class="library-note">Click a photo to select it and edit its chapter. Favorites are always included if the chosen draft has room.</p>
    <label class="field-label">Find photos<input id="library-search" class="input" type="search" value="${escapeHtml(state.libraryQuery)}" placeholder="Search filenames"></label>
    <div class="library-pagination"><button class="small-btn" data-action="library-prev" ${!state.libraryPage?'disabled':''}>Previous photos</button><span>Page ${state.libraryPage+1} of ${pages} · ${photos.length} photos</span><button class="small-btn" data-action="library-next" ${state.libraryPage>=pages-1?'disabled':''}>Next photos</button></div>
    <div class="library-list" id="library-list">${pagePhotos.map(photo => renderPhotoTile(photo, state.selected.has(photo.id), 'toggle-photo', true)).join('')}</div>
  </aside>`;
}

function renderSpread(spread, { compact = false, interactive = true } = {}) {
  if (!spread) return `<div class="album-spread" style="${spreadStyle()}"></div>`;
  const byId = photoMap();
  const rects = state.layouts[spread.layout] || [];
  const slots = rects.map(([x, y, w, h], index) => {
    const slot = spread.slots[index];
    const photo = byId[slot?.photo_id];
    const style = `left:${x * 100}%;top:${y * 100}%;width:${w * 100}%;height:${h * 100}%`;
    const image = photo ? `<img src="${compact ? photo.thumb_url : photo.preview_url}" alt="${escapeHtml(photo.name)}" draggable="false" style="object-position:${(slot.focus_x ?? .5) * 100}% ${(slot.focus_y ?? .5) * 100}%" ${compact ? 'loading="lazy"' : ''}>` : '';
    if (compact || !interactive) return `<div class="album-slot" style="${style}">${image}</div>`;
    return `<button type="button" class="album-slot ${state.activeSlot === index ? 'active' : ''}" style="${style}"
      data-action="slot" data-index="${index}" title="Drag within this frame to reposition; drag onto another frame to swap photos">${image}</button>`;
  }).join('');
  return `<div class="album-spread" style="${spreadStyle()};${guideStyle()}">${slots}<div class="seam-shadow"></div>${!compact && state.mode === 'studio' && isVerifiedFormat() ? '<div class="safe-guide" title="Suggested safe area"></div>' + (isSaal() ? '' : '<div class="bleed-guide" title="Approximate trim/bleed"></div>') : ''}</div>`;
}

function renderFilmstrip() {
  return `<div class="filmstrip" data-key="filmstrip" aria-label="Album spreads">${state.project.spreads.map((spread, index) => `<button type="button" data-key="spread-${spread.id}" data-action="go-spread" data-index="${index}" class="film-card ${index === state.active ? 'active' : ''}" aria-label="Open spread ${index + 1}">
    ${renderSpread(spread, { compact: true, interactive: false })}<span class="film-card-caption">Spread ${String(index + 1).padStart(2, '0')} · pages ${index * 2 + (isSaal()?1:2)}–${index * 2 + (isSaal()?2:3)}</span></button>`).join('')}</div>`;
}

function renderLayoutMini(name) {
  return `<div class="layout-mini" style="${spreadStyle()}">${(state.layouts[name] || []).map(([x, y, w, h]) => `<i style="left:${x * 100}%;top:${y * 100}%;width:${w * 100}%;height:${h * 100}%"></i>`).join('')}</div>`;
}

function renderInspector() {
  const spread = currentSpread();
  const slot = spread?.slots?.[state.activeSlot];
  const photo = slot && photoMap()[slot.photo_id];
  return `<aside class="sidebar inspector"><span class="eyebrow">03 / Refine</span><h2>Spread details</h2>
    ${spread ? `<p class="small muted">Drag within a frame to reposition its crop, or drag a photo onto another frame to swap them. Click once to select; click again to replace.</p>
      <button type="button" class="btn btn-soft full" data-action="suggest-layouts">✦ Find layouts for these photos</button>
      ${state.suggestions?.spreadId === spread.id ? `<div class="layout-suggestions">${state.suggestions.options.map((option, index) => `<button type="button" class="layout-suggestion ${option.layout === spread.layout ? 'active' : ''}" data-action="apply-suggestion" data-index="${index}">${renderLayoutMini(option.layout)}<span>${escapeHtml(layoutLabel(option.layout))}${option.layout === spread.layout ? ' · current' : ''}</span></button>`).join('')}</div>` : ''}
      <button type="button" class="btn btn-secondary full create-layout-button" data-action="new-layout">＋ Create my own layout</button><div class="layout-grid">${Object.keys(state.layouts).map(name => `<button type="button" data-action="layout" data-layout="${name}" class="layout-button ${name === spread.layout ? 'active' : ''}">${renderLayoutMini(name)}<span>${escapeHtml(layoutLabel(name))}</span>${state.customLayouts[name] ? '<small>My layout</small>' : ''}</button>`).join('')}</div>
      <hr class="section-rule"><div class="stack"><button class="btn btn-secondary full" data-action="add-spread">＋ Add a spread</button>
      <div class="row"><button class="small-btn full" data-action="add-photo-to-spread" ${spread.slots.length>=4?'disabled':''}>＋ Photo here</button><button class="small-btn full" data-action="remove-photo-from-spread" ${spread.slots.length<=1||state.activeSlot===null?'disabled':''}>− Selected photo</button></div>
      <div class="row"><button class="small-btn full" data-action="slot-earlier" ${state.activeSlot===null||state.activeSlot===0?'disabled':''}>← Photo earlier</button><button class="small-btn full" data-action="slot-later" ${state.activeSlot===null||state.activeSlot===spread.slots.length-1?'disabled':''}>Photo later →</button></div>
      <div class="row"><button class="small-btn full" data-action="move-left" ${state.active === 0 ? 'disabled' : ''}>← Move earlier</button><button class="small-btn full" data-action="move-right" ${state.active === state.project.spreads.length - 1 ? 'disabled' : ''}>Move later →</button></div>
      <button class="btn btn-danger full" data-action="remove-spread">Remove this spread</button></div>
      <hr class="section-rule">${photo ? `<span class="eyebrow">Selected photo</span><h3>${escapeHtml(photo.name)}</h3>
      <p class="small muted">${photo.width} × ${photo.height} px · drag the image or use the sliders to keep the subject clear of the trim.</p>
      <div class="range-row"><label>Horizontal position <span id="focus-x-value">${Math.round(slot.focus_x * 100)}%</span></label><input type="range" min="0" max="100" value="${Math.round(slot.focus_x * 100)}" data-action="focus-x"></div>
      <div class="range-row"><label>Vertical position <span id="focus-y-value">${Math.round(slot.focus_y * 100)}%</span></label><input type="range" min="0" max="100" value="${Math.round(slot.focus_y * 100)}" data-action="focus-y"></div>
      <div class="row"><button class="btn btn-secondary full" data-action="reset-crop" ${slot.focus_x === 0.5 && slot.focus_y === 0.5 ? 'disabled' : ''}>Reset position</button><button class="btn btn-secondary full" data-action="replace-active">Change photo</button></div>
      <button class="btn btn-soft full" data-action="inspect-crop">Inspect full-resolution crop ↗</button>
      <button class="btn btn-secondary full" data-action="use-as-cover" ${state.project.cover_photo_id===photo.id?'disabled':''}>Use this photo on the cover</button>` : '<p class="small muted">Select a photo on the spread to adjust its crop.</p>'}
      <hr class="section-rule"><div class="hint-card">${isVerifiedFormat() ? 'The dashed line marks the suggested safety margin. Check the supplier hand-off for bleed and trim guidance. Final output is rendered from original photos; inspect crops and the print check before ordering.' : 'This is a format and crop preview. Confirm the exact Graphistudio template and trim guides before preparing print files.'}</div>
    ` : '<p class="small muted">Build a draft to start arranging spreads.</p>'}
  </aside>`;
}

function renderSaalWorkflow() {
  const offer = state.saalOffers[designFormat()], proof = state.proof, checks = state.preflight;
  const coverId = state.project.cover_photo_id || state.project.spreads[0]?.slots[0]?.photo_id;
  const photo = photoMap()[coverId], crop = state.project.cover_crop || {focus_x:.5,focus_y:.5};
  return `<div class="workflow-grid" data-key="workflow-grid">
    <section class="workflow-card" data-key="review-workflow"><span class="eyebrow">04 / Client review</span><h2>${escapeHtml(state.saalOffers[designFormat()].product_label)} · ${euro(state.saalOffers[designFormat()].base_eur)}</h2>
      <p>${escapeHtml(offer.closed_size)} · Matte photographic paper · acrylic + white leatherette · no barcode.</p>
      <p class="small muted">13–40 spreads (26–80 book pages), excluding the cover. The first left and last right halves print inside the covers. Review these as part of the design.</p>
      ${photo ? `<div class="saal-cover-preview" style="aspect-ratio:${offer.cover_ratio}"><img src="${photo.preview_url}" alt="Saal cover crop" style="object-position:${crop.focus_x*100}% ${crop.focus_y*100}%"></div>
        <p class="small muted">Cover crop at the print trim. Bleed is added outside this preview.</p>
        <label class="field-label">Cover left / right<input type="range" min="0" max="100" data-cover-axis="focus_x" value="${Math.round(crop.focus_x*100)}"></label>
        <label class="field-label">Cover up / down<input type="range" min="0" max="100" data-cover-axis="focus_y" value="${Math.round(crop.focus_y*100)}"></label>
        <button class="small-btn" data-action="cover-picker">Choose another cover photo</button>` : ''}
      <p class="small muted">Publish a private proof for this exact size and crop. Changes replace the current review link.</p>
      <label class="field-label">Album review password<input id="proof-password" class="input" type="password" autocomplete="new-password" placeholder="Leave blank to generate one"></label>
      <p class="small muted">This password is separate from the gallery password and is shown once when you publish. If it is lost, choose a new one here and replace the proof link; the old link stops working.</p>
      <button class="btn btn-secondary" data-action="publish-proof" ${state.project.spreads.length<13?'disabled':''}>${proof?'Replace proof link':'Prepare review link'}</button>
      ${galleryPublishButton(13)}
      ${state.project.spreads.length<13?'<p>Add at least 13 spreads before sharing a Saal proof.</p>':''}
      ${state.newProof ? `<div class="proof-share"><label>${state.newProof.published?'Client review link':'Local test link'}<input class="input" readonly value="${escapeHtml(state.newProof.url)}"></label><label>Password — shown once<input class="input" readonly value="${escapeHtml(state.newProof.password)}"></label><p>Share the published link and password separately.</p></div>`:''}
      ${proof ? `<div class="requests"><strong>${proof.approved?'Client approved · Saal Professional Line':`${proof.feedback.length} change requests`}</strong>${proof.feedback.map(item=>`<p>${escapeHtml(item.message)}</p>`).join('')}</div>`:''}
      <button class="small-btn" data-action="refresh-proof" ${!state.project.active_proof_id?'disabled':''}>Refresh client responses</button>
      ${proof?.client_edits_pending?`<p>Client edits${proof.shape_review_required ? `, including a new ${escapeHtml(proof.offer.closed_size)} book shape,` : ''} need your review, then a revised proof.</p><button class="btn btn-secondary" data-action="apply-client-layout">Apply client edits to Studio</button>`:''}
      ${proof?.approved && state.project.approved_proof_id!==proof.id?'<button class="btn btn-primary" data-action="adopt-approval">Confirm approved proof for print</button>':''}
    </section>
    <section class="workflow-card" data-key="print-workflow"><span class="eyebrow">05 / Print check</span><h2>Check the Saal print files</h2>
      <p>Fetch the cover and selected originals, then check sharpness, colour profiles and crops.</p>
      <button class="btn btn-secondary" data-action="fetch-picpeak-originals" ${state.busy?'disabled':''}>Fetch selected originals from R2</button>
      <button class="btn btn-secondary" data-action="preflight" ${!state.project.spreads.length?'disabled':''}>Run print check</button>
      ${checks?`<div class="check-list"><strong>${checks.errors.length} blocking issues · ${checks.issues.length} review notes</strong>${checks.errors.map(message=>`<p class="check-error">${escapeHtml(message)}</p>`).join('')}${checks.issues.slice(0,12).map(item=>`<p>${escapeHtml(item.message)}</p>`).join('')}</div>`:''}
    </section>
    <section class="workflow-card" data-key="order-workflow"><span class="eyebrow">06 / Saal Digital</span><h2>Export and order with Saal</h2>
      <p>Export the ZIP above, extract it, then upload the separate cover and interior PDFs in Saal’s online shop. Each interior PDF page is one complete spread. Numbered JPEGs are included for files that exceed the shop’s 200 MB limit.</p>
      <p>The ${euro(offer.base_eur)} client price is separate from Saal’s manufacturing and delivery charges. You arrange payment and place the order after reviewing Saal’s preview.</p>
      <p class="small muted">This first profile supports standard Professional Line, matte paper and acrylic + white leatherette only. Validate a printed sample before a paid client order.</p>
      <a href="https://www.saal-digital.eu/service/professional-zone/upload-your-pdf-in-the-online-shop/" target="_blank" rel="noopener noreferrer">Open Saal’s upload guide ↗</a>
    </section></div>`;
}

function renderWorkflowPanel() {
  if (isSaal()) return renderSaalWorkflow();
  if (!isVerifiedFormat()) return `<div class="workflow-grid" data-key="workflow-grid"><section class="workflow-card"><span class="eyebrow">Format comparison</span><h2>${escapeHtml(state.designFormats[designFormat()]?.label || '')}</h2><p class="small muted">Choose a supported book format to prepare a client proof and print files.</p></section></div>`;
  const proof = state.proof;
  const checks = state.preflight;
  const details = state.project.order_details || {};
  const isEdge = designFormat() === 'portrait_30x40' || designFormat() === 'landscape_40x30';
  const coverId = state.project.cover_photo_id || state.project.spreads[0]?.slots[0]?.photo_id;
  const coverPhoto = state.photos.find(photo => photo.id === coverId);
  return `<div class="workflow-grid" data-key="workflow-grid"><section class="workflow-card" data-key="review-workflow"><span class="eyebrow">04 / Client review</span><h2>Prepare a private proof</h2>
    <p class="small muted">Publish a frozen copy for review. ${isLegacyFormat() ? 'Clients can compare GoBook and Original Wedding Book.' : 'Clients can compare GoBook and EDGE side by side, approve one, and choose companion copies.'} Edits here replace the old link.</p>
    ${coverPhoto ? `<div class="cover-choice"><img src="${coverPhoto.thumb_url}" alt="Selected cover photo" loading="lazy"><div><strong>Cover photograph</strong><span>${escapeHtml(coverPhoto.name)}</span><button class="small-btn" data-action="cover-picker">Choose another photo</button></div></div><p class="small muted">The client mockup uses this image on both books. Changing it replaces any published proof.</p>` : ''}
    <label class="field-label">Album review password <input id="proof-password" class="input" type="password" autocomplete="new-password" placeholder="Leave blank to generate one"></label>
    <p class="small muted">Separate from the gallery password. It is shown once when publishing. If it is lost, choose a new one and replace the proof link; the old link stops working.</p>
    <button class="btn btn-secondary" data-action="publish-proof" ${state.project.spreads.length < 10?'disabled':''}>${proof?'Replace proof link':'Prepare review link'}</button>
    ${galleryPublishButton(10)}
    ${state.project.spreads.length < 10 ? '<p class="small muted">Add at least 10 spreads before preparing a client proof.</p>' : ''}
    ${state.newProof ? `<div class="proof-share"><label>${state.newProof.published ? 'Client review link' : 'Local test link'} <input class="input" readonly value="${escapeHtml(state.newProof.url)}"></label>
      <label>Password — shown once <input class="input" readonly value="${escapeHtml(state.newProof.password)}"></label><p class="small muted">${state.newProof.published ? 'Share the link and password separately with your client.' : 'The Pi publisher is not configured. Do not send this local link to a client.'}</p></div>` : ''}
    ${proof ? `<div class="requests"><strong>Review status: ${proof.approved ? `Approved · ${proof.approved.product === 'gobook'?'GoBook':proof.approved.product === 'edge'?'EDGE':'Original Wedding Book'}` : `${proof.feedback.length} change request${proof.feedback.length===1?'':'s'}`}</strong>
      ${proof.payment?.status === 'paid' ? '<p class="small">Stripe test payment received. Review delivery details in the private proof record before ordering.</p>' : proof.payment?.status === 'pending' ? '<p class="small">Stripe test checkout started; payment is not yet confirmed.</p>' : ''}
      ${proof.feedback.map(item => `<p class="small">Spread ${proof.spreads.findIndex(spread=>spread.id===item.spread_id)+1}: ${escapeHtml(item.message)}${item.photo_id ? ` · suggested ${escapeHtml(state.photos.find(photo=>photo.id===item.photo_id)?.name || '')}`:''}</p>`).join('')}</div>` : ''}
    <button class="small-btn" data-action="refresh-proof" ${!state.project.active_proof_id?'disabled':''}>Refresh client responses</button>
    ${proof?.client_edits_pending ? `<div class="notice"><strong>Client album edits are ready.</strong>${proof.shape_review_required ? `<p>Requested book shape: ${escapeHtml(proof.offer.closed_size)}. Applying this draft also changes the Studio book shape.</p>` : ''}<p>Apply their draft, then review the book shape, cover photo, spreads and crops in Studio. The current link will be replaced; fetch any newly used originals and run print checks before publishing a revised proof.</p></div><button class="btn btn-secondary full" data-action="apply-client-layout">Apply client edits to Studio</button>` : ''}
    ${proof?.approved && state.project.approved_proof_id !== proof.id ? '<button class="btn btn-primary full" data-action="adopt-approval">Confirm approved proof for print</button>' : ''}</section>
    <section class="workflow-card" data-key="print-workflow"><span class="eyebrow">05 / Print check</span><h2>${isLegacyFormat() ? 'Check both formats' : `Check this ${designFormat().startsWith('gobook_') ? 'GoBook' : 'EDGE'}`}</h2>
      <p class="small muted">Check source resolution, embedded colour profiles, missing originals and fold placement before export.</p>
      ${state.project.spreads.some(spread => spread.slots.some(slot => photoMap()[slot.photo_id]?.picpeak_photo_id)) ? `<button class="btn btn-secondary" data-action="fetch-picpeak-originals" ${state.busy?'disabled':''}>Fetch selected originals from R2</button><p class="small muted">Only photos placed in this draft are downloaded to this PC for full-resolution inspection and print export.</p>` : ''}
      <button class="btn btn-secondary" data-action="preflight" ${!state.project.spreads.length?'disabled':''}>Run print check</button>
      ${checks ? `<div class="check-list"><strong>${checks.errors.length} blocking issue${checks.errors.length===1?'':'s'} · ${checks.issues.length} review note${checks.issues.length===1?'':'s'}</strong>
        ${checks.source_profiles?.length ? `<p>Embedded source profiles: ${escapeHtml(checks.source_profiles.join(', '))}</p>` : ''}
        ${checks.errors.map(item=>`<p class="check-error">${escapeHtml(item)}</p>`).join('')}${checks.issues.slice(0,12).map(item=>`<p>Spread ${item.spread} · ${escapeHtml(item.message)}</p>`).join('')}
        ${checks.issues.length>12?`<p>Plus ${checks.issues.length-12} more in the export report.</p>`:''}</div>`:''}</section>
    <section class="workflow-card" data-key="order-workflow"><span class="eyebrow">06 / Graphistudio</span><h2>Order hand-off</h2>
      <p class="small muted">${isLegacyFormat() ? 'Draft ZIPs compare both editions.' : designFormat().startsWith('gobook_') ? 'This ZIP uses Graphistudio’s GoBook interior dimensions and lists selected companion copies.' : 'This ZIP uses the general Print & Bind dimensions for EDGE interiors; check a test spread with Graphistudio before ordering.'} Once approved, the ZIP contains the client's chosen edition and its order details. ${isEdge ? 'Complete the cover and printed edges in Graphistudio’s EDGE designer; no box is planned.' : 'Choose the cover and box in the Graphistudio Trade Area.'}</p>
      ${renderOrderChoices(state.offers[designFormat()], details)}
      <button class="small-btn" data-action="save-order">Save order choices</button></section></div>`;
}

function renderStudio() {
  const project = state.project;
  const spread = currentSpread();
  return `<div class="studio-shell" data-key="studio-${project.id}">${renderLibrary()}<main class="workspace" data-key="workspace">
    ${project.source_issues?.length ? `<div class="notice" data-key="source-warning" role="alert"><strong>Review ${project.source_issues.length} unavailable album photo${project.source_issues.length===1?'':'s'} before sharing.</strong><p>Your layout is preserved. Refresh the source gallery, then replace or remove these photographs:</p>${project.source_issues.map(item=>`<p>${escapeHtml(item.name)} · ${escapeHtml(item.reason)}</p>`).join('')}</div>` : ''}
    <div class="workspace-head" data-key="workspace-head"><div><span class="eyebrow">02 / Design</span><h1>${escapeHtml(project.title)}</h1>
      <p>Build an album with your photos, then compare how its crops feel in a landscape or portrait book.</p></div>
      <span class="step-tag">${project.spreads.length} spreads · ${project.spreads.length * 2} printed sides</span></div>
    <div class="row" data-key="album-controls" style="margin-top:19px; flex-wrap:wrap"><label class="field-label" for="project-select" style="margin:0">Album</label>
      <select id="project-select" class="select" style="width:auto; min-width:180px">${state.projects.map(item => `<option value="${item.id}" ${item.id === project.id ? 'selected' : ''}>${escapeHtml(item.title)}</option>`).join('')}</select>
      <button class="small-btn" data-action="new-project">＋ New album</button>
      <label class="field-label" for="design-format" style="margin:0">Book shape</label>
      <select id="design-format" class="select" style="width:auto">${Object.entries(state.designFormats).map(([key, format]) => `<option value="${key}" ${state.offers[key]?.available===false && designFormat()!==key?'disabled':''} ${designFormat() === key ? 'selected' : ''}>${escapeHtml(format.label)} · open ${format.spread_width_cm}×${format.spread_height_cm}</option>`).join('')}</select>
      <span class="chip ${project.status === 'approved' ? 'chip-approved' : 'chip-draft'}">${project.approved_proof_id ? '✓ Client approved' : project.status === 'approved' ? '✓ Local preview approved' : '● Draft'}</span></div>
    ${spread ? `<div class="canvas-head" data-key="canvas-head"><div><strong>Spread ${state.active + 1} of ${project.spreads.length}</strong><br><span>Pages ${state.active * 2 + (isSaal()?1:2)}–${state.active * 2 + (isSaal()?2:3)} · ${escapeHtml(layoutLabel(spread.layout))}</span></div><span>Drag between frames to swap · drop a library photo on the stage edge to add and reflow</span></div>
      <div class="album-stage" data-key="stage-${spread.id}">${renderSpread(spread)}</div>
      <p class="small muted" style="margin:10px 0 0">${escapeHtml(state.designFormats[designFormat()]?.label || '')} · open spread ${state.designFormats[designFormat()]?.spread_width_cm}×${state.designFormats[designFormat()]?.spread_height_cm} cm. Change the shape above to compare the same photos; rebuild the draft to reselect layouts for that shape.</p>
      <div class="spread-controls" data-key="spread-controls"><div class="row"><button class="btn btn-secondary" data-action="prev" ${state.active === 0 ? 'disabled' : ''}>← Previous</button><button class="btn btn-secondary" data-action="next" ${state.active === project.spreads.length - 1 ? 'disabled' : ''}>Next →</button>
      <button class="btn btn-secondary" data-action="undo" ${!editHistory.undo.length ? 'disabled' : ''} title="Ctrl/Cmd+Z">↶ Undo</button><button class="btn btn-secondary" data-action="redo" ${!editHistory.redo.length ? 'disabled' : ''} title="Ctrl/Cmd+Shift+Z">↷ Redo</button>
      <button class="btn btn-secondary" data-action="add-photo-to-spread" ${spread.slots.length >= 4 ? 'disabled' : ''}>＋ Add photo &amp; reflow</button></div>
      <button class="btn btn-soft" data-action="mode-client">Client Preview ↗</button></div>
      ${renderFilmstrip()}` : `<div class="album-stage" style="margin-top:28px"><div class="album-spread" style="${spreadStyle()};display:grid; place-items:center; text-align:center; padding:30px"><div><span class="eyebrow">Ready when you are</span><h2>Make the first draft in one click.</h2><p class="small muted">Select photos on the left, then choose Build draft.</p><button class="btn btn-primary" data-action="autobuild">✦ Build draft</button></div></div></div>`}
    ${renderWorkflowPanel()}${renderApprovalPanel(project, state.crm)}${renderJobs()}
  </main>${renderInspector()}</div>`;
}

function renderProof() {
  const project = state.project;
  const spread = currentSpread();
  const approved = project.status === 'approved';
  const formatPreview = !isLegacyFormat();
  return `<main class="proof-shell"><div class="proof-hero"><div><span class="eyebrow">Your story, already designed</span><h1>${escapeHtml(project.title)}</h1>
    <p>Explore the ${escapeHtml(state.designFormats[designFormat()]?.label || 'album')} spread by spread. Tap a photo to replace it, or drag it to adjust the crop.</p></div>
    <span class="chip ${approved ? 'chip-approved' : 'chip-draft'}">${project.approved_proof_id ? '✓ Client approved' : approved ? '✓ Local preview approved' : '● Local preview'}</span></div>
    ${spread ? `<div class="album-stage">${renderSpread(spread)}</div>
      <div class="proof-toolbar"><button class="btn btn-secondary" data-action="prev" ${state.active === 0 ? 'disabled' : ''}>← Previous spread</button>
      <span class="small muted">${state.active + 1} / ${project.spreads.length} spreads</span>${state.activeSlot !== null ? '<button class="small-btn" data-action="reset-crop">Reset selected photo</button>' : ''}
      <button class="btn btn-secondary" data-action="next" ${state.active === project.spreads.length - 1 ? 'disabled' : ''}>Next spread →</button></div>
      ${renderFilmstrip()}
      <div class="row" style="margin-top:12px"><button class="btn btn-secondary" data-action="add-spread" ${project.spreads.length >= 40 ? 'disabled' : ''}>＋ Add a spread with more photos</button>
      <span class="small muted">Each spread adds two printed sides${formatPreview ? '' : ' · GoBook maximum: 80 sides'}</span></div>` : '<p>No album draft yet. Return to Studio and build one.</p>'}
    ${formatPreview ? renderPreviewOffer() : `<div class="proof-purchase"><div><span class="eyebrow">Choose how to keep it</span><h2>One design. Two beautiful editions.</h2>
      <p>Both editions use your approved photo story. The final material and cover details are checked with Graphistudio before production.</p>
      <div class="hint-card">This editable preview stays on this PC. Publish a protected review link from Studio for real client comments and approval. Checkout and Graphistudio ordering remain manual.</div></div>
      <div><div class="product-options"><button type="button" class="product-option ${project.product === 'gobook' ? 'active' : ''}" data-action="product" data-product="gobook"><div><strong>GoBook</strong><small>35×25 cm · book and presentation box</small></div><span class="product-price">€300</span></button>
      <button type="button" class="product-option ${project.product === 'wedding_book' ? 'active' : ''}" data-action="product" data-product="wedding_book"><div><strong>Original Wedding Book</strong><small>45×35 cm · larger heirloom edition</small></div><span class="product-price">€500</span></button></div>
      ${approved ? `<div class="approved-panel" style="margin-top:13px">✓ This version is ${project.approved_proof_id ? 'client approved' : 'preview approved'} for the ${project.product === 'gobook' ? 'GoBook' : 'Original Wedding Book'}. Checkout is not connected.</div>` : `<button class="btn btn-primary btn-large full" style="margin-top:13px" data-action="approve" ${!project.product || project.spreads.length < 10 ? 'disabled' : ''}>Approve local preview →</button>
      <div class="approval-note">${project.spreads.length < 10 ? 'At least 10 spreads (20 sides) are needed for a GoBook proof.' : 'This local action is for previewing; publish a protected review link for client approval.'}</div>`}</div></div>`}
  </main>`;
}

function renderPickerGrid() {
  if (!state.picker) return '';
  const query = state.picker.query.toLowerCase();
  const allowed = state.mode === 'client' || state.picker.type === 'cover' ? new Set(state.project.photo_ids) : null;
  const photos = libraryPhotos().filter(photo => (!allowed || allowed.has(photo.id)) && photo.name.toLowerCase().includes(query));
  return photos.map(photo => renderPhotoTile(photo, state.picker.selected.has(photo.id), 'pick-photo')).join('');
}

function renderPicker() {
  const picker = state.picker;
  if (!picker) return '';
  const add = picker.type === 'add';
  const addToSpread = picker.type === 'add-to-spread';
  const cover = picker.type === 'cover';
  return `<div class="modal-backdrop" data-action="close-picker"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="picker-title">
    <div class="modal-head"><div><span class="eyebrow">${cover ? 'The first impression' : add || addToSpread ? 'Extend the story' : 'Choose a different moment'}</span><h2 id="picker-title">${cover ? 'Choose the cover photograph' : add ? 'Add a new spread' : addToSpread ? 'Add a photo to this spread' : 'Replace this photo'}</h2></div><button class="btn btn-ghost btn-icon" data-action="close-picker" aria-label="Close">×</button></div>
    <div class="modal-controls"><input class="input" id="picker-search" type="search" placeholder="Find a photo by filename…" value="${escapeHtml(picker.query)}"><span class="chip chip-local">${state.mode === 'client' || cover ? state.project.photo_ids.length : libraryPhotos().length} photos</span></div>
    <div id="picker-grid" class="picker-grid">${renderPickerGrid()}</div>
    <div class="modal-foot"><p>${cover ? 'Choose one album photo. The preview shows an illustrative crop and finish.' : add ? `${picker.selected.size} selected · choose 1–4 photos for one spread.` : 'Tap a photo to place it here.'}</p>
      ${add ? `<button class="btn btn-primary" data-action="create-spread" ${!picker.selected.size || picker.selected.size > 4 ? 'disabled' : ''}>Add spread →</button>` : ''}</div>
  </div></div>`;
}

function renderLayoutEditor() {
  const editor = state.layoutEditor;
  if (!editor) return '';
  const selected = editor.rects[editor.selected];
  const issue = layoutIssue(editor.rects);
  const field = (label, index) => `<label>${label}<input type="number" min="0" max="100" step="0.5" data-layout-field="${index}" value="${Number((selected[index] * 100).toFixed(1))}"><span>%</span></label>`;
  return `<div class="modal-backdrop" data-action="close-layout-editor"><div class="modal layout-editor-modal" role="dialog" aria-modal="true" aria-labelledby="layout-editor-title">
    <div class="modal-head"><div><span class="eyebrow">Your reusable template</span><h2 id="layout-editor-title">Design a spread layout</h2></div><button type="button" class="btn btn-ghost btn-icon" data-action="close-layout-editor" aria-label="Close layout editor">×</button></div>
    <div class="layout-editor-body"><div class="layout-editor-top"><label class="layout-name-label">Layout name<input class="input" id="layout-name" type="text" maxlength="50" value="${escapeHtml(editor.name)}" placeholder="e.g. Portraits and details"></label>
      <label class="layout-auto-label"><input type="checkbox" id="layout-auto-build" ${editor.autoBuild ? 'checked' : ''}><span>Offer this layout to the automatic builder</span></label></div>
      <p class="small muted">Drag a numbered frame to move it or its lower-right corner to resize. Edges snap to nearby frames and stop before overlapping. The centre line marks the fold; the dotted line is the approximate print-safe area.</p>
      <div class="layout-editor-canvas" id="layout-editor-canvas" style="${spreadStyle()};${guideStyle()}">${editor.rects.map(([x, y, w, h], index) => `<button type="button" class="editor-frame ${index === editor.selected ? 'active' : ''}" data-action="select-editor-frame" data-index="${index}" style="left:${x * 100}%;top:${y * 100}%;width:${w * 100}%;height:${h * 100}%"><span>Photo ${index + 1}</span><span class="editor-handle" aria-hidden="true"></span></button>`).join('')}${isVerifiedFormat() ? '<div class="editor-safe-guide"></div>' : ''}</div>
      <div class="layout-editor-tools"><div class="layout-preset-row"><span class="small muted">Start from:</span>${[['hero', 'One'], ['pair', 'Two'], ['feature', 'Three'], ['grid', 'Four']].map(([name, title]) => `<button type="button" class="small-btn" data-action="editor-preset" data-layout="${name}">${title} photo${title === 'One' ? '' : 's'}</button>`).join('')}</div>
        <div class="row"><button type="button" class="small-btn" data-action="editor-add-frame" ${editor.rects.length >= 4 ? 'disabled' : ''}>＋ Split selected frame</button><button type="button" class="small-btn" data-action="editor-remove-frame" ${editor.rects.length <= 1 ? 'disabled' : ''}>Remove selected frame</button></div></div>
      <div class="layout-frame-fields"><strong>Photo frame ${editor.selected + 1}</strong><div class="layout-field-grid">${field('Left', 0)}${field('Top', 1)}${field('Width', 2)}${field('Height', 3)}</div></div>
      <p class="layout-editor-feedback ${issue ? 'error' : ''}" role="status">${issue || 'Ready to save. This layout will scale with the selected spread shape.'}</p>
    </div>
    <div class="modal-foot"><p>Saving creates a new version; existing album designs stay as they are.</p><button type="button" class="btn btn-primary" data-action="save-layout" ${issue || !editor.name.trim() ? 'disabled' : ''}>Save layout and use it →</button></div>
  </div></div>`;
}

function renderCropInspector() {
  const inspector = state.cropInspector;
  const spread = currentSpread();
  const slot = spread?.slots?.[inspector?.index];
  const photo = slot && photoMap()[slot.photo_id];
  const frame = inspector && spread && state.layouts[spread.layout]?.[inspector.index];
  if (!inspector || !photo || !frame) return '';
  const frameRatio = spreadRatio() * frame[2] / frame[3];
  const crop = cropGeometry(photo.width, photo.height, frameRatio, slot.focus_x, slot.focus_y);
  const actual = inspector.zoom === 'actual';
  const width = actual ? `${Math.round(crop.cropWidth)}px` : `min(100%, ${Math.round(frameRatio * 68)}vh, 1100px)`;
  const frameStyle = `width:${width};aspect-ratio:${frameRatio}`;
  const imageStyle = `width:${photo.width / crop.cropWidth * 100}%;height:${photo.height / crop.cropHeight * 100}%;left:-${crop.left / crop.cropWidth * 100}%;top:-${crop.top / crop.cropHeight * 100}%`;
  const crossesFold = frame[0] < .5 && frame[0] + frame[2] > .5;
  const fold = crossesFold ? `<div class="crop-fold" style="left:${(.5 - frame[0]) / frame[2] * 100}%" title="Centre fold"></div>` : '';
  return `<div class="modal-backdrop crop-backdrop" data-action="close-crop"><div class="modal crop-modal" role="dialog" aria-modal="true" aria-labelledby="crop-title">
    <div class="modal-head"><div><span class="eyebrow">Original-image inspection</span><h2 id="crop-title">${escapeHtml(photo.name)}</h2></div><button class="btn btn-ghost btn-icon" data-action="close-crop" aria-label="Close crop inspection">×</button></div>
    <div class="crop-toolbar"><span>${photo.width.toLocaleString()} × ${photo.height.toLocaleString()} source pixels · about ${Math.round(crop.cropWidth).toLocaleString()} × ${Math.round(crop.cropHeight).toLocaleString()} in this crop</span>
      <div class="row"><button class="small-btn ${!actual ? 'active' : ''}" data-action="crop-fit">Fit crop</button><button class="small-btn ${actual ? 'active' : ''}" data-action="crop-actual">100% pixels</button></div></div>
    <div class="crop-scroll"><div class="crop-window" style="${frameStyle}"><img src="/media/original/${photo.id}" alt="Full-resolution crop of ${escapeHtml(photo.name)}" style="${imageStyle}" draggable="false">${fold}</div></div>
    <div class="modal-foot"><p>This uses the original local photo and the current frame position. At 100%, scroll to inspect sharpness; review bleed and final print check separately.</p><button class="btn btn-secondary" data-action="close-crop">Close</button></div>
  </div></div>`;
}

function updateEditorField(target) {
  const editor = state.layoutEditor;
  if (!editor) return;
  const value = Number(target.value) / 100;
  if (!Number.isFinite(value)) return;
  const rect = editor.rects[editor.selected];
  rect[Number(target.dataset.layoutField)] = value;
  rect[2] = clamp(rect[2], .06, 1);
  rect[3] = clamp(rect[3], .12, 1);
  rect[0] = clamp(rect[0], 0, 1 - rect[2]);
  rect[1] = clamp(rect[1], 0, 1 - rect[3]);
  const frame = document.querySelector(`.editor-frame[data-index="${editor.selected}"]`);
  if (frame) {
    frame.style.left = `${rect[0] * 100}%`;
    frame.style.top = `${rect[1] * 100}%`;
    frame.style.width = `${rect[2] * 100}%`;
    frame.style.height = `${rect[3] * 100}%`;
  }
  const issue = layoutIssue(editor.rects);
  const feedback = document.querySelector('.layout-editor-feedback');
  if (feedback) {
    feedback.textContent = issue || 'Ready to save. This layout will scale with the selected spread shape.';
    feedback.classList.toggle('error', !!issue);
  }
  const save = document.querySelector('[data-action="save-layout"]');
  if (save) save.disabled = !!issue || !editor.name.trim();
}

function render() {
  renderJobControl();
  root.inert = state.busy;
  patchHtml(root, renderHeader() + (!state.project ? renderWelcome() : state.mode === 'studio' ? renderStudio() : renderProof()) + renderPicker() + renderLayoutEditor() + renderCropInspector() + renderPicPeakDialog() + renderCatalogue(state.catalogueEditor, state.offers));
  if (state.cropInspector?.zoom === 'actual') {
    const viewport = root.querySelector('.crop-scroll');
    if (viewport) {
      viewport.scrollLeft = Math.max(0, (viewport.scrollWidth - viewport.clientWidth) / 2);
      viewport.scrollTop = Math.max(0, (viewport.scrollHeight - viewport.clientHeight) / 2);
    }
  }
}

function renderPicPeakDialog() {
  if (!state.picpeakGalleries) return '';
  return `<div class="modal-backdrop" data-action="close-picpeak"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="picpeak-title">
    <div class="modal-head"><div><span class="eyebrow">Private gallery import</span><h2 id="picpeak-title">Choose a wedding gallery</h2></div><button class="btn btn-ghost btn-icon" data-action="close-picpeak" aria-label="Close">×</button></div>
    <p>Small previews come directly from your private R2 storage. Choose the best photos here; full-size files are fetched only for the finished draft.</p>
    <label class="field-label">Gallery<select id="picpeak-gallery" class="select">${state.picpeakGalleries.map(item => `<option value="${item.id}">${escapeHtml(item.event_name)} · ${item.photo_count} photos</option>`).join('')}</select></label>
    <div class="modal-foot"><button class="btn btn-secondary" data-action="close-picpeak">Cancel</button><button class="btn btn-primary" data-action="start-picpeak-import" ${!state.picpeakGalleries.length?'disabled':''}>Import previews →</button></div>
  </div></div>`;
}

function renderJobs() {
  const jobs = state.jobs.filter(job => job.kind?.startsWith('picpeak-') || job.project_id === state.project.id || !job.project_id);
  return `<section class="workflow-card export-history" data-key="job-history"><h2>Recent tasks and print files</h2><p class="small muted">Completed print ZIPs stay available after restarting Studio. Each download keeps its saved album revision and checksum.</p><button class="small-btn" data-action="refresh-jobs">Refresh tasks</button>
    ${jobs.slice(0,30).map(job=>`<div class="job-row" data-key="job-${job.id}"><strong>${escapeHtml(job.project_title || job.gallery_name || (job.kind==='export'?'Recovered print export':'PicPeak task'))}</strong><span>${escapeHtml(job.status)}${job.legacy_unversioned?' · legacy export · revision unknown':job.project_revision?` · album revision ${job.project_revision}`:''}</span>
      ${job.status==='ready' && job.kind==='export' ? `<a class="small-btn" href="/download/${job.id}">Download saved ZIP</a><details><summary>File verification</summary><small>SHA-256: ${escapeHtml(job.sha256 || '')}</small></details>`:''}
      ${job.status==='ready' && job.kind==='picpeak-import' ? `<button class="small-btn" data-action="open-imported-album" data-job-id="${job.id}">Start album from this import</button>`:''}
      ${['queued','working','cancelling'].includes(job.status) && job.kind?.startsWith('picpeak-') ? `<button class="small-btn" data-action="cancel-job" data-job-id="${job.id}" ${job.status==='cancelling'?'disabled':''}>Cancel task</button>`:''}
      ${job.status==='failed' && job.kind==='export' && job.project_id===state.project.id ? '<button class="small-btn" data-action="export">Export this album again</button>' : ''}
      ${job.error ? `<p class="small">${escapeHtml(job.error)}</p>`:''}
    </div>`).join('') || '<p class="small muted">No saved tasks yet.</p>'}</section>`;
}

let jobsTimer;
async function refreshJobs() {
  const data = await api('/api/jobs');
  state.jobs = data.jobs;
  render();
  clearTimeout(jobsTimer);
  if (state.jobs.some(job=>['queued','working','checking','cancelling'].includes(job.status))) {
    jobsTimer = setTimeout(() => refreshJobs().catch(error=>showToast(error.message)),2000);
  }
}

function renderJobControl() {
  const node = document.getElementById('job-control');
  if (!node) return;
  node.hidden = !state.activeJob;
  if (state.activeJob) patchHtml(node, `<div class="job-progress" role="status">PicPeak · ${escapeHtml(state.activeJob.status)} · ${state.activeJob.done || 0} of ${state.activeJob.total || 0} photos <button class="small-btn" data-action="cancel-job" data-job-id="${state.activeJob.id}" ${state.activeJob.status==='cancelling'?'disabled':''}>Cancel task</button></div>`);
}

async function waitPicPeakJob(jobId) {
  try {
    while (true) {
      const job = await api(`/api/picpeak/jobs/${jobId}`);
      state.activeJob = job; renderJobControl();
      state.jobs = [job,...state.jobs.filter(value=>value.id!==job.id)];
      if (job.status === 'failed' || job.status === 'cancelled') throw new Error(job.error || 'PicPeak task stopped; you can retry it.');
      if (job.status === 'ready') return job;
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
  } finally { state.activeJob = null; renderJobControl(); }
}

async function refreshSourceGallery() {
  if (!state.project?.source) return;
  setBusy(true);
  try {
    await flushSaves();
    const started = await api('/api/picpeak/import',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({event_id:state.project.source.event_id})});
    await waitPicPeakJob(started.job_id);
    const project = await api(`/api/projects/${state.project.id}/refresh-source`,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:state.project.revision})});
    state.project = project; saver.open(project);
    state.selected = new Set(project.shortlist ?? project.photo_ids);
    await refreshState();
    showToast(project.source_issues?.length ? 'Gallery refreshed. Review unavailable photos already used in this album before sharing.' : 'Gallery refreshed; your layout is preserved.');
  } catch (error) {
    // Even a completed empty source listing is meaningful: expose its removal
    // warnings while retaining the historical album and its crops.
    await refreshState();
    const current = await api(`/api/projects/${state.project.id}`);
    state.project = saver.open(current);
    throw error;
  } finally { setBusy(false); }
}

async function importPicPeakGallery(eventId, title) {
  state.picpeakGalleries = null;
  setBusy(true);
  try {
    const started = await api('/api/picpeak/import', {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({event_id:eventId})});
    const job = await waitPicPeakJob(started.job_id);
    await refreshState();
    await createProject(`${title} album`, started.job_id);
    state.selected = new Set(job.suggested_ids);
    state.targetSpreads = 25;
    state.photosPerSpread = 3;
    saveDraftSettings();
    render();
    showToast(`${job.done} PicPeak previews imported${job.skipped ? `; ${job.skipped} unavailable photos skipped` : ''}. ${job.suggested_ids.length} suggested for your first selection; refine it before building.`);
  } finally { setBusy(false); }
}

async function fetchPicPeakOriginals() {
  if (!state.project?.spreads?.length) return;
  setBusy(true);
  try {
    await flushSaves();
    const started = await api(`/api/projects/${state.project.id}/fetch-originals`,
      {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const job = await waitPicPeakJob(started.job_id);
    state.project = await api(`/api/projects/${state.project.id}`);
    saver.open(state.project);
    state.proof = null;
    await refreshState();
    showToast(`${job.done} print sources checked. Run Print check before publishing.`);
    await runPreflight();
  } finally { setBusy(false); }
}

async function importFiles(files) {
  if (!files.length) return;
  setBusy(true);
  let done = 0;
  const importedIds = [];
  const queue = [...files];
  async function worker() {
    while (queue.length) {
      const file = queue.shift();
      try {
        const photo = await api(`/api/photos?name=${encodeURIComponent(file.name)}`, { method: 'POST', body: file });
        importedIds.push(photo.id);
        done++;
        showToast(`Imported ${done} of ${files.length} photos`);
      } catch (error) { showToast(`${file.name}: ${error.message}`); }
    }
  }
  await Promise.all([worker(), worker()]);
  await refreshState();
  if (!state.project && state.photos.length) await createProject('New album');
  if (state.project) {
    if (!state.project.source) {
      state.selected = new Set([...state.selected, ...importedIds]);
      saveSelection();
    } else showToast('JPEGs imported to the library. This album stays linked to its PicPeak gallery.');
  }
  setBusy(false);
  showToast(`${done} photo${done === 1 ? '' : 's'} imported. Build or refine the draft.`);
}

function timestampDraftSummary(report) {
  const warnings = [];
  if (report.missing_timestamps) warnings.push(`${report.missing_timestamps} missing timestamps`);
  if (report.mixed_offsets) warnings.push('mixed known and unknown camera timezones');
  if (report.identical_timestamps) warnings.push('all capture times are identical');
  if (report.sampling === 'photo_order') warnings.push('used photo-order fallback');
  if (report.selected_count < report.requested_count) warnings.push('fewer eligible photos than requested');
  return `Last timestamp draft: ${report.selected_count} photos from ${report.input_count}${warnings.length ? `; ${warnings.join('; ')}` : ''}. Photographer review required.`;
}

async function autoBuild(fromTimestamps = false) {
  if (!state.project) await createProject('New album');
  if (!libraryPhotos().length) return showToast('Import photos or try the sample album first.');
  if (!fromTimestamps && !state.selected.size) return showToast('Select at least one photo before building a draft.');
  const timestampCount = Number(document.getElementById('timestamp-photo-count')?.value ?? state.timestampPhotoCount);
  if (fromTimestamps && (!Number.isInteger(timestampCount) || timestampCount < 50 || timestampCount > 100))
    return showToast('Choose 50 to 100 photos for the timestamp draft.');
  if (state.project.spreads.length && !confirm('Replace the current draft with a new automatic layout?')) return;
  setBusy(true);
  try {
    await flushSaves();
    const before = captureEdit();
    const project = await api(`/api/projects/${state.project.id}/autobuild`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: state.project.revision,
        photo_ids: fromTimestamps ? libraryPhotos().map(photo => photo.id) : [...state.selected], directed: true,
        ...(fromTimestamps ? {selection_method:'timestamps', target_photos:timestampCount} : {}),
        target_spreads: state.targetSpreads, photos_per_spread: state.photosPerSpread,
        avoid_similar: state.avoidSimilar }),
    });
    state.project = project;
    state.selected = new Set(project.shortlist ?? project.photo_ids);
    state.targetSpreads = project.draft_settings?.target_spreads ?? state.targetSpreads;
    state.photosPerSpread = project.draft_settings?.photos_per_spread ?? state.photosPerSpread;
    state.avoidSimilar = project.draft_settings?.avoid_similar ?? state.avoidSimilar;
    state.timestampPhotoCount = project.draft_settings?.target_photos ?? state.timestampPhotoCount;
    rememberEdit(before);
    saver.open(project);
    state.active = 0;
    state.activeSlot = null;
    await refreshState();
    const skipped = project.draft_settings?.similar_skipped || 0;
    showToast(fromTimestamps ? timestampDraftSummary(project.timestamp_draft) :
      `Draft created with ${project.spreads.length} spreads${skipped ? `; ${skipped} near-identical burst photo${skipped===1?'':'s'} skipped` : ''}. Review the story and crops.`);
  } catch (error) { showToast(error.message); }
  finally { setBusy(false); }
}

async function updatePhoto(photoId, changes) {
  const photo = await api(`/api/photos/${photoId}`, {method:'PUT',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(changes)});
  const index = state.photos.findIndex(item => item.id === photoId);
  if (index >= 0) state.photos = state.photos.map(item => item.id === photoId ? photo : item);
  render();
}

async function refreshProof() {
  if (!state.project?.active_proof_id) return;
  try {
    const result = await api(`/api/projects/${state.project.id}/proof`);
    if (state.project?.id === result.proof?.project_id || state.project?.active_proof_id === result.proof?.id) {
      state.proof = result.proof;
      render();
    }
  } catch(error) { showToast(`Review status: ${error.message}`); }
}

function galleryPublishButton(minimum) {
  if (!state.project.source) return '';
  return `<button class="btn btn-primary" data-action="publish-gallery" ${state.busy || state.project.spreads.length < minimum ? 'disabled' : ''}>Publish in gallery</button><p class="small muted">Shows this reviewed album's cover and frozen price to everyone who has access to its linked wedding gallery. Opening the album still requires its separate review password; share that password with the couple. Replaces the current proof.</p>`;
}

async function publishProof(showInGallery = false) {
  const password = document.getElementById('proof-password')?.value || null;
  setBusy(true);
  try {
    await flushSaves();
    if (state.project.active_proof_id && !confirm('Replace the current review link? The old link will stop working.')) return;
    const result = await api(`/api/projects/${state.project.id}/proof`, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({password, revision:state.project.revision, show_in_gallery:showInGallery})});
    state.newProof = result;
    state.project.active_proof_id = result.id;
    state.project.revision = result.revision;
    state.project.show_in_gallery = result.show_in_gallery;
    saver.open(state.project);
    await refreshProof();
    render();
    showToast(showInGallery && result.published ? 'Published for the couple in their gallery. Save the album password now.' : 'Private review prepared. Save the password now; it will not be shown again.');
  } finally { setBusy(false); }
}

async function runPreflight() {
  await flushSaves();
  state.preflight = await api(`/api/projects/${state.project.id}/preflight`);
  render();
}

async function adjustSpreadPhoto(add, photoId = null) {
  const spread = currentSpread();
  if (!spread) return;
  const target = layoutTarget.capture(state.project, state.active);
  const ids = spread.slots.map(slot => slot.photo_id);
  if (add) ids.push(photoId);
  else ids.splice(state.activeSlot, 1);
  if (!ids.length || ids.length > 4) return;
  const suggestion = await api('/api/layout-suggestion', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({photo_ids:ids, design_format:designFormat()})});
  if (!layoutTarget.matches(target, state.project, state.active)) return;
  editProject(project => {
    project.spreads[target.active].layout = suggestion.layout;
    project.spreads[target.active].slots = reflowSlots(spread.slots, suggestion.photo_ids);
    if (add) project.photo_ids = [...new Set([...project.photo_ids, photoId])];
  });
  state.activeSlot = null;
  state.picker = null;
  render();
}

function openPicker(type, slotIndex = null) {
  state.picker = { type, slotIndex, query: '', selected: new Set(type === 'cover' && state.project.cover_photo_id ? [state.project.cover_photo_id] : []) };
  render();
  document.getElementById('picker-search')?.focus();
}

function replaceSlot(photoId, slotIndex) {
  editProject(project => {
    project.spreads[state.active].slots[slotIndex] = newSlot(photoId);
    project.photo_ids = [...new Set([...project.photo_ids, photoId])];
  });
  state.activeSlot = slotIndex;
  state.picker = null;
  render();
  showToast('Photo replaced. Review its crop before approving.');
}

async function createSpreadFromPicker() {
  const ids = [...state.picker.selected];
  if (!ids.length || ids.length > 4) return showToast('Choose between one and four photos.');
  if (state.project.spreads.length >= 40) return showToast('This prototype allows up to 40 spreads.');
  const target = layoutTarget.capture(state.project, state.active);
  const { layout, photo_ids } = await api('/api/layout-suggestion', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ photo_ids: ids, design_format: designFormat() }),
  });
  if (!layoutTarget.matches(target, state.project, state.active)) return;
  editProject(project => {
    project.spreads.splice(target.active + 1, 0, {
      id: crypto.randomUUID(), layout, slots: photo_ids.map(newSlot),
    });
    project.photo_ids = [...new Set([...project.photo_ids, ...ids])];
  });
  state.active += 1;
  state.activeSlot = null;
  state.picker = null;
  render();
  showToast('New spread added. Check its layout and crops.');
}

function applyLayout(layout) {
  const spread = currentSpread();
  if (!spread || spread.layout === layout) return;
  const count = state.layouts[layout].length;
  const existing = spread.slots.map(slot => slot.photo_id);
  const fallback = state.project.photo_ids.filter(id => !existing.includes(id));
  const slots = spread.slots.slice(0, count);
  while (slots.length < count) slots.push(newSlot(fallback.shift() || existing.at(-1)));
  editProject(project => { project.spreads[state.active] = { ...spread, layout, slots }; });
  state.activeSlot = null;
  render();
}

async function suggestLayouts() {
  const spread = currentSpread();
  if (!spread) return;
  const target = layoutTarget.capture(state.project, state.active);
  const answer = await api('/api/layout-suggestions', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({
      photo_ids:spread.slots.map(slot=>slot.photo_id), design_format:designFormat()})});
  if (!layoutTarget.matches(target, state.project, state.active)) return;
  state.suggestions = {target, spreadId:spread.id, options:answer.options};
  render();
}

function applySuggestedLayout(index) {
  const spread = currentSpread();
  const option = state.suggestions && layoutTarget.matches(state.suggestions.target, state.project, state.active) &&
    state.suggestions.spreadId === spread?.id && state.suggestions.options[index];
  if (!option || !state.layouts[option.layout] || option.photo_ids.length !== spread.slots.length) return;
  const remaining = spread.slots.map(slot => clone(slot));
  const slots = option.photo_ids.map(id => {
    const match = remaining.findIndex(slot => slot.photo_id === id);
    return match < 0 ? newSlot(id) : remaining.splice(match, 1)[0];
  });
  editProject(project => { project.spreads[state.active] = {...spread, layout:option.layout, slots}; });
  state.activeSlot = null;
  render();
}

function openLayoutEditor() {
  const spread = currentSpread();
  if (!spread) return;
  state.picker = null;
  state.layoutEditor = {
    name: `${state.customLayouts[spread.layout] ? 'Variation of' : 'My'} ${layoutLabel(spread.layout)}`,
    rects: clone(state.layouts[spread.layout]), selected: 0, autoBuild: false,
  };
  render();
  document.getElementById('layout-name')?.focus();
}

async function saveCustomLayout() {
  const editor = state.layoutEditor;
  if (!editor || layoutIssue(editor.rects) || !editor.name.trim()) return;
  const target = layoutTarget.capture(state.project, state.active);
  await flushSaves();
  const created = await api('/api/layouts', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: editor.name.trim(), rects: editor.rects, auto_build: editor.autoBuild }),
  });
  await refreshState();
  if (!layoutTarget.matches(target, state.project, state.active)) return;
  state.layoutEditor = null;
  applyLayout(created.id);
  await flushSaves();
  showToast(`“${created.name}” saved and applied. It is available in every album.`);
}

async function startExport() {
  if (state.exporting) return;
  try {
    await flushSaves();
    state.exporting = true;
    render();
    const { job_id } = await api(`/api/projects/${state.project.id}/export`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    showToast(isSaal() ? 'Preparing Saal cover, spreads and PDFs…' : 'Rendering full-resolution album JPEGs…');
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 1800));
      const job = await api(`/api/exports/${job_id}`);
      state.jobs = [job,...state.jobs.filter(value=>value.id!==job.id)];
      if (job.status === 'failed') throw new Error(job.error);
      if (job.status === 'ready') {
        const link = document.createElement('a');
        link.href = `/download/${job_id}`;
        link.download = `album-revision-${job.project_revision}-${job_id.slice(0,8)}.zip`;
        document.body.append(link);
        link.click();
        link.remove();
        const warnings = job.manifest?.warnings?.length || 0;
        showToast(`Print files ready. ${warnings ? `${warnings} warnings are listed in print-check.json.` : 'Check every spread before ordering.'}`);
        break;
      }
    }
  } catch (error) { showToast(`Export failed: ${error.message}`); }
  finally { state.exporting = false; render(); }
}

function setMode(mode) {
  state.mode = mode;
  state.activeSlot = null;
  history.pushState({ mode }, '', mode === 'client' ? '/proof' : '/studio');
  render();
}

async function openClientPreview() {
  if (!state.project) return;
  await flushSaves();
  const result = await api(`/api/projects/${state.project.id}/client-preview`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  location.assign(result.url);
}

document.addEventListener('click', async event => {
  const element = event.target.closest('[data-action]');
  if (!element) return;
  const action = element.dataset.action;
  if (action === 'slot' && element === suppressedSlotClick) {
    suppressedSlotClick = null;
    return;
  }
  if (action === 'close-picker' && event.target.closest('.modal') && element.classList.contains('modal-backdrop')) return;
  if (action === 'close-layout-editor' && event.target.closest('.modal') && element.classList.contains('modal-backdrop')) return;
  if (action === 'close-crop' && event.target.closest('.modal') && element.classList.contains('modal-backdrop')) return;
  try {
    switch (action) {
      case 'refresh-jobs': await refreshJobs(); break;
      case 'refresh-source': await refreshSourceGallery(); break;
      case 'cancel-job': {
        const job = await api(`/api/picpeak/jobs/${element.dataset.jobId}/cancel`,{method:'POST',
          headers:{'Content-Type':'application/json'},body:'{}'});
        if (state.activeJob?.id===job.id) {state.activeJob=job;renderJobControl();}
        else await refreshJobs();
        break;
      }
      case 'open-imported-album': {
        const job = state.jobs.find(value=>value.id===element.dataset.jobId);
        await createProject(`${job.gallery_name || 'PicPeak'} album`,job.id); break;
      }
      case 'recover-draft': {
        if (!confirm('Open this PC draft for review? If the saved album has newer changes, they will be protected.')) break;
        const project = await api(`/api/projects/${state.project.id}`);
        state.project = saver.recover(project.id, element.dataset.draftKey, project);
        state.selected = new Set(state.project.shortlist ?? state.project.photo_ids);
        state.active = 0; layoutTarget.invalidate(); render(); break;
      }
      case 'retry-save': await flushSaves(); break;
      case 'download-draft': {
        const draft = saver.draft(state.project.id);
        if (!draft) break;
        const link = document.createElement('a');
        const url = URL.createObjectURL(new Blob([JSON.stringify(draft, null, 2)], {type:'application/json'}));
        link.href = url; link.download = 'album-PC-draft.json'; link.click(); URL.revokeObjectURL(url);
        break;
      }
      case 'use-saved-album': {
        if (!confirm('Use the saved album and set aside this PC draft? You can download your draft first.')) break;
        const id = state.project.id;
        saver.discard(id); await loadProject(id); break;
      }
      case 'undo': travelEdit('undo'); break;
      case 'redo': travelEdit('redo'); break;
      case 'demo': {
        setBusy(true);
        const album = await api('/api/demo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        await refreshState();
        await loadProject(album.id);
        setBusy(false);
        showToast('Sample album ready. Try replacing a photo or adding a spread.');
        break;
      }
      case 'new-project': {
        const title = prompt('Name this album', 'New wedding album');
        if (title) await createProject(title);
        break;
      }
      case 'upload': uploadInput.click(); break;
      case 'picpeak-galleries': {
        const result = await api('/api/picpeak/galleries');
        state.picpeakGalleries = result.galleries.filter(item => item.photo_count > 0);
        render(); break;
      }
      case 'close-picpeak': state.picpeakGalleries = null; render(); break;
      case 'start-picpeak-import': {
        const id = Number(document.getElementById('picpeak-gallery')?.value);
        const gallery = state.picpeakGalleries.find(item => item.id === id);
        if (gallery) await importPicPeakGallery(id, gallery.event_name);
        break;
      }
      case 'fetch-picpeak-originals': await fetchPicPeakOriginals(); break;
      case 'library-prev': state.libraryPage--; render(); break;
      case 'library-next': state.libraryPage++; render(); break;
      case 'select-all': state.selected = new Set(libraryPhotos().map(photo => photo.id)); saveSelection(); render(); break;
      case 'select-none': state.selected.clear(); saveSelection(); render(); break;
      case 'select-even': {
        const photos = libraryPhotos();
        const count = Math.min(photos.length, 48);
        state.selected = new Set(Array.from({ length: count }, (_, i) => photos[Math.round(i * (photos.length - 1) / Math.max(1, count - 1))].id));
        saveSelection(); render(); break;
      }
      case 'toggle-photo': {
        const id = element.dataset.photoId;
        state.curatePhotoId = id;
        state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
        saveSelection();
        render(); break;
      }
      case 'favorite': {
        const id = element.dataset.photoId;
        const photo = photoMap()[id];
        state.curatePhotoId = id;
        state.selected.add(id);
        saveSelection();
        await updatePhoto(id, {favorite: !photo.favorite});
        break;
      }
      case 'bulk-chapter': {
        const chapter = document.getElementById('bulk-chapter').value;
        if (!chapter || !state.selected.size) return showToast('Choose a chapter and at least one selected photo.');
        const result = await api('/api/photos-batch', {method:'PUT', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({photo_ids:[...state.selected], chapter})});
        const updated = new Map(result.photos.map(photo => [photo.id, photo]));
        state.photos = state.photos.map(photo => updated.get(photo.id) || photo);
        render(); showToast(`${updated.size} photos moved to ${chapter}.`);
        break;
      }
      case 'autobuild': await autoBuild(); break;
      case 'timestamp-draft': await autoBuild(true); break;
      case 'mode-client': await openClientPreview(); break;
      case 'mode-studio': setMode('studio'); break;
      case 'prev': layoutTarget.invalidate(); state.active = Math.max(0, state.active - 1); state.activeSlot = null; render(); break;
      case 'next': layoutTarget.invalidate(); state.active = Math.min(state.project.spreads.length - 1, state.active + 1); state.activeSlot = null; render(); break;
      case 'go-spread': layoutTarget.invalidate(); state.active = Number(element.dataset.index); state.activeSlot = null; render(); break;
      case 'slot': {
        const index = Number(element.dataset.index);
        if (state.mode === 'studio' && state.activeSlot !== index) {
          state.activeSlot = index;
          render();
        } else openPicker('replace', index);
        break;
      }
      case 'replace-active': openPicker('replace', state.activeSlot); break;
      case 'cover-picker': openPicker('cover'); break;
      case 'use-as-cover': {
        const photoId = currentSpread()?.slots[state.activeSlot]?.photo_id;
        if (photoId) { editProject(project => { project.cover_photo_id = photoId; if (isSaal()) project.cover_crop = {focus_x:.5,focus_y:.5}; }); showToast('Cover photo updated. Publish a new proof to show it to clients.'); }
        break;
      }
      case 'inspect-crop': state.cropInspector = { index: state.activeSlot, zoom: 'fit' }; render(); break;
      case 'crop-fit': state.cropInspector.zoom = 'fit'; render(); break;
      case 'crop-actual': state.cropInspector.zoom = 'actual'; render(); break;
      case 'close-crop': state.cropInspector = null; render(); break;
      case 'reset-crop': {
        const slot = currentSpread()?.slots?.[state.activeSlot];
        if (!slot || (slot.focus_x === 0.5 && slot.focus_y === 0.5)) break;
        editProject(project => {
          const selected = project.spreads[state.active].slots[state.activeSlot];
          selected.focus_x = 0.5;
          selected.focus_y = 0.5;
        });
        showToast('Photo position reset to centre.');
        break;
      }
      case 'add-spread': openPicker('add'); break;
      case 'add-photo-to-spread': openPicker('add-to-spread'); break;
      case 'remove-photo-from-spread': await adjustSpreadPhoto(false); break;
      case 'slot-earlier': case 'slot-later': {
        const from = state.activeSlot;
        const to = from + (action === 'slot-earlier' ? -1 : 1);
        if (from === null || to < 0 || to >= currentSpread().slots.length) break;
        editProject(project => {
          const slots = project.spreads[state.active].slots;
          [slots[from], slots[to]] = [slots[to], slots[from]];
        });
        state.activeSlot = to; render(); break;
      }
      case 'close-picker': state.picker = null; render(); break;
      case 'pick-photo': {
        const id = element.dataset.photoId;
        if (state.picker.type === 'replace') replaceSlot(id, state.picker.slotIndex);
        else if (state.picker.type === 'cover') {
          state.picker = null;
          editProject(project => { project.cover_photo_id = id; if (isSaal()) project.cover_crop = {focus_x:.5,focus_y:.5}; });
          showToast('Cover photo updated. Publish a new proof to show it to clients.');
        }
        else if (state.picker.type === 'add-to-spread') await adjustSpreadPhoto(true, id);
        else {
          state.picker.selected.has(id) ? state.picker.selected.delete(id) : state.picker.selected.add(id);
          render();
        }
        break;
      }
      case 'create-spread': await createSpreadFromPicker(); break;
      case 'layout': applyLayout(element.dataset.layout); break;
      case 'suggest-layouts': await suggestLayouts(); break;
      case 'apply-suggestion': applySuggestedLayout(Number(element.dataset.index)); break;
      case 'new-layout': openLayoutEditor(); break;
      case 'close-layout-editor': state.layoutEditor = null; render(); break;
      case 'select-editor-frame': state.layoutEditor.selected = Number(element.dataset.index); render(); break;
      case 'editor-preset': {
        state.layoutEditor.rects = clone(state.layouts[element.dataset.layout]);
        state.layoutEditor.selected = 0;
        render(); break;
      }
      case 'editor-add-frame': {
        const editor = state.layoutEditor;
        if (editor.rects.length >= 4) break;
        const frame = editor.rects[editor.selected];
        const [x, y, width, height] = frame;
        const splitWidth = width >= .14;
        const splitHeight = height >= .26;
        if (!splitWidth && !splitHeight) {
          showToast('Select a larger frame to split, or resize this one first.');
          break;
        }
        const horizontal = splitWidth && (!splitHeight || width * 2.733 >= height);
        const gap = .02;
        if (horizontal) {
          const half = (width - gap) / 2;
          editor.rects[editor.selected] = [x, y, half, height];
          editor.rects.splice(editor.selected + 1, 0, [x + half + gap, y, half, height]);
        } else {
          const half = (height - gap) / 2;
          editor.rects[editor.selected] = [x, y, width, half];
          editor.rects.splice(editor.selected + 1, 0, [x, y + half + gap, width, half]);
        }
        editor.selected += 1;
        render(); break;
      }
      case 'editor-remove-frame': {
        const editor = state.layoutEditor;
        if (editor.rects.length <= 1) break;
        editor.rects.splice(editor.selected, 1);
        editor.selected = Math.min(editor.selected, editor.rects.length - 1);
        render(); break;
      }
      case 'save-layout': await saveCustomLayout(); break;
      case 'move-left': case 'move-right': {
        const from = state.active;
        const to = from + (action === 'move-left' ? -1 : 1);
        if (to < 0 || to >= state.project.spreads.length) break;
        editProject(project => {
          const [spread] = project.spreads.splice(from, 1);
          project.spreads.splice(to, 0, spread);
        });
        state.active = to; render(); break;
      }
      case 'remove-spread': {
        if (!confirm('Remove this spread from the album?')) break;
        editProject(project => { project.spreads.splice(state.active, 1); });
        state.active = Math.min(state.active, state.project.spreads.length - 1);
        state.activeSlot = null;
        render(); break;
      }
      case 'product': {
        if (state.project.product !== element.dataset.product) {
          editProject(project => {
            project.product = element.dataset.product;
            project.status = 'draft';
            delete project.approved_at;
          }, { keepApproval: true });
        }
        break;
      }
      case 'preview-format': {
        const nextFormat = element.dataset.format;
        if (!(nextFormat in state.designFormats) || nextFormat === designFormat()) break;
        if (state.project.active_proof_id && !confirm('Changing the book shape will replace the current client proof link. Continue?')) break;
        editProject(project => { project.design_format = nextFormat; project.cover_crop = {focus_x:.5,focus_y:.5}; project.companion_option = null;
          project.companion_quantities = {one_mid:0,two_small:0}; delete project.active_proof_id; });
        showToast('Book size updated. Rebuild the draft in Studio for layouts matched to this shape.');
        break;
      }
      case 'approve': {
        if (!state.project.product) return showToast('Choose an album edition first.');
        if (state.project.spreads.length < 10) return showToast('An album needs at least 20 printed sides.');
        await flushSaves();
        editProject(project => { project.status = 'approved'; }, { keepApproval: true });
        await flushSaves();
        showToast('Local preview marked approved. Publish a protected link for real client approval.');
        break;
      }
      case 'export': await startExport(); break;
      case 'open-catalogue':
        await flushSaves(); await refreshState();
        state.catalogueEditor = clone(state.catalogue); render(); break;
      case 'close-catalogue': state.catalogueEditor = null; render(); break;
      case 'save-catalogue': {
        const catalogue = readCatalogue(root, state.catalogueEditor);
        await api('/api/catalogue',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(catalogue)});
        state.catalogueEditor = null; await refreshState(); render();
        showToast('Catalogue saved. Existing proofs keep their original offers.'); break;
      }
      case 'check-crm':
        await flushSaves();
        state.crm = await api(`/api/projects/${state.project.id}/crm`); render(); break;
      case 'create-crm-quote': {
        const vat = document.getElementById('crm-vat').value.trim();
        if (!/^\d{1,3}(?:\.\d{1,3})?$/.test(vat)) throw Error('Confirm the VAT rate, including 0 if applicable.');
        const body = {admin_id:Number(document.getElementById('crm-admin').value),
          customer_id:Number(document.getElementById('crm-customer').value),vat_rate:Number(vat)};
        setBusy(true);
        try {
          await flushSaves();
          state.project = await api(`/api/projects/${state.project.id}/crm-quote`, {method:'POST',
            headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,revision:state.project.revision})});
          saver.open(state.project); showToast('CRM draft prepared. Review it in PicPeak before sending.');
        } finally {setBusy(false);}
        break;
      }
      case 'publish-proof': await publishProof(); break;
      case 'publish-gallery': await publishProof(true); break;
      case 'refresh-proof': await refreshProof(); break;
      case 'apply-client-layout': {
        setBusy(true);
        try {
          await flushSaves();
          if (!confirm('Apply the client layout to this Studio album? The current review link will be revoked so you can check it and publish a revised proof.')) break;
          const project = await api(`/api/projects/${state.project.id}/apply-client-layout`, {
            method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:state.project.revision})});
          state.project = project;
          state.proof = null;
          state.newProof = null;
          state.selected = new Set(project.photo_ids);
          saver.open(project);
          render();
          showToast('Client layout applied. Check crops, fetch missing originals, then publish a new proof.');
        } finally { setBusy(false); }
        break;
      }
      case 'adopt-approval': {
        setBusy(true);
        try {
          await flushSaves();
          const project = await api(`/api/projects/${state.project.id}/adopt-approval`, {
            method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:state.project.revision})});
          state.project = project;
          saver.open(project);
          render();
          showToast('Client approval recorded for the selected edition.');
        } finally { setBusy(false); }
        break;
      }
      case 'preflight': await runPreflight(); break;
      case 'save-order': {
        const details = {
          cover: document.getElementById('order-cover').value.trim(),
          box: document.getElementById('order-box').value.trim(),
          finish: document.getElementById('order-finish').value.trim(),
        };
        editProject(project => { project.order_details = details; });
        await flushSaves();
        showToast('Order choices saved.');
        break;
      }
    }
  } catch (error) { state.busy = false; render(); showToast(error.message); }
});

document.addEventListener('input', event => {
  if (event.target.id === 'timestamp-photo-count') {
    const count = Number(event.target.value);
    if (Number.isInteger(count) && count >= 50 && count <= 100) { state.timestampPhotoCount = count; saveDraftSettings(); }
  }
  if (event.target.id === 'target-spreads') {
    const count = Number(event.target.value);
    if (Number.isInteger(count) && count >= 1 && count <= 40) { state.targetSpreads = count; saveDraftSettings(); }
  }
  if (event.target.id === 'library-search') { state.libraryQuery = event.target.value; state.libraryPage = 0; render(); }
  if (event.target.dataset.layoutField !== undefined && state.layoutEditor) {
    updateEditorField(event.target);
  }
  if (event.target.id === 'layout-name' && state.layoutEditor) {
    state.layoutEditor.name = event.target.value;
    const save = document.querySelector('[data-action="save-layout"]');
    if (save) save.disabled = !state.layoutEditor.name.trim() || !!layoutIssue(state.layoutEditor.rects);
  }
  if (event.target.id === 'picker-search' && state.picker) {
    state.picker.query = event.target.value;
    document.getElementById('picker-grid').innerHTML = renderPickerGrid();
  }
  if (event.target.dataset.action?.startsWith('focus-')) {
    const axis = event.target.dataset.action === 'focus-x' ? 'x' : 'y';
    const slot = currentSpread()?.slots?.[state.activeSlot];
    if (!slot) return;
    slot[`focus_${axis}`] = Number(event.target.value) / 100;
    const label = document.getElementById(`focus-${axis}-value`);
    if (label) label.textContent = `${event.target.value}%`;
    const image = document.querySelector(`.album-stage .album-slot:nth-child(${state.activeSlot + 1}) img`);
    if (image) image.style.objectPosition = `${slot.focus_x * 100}% ${slot.focus_y * 100}%`;
  }
});

document.addEventListener('change', async event => {
  if (event.target.dataset.coverAxis && isSaal()) {
    const axis = event.target.dataset.coverAxis, value = Number(event.target.value) / 100;
    if (['focus_x','focus_y'].includes(axis) && Number.isFinite(value) && value >= 0 && value <= 1) {
      editProject(project => { project.cover_crop = {...(project.cover_crop || {focus_x:.5,focus_y:.5}), [axis]:value}; });
    }
  }
  if (event.target.id === 'target-spreads') {
    const count = Number(event.target.value);
    if (!Number.isInteger(count) || count < 1 || count > 40) {showToast('Choose between 1 and 40 spreads.');return;}
    state.targetSpreads = count;
  }
  if (event.target.id === 'photos-per-spread') state.photosPerSpread = Number(event.target.value);
  if (event.target.id === 'avoid-similar') state.avoidSimilar = event.target.checked;
  if (['target-spreads','photos-per-spread','avoid-similar'].includes(event.target.id)) saveDraftSettings();
  if (event.target.id === 'photo-chapter' && state.curatePhotoId) {
    try { await updatePhoto(state.curatePhotoId, {chapter:event.target.value}); }
    catch(error) { showToast(error.message); }
  }
  if (event.target.id === 'photo-excluded' && state.curatePhotoId) {
    try { await updatePhoto(state.curatePhotoId, {excluded:event.target.checked}); }
    catch(error) { showToast(error.message); }
  }
  if (event.target.id === 'layout-auto-build' && state.layoutEditor) {
    state.layoutEditor.autoBuild = event.target.checked;
  }
  if (event.target.dataset.layoutField !== undefined && state.layoutEditor) {
    updateEditorField(event.target);
    render();
  }
  if (event.target.id === 'project-select') {
    try { await loadProject(event.target.value); } catch (error) { showToast(error.message); }
  }
  if (event.target.id === 'design-format' && event.target.value !== designFormat()) {
    if (state.project.active_proof_id && !confirm('Changing the book shape will replace the current client proof link. Continue?')) {
      event.target.value = designFormat();
      return;
    }
    const nextFormat = event.target.value;
    editProject(project => { project.design_format = nextFormat; project.cover_crop = {focus_x:.5,focus_y:.5}; project.companion_option = null;
      project.companion_quantities = {one_mid:0,two_small:0}; delete project.active_proof_id; });
    showToast('Book shape updated. Rebuild the draft if you want layouts matched to this shape.');
  }
  if (event.target.dataset.action === 'studio-companion-quantity') {
    const key = event.target.dataset.companion, quantity = Number(event.target.value);
    if (!['one_mid','two_small'].includes(key) || !Number.isInteger(quantity) || quantity < 0 ||
        quantity > 1000 || (key === 'two_small' && quantity % 2)) {
      showToast('Larger copies need a whole number; smaller copies must be in pairs.'); render(); return;
    }
    editProject(project => {
      const counts = {...(project.companion_quantities || {one_mid:0,two_small:0}),[key]:quantity};
      project.companion_quantities = counts;
      project.companion_option = counts.one_mid===1 && !counts.two_small ? 'one_mid' :
        counts.two_small===2 && !counts.one_mid ? 'two_small' : null;
    });
  }
  if (event.target.dataset.action?.startsWith('focus-') && currentSpread()) {
    if (sliderBefore && JSON.stringify(sliderBefore) !== JSON.stringify(captureEdit())) {
      rememberEdit(sliderBefore);
      invalidateApproval();
      queueSave();
    }
    sliderBefore = null;
    render();
  }
});

document.addEventListener('focusin', event => {
  if (event.target.dataset.action?.startsWith('focus-') && state.project) sliderBefore = captureEdit();
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.cropInspector) {
    state.cropInspector = null;
    render();
    return;
  }
  if (!(event.ctrlKey || event.metaKey) || event.altKey || event.key.toLowerCase() !== 'z' && event.key.toLowerCase() !== 'y') return;
  if (event.target.closest('input, textarea, select, [contenteditable], .modal') || state.mode !== 'studio') return;
  event.preventDefault();
  travelEdit(event.key.toLowerCase() === 'y' || event.shiftKey ? 'redo' : 'undo');
});

document.addEventListener('pointerdown', event => {
  if (event.target.dataset.action?.startsWith('focus-') && state.project) sliderBefore = captureEdit();
  const element = event.target.closest('.album-stage .album-slot');
  if (!element || event.button !== 0 || !event.isPrimary) return;
  const index = Number(element.dataset.index);
  const slot = currentSpread()?.slots?.[index];
  const photo = slot && photoMap()[slot.photo_id];
  if (!photo) return;
  const rect = element.getBoundingClientRect();
  const scale = Math.max(rect.width / photo.width, rect.height / photo.height);
  pointerDrag = {
    element, index, pointerId: event.pointerId, x: event.clientX, y: event.clientY,
    before: captureEdit(), target: null,
    focusX: slot.focus_x ?? 0.5, focusY: slot.focus_y ?? 0.5,
    overflowX: Math.max(0, photo.width * scale - rect.width),
    overflowY: Math.max(0, photo.height * scale - rect.height), moved: false,
  };
  element.setPointerCapture(event.pointerId);
});

document.addEventListener('pointermove', event => {
  const drag = pointerDrag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  const deltaX = event.clientX - drag.x;
  const deltaY = event.clientY - drag.y;
  if (!drag.moved && Math.hypot(deltaX, deltaY) < 4) return;
  drag.moved = true;
  event.preventDefault();
  const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('.album-stage .album-slot');
  if (drag.target && drag.target !== target) drag.target.classList.remove('drag-over');
  drag.target = target && target !== drag.element && state.mode === 'studio' ? target : null;
  if (drag.target) {
    drag.target.classList.add('drag-over');
    drag.element.classList.remove('repositioning');
    const source = currentSpread().slots[drag.index];
    source.focus_x = drag.focusX;
    source.focus_y = drag.focusY;
    const image = drag.element.querySelector('img');
    if (image) image.style.objectPosition = `${drag.focusX * 100}% ${drag.focusY * 100}%`;
    return;
  }
  drag.element.classList.add('repositioning');
  const slot = currentSpread().slots[drag.index];
  slot.focus_x = drag.overflowX ? Math.max(0, Math.min(1, drag.focusX - deltaX / drag.overflowX)) : drag.focusX;
  slot.focus_y = drag.overflowY ? Math.max(0, Math.min(1, drag.focusY - deltaY / drag.overflowY)) : drag.focusY;
  const image = drag.element.querySelector('img');
  if (image) image.style.objectPosition = `${slot.focus_x * 100}% ${slot.focus_y * 100}%`;
});

document.addEventListener('pointerup', event => {
  const drag = pointerDrag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  pointerDrag = null;
  if (!drag.moved) return;
  suppressedSlotClick = drag.element;
  drag.target?.classList.remove('drag-over');
  const slot = currentSpread().slots[drag.index];
  const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('.album-stage .album-slot');
  if (target && target !== drag.element && state.mode === 'studio') {
    slot.focus_x = drag.focusX;
    slot.focus_y = drag.focusY;
    const targetIndex = Number(target.dataset.index);
    editProject(project => {
      project.spreads[state.active].slots = swapSlots(project.spreads[state.active].slots, drag.index, targetIndex);
    });
    state.activeSlot = targetIndex;
    render();
    showToast('Photos swapped. Their crop positions moved with them.');
    return;
  }
  state.activeSlot = drag.index;
  if (slot.focus_x !== drag.focusX || slot.focus_y !== drag.focusY) {
    rememberEdit(drag.before);
    invalidateApproval();
    state.preflight = null;
    queueSave();
  }
  requestAnimationFrame(render);
});

document.addEventListener('pointercancel', event => {
  const drag = pointerDrag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  pointerDrag = null;
  drag.target?.classList.remove('drag-over');
  const slot = currentSpread().slots[drag.index];
  slot.focus_x = drag.focusX;
  slot.focus_y = drag.focusY;
  drag.element.classList.remove('repositioning');
  const image = drag.element.querySelector('img');
  if (image) image.style.objectPosition = `${slot.focus_x * 100}% ${slot.focus_y * 100}%`;
});

document.addEventListener('pointerdown', event => {
  const frame = event.target.closest('.editor-frame');
  if (!frame || !state.layoutEditor || event.button !== 0 || !event.isPrimary) return;
  const index = Number(frame.dataset.index);
  const canvas = document.getElementById('layout-editor-canvas').getBoundingClientRect();
  layoutDrag = {
    element: frame, index, pointerId: event.pointerId,
    x: event.clientX, y: event.clientY,
    canvasWidth: canvas.width, canvasHeight: canvas.height,
    start: [...state.layoutEditor.rects[index]],
    resize: !!event.target.closest('.editor-handle'),
  };
  state.layoutEditor.selected = index;
  document.querySelectorAll('.editor-frame').forEach(item => item.classList.toggle('active', item === frame));
  frame.setPointerCapture(event.pointerId);
});

document.addEventListener('pointermove', event => {
  const drag = layoutDrag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  event.preventDefault();
  const deltaX = event.clientX - drag.x;
  const deltaY = event.clientY - drag.y;
  const others = state.layoutEditor.rects.filter((_, index) => index !== drag.index);
  const fitted = (drag.resize ? resizeLayoutFrame : moveLayoutFrame)(
    drag.start, others, deltaX, deltaY, drag.canvasWidth, drag.canvasHeight);
  state.layoutEditor.rects[drag.index] = fitted.rect;
  drag.element.classList.toggle('snapped', fitted.snapped);
  const fittedRect = fitted.rect;
  drag.element.style.left = `${fittedRect[0] * 100}%`;
  drag.element.style.top = `${fittedRect[1] * 100}%`;
  drag.element.style.width = `${fittedRect[2] * 100}%`;
  drag.element.style.height = `${fittedRect[3] * 100}%`;
});

document.addEventListener('pointerup', event => {
  if (!layoutDrag || event.pointerId !== layoutDrag.pointerId) return;
  layoutDrag = null;
  requestAnimationFrame(render);
});

document.addEventListener('pointercancel', event => {
  const drag = layoutDrag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  state.layoutEditor.rects[drag.index] = drag.start;
  layoutDrag = null;
  render();
});

uploadInput.addEventListener('change', async () => {
  await importFiles([...uploadInput.files]);
  uploadInput.value = '';
});

document.addEventListener('dragstart', event => {
  const tile = event.target.closest('.photo-tile[draggable]');
  if (tile) event.dataTransfer.setData('text/plain', tile.dataset.photoId);
});
document.addEventListener('dragover', event => {
  const slot = event.target.closest('.album-stage .album-slot');
  if (slot && state.mode === 'studio') { event.preventDefault(); slot.classList.add('drag-over'); }
  else if (event.target.closest('.album-stage') && state.mode === 'studio' && currentSpread()?.slots.length < 4) {
    event.preventDefault();
    event.target.closest('.album-stage').classList.add('drag-over');
  }
});
document.addEventListener('dragleave', event => {
  event.target.closest('.album-slot')?.classList.remove('drag-over');
  const stage = event.target.closest('.album-stage');
  if (stage && !stage.contains(event.relatedTarget)) stage.classList.remove('drag-over');
});
document.addEventListener('drop', async event => {
  const slot = event.target.closest('.album-stage .album-slot');
  const stage = event.target.closest('.album-stage');
  if (!stage || state.mode !== 'studio') return;
  event.preventDefault();
  stage.classList.remove('drag-over');
  slot?.classList.remove('drag-over');
  const id = event.dataTransfer.getData('text/plain');
  if (!libraryPhotos().some(photo => photo.id === id)) return;
  if (slot) replaceSlot(id, Number(slot.dataset.index));
  else if (currentSpread()?.slots.length < 4) {
    try { await adjustSpreadPhoto(true, id); showToast('Photo added and spread reflowed. Review the new crops.'); }
    catch (error) { showToast(error.message); }
  }
});

window.addEventListener('popstate', () => {
  state.mode = 'studio';
  render();
});

window.addEventListener('beforeunload', event => {
  if (saver.dirty()) { event.preventDefault(); event.returnValue = ''; }
});
// A duplicated tab can inherit sessionStorage. Storage events are delivered only
// to other tabs, so separate their outboxes before either can replace a draft.
window.addEventListener('storage', event => {
  if (!event.key || ![...saver.entries.keys()].some(id => saver.key(id) === event.key)) return;
  draftClientId = crypto.randomUUID();
  sessionStorage.setItem('album-studio-tab-id', draftClientId);
  try { saver.fork(draftClientId); render(); } catch (error) { showToast(error.message); }
});
window.addEventListener('online', () => { if (state.project) saver.run(state.project.id); });

async function boot() {
  try {
    const oldPreviewLink = location.pathname === '/proof';
    await refreshState();
    if (state.jobs.some(job=>['queued','working','checking','cancelling'].includes(job.status))) refreshJobs().catch(error=>showToast(error.message));
    if (state.projects.length) {
      const remembered = sessionStorage.getItem('album-studio-active-project');
      await loadProject(state.projects.some(project=>project.id===remembered) ? remembered : state.projects[0].id);
    }
    else render();
    if (oldPreviewLink) {
      history.replaceState({mode:'studio'}, '', '/studio');
      if (state.project?.spreads.length) await openClientPreview();
    }
  } catch (error) { root.innerHTML = `<div class="loading">Could not open the prototype: ${escapeHtml(error.message)}</div>`; }
}
boot();
