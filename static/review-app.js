const root = document.getElementById('app');
const base = location.pathname.replace(/\/$/, '');
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const brandHtml = '<div class="brand"><img src="/review/brand/album-atelier.svg" alt="Album Atelier" width="120" height="62"><span>Wedding album review</span></div>';
const saalDescription = 'Every book is assembled using 4 mm acrylic glass, and leatherette. Pages are printed on Fujifilm Crystal Archive HD photographic paper at 368 g/m² and bound with Layflat panoramic binding for seamless double-page spreads.';
const state = {proof: null, active: 0, product: 'gobook', selectedFormat: null,
  companions: {one_mid: 0, two_small: 0},
  step: 'view', viewerExpanded: false, draft: [], dirty: false, selectedSlot: null, adding: false, addPhotos: [],
  coverPhotoId: null, choosingCover: false, changingShape: false,
  galleryQuery: '', libraryLimit: 80};
const clone = value => JSON.parse(JSON.stringify(value));
const currentSpread = () => (state.step === 'edit' ? state.draft : state.proof.spreads)[state.active];
const currentOffer = () => (state.proof.offers || []).find(item => item.design_format === state.selectedFormat) || state.proof.offer;
const priceMinor = offer => offer.base_minor ?? offer.amount_minor ?? Math.round(offer.base_eur * 100);
function shapeChoices() {
  const offer = currentOffer();
  return offer ? (state.proof.shape_offers || []).filter(item => item.product === offer.product &&
    item.currency === offer.currency && priceMinor(item) === priceMinor(offer) &&
    (item.companion_eur || 0) === (offer.companion_eur || 0)) : [];
}
function shapeLabel(offer) {
  const [width, height] = offer.closed_size.split('×').map(parseFloat);
  return width === height ? 'Square' : width > height ? 'Landscape' : 'Portrait';
}
function shapeChoicesHtml() {
  const choices = shapeChoices(), offer = currentOffer();
  if (choices.length < 2) return '';
  const locked = state.proof.approved || ['pending','paid'].includes(state.proof.payment?.status) || state.changingShape;
  return `<fieldset class="book-shapes" aria-describedby="shape-review-note"><legend>Book shape</legend>
    <p>Same ${escapeHtml(offer.product_label)}, same €${offer.base_eur} album price.</p>
    <p class="small" id="shape-review-note">Changing shape affects photo crops. You’ll need to review the cover and every spread again. Your photographer checks the updated design before approval.</p>
    <div class="book-shape-options">${choices.map(item => `<button type="button" data-action="shape" data-format="${item.design_format}" aria-pressed="${state.selectedFormat === item.design_format}" ${locked?'disabled':''}>
      <strong>${shapeLabel(item)}</strong><span>${escapeHtml(item.closed_size)}</span></button>`).join('')}</div>
    ${state.changingShape?'<p role="status">Updating your book shape…</p>':''}</fieldset>`;
}
async function changeShape(designFormat) {
  const chosen = shapeChoices().find(item => item.design_format === designFormat);
  if (!chosen || designFormat === state.selectedFormat || state.changingShape || state.dirty) return;
  const body = {revision:state.proof.layout_revision || 0, spreads:state.proof.spreads,
    cover_photo_id:state.proof.cover_photo_id, from_format:state.selectedFormat, design_format:designFormat};
  state.changingShape = true; render();
  try {
    state.proof = await request('/layout','PUT',body);
    state.selectedFormat = state.proof.design_format;
    state.product = currentOffer().product;
    state.draft = clone(state.proof.spreads);
    state.coverPhotoId = state.proof.cover_photo_id;
    state.active = 0; state.selectedSlot = null; state.step = 'view'; state.viewerExpanded = false;
    window.scrollTo({top:0,behavior:'smooth'});
  } catch(error) { alert(error.message); }
  finally { state.changingShape = false; render(); }
}
const pendingEditsMessage = () => state.proof.shape_review_required ?
  'Your book shape has changed. Review the cover and every spread again: photo crops may be different. Your photographer will check this design and send a revised proof before approval or ordering.' :
  'Your changes are awaiting photographer review. Approval and ordering wait for a revised proof.';
const isSaal = () => currentOffer()?.product === 'saal';
const minimumSpreads = () => currentOffer()?.minimum_spreads || 10;
const companionCounts = value => value || {one_mid: 0, two_small: 0};
const countsFromProof = proof => {
  if (proof.approved?.companion_quantities) return {...proof.approved.companion_quantities};
  if (proof.companion_quantities) return {...proof.companion_quantities};
  const option = proof.approved?.companion_option || proof.companion_option;
  return {one_mid: option === 'one_mid' ? 1 : 0, two_small: option === 'two_small' ? 2 : 0};
};

async function request(path, method = 'GET', body) {
  const response = await fetch(`${base}${path}`, {method, credentials: 'same-origin',
    headers: body ? {'Content-Type':'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  let value = {};
  try { value = await response.json(); } catch { /* Proxies can return an HTML error page. */ }
  if (!response.ok) throw new Error(value.error || (response.status === 413
    ? 'This album is too large to save. Your changes are still here; contact your photographer.'
    : `Could not save or load the album (${response.status}). Please try again.`));
  return value;
}

function login(message = '') {
  const explanation = message === 'Incorrect review password'
    ? 'That is not the album review password. Your gallery password is separate.' : message;
  root.innerHTML = `<main class="login">${brandHtml}<section class="login-card">
    <span class="eyebrow">A private first look</span><h1>Your wedding album</h1><p>Enter the album review password supplied with this link. It is separate from your gallery password.</p>
    <form id="unlock"><label for="password">Album review password</label><input id="password" type="password" autocomplete="current-password" required autofocus>
    <button type="submit">Open album →</button><p class="error" role="alert">${escapeHtml(explanation)}</p></form><p class="small">If you don’t have the album password, ask your photographer for access.</p></section></main>`;
  document.getElementById('unlock').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await request('/unlock', 'POST', {password: document.getElementById('password').value});
      await load();
    } catch(error) { login(error.message); }
  });
}

