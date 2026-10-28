import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../crm_bridge.cjs',import.meta.url),'utf8');

function fixture() {
  const records = [], calls = [];
  const config = {flag:true,permissions:['quotes.manage'],active:true,customer:2};
  const db = table => {
    const query = {filters:[],where(...args){this.filters.push(args);return this;},join(){return this;},
      async select(){
        if(table==='admin_users') return [{id:1,username:'Synthetic admin',is_active:config.active}];
        if(table==='customer_accounts as c') return [{id:config.customer,display_name:'Synthetic customer',is_active:true}];
        throw Error('Unexpected select '+table);
      },async first(){
        if(table==='feature_flags') return {value:config.flag};
        if(table==='events') return {id:7};
        if(table==='business_profile') return {company_name:'Synthetic studio',address_line1:'Test road',country_code:'IE'};
        if(table==='quotes') {
          const filter=this.filters[0];
          return typeof filter[0]==='object' ? records.find(row=>row.id===filter[0].id) :
            records.find(row=>row.internal_notes.startsWith(filter[2].slice(0,-1)));
        }
        throw Error('Unexpected first '+table);
      }};
    return query;
  };
  db.schema={hasTable:async()=>true}; db.client={config:{client:'pg'},acquireConnection:async()=>({}),releaseConnection:async()=>{}};
  db.raw=()=>({connection:async()=>{}}); db.destroy=async()=>{};
  const service={_internal:{computeTotals:(items,vat)=>{
    const net=items[0].unit_price_minor;
    return {totalAmountMinor:net+Math.round(net*vat/100)};
  }},createQuote:async(payload,admin)=>{
    calls.push({payload,admin});
    records.push({id:records.length+1,quote_number:'Q-SYNTHETIC',status:'draft',
      customer_account_id:payload.customerAccountId,created_by_admin_id:admin,vat_rate:payload.vatRate,internal_notes:payload.internalNotes});
    return records.length;
  }};
  const run=request=>new Promise((resolve,reject)=>{
    const sandbox={albumRequest:request,require:name=>{
      if(name.endsWith('/database/db'))return {db};
      if(name.endsWith('/middleware/permissions'))return {getUserPermissions:async()=>({permissions:config.permissions})};
      if(name.endsWith('/services/quoteService'))return service;
      if(name.endsWith('/utils/appSettings'))return {getAppSetting:async()=>false};
      throw Error('Unexpected import '+name);
    },setTimeout,clearTimeout,process:{exit:()=>reject(Error('deadline')),stdout:{write:text=>{
      if(text.startsWith('\nALBUM_RESULT:'))resolve(JSON.parse(text.slice(14)));
    }}}};
    vm.runInNewContext(source,sandbox,{filename:'crm_bridge.cjs'});
  });
  return {config,records,calls,run};
}
const agreement={project_id:'11111111-1111-4111-8111-111111111111',sha256:'a'.repeat(64),
  total_minor:15000,offer:{product_label:'Synthetic album',closed_size:'22×30 cm'}};
const request={operation:'draft',event_id:7,admin_id:1,customer_id:2,vat_rate:23,
  agreement,summary:'Synthetic frozen specification',title:'Synthetic album'};

const ready=fixture();
assert.equal((await ready.run({...request,operation:'status'})).value.ready,true);
assert.equal((await ready.run(request)).value.status,'draft');
assert.equal(ready.calls.length,1);
assert.equal(ready.calls[0].payload.currency,'EUR');
assert.equal(ready.calls[0].payload.lineItems[0].details_text,request.summary);
assert.ok(ready.calls[0].payload.internalNotes.includes(JSON.stringify(agreement)));
assert.equal((await ready.run(request)).value.existing,true);
assert.equal(ready.calls.length,1,'lost-response retries must not create another quote');
assert.equal((await ready.run({...request,vat_rate:0})).ok,false,'tax details cannot silently change an existing handoff');

const disabled=fixture(); disabled.config.flag=false;
assert.match((await disabled.run(request)).error,/Enable Quotes/);
assert.equal(disabled.calls.length,0);
const forbidden=fixture(); forbidden.config.permissions=[];
assert.equal((await forbidden.run(request)).ok,false);
assert.equal(forbidden.calls.length,0);
const foreign=fixture();
assert.match((await foreign.run({...request,customer_id:99})).error,/assigned to this gallery/);
assert.equal(foreign.calls.length,0);
const inactive=fixture(); inactive.config.active=false;
assert.equal((await inactive.run(request)).ok,false);
assert.equal(inactive.calls.length,0);
console.log('CRM draft: permissions, feature flag, customer scoping, frozen totals, evidence and retry tests passed');
