const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;',
})[char]);

export function priceMinor(value) {
  if (!/^\d{1,6}(?:\.\d{1,2})?$/.test(value.trim())) throw Error('Use a price such as 150 or 150.50.');
  const [whole, fraction=''] = value.trim().split('.');
  const amount = Number(whole) * 100 + Number(fraction.padEnd(2,'0'));
  if (amount > 10000000) throw Error('The price is too large.');
  return amount;
}

export function renderCatalogue(catalogue, offers) {
  if (!catalogue) return '';
  return `<div class="modal-backdrop"><section class="modal catalogue-dialog" role="dialog" aria-modal="true" aria-labelledby="catalogue-title">
    <div class="modal-head"><div><span class="eyebrow">Photographer settings</span><h2 id="catalogue-title">Album catalogue</h2></div><button class="btn btn-ghost" data-action="close-catalogue">Close</button></div>
    <p>Prices and offered cover, box and finish choices are shared across Studio. Existing client proofs keep their original offer. Printer dimensions stay fixed to the verified profiles.</p>
    ${Object.entries(catalogue.products).map(([key,item])=>`<fieldset data-catalogue-product="${key}" class="catalogue-product"><legend>${escapeHtml(offers[key]?.product_label)} · ${escapeHtml(offers[key]?.closed_size)}</legend>
      <label><input type="checkbox" data-field="enabled" ${item.enabled?'checked':''}> Offer this album</label>
      <label class="field-label">Name<input class="input" data-field="label" maxlength="80" value="${escapeHtml(item.label)}"></label>
      <div class="row"><label class="field-label">Album price (€)<input class="input" inputmode="decimal" data-field="price_minor" value="${(item.price_minor/100).toFixed(2)}"></label>
      ${Object.keys(offers[key]?.companions || {}).length?`<label class="field-label">Larger copy / smaller pair (€)<input class="input" inputmode="decimal" data-field="companion_price_minor" value="${(item.companion_price_minor/100).toFixed(2)}"></label>`:''}</div>
      ${['cover','box','finish'].map(field=>`<label class="field-label">${field[0].toUpperCase()+field.slice(1)} choices · one per line<textarea class="input" data-choice="${field}" rows="2" ${key.startsWith('saal_')?'readonly':''}>${escapeHtml(item.choices[field].join('\n'))}</textarea></label>`).join('')}
      <p class="small muted">${key.startsWith('saal_')?'This Saal construction is fixed by its verified print profile.':'Include the colour in the cover or box name. Leave a list empty to use photographer notes. These choices do not add a price surcharge.'}</p>
    </fieldset>`).join('')}
    <div class="modal-foot"><button class="btn btn-secondary" data-action="close-catalogue">Cancel</button><button class="btn btn-primary" data-action="save-catalogue">Save catalogue</button></div>
  </section></div>`;
}

export function readCatalogue(root, current) {
  const value = structuredClone(current);
  for (const node of root.querySelectorAll('[data-catalogue-product]')) {
    const item = value.products[node.dataset.catalogueProduct];
    item.enabled = node.querySelector('[data-field="enabled"]').checked;
    item.label = node.querySelector('[data-field="label"]').value.trim();
    item.price_minor = priceMinor(node.querySelector('[data-field="price_minor"]').value);
    const companion = node.querySelector('[data-field="companion_price_minor"]');
    if (companion) item.companion_price_minor = priceMinor(companion.value);
    for (const field of ['cover','box','finish']) item.choices[field] =
      node.querySelector(`[data-choice="${field}"]`).value.split('\n').map(text=>text.trim()).filter(Boolean);
  }
  return value;
}

export function renderOrderChoices(offer, details) {
  return `<div class="order-fields">${['cover','box','finish'].map(field=>{
    const choices = offer?.order_choices?.[field] || [];
    const value = details[field] || (choices.length===1?choices[0]:'');
    const control = choices.length ? `<select id="order-${field}" class="select"><option value="">Choose ${field}</option>${choices.map(choice=>`<option ${choice===value?'selected':''}>${escapeHtml(choice)}</option>`).join('')}${value&&!choices.includes(value)?`<option selected>${escapeHtml(value)}</option>`:''}</select>` :
      `<input id="order-${field}" class="input" maxlength="200" value="${escapeHtml(value)}">`;
    return `<label>${field[0].toUpperCase()+field.slice(1)} / colour${control}</label>`;
  }).join('')}</div>`;
}

export function renderApprovalPanel(project, crm) {
  const approved = project.status==='approved' && project.approval_record;
  const names = {published:'Proof published',proof_published:'Proof published',approved:'Client approved',
    approval_adopted:'Approval confirmed for print',design_changed:'Design or order choices changed',
    review_required:'Fresh review required',crm_draft_created:'CRM draft prepared',client_layout_adopted:'Client layout adopted'};
  const history = project.history || [];
  return `<section class="workflow-card" data-key="approval-record"><span class="eyebrow">Order record & CRM</span><h2>Keep the approved order together</h2>
    ${project.sharing_pending?'<p class="check-error" role="alert">A review link still needs to be withdrawn. Check the Pi connection, then retry fetching originals before sharing or ordering.</p>':''}
    <p class="small muted">${project.sources_checked_at?`Original photos checked ${escapeHtml(new Date(project.sources_checked_at).toLocaleString())}.`:'Original photos are checked when preparing a proof or print export.'} Checks run on demand.</p>
    ${approved?`<p>Approved total: €${(project.approval_record.total_minor/100).toFixed(2)}. The approval download includes the product, choices, price and exact photo versions.</p><a class="btn btn-secondary" href="/api/projects/${project.id}/approval-record" download="album-approval-${project.id}.json">Download approval record</a>
      <button class="small-btn" data-action="check-crm">Check PicPeak CRM</button>`:'<p>Publish a protected proof and confirm the client’s approval to prepare a CRM quote.</p>'}
    ${project.crm_quote?`<p>CRM quote ${escapeHtml(project.crm_quote.number)} was prepared for ${project.crm_quote.agreement_sha256===project.approval_record?.sha256?'this approval':'an earlier approval'}. Open PicPeak CRM to review its current status.</p>`:''}
    ${crm?`<p ${crm.ready?'':'class="notice"'}>${escapeHtml(crm.reason || 'Create an unsent draft, then review it in PicPeak before sending or converting it.')}</p>`:''}
    ${approved && crm?.ready?`<div class="order-fields"><label>PicPeak administrator<select id="crm-admin" class="select"><option value="">Choose administrator</option>${crm.admins.map(row=>`<option value="${row.id}">${escapeHtml(row.label)}</option>`).join('')}</select></label>
      <label>Gallery customer<select id="crm-customer" class="select"><option value="">Choose customer</option>${crm.customers.map(row=>`<option value="${row.id}">${escapeHtml(row.label)}</option>`).join('')}</select></label>
      <label>Confirmed VAT rate (%)<input id="crm-vat" class="input" inputmode="decimal" placeholder="Confirm your VAT rate"></label></div><p class="small muted">The approved total stays the same, including VAT. Delivery is not added by Studio.</p><button class="btn btn-secondary" data-action="create-crm-quote">Create CRM draft quote</button>`:''}
    ${history.length?`<details><summary>Review history (${history.length})</summary>${history.slice(-30).reverse().map(row=>`<p class="small">${escapeHtml(new Date(row.at).toLocaleString())} · ${escapeHtml(names[row.event] || row.event)}${row.reason?` · ${escapeHtml(row.reason)}`:''}</p>`).join('')}</details>`:''}
  </section>`;
}