function previewNotice() {
  if (!state.proof?.preview_only) return '';
  return `<div class="notice local-preview-notice"><strong>Local client preview</strong> · This is the same review flow your client sees. Changes and approval here stay on this PC and do not alter the published proof. <a href="${escapeHtml(state.proof.preview_studio_url)}">Return to Studio ↗</a></div>`;
}

function spreadHtml(spread, compact = false, edition = null, editable = false) {
  const layouts = {
    hero: [[0,0,1,1]], portrait_solo: [[.135,.085,.23,.83]],
    pair: [[.025,.045,.465,.91],[.51,.045,.465,.91]],
    portrait_pair: [[.135,.085,.23,.83],[.635,.085,.23,.83]],
    portrait_focus: [[.115,.08,.25,.84],[.53,.20,.44,.60]],
    feature: [[.025,.045,.56,.91],[.605,.045,.37,.445],[.605,.51,.37,.445]],
    triptych: [[.025,.045,.303,.91],[.348,.045,.304,.91],[.672,.045,.303,.91]],
    portrait_trio: [[.05,.085,.19,.83],[.27,.085,.19,.83],[.635,.085,.23,.83]],
    portrait_story: [[.12,.07,.25,.86],[.53,.07,.44,.40],[.53,.53,.44,.40]],
    grid: [[.025,.045,.465,.445],[.51,.045,.465,.445],[.025,.51,.465,.445],[.51,.51,.465,.445]],
    portrait_four: [[.05,.085,.19,.83],[.28,.085,.19,.83],[.55,.085,.19,.83],[.78,.085,.19,.83]],
    mosaic_four: [[.05,.085,.19,.83],[.28,.085,.19,.83],[.53,.07,.44,.40],[.53,.53,.44,.40]],
  };
  const rects = state.proof.layouts?.[spread.layout] || layouts[spread.layout] || [];
  return `<div class="spread" style="--spread-ratio:${edition?.spread_ratio || state.proof.offer?.spread_ratio || 69.7 / 25.5};aspect-ratio:var(--spread-ratio)">${rects.map(([x,y,w,h], i) => {
    const slot = spread.slots[i], photo = state.proof.photos.find(p => p.id === slot?.photo_id);
    return `<div class="frame ${editable?'editable-frame':''} ${editable && state.selectedSlot===i?'selected-frame':''}" ${editable ? `data-action="edit-slot" data-slot="${i}" role="button" tabindex="0" aria-label="Edit ${escapeHtml(photo?.name || 'photo')}"` : ''} style="left:${x*100}%;top:${y*100}%;width:${w*100}%;height:${h*100}%">
      ${photo ? `<img src="${base}/media/${photo.id}${compact ? '-thumb' : ''}.jpg" ${compact ? 'loading="lazy"' : ''} alt="${escapeHtml(photo.name)}" style="object-position:${(slot.focus_x??.5)*100}% ${(slot.focus_y??.5)*100}%">` : ''}</div>`;
  }).join('')}</div>`;
}

function saalCoverHtml(offer) {
  const coverId = state.proof.cover_photo_id || state.proof.spreads[0]?.slots[0]?.photo_id;
  const crop = state.proof.cover_crop || {focus_x:.5,focus_y:.5};
  return `<section class="client-cover-choice"><div class="saal-cover-preview" style="aspect-ratio:${offer.cover_ratio}"><img src="${base}/media/${coverId}.jpg" alt="Your Saal cover crop" style="object-position:${crop.focus_x*100}% ${crop.focus_y*100}%"></div><div><span class="eyebrow">Your cover</span><h2>Saal Professional Line · ${escapeHtml(offer.closed_size)}</h2><p>${escapeHtml(saalDescription)}</p><strong>€${offer.base_eur}</strong><p>Your photographer will confirm payment and ordering after approval.</p></div></section>`;
}

