import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/review-app.js',import.meta.url),'utf8');
const offer = (format,size,ratio,price=15000) => ({design_format:format,closed_size:size,
  product:'saal',product_label:'Saal Professional Line',currency:'EUR',base_minor:price,
  base_eur:price/100,companion_eur:0,companions:{},spread_ratio:ratio,minimum_spreads:13});
async function client({fail=false}={}) {
  const portrait=offer('saal_portrait_22x30','22×30 cm',438/294);
  const landscape=offer('saal_landscape_30x21','30×21 cm',588/204);
  const proof={title:'Synthetic wedding',design_format:portrait.design_format,offer:portrait,offers:[portrait],
    shape_offers:[portrait,landscape,offer('too-expensive','30×30 cm',2,17500)],photos:[],feedback:[],
    cover_photo_id:'synthetic-cover',spreads:Array.from({length:13},(_,index)=>({id:String(index),layout:'hero',slots:[]}))};
  const events={}, writes=[], alerts=[];
  const root={innerHTML:'',querySelector:()=>null,querySelectorAll:()=>[]};
  const context=vm.createContext({location:{pathname:'/review/synthetic'},console,
    document:{getElementById:()=>root,body:{style:{}},addEventListener:(name,fn)=>{events[name]=fn;}},
    window:{addEventListener:()=>{},scrollTo:()=>{}},alert:message=>alerts.push(message),
    fetch:async(url,options)=>{
      if(options.method==='PUT') {
        const body=JSON.parse(options.body);writes.push(body);
        if(fail)return {ok:false,status:400,json:async()=>({error:'This album changed in another tab'})};
        Object.assign(proof,{design_format:landscape.design_format,offer:landscape,offers:[landscape],
          layout_revision:1,client_edits_pending:true,shape_review_required:true});
      }
      return {ok:true,json:async()=>structuredClone(proof)};
    }});
  vm.runInContext(source.replace(/load\(\);\s*$/,'')+'\nthis.clientState=state;this.start=load;',context);
  await context.start();
  const click = data => events.click({target:{closest:()=>({dataset:data})}});
  return {context,root,writes,alerts,click};
}

test('shape click persists the chosen format, returns to the first spread and keeps approval blocked',async()=>{
  const c=await client();
  await c.click({action:'go',index:'9'});
  assert.equal(c.context.clientState.active,9);
  assert.match(c.root.innerHTML,/Spread 10 of 13/);
  assert.doesNotMatch(c.root.innerHTML,/data-format="too-expensive"/);
  await c.click({action:'shape',format:'saal_landscape_30x21'});
  assert.equal(c.writes.length,1);
  assert.equal(c.writes[0].from_format,'saal_portrait_22x30');
  assert.equal(c.writes[0].design_format,'saal_landscape_30x21');
  assert.equal(c.context.clientState.active,0);
  assert.match(c.root.innerHTML,/same €150 album price/);
  assert.match(c.root.innerHTML,/Review the cover and every spread again/);
  assert.match(c.root.innerHTML,/Spread 1 of 13/);
  assert.match(c.root.innerHTML,/--spread-ratio:2\.882/);
  await c.click({action:'view-options'});
  assert.match(c.root.innerHTML,/data-action="approve" disabled/);
});

test('a revision conflict keeps the existing shape and page instead of claiming success',async()=>{
  const c=await client({fail:true});
  await c.click({action:'go',index:'9'});
  await c.click({action:'shape',format:'saal_landscape_30x21'});
  assert.equal(c.context.clientState.selectedFormat,'saal_portrait_22x30');
  assert.equal(c.context.clientState.active,9);
  assert.equal(c.context.clientState.changingShape,false);
  assert.deepEqual(c.alerts,['This album changed in another tab']);
});
