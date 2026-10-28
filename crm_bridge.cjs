// Owner-only desktop integration. Native services retain all financial writes.
'use strict';
const {db} = require('/app/src/database/db');
const {getUserPermissions} = require('/app/src/middleware/permissions');
const quoteService = require('/app/src/services/quoteService');
const {getAppSetting} = require('/app/src/utils/appSettings');
const enabled = value => [true,1,'1'].includes(value);
const disabled = value => [false,0,'0'].includes(value);
const fail = message => {throw Error(message);};

async function context(request) {
  const required = ['quotes','customer_accounts','event_customer_assignments','business_profile'];
  for (const table of required) if (!await db.schema.hasTable(table))
    return {ready:false, reason:'This PicPeak version needs its CRM tables before connecting Studio.', admins:[], customers:[]};
  const flag = await db('feature_flags').where({key:'quotes'}).first();
  const admins = [];
  for (const admin of await db('admin_users').select('*')) {
    if (disabled(admin.is_active) || enabled(admin.must_change_password)) continue;
    const permissions = await getUserPermissions(admin.id);
    if (permissions.permissions.includes('quotes.manage')) admins.push({id:admin.id,label:admin.username || admin.email});
  }
  const event = request.event_id ? await db('events').where({id:request.event_id}).first() : null;
  const customers = event ? await db('customer_accounts as c')
    .join('event_customer_assignments as a','a.customer_account_id','c.id')
    .where('a.event_id',event.id).select('c.*') : [];
  const eligible = customers.filter(row => !disabled(row.is_active) && !disabled(row.feature_quotes))
    .map(row => ({id:row.id,label:row.display_name || [row.first_name,row.last_name].filter(Boolean).join(' ') || row.email}));
  const profile = await db('business_profile').where({id:1}).first();
  const reason = !enabled(flag?.value) ? 'Enable Quotes in PicPeak settings when your CRM is ready.' :
    !admins.length ? 'An active PicPeak administrator needs quotes.manage permission.' :
    !profile?.company_name || !profile?.address_line1 || !profile?.country_code ? 'Complete the business profile in PicPeak before creating a quote.' :
    !event ? 'Link this album to a PicPeak gallery before creating its CRM quote.' :
    !eligible.length ? 'Assign an active customer with Quotes enabled to this gallery in PicPeak.' : null;
  return {ready:!reason,reason,admins,customers:eligible,vat_rate:profile?.vat_rate_default ?? null};
}

async function createDraft(request, status) {
  if (!status.ready) fail(status.reason);
  if (!status.admins.some(row => row.id === request.admin_id)) fail('Choose an active administrator with quotes.manage permission.');
  if (!status.customers.some(row => row.id === request.customer_id)) fail('Choose a customer assigned to this gallery.');
  const record = request.agreement;
  if (!/^[a-f0-9]{64}$/.test(record?.sha256 || '') || !/^[a-f0-9-]{36}$/.test(record?.project_id || '')) fail('Invalid approval reference');
  if (!Number.isFinite(request.vat_rate) || request.vat_rate < 0 || request.vat_rate > 100) fail('Confirm the VAT rate before creating a quote.');
  const marker = `[album-studio:${record.project_id}:${record.sha256}]`;
  // A session advisory lock survives the native service's own transaction.
  // It also makes retries after a lost HTTP response safe across Studio restarts.
  if (db.client.config.client !== 'pg') fail('Studio CRM draft creation requires PostgreSQL. The approval download remains available.');
  const connection = await db.client.acquireConnection();
  try {
    await db.raw('SELECT pg_advisory_lock(hashtext(?))',[marker]).connection(connection);
    const existing = await db('quotes').where('internal_notes','like',marker + '%').first();
    if (existing) {
      if (Number(existing.customer_account_id) !== request.customer_id || Number(existing.created_by_admin_id) !== request.admin_id || Number(existing.vat_rate) !== request.vat_rate)
        fail('This approval already has a CRM quote with different customer or tax details. Open that quote in PicPeak.');
      return {id:existing.id,number:existing.quote_number,status:existing.status,existing:true,agreement_sha256:record.sha256};
    }
    const gross = record.total_minor;
    const centre = Math.round(gross / (1 + request.vat_rate / 100));
    const roundTotal = (await getAppSetting('crm_invoice_round_total',false)) === true;
    let lineItems;
    for (let offset = -2; offset <= 2; offset++) {
      const candidate = [{description:`${record.offer.product_label} · ${record.offer.closed_size}`,
        quantity:1, unit_price_minor:centre+offset, discount_percent:0, position:0,
        details_text:request.summary}];
      if (quoteService._internal.computeTotals(candidate,request.vat_rate,0,{roundTotal}).totalAmountMinor === gross) {lineItems=candidate;break;}
    }
    if (!lineItems) fail('PicPeak rounding cannot preserve the approved total at this VAT rate. Review CRM tax and rounding settings.');
    const id = await quoteService.createQuote({customerAccountId:request.customer_id,
      eventId:request.event_id,eventName:request.title,currency:'EUR',language:'en',
      vatRate:request.vat_rate,shippingAmountMinor:0,lineItems,
      internalNotes:marker + '\n' + JSON.stringify(record)},request.admin_id);
    const quote = await db('quotes').where({id}).first();
    return {id:quote.id,number:quote.quote_number,status:quote.status,existing:false,agreement_sha256:record.sha256};
  } finally {
    try {await db.raw('SELECT pg_advisory_unlock(hashtext(?))',[marker]).connection(connection);}
    finally {await db.client.releaseConnection(connection);}
  }
}

async function main() {
  const status = await context(albumRequest);
  return albumRequest.operation === 'status' ? status : await createDraft(albumRequest,status);
}
const deadline = setTimeout(()=>process.exit(124),45000);
main().then(value=>process.stdout.write('\nALBUM_RESULT:' + JSON.stringify({ok:true,value}) + '\n'))
  .catch(error=>process.stdout.write('\nALBUM_RESULT:' + JSON.stringify({ok:false,error:error.message}) + '\n'))
  .finally(async()=>{clearTimeout(deadline);await db.destroy();});