function comparisonHtml() {
  const editions = state.proof.offers || [];
  const measures = editions.map(item => {
    const [closedW, closedH] = item.closed_size.split('×').map(parseFloat);
    return {...item, closedW, closedH};
  });
  const gobook = measures.find(item => item.product === 'gobook');
  const edge = measures.find(item => item.product === 'edge');
  const areaDifference = gobook && edge ? Math.round((edge.closedW * edge.closedH / (gobook.closedW * gobook.closedH) - 1) * 100) : 0;
  const sizeComparison = `<section class="book-size-comparison" aria-label="Closed album size comparison">
    <div><span class="eyebrow">Relative size comparison</span><h2>See the difference in size</h2>
      <p>These closed-book outlines use the same scale. ${areaDifference > 0 ? `The EDGE cover is about ${areaDifference}% larger in area.` : 'Compare the dimensions of each book below.'}</p></div>
    <div class="size-comparison-books">${measures.map(item => `<div class="size-comparison-item">
      <div class="size-comparison-outline ${item.product === 'edge' ? 'size-edge' : 'size-gobook'}" style="--closed-width:${item.closedW};--closed-height:${item.closedH}" role="img" aria-label="${escapeHtml(item.product_label)} closed album, ${escapeHtml(item.closed_size)}"><span>${escapeHtml(item.product_label)}</span></div>
      <strong>${escapeHtml(item.product_label)}</strong><small>${escapeHtml(item.closed_size)} closed</small>
    </div>`).join('')}</div>
  </section>`;
  return `${sizeComparison}<div class="book-comparison">${measures.map(item => `<article class="book-choice ${state.selectedFormat===item.design_format?'active':''}">
    <span class="book-title"><strong>${escapeHtml(item.product_label)}</strong><span>€${item.base_eur}</span></span>
    <span class="book-edition-note">${item.product==='edge'?'Larger, display-led album · no box included':'Classic album presentation · GoBox included'}</span>
    <div class="book-mockup"><canvas data-mockup-format="${item.design_format}" width="${item.design_format==='gobook_portrait_25x35'?840:1280}" height="${item.design_format==='gobook_portrait_25x35'?1040:item.product==='edge'?item.design_format==='portrait_30x40'?956:900:853}" role="img" aria-label="Your photograph in the ${escapeHtml(item.product_label)} ${escapeHtml(item.closed_size)} scene">Your cover in the ${escapeHtml(item.product_label)} product scene</canvas>
      <div class="book-mockup-footer"><small>Neutral illustration of your chosen cover. Actual construction and finish may differ.</small>
      <a class="mockup-download" aria-disabled="true" download="${item.design_format}-album-mockup.jpg">Save JPG</a></div></div>
    <div class="book-highlights"><strong>${item.product==='edge'?'Why choose EDGE':'Why choose GoBook'}</strong>
      <ul>${(item.product==='edge' ? [
        'A larger cover and greater visual impact.',
        'Rigid, flush-mounted pages lie flat for panoramic photographs.',
        'Your cover photograph can continue onto the printed page edges.',
        'An acrylic photo cover gives it a modern, display-worthy finish.'
      ] : [
        'The more affordable way to keep your wedding story in print.',
        'The GoBox is included for presentation and safekeeping.',
        'Layflat photographic pages let images flow across a spread.',
        'A photo cover, GoColor spine and coordinated box give it a personal finish.'
      ]).map(point => `<li>${point}</li>`).join('')}</ul>
    </div>
    <a class="book-product-link" href="${item.product==='edge'?'https://www.graphistudio.com/en/the-edge/':'https://www.graphistudio.com/en/products/album/gobook/'}" target="_blank" rel="noopener noreferrer">Explore ${item.product==='edge'?'EDGE':'GoBook'} on Graphistudio ↗</a>
    <button type="button" class="book-select" data-action="format" data-format="${item.design_format}" aria-pressed="${state.selectedFormat===item.design_format}" ${state.proof.approved?'disabled':''}>${state.selectedFormat===item.design_format?'✓ Selected':'Choose this book'}</button>
  </article>`).join('')}</div><p class="mockup-note">The size outlines compare closed dimensions; your cover mockups are illustrative and are not shown to scale. Cover crop, finish, box colour, printed edges and final print colour are confirmed before production.</p>`;
}

async function paintMockups() {
  const coverId = state.proof.cover_photo_id || state.proof.spreads[0]?.slots[0]?.photo_id;
  if (!coverId) return;
  let renderMockup;
  try { ({renderMockup} = await import(`${base}/mockup.js`)); }
  catch { return; }
  for (const canvas of root.querySelectorAll('canvas[data-mockup-format]')) {
    renderMockup(canvas, canvas.dataset.mockupFormat,
      `${base}/media/${coverId}.jpg`, `${base}/product`).then(() => {
      if (canvas.isConnected && canvas.dataset.ready === 'true') {
        const link = canvas.closest('.book-mockup').querySelector('.mockup-download');
        link.href = canvas.toDataURL('image/jpeg',.92);
        link.removeAttribute('aria-disabled');
      }
    }).catch(() => {
      if (canvas.isConnected) canvas.closest('.book-mockup').querySelector('small').textContent =
        'This product scene could not load. The album-size comparison above is still available.';
    });
  }
}

function renderEditor() {
  const proof = state.proof, spread = currentSpread();
  const selected = spread.slots[state.selectedSlot];
  const selectedPhoto = proof.photos.find(photo => photo.id === selected?.photo_id);
  const coverPhoto = proof.photos.find(photo => photo.id === state.coverPhotoId);
  const query = state.galleryQuery.trim().toLowerCase();
  const matching = proof.photos.filter(photo => photo.name.toLowerCase().includes(query) ||
    String(photo.chapter || '').toLowerCase().includes(query));
  const shown = matching.slice(0, state.libraryLimit);
  root.innerHTML = `<header>${brandHtml}<div class="pill">Private proof · expires ${new Date(proof.expires_at).toLocaleDateString()}</div></header>
    <main class="client-editor"><button data-action="back-to-view">← Back to your album</button><div class="intro"><div><span class="eyebrow">Make it yours</span><h1>${escapeHtml(proof.title)}</h1>
    <p>Review the story first. Tap a frame, then choose a photo from your gallery. Add spreads and adjust the image position. Your photographer checks any edits before ordering.</p></div>
    <div class="pill">${state.draft.length} spreads · ${state.draft.length*2} printed sides</div></div>
    ${previewNotice()}
    ${proof.client_edits_pending ? '<div class="notice">Your latest edits are saved for photographer review. You can keep refining them; approval and checkout wait for a revised proof.</div>' : ''}
    <section class="client-cover-choice"><img src="${base}/media/${state.coverPhotoId}${isSaal()?'':'-thumb'}.jpg" ${isSaal()?`style="width:140px;height:${140/currentOffer().cover_ratio}px;aspect-ratio:${currentOffer().cover_ratio};object-fit:cover;object-position:${(state.coverPhotoId===proof.cover_photo_id?(proof.cover_crop?.focus_x??.5):.5)*100}% ${(state.coverPhotoId===proof.cover_photo_id?(proof.cover_crop?.focus_y??.5):.5)*100}%"`:''} alt="Current cover photograph${coverPhoto ? `: ${escapeHtml(coverPhoto.name)}` : ''}" loading="eager">
      <div><span class="eyebrow">Your album cover</span><h2>Choose your cover photo</h2>
      <p>${coverPhoto ? `Currently: ${escapeHtml(coverPhoto.name)}.` : 'Choose a photo from your wedding gallery.'} ${isSaal() ? 'Your photographer will check the cover crop before you approve.' : 'The same photograph appears on both book mockups after you finish editing.'}</p>
      <button type="button" data-action="choose-cover">${state.choosingCover ? 'Choosing a cover below…' : 'Choose or change cover photo →'}</button></div></section>
    <div class="editor-layout"><section><div class="editor-stage">${spreadHtml(spread, false, currentOffer(), true)}</div>
      <div class="toolbar"><button data-action="prev" ${state.active===0?'disabled':''}>← Previous</button><span>Spread ${state.active+1} of ${state.draft.length}${spread.chapter ? ` · ${escapeHtml(spread.chapter)}` : ''}</span>
      <button data-action="next" ${state.active===state.draft.length-1?'disabled':''}>Next →</button></div>
      <div class="strip">${state.draft.map((item,i) => `<button data-action="go" data-index="${i}" class="${i===state.active?'active':''}">${spreadHtml(item,true,currentOffer())}<span>${i+1}</span></button>`).join('')}</div>
      <div class="editor-actions"><button data-action="add-spread" ${state.draft.length>=40?'disabled':''}>＋ Add a spread after this</button>
      <button data-action="remove-spread" ${state.draft.length<=minimumSpreads()?'disabled':''}>Remove this spread</button></div>
      ${selected ? `<div class="crop-controls"><strong>Place ${escapeHtml(selectedPhoto?.name || 'this photo')}</strong><p>Drag the image within its frame, or use these controls.</p>
      <label>Left / right <input data-action="crop-x" type="range" min="0" max="100" value="${Math.round((selected.focus_x??.5)*100)}"></label>
      <label>Up / down <input data-action="crop-y" type="range" min="0" max="100" value="${Math.round((selected.focus_y??.5)*100)}"></label>
      <button data-action="reset-crop">Reset position</button></div>` : '<p class="small">Tap a photo on the spread to replace or reposition it.</p>'}</section>
      <aside class="gallery-picker" id="gallery-picker"><span class="eyebrow">Your wedding gallery</span><h2>${state.choosingCover ? 'Select your cover photo' : 'Choose the moments'}</h2>
      <p>${proof.gallery_photo_count ? `${proof.gallery_photo_count} preview photos from your linked PicPeak gallery.` : `${proof.photos.length} photos in this proof.`} ${state.choosingCover ? 'Tap any photo to use it on the album cover.' : 'Select a frame first to replace its photo.'}</p>
      ${state.choosingCover ? '<div class="add-instructions"><strong>Choosing the album cover</strong><p>Pick one photo below. Your photographer will review the choice before you approve the album.</p><button data-action="cancel-cover">Cancel cover choice</button></div>' : ''}
      ${state.adding ? `<div class="add-instructions"><strong>New spread</strong><p>Select 1–4 photos below, then add them after the current spread.</p>
      <button data-action="insert-spread" ${!state.addPhotos.length?'disabled':''}>Add spread with ${state.addPhotos.length} photo${state.addPhotos.length===1?'':'s'}</button>
      <button data-action="cancel-add">Cancel</button></div>` : ''}
      <input id="gallery-search" type="search" placeholder="Search filenames or moments…" value="${escapeHtml(state.galleryQuery)}" aria-label="Search gallery photos">
      <div class="gallery-grid">${shown.map(photo => `<button type="button" data-action="choose-gallery-photo" data-photo="${photo.id}" class="${state.addPhotos.includes(photo.id) || (state.choosingCover && photo.id === state.coverPhotoId)?'chosen':''}" title="${escapeHtml(photo.name)}">
      <img src="${base}/media/${photo.id}-thumb.jpg" alt="${escapeHtml(photo.name)}" loading="lazy"><span>${escapeHtml(photo.name)}${state.choosingCover && photo.id === state.coverPhotoId ? ' · Current cover' : ''}</span></button>`).join('')}</div>
      ${matching.length>shown.length ? `<button data-action="more-photos">Show more · ${matching.length-shown.length} remaining</button>` : ''}</aside></div>
    <div class="editor-finish"><div><strong>${state.dirty?'Unsaved album changes':'Ready for your next step'}</strong><p>${state.dirty?'Finishing saves the layout for your photographer to check.':'You can return to editing before approval.'}</p></div>
    <button class="primary" data-action="finish-layout">${state.dirty?'Save layout and view books →':'View book options →'}</button></div></main>`;
}

function coverRatio(offer) {
  const size = (offer?.closed_size || '').match(/([\d.]+)\s*[×x]\s*([\d.]+)/);
  return offer?.cover_ratio || (size ? Number(size[1]) / Number(size[2]) : 1);
}

function renderViewer() {
  const proof = state.proof, offer = currentOffer(), spread = proof.spreads[state.active];
  const coverId = proof.cover_photo_id || proof.spreads[0]?.slots[0]?.photo_id;
  const crop = proof.cover_crop || {focus_x:.5,focus_y:.5};
  const pending = proof.client_edits_pending || proof.feedback.length > 0;
  const focus = document.activeElement?.dataset.action;
  const focusIndex = document.activeElement?.dataset.index;
  const stripScroll = root.querySelector('.album-reader .strip')?.scrollLeft || 0;
  const approved = Boolean(proof.approved);
  root.innerHTML = `<header>${brandHtml}${proof.gallery_path ? `<a class="gallery-return" href="${escapeHtml(proof.gallery_path)}">← Back to your gallery</a>` : '<span class="pill">Your private album</span>'}</header>
    <main class="album-presentation">
      ${previewNotice()}
      <section class="album-welcome" aria-labelledby="album-title">
        <div class="album-cover-scene"><div class="album-cover" style="aspect-ratio:${coverRatio(offer)}"><img src="${base}/media/${coverId}.jpg" alt="Your album cover photograph" style="object-position:${crop.focus_x*100}% ${crop.focus_y*100}%"></div><span class="album-cover-caption">Your cover photograph</span></div>
        <div class="album-welcome-copy"><span class="eyebrow">Your day, brought together</span><h1 id="album-title">${escapeHtml(proof.title)}</h1>
          <p class="album-invitation">Your wedding album, already designed for you.</p><p>Take your time with the story we've created from your photographs. Enjoy it just as it is, or make it your own with a few changes.</p>
          <p class="album-specification">${proof.spreads.length} spreads · ${proof.spreads.length*2} pages${offer ? ` · ${escapeHtml(offer.closed_size)}` : ''}</p>
          ${offer ? `<p class="album-price">${escapeHtml(offer.product_label)} <strong>€${offer.base_eur}</strong>${isSaal() ? `<span class="album-description">${escapeHtml(saalDescription)}</span>` : ''}</p>` : ''}
          ${shapeChoicesHtml()}
          <div class="album-actions"><button class="primary" data-action="browse-album">Browse your album ↓</button><button data-action="view-options">${approved ? 'View approved details' : pending ? 'View book details' : 'I love this design →'}</button>${!approved ? '<button data-action="make-changes">Make changes</button>' : ''}</div>
          ${pending ? `<div class="notice" role="status">${pendingEditsMessage()}</div>` : approved ? '<div class="notice good" role="status">You have approved this exact album design. Your photographer will confirm the next steps.</div>' : '<p class="small">You can browse without making any changes. Approval comes after reviewing your book options.</p>'}
        </div>
      </section>
      <section class="album-reader ${state.viewerExpanded ? 'reader-expanded' : ''}" id="album-reader" aria-label="Your complete album" ${state.viewerExpanded ? 'role="dialog" aria-modal="true"' : ''}>
        <div class="reader-heading"><div><span class="eyebrow">${pending ? 'Your proposed design' : 'The complete story'}</span><h2>Your album</h2></div><button data-action="toggle-reader">${state.viewerExpanded ? 'Close full screen ✕' : 'Full screen ↗'}</button></div>
        <div class="stage reader-stage">${spreadHtml(spread, false, offer)}</div>
        <div class="toolbar"><button data-action="prev" aria-label="Previous spread" ${state.active===0?'disabled':''}>← Previous</button><span aria-live="polite">Spread ${state.active+1} of ${proof.spreads.length}${spread.chapter && spread.chapter !== 'Unsorted' ? ` · ${escapeHtml(spread.chapter)}` : ''}</span><button data-action="next" aria-label="Next spread" ${state.active===proof.spreads.length-1?'disabled':''}>Next →</button></div>
        <div class="strip" aria-label="All album spreads">${proof.spreads.map((item,i) => `<button data-action="go" data-index="${i}" aria-label="View spread ${i+1}" ${i===state.active?'aria-current="true"':''} class="${i===state.active?'active':''}">${spreadHtml(item,true,offer)}<span>${i+1}</span></button>`).join('')}</div>
      </section>
      <div class="album-next-step"><div><h2>A keepsake of your day</h2><p>${pending ? 'Your photographer will check your changes and prepare a revised proof.' : 'Happy with the story? Review the book, cover and price before you approve.'}</p></div><div class="album-actions"><button class="primary" data-action="view-options">${approved ? 'View approved details' : 'View book options →'}</button>${!approved ? '<button data-action="make-changes">Make changes</button>' : ''}</div></div>
    </main>`;
  document.body.style.overflow = state.viewerExpanded ? 'hidden' : '';
  const strip = root.querySelector('.album-reader .strip');
  if (strip) strip.scrollLeft = stripScroll;
  if (focus) {
    const index = focus === 'go' && focusIndex !== undefined ? `[data-index="${Number(focusIndex)}"]` : '';
    const target = root.querySelector(`[data-action="${focus}"]${index}:not(:disabled)`) ||
      (state.viewerExpanded ? root.querySelector('[data-action="toggle-reader"]') : null);
    target?.focus({preventScroll:true});
  }
}

function render() {
  document.body.style.overflow = '';
  if (state.step === 'view') return renderViewer();
  if (state.step === 'edit' && !state.proof.approved) return renderEditor();
  const proof = state.proof, spread = proof.spreads[state.active], pending = proof.feedback.length > 0;
  const offer = (proof.offers || []).find(item => item.design_format === state.selectedFormat) || proof.offer;
  const basePrice = offer?.base_eur ?? offer?.gobook_eur ?? 0;
  const productName = offer?.product_label || 'GoBook';
  const companionChoices = Object.entries(offer?.companions || {});
  const total = basePrice + (offer?.companion_eur || 0) * (state.companions.one_mid + state.companions.two_small / 2);
  const pendingLayout = Boolean(proof.client_edits_pending);
  const hasComparison = (proof.offers || []).length > 1;
  root.innerHTML = `<header>${brandHtml}<div class="pill">Private proof · expires ${new Date(proof.expires_at).toLocaleDateString()}</div></header>
    <main><div class="intro"><div><span class="eyebrow">${isSaal() ? 'Step 2 / Review your Saal album' : 'Step 2 / Compare your book options'}</span><h1>${escapeHtml(proof.title)}</h1>
    <p>${isSaal() ? 'Review your Professional Line album and cover. Your photographer will arrange payment and ordering.' : 'Compare your album as a GoBook or EDGE, then choose any extra copies you would like.'}</p><button data-action="back-to-view">← Browse your album</button>${!proof.approved?'<button data-action="back-to-edit">Make changes</button>':''}</div>
    <div class="pill">${proof.spreads.length} spreads · ${proof.spreads.length*2} printed sides</div></div>
    ${previewNotice()}
    ${pendingLayout ? `<div class="notice">${pendingEditsMessage()}</div>` : ''}
    ${shapeChoicesHtml()}
    ${isSaal() ? saalCoverHtml(offer) : ''}<div class="stage">${hasComparison ? comparisonHtml() : spreadHtml(spread)}</div>${hasComparison ? '' : `<div class="toolbar"><button data-action="prev" ${state.active===0?'disabled':''}>← Previous</button>
    <span>Spread ${state.active+1} of ${proof.spreads.length} · pages ${state.active*2+(isSaal()?1:2)}–${state.active*2+(isSaal()?2:3)}${spread.chapter ? ` · ${escapeHtml(spread.chapter)}` : ''}</span>
    <button data-action="next" ${state.active===proof.spreads.length-1?'disabled':''}>Next →</button></div>
    <div class="strip">${proof.spreads.map((item,i) => `<button data-action="go" data-index="${i}" class="${i===state.active?'active':''}">${spreadHtml(item,true,offer)}<span>${i+1}</span></button>`).join('')}</div>`}
    <div class="grid album-choice-only"><section><span class="eyebrow">Your album choice</span><h2>${offer ? `Your ${escapeHtml(productName)}${companionChoices.length ? ' and extra copies' : ''}` : 'Choose an edition'}</h2><p>${offer ? (companionChoices.length ? 'Choose a book above, then add as many companion copies as you need. Smaller copies come in pairs.' : 'Review this album size and design before approving.') : 'The same story can be prepared for either format.'}</p>
      ${offer ? `<div class="choices"><div class="choice active"><strong>${escapeHtml(productName)} · ${escapeHtml(offer.closed_size)}</strong><span>€${basePrice}</span></div>
        ${companionChoices.length ? `<div class="companion-quantity"><div><strong>${escapeHtml(offer.companions.one_mid.replace(/^One /, ''))}</strong><small>€${offer.companion_eur} per larger copy</small></div><label>Copies <input type="number" inputmode="numeric" min="0" max="1000" step="1" data-quantity="one_mid" value="${state.companions.one_mid}" ${proof.approved?'disabled':''}></label></div>
        <div class="companion-quantity"><div><strong>${escapeHtml(offer.companions.two_small.replace(/^Two /, ''))}</strong><small>€${offer.companion_eur} per pair · even numbers only</small></div><label>Copies <input type="number" inputmode="numeric" min="0" max="1000" step="2" data-quantity="two_small" value="${state.companions.two_small}" ${proof.approved?'disabled':''}></label></div>` : ''}</div>
        <div class="offer-total"><strong>Total proposal</strong><strong>€${total}</strong></div>` : `<div class="choices"><button class="choice ${state.product==='gobook'?'active':''}" data-action="product" data-product="gobook"><strong>GoBook · 35×25 cm</strong><span>€300</span></button>
      <button class="choice ${state.product==='wedding_book'?'active':''}" data-action="product" data-product="wedding_book"><strong>Original Wedding Book · 45×35 cm</strong><span>€500</span></button></div>`}
      ${Object.values(offer?.order_details || proof.order_details || {}).some(Boolean) ? `<div class="notice"><strong>Order choices</strong>${Object.entries(offer?.order_details || proof.order_details).filter(([,value])=>value).map(([key,value])=>`<p>${escapeHtml(key[0].toUpperCase()+key.slice(1))}: ${escapeHtml(value)}</p>`).join('')}</div>` : ''}
      ${proof.approved ? `<div class="notice good">Approved for ${escapeHtml(offer?.product_label || (proof.approved.product==='gobook'?'GoBook':'Original Wedding Book'))} ${isSaal() ? '' : `with ${state.companions.one_mid} larger and ${state.companions.two_small} smaller companion copies`} on ${new Date(proof.approved.at).toLocaleDateString()}. ${proof.payment?.status==='paid'?'Test payment recorded. Your photographer will prepare the order.':proof.checkout_available?'Your approved design is ready for secure Stripe test checkout.':'Your photographer will confirm ordering and payment.'}</div>
      ${proof.checkout_available && proof.payment?.status!=='paid' ? '<button class="primary wide" data-action="checkout">Continue to secure test checkout →</button><p class="small">Test mode only: no real charge will be taken. Price includes VAT and delivery within Ireland.</p>' : ''}` :
      `<div class="notice">${pendingLayout?'Your direct edits need photographer review before approval.':pending?'A previous change request is awaiting a revised proof before approval.':'Approval confirms this exact design and album edition. It does not place an order or take payment.'}</div>
      <button class="primary wide" data-action="approve" ${pendingLayout || pending || proof.spreads.length<minimumSpreads()?'disabled':''}>Approve this album →</button>`}</section></div></main>`;
  paintMockups();
}

async function load() {
  try { state.proof = await request('/api'); state.selectedFormat = state.proof.approved?.design_format || state.proof.design_format;
    state.product = state.proof.approved?.product || (state.proof.offers || []).find(item => item.design_format===state.selectedFormat)?.product || state.proof.offer?.product || 'gobook';
    state.companions = countsFromProof(state.proof);
    state.draft = clone(state.proof.spreads);
    state.coverPhotoId = state.proof.cover_photo_id || state.proof.spreads[0]?.slots[0]?.photo_id;
    state.choosingCover = false;
    state.step = 'view';
    state.dirty = false; render(); }
  catch(error) { login(error.message === 'Enter the review password' ? '' : error.message); }
}

document.addEventListener('change', event => {
  if (event.target.dataset.quantity) {
    const key = event.target.dataset.quantity, quantity = Number(event.target.value);
    if (!Number.isInteger(quantity) || quantity < 0 || quantity > 1000 || (key === 'two_small' && quantity % 2)) {
      alert(key === 'two_small' ? 'Smaller companion books must be ordered in pairs.' : 'Enter a whole number of copies.');
      event.target.value = state.companions[key]; return;
    }
    state.companions[key] = quantity; render();
  }
  if (event.target.dataset.action === 'crop-x' || event.target.dataset.action === 'crop-y') {
    const slot = currentSpread()?.slots[state.selectedSlot];
    if (!slot) return;
    slot[event.target.dataset.action === 'crop-x' ? 'focus_x' : 'focus_y'] = Number(event.target.value) / 100;
    state.dirty = true; render();
  }
});
document.addEventListener('input', event => {
  if (event.target.dataset.quantity) {
    const key = event.target.dataset.quantity, quantity = Number(event.target.value);
    if (event.target.value !== '' && Number.isInteger(quantity) && quantity >= 0 && quantity <= 1000 &&
        (key !== 'two_small' || quantity % 2 === 0)) {
      state.companions[key] = quantity;
      const total = (currentOffer()?.base_eur || 0) + (currentOffer()?.companion_eur || 0) *
        (state.companions.one_mid + state.companions.two_small / 2);
      const node = document.querySelector('.offer-total strong:last-child');
      if (node) node.textContent = `€${total}`;
    }
    return;
  }
  if (event.target.id !== 'gallery-search') return;
  const position = event.target.selectionStart;
  state.galleryQuery = event.target.value;
  state.libraryLimit = 80;
  render();
  document.getElementById('gallery-search')?.focus();
  document.getElementById('gallery-search')?.setSelectionRange(position, position);
});
let cropDrag = null;
document.addEventListener('pointerdown', event => {
  const frame = event.target.closest('.editable-frame');
  if (!frame || state.step !== 'edit') return;
  const slotIndex = Number(frame.dataset.slot), slot = currentSpread()?.slots[slotIndex];
  if (!slot) return;
  cropDrag = {slotIndex, x:event.clientX, y:event.clientY,
              focusX:slot.focus_x??.5, focusY:slot.focus_y??.5,
              width:frame.clientWidth, height:frame.clientHeight};
  frame.setPointerCapture?.(event.pointerId);
});
document.addEventListener('pointermove', event => {
  if (!cropDrag || state.step !== 'edit') return;
  const drag = cropDrag;
  const x = Math.max(0,Math.min(1,drag.focusX-(event.clientX-drag.x)/Math.max(1,drag.width)));
  const y = Math.max(0,Math.min(1,drag.focusY-(event.clientY-drag.y)/Math.max(1,drag.height)));
  const image = document.querySelector(`.editor-stage .editable-frame[data-slot="${drag.slotIndex}"] img`);
  if (image) image.style.objectPosition = `${x*100}% ${y*100}%`;
});
document.addEventListener('pointerup', event => {
  if (!cropDrag || state.step !== 'edit') return;
  const drag = cropDrag; cropDrag = null;
  const dx = event.clientX-drag.x, dy = event.clientY-drag.y;
  if (Math.abs(dx)+Math.abs(dy) < 5) return;
  const slot = currentSpread()?.slots[drag.slotIndex];
  if (!slot) return;
  slot.focus_x = Math.max(0,Math.min(1,drag.focusX-dx/Math.max(1,drag.width)));
  slot.focus_y = Math.max(0,Math.min(1,drag.focusY-dy/Math.max(1,drag.height)));
  state.selectedSlot = drag.slotIndex;
  state.dirty = true;
  render();
});
window.addEventListener('beforeunload', event => {
  if (!state.dirty) return;
  event.preventDefault();
  event.returnValue = '';
});
document.addEventListener('click', async event => {
  const element = event.target.closest('[data-action]'); if (!element || !state.proof) return;
  const action = element.dataset.action;
  if (action === 'shape') { await changeShape(element.dataset.format); return; }
  if (action === 'browse-album') { document.getElementById('album-reader')?.scrollIntoView({behavior:'smooth',block:'start'}); return; }
  if (action === 'toggle-reader') { state.viewerExpanded = !state.viewerExpanded; render(); root.querySelector('[data-action="toggle-reader"]')?.focus(); return; }
  if (action === 'view-options') { state.viewerExpanded = false; state.step = 'proposal'; render(); window.scrollTo(0,0); return; }
  if (action === 'make-changes' && !state.proof.approved) { state.viewerExpanded = false; state.step = 'edit'; render(); window.scrollTo(0,0); return; }
  if (action === 'back-to-view') {
    if (state.dirty && !confirm('Discard your unsaved changes and return to the album?')) return;
    state.draft = clone(state.proof.spreads); state.coverPhotoId = state.proof.cover_photo_id;
    state.dirty = false; state.viewerExpanded = false; state.active = Math.min(state.active,state.proof.spreads.length-1);
    state.step = 'view'; render(); window.scrollTo(0,0); return;
  }
  if (action === 'back-to-edit' && !state.proof.approved) { state.step = 'edit'; render(); return; }
  if (action === 'edit-slot' && state.step === 'edit') { state.selectedSlot = Number(element.dataset.slot); state.adding = false; state.choosingCover = false; render(); return; }
  if (action === 'choose-cover' && state.step === 'edit') {
    state.choosingCover = true; state.adding = false; state.addPhotos = []; state.selectedSlot = null;
    render(); document.getElementById('gallery-picker')?.scrollIntoView({behavior:'smooth',block:'start'}); return;
  }
  if (action === 'cancel-cover' && state.step === 'edit') { state.choosingCover = false; render(); return; }
  if (action === 'add-spread' && state.step === 'edit') { state.adding = true; state.addPhotos = []; state.selectedSlot = null; state.choosingCover = false; render(); return; }
  if (action === 'cancel-add') { state.adding = false; state.addPhotos = []; render(); return; }
  if (action === 'more-photos') { state.libraryLimit += 80; render(); return; }
  if (action === 'choose-gallery-photo' && state.step === 'edit') {
    const id = element.dataset.photo;
    if (state.choosingCover) {
      if (state.coverPhotoId !== id) { state.coverPhotoId = id; state.dirty = true; }
      state.choosingCover = false; render(); document.querySelector('.client-cover-choice')?.scrollIntoView({behavior:'smooth',block:'start'}); return;
    }
    if (state.adding) {
      state.addPhotos = state.addPhotos.includes(id) ? state.addPhotos.filter(photo=>photo!==id) :
        state.addPhotos.length < 4 ? [...state.addPhotos,id] : state.addPhotos;
      render(); return;
    }
    const slot = currentSpread()?.slots[state.selectedSlot];
    if (!slot) { alert('Tap a photo on the album spread first, then choose its replacement.'); return; }
    slot.photo_id = id; slot.focus_x = .5; slot.focus_y = .5; state.dirty = true; render(); return;
  }
  if (action === 'insert-spread' && state.adding && state.addPhotos.length && state.draft.length < 40) {
    const layouts = {1:'hero',2:'pair',3:'feature',4:'grid'};
    state.draft.splice(state.active+1,0,{id:crypto.randomUUID(),layout:layouts[state.addPhotos.length],
      slots:state.addPhotos.map(photo_id=>({photo_id,focus_x:.5,focus_y:.5})),chapter:currentSpread()?.chapter||''});
    state.active++; state.selectedSlot = null; state.adding = false; state.addPhotos = [];
    state.dirty = true; render(); return;
  }
  if (action === 'remove-spread' && state.step === 'edit') {
    if (state.draft.length <= minimumSpreads() || !confirm('Remove this spread from the album?')) return;
    state.draft.splice(state.active,1); state.active = Math.min(state.active,state.draft.length-1);
    state.selectedSlot = null; state.dirty = true; render(); return;
  }
  if (action === 'reset-crop' && state.step === 'edit') {
    const slot = currentSpread()?.slots[state.selectedSlot];
    if (slot) { slot.focus_x=.5; slot.focus_y=.5; state.dirty=true; render(); } return;
  }
  if (action === 'finish-layout' && state.step === 'edit') {
    try {
      if (state.dirty) {
        state.proof = await request('/layout','PUT',{revision:state.proof.layout_revision||0,spreads:state.draft,cover_photo_id:state.coverPhotoId});
        state.draft = clone(state.proof.spreads); state.dirty = false;
      }
      state.step = 'proposal'; render();
    } catch(error) { alert(error.message); }
    return;
  }
  if (action === 'prev') state.active = Math.max(0,state.active-1);
  if (action === 'next') state.active = Math.min((state.step==='edit'?state.draft:state.proof.spreads).length-1,state.active+1);
  if (action === 'go') state.active = Number(element.dataset.index);
  if (action === 'product') state.product = element.dataset.product;
  if (action === 'format' && !state.proof.approved) {
    const chosen = state.proof.offers?.find(item => item.design_format === element.dataset.format);
    if (!chosen) return;
    state.selectedFormat = chosen.design_format;
    state.product = chosen.product;
  }
  if (['prev','next','go','product','format'].includes(action)) { state.selectedSlot=null; render(); return; }
  try {
    if (action === 'approve') {
      if (!confirm('Approve this exact album design and selected edition?')) return;
      await request('/approve', 'POST', {product:state.product,
        companion_quantities:state.companions,
        design_format:state.selectedFormat});
      state.proof = await request('/api'); render();
    }
    if (action === 'checkout') {
      const session = await request('/checkout', 'POST', {});
      if (!session.url?.startsWith('https://checkout.stripe.com/')) throw new Error('Unexpected checkout destination');
      location.assign(session.url);
    }
  } catch(error) { alert(error.message); }
});
document.addEventListener('keydown', event => {
  if (state.step !== 'view' || !state.proof) return;
  if (event.key === 'Escape' && state.viewerExpanded) {
    state.viewerExpanded = false; render(); root.querySelector('[data-action="toggle-reader"]')?.focus(); return;
  }
  if (state.viewerExpanded && event.key === 'Tab') {
    const controls = [...root.querySelectorAll('.album-reader button:not(:disabled)')];
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
  if (!['ArrowLeft','ArrowRight'].includes(event.key) || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
  event.preventDefault();
  state.active = Math.max(0, Math.min(state.proof.spreads.length-1, state.active + (event.key==='ArrowLeft'?-1:1)));
  render();
});
load();
